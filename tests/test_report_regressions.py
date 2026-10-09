"""Regression contracts from the laboratory, evacuation and observatory reports."""
import pytest
import httpx

from heterosbox.api import UnconfiguredAdapter, create_app
from heterosbox.compiler import DraftPlan, compile_plan
from heterosbox.reachability import goal_reachable
from heterosbox.gateway import ModelGateway, ModelOutput, ScriptedModelAdapter
from heterosbox.models import AgentResponse, SemanticTerminationResult
from heterosbox.session import Session


def plan():
    return {
        "name": "断电实验室", "story": "基地断电，需要形成能源方案并保障人员安全。",
        "public_story": "基地断电，需要形成能源方案并保障人员安全。",
        "mode": "cooperative", "shared_goal": "形成能源方案并保障人员安全",
        "agents": [{"name": "主管", "role": "管理", "persona": "谨慎", "goal": "维持安全"},
                   {"name": "工程师", "role": "维修", "persona": "务实", "goal": "恢复能源"}],
        "metrics": [{"name": "能源储备", "initial": 68, "maximum": 100},
                    {"name": "方案完成度", "initial": 0, "maximum": 100}],
        "actions": [{"name": "制定方案", "description": "形成能源方案", "allowed_agent_indices": [1],
                     "effects": [{"index": 1, "op": "add", "value": 25}]}],
        "target_metric_index": 1, "target_value": 100,
        "success_conditions": [{"metric_index": 0, "op": "GE", "value": 30},
                               {"metric_index": 1, "op": "GE", "value": 100}],
        "max_rounds": 4,
    }


def test_laboratory_rejects_initially_satisfied_success():
    data = plan()
    data["success_conditions"] = [{"metric_index": 0, "op": "GE", "value": 30}]
    with pytest.raises(ValueError, match="already hold at start"):
        DraftPlan.model_validate(data)


@pytest.mark.asyncio
async def test_manual_scene_cannot_restore_initial_success():
    scene = compile_plan(DraftPlan.model_validate(plan()), "mock").model_dump(mode="json")
    scene["termination_policy"]["hard_conditions"][-1]["expr"] = "world['metric_1'] >= 30"
    app = create_app(adapter=UnconfiguredAdapter(), db_path=None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/scenes/validate", json={"scene": scene},
            headers={"x-principal-id": "local-owner"})
    assert response.status_code == 422
    assert "初始状态" in response.json()["detail"]


@pytest.mark.asyncio
async def test_draft_failure_does_not_expose_pydantic_paths():
    class InvalidDraftAdapter:
        async def list_models(self):
            return [{"model_id": "mock"}]

        async def generate_scene_plan(self, description, model_id, feedback=""):
            data = plan()
            data["metrics"][0]["maximum"] = 10
            return data

    app = create_app(InvalidDraftAdapter(), db_path=None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/scenes/draft", json={"description": "断电实验室仍需完成能源方案", "model_id": "mock"},
            headers={"x-principal-id": "local-owner"})
    assert response.status_code == 422
    assert "场景初稿" in response.json()["detail"]
    assert "DraftPlan" not in response.text and "metrics.0" not in response.text


@pytest.mark.asyncio
async def test_draft_treats_effectless_proposal_as_speech():
    class SpeechDraftAdapter:
        async def list_models(self):
            return [{"model_id": "mock"}]

        async def generate_scene_plan(self, description, model_id, feedback=""):
            data = plan()
            data["actions"].append({"name": "提出搬运方案", "description": "公开商议", "effects": []})
            return data

    app = create_app(SpeechDraftAdapter(), db_path=None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/scenes/draft", json={"description": "断电实验室需要商议能源方案", "model_id": "mock"},
            headers={"x-principal-id": "local-owner"})
    assert response.status_code == 200
    assert len(response.json()["scene"]["action_specs"]) == 1
    assert any("普通发言" in warning for warning in response.json()["warnings"])


