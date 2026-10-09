import json
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from heterosbox.api import create_app
from heterosbox.gateway import ModelOutput, ScriptedModelAdapter
from heterosbox.models import AgentResponse


def test_websocket_authorization_and_backlog(scene):
    app = create_app(ScriptedModelAdapter([ModelOutput(response=AgentResponse(speech="hello"))]),
        str(Path("test_output") / f"ws-{uuid4()}.sqlite3"))
    headers = {"x-principal-id": "local-owner"}
    with TestClient(app) as client:
        created = client.post("/sessions", json={"scene": scene.model_dump(mode="json")}, headers=headers).json()
        sid = created["session_id"]
        client.post(f"/sessions/{sid}/commands", json={"name": "STEP"}, headers=headers)
        with client.websocket_connect(f"/sessions/{sid}/ws?perspective=AGENT:b&after_seq=0", headers=headers) as websocket:
            seen = websocket.receive_json()
            assert seen["event_seq"] > 0
            assert "CANARY" not in json.dumps(seen)


def test_api_rejects_invalid_commands(scene):
    app = create_app(ScriptedModelAdapter([]), str(Path("test_output") / f"bad-{uuid4()}.sqlite3"))
    headers = {"x-principal-id": "local-owner"}
    with TestClient(app) as client:
        sid = client.post("/sessions", json={"scene": scene.model_dump(mode="json")}, headers=headers).json()["session_id"]
        result = client.post(f"/sessions/{sid}/commands", json={"name": "UNKNOWN"}, headers=headers)
        assert result.status_code == 409
        events = client.get(f"/sessions/{sid}/events?perspective=GOD", headers=headers).json()["events"]
        assert any(event["type"] == "command_rejected" for event in events)
