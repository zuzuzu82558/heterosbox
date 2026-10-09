import pytest
from heterosbox.arbiter import WriteArbiter
from heterosbox.events import EventStore
from heterosbox.gateway import ModelGateway, ModelOutput, ScriptedModelAdapter
from heterosbox.models import ActionPayload, AgentResponse, PrivateAgentState, PrivateStateUpdate
from heterosbox.projection import ProjectionService
from heterosbox.turn import TurnExecutor


@pytest.mark.asyncio
async def test_read_loop_and_speech_commit(scene_dict):
    scene_dict["action_specs"].append({"action_type": "inspect", "kind": "READ", "description": "read", "params_schema": {"type": "object"}, "visibility": {"proposal": {"audience": "SELF", "owner_id": "a"}, "result": {"audience": "SELF", "owner_id": "a"}}})
    scene_dict["agents"][0]["allowed_tools"].append("inspect")
    from heterosbox.models import SceneRuntimeConfig
    scene = SceneRuntimeConfig.model_validate(scene_dict)
    adapter = ScriptedModelAdapter([
        ModelOutput(response=AgentResponse(speech="draft-CANARY"), read_action=ActionPayload(action_type="inspect")),
        ModelOutput(response=AgentResponse(speech="done", private_update=PrivateStateUpdate(current_goal="finished")), write_action=ActionPayload(action_type="spend", parameters={"n": 2})),
    ])
    gateway = ModelGateway(adapter, scene.model_failure_policy, scene.budget_policy)
    event_store = EventStore("s")
    states = {"a": PrivateAgentState(), "b": PrivateAgentState()}
    messages = []
    result, world = await TurnExecutor(scene, gateway, ProjectionService(scene), WriteArbiter(scene)).execute_turn("a", scene.initial_world_state, states, messages, event_store, round_index=0, turn_index=0)
    assert result.completed and world["gold"] == 8
    assert gateway.ledger.llm_calls == 2
    assert states["a"].current_goal == "finished"
    assert [m.content for m in messages] == ["done"]
    assert "draft-CANARY" not in str(event_store.events)


@pytest.mark.asyncio
async def test_failed_action_never_broadcasts_success(scene):
    adapter = ScriptedModelAdapter([ModelOutput(response=AgentResponse(speech="I transferred 100"), write_action=ActionPayload(action_type="spend", parameters={"n": 100}))])
    gateway = ModelGateway(adapter, scene.model_failure_policy, scene.budget_policy)
    event_store = EventStore("s")
    states = {"a": PrivateAgentState(), "b": PrivateAgentState()}
    messages = []
    _, world = await TurnExecutor(scene, gateway, ProjectionService(scene), WriteArbiter(scene)).execute_turn("a", scene.initial_world_state, states, messages, event_store, round_index=0, turn_index=0)
    assert world["gold"] == 10
    assert "transferred" not in str(messages)
