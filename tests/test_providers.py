"""Multi-provider model discovery and routing without live credentials."""
import httpx
import pytest

from heterosbox.deepseek import DeepSeekAdapter
from heterosbox.providers import MultiProviderAdapter


@pytest.mark.asyncio
async def test_provider_namespaces_and_request_shapes():
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/models"):
            name = "mimo-v2.6-flash" if "mimo" in request.url.host else "gemini-2.5-flash"
            return httpx.Response(200, json={"data": [{"id": name}]})
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": '{"speech":"已知悉"}'}}], "usage": {"total_tokens": 4}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        router = MultiProviderAdapter({
            "mimo": DeepSeekAdapter(api_key="test", client=client,
                base_url="https://api.xiaomimimo.com/v1", api_key_env="MIMO_API_KEY",
                provider="MiMo", include_thinking=True,
                token_limit_field="max_completion_tokens"),
            "google": DeepSeekAdapter(api_key="test", client=client,
                base_url="https://generativelanguage.googleapis.com/v1beta/openai",
                api_key_env="GEMINI_API_KEY", provider="Google", include_thinking=False),
        })
        listed = await router.list_models()
        assert [(item["provider"], item["model_id"]) for item in listed] == [
            ("MiMo", "mimo:mimo-v2.6-flash"), ("Google", "google:gemini-2.5-flash")]
        await router.call("mimo:mimo-v2.6-flash", {"kind": "AGENT"})
        await router.call("google:gemini-2.5-flash", {"kind": "AGENT"})
    import json
    bodies = [json.loads(request.content) for request in requests if request.url.path.endswith("/chat/completions")]
    assert bodies[0]["model"] == "mimo-v2.6-flash"
    assert "max_completion_tokens" in bodies[0] and bodies[0]["thinking"] == {"type": "disabled"}
    assert bodies[1]["model"] == "gemini-2.5-flash"
    assert "max_tokens" in bodies[1] and "thinking" not in bodies[1]


@pytest.mark.asyncio
async def test_one_provider_failure_does_not_hide_other_models():
    def respond(request):
        if "mimo" in request.url.host:
            return httpx.Response(401)
        return httpx.Response(200, json={"data": [{"id": "gemini-2.5-flash"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        router = MultiProviderAdapter({
            "mimo": DeepSeekAdapter(api_key="test", client=client,
                base_url="https://api.xiaomimimo.com/v1"),
            "google": DeepSeekAdapter(api_key="test", client=client,
                base_url="https://generativelanguage.googleapis.com/v1beta/openai",
                provider="Google"),
        })
        assert len(await router.list_models()) == 1
        assert "mimo" in router.connection_errors
        assert "google" not in router.connection_errors
