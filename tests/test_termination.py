import pytest
from heterosbox.gateway import ModelGateway, ModelOutput, ScriptedModelAdapter
from heterosbox.models import ActionPayload, AgentResponse, SceneRuntimeConfig, SemanticTerminationResult
from heterosbox.session import Session


@pytest.mark.asyncio
async def test_early_control_requires_judger_decision(scene_dict):
    scene_dict["action_specs"].append({"action_type": "request_check", "kind": "CONTROL", "description": "check",
        "params_schema": {"type": "object"}, "control_signal": "TASK_COMPLETION",
        "visibility": {"proposal": {"audience": "ALL_AGENTS"}, "result": {"audience": "ALL_AGENTS"}}})
    scene_dict["agents"][0]["allowed_tools"].append("request_check")
    scene_dict["early_semantic_trigger_policy"] = {"enabled": True}
    scene_dict["termination_policy"]["hard_conditions"] = [{"condition_id": "checked",
        "expr": "world['gold'] == 10", "outcome": "GOAL_REACHED", "reason": "目标已满足"}]
    scene = SceneRuntimeConfig.model_validate(scene_dict)
    adapter = ScriptedModelAdapter([
        ModelOutput(response=AgentResponse(speech="check"), control_action=ActionPayload(action_type="request_check")),
        ModelOutput(semantic_result=SemanticTerminationResult(goal_completion=1, consensus_score=0,
            stalemate_score=0, should_terminate=True, outcome="GOAL_REACHED", reason="evidence", evidence_refs=["turn:1"])),
    ])
    session = Session(scene, ModelGateway(adapter, scene.model_failure_policy, scene.budget_policy))
    await session.start(paused=True)
    await session.command("STEP", principal_id="owner")
    assert session.status == "FINISHED"
    assert len(adapter.calls) == 2
    assert any(e.type.value == "semantic_termination_decided" for e in session.events.events)
    assert session.scheduler.next_agent_offset == 1


@pytest.mark.asyncio
async def test_deterministic_hard_condition(scene_dict):
    scene_dict["termination_policy"]["hard_conditions"] = [{"condition_id": "empty", "expr": "world['gold'] == 9", "outcome": "GOAL_REACHED", "reason": "gold"}]
    scene = SceneRuntimeConfig.model_validate(scene_dict)
    adapter = ScriptedModelAdapter([ModelOutput(write_action=ActionPayload(action_type="spend", parameters={"n": 1}))])
    session = Session(scene, ModelGateway(adapter, scene.model_failure_policy, scene.budget_policy))
    await session.start(paused=True)
    await session.command("STEP", principal_id="owner")
    assert session.status == "FINISHED" and session.world["gold"] == 9
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_agent_receives_round_budget(scene_dict):
    scene_dict["termination_policy"] = {"max_turns": None, "max_rounds": 2}
    scene = SceneRuntimeConfig.model_validate(scene_dict)
    adapter = ScriptedModelAdapter([ModelOutput(response=AgentResponse(speech="先讨论"))])
    session = Session(scene, ModelGateway(adapter, scene.model_failure_policy, scene.budget_policy))
    await session.start(paused=True)
    await session.command("STEP", principal_id="owner")
    assert adapter.calls[0]["context"]["time_budget"] == {
        "current_round": 1, "max_rounds": 2, "remaining_rounds_including_current": 2}
