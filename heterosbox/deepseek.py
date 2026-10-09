"""DeepSeek Chat Completions adapter; credentials are read from process environment."""
import json
import os
import re
import time
from typing import Any

import httpx

from .gateway import ModelOutput
from .models import ActionPayload, AgentResponse, PrivateStateUpdate, SemanticTerminationResult


class DeepSeekAdapter:
    def __init__(self, *, api_key: str | None = None, client: httpx.AsyncClient | None = None,
        base_url: str = "https://api.deepseek.com", timeout_s: float = 60.0,
        pricing_per_million_tokens: dict[str, tuple[float, float]] | None = None,
        max_output_tokens: int = 512, api_key_env: str = "DEEPSEEK_API_KEY",
        provider: str = "DeepSeek", include_thinking: bool = True,
        token_limit_field: str = "max_tokens"):
        self._api_key = api_key
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.pricing = pricing_per_million_tokens or {}
        self.max_output_tokens = max_output_tokens
        self.api_key_env = api_key_env
        self.provider = provider
        self.include_thinking = include_thinking
        self.token_limit_field = token_limit_field
        self._models_cache: tuple[float, list[dict]] | None = None

    async def list_models(self) -> list[dict]:
        if self._models_cache and time.monotonic() - self._models_cache[0] < 60:
            return self._models_cache[1]
        key = self._api_key or os.getenv(self.api_key_env)
        if not key:
            raise RuntimeError(f"{self.api_key_env} is not configured")
        headers = {"Authorization": f"Bearer {key}"}
        if self.client is None:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                response = await client.get(f"{self.base_url}/models", headers=headers)
        else:
            response = await self.client.get(f"{self.base_url}/models", headers=headers)
        response.raise_for_status()
        models = [{"model_id": item["id"], "name": item.get("name") or item["id"],
                "provider": self.provider, "context_window": item.get("context_window"),
                 "available": True} for item in response.json().get("data", []) if item.get("id")]
        self._models_cache = (time.monotonic(), models)
        return models

    async def generate_scene_plan(self, description: str, model_id: str, feedback: str = "") -> dict:
        from .compiler import draft_prompt
        messages = [
            {"role": "system", "content": "You are a simulation scenario designer. Output valid JSON only. All human-readable values must be in Simplified Chinese."},
            {"role": "user", "content": draft_prompt(description) + (f" Previous JSON was invalid: {feedback[:900]}. Correct it." if feedback else "")},
        ]
        return await self._scene_json(model_id, messages)

    async def repair_scene_plan(self, description: str, previous: dict, feedback: str, model_id: str) -> dict:
        from .compiler import draft_prompt
        messages = [
            {"role": "system", "content": "You repair a simulation scenario JSON. Return the complete corrected JSON object only. Preserve the user's numeric constraints, roles, private facts, and working rules. All human-readable values must be in Simplified Chinese."},
            {"role": "user", "content": draft_prompt(description)},
            {"role": "assistant", "content": json.dumps(previous, ensure_ascii=False, separators=(",", ":"))},
            {"role": "user", "content": "Repair the previous JSON using these exact validation findings: " + feedback[:1600] +
                " Keep valid fields. Check every failure condition against initial values; if unfinished work only fails at the deadline, use max_rounds. Check that authorized roles can finish the target within the round limit, including resource recovery turns. Return the full corrected JSON."},
        ]
        return await self._scene_json(model_id, messages)

    async def _scene_json(self, model_id: str, messages: list[dict]) -> dict:
        key = self._api_key or os.getenv(self.api_key_env)
        if not key:
            raise RuntimeError(f"{self.api_key_env} is not configured")
        body = {"model": model_id, "messages": messages,
            "response_format": {"type": "json_object"},
            "stream": False, self.token_limit_field: 6000}
        if self.include_thinking:
            body["thinking"] = {"type": "disabled"}
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        if self.client is None:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                response = await client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
        else:
            response = await self.client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
        response.raise_for_status()
        choice = response.json()["choices"][0]
        content = choice["message"].get("content") or ""
        if not content:
            raise ValueError(f"model returned an empty scene draft (finish_reason={choice.get('finish_reason')})")
        return json.loads(content)

    async def call(self, model_id: str, context: dict[str, Any]) -> ModelOutput:
        key = self._api_key or os.getenv(self.api_key_env)
        if not key:
            raise RuntimeError(f"{self.api_key_env} is not configured")
        is_judge = context.get("kind") == "SEMANTIC_JUDGE"
        if is_judge:
            system = ("Return only a JSON object with goal_completion, consensus_score, stalemate_score, "
                      "should_terminate, outcome, reason, evidence_refs. Scores are numbers from 0 to 1. "
                      "When should_terminate is false, outcome must be null. When true, outcome must be one of "
                      "WINNER, DRAW, CONSENSUS, GOAL_REACHED, STALEMATE, HUMAN_TERMINATED, FAILED, MAX_TURNS. "
                      "Use only event_id values supplied in evidence_refs; do not invent evidence. "
                      "Judge the declared goals using the world and evidence. Never modify world state. "
                      "Write reason in Simplified Chinese.")
            tools = None
        else:
            system = ("You are a participant in a bounded simulation. World facts are only those in the authorized view. "
                      "The identity object is your authoritative name, role, persona and goals. Speak only as that participant; "
                      "do not impersonate another role or ask the observer to decide unless a declared tool requires it. "
                      "Use only the declared tools. Do not claim a WRITE succeeded before feedback. "
                      "Each turn is a scarce decision opportunity. If an authorized action safely advances your goal, "
                      "propose it this turn instead of repeating discussion; use speech-only turns when action is unsafe or unavailable. "
                      "Use time_budget to judge urgency; do not assume another round remains when it says one remains. "
                      "Use Chinese display names rather than internal agent, action or metric IDs in speech. "
                      "Final content may be JSON with speech, target_channel, recipient_ids and private_update. "
                      "If target_channel is present, use exactly PUBLIC or WHISPER; "
                      "do not include hidden reasoning. Write all speech and human-readable text in Simplified Chinese.")
            tools = self._tools(context.get("tool_specs", []))
        body: dict[str, Any] = {
            "model": model_id,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": json.dumps(context, ensure_ascii=False, default=str)}],
            "stream": False,
            self.token_limit_field: self.max_output_tokens,
        }
        if self.include_thinking:
            body["thinking"] = {"type": "disabled"}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if is_judge:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        if self.client is None:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                response = await client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
        else:
            response = await self.client.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
        response.raise_for_status()
        data = response.json()
        choice = data["choices"][0]
        if choice.get("finish_reason") not in ("stop", "tool_calls"):
            raise RuntimeError(f"{self.provider} response incomplete: {choice.get('finish_reason')}")
        message = choice["message"]
        usage = data.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        prices = self.pricing.get(model_id)
        cost_usd = (prompt_tokens * prices[0] + completion_tokens * prices[1]) / 1_000_000 if prices else 0.0
        if is_judge:
            raw_decision = json.loads(message.get("content") or "{}")
            if isinstance(raw_decision, dict) and isinstance(raw_decision.get("outcome"), str):
                raw_decision["outcome"] = raw_decision["outcome"].strip().upper()
            if isinstance(raw_decision, dict) and raw_decision.get("should_terminate") is False:
                raw_decision["outcome"] = None
            decision = SemanticTerminationResult.model_validate(raw_decision)
            return ModelOutput(semantic_result=decision, tokens=usage.get("total_tokens", prompt_tokens + completion_tokens),
                cost_usd=cost_usd, request_id=data.get("id"), finish_reason=choice.get("finish_reason", "stop"))
        text = message.get("content") or ""
        try:
            response_json = json.loads(text)
        except json.JSONDecodeError:
            fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.IGNORECASE | re.DOTALL)
            if fenced:
                try:
                    response_json = json.loads(fenced.group(1))
                except json.JSONDecodeError:
                    response_json = None
            else:
                response_json = None
        if isinstance(response_json, dict):
            channel = response_json.get("target_channel")
            if isinstance(channel, str) and channel.strip().upper() in ("PUBLIC", "WHISPER"):
                response_json["target_channel"] = channel.strip().upper()
            agent_response = AgentResponse.model_validate(response_json)
        else:
            agent_response = AgentResponse(speech=text)
        output = ModelOutput(response=agent_response, tokens=usage.get("total_tokens", prompt_tokens + completion_tokens),
            cost_usd=cost_usd, request_id=data.get("id"), finish_reason=choice.get("finish_reason", "stop"))
        declared = {item["action_type"]: item["kind"] for item in context.get("tool_specs", [])}
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            category = {"heterosbox_read": "READ", "heterosbox_write": "WRITE", "heterosbox_control": "CONTROL"}.get(function.get("name"))
            if category is None:
                continue
            arguments = json.loads(function.get("arguments") or "{}")
            action = ActionPayload.model_validate(arguments)
            if declared.get(action.action_type) != category:
                raise ValueError("model called undeclared action")
            slot = {"READ": "read_action", "WRITE": "write_action", "CONTROL": "control_action"}[category]
            if getattr(output, slot) is not None:
                raise ValueError("multiple actions of same kind in one response")
            setattr(output, slot, action)
        return output

    @staticmethod
    def _tools(specs: list[dict]) -> list[dict]:
        categories = {item["kind"] for item in specs}
        return [{"type": "function", "function": {
            "name": f"heterosbox_{kind.lower()}",
            "description": f"Propose one declared {kind} action. The engine validates permissions and execution.",
            "parameters": {"type": "object", "properties": {
                "action_type": {"type": "string", "enum": [item["action_type"] for item in specs if item["kind"] == kind]},
                "parameters": {"type": "object"}}, "required": ["action_type", "parameters"]}}}
            for kind in ("READ", "WRITE", "CONTROL") if kind in categories]
