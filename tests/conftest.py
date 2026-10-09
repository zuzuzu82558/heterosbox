import pytest

from heterosbox.models import SceneRuntimeConfig


@pytest.fixture
def scene_dict():
    visible = {"audience": "PUBLIC"}
    event_visible = {"audience": "ALL_AGENTS"}
    return {
        "scene_id": "test", "scene_version": "1", "scenario_name": "test",
        "background_story": "", "interaction_mode": "cooperative",
        "world_state_schema": {"schema_version": "1", "fields": [
            {"path": "gold", "type": "int", "min": 0, "visibility": visible},
            {"path": "secret", "type": "str", "visibility": {"audience": "OWNER"}, "owner_id": "a"},
        ], "invariants": ["working['gold'] >= 0"]},
        "initial_world_state": {"gold": 10, "secret": "CANARY"},
        "action_specs": [{"action_type": "spend", "kind": "WRITE", "description": "spend", "params_schema": {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}, "preconditions": [{"expr": "world['gold'] >= params['n']", "reason_code": "NO_GOLD"}], "effects": [{"op": "sub", "path": "gold", "value": "params['n']"}], "visibility": {"proposal": event_visible, "result": event_visible}, "retry_allowed_reasons": ["NO_GOLD"]}],
        "agents": [
            {"agent_id": "a", "display_name": "A", "persona": "", "public_role": "", "model_id": "mock", "allowed_tools": ["spend"]},
            {"agent_id": "b", "display_name": "B", "persona": "", "public_role": "", "model_id": "mock", "allowed_tools": ["spend"]},
        ],
        "scheduler_policy": "fixed_round_robin", "random_seed": 5,
        "judging_policy": {"enabled": False}, "leaderboard_config": {"enabled": False, "leaderboard_type": "contribution"},
        "termination_policy": {"max_turns": 10}, "early_semantic_trigger_policy": {"enabled": False},
    }


@pytest.fixture
def scene(scene_dict):
    return SceneRuntimeConfig.model_validate(scene_dict)
