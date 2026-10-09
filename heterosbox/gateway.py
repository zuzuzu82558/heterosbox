"""Provider neutral model calls with one retry/fallback policy and budget ledger."""
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from .models import ActionPayload, AgentResponse, BudgetPolicy, ModelFailurePolicy, SemanticTerminationResult


class ModelExhausted(RuntimeError):
    pass


class BudgetExhausted(RuntimeError):
    pass


@dataclass
class ModelOutput:
    response: AgentResponse = field(default_factory=AgentResponse)
    read_action: ActionPayload | None = None
    write_action: ActionPayload | None = None
    control_action: ActionPayload | None = None
    semantic_result: SemanticTerminationResult | None = None
    tokens: int = 0
    cost_usd: float = 0.0
    request_id: str | None = None
    finish_reason: str = "stop"


class ModelAdapter(Protocol):
    async def call(self, model_id: str, context: dict[str, Any]) -> ModelOutput: ...


@dataclass
class BudgetLedger:
    llm_calls: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0

    def exhausted(self, policy: BudgetPolicy) -> bool:
        return ((policy.max_llm_calls is not None and self.llm_calls >= policy.max_llm_calls)
                or (policy.max_total_tokens is not None and self.total_tokens >= policy.max_total_tokens)
                or (policy.max_cost_usd is not None and self.cost_usd >= policy.max_cost_usd))


class ModelGateway:
    def __init__(self, adapter: ModelAdapter, policy: ModelFailurePolicy, budget_policy: BudgetPolicy):
        self.adapter = adapter
        self.policy = policy
        self.budget_policy = budget_policy
        self.ledger = BudgetLedger()
        self.recordings: list[dict] = []

    async def call_with_failure_policy(self, model_id: str, context: dict) -> ModelOutput:
        last_error = None
        chain = [model_id] * (1 + self.policy.provider_retries) + self.policy.fallback_model_ids
        for candidate in chain:
            if self.ledger.exhausted(self.budget_policy):
                raise BudgetExhausted("model budget exhausted")
            started = time.monotonic()
            self.ledger.llm_calls += 1
            try:
                output = await self.adapter.call(candidate, context)
                self.ledger.total_tokens += output.tokens
                self.ledger.cost_usd += output.cost_usd
                self.recordings.append({"model_id": candidate, "request_id": output.request_id,
                    "normalized_request_hash": hashlib.sha256(json.dumps(context, sort_keys=True, default=str).encode()).hexdigest(),
                    "usage": {"tokens": output.tokens, "cost_usd": output.cost_usd},
                    "latency_s": time.monotonic() - started, "finish_reason": output.finish_reason,
                    "fallback": candidate != model_id})
                return output
            except Exception as exc:
                last_error = exc
                self.recordings.append({"model_id": candidate, "error": type(exc).__name__, "latency_s": time.monotonic() - started})
        raise ModelExhausted(str(last_error)) from last_error


class ScriptedModelAdapter:
    """Deterministic adapter for headless tests and local examples."""
    def __init__(self, outputs: list[ModelOutput]):
        self.outputs = list(outputs)
        self.calls: list[dict] = []

    async def call(self, model_id: str, context: dict) -> ModelOutput:
        self.calls.append({"model_id": model_id, "context": context})
        if not self.outputs:
            raise RuntimeError("script exhausted")
        return self.outputs.pop(0)
