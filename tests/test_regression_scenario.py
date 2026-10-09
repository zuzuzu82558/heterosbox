"""A fixed underground-crisis contract across compiler, runtime and replay."""
import asyncio
import copy

import httpx
import pytest

from heterosbox.api import create_app
from heterosbox.compiler import DraftAction, DraftPlan, compile_plan
from heterosbox.deepseek import DeepSeekAdapter
from heterosbox.gateway import ModelGateway, ModelOutput, ScriptedModelAdapter
from heterosbox.models import ActionPayload, AgentResponse, Perspective, SessionGrant, SimEvent
from heterosbox.projection import ProjectionService
from heterosbox.results import leaderboard
from heterosbox.session import Session


def underground_plan():
    return DraftPlan.model_validate({
        "name": "地底三十日", "story": "地底城市面临能源衰减。工程师掌握真实能耗。",
        "public_story": "地底城市面临能源衰减，各方必须保障医疗和城市稳定。",
        "mode": "cooperative", "shared_goal": "保障医疗、稳定并控制事故风险",
        "agents": [
            {"name": "工程师", "role": "能源维护", "persona": "谨慎", "goal": "修复系统"},
            {"name": "市长", "role": "市政决策", "persona": "务实", "goal": "维持稳定"},
            {"name": "居民代表", "role": "居民协调", "persona": "直率", "goal": "保护居民"},
            {"name": "监察员", "role": "监督", "persona": "审慎", "goal": "核查风险"}],
        "metrics": [
            {"name": "能源库存", "initial": 6, "maximum": 10, "preference": "HIGHER"},
            {"name": "城市稳定度", "initial": 4, "maximum": 10, "preference": "HIGHER"},
            {"name": "事故风险", "initial": 3, "maximum": 10, "preference": "LOWER"},
            {"name": "医疗覆盖", "initial": 0, "maximum": 10, "preference": "HIGHER"}],
        "facts": [{"name": "真实能耗", "value": "每回合消耗一单位能源", "visibility": "OWNER",
                   "owner_agent_index": 0}],
        "actions": [
            {"name": "密报", "description": "工程师向市长提交能耗数据", "allowed_agent_indices": [0],
             "effects": [{"target": "FACT", "index": 0, "op": "reveal", "visible_to_agent_indices": [1]}]},
            {"name": "修复", "description": "消耗能源修复医疗系统", "allowed_agent_indices": [0],
             "conditions": [{"metric_index": 0, "op": "GE", "value": 2}],
             "effects": [{"index": 0, "op": "sub", "value": 2},
                         {"index": 2, "op": "sub", "value": 1},
                         {"index": 3, "op": "add", "value": 2}]},
            {"name": "配给", "description": "配给可能牺牲稳定", "allowed_agent_indices": [1],
             "effects": [{"index": 0, "op": "add", "value": 1},
                         {"index": 1, "op": "sub", "value": 1}]},
            {"name": "协调", "description": "居民协调", "allowed_agent_indices": [2],
             "effects": [{"index": 1, "op": "add", "value": 1}]},
            {"name": "核查", "description": "降低事故风险", "allowed_agent_indices": [3],
             "effects": [{"index": 2, "op": "sub", "value": 1}]}],
        "target_metric_index": 3, "target_value": 2,
        "success_conditions": [{"metric_index": 3, "op": "GE", "value": 2},
                               {"metric_index": 1, "op": "GE", "value": 4},
                               {"metric_index": 2, "op": "LE", "value": 2}],
        "failure_conditions": [{"metric_index": 2, "op": "GE", "value": 5}],
        "settlement_effects": [{"index": 0, "op": "sub", "value": 1},
                               {"index": 2, "op": "add", "value": 1}],
        "max_rounds": 2,
    })


def _view(scene, agent_id):
    view = Perspective(kind="AGENT", agent_id=agent_id, principal_role="PARTICIPANT")
    grant = SessionGrant(session_id="test", principal_id=agent_id, role="PARTICIPANT",
                         allowed_perspectives=[f"AGENT:{agent_id}"], audit_level="NONE")
    return view, grant


