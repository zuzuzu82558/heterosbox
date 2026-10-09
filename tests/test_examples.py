import json
from pathlib import Path

from heterosbox.arbiter import WriteArbiter
from heterosbox.models import ActionPayload, SceneRuntimeConfig
from heterosbox.projection import ProjectionService


def test_handwritten_scenes_compile_and_transact():
    for name in ("cooperative", "competitive"):
        scene = SceneRuntimeConfig.model_validate(json.loads((Path("examples") / f"{name}.json").read_text(encoding="utf-8")))
        arbiter = WriteArbiter(scene)
        actor = scene.agents[0]
        result = arbiter.execute(scene.initial_world_state, ActionPayload(action_type=actor.allowed_tools[0]),
            actor.agent_id, {"round_index": 0, "turn_index": 0, "branch_id": "main"})
        assert result.feedback.status == "ACCEPTED"
        assert result.changes
        ProjectionService(scene)