@pytest.mark.asyncio
async def test_draft_uses_round_limit_for_initially_true_target_failure():
    class DeadlineDraftAdapter:
        async def list_models(self):
            return [{"model_id": "mock"}]

        async def generate_scene_plan(self, description, model_id, feedback=""):
            data = plan()
            data["failure_conditions"] = [{"metric_index": 1, "op": "LE", "value": 0}]
            return data

    app = create_app(DeadlineDraftAdapter(), db_path=None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/scenes/draft", json={"description": "三轮内需要完成实验室能源方案", "model_id": "mock"},
            headers={"x-principal-id": "local-owner"})
    assert response.status_code == 200
    assert len(response.json()["scene"]["termination_policy"]["hard_conditions"]) == 1
    assert response.json()["scene"]["termination_policy"]["round_limit_outcome"] == "FAILED"
    assert any("回合上限" in warning for warning in response.json()["warnings"])


@pytest.mark.asyncio
async def test_draft_repairs_previous_plan_instead_of_regenerating():
    class RepairingAdapter:
        generated = 0
        repaired = 0

        async def list_models(self):
            return [{"model_id": "mock"}]

        async def generate_scene_plan(self, description, model_id, feedback=""):
            self.generated += 1
            data = plan()
            data["failure_conditions"] = [{"metric_index": 0, "op": "LE", "value": 68}]
            return data

        async def repair_scene_plan(self, description, previous, feedback, model_id):
            self.repaired += 1
            assert previous["metrics"][0]["initial"] == 68
            assert "failure condition already holds" in feedback
            previous["failure_conditions"] = []
            return previous

    adapter = RepairingAdapter()
    app = create_app(adapter, db_path=None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/scenes/draft", json={"description": "实验室需要在四轮内完成能源方案", "model_id": "mock"},
            headers={"x-principal-id": "local-owner"})
    assert response.status_code == 200, response.text
    assert adapter.generated == adapter.repaired == 1


@pytest.mark.asyncio
async def test_draft_flags_input_number_missing_from_executable_plan():
    class NumberAdapter:
        async def list_models(self):
            return [{"model_id": "mock"}]

        async def generate_scene_plan(self, description, model_id, feedback=""):
            return plan()

    app = create_app(NumberAdapter(), db_path=None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/scenes/draft", json={"description": "实验室有99名人员需要制定能源方案", "model_id": "mock"},
            headers={"x-principal-id": "local-owner"})
    assert response.status_code == 200
    assert any("99" in warning and "可执行配置" in warning for warning in response.json()["warnings"])


def test_evacuation_scale_and_countdown_at_boundary():
    data = plan()
    data["name"] = "最后一艘撤离船"
    data["metrics"] = [{"name": "已撤离人数", "initial": 0, "maximum": 3000, "unit": "人"},
                       {"name": "剩余时间", "initial": 48, "maximum": 48, "unit": "小时"}]
    data["actions"][0]["effects"] = [{"index": 0, "op": "add", "value": 500}]
    data["target_metric_index"] = 0
    data["target_value"] = 2500
    data["success_conditions"] = [{"metric_index": 0, "op": "GE", "value": 2500}]
    data["max_rounds"] = 5
    data["settlement_effects"] = [{"index": 1, "op": "sub", "value": 1}]
    scene = compile_plan(DraftPlan.model_validate(data), "mock")
    assert scene.world_state_schema.fields[0].max == 3000
    assert scene.world_state_schema.fields[0].unit == "人"
    assert scene.initial_world_state["metric_2"] == 48


def test_round_countdown_cannot_be_consumed_by_action_and_settlement():
    data = plan()
    data["metrics"].append({"name": "剩余轮数", "initial": 12, "maximum": 12})
    data["actions"][0]["effects"].append({"index": 2, "op": "sub", "value": 1})
    data["settlement_effects"] = [{"index": 2, "op": "sub", "value": 1}]
    with pytest.raises(ValueError, match="round countdown"):
        DraftPlan.model_validate(data)


def test_round_countdown_can_use_settlement_alone():
    data = plan()
    data["metrics"].append({"name": "剩余轮数", "initial": 12, "maximum": 12})
    data["settlement_effects"] = [{"index": 2, "op": "sub", "value": 1}]
    scene = compile_plan(DraftPlan.model_validate(data), "mock")
    assert scene.settlement_spec.effects[0].path == "metric_3"


