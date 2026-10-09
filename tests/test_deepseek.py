import json
import httpx
import pytest

from heterosbox.deepseek import DeepSeekAdapter


@pytest.mark.asyncio
async def test_deepseek_tool_call_normalization_without_network():
    requests = []
    def handler(request: httpx.Request):
        requests.append(request)
        response = {
            "id": "req-1",
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "content": json.dumps({"speech": "Working on it", "private_update": {"current_goal": "save gold"}}),
                    "tool_calls": [{"function": {
                        "name": "heterosbox_write",
                        "arguments": json.dumps({"action_type": "spend", "parameters": {"n": 2}}),
                    }}],
                },
            }],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        }
        return httpx.Response(200, json=response)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = DeepSeekAdapter(api_key="test-only", client=client)
        output = await adapter.call("deepseek-flash", {"world": {"gold": 10}, "tool_specs": [
            {"action_type": "spend", "kind": "WRITE", "description": "spend", "params_schema": {"type": "object"}}]})
    assert output.write_action.action_type == "spend"
    assert output.response.private_update.current_goal == "save gold"
    assert output.tokens == 30 and output.request_id == "req-1"
    assert requests[0].url.path == "/chat/completions"
    body = json.loads(requests[0].content)
    assert body["tools"][0]["function"]["name"] == "heterosbox_write"


@pytest.mark.asyncio
async def test_deepseek_normalizes_channel_case_without_changing_invalid_values():
    def response(channel):
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
            "speech": "观察苹果落下", "target_channel": channel})}}]}

    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=response(" public ")))) as client:
        output = await DeepSeekAdapter(api_key="test-only", client=client).call(
            "deepseek-flash", {"tool_specs": []})
        assert output.response.target_channel == "PUBLIC"

    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=response("private")))) as client:
        with pytest.raises(ValueError):
            await DeepSeekAdapter(api_key="test-only", client=client).call(
                "deepseek-flash", {"tool_specs": []})


@pytest.mark.asyncio
async def test_deepseek_extracts_fenced_agent_response():
    response = {"choices": [{"finish_reason": "stop", "message": {"content":
        '说明如下：\n```json\n{"speech":"按自身职责处理","target_channel":"public"}\n```'}}]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=response))) as client:
        output = await DeepSeekAdapter(api_key="test-only", client=client).call(
            "deepseek-flash", {"tool_specs": []})
    assert output.response.speech == "按自身职责处理"
    assert output.response.target_channel == "PUBLIC"


@pytest.mark.asyncio
async def test_deepseek_rejects_undeclared_tool():
    response = {"choices": [{"finish_reason": "tool_calls", "message": {"content": None,
        "tool_calls": [{"function": {"name": "heterosbox_write", "arguments": '{"action_type":"other","parameters":{}}'}}]}}]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))) as client:
        adapter = DeepSeekAdapter(api_key="test-only", client=client)
        with pytest.raises(ValueError, match="undeclared"):
            await adapter.call("deepseek-flash", {"tool_specs": [{"action_type": "spend", "kind": "WRITE"}]})


@pytest.mark.asyncio
async def test_deepseek_rejects_truncated_response():
    response = {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]}
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))) as client:
        adapter = DeepSeekAdapter(api_key="test-only", client=client)
        with pytest.raises(RuntimeError, match="incomplete"):
            await adapter.call("deepseek-flash", {"tool_specs": []})


@pytest.mark.asyncio
async def test_model_catalog_and_json_scene_draft_without_network():
    seen = []
    def handler(request: httpx.Request):
        seen.append(request)
        if request.url.path == "/models":
            return httpx.Response(200, json={"data": [{"id": "deepseek-flash", "name": "Flash", "context_window": 1000}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"name":"demo"}'}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = DeepSeekAdapter(api_key="test-only", client=client)
        assert (await adapter.list_models())[0]["model_id"] == "deepseek-flash"
        await adapter.list_models()
        assert len([item for item in seen if item.url.path == "/models"]) == 1
        assert await adapter.generate_scene_plan("a description", "deepseek-flash") == {"name": "demo"}
    body = json.loads(seen[-1].content)
    assert body["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_scene_repair_includes_previous_json_and_diagnostic():
    sent = []
    def handler(request: httpx.Request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"name":"repaired"}'}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = DeepSeekAdapter(api_key="test-only", client=client)
        repaired = await adapter.repair_scene_plan("30 boxes, 6 rounds", {"name": "old", "max_rounds": 6},
            "success target unreachable within round limit", "deepseek-flash")
    assert repaired == {"name": "repaired"}
    messages = sent[0]["messages"]
    assert json.loads(messages[2]["content"])["max_rounds"] == 6
    assert "unreachable" in messages[3]["content"]