def _output(action=None):
    return ModelOutput(response=AgentResponse(speech="按已知信息行动"),
                       write_action=ActionPayload(action_type=action) if action else None)


def test_compiled_contract_and_atomic_effects():
    scene = compile_plan(underground_plan(), "mock")
    assert scene.agents[1].allowed_tools == ["action_3"]
    assert scene.world_state_schema.fields[2].preference == "LOWER"
    assert scene.action_specs[1].intent == "消耗能源修复医疗系统"
    projection = ProjectionService(scene)
    engineer, grant = _view(scene, "agent_1")
    mayor, mayor_grant = _view(scene, "agent_2")
    assert projection.project_world(scene.initial_world_state, engineer, grant)["fact_1"]
    assert "fact_1" not in projection.project_world(scene.initial_world_state, mayor, mayor_grant)
    prompt = projection.build_agent_prompt_view("agent_2", scene.initial_world_state, {}, [], [])
    assert prompt["identity"]["display_name"] == "市长"
    assert prompt["identity"]["personal_goal"] == "维持稳定"
    assert "每回合消耗一单位能源" not in str(prompt)
    from heterosbox.arbiter import WriteArbiter
    arbiter = WriteArbiter(scene)
    denied = arbiter.execute(scene.initial_world_state, ActionPayload(action_type="action_2"),
                             "agent_2", {"round_index": 0})
    assert denied.feedback.reason_code == "PERMISSION_DENIED" and not denied.changes
    accepted = arbiter.execute(scene.initial_world_state, ActionPayload(action_type="action_2"),
                               "agent_1", {"round_index": 0})
    assert accepted.changes == {"metric_1": 4, "metric_3": 2, "metric_4": 2}
    low = copy.deepcopy(scene.initial_world_state)
    low["metric_1"] = 1
    rejected = arbiter.execute(low, ActionPayload(action_type="action_2"), "agent_1", {"round_index": 0})
    assert rejected.feedback.status == "REJECTED" and rejected.world_state == low
    cost_scene = scene.model_dump()
    cost_scene["action_specs"][3]["cost"] = {"metric_1": 1}
    from heterosbox.models import SceneRuntimeConfig
    cost_arbiter = WriteArbiter(SceneRuntimeConfig.model_validate(cost_scene))
    paid = cost_arbiter.execute(scene.initial_world_state, ActionPayload(action_type="action_4"),
                                "agent_3", {"round_index": 0})
    assert paid.changes == {"metric_1": 5, "metric_2": 5}
    low["metric_1"] = 0
    unpaid = cost_arbiter.execute(low, ActionPayload(action_type="action_4"), "agent_3", {"round_index": 0})
    assert unpaid.feedback.reason_code == "INSUFFICIENT_RESOURCE" and not unpaid.changes


def test_risk_increase_is_not_a_positive_contribution():
    scene = compile_plan(underground_plan(), "mock")
    event = SimEvent.model_validate({"event_id": "risk-up", "session_id": "test", "branch_id": "main",
        "event_seq": 1, "parent_event_id": None, "round_index": 0, "turn_index": 0,
        "actor_id": "agent_3", "type": "world_state_delta",
        "payload": {"changes": {"metric_3": 4}}, "visibility": {"audience": "ALL_AGENTS"},
        "created_at": 0.0})
    row = next(item for item in leaderboard(scene, [event])["rows"] if item["agent_id"] == "agent_3")
    assert row["score"] < 0 and row["breakdown"]["metric_3"] < 0


def test_compiler_rejects_explicit_effect_contradiction():
    plan = underground_plan().model_dump()
    plan["actions"][1]["description"] = "修复后提升事故风险"
    with pytest.raises(ValueError, match="contradicts"):
        DraftPlan.model_validate(plan)


