"""One participant turn with ephemeral READ loop and ordered commit boundary."""
import copy
import ast
import re
import uuid
from dataclasses import dataclass
from difflib import SequenceMatcher
from jsonschema import Draft202012Validator

from .arbiter import SceneEvaluationError, WriteArbiter
from .events import EventStore
from .gateway import ModelGateway, ModelExhausted, BudgetExhausted
from .models import ActionPayload, EventVisibility, ExecutionFeedback, MessageEntry, Perspective, PrivateAgentState, SceneRuntimeConfig, SessionGrant
from .projection import ProjectionService

MAX_ACTION_CORRECTION_RETRIES = 1
MAX_SPEECH_REGENERATION_CALLS = 1


@dataclass
class TurnResult:
    completed: bool
    should_terminate: bool = False
    early_semantic_requested: bool = False
    failure_outcome: str | None = None
    last_event_seq: int = 0


class TurnExecutor:
    def __init__(self, scene: SceneRuntimeConfig, gateway: ModelGateway, projection: ProjectionService, arbiter: WriteArbiter):
        self.scene = scene
        self.gateway = gateway
        self.projection = projection
        self.arbiter = arbiter

    async def execute_turn(self, actor: str, world: dict, private_states: dict[str, PrivateAgentState],
                           messages: list[MessageEntry], events: EventStore, *, round_index: int, turn_index: int) -> tuple[TurnResult, dict]:
        profile = next(a for a in self.scene.agents if a.agent_id == actor)
        view = self.projection.build_agent_prompt_view(actor, world, private_states, messages, events.events)
        max_rounds = self.scene.termination_policy.max_rounds
        view["time_budget"] = {"current_round": round_index + 1, "max_rounds": max_rounds,
            "remaining_rounds_including_current": max_rounds - round_index if max_rounds is not None else None}
        available_specs = []
        unavailable = []
        visible_paths = {path for path in self.projection.fields if self._view_has_path(view["world"], path)}
        for spec in self.scene.action_specs:
            if spec.action_type not in profile.allowed_tools:
                continue
            rule_paths = self._precheck_paths(spec.preconditions)
            can_precheck = (spec.kind == "WRITE" and not spec.params_schema.get("required")
                and rule_paths is not None and rule_paths <= visible_paths
                and set(spec.cost) <= visible_paths)
            if can_precheck:
                try:
                    preview = self.arbiter.execute(world, ActionPayload(action_type=spec.action_type), actor,
                        {"round_index": round_index, "turn_index": turn_index, "branch_id": events.branch_id}, dry_run=True)
                except SceneEvaluationError:
                    preview = None
                if preview is not None and preview.feedback.status == "REJECTED":
                    unavailable.append({"action_type": spec.action_type, "description": spec.description,
                                        "reason": preview.feedback.message})
                    continue
            available_specs.append({"action_type": spec.action_type, "kind": spec.kind,
                "description": spec.description, "params_schema": spec.params_schema})
        view["tool_specs"] = available_specs
        view["unavailable_actions"] = unavailable
        ephemeral = []
        try:
            output = await self.gateway.call_with_failure_policy(profile.model_id, {**view, "ephemeral": ephemeral})
            read_count = 0
            while output.read_action is not None and read_count < self.scene.turn_tool_policy.max_read_calls:
                action = output.read_action
                spec = next((s for s in self.scene.action_specs if s.action_type == action.action_type), None)
                if (spec is None or spec.kind != "READ" or action.action_type not in profile.allowed_tools
                        or not Draft202012Validator(spec.params_schema).is_valid(action.parameters)):
                    result = {"error": "SYSTEM_ACTION_UNAVAILABLE"}
                else:
                    # Tool reads can only select from the already authorized world projection.
                    path = action.parameters.get("path")
                    value = view["world"]
                    if path is not None:
                        for part in str(path).split("."):
                            value = value.get(part) if isinstance(value, dict) else None
                    result = {"world": value} if path is None else {"value": value}
                ephemeral.append({"action": action.model_dump(), "result": result})
                read_count += 1
                output = await self.gateway.call_with_failure_policy(profile.model_id, {**view, "ephemeral": copy.deepcopy(ephemeral)})
        except (ModelExhausted, BudgetExhausted) as exc:
            outcome = self.gateway.policy.on_exhausted if isinstance(exc, ModelExhausted) else self.scene.budget_policy.on_exhausted
            await events.append("model_error" if isinstance(exc, ModelExhausted) else "budget_exhausted", {"actor_id": actor, "detail": str(exc)}, round_index=round_index, turn_index=turn_index, actor_id=actor)
            return TurnResult(completed=outcome == "SKIP_TURN", failure_outcome=outcome, last_event_seq=events.last_seq), world
        if output.read_action is not None:
            # The last answer is still a READ draft; it cannot be committed.
            output.response = output.response.model_copy(update={"speech": "", "private_update": None})
            output.write_action = None
            output.control_action = None
            output.read_action = None
        if output.response.target_channel == "WHISPER" and (not output.response.recipient_ids or any(r not in {a.agent_id for a in self.scene.agents} for r in output.response.recipient_ids)):
            output.response.speech = ""
        early_requested = False
        if output.control_action is not None:
            control = output.control_action
            control_spec = next((s for s in self.scene.action_specs if s.action_type == control.action_type), None)
            if (control_spec and control_spec.kind == "CONTROL" and control.action_type in profile.allowed_tools
                    and control_spec.control_signal != "NONE" and (self.scene.turn_tool_policy.allow_control_with_write or output.write_action is None)):
                early_requested = True
                await events.append("early_trigger", {"actor_id": actor, "signal": control_spec.control_signal},
                    round_index=round_index, turn_index=turn_index, actor_id=actor, visibility=control_spec.visibility.result)
            else:
                await events.append("permission_denied", {"action_type": control.action_type}, round_index=round_index, turn_index=turn_index,
                    actor_id=actor, visibility=EventVisibility(audience="SELF", owner_id=actor))
        write = output.write_action if self.scene.turn_tool_policy.max_write_calls >= 1 else None
        feedback = ExecutionFeedback(status="ACCEPTED", message="No action.")
        committed_world = world
        if write:
            await events.append("speech_pending", {"actor_id": actor}, round_index=round_index, turn_index=turn_index, actor_id=actor, visibility=EventVisibility(audience="AUDIT_ONLY"))
            spec = next((s for s in self.scene.action_specs if s.action_type == write.action_type), None)
            proposal_visibility = spec.visibility.proposal if spec else EventVisibility(audience="OPERATOR_ONLY")
            await events.append("action_proposed", {"action": write.model_dump()}, round_index=round_index, turn_index=turn_index, actor_id=actor, visibility=proposal_visibility)
            for attempt in range(MAX_ACTION_CORRECTION_RETRIES + 1):
                try:
                    transaction = self.arbiter.execute(committed_world, write, actor,
                        {"round_index": round_index, "turn_index": turn_index, "branch_id": events.branch_id})
                    feedback = transaction.feedback
                except SceneEvaluationError as exc:
                    await events.append("rule_evaluation_error", {"code": exc.code, "detail": str(exc)}, round_index=round_index, turn_index=turn_index, actor_id=actor)
                    feedback = ExecutionFeedback(status="REJECTED", reason_code="SYSTEM_ACTION_UNAVAILABLE", message="Action unavailable.")
                    break
                if feedback.status == "ACCEPTED":
                    committed_world = transaction.world_state
                    await events.append("arbiter_accepted", {"action_type": write.action_type}, round_index=round_index, turn_index=turn_index, actor_id=actor, visibility=spec.visibility.result if spec else None)
                    if transaction.changes:
                        await events.append("world_state_delta", {"changes": transaction.changes,
                            "before": transaction.before}, round_index=round_index, turn_index=turn_index,
                            actor_id=actor, visibility=EventVisibility(audience="ALL_AGENTS"))
                    for reveal in transaction.reveals:
                        await events.append("visibility_revealed", reveal, round_index=round_index,
                            turn_index=turn_index, actor_id=actor, visibility=EventVisibility(audience="ALL_AGENTS"))
                    break
                await events.append("arbiter_rejected", {"reason_code": feedback.reason_code,
                    "message": feedback.message, "action_type": write.action_type}, round_index=round_index, turn_index=turn_index, actor_id=actor, visibility=EventVisibility(audience="SELF", owner_id=actor))
                if not feedback.retry_allowed or attempt >= MAX_ACTION_CORRECTION_RETRIES:
                    break
                await events.append("retry_attempted", {"actor_id": actor}, round_index=round_index, turn_index=turn_index, actor_id=actor)
                try:
                    correction = await self.gateway.call_with_failure_policy(profile.model_id, {**view, "ephemeral": ephemeral, "feedback": feedback.model_dump()})
                except (ModelExhausted, BudgetExhausted) as exc:
                    await events.append("model_error", {"actor_id": actor, "detail": str(exc)}, round_index=round_index, turn_index=turn_index, actor_id=actor)
                    await events.append("speech_discarded", {"actor_id": actor}, round_index=round_index, turn_index=turn_index,
                        actor_id=actor, visibility=EventVisibility(audience="AUDIT_ONLY"))
                    outcome = self.gateway.policy.on_exhausted if isinstance(exc, ModelExhausted) else self.scene.budget_policy.on_exhausted
                    return TurnResult(completed=outcome == "SKIP_TURN", failure_outcome=outcome, last_event_seq=events.last_seq), world
                if correction.write_action is None:
                    break
                write = correction.write_action
                spec = next((s for s in self.scene.action_specs if s.action_type == write.action_type), None)
                await events.append("action_proposed", {"action": write.model_dump()}, round_index=round_index,
                    turn_index=turn_index, actor_id=actor,
                    visibility=spec.visibility.proposal if spec else EventVisibility(audience="OPERATOR_ONLY"))
                output.response = correction.response
        # Private state is committed only after final action feedback.
        if output.response.private_update is not None or write is not None:
            current = private_states[actor]
            update = output.response.private_update
            if update is not None:
                for name in ("current_goal", "current_plan", "next_intent"):
                    value = getattr(update, name)
                    if value is not None:
                        setattr(current, name, value)
                current.beliefs.update(update.beliefs)
                current.relationships.update(update.relationships)
            if write is not None:
                current.observations.append({"action_type": write.action_type, "feedback": feedback.model_dump()})
                current.observations = current.observations[-self.scene.private_memory_policy.max_observations:]
            await events.append("private_observation_added", {"actor_id": actor, "feedback": feedback.model_dump(), "state": current.model_dump()}, round_index=round_index, turn_index=turn_index, actor_id=actor, visibility=EventVisibility(audience="SELF", owner_id=actor))
        speech = output.response.speech
        if write and feedback.status != "ACCEPTED":
            speech = "[系统] 关联动作执行失败。" if speech else ""
            await events.append("speech_discarded", {"actor_id": actor}, round_index=round_index, turn_index=turn_index, actor_id=actor, visibility=EventVisibility(audience="AUDIT_ONLY"))
        if speech and self._speech_leaks_private_fact(actor, output.response.target_channel,
                output.response.recipient_ids, speech, committed_world, events):
            speech = ""
            await events.append("speech_discarded", {"actor_id": actor, "reason": "PRIVATE_FACT"},
                round_index=round_index, turn_index=turn_index, actor_id=actor,
                visibility=EventVisibility(audience="AUDIT_ONLY"))
        if speech:
            visibility = EventVisibility(audience="ALL_AGENTS") if output.response.target_channel == "PUBLIC" else EventVisibility(audience="SPECIFIC", audience_ids=list(set(output.response.recipient_ids + [actor])))
            event = await events.append("speech_committed", {"message": {"sender_id": actor, "channel": output.response.target_channel, "recipient_ids": output.response.recipient_ids, "content": speech}}, round_index=round_index, turn_index=turn_index, actor_id=actor, visibility=visibility)
            messages.append(MessageEntry(message_id=str(uuid.uuid4()), sender_id=actor, channel=output.response.target_channel, recipient_ids=output.response.recipient_ids, content=speech, committed_event_seq=event.event_seq))
        return TurnResult(completed=True, early_semantic_requested=early_requested, last_event_seq=events.last_seq), committed_world

    def _speech_leaks_private_fact(self, actor: str, channel: str, recipients: list[str],
                                   speech: str, world: dict, events: EventStore) -> bool:
        audience = ([a.agent_id for a in self.scene.agents if a.agent_id != actor]
                    if channel == "PUBLIC" else [a for a in recipients if a != actor])
        if not audience:
            return False
        actor_view = self.projection.build_agent_prompt_view(actor, world, {}, [], events.events)["world"]
        text = re.sub(r"[^\w]", "", speech).casefold()
        for field in self.scene.world_state_schema.fields:
            if field.visibility.audience == "PUBLIC" or not self._view_has_path(actor_view, field.path):
                continue
            unknown_to_recipient = False
            for recipient in audience:
                perspective = Perspective(kind="AGENT", agent_id=recipient, principal_role="PARTICIPANT")
                grant = SessionGrant(session_id="internal", principal_id=recipient, role="PARTICIPANT",
                                     allowed_perspectives=[f"AGENT:{recipient}"], audit_level="NONE")
                visible = self.projection.project_world(world, perspective, grant, events=events.events)
                if not self._view_has_path(visible, field.path):
                    unknown_to_recipient = True
                    break
            if not unknown_to_recipient:
                continue
            value = world.get(field.path)
            clues = [field.description]
            if isinstance(value, str) and len(value) >= 4:
                clues.append(value)
            for clue in clues:
                normalized = re.sub(r"[^\w]", "", clue).casefold()
                if len(normalized) < 4:
                    continue
                matches = SequenceMatcher(None, normalized, text, autojunk=False).get_matching_blocks()
                if max(block.size for block in matches) >= 3 and sum(block.size for block in matches) / len(normalized) >= 0.65:
                    return True
        return False

    @staticmethod
    def _view_has_path(world: dict, path: str) -> bool:
        value = world
        for part in path.split("."):
            if not isinstance(value, dict) or part not in value:
                return False
            value = value[part]
        return True

    @staticmethod
    def _precheck_paths(rules) -> set[str] | None:
        paths = set()
        for rule in rules:
            try:
                parsed = ast.parse(rule.expr, mode="eval")
            except SyntaxError:
                return None
            for node in ast.walk(parsed):
                if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "world":
                    if not isinstance(node.slice, ast.Constant) or not isinstance(node.slice.value, str):
                        return None
                    paths.add(node.slice.value)
        return paths
