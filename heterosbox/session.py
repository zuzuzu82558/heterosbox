"""Authoritative session scheduler, commands, checkpoints and branch fork."""
import asyncio
import copy
import random
import uuid

from .arbiter import SceneEvaluationError, WriteArbiter
from .events import EventStore, BufferedEventStore
from .gateway import ModelGateway
from .models import Checkpoint, MessageEntry, PrivateAgentState, RunIntegrity, SceneRuntimeConfig, SchedulerState
from .projection import ProjectionService
from .turn import TurnExecutor


class CommandRejected(ValueError):
    pass


class Session:
    def __init__(self, scene: SceneRuntimeConfig, gateway: ModelGateway, *, session_id: str | None = None, branch_id: str = "main", repository=None):
        self.scene = scene
        self.session_id = session_id or str(uuid.uuid4())
        self.repository = repository
        self.events = EventStore(self.session_id, branch_id, repository)
        if repository is not None:
            repository.save_scene(self.session_id, branch_id, scene)
        self.gateway = gateway
        self.projection = ProjectionService(scene)
        self.arbiter = WriteArbiter(scene)
        self.turn_executor = TurnExecutor(scene, gateway, self.projection, self.arbiter)
        self.world = copy.deepcopy(scene.initial_world_state)
        self.base_world = copy.deepcopy(scene.initial_world_state)
        self.private_states = {a.agent_id: PrivateAgentState() for a in scene.agents}
        self.messages: list[MessageEntry] = []
        self.scheduler = SchedulerState(round_index=0, turn_index=0, actual_turn_order=self._order(0), next_agent_offset=0, random_seed=scene.random_seed)
        self.status = "CREATED"
        self.version = 0
        self.command_seq = 0
        self.last_consumed_command_seq = 0
        self.pending_commands: list[dict] = []
        self.checkpoints: list[Checkpoint] = []
        self.run_integrity = RunIntegrity()
        self.completed_turns = 0
        self._turn_task: asyncio.Task | None = None
        self._run_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._stepping = False
        self.budget_blocked = False
        self.early_checks_by_round: dict[int, int] = {}
        self.last_early_check_round: int | None = None
        self._round_start_world = copy.deepcopy(self.world)
        self.stalemate_rounds = 0
        self.semantic_unavailable_reason: str | None = None
        self._fork_parent_checkpoint_id: str | None = None

    def _order(self, round_index: int) -> list[str]:
        ids = [a.agent_id for a in self.scene.agents]
        if self.scene.scheduler_policy == "shuffle_round_robin":
            random.Random(self.scene.random_seed + round_index).shuffle(ids)
        # Priority uses declared agent order until a deterministic priority field is configured.
        return ids

    async def start(self, *, paused: bool = False):
        if self.status != "CREATED":
            raise CommandRejected("already started")
        self.status = "RUNNING"
        self.version += 1
        await self.events.append("round_started", {"round_index": 0}, round_index=0)
        if paused:
            self.status = "PAUSING"
            await self.events.append("simulation_pausing", {})
            await self._checkpoint("TURN")
            self.status = "PAUSED"
            await self.events.append("simulation_paused", {})
        else:
            self._run_task = asyncio.create_task(self.run())

    async def command(self, name: str, *, principal_id: str, expected_version: int | None = None, payload: dict | None = None) -> dict:
        payload = payload or {}
        async with self._lock:
            self.command_seq += 1
            seq = self.command_seq
            command_id = str(uuid.uuid4())
            async def reject(reason: str):
                await self.events.append("command_rejected", {"command_id": command_id, "command_seq": seq, "reason": reason})
                raise CommandRejected(reason)
            if expected_version is not None and expected_version != self.version:
                await self.events.append("command_conflict", {"command_id": command_id, "command_seq": seq, "expected_version": expected_version, "actual_version": self.version})
                raise CommandRejected("version conflict")
            if self.status in ("STOPPING", "FINISHED") and name != "STOP":
                await reject("session stopping or finished")
            if name == "PAUSE":
                if self.status == "RUNNING":
                    self.status = "PAUSING"
                    await self.events.append("simulation_pausing", {})
                elif self.status not in ("PAUSING", "PAUSED"):
                    await reject("PAUSE requires RUNNING")
            elif name == "RESUME":
                if self.status != "PAUSED" or self.budget_blocked:
                    await reject("RESUME requires unblocked PAUSED")
                self.status = "RUNNING"
                await self.events.append("simulation_resumed", {})
                self._run_task = asyncio.create_task(self.run())
            elif name == "STOP":
                if self.status != "FINISHED":
                    self.status = "STOPPING"
                    await self.events.append("simulation_stopping", {})
                    if self._turn_task and not self._turn_task.done():
                        self._turn_task.cancel()
                    if self._run_task and not self._run_task.done():
                        self._run_task.cancel()
                    await self._checkpoint("TURN")
                    await self.events.append("simulation_terminated", {"outcome": "HUMAN_TERMINATED"})
                    await self._publish_leaderboard()
                    self.status = "FINISHED"
            elif name == "UPDATE_BUDGET":
                if self.status != "PAUSED":
                    await reject("UPDATE_BUDGET requires PAUSED")
                from .models import BudgetPolicy
                new_policy = BudgetPolicy.model_validate(payload)
                if self.gateway.ledger.exhausted(new_policy):
                    await reject("new budget remains exhausted")
                self.gateway.budget_policy = new_policy
                self.budget_blocked = False
                await self.events.append("budget_updated", {"policy": new_policy.model_dump()})
            elif name == "STEP":
                if self.status != "PAUSED" or self._stepping:
                    await reject("STEP requires PAUSED")
                self._stepping = True
            else:
                await reject("unsupported command")
            self.version += 1
            self.last_consumed_command_seq = seq
            await self.events.append("command_accepted", {"command_id": command_id, "command_seq": seq, "name": name, "principal_id": principal_id})
        if name == "STEP":
            try:
                await self._advance_one()
                if self.status == "PAUSED":
                    await self._checkpoint("TURN")
            finally:
                self._stepping = False
        return {"command_id": command_id, "command_seq": seq, "status": self.status, "version": self.version}

    async def run(self):
        try:
            while self.status == "RUNNING":
                await self._advance_one()
                if self.status == "PAUSING":
                    await self._checkpoint("TURN")
                    self.status = "PAUSED"
                    await self.events.append("simulation_paused", {})
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self.status = "ERROR"
            await self.events.append("simulation_error", {"error": str(exc)})

    async def _advance_one(self):
        if self.status not in ("RUNNING", "PAUSED", "PAUSING"):
            return
        if self.scheduler.next_agent_offset >= len(self.scheduler.actual_turn_order):
            await self._finish_round()
            return
        actor = self.scheduler.actual_turn_order[self.scheduler.next_agent_offset]
        await self.events.append("turn_started", {"actor_id": actor}, round_index=self.scheduler.round_index, turn_index=self.scheduler.turn_index, actor_id=actor)
        buffered = BufferedEventStore(self.events)
        private_copy = {key: value.model_copy(deep=True) for key, value in self.private_states.items()}
        message_copy = copy.deepcopy(self.messages)
        self._turn_task = asyncio.create_task(self.turn_executor.execute_turn(actor, copy.deepcopy(self.world), private_copy, message_copy, buffered,
            round_index=self.scheduler.round_index, turn_index=self.scheduler.turn_index))
        try:
            result, committed_world = await self._turn_task
        except asyncio.CancelledError:
            await self.events.append("model_call_cancelled", {"actor_id": actor})
            return
        finally:
            self._turn_task = None
        async with self._lock:
            if self.status in ("STOPPING", "FINISHED"):
                return
            mapping = await buffered.commit()
            for message in message_copy[len(self.messages):]:
                message.committed_event_seq = mapping.get(message.committed_event_seq, message.committed_event_seq)
            if result.completed:
                self.world = committed_world
                self.private_states = private_copy
                self.messages = message_copy
        if result.failure_outcome == "PAUSE":
            self.budget_blocked = self.gateway.ledger.exhausted(self.gateway.budget_policy)
            await self._checkpoint("TURN")
            self.status = "PAUSED"
            await self.events.append("simulation_paused", {})
            return
        if result.failure_outcome == "TERMINATE":
            await self._terminate("FAILED")
            return
        if not result.completed:
            return
        self.completed_turns += 1
        self.scheduler.turn_index += 1
        self.scheduler.next_agent_offset += 1
        if result.early_semantic_requested and self.scene.early_semantic_trigger_policy.enabled:
            policy = self.scene.early_semantic_trigger_policy
            count = self.early_checks_by_round.get(self.scheduler.round_index, 0)
            cooldown_ok = self.last_early_check_round is None or self.scheduler.round_index - self.last_early_check_round >= policy.cooldown_rounds
            if count < policy.max_early_checks_per_round and cooldown_ok:
                self.early_checks_by_round[self.scheduler.round_index] = count + 1
                self.last_early_check_round = self.scheduler.round_index
                if await self._semantic_check("EARLY"):
                    return
            else:
                await self.events.append("arbiter_rejected", {"reason_code": "EARLY_TRIGGER_RATE_LIMIT"})
        if await self._hard_termination():
            return
        if self.scheduler.next_agent_offset == len(self.scheduler.actual_turn_order):
            await self._finish_round(end_after_settlement=(self.scene.termination_policy.max_turns is not None
                and self.completed_turns >= self.scene.termination_policy.max_turns))
        else:
            if self.scene.termination_policy.max_turns is not None and self.completed_turns >= self.scene.termination_policy.max_turns:
                await self._terminate("MAX_TURNS", reason="达到角色决策机会数上限")
                return
            await self._checkpoint("TURN")

    async def _finish_round(self, *, end_after_settlement: bool = False):
        if not self.scheduler.settlement_done:
            try:
                result = self.arbiter.settle(self.world, {"round_index": self.scheduler.round_index, "turn_index": self.scheduler.turn_index, "branch_id": self.events.branch_id})
            except SceneEvaluationError as exc:
                self.status = "ERROR"
                await self.events.append("rule_evaluation_error", {"code": exc.code, "detail": str(exc)})
                return
            if result.feedback.status == "ACCEPTED":
                self.world = result.world_state
                await self.events.append("round_settlement_committed", {"round_index": self.scheduler.round_index},
                    round_index=self.scheduler.round_index, turn_index=self.scheduler.turn_index)
                if result.changes:
                    from .models import EventVisibility
                    await self.events.append("world_state_delta", {"changes": result.changes,
                        "before": result.before}, round_index=self.scheduler.round_index,
                        turn_index=self.scheduler.turn_index, visibility=EventVisibility(audience="ALL_AGENTS"))
                for reveal in result.reveals:
                    from .models import EventVisibility
                    await self.events.append("visibility_revealed", reveal,
                        visibility=EventVisibility.model_validate(reveal["new_visibility"]),
                        round_index=self.scheduler.round_index, turn_index=self.scheduler.turn_index)
            self.scheduler.settlement_done = True
        if await self._hard_termination():
            return
        if end_after_settlement:
            await self._terminate("MAX_TURNS", reason="达到角色决策机会数上限")
            return
        if self.world == self._round_start_world:
            self.stalemate_rounds += 1
        else:
            self.stalemate_rounds = 0
        if (self.scene.termination_policy.allow_stalemate and self.scene.termination_policy.stalemate_no_delta_rounds is not None
                and self.stalemate_rounds >= self.scene.termination_policy.stalemate_no_delta_rounds):
            await self._terminate("STALEMATE")
            return
        judging = self.scene.judging_policy
        if (judging.enabled and judging.semantic_judging_enabled
                and (self.scheduler.round_index + 1) % judging.semantic_check_interval_rounds == 0):
            if await self._semantic_check("PERIODIC"):
                return
        await self._checkpoint("ROUND")
        if self.scene.termination_policy.max_rounds is not None and self.scheduler.round_index + 1 >= self.scene.termination_policy.max_rounds:
            outcome = self.scene.termination_policy.round_limit_outcome
            await self._terminate(outcome, reason="到完整轮数上限仍未达成目标" if outcome == "FAILED" else "达到完整轮数上限")
            return
        self.scheduler.round_index += 1
        self.scheduler.actual_turn_order = self._order(self.scheduler.round_index)
        self.scheduler.next_agent_offset = 0
        self.scheduler.settlement_done = False
        self._round_start_world = copy.deepcopy(self.world)
        await self.events.append("round_started", {"round_index": self.scheduler.round_index},
            round_index=self.scheduler.round_index, turn_index=self.scheduler.turn_index)

    async def _hard_termination(self) -> bool:
        from .arbiter import SceneEvaluationError
        from .expr import ExpressionError, freeze
        for condition in self.scene.termination_policy.hard_conditions:
            if condition.outcome == "GOAL_REACHED" and any(
                    check.group == "SUCCESS" and any(word in check.label for word in
                        ("机会", "概率", "可能性", "成功率"))
                    for check in self.scene.goal_checks):
                # A probability or opportunity is not evidence that the narrated outcome occurred.
                continue
            try:
                result = self.arbiter.evaluator.evaluate(condition.expr,
                    {"world": freeze(self.world), "working": freeze(self.world), "params": freeze({}),
                     "actor": "__system__", "session": freeze({"round_index": self.scheduler.round_index,
                        "turn_index": self.scheduler.turn_index, "branch_id": self.events.branch_id})}, phase="termination")
            except ExpressionError as exc:
                self.status = "ERROR"
                await self.events.append("rule_evaluation_error", {"code": "TERMINATION_EVAL_ERROR", "detail": str(exc)})
                return True
            if type(result) is not bool:
                self.status = "ERROR"
                await self.events.append("rule_evaluation_error", {"code": "TERMINATION_EVAL_ERROR", "detail": "hard condition must be bool"})
                return True
            if result:
                await self._terminate(condition.outcome, reason=condition.reason, condition_id=condition.condition_id)
                return True
        return False

    async def _semantic_check(self, source: str) -> bool:
        if self.semantic_unavailable_reason is not None:
            return False
        await self.events.append("semantic_check", {"source": source})
        try:
            model_id = self.scene.agents[0].model_id
            output = await self.gateway.call_with_failure_policy(model_id,
                {"kind": "SEMANTIC_JUDGE", "source": source, "world": copy.deepcopy(self.world),
                 "goals": self.scene.shared_goals,
                 "judging_dimensions": self.scene.judging_policy.judging_dimensions,
                 "evidence": self._judge_evidence(),
                 "round_index": self.scheduler.round_index, "event_seq": self.events.last_seq})
            if output.semantic_result is None:
                raise ValueError("judger returned no SemanticTerminationResult")
        except Exception as exc:
            self.semantic_unavailable_reason = str(exc)
            await self.events.append("model_error", {"source": "SEMANTIC_JUDGE", "detail": str(exc)})
            # Semantic assessment is advisory; deterministic rules remain active.
            return False
        decision = output.semantic_result
        valid_refs = {item["event_id"] for item in self._judge_evidence()}
        decision.evidence_refs = self._normalize_judge_refs(decision.evidence_refs)
        if self.scene.judging_policy.require_evidence and valid_refs and not decision.evidence_refs:
            await self.events.append("model_error", {"source": "SEMANTIC_JUDGE", "detail": "Judger returned no valid evidence_refs"})
            return False
        if decision.should_terminate:
            from .expr import freeze
            matching = [condition for condition in self.scene.termination_policy.hard_conditions
                if condition.outcome == decision.outcome]
            if matching:
                context = {"world": freeze(self.world), "working": freeze(self.world),
                    "params": freeze({}), "actor": "__system__", "session": freeze({
                        "round_index": self.scheduler.round_index, "turn_index": self.scheduler.turn_index,
                        "branch_id": self.events.branch_id})}
                if not any(self.arbiter.evaluator.evaluate(item.expr, context, phase="termination") for item in matching):
                    decision.should_terminate = False
                    decision.outcome = None
                    decision.reason += "；可验证终局条件尚未满足"
            else:
                decision.should_terminate = False
                decision.outcome = None
                decision.reason += "；该结局没有配置可验证的硬条件，推演继续"
        await self.events.append("semantic_termination_decided", {**decision.model_dump(), "source": source})
        if decision.should_terminate:
            await self._terminate(decision.outcome, already_judged=True, reason=decision.reason)
            return True
        return False

    def _judge_evidence(self) -> list[dict]:
        types = {"turn_started", "speech_committed", "arbiter_accepted", "arbiter_rejected", "world_state_delta"}
        return [{"event_id": e.event_id, "type": e.type.value, "actor_id": e.actor_id,
                 "payload": e.payload} for e in self.events.events if e.type.value in types][-80:]

    def _normalize_judge_refs(self, refs: list[str]) -> list[str]:
        evidence = self._judge_evidence()
        ids = {item["event_id"] for item in evidence}
        turns = [event.event_id for event in self.events.events if event.type.value == "turn_started"]
        normalized = []
        for ref in refs:
            if ref in ids:
                normalized.append(ref)
            elif ref.startswith("turn:") and ref[5:].isdigit():
                index = int(ref[5:]) - 1
                if 0 <= index < len(turns):
                    normalized.append(turns[index])
        return list(dict.fromkeys(normalized))

    async def _publish_leaderboard(self):
        from .results import leaderboard
        result = leaderboard(self.scene, self.events.events)
        if result is not None:
            await self.events.append("leaderboard_updated", result)

    async def _terminate(self, outcome: str, *, already_judged: bool = False,
                         reason: str | None = None, condition_id: str | None = None):
        self.status = "STOPPING"
        if (self.scene.judging_policy.enabled and self.scene.judging_policy.semantic_judging_enabled
                and not already_judged and self.semantic_unavailable_reason is None):
            try:
                output = await self.gateway.call_with_failure_policy(self.scene.agents[0].model_id,
                    {"kind": "SEMANTIC_JUDGE", "source": "FINAL", "world": copy.deepcopy(self.world),
                     "goals": self.scene.shared_goals,
                     "judging_dimensions": self.scene.judging_policy.judging_dimensions,
                     "evidence": self._judge_evidence(), "round_index": self.scheduler.round_index,
                     "event_seq": self.events.last_seq})
                if output.semantic_result is not None:
                    decision = output.semantic_result
                    valid_refs = {item["event_id"] for item in self._judge_evidence()}
                    decision.evidence_refs = self._normalize_judge_refs(decision.evidence_refs)
                    if self.scene.judging_policy.require_evidence and valid_refs and not decision.evidence_refs:
                        await self.events.append("model_error", {"source": "FINAL_JUDGE", "detail": "Judger returned no valid evidence_refs"})
                    else:
                        await self.events.append("semantic_termination_decided", {**decision.model_dump(), "source": "FINAL"})
            except Exception as exc:
                await self.events.append("model_error", {"source": "FINAL_JUDGE", "detail": str(exc)})
        await self._publish_leaderboard()
        await self._checkpoint("TURN")
        await self.events.append("simulation_terminated", {"outcome": outcome,
            "reason": reason or ("达到推演上限" if outcome == "MAX_TURNS" else "推演结束"),
            "condition_id": condition_id})
        self.status = "FINISHED"

    async def _checkpoint(self, kind: str) -> Checkpoint:
        checkpoint = Checkpoint(checkpoint_id=str(uuid.uuid4()), session_id=self.session_id, branch_id=self.events.branch_id,
            parent_checkpoint_id=self.checkpoints[-1].checkpoint_id if self.checkpoints else self._fork_parent_checkpoint_id, event_seq=self.events.last_seq,
            round_index=self.scheduler.round_index, turn_index=self.scheduler.turn_index, world_state=copy.deepcopy(self.world),
            private_states={k: v.model_dump() for k, v in self.private_states.items()}, public_blackboard="",
            scheduler_state=self.scheduler.model_dump(), counters={"completed_turns": self.completed_turns,
                "stalemate_rounds": self.stalemate_rounds, "early_checks_by_round": self.early_checks_by_round,
                "last_early_check_round": self.last_early_check_round, "round_start_world": self._round_start_world,
                "semantic_unavailable_reason": self.semantic_unavailable_reason},
            leaderboard_snapshot=next((e.payload for e in reversed(self.events.events) if e.type.value == "leaderboard_updated"), None),
            judger_state=next((e.payload for e in reversed(self.events.events) if e.type.value == "semantic_termination_decided"), {}),
            schema_versions={"world": self.scene.world_state_schema.schema_version, "scene": self.scene.scene_version},
            budget_state=self.gateway.ledger.__dict__.copy(), pending_commands=copy.deepcopy(self.pending_commands),
            visibility_cursor=self.events.last_seq, kind=kind, run_integrity=self.run_integrity.model_copy(deep=True),
            takeover_state=None, last_consumed_command_seq=self.last_consumed_command_seq)
        self.checkpoints.append(checkpoint)
        if self.repository is not None:
            self.repository.save_checkpoint(checkpoint)
        await self.events.append("checkpoint_created", {"checkpoint_id": checkpoint.checkpoint_id, "at_event_seq": checkpoint.event_seq})
        return checkpoint

    def snapshot(self) -> dict:
        return {"session_id": self.session_id, "branch_id": self.events.branch_id, "event_seq": self.events.last_seq,
            "status": self.status, "world_state": copy.deepcopy(self.world), "scheduler_state": self.scheduler.model_dump(),
            "version": self.version, "run_integrity": self.run_integrity.model_dump()}

    def fork(self, checkpoint_id: str, gateway: ModelGateway) -> "Session":
        checkpoint = next(c for c in self.checkpoints if c.checkpoint_id == checkpoint_id)
        branch = Session(self.scene, gateway, session_id=self.session_id, branch_id=str(uuid.uuid4()), repository=self.repository)
        branch.world = copy.deepcopy(checkpoint.world_state)
        branch.base_world = copy.deepcopy(checkpoint.world_state)
        branch.private_states = {k: PrivateAgentState.model_validate(v) for k, v in checkpoint.private_states.items()}
        branch.scheduler = SchedulerState.model_validate(checkpoint.scheduler_state)
        branch.completed_turns = checkpoint.counters["completed_turns"]
        branch.semantic_unavailable_reason = checkpoint.counters.get("semantic_unavailable_reason")
        branch.gateway.ledger.__dict__.update(checkpoint.budget_state)
        branch._fork_parent_checkpoint_id = checkpoint.checkpoint_id
        branch.messages = [copy.deepcopy(m) for m in self.messages if m.committed_event_seq <= checkpoint.event_seq]
        branch.run_integrity = checkpoint.run_integrity.model_copy(deep=True)
        branch.run_integrity.branch_forked_from_intervened_run = not checkpoint.run_integrity.pure_agent_run
        branch.status = "PAUSED"
        return branch

    async def fork_branch(self, checkpoint_id: str, gateway: ModelGateway) -> "Session":
        branch = self.fork(checkpoint_id, gateway)
        await branch.events.append("branch_created", {"parent_branch_id": self.events.branch_id,
            "parent_checkpoint_id": checkpoint_id, "scene_version": self.scene.scene_version,
            "world_schema_version": self.scene.world_state_schema.schema_version})
        await branch._checkpoint("TURN")
        return branch

    @classmethod
    def restore(cls, scene: SceneRuntimeConfig, gateway: ModelGateway, repository, session_id: str, branch_id: str) -> "Session":
        session = cls(scene, gateway, session_id=session_id, branch_id=branch_id, repository=repository)
        session.events.events = repository.load_events(session_id, branch_id)
        session.checkpoints = repository.load_checkpoints(session_id, branch_id)
        if not session.checkpoints:
            return session
        checkpoint = session.checkpoints[-1]
        if branch_id != "main":
            session.base_world = copy.deepcopy(session.checkpoints[0].world_state)
        session.world = copy.deepcopy(checkpoint.world_state)
        session.private_states = {key: PrivateAgentState.model_validate(value) for key, value in checkpoint.private_states.items()}
        session.scheduler = SchedulerState.model_validate(checkpoint.scheduler_state)
        session.completed_turns = checkpoint.counters["completed_turns"]
        session.stalemate_rounds = checkpoint.counters.get("stalemate_rounds", 0)
        session.semantic_unavailable_reason = checkpoint.counters.get("semantic_unavailable_reason")
        session.early_checks_by_round = {int(key): value for key, value in checkpoint.counters.get("early_checks_by_round", {}).items()}
        session.last_early_check_round = checkpoint.counters.get("last_early_check_round")
        session._round_start_world = checkpoint.counters.get("round_start_world", copy.deepcopy(session.world))
        session.run_integrity = checkpoint.run_integrity.model_copy(deep=True)
        session.last_consumed_command_seq = checkpoint.last_consumed_command_seq
        session.command_seq = max((e.payload.get("command_seq", 0) for e in session.events.events), default=0)
        session.gateway.ledger.__dict__.update(checkpoint.budget_state)
        for event in session.events.events:
            if event.event_seq > checkpoint.event_seq:
                if event.type.value == "world_state_delta":
                    from .arbiter import _set
                    for path, value in event.payload["changes"].items():
                        _set(session.world, path, copy.deepcopy(value))
                elif event.type.value == "private_observation_added" and "state" in event.payload:
                    session.private_states[event.payload["actor_id"]] = PrivateAgentState.model_validate(event.payload["state"])
        for event in session.events.events:
            if event.type.value == "speech_committed":
                message = event.payload["message"]
                session.messages.append(MessageEntry(message_id=event.event_id, sender_id=message["sender_id"],
                    channel=message["channel"], recipient_ids=message["recipient_ids"], content=message["content"],
                    committed_event_seq=event.event_seq))
        session.status = "FINISHED" if any(e.type.value == "simulation_terminated" for e in session.events.events) else "PAUSED"
        return session