def test_literal_fact_effect_is_not_parsed_as_an_expression():
    plan = underground_plan()
    plan.actions.append(DraftAction.model_validate({"name": "复核能耗", "description": "更新私人能耗结论",
        "allowed_agent_indices": [0], "effects": [{"target": "FACT", "index": 0, "op": "set",
        "value": "已复核：每回合消耗一单位能源"}]}))
    scene = compile_plan(plan, "mock")
    from heterosbox.arbiter import WriteArbiter
    result = WriteArbiter(scene).execute(scene.initial_world_state,
        ActionPayload(action_type="action_6"), "agent_1", {"round_index": 0})
    assert result.feedback.status == "ACCEPTED"
    assert result.world_state["fact_1"].startswith("已复核")


@pytest.mark.asyncio
async def test_preflight_rejects_manual_description_effect_conflict():
    scene = compile_plan(underground_plan(), "mock").model_dump(mode="json")
    scene["action_specs"][1]["description"] = "修复设施并提升事故风险"
    app = create_app(db_path=None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/scenes/validate", json={"scene": scene},
            headers={"x-principal-id": "local-owner"})
    assert response.status_code == 422
    assert "实际效果不一致" in response.json()["detail"]


@pytest.mark.asyncio
async def test_private_propagation_environment_success_and_ranking():
    scene = compile_plan(underground_plan(), "mock")
    scene.judging_policy.enabled = False
    adapter = ScriptedModelAdapter([_output("action_1"), _output(), _output(), _output(),
                                    _output("action_2"), _output(), _output(), _output("action_5")])
    session = Session(scene, ModelGateway(adapter, scene.model_failure_policy, scene.budget_policy))
    await session.start(paused=True)
    for _ in range(4):
        await session.command("STEP", principal_id="owner")
    assert session.world["metric_1"] == 5 and session.world["metric_3"] == 4
    reveal_seq = next(e.event_seq for e in session.events.events if e.type.value == "visibility_revealed")
    mayor, grant = _view(scene, "agent_2")
    resident, resident_grant = _view(scene, "agent_3")
    assert "fact_1" not in session.projection.project_world(session.world, mayor, grant,
        events=session.events.events, at_seq=reveal_seq - 1)
    assert session.projection.project_world(session.world, mayor, grant, events=session.events.events)["fact_1"]
    assert "fact_1" not in session.projection.project_world(session.world, resident, resident_grant,
        events=session.events.events)
    assert "fact_1" not in str(session.projection.project_events(session.events.events, mayor, grant,
        at_seq=reveal_seq - 1))
    assert any(e["type"] == "visibility_revealed" for e in session.projection.project_events(
        session.events.events, mayor, grant, at_seq=reveal_seq))
    for _ in range(4):
        await session.command("STEP", principal_id="owner")
    assert session.status == "FINISHED"
    assert next(e.payload["outcome"] for e in session.events.events if e.type.value == "simulation_terminated") == "GOAL_REACHED"
    assert session.world["metric_4"] == 2 and session.world["metric_3"] == 2
    board = leaderboard(scene, session.events.events)
    engineer = next(row for row in board["rows"] if row["agent_id"] == "agent_1")
    assert engineer["breakdown"]["metric_3"] > 0
    app = create_app(adapter, None)
    app.state.branches[(session.session_id, "main")] = session
    headers = {"x-principal-id": "local-owner"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        past = await client.get(f"/sessions/{session.session_id}/replay",
            params={"at_seq": reveal_seq - 1, "perspective": "AGENT:agent_2"}, headers=headers)
        later = await client.get(f"/sessions/{session.session_id}/replay",
            params={"at_seq": reveal_seq, "perspective": "AGENT:agent_2"}, headers=headers)
        past_events = await client.get(f"/sessions/{session.session_id}/events",
            params={"at_seq": reveal_seq - 1, "perspective": "AGENT:agent_2"}, headers=headers)
        report = await client.get(f"/sessions/{session.session_id}/report", headers=headers)
    assert "fact_1" not in past.json()["world_state"]
    assert later.json()["world_state"]["fact_1"] == "每回合消耗一单位能源"
    assert "每回合消耗一单位能源" not in past_events.text
    assert report.json()["outcome"] == "GOAL_REACHED"
    assert all(item["met"] for item in report.json()["goal_checks"] if item["group"] == "SUCCESS")


@pytest.mark.asyncio
async def test_failure_and_max_round_are_distinct():
    for risk, expected in ((4, "FAILED"), (0, "MAX_TURNS")):
        scene = compile_plan(underground_plan(), "mock")
        scene.initial_world_state["metric_3"] = risk
        scene.judging_policy.enabled = False
        adapter = ScriptedModelAdapter([_output() for _ in range(8)])
        session = Session(scene, ModelGateway(adapter, scene.model_failure_policy, scene.budget_policy))
        await session.start(paused=True)
        for _ in range(8):
            if session.status == "FINISHED":
                break
            await session.command("STEP", principal_id="owner")
        assert session.status == "FINISHED"
        assert next(e.payload["outcome"] for e in session.events.events if e.type.value == "simulation_terminated") == expected
        assert session.world["metric_1"] == (5 if expected == "FAILED" else 4)


@pytest.mark.asyncio
async def test_explicit_deadline_failure_outcome():
    scene = compile_plan(underground_plan(), "mock")
    scene.termination_policy.round_limit_outcome = "FAILED"
    scene.initial_world_state["metric_3"] = 0
    scene.judging_policy.enabled = False
    adapter = ScriptedModelAdapter([_output() for _ in range(8)])
    session = Session(scene, ModelGateway(adapter, scene.model_failure_policy, scene.budget_policy))
    await session.start(paused=True)
    for _ in range(8):
        if session.status == "FINISHED":
            break
        await session.command("STEP", principal_id="owner")
    terminated = next(e for e in session.events.events if e.type.value == "simulation_terminated")
    assert terminated.payload["outcome"] == "FAILED"
    assert "未达成目标" in terminated.payload["reason"]


@pytest.mark.asyncio
async def test_semantic_enum_normalization_and_failure_fallback():
    def reply(request):
        return httpx.Response(200, json={"id": "judge-test", "choices": [{"finish_reason": "stop", "message": {
            "content": '{"goal_completion":0,"consensus_score":0,"stalemate_score":1,"should_terminate":true,"outcome":"stalemate","reason":"无法推进","evidence_refs":[]}'}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        output = await DeepSeekAdapter(api_key="test", client=client).call("mock", {"kind": "SEMANTIC_JUDGE"})
    assert output.semantic_result.outcome == "STALEMATE"

    scene = compile_plan(underground_plan(), "mock")
    adapter = ScriptedModelAdapter([])
    session = Session(scene, ModelGateway(adapter, scene.model_failure_policy, scene.budget_policy))
    await session.start(paused=True)
    assert await session._semantic_check("PERIODIC") is False
    assert session.semantic_unavailable_reason is not None
    assert session.status == "PAUSED"
    assert await session._semantic_check("PERIODIC") is False
    assert sum(e.type.value == "model_error" for e in session.events.events) == 1


@pytest.mark.asyncio
async def test_finish_status_waits_for_terminal_event_and_final_judge():
    class SlowJudge:
        def __init__(self):
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def call(self, model_id, context):
            self.entered.set()
            await self.release.wait()
            return ModelOutput()

    scene = compile_plan(underground_plan(), "mock")
    adapter = SlowJudge()
    session = Session(scene, ModelGateway(adapter, scene.model_failure_policy, scene.budget_policy))
    session.status = "RUNNING"
    finishing = asyncio.create_task(session._terminate("MAX_TURNS"))
    await adapter.entered.wait()
    assert session.status == "STOPPING"
    assert not any(e.type.value == "simulation_terminated" for e in session.events.events)
    adapter.release.set()
    await finishing
    assert session.status == "FINISHED"
    assert any(e.type.value == "simulation_terminated" for e in session.events.events)
