import pytest
from heterosbox.gateway import ModelGateway, ModelOutput, ScriptedModelAdapter
from heterosbox.models import ActionPayload, AgentResponse, SceneRuntimeConfig
from heterosbox.session import CommandRejected, Session


@pytest.mark.asyncio
async def test_step_round_boundary_and_fork(scene_dict):
    scene_dict["settlement_spec"] = {"enabled": True, "effects": [{"op": "add", "path": "gold", "value": 1}], "visibility": {"audience": "ALL_AGENTS"}}
    scene = SceneRuntimeConfig.model_validate(scene_dict)
    adapter = ScriptedModelAdapter([ModelOutput(response=AgentResponse(speech="a"), write_action=ActionPayload(action_type="spend", parameters={"n": 2})), ModelOutput(response=AgentResponse(speech="b"))])
    gateway = ModelGateway(adapter, scene.model_failure_policy, scene.budget_policy)
    session = Session(scene, gateway)
    await session.start(paused=True)
    with pytest.raises(CommandRejected):
        await session.command("RESUME", principal_id="owner", expected_version=999)
    await session.command("STEP", principal_id="owner")
    assert session.status == "PAUSED" and session.world["gold"] == 8
    assert session.scheduler.next_agent_offset == 1
    await session.command("STEP", principal_id="owner")
    assert session.status == "PAUSED" and session.world["gold"] == 9
    assert session.scheduler.round_index == 1 and not session.scheduler.settlement_done
    assert sum(e.type.value == "round_settlement_committed" for e in session.events.events) == 1
    checkpoint = next(c for c in session.checkpoints if c.kind == "ROUND")
    assert "settlement_done" not in type(checkpoint).model_fields
    fork = session.fork(checkpoint.checkpoint_id, ModelGateway(ScriptedModelAdapter([]), scene.model_failure_policy, scene.budget_policy))
    assert fork.world == checkpoint.world_state and fork.scheduler.settlement_done
    assert fork.events.branch_id != session.events.branch_id


@pytest.mark.asyncio
async def test_budget_pause_needs_update(scene_dict):
    scene_dict["budget_policy"] = {"max_llm_calls": 0, "on_exhausted": "PAUSE"}
    scene = SceneRuntimeConfig.model_validate(scene_dict)
    session = Session(scene, ModelGateway(ScriptedModelAdapter([]), scene.model_failure_policy, scene.budget_policy))
    await session.start(paused=True)
    await session.command("STEP", principal_id="owner")
    assert session.status == "PAUSED" and session.budget_blocked
    with pytest.raises(CommandRejected):
        await session.command("RESUME", principal_id="owner")
    await session.command("UPDATE_BUDGET", principal_id="owner", payload={"max_llm_calls": 1})
    assert not session.budget_blocked
