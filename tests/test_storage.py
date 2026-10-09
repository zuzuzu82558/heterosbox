import pytest
import sqlite3
from pathlib import Path
from uuid import uuid4
from heterosbox.gateway import ModelGateway, ModelOutput, ScriptedModelAdapter
from heterosbox.models import ActionPayload, AgentResponse
from heterosbox.session import Session
from heterosbox.storage import SqliteRepository
from heterosbox.models import EventVisibility, SimEvent


@pytest.mark.asyncio
async def test_checkpoint_resume_without_model_replay(scene):
    repository = SqliteRepository(Path("test_output") / f"state-{uuid4()}.sqlite3")
    adapter = ScriptedModelAdapter([ModelOutput(response=AgentResponse(speech="hello"), write_action=ActionPayload(action_type="spend", parameters={"n": 2}))])
    session = Session(scene, ModelGateway(adapter, scene.model_failure_policy, scene.budget_policy), repository=repository)
    await session.start(paused=True)
    await session.command("STEP", principal_id="local-owner")
    restored_adapter = ScriptedModelAdapter([])
    restored = Session.restore(scene, ModelGateway(restored_adapter, scene.model_failure_policy, scene.budget_policy), repository, session.session_id, "main")
    assert restored.world == session.world
    assert restored.scheduler == session.scheduler
    assert [m.content for m in restored.messages] == ["hello"]
    assert len(restored.events.events) == len(session.events.events)
    assert restored_adapter.calls == []


def test_sqlite_event_batch_rolls_back_on_error():
    # Existing workspace directory avoids restricted OS temporary paths.
    repository = SqliteRepository(Path("test_output") / f"rollback-{uuid4()}.sqlite3")
    base = SimEvent(event_id="one", session_id="s", branch_id="main", event_seq=1,
        parent_event_id=None, round_index=0, turn_index=0, actor_id=None, type="turn_started",
        payload={}, visibility=EventVisibility(audience="OPERATOR_ONLY"), created_at=0)
    repository.save_event(base)
    new = base.model_copy(update={"event_id": "two", "event_seq": 2})
    duplicate = base.model_copy(update={"event_id": "duplicate"})
    with pytest.raises(sqlite3.IntegrityError):
        repository.save_events_batch([new, duplicate])
    assert [event.event_seq for event in repository.load_events("s", "main")] == [1]
