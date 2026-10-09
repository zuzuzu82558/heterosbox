from pathlib import Path
from uuid import uuid4
import json

from fastapi.testclient import TestClient

from heterosbox.api import create_app
from heterosbox.gateway import ModelOutput
from heterosbox.models import ActionPayload, AgentResponse, SemanticTerminationResult


class ProductAdapter:
    async def list_models(self):
        return [{"model_id": "test-model", "name": "Test model", "provider": "Test", "available": True}]

    async def generate_scene_plan(self, description, model_id, feedback=""):
        return {
            "name": "社区能源协商", "story": "两个角色合作提升社区能源稳定性。",
            "mode": "cooperative", "shared_goal": "将能源稳定性提高到 1",
            "agents": [
                {"name": "研究员", "role": "研究", "persona": "谨慎分析", "goal": "找到稳定方案"},
                {"name": "工程师", "role": "实施", "persona": "务实执行", "goal": "完成升级"}],
            "metrics": [{"name": "能源稳定性", "initial": 0, "maximum": 3}],
            "actions": [{"name": "升级", "description": "修复能源设施", "metric_index": 0, "delta": 1}],
            "target_metric_index": 0, "target_value": 1,
        }

    async def call(self, model_id, context):
        if context.get("kind") == "SEMANTIC_JUDGE":
            refs = [item["event_id"] for item in context["evidence"] if item["type"] == "world_state_delta"]
            return ModelOutput(semantic_result=SemanticTerminationResult(
                goal_completion=1, consensus_score=.5, stalemate_score=0,
                should_terminate=False, reason="目标指标已达成", evidence_refs=refs))
        return ModelOutput(response=AgentResponse(speech="同意修复能源设施"),
            write_action=ActionPayload(action_type="action_1", parameters={}))


def test_draft_to_report_and_evidence_replay():
    db = str(Path("test_output") / f"product-{uuid4()}.sqlite3")
    app = create_app(ProductAdapter(), db)
    headers = {"x-principal-id": "local-owner"}
    with TestClient(app) as client:
        assert client.get("/app/").status_code == 200
        models = client.get("/models", headers=headers).json()
        assert models["models"][0]["model_id"] == "test-model"
        draft = client.post("/scenes/draft", json={"description": "两个角色合作提升社区能源稳定性"}, headers=headers)
        assert draft.status_code == 200
        scene = draft.json()["scene"]
        assert len(scene["agents"]) == 2 and scene["termination_policy"]["hard_conditions"]
        scene_id = scene["scene_id"]
        assert client.get(f"/scenes/{scene_id}/visual", headers=headers).json() == {"theme": "council"}
        assert client.put(f"/scenes/{scene_id}/visual", json={"theme": "garden"}, headers=headers).json() == {"theme": "garden"}
        assert client.post("/scenes/validate", json={"scene": scene}, headers=headers).json()["valid"]
        created = client.post("/sessions", json={"scene": scene}, headers=headers).json()
        sid = created["session_id"]
        step = client.post(f"/sessions/{sid}/commands", json={"name": "STEP"}, headers=headers)
        assert step.status_code == 200
        assert step.json()["status"] == "FINISHED"
        report = client.get(f"/sessions/{sid}/report", headers=headers).json()
        assert report["outcome"] == "GOAL_REACHED"
        assert report["arbiter"]["accepted"] == 1
        assert report["judger"]["status"] == "READY"
        row = report["leaderboard"]["rows"][0]
        assert row["agent_id"] == "agent_1" and row["score"] > 0 and row["evidence_refs"]
        events = client.get(f"/sessions/{sid}/events", headers=headers).json()["events"]
        ref = next(e for e in events if e["event_id"] == row["evidence_refs"][0])
        replay = client.get(f"/sessions/{sid}/replay?at_seq={ref['event_seq']}", headers=headers).json()
        assert replay["world_state"]["metric_1"] == 1
        assert client.get("/sessions", headers=headers).json()["sessions"][0]["session_id"] == sid
    restored = create_app(ProductAdapter(), db)
    with TestClient(restored) as client:
        assert client.get(f"/scenes/{scene_id}/visual", headers=headers).json() == {"theme": "garden"}


def test_preflight_rejects_action_that_breaks_world_bounds():
    scene = json.loads((Path("examples") / "cooperative.json").read_text(encoding="utf-8"))
    scene["action_specs"][0]["effects"][0]["value"] = 4
    app = create_app(ProductAdapter(), None)
    with TestClient(app) as client:
        response = client.post("/scenes/validate", json={"scene": scene},
            headers={"x-principal-id": "local-owner"})
    assert response.status_code == 422
    assert "预检失败" in response.json()["detail"]
