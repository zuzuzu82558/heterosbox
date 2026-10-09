"""Route model calls to configured OpenAI-compatible providers."""
from __future__ import annotations

import os
import httpx

from .deepseek import DeepSeekAdapter


class MultiProviderAdapter:
    def __init__(self, adapters: dict[str, DeepSeekAdapter] | None = None):
        self.adapters = adapters if adapters is not None else self._configured()
        self.connection_errors: dict[str, str] = {}

    @staticmethod
    def _configured() -> dict[str, DeepSeekAdapter]:
        providers = {}
        if os.getenv("DEEPSEEK_API_KEY"):
            providers["deepseek"] = DeepSeekAdapter()
        if os.getenv("MIMO_API_KEY"):
            providers["mimo"] = DeepSeekAdapter(
                base_url=os.getenv("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1"), api_key_env="MIMO_API_KEY",
                provider="MiMo", include_thinking=True,
                token_limit_field="max_completion_tokens", max_output_tokens=2048)
        if os.getenv("GEMINI_API_KEY"):
            providers["google"] = DeepSeekAdapter(
                base_url="https://generativelanguage.googleapis.com/v1beta/openai",
                api_key_env="GEMINI_API_KEY", provider="Google",
                include_thinking=False, max_output_tokens=2048)
        return providers

    def _route(self, model_id: str) -> tuple[DeepSeekAdapter, str]:
        if ":" in model_id:
            provider, native_id = model_id.split(":", 1)
        else:
            provider, native_id = "deepseek", model_id  # Existing scenes retain their IDs.
        if provider not in self.adapters or not native_id:
            raise ValueError(f"model provider unavailable: {provider}")
        return self.adapters[provider], native_id

    async def list_models(self) -> list[dict]:
        models = []
        self.connection_errors = {}
        for provider, adapter in self.adapters.items():
            try:
                listed = await adapter.list_models()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                try:
                    detail = exc.response.json().get("error", {})
                except ValueError:
                    detail = {}
                if detail.get("status") == "FAILED_PRECONDITION" and "location" in detail.get("message", "").lower():
                    reason = "当前地区不支持 API 使用"
                elif status in (401, 403):
                    reason = "密钥无效或未获授权"
                elif status == 429:
                    reason = "请求频率受限"
                else:
                    reason = f"接口返回 HTTP {status}"
                self.connection_errors[provider] = reason
                continue
            except httpx.RequestError:
                self.connection_errors[provider] = "网络连接失败"
                continue
            except Exception as exc:
                self.connection_errors[provider] = type(exc).__name__
                continue
            for item in listed:
                native = item["model_id"]
                lower = native.lower()
                if provider == "google" and (not lower.startswith("gemini-") or any(
                    part in lower for part in ("image", "tts", "live", "embedding", "robotics"))):
                    continue
                if provider == "mimo" and (not lower.startswith("mimo-") or any(
                    part in lower for part in ("tts", "asr", "embedding"))):
                    continue
                models.append({**item, "model_id": native if provider == "deepseek" else f"{provider}:{native}",
                               "native_model_id": native})
        return models

    async def generate_scene_plan(self, description: str, model_id: str, feedback: str = "") -> dict:
        adapter, native_id = self._route(model_id)
        return await adapter.generate_scene_plan(description, native_id, feedback)

    async def repair_scene_plan(self, description: str, previous: dict, feedback: str, model_id: str) -> dict:
        adapter, native_id = self._route(model_id)
        return await adapter.repair_scene_plan(description, previous, feedback, native_id)

    async def call(self, model_id: str, context: dict):
        adapter, native_id = self._route(model_id)
        return await adapter.call(native_id, context)
