import pytest
from pathlib import Path
from uuid import uuid4
from httpx import ASGITransport, AsyncClient
from heterosbox.api import create_app
from heterosbox.gateway import ModelOutput, ScriptedModelAdapter
from heterosbox.models import ActionPayload, AgentResponse


@pytest.mark.asyncio
async def test_api_snapshot_acl_resume(scene):
    adapter = ScriptedModelAdapter([ModelOutput(response=AgentResponse(speech="hello"), write_action=ActionPayload(action_type="spend", parameters={"n": 1}))])
    database = str(Path("test_output") / f"api-{uuid4()}.sqlite3")
    app = create_app(adapter, database)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.post("/sessions", json={"scene": scene.model_dump(mode="json")})
        assert denied.status_code == 403
        headers = {"x-principal-id": "local-owner"}
        created = await client.post("/sessions", json={"scene": scene.model_dump(mode="json")}, headers=headers)
        assert created.status_code == 200
        sid = created.json()["session_id"]
        before = (await client.get(f"/sessions/{sid}/snapshot?perspective=AGENT:b", headers=headers)).json()
        assert "secret" not in before["world_state"]
        n = before["event_seq"]
        advanced = await client.post(f"/sessions/{sid}/commands", json={"name": "STEP"}, headers=headers)
        assert advanced.status_code == 200
        after = (await client.get(f"/sessions/{sid}/snapshot?perspective=AGENT:b", headers=headers)).json()
        assert after["event_seq"] > n and after["world_state"]["gold"] == 9
        historical = (await client.get(f"/sessions/{sid}/replay?perspective=AGENT:b&at_seq={n}", headers=headers)).json()
        assert historical["world_state"]["gold"] == 10
        assert historical["historical"] is True and historical["status"] is None
        stream = (await client.get(f"/sessions/{sid}/events?perspective=AGENT:b&after_seq={n}", headers=headers)).json()["events"]
        assert stream and all(event["event_seq"] > n for event in stream)
        assert "CANARY" not in str(stream)
        checkpoint_id = app.state.sessions[sid].checkpoints[-1].checkpoint_id
        forked = (await client.post(f"/sessions/{sid}/fork", json={"checkpoint_id": checkpoint_id}, headers=headers)).json()
        branch_id = forked["branch_id"]
        assert branch_id != "main"
        branch = (await client.get(f"/sessions/{sid}/snapshot?branch_id={branch_id}", headers=headers)).json()
        assert branch["world_state"]["gold"] == 9 and branch["event_seq"] > 0
    restored_app = create_app(ScriptedModelAdapter([]), database)
    assert (sid, branch_id) in restored_app.state.branches
