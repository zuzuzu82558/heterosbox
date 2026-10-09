"""The sole event type registry. EventType is derived at import time."""
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, create_model


class RegistryEntry(BaseModel):
    payload_schema: dict[str, Any] = Field(default_factory=dict)
    default_visibility: str = "OPERATOR_ONLY"
    causal_parent_required: bool = False


_NAMES = """round_started turn_started agent_status_changed
speech_pending speech_discarded speech_committed
action_proposed permission_denied arbiter_accepted arbiter_rejected retry_attempted rule_evaluation_error
world_state_delta entity_state_changed round_settlement_committed visibility_revealed
private_observation_added private_memory_compressed
operator_event_applied takeover_started takeover_ended takeover_timeout
command_accepted command_rejected command_conflict
checkpoint_created branch_created leaderboard_updated semantic_check early_trigger semantic_termination_decided
judging_policy_updated tool_permission_changed budget_warning budget_exhausted budget_updated
model_error model_call_cancelled fallback_model_used
simulation_pausing simulation_paused simulation_resumed simulation_stopping simulation_error simulation_terminated""".split()

assert len(_NAMES) == len(set(_NAMES))
EVENT_REGISTRY: dict[str, RegistryEntry] = {name: RegistryEntry() for name in _NAMES}
EVENT_REGISTRY["speech_committed"] = RegistryEntry(payload_schema={"message": "object"}, default_visibility="ALL_AGENTS")
EVENT_REGISTRY["world_state_delta"] = RegistryEntry(payload_schema={"changes": "object"})
EVENT_REGISTRY["visibility_revealed"] = RegistryEntry(payload_schema={"field_path": "str", "new_visibility": "object", "duration": "str", "retroactive": "bool"})
EventType = Enum("EventType", {name: name for name in EVENT_REGISTRY}, type=str)


def validate_event_type(value: str) -> EventType:
    return EventType(value)


def validate_payload(name: EventType, payload: dict[str, Any]) -> None:
    for key, kind in EVENT_REGISTRY[name.value].payload_schema.items():
        if key not in payload:
            raise ValueError(f"{name.value} missing payload key {key}")
        expected = {"str": str, "bool": bool, "object": dict}[kind]
        if not isinstance(payload[key], expected):
            raise ValueError(f"{name.value}.{key} must be {kind}")

