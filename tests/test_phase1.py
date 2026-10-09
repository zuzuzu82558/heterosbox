import pytest
from pydantic import ValidationError
from heterosbox.models import SceneRuntimeConfig, SimEvent
from heterosbox.registry import EVENT_REGISTRY, EventType


def test_registry_is_single_source():
    assert {item.value for item in EventType} == set(EVENT_REGISTRY)
    with pytest.raises(ValidationError):
        SimEvent.model_validate({"type": "made_up"})


def test_scene_static_validation(scene_dict):
    SceneRuntimeConfig.model_validate(scene_dict)
    scene_dict["agents"][0]["allowed_tools"] = ["missing"]
    with pytest.raises(ValidationError, match="allowed_tools"):
        SceneRuntimeConfig.model_validate(scene_dict)


def test_owner_must_resolve(scene_dict):
    scene_dict["world_state_schema"]["fields"][1].pop("owner_id")
    with pytest.raises(ValidationError, match="OWNER"):
        SceneRuntimeConfig.model_validate(scene_dict)


def test_write_path_must_exist(scene_dict):
    scene_dict["action_specs"][0]["effects"][0]["path"] = "nowhere"
    with pytest.raises(ValidationError, match="effect path"):
        SceneRuntimeConfig.model_validate(scene_dict)


def test_overlapping_visibility_paths_rejected(scene_dict):
    scene_dict["world_state_schema"]["fields"].append({"path": "secret.inner", "type": "str", "visibility": {"audience": "PUBLIC"}})
    with pytest.raises(ValidationError, match="overlapping"):
        SceneRuntimeConfig.model_validate(scene_dict)
