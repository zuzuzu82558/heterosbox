"""Local first FastAPI transport. No frontend or provider credentials are included."""
import asyncio
import copy
import json
import os
import re
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from typing import Literal

from .gateway import ModelGateway, ModelOutput
from .models import Perspective, SceneRuntimeConfig, SessionGrant
from .projection import ProjectionDenied
from .session import CommandRejected, Session
from .storage import SqliteRepository
from .deepseek import DeepSeekAdapter
from .providers import MultiProviderAdapter
from .compiler import DraftPlan, compile_plan
from .reachability import goal_reachable
from .arbiter import SceneEvaluationError, WriteArbiter
from .models import ActionPayload


class UnconfiguredAdapter:
    async def call(self, model_id: str, context: dict) -> ModelOutput:
        raise RuntimeError("No model adapter configured")


class CreateSessionRequest(BaseModel):
    scene: SceneRuntimeConfig
    paused: bool = True


class CommandRequest(BaseModel):
    name: str
    expected_version: int | None = None
    payload: dict = Field(default_factory=dict)


class ForkRequest(BaseModel):
    checkpoint_id: str


class DraftRequest(BaseModel):
    description: str = Field(min_length=12, max_length=4000)
    model_id: str | None = None


class SceneCheckRequest(BaseModel):
    scene: SceneRuntimeConfig


class VisualSettings(BaseModel):
    theme: Literal["garden", "council", "market"] = "council"