def test_draft_rejects_failure_that_success_implies():
    data = plan()
    data["failure_conditions"] = [{"metric_index": 1, "op": "GE", "value": 100}]
    with pytest.raises(ValueError, match="success conditions imply"):
        DraftPlan.model_validate(data)


def test_draft_rejects_impossible_condition_threshold():
    data = plan()
    data["failure_conditions"] = [{"metric_index": 0, "op": "LE", "value": -1}]
    with pytest.raises(ValueError, match="threshold must be within"):
        DraftPlan.model_validate(data)


def test_draft_rejects_goal_blocked_by_nonrenewable_workers():
    data = plan()
    data["metrics"] = [{"name": "待搬书箱", "initial": 30, "maximum": 30},
                       {"name": "可用人手", "initial": 2, "maximum": 2}]
    data["actions"][0]["effects"] = [{"index": 0, "op": "sub", "value": 1},
                                      {"index": 1, "op": "sub", "value": 1}]
    data["target_metric_index"] = 0
    data["target_value"] = 0
    data["success_conditions"] = [{"metric_index": 0, "op": "LE", "value": 0}]
    data["max_rounds"] = 30
    with pytest.raises(ValueError, match="nonrenewable resource"):
        DraftPlan.model_validate(data)


def test_draft_rejects_more_work_than_all_turns_can_perform():
    data = plan()
    data["metrics"][1] = {"name": "待搬书箱", "initial": 30, "maximum": 30}
    data["actions"][0]["effects"] = [{"index": 1, "op": "sub", "value": 1}]
    data["target_value"] = 0
    data["success_conditions"] = [{"metric_index": 1, "op": "LE", "value": 0}]
    data["max_rounds"] = 6
    with pytest.raises(ValueError, match="within round limit"):
        DraftPlan.model_validate(data)


def test_draft_rejects_exact_goal_with_incompatible_action_steps():
    data = plan()
    data["metrics"][1] = {"name": "待搬书箱", "initial": 30, "maximum": 30}
    data["actions"][0]["effects"] = [{"index": 1, "op": "sub", "value": 4}]
    data["target_value"] = 0
    data["success_conditions"] = [{"metric_index": 1, "op": "LE", "value": 0}]
    data["max_rounds"] = 6
    with pytest.raises(ValueError, match="action step sizes"):
        DraftPlan.model_validate(data)
    data["actions"].append({"name": "最后一批", "description": "搬两箱",
        "allowed_agent_indices": [0, 1], "effects": [{"index": 1, "op": "sub", "value": 2}]})
    DraftPlan.model_validate(data)


def test_draft_rejects_full_capacity_as_failure_when_goal_uses_all_space():
    data = plan()
    data["metrics"] = [
        {"name": "待搬书箱", "initial": 30, "maximum": 30},
        {"name": "仓库剩余容量", "initial": 20, "maximum": 20},
        {"name": "活动室剩余容量", "initial": 10, "maximum": 10},
    ]
    data["actions"] = [
        {"name": "搬到仓库", "description": "搬四箱", "allowed_agent_indices": [0, 1],
         "effects": [{"index": 0, "op": "sub", "value": 4},
                     {"index": 1, "op": "sub", "value": 4}]},
        {"name": "搬到活动室", "description": "搬两箱", "allowed_agent_indices": [0, 1],
         "effects": [{"index": 0, "op": "sub", "value": 2},
                     {"index": 2, "op": "sub", "value": 2}]},
    ]
    data["target_metric_index"] = 0
    data["target_value"] = 0
    data["success_conditions"] = [{"metric_index": 0, "op": "LE", "value": 0}]
    data["failure_conditions"] = [{"metric_index": 1, "op": "LE", "value": 0}]
    data["max_rounds"] = 10
    with pytest.raises(ValueError, match="full capacity is a failure condition"):
        DraftPlan.model_validate(data)
    data["failure_conditions"] = []
    DraftPlan.model_validate(data)


