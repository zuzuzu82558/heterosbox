import copy
import pytest
from heterosbox.arbiter import SceneEvaluationError, WriteArbiter
from heterosbox.expr import ExpressionError, ExpressionEvaluator, freeze
from heterosbox.models import ActionPayload, SceneRuntimeConfig


def test_atomic_accept_reject(scene):
    arbiter = WriteArbiter(scene)
    world = copy.deepcopy(scene.initial_world_state)
    ok = arbiter.execute(world, ActionPayload(action_type="spend", parameters={"n": 3}), "a", {})
    assert ok.world_state["gold"] == 7 and world["gold"] == 10
    denied = arbiter.execute(world, ActionPayload(action_type="spend", parameters={"n": 30}), "a", {})
    assert denied.feedback.reason_code == "NO_GOLD" and denied.feedback.retry_allowed
    assert denied.world_state == world


def test_world_and_working_are_distinct(scene_dict):
    effects = scene_dict["action_specs"][0]["effects"]
    effects[:] = [{"op": "set", "path": "gold", "value": "world['gold'] - 2"}, {"op": "set", "path": "gold", "value": "working['gold'] - 3"}]
    scene_dict["action_specs"][0]["preconditions"] = []
    scene = SceneRuntimeConfig.model_validate(scene_dict)
    result = WriteArbiter(scene).execute(scene.initial_world_state, ActionPayload(action_type="spend", parameters={"n": 1}), "a", {})
    assert result.world_state["gold"] == 5


def test_eval_error_is_system_error_without_mutation(scene_dict):
    scene_dict["action_specs"][0]["preconditions"][0]["expr"] = "world['missing'] > 0"
    scene = SceneRuntimeConfig.model_validate(scene_dict)
    with pytest.raises(SceneEvaluationError, match="precondition") as error:
        WriteArbiter(scene).execute(scene.initial_world_state, ActionPayload(action_type="spend", parameters={"n": 1}), "a", {})
    assert error.value.code == "RULE_EVAL_ERROR"
    assert scene.initial_world_state["gold"] == 10


def test_restricted_ast_and_invariant(scene_dict):
    with pytest.raises(ExpressionError):
        ExpressionEvaluator().evaluate("world.__class__", {"world": freeze({})}, phase="effect")
    scene_dict["world_state_schema"]["invariants"] = ["working['gold'] == world['gold']"]
    scene = SceneRuntimeConfig.model_validate(scene_dict)
    with pytest.raises(SceneEvaluationError) as error:
        WriteArbiter(scene).execute(scene.initial_world_state, ActionPayload(action_type="spend", parameters={"n": 1}), "a", {})
    assert error.value.code == "INVARIANT_EVAL_ERROR"