def create_app(adapter=None, db_path: str | None = "heterosbox.sqlite3") -> FastAPI:
    app = FastAPI(title="HeteroSbox Headless API")
    app.state.sessions: dict[str, Session] = {}
    app.state.branches: dict[tuple[str, str], Session] = {}
    app.state.adapter = adapter or (MultiProviderAdapter() if any(os.getenv(name) for name in
        ("DEEPSEEK_API_KEY", "MIMO_API_KEY", "GEMINI_API_KEY")) else UnconfiguredAdapter())
    app.state.repository = SqliteRepository(db_path) if db_path else None
    app.state.visual_settings: dict[str, dict] = {}
    if app.state.repository is not None:
        for session_id, branch_id, scene in app.state.repository.list_sessions():
            restored = Session.restore(scene,
                ModelGateway(app.state.adapter, scene.model_failure_policy, scene.budget_policy),
                app.state.repository, session_id, branch_id)
            app.state.branches[(session_id, branch_id)] = restored
            if branch_id == "main":
                app.state.sessions[session_id] = restored

    def owner(x_principal_id: str | None = Header(default=None)) -> str:
        if x_principal_id != "local-owner":
            raise HTTPException(status_code=403, detail="local owner principal required")
        return x_principal_id

    async def available_models() -> list[dict]:
        if not hasattr(app.state.adapter, "list_models"):
            return []
        return await app.state.adapter.list_models()

    async def verify_scene_models(scene: SceneRuntimeConfig):
        if not isinstance(app.state.adapter, (DeepSeekAdapter, MultiProviderAdapter)):
            return
        try:
            available = {item["model_id"] for item in await available_models()}
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"模型连接不可用：{exc}") from exc
        missing = sorted({agent.model_id for agent in scene.agents} - available)
        if missing:
            raise HTTPException(status_code=422, detail=f"模型未验证可用：{', '.join(missing)}")

    def preflight_scene(scene: SceneRuntimeConfig) -> list[str]:
        arbiter = WriteArbiter(scene)
        warnings = []
        from .expr import freeze
        initial_context = {"world": freeze(scene.initial_world_state), "working": freeze(scene.initial_world_state),
            "params": freeze({}), "actor": "__system__", "session": freeze({"round_index": 0,
                "turn_index": 0, "branch_id": "main"})}
        for condition in scene.termination_policy.hard_conditions:
            if condition.outcome == "GOAL_REACHED" and arbiter.evaluator.evaluate(
                    condition.expr, initial_context, phase="termination"):
                raise HTTPException(status_code=422, detail="成功条件在初始状态就已满足；请加入可验证的任务进展条件")
        for action in scene.action_specs:
            if action.kind != "WRITE":
                continue
            if any(re.match(r"^(?:提出|建议|商议|讨论|拒绝|公开发言|公开商议)", label)
                    for label in (action.intent or "", action.description)):
                changed = [field.description or field.path for field in scene.world_state_schema.fields
                    if field.type in ("int", "float") and any(effect.path == field.path and effect.op != "reveal"
                        for effect in action.effects)]
                if changed:
                    warnings.append(f"{action.description}：名称像发言，但会直接改变{'、'.join(changed)}；请核对行动效果")
            for field in scene.world_state_schema.fields:
                if not field.description or field.type not in ("int", "float"):
                    continue
                label = re.escape(field.description)
                plus = re.search(rf"(?:增加|提升|提高)\s*{label}|{label}\s*(?:增加|提升|提高)", action.description)
                minus = re.search(rf"(?:降低|减少|消耗|削减)\s*{label}|{label}\s*(?:降低|减少|消耗|削减)", action.description)
                if not plus and not minus:
                    continue
                writes = [effect for effect in action.effects if effect.path == field.path and effect.op != "reveal"]
                if len(writes) != 1 or writes[0].op not in ("add", "sub") or (plus and writes[0].op != "add") or (minus and writes[0].op != "sub"):
                    raise HTTPException(status_code=422, detail=f"行动 {action.action_type} 的说明与 {field.description} 的实际效果不一致")
            actor = next((agent.agent_id for agent in scene.agents if action.action_type in agent.allowed_tools), None)
            if actor is None:
                warnings.append(f"行动 {action.action_type} 未分配给任何角色")
                continue
            if (action.params_schema.get("required") or []):
                warnings.append(f"行动 {action.action_type} 有必填参数，未执行空参数预检")
                continue
            try:
                result = arbiter.execute(scene.initial_world_state,
                    ActionPayload(action_type=action.action_type, parameters={}), actor,
                    {"round_index": 0, "turn_index": 0, "branch_id": "main"}, dry_run=True)
            except SceneEvaluationError as exc:
                raise HTTPException(status_code=422, detail=f"行动 {action.action_type} 预检失败：{exc}") from exc
            if result.feedback.status == "REJECTED":
                warnings.append(f"{action.description}：初始状态不可执行（{result.feedback.message}）")
        public_context = scene.public_story or scene.background_story
        if (re.search(r"暴雨|洪水|倒计时|持续恶化|每回合|每轮|逐日|逐小时|能源耗尽", public_context)
                and not (scene.settlement_spec and scene.settlement_spec.enabled)):
            warnings.append("场景包含持续危机，但没有每轮环境结算；危机不会独立于角色行动演化")
        scored = {metric.metric_id for metric in scene.leaderboard_config.metrics}
        for field in scene.world_state_schema.fields:
            if field.path in scored and field.preference == "NEUTRAL":
                warnings.append(f"指标 {field.description or field.path} 未声明改善方向，不会参与确定性排行")
        return warnings

    def session_for(session_id: str, branch_id: str = "main") -> Session:
        session = app.state.branches.get((session_id, branch_id))
        if not session:
            raise HTTPException(status_code=404, detail="unknown session")
        return session

    def perspective_for(session: Session, perspective: str):
        if perspective == "GOD":
            view = Perspective(kind="GOD", principal_role="OWNER", audit_level="FULL")
        elif perspective == "OBSERVER":
            view = Perspective(kind="OBSERVER", principal_role="OWNER")
        elif perspective.startswith("AGENT:"):
            view = Perspective(kind="AGENT", agent_id=perspective.split(":", 1)[1], principal_role="OWNER")
        else:
            raise HTTPException(status_code=400, detail="invalid perspective")
        grant = SessionGrant(session_id=session.session_id, principal_id="local-owner", role="OWNER",
            allowed_perspectives=["GOD", "OBSERVER"] + [f"AGENT:{a.agent_id}" for a in session.scene.agents], audit_level="FULL")
        try:
            session.projection.authorize(view, grant)
        except ProjectionDenied as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return view, grant

    async def authorized_snapshot(session: Session, perspective: str, at_seq: int | None = None):
        view, grant = perspective_for(session, perspective)
        # The session lock keeps committed events and private state on one boundary.
        async with session._lock:
            last_seq = session.events.last_seq
            if at_seq is not None and (at_seq < 0 or at_seq > last_seq):
                raise HTTPException(status_code=422, detail="invalid event sequence")
            prefix = [e for e in session.events.events if at_seq is None or e.event_seq <= at_seq]
            if at_seq is None:
                private_states = {key: value.model_copy(deep=True) for key, value in session.private_states.items()}
                scheduler_state = session.scheduler.model_dump()
                run_integrity = session.run_integrity.model_dump()
            else:
                from .models import PrivateAgentState
                private_states = {a.agent_id: PrivateAgentState() for a in session.scene.agents}
                for event in prefix:
                    if event.type.value == "private_observation_added" and "state" in event.payload:
                        private_states[event.payload["actor_id"]] = PrivateAgentState.model_validate(event.payload["state"])
                checkpoints = [c for c in session.checkpoints if c.event_seq <= at_seq]
                last_round = next((e.payload["round_index"] for e in reversed(prefix)
                    if e.type.value == "round_started"), 0)
                scheduler_state = {"round_index": last_round,
                    "turn_index": next((e.turn_index for e in reversed(prefix) if e.type.value == "turn_started"), 0)}
                run_integrity = checkpoints[-1].run_integrity.model_dump() if checkpoints and checkpoints[-1].event_seq == at_seq else None
            status = session.status if at_seq is None else None
        world = copy.deepcopy(session.base_world)
        for event in prefix:
            if event.type.value == "world_state_delta":
                for path, value in event.payload["changes"].items():
                    target = world
                    pieces = path.split(".")
                    for piece in pieces[:-1]:
                        target = target[piece]
                    target[pieces[-1]] = copy.deepcopy(value)
        return {"session_id": session.session_id, "branch_id": session.events.branch_id,
            "event_seq": prefix[-1].event_seq if prefix else 0, "status": status,
            "version": session.version if at_seq is None else None, "historical": at_seq is not None,
            "world_state": session.projection.project_world(world, view, grant, events=prefix),
            "private_states": session.projection.project_private(private_states, view, grant),
            "scheduler_state": scheduler_state, "run_integrity": run_integrity}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/models")
    async def models(principal: str = Depends(owner)):
        try:
            models_list = await available_models()
        except Exception as exc:
            return {"configured": False, "models": [], "error": str(exc)}
        errors = getattr(app.state.adapter, "connection_errors", {})
        return {"configured": bool(models_list), "models": models_list,
                "provider_errors": errors,
                "error": None if models_list else "尚未连接可验证的模型"}

    @app.post("/scenes/draft")
    async def scene_draft(request: DraftRequest, principal: str = Depends(owner)):
        try:
            models_list = await available_models()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"模型连接不可用：{exc}") from exc
        ids = {item["model_id"] for item in models_list}
        preferred = next((item["model_id"] for item in models_list if "flash" in item["model_id"].lower()), None)
        model_id = request.model_id or preferred or (models_list[0]["model_id"] if models_list else None)
        if model_id not in ids:
            raise HTTPException(status_code=422, detail="请先选择已验证可用的模型")
        if not hasattr(app.state.adapter, "generate_scene_plan"):
            raise HTTPException(status_code=503, detail="当前模型不支持场景生成")
        feedback = ""
        previous_raw = None
        for _ in range(5):
            try:
                if previous_raw is not None and hasattr(app.state.adapter, "repair_scene_plan"):
                    raw = await app.state.adapter.repair_scene_plan(
                        request.description, previous_raw, feedback, model_id)
                else:
                    raw = await app.state.adapter.generate_scene_plan(request.description, model_id, feedback=feedback)
                omitted_speech = []
                if isinstance(raw, dict) and isinstance(raw.get("actions"), list):
                    kept = []
                    for action in raw["actions"]:
                        if (isinstance(action, dict) and not action.get("effects")
                                and action.get("metric_index") is None and action.get("delta") is None
                                and re.match(r"^(?:公开提议|公开商议|公开发言|提议|提出|建议|商议|讨论|拒绝)",
                                    str(action.get("name", "")))):
                            omitted_speech.append(str(action["name"]))
                        else:
                            kept.append(action)
                    raw = {**raw, "actions": kept}
                omitted_deadline = False
                if (isinstance(raw, dict) and raw.get("max_rounds") is not None
                        and isinstance(raw.get("metrics"), list)
                        and isinstance(raw.get("failure_conditions"), list)):
                    target_index = raw.get("target_metric_index")
                    failures = []
                    for condition in raw["failure_conditions"]:
                        index = condition.get("metric_index") if isinstance(condition, dict) else None
                        if index == target_index and type(index) is int and 0 <= index < len(raw["metrics"]):
                            initial = raw["metrics"][index].get("initial")
                            value = condition.get("value")
                            op = condition.get("op")
                            met = (type(initial) is int and type(value) is int and
                                ((op == "GE" and initial >= value) or (op == "LE" and initial <= value)
                                 or (op == "EQ" and initial == value)))
                            if met:
                                omitted_deadline = True
                                continue
                        failures.append(condition)
                    raw = {**raw, "failure_conditions": failures}
                if (isinstance(raw, dict) and raw.get("max_rounds") is not None
                        and (omitted_deadline or re.search(r"(?:轮|回合|期限|截止).{0,80}失败", request.description))):
                    raw = {**raw, "round_limit_outcome": "FAILED"}
                if isinstance(raw, dict):
                    previous_raw = raw
                plan = DraftPlan.model_validate(raw)
                scene = compile_plan(plan, model_id)
                warnings = preflight_scene(scene)
                reachable = goal_reachable(scene)
                if reachable is False:
                    raise ValueError("no executable path reaches the goal within the round limit")
                if reachable is None:
                    warnings.append("自动路径检查未覆盖全部状态；请人工核对目标能否在期限内达成")
                requested_numbers = {int(value) for value in re.findall(r"(?<!\d)\d+(?!\d)", request.description)
                    if int(value) >= 2}
                structured_numbers = {len(plan.agents), plan.target_value}
                if plan.max_rounds is not None:
                    structured_numbers.add(plan.max_rounds)
                for metric in plan.metrics:
                    structured_numbers.update(value for value in (metric.initial, metric.minimum,
                        metric.maximum, metric.target_min, metric.target_max) if type(value) is int)
                for action in plan.actions:
                    structured_numbers.update(effect.value for effect in action.effects
                        if type(effect.value) is int)
                    structured_numbers.update(condition.value for condition in action.conditions)
                structured_numbers.update(condition.value for condition in
                    plan.success_conditions + plan.failure_conditions)
                structured_numbers.update(fact.value for fact in plan.facts if type(fact.value) is int)
                missing_numbers = sorted(requested_numbers - structured_numbers)
                if missing_numbers:
                    warnings.append(f"输入中的数值 {', '.join(map(str, missing_numbers[:8]))} 未出现在可执行配置中；请核对是否遗漏关键约束")
                if omitted_speech:
                    warnings.append(f"{'、'.join(omitted_speech)}没有可执行效果，已作为普通发言处理")
                if omitted_deadline:
                    warnings.append("开局已成立的目标失败条件已移除；未完成任务由回合上限结束")
                break
            except ValidationError as exc:
                feedback = "; ".join(dict.fromkeys(
                    f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                    if error["loc"] else error["msg"] for error in exc.errors()))[:1200]
                if "failure condition already holds at start" in feedback:
                    feedback += (". A deadline failure must not be a failure_condition true at the initial state. "
                                 "Use max_rounds for the deadline and remove that initial-true failure condition.")
                if "success conditions imply a failure condition" in feedback:
                    feedback += (". Success and failure conditions must not overlap. Remove any failure condition "
                                 "that becomes true when the success target is reached.")
                if "success consumes all available capacity but full capacity is a failure condition" in feedback:
                    feedback += (". Filling the user's available spaces is required to place all items. "
                                 "Remove failure conditions for remaining capacity reaching zero; "
                                 "keep the capacity lower bounds to prevent overflow.")
                if "condition threshold must be within metric bounds" in feedback:
                    feedback += (". Do not invent an impossible threshold to avoid an initial failure. "
                                 "For unfinished work at the deadline, omit that failure condition and use max_rounds.")
                if "success target unreachable with nonrenewable resource" in feedback:
                    feedback += (". A progress action consumes a resource too quickly for the target. "
                                 "Reusable workers must not be permanently subtracted per task, or add a valid way to replenish them.")
                if "exact success target unreachable with action step sizes" in feedback:
                    feedback += (". Repair the named metric in this error, not a different metric. "
                                 "Add a smaller final-batch action or adjust an existing action that changes "
                                 "this metric, with matching effects on related capacities.")
                if "success target unreachable within round limit" in feedback:
                    feedback += (". Fix the named target metric. Each agent gets one action per round. "
                                 "Keep the user's deadline and capacities; increase the effect per relevant action "
                                 "or authorize more appropriate agents to perform it. Increasing unrelated maxima "
                                 "does not increase goal progress.")
                if "success target unreachable after resource replenishment turns" in feedback:
                    feedback += (". Moving goods and restoring the consumed resource both take turns. "
                                 "Do not permanently consume reusable workers, or increase batch size so the target fits the round limit.")
                if "success target unreachable with role and resource turns" in feedback:
                    feedback += (". Account for each role's allowed actions and recovery turns. "
                                 "Increase realistic batch sizes or treat reusable workers as available each turn rather than permanently consumed.")
                if "no executable path reaches the goal" in feedback:
                    feedback += (". Repair the previous plan's action preconditions, resource costs, capacities, and "
                                 "role permissions so an actual sequence of legal actions can reach all goals before max_rounds. "
                                 "Do not merely raise a numeric target or remove essential user constraints.")
            except Exception as exc:
                feedback = str(exc)[:900]
                if "no executable path reaches the goal" in feedback:
                    feedback += (". Keep the user's deadline and capacities. Check whether an action needed "
                                 "for one goal uses the same actor and turn as an action needed for another goal. "
                                 "Check whether settlement reverses progress, especially risk or countdown fields. "
                                 "Repair action effects, permissions, and preconditions so one legal sequence "
                                 "satisfies every success condition together.")
        else:
            if "unreachable" in feedback or "no executable path reaches the goal" in feedback:
                detail = "场景目标在当前轮数或资源限制内不可达；请调整行动、资源消耗或目标约束"
            elif any(term in feedback for term in ("at most", "too_long", "too_short", "maximum", "metric_index")):
                detail = "场景初稿超出当前建模容量或字段范围；请缩小场景，或在生成后检查关键目标与行动"
            else:
                detail = "场景初稿未通过规则校验；请重试，并在草案中核对目标、行动与终止条件"
            raise HTTPException(status_code=422, detail=detail)
        description = request.description
        theme = "market" if any(word in description for word in ("市场", "交易", "竞价", "资源竞争")) else (
            "garden" if any(word in description for word in ("城市", "浮空", "地图", "山谷", "桥梁")) else "council")
        settings = {"theme": theme}
        app.state.visual_settings[scene.scene_id] = settings
        if app.state.repository is not None:
            app.state.repository.save_visual_settings(scene.scene_id, settings)
        return {"scene": scene.model_dump(mode="json"), "visual": settings, "source": "model_draft",
                "warnings": warnings,
                "notice": "这是可编辑初稿；请检查每条规则、角色目标与终止条件。"}

    @app.get("/scenes/{scene_id}/visual")
    async def get_visual(scene_id: str, principal: str = Depends(owner)):
        settings = (app.state.repository.load_visual_settings(scene_id) if app.state.repository is not None else None)
        return settings or app.state.visual_settings.get(scene_id) or {"theme": "council"}

    @app.put("/scenes/{scene_id}/visual")
    async def put_visual(scene_id: str, settings: VisualSettings, principal: str = Depends(owner)):
        data = settings.model_dump()
        app.state.visual_settings[scene_id] = data
        if app.state.repository is not None:
            app.state.repository.save_visual_settings(scene_id, data)
        return data

    @app.post("/scenes/validate")
    async def scene_validate(request: SceneCheckRequest, principal: str = Depends(owner)):
        try:
            warnings = preflight_scene(request.scene)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        await verify_scene_models(request.scene)
        return {"valid": True, "agent_count": len(request.scene.agents),
                "action_count": len(request.scene.action_specs),
                "warnings": warnings}

    @app.get("/examples")
    async def examples(principal: str = Depends(owner)):
        root = Path(__file__).resolve().parent.parent / "examples"
        return {"examples": [{"name": path.stem, "scene": json.loads(path.read_text(encoding="utf-8"))}
                             for path in sorted(root.glob("*.json"))]}

    @app.get("/sessions")
    async def list_sessions(principal: str = Depends(owner)):
        return {"sessions": [{"session_id": session.session_id, "branch_id": branch_id,
                 "scenario_name": session.scene.scenario_name, "status": session.status,
                 "event_seq": session.events.last_seq, "agent_count": len(session.scene.agents)}
                 for (_, branch_id), session in app.state.branches.items()]}

    @app.get("/sessions/{session_id}/overview")
    async def overview(session_id: str, branch_id: str = "main", principal: str = Depends(owner)):
        session = session_for(session_id, branch_id)
        return {"scene": session.scene.model_dump(mode="json"), "status": session.status,
                "version": session.version, "event_seq": session.events.last_seq,
                "checkpoints": [{"checkpoint_id": c.checkpoint_id, "event_seq": c.event_seq,
                                 "kind": c.kind} for c in session.checkpoints]}

    @app.get("/sessions/{session_id}/report")
    async def report(session_id: str, branch_id: str = "main", principal: str = Depends(owner)):
        session = session_for(session_id, branch_id)
        events_list = session.events.events
        latest = lambda kind: next((e for e in reversed(events_list) if e.type.value == kind), None)
        terminated = latest("simulation_terminated")
        judging = latest("semantic_termination_decided")
        judge_error = next((e for e in reversed(events_list) if e.type.value == "model_error"
            and e.payload.get("source") in ("SEMANTIC_JUDGE", "FINAL_JUDGE")), None)
        ranking = latest("leaderboard_updated")
        judge_status = ("READY" if judging and (not terminated or judging.payload.get("source") == "FINAL"
                        or judging.payload.get("should_terminate")) else "INTERIM" if judging else "UNAVAILABLE")
        goal_checks = []
        for check in session.scene.goal_checks:
            value = session.world
            for part in check.path.split("."):
                value = value[part]
            met = {"GE": value >= check.target, "LE": value <= check.target,
                   "EQ": value == check.target}[check.op]
            goal_checks.append({**check.model_dump(), "current": value, "met": met})
        participants = sorted({e.actor_id for e in events_list if e.type.value == "turn_started" and e.actor_id})
        return {"status": session.status, "outcome": terminated.payload.get("outcome") if terminated else None,
                "outcome_reason": terminated.payload.get("reason") if terminated else None,
                "goal_checks": goal_checks, "final_world_state": copy.deepcopy(session.world),
                "participating_agents": participants,
                "run_integrity": session.run_integrity.model_dump(),
                "judger": {"status": judge_status, "result": judging.payload, "event_id": judging.event_id,
                           "judge_bias_risk": True}
                    if judging else {"status": "UNAVAILABLE", "reason":
                        "语义裁判不可用，终局由确定性规则决定" if judge_error else "尚无裁判结果"},
                "leaderboard": ranking.payload if ranking else {"status": "UNAVAILABLE", "reason": "尚无排行结果"},
                "arbiter": {"accepted": sum(e.type.value == "arbiter_accepted" for e in events_list),
                            "rejected": sum(e.type.value == "arbiter_rejected" for e in events_list),
                            "evidence_refs": [e.event_id for e in events_list if e.type.value in ("arbiter_accepted", "arbiter_rejected")]}}

    @app.post("/sessions")
    async def create_session(request: CreateSessionRequest, principal: str = Depends(owner)):
        try:
            preflight_scene(request.scene)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        await verify_scene_models(request.scene)
        session = Session(request.scene, ModelGateway(app.state.adapter, request.scene.model_failure_policy, request.scene.budget_policy), repository=app.state.repository)
        app.state.sessions[session.session_id] = session
        app.state.branches[(session.session_id, session.events.branch_id)] = session
        await session.start(paused=request.paused)
        return {"session_id": session.session_id, "branch_id": session.events.branch_id, "status": session.status}

    @app.post("/sessions/{session_id}/commands")
    async def command(session_id: str, request: CommandRequest, branch_id: str = "main", principal: str = Depends(owner)):
        session = session_for(session_id, branch_id)
        try:
            return await session.command(request.name, principal_id=principal, expected_version=request.expected_version, payload=request.payload)
        except CommandRejected as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/sessions/{session_id}/snapshot")
    async def snapshot(session_id: str, perspective: str = "GOD", branch_id: str = "main", principal: str = Depends(owner)):
        return await authorized_snapshot(session_for(session_id, branch_id), perspective)

    @app.get("/sessions/{session_id}/replay")
    async def replay(session_id: str, at_seq: int, perspective: str = "GOD", branch_id: str = "main", principal: str = Depends(owner)):
        return await authorized_snapshot(session_for(session_id, branch_id), perspective, at_seq=at_seq)

    @app.post("/sessions/{session_id}/fork")
    async def fork(session_id: str, request: ForkRequest, branch_id: str = "main", principal: str = Depends(owner)):
        session = session_for(session_id, branch_id)
        try:
            branch = await session.fork_branch(request.checkpoint_id,
                ModelGateway(app.state.adapter, session.scene.model_failure_policy, session.scene.budget_policy))
        except StopIteration as exc:
            raise HTTPException(status_code=404, detail="checkpoint not found") from exc
        app.state.branches[(session_id, branch.events.branch_id)] = branch
        return {"session_id": session_id, "branch_id": branch.events.branch_id,
            "parent_checkpoint_id": request.checkpoint_id, "status": branch.status}

    @app.get("/sessions/{session_id}/events")
    async def events(session_id: str, perspective: str = "GOD", after_seq: int = 0,
                     at_seq: int | None = None, branch_id: str = "main", principal: str = Depends(owner)):
        session = session_for(session_id, branch_id)
        view, grant = perspective_for(session, perspective)
        if at_seq is not None and (at_seq < 0 or at_seq > session.events.last_seq):
            raise HTTPException(status_code=422, detail="invalid event sequence")
        return {"events": session.projection.project_events(session.events.events, view, grant,
                after_seq=after_seq, at_seq=at_seq),
                "last_event_seq": at_seq if at_seq is not None else session.events.last_seq}

    @app.get("/sessions/{session_id}/stream")
    async def stream(session_id: str, perspective: str = "GOD", after_seq: int = 0, branch_id: str = "main", principal: str = Depends(owner)):
        session = session_for(session_id, branch_id)
        view, grant = perspective_for(session, perspective)
        async def generator():
            async for event in session.events.subscribe(after_seq):
                projected = session.projection.project_events(session.events.events, view, grant, after_seq=event.event_seq - 1, at_seq=event.event_seq)
                current = next((e for e in projected if e["event_seq"] == event.event_seq), None)
                if current is not None:
                    yield f"id: {event.event_seq}\nevent: sim_event\ndata: {json.dumps(current, ensure_ascii=False)}\n\n"
        return StreamingResponse(generator(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @app.websocket("/sessions/{session_id}/ws")
    async def websocket(websocket: WebSocket, session_id: str, perspective: str = Query(default="GOD"), after_seq: int = Query(default=0), branch_id: str = Query(default="main")):
        if websocket.headers.get("x-principal-id") != "local-owner":
            await websocket.close(code=4403)
            return
        session = app.state.branches.get((session_id, branch_id))
        if session is None:
            await websocket.close(code=4404)
            return
        try:
            view, grant = perspective_for(session, perspective)
        except HTTPException:
            await websocket.close(code=4403)
            return
        await websocket.accept()
        try:
            async for event in session.events.subscribe(after_seq):
                projected = session.projection.project_events(session.events.events, view, grant, after_seq=event.event_seq - 1, at_seq=event.event_seq)
                current = next((e for e in projected if e["event_seq"] == event.event_seq), None)
                if current is not None:
                    await websocket.send_json(current)
        except (WebSocketDisconnect, asyncio.CancelledError):
            return

    frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
    if frontend_dir.exists():
        app.mount("/app", StaticFiles(directory=frontend_dir, html=True), name="frontend")
        @app.get("/")
        async def homepage():
            return RedirectResponse("/app/")

    return app


app = create_app()
