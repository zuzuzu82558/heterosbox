import asyncio
import pytest
from heterosbox.gateway import ModelGateway, ModelOutput
from heterosbox.models import ActionPayload, AgentResponse
from heterosbox.session import Session


class SlowAdapter:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def call(self, model_id, context):
        self.started.set()
        await self.release.wait()
        return ModelOutput(response=AgentResponse(speech="late success"), write_action=ActionPayload(action_type="spend", parameters={"n": 1}))


@pytest.mark.asyncio
async def test_immediate_stop_discards_inflight_model(scene):
    adapter = SlowAdapter()
    session = Session(scene, ModelGateway(adapter, scene.model_failure_policy, scene.budget_policy))
    await session.start()
    await asyncio.wait_for(adapter.started.wait(), timeout=1)
    await session.command("STOP", principal_id="owner")
    adapter.release.set()
    await asyncio.sleep(0)
    assert session.status == "FINISHED"
    assert session.world == scene.initial_world_state
    assert session.messages == []
    assert not any(e.type.value == "world_state_delta" for e in session.events.events)