def test_draft_counts_replenishment_turns_for_exact_goal():
    data = plan()
    data["agents"].append({"name": "居民", "role": "搬运", "persona": "积极", "goal": "保护书"})
    data["metrics"] = [{"name": "待搬书箱", "initial": 30, "maximum": 30},
                       {"name": "可用人手", "initial": 2, "maximum": 2}]
    data["actions"] = [
        {"name": "搬书", "description": "每次搬两箱", "allowed_agent_indices": [0, 1, 2],
         "effects": [{"index": 0, "op": "sub", "value": 2},
                     {"index": 1, "op": "sub", "value": 1}]},
        {"name": "补充人手", "description": "补充一组人手", "allowed_agent_indices": [0, 1, 2],
         "effects": [{"index": 1, "op": "add", "value": 1}]}]
    data["target_metric_index"] = 0
    data["target_value"] = 0
    data["success_conditions"] = [{"metric_index": 0, "op": "EQ", "value": 0}]
    data["max_rounds"] = 6
    with pytest.raises(ValueError, match="replenishment turns"):
        DraftPlan.model_validate(data)


def test_draft_counts_recovery_turns_of_authorized_role():
    data = plan()
    data["agents"].append({"name": "居民", "role": "搬运", "persona": "积极", "goal": "保护书"})
    data["metrics"] = [{"name": "已搬书箱", "initial": 0, "maximum": 30},
                       {"name": "可用人手", "initial": 2, "maximum": 2}]
    data["actions"] = [
        {"name": "搬书", "description": "搬三箱", "allowed_agent_indices": [1, 2],
         "effects": [{"index": 0, "op": "add", "value": 3},
                     {"index": 1, "op": "sub", "value": 1}]},
        {"name": "休息", "description": "恢复人手", "allowed_agent_indices": [1],
         "effects": [{"index": 1, "op": "set", "value": 2}]}]
    data["target_metric_index"] = 0
    data["target_value"] = 30
    data["success_conditions"] = [{"metric_index": 0, "op": "GE", "value": 30}]
    data["max_rounds"] = 6
    with pytest.raises(ValueError, match="role and resource turns"):
        DraftPlan.model_validate(data)


def test_bounded_path_search_respects_role_permissions():
    scene = compile_plan(DraftPlan.model_validate(plan()), "mock")
    assert goal_reachable(scene) is True
    scene.agents[1].allowed_tools = []
    assert goal_reachable(scene) is False


@pytest.mark.asyncio
async def test_preflight_flags_speech_named_action_that_changes_world():
    data = plan()
    data["actions"][0]["name"] = "提出方案"
    scene = compile_plan(DraftPlan.model_validate(data), "mock")
    app = create_app(adapter=UnconfiguredAdapter(), db_path=None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/scenes/validate", json={"scene": scene.model_dump(mode="json")},
            headers={"x-principal-id": "local-owner"})
    assert response.status_code == 200, response.text
    assert any("名称像发言" in warning for warning in response.json()["warnings"])


def test_observatory_capacity_keeps_all_actions_and_metrics():
    data = plan()
    data["name"] = "晨星号太阳风暴危机"
    data["metrics"] += [{"name": f"资源{i}", "initial": 10, "maximum": 20} for i in range(5)]
    data["actions"] += [{"name": f"行动{i}", "description": "调度资源",
                         "allowed_agent_indices": [0], "effects": [{"index": 1, "op": "add", "value": 1}]}
                        for i in range(7)]
    scene = compile_plan(DraftPlan.model_validate(data), "mock")
    assert len(scene.world_state_schema.fields) == 7
    assert len(scene.action_specs) == 8


@pytest.mark.asyncio
async def test_observatory_semantic_stalemate_is_advisory():
    scene = compile_plan(DraftPlan.model_validate(plan()), "mock")
    adapter = ScriptedModelAdapter([ModelOutput(semantic_result=SemanticTerminationResult(
        goal_completion=0.2, consensus_score=0.1, stalemate_score=1,
        should_terminate=True, outcome="STALEMATE", reason="没有可行路径", evidence_refs=[]))])
    session = Session(scene, ModelGateway(adapter, scene.model_failure_policy, scene.budget_policy))
    await session.start(paused=True)
    assert await session._semantic_check("PERIODIC") is False
    assert session.status == "PAUSED"
    decided = next(e for e in session.events.events if e.type.value == "semantic_termination_decided")
    assert decided.payload["should_terminate"] is False
