import pytest
from pydantic import ValidationError

from heterosbox.arbiter import WriteArbiter
from heterosbox.compiler import DraftPlan, compile_plan
from heterosbox.gateway import ModelGateway, ModelOutput, ScriptedModelAdapter
from heterosbox.models import ActionPayload, AgentResponse, SceneRuntimeConfig
from heterosbox.session import Session


def _plan(goal_name="修复完成"):
    return {
        "name": "风暴中继站", "story": "风暴中的中继站需要修好信标并观察环境。",
        "public_story": "风暴中的中继站需要修好信标并观察环境。",
        "mode": "cooperative", "shared_goal": "完成信标修复",
        "agents": [
            {"name": "技师", "role": "维修", "persona": "审慎", "goal": "修好信标"},
            {"name": "观察员", "role": "观测", "persona": "谨慎", "goal": "监测风暴"}],
        "metrics": [
            {"name": "电量", "initial": 0, "minimum": 0, "maximum": 5},
            {"name": "风速", "initial": 0, "minimum": 0, "maximum": 5},
            {"name": goal_name, "initial": 0, "minimum": 0, "maximum": 1}],
        "facts": [{"name": "备用电池裂纹", "value": "备用电池有裂纹",
                   "visibility": "OWNER", "owner_agent_index": 0}],
        "actions": [{"name": "修复信标", "description": "修复信标",
                     "allowed_agent_indices": [0],
                     "effects": [{"index": 2, "op": "add", "value": 1}]}],
        "target_metric_index": 2, "target_value": 1,
        "success_conditions": [{"metric_index": 2, "op": "GE", "value": 1}],
        "settlement_effects": [{"index": 0, "op": "sub", "value": 1},
                               {"index": 1, "op": "add", "value": 1}],
        "max_rounds": 2,
    }


def test_environment_continues_when_another_metric_is_at_its_bound():
    scene = compile_plan(DraftPlan.model_validate(_plan()), "mock")
    result = WriteArbiter(scene).settle(scene.initial_world_state, {"round_index": 0})
    assert result.feedback.status == "ACCEPTED"
    assert result.changes == {"metric_2": 1}


def test_saved_legacy_settlement_continues_at_boundary():
    data = compile_plan(DraftPlan.model_validate(_plan()), "mock").model_dump(mode="json")
    data["settlement_spec"]["preconditions"] = [{"expr": "world['metric_1'] - 1 >= 0",
        "reason_code": "SETTLEMENT_BOUNDARY"}]
    data["settlement_spec"]["effects"] = [
        {"op": "sub", "path": "metric_1", "value": 1},
        {"op": "add", "path": "metric_2", "value": 1}]
    scene = SceneRuntimeConfig.model_validate(data)
    result = WriteArbiter(scene).settle(scene.initial_world_state, {"round_index": 0})
    assert result.feedback.status == "ACCEPTED"
    assert result.changes == {"metric_2": 1}


def test_chance_metric_cannot_be_success_evidence():
    with pytest.raises(ValidationError, match="chance or probability"):
        DraftPlan.model_validate(_plan("救援机会"))


def test_targeted_reveal_cannot_name_every_agent():
    plan = _plan()
    plan["agents"].append({"name": "通信员", "role": "通信", "persona": "审慎", "goal": "守护信息"})
    plan["actions"].append({"name": "定向传递", "description": "只向通信员传递秘密",
        "allowed_agent_indices": [0], "effects": [{"target": "FACT", "index": 0,
            "op": "reveal", "visible_to_agent_indices": [0, 1, 2]}]})
    with pytest.raises(ValidationError, match="targeted reveal cannot include every agent"):
        DraftPlan.model_validate(plan)


def test_saved_broadcast_disguised_as_targeted_is_rejected(scene_dict):
    scene_dict["agents"].append({"agent_id": "c", "display_name": "C", "persona": "",
                                  "public_role": "", "model_id": "mock", "allowed_tools": []})
    scene_dict["action_specs"].append({"action_type": "tell_all", "kind": "WRITE",
        "description": "定向披露", "params_schema": {"type": "object"},
        "effects": [{"op": "reveal", "path": "secret", "value": {
            "audience": "SPECIFIC", "audience_ids": ["a", "b", "c"]}}],
        "visibility": {"proposal": {"audience": "ALL_AGENTS"},
                       "result": {"audience": "ALL_AGENTS"}}})
    scene_dict["agents"][0]["allowed_tools"].append("tell_all")
    scene = SceneRuntimeConfig.model_validate(scene_dict)
    result = WriteArbiter(scene).execute(scene.initial_world_state,
        ActionPayload(action_type="tell_all"), "a", {"round_index": 0})
    assert result.feedback.reason_code == "INVALID_REVEAL_SCOPE"
    assert not result.reveals


@pytest.mark.asyncio
async def test_existing_chance_goal_does_not_end_as_success(scene_dict):
    scene_dict["goal_checks"] = [{"path": "gold", "op": "GE", "target": 10,
                                  "group": "SUCCESS", "label": "救援机会"}]
    scene_dict["termination_policy"]["hard_conditions"] = [{
        "condition_id": "goal_reached", "expr": "world['gold'] >= 10",
        "outcome": "GOAL_REACHED", "reason": "达成目标"}]
    scene = SceneRuntimeConfig.model_validate(scene_dict)
    adapter = ScriptedModelAdapter([])
    session = Session(scene, ModelGateway(adapter, scene.model_failure_policy, scene.budget_policy))
    await session.start(paused=True)
    assert await session._hard_termination() is False
    assert session.status == "PAUSED"


@pytest.mark.asyncio
async def test_private_fact_speech_blocked_until_authorized_reveal(scene_dict):
    scene_dict["world_state_schema"]["fields"][1]["description"] = "备用电池裂纹"
    scene_dict["initial_world_state"]["secret"] = "备用电池有裂纹"
    scene_dict["agents"].append({"agent_id": "c", "display_name": "C", "persona": "",
                                  "public_role": "", "model_id": "mock", "allowed_tools": []})
    scene_dict["action_specs"].append({"action_type": "tell_b", "kind": "WRITE",
        "description": "向 B 披露", "params_schema": {"type": "object"},
        "effects": [{"op": "reveal", "path": "secret",
                     "value": {"audience": "SPECIFIC", "audience_ids": ["a", "b"]}}],
        "visibility": {"proposal": {"audience": "ALL_AGENTS"},
                       "result": {"audience": "ALL_AGENTS"}}})
    scene_dict["agents"][0]["allowed_tools"].append("tell_b")
    scene = SceneRuntimeConfig.model_validate(scene_dict)
    adapter = ScriptedModelAdapter([
        ModelOutput(response=AgentResponse(speech="备用电池组有裂纹，大家小心")),
        ModelOutput(response=AgentResponse(speech="备用电池组有裂纹", target_channel="WHISPER",
                                           recipient_ids=["b"]),
                    write_action=ActionPayload(action_type="tell_b")),
    ])
    session = Session(scene, ModelGateway(adapter, scene.model_failure_policy, scene.budget_policy))
    await session.start(paused=True)
    await session.command("STEP", principal_id="owner")
    assert not session.messages
    assert any(e.type.value == "speech_discarded" and e.payload.get("reason") == "PRIVATE_FACT"
               for e in session.events.events)
    # Exercise the next A turn directly, after the blocked public message.
    await session.turn_executor.execute_turn("a", session.world, session.private_states,
        session.messages, session.events, round_index=0, turn_index=1)
    assert len(session.messages) == 1
    assert session.messages[0].channel == "WHISPER"
    assert session.messages[0].recipient_ids == ["b"]
