"""All authorized views of committed data pass through this service."""
import copy
from typing import Iterable

from .models import EventVisibility, FieldSpec, MessageEntry, Perspective, PrivateAgentState, SceneRuntimeConfig, SessionGrant, SimEvent


class ProjectionDenied(PermissionError):
    pass


class ProjectionService:
    def __init__(self, scene: SceneRuntimeConfig):
        self.scene = scene
        self.fields = {f.path: f for f in scene.world_state_schema.fields}

    def authorize(self, perspective: Perspective, grant: SessionGrant):
        key = "GOD" if perspective.kind == "GOD" else (f"AGENT:{perspective.agent_id}" if perspective.kind == "AGENT" else "OBSERVER")
        if key not in grant.allowed_perspectives or perspective.principal_role != grant.role:
            raise ProjectionDenied("perspective not granted")
        if perspective.kind == "GOD" and grant.role not in ("OWNER", "OPERATOR"):
            raise ProjectionDenied("GOD requires operator role")
        if perspective.kind == "AGENT" and perspective.agent_id not in {a.agent_id for a in self.scene.agents}:
            raise ProjectionDenied("unknown agent")
        if perspective.audit_level != "NONE" and grant.audit_level == "NONE":
            raise ProjectionDenied("audit not granted")

    def field_to_event_visibility(self, field: FieldSpec) -> EventVisibility:
        audience = field.visibility.audience
        if audience == "PUBLIC":
            return EventVisibility(audience="ALL_AGENTS")
        if audience == "OWNER":
            if not field.owner_id:
                raise ProjectionDenied("unresolved owner")
            return EventVisibility(audience="SELF", owner_id=field.owner_id)
        if audience == "SPECIFIC":
            return EventVisibility(audience="SPECIFIC", audience_ids=field.visibility.audience_ids)
        return EventVisibility(audience="OPERATOR_ONLY")

    def _visible(self, visibility: EventVisibility, perspective: Perspective) -> bool:
        if perspective.kind == "GOD":
            return perspective.principal_role in ("OWNER", "OPERATOR") and (visibility.audience != "AUDIT_ONLY" or perspective.audit_level == "FULL")
        if visibility.audience == "ALL_AGENTS":
            return True
        if perspective.kind != "AGENT":
            return False
        if visibility.audience == "SELF":
            return visibility.owner_id == perspective.agent_id
        if visibility.audience == "SPECIFIC":
            return perspective.agent_id in visibility.audience_ids
        return False

    def _reveals(self, events: Iterable[SimEvent], at_seq: int) -> dict[str, EventVisibility]:
        active = {}
        for event in events:
            if event.event_seq > at_seq:
                break
            if event.type.value == "visibility_revealed" and event.payload.get("duration") == "PERMANENT":
                active[event.payload["field_path"]] = EventVisibility.model_validate(event.payload["new_visibility"])
        return active

    def project_world(self, world: dict, perspective: Perspective, grant: SessionGrant, *, events: Iterable[SimEvent] = (), at_seq: int | None = None) -> dict:
        self.authorize(perspective, grant)
        event_list = list(events)
        reveals = self._reveals(event_list, at_seq if at_seq is not None else (event_list[-1].event_seq if event_list else 0))
        result = {}
        for path, field in self.fields.items():
            visibility = reveals.get(path, self.field_to_event_visibility(field))
            if self._visible(visibility, perspective):
                value = world
                try:
                    for part in path.split("."):
                        value = value[part]
                except (KeyError, TypeError):
                    continue
                target = result
                parts = path.split(".")
                for part in parts[:-1]:
                    target = target.setdefault(part, {})
                target[parts[-1]] = copy.deepcopy(value)
        return result

    def project_events(self, events: Iterable[SimEvent], perspective: Perspective, grant: SessionGrant, *, after_seq: int = 0, at_seq: int | None = None) -> list[dict]:
        self.authorize(perspective, grant)
        event_list = list(events)
        query_seq = at_seq if at_seq is not None else (event_list[-1].event_seq if event_list else 0)
        retro_reveals = [(e.payload["field_path"], e.payload["retroactive_from_event_seq"], e.event_seq,
            EventVisibility.model_validate(e.payload["new_visibility"])) for e in event_list
            if e.type.value == "visibility_revealed" and e.event_seq <= query_seq and e.payload.get("retroactive")]
        reveals = {}
        output = []
        for event in event_list:
            if event.event_seq > query_seq:
                break
            if event.type.value == "visibility_revealed":
                # A reveal becomes effective at its own sequence, never before.
                if event.payload.get("duration") == "PERMANENT":
                    reveals[event.payload["field_path"]] = EventVisibility.model_validate(event.payload["new_visibility"])
            if event.event_seq <= after_seq or not self._visible(event.visibility, perspective):
                continue
            data = event.model_dump(mode="json")
            if event.type.value == "world_state_delta":
                data["payload"]["changes"] = {path: value for path, value in event.payload["changes"].items()
                    if path in self.fields and (self._visible(reveals.get(path, self.field_to_event_visibility(self.fields[path])), perspective)
                    or any(reveal_path == path and first <= event.event_seq < reveal_seq and self._visible(visibility, perspective)
                        for reveal_path, first, reveal_seq, visibility in retro_reveals))}
                data["payload"]["before"] = {path: value for path, value in event.payload.get("before", {}).items()
                    if path in data["payload"]["changes"]}
                if not data["payload"]["changes"]:
                    continue
            output.append(data)
        return output

    def project_private(self, private_states: dict[str, PrivateAgentState], perspective: Perspective, grant: SessionGrant) -> dict:
        self.authorize(perspective, grant)
        if perspective.kind == "GOD":
            return {key: value.model_dump() for key, value in private_states.items()}
        if perspective.kind == "AGENT" and perspective.agent_id in private_states:
            return {perspective.agent_id: private_states[perspective.agent_id].model_dump()}
        return {}

    def build_agent_prompt_view(self, agent_id: str, world: dict, private_states: dict[str, PrivateAgentState], messages: list[MessageEntry], events: list[SimEvent]) -> dict:
        profile = next(a for a in self.scene.agents if a.agent_id == agent_id)
        perspective = Perspective(kind="AGENT", agent_id=agent_id, principal_role="PARTICIPANT")
        grant = SessionGrant(session_id="internal", principal_id=agent_id, role="PARTICIPANT", allowed_perspectives=[f"AGENT:{agent_id}"], audit_level="NONE")
        return {
            "identity": {"agent_id": profile.agent_id, "display_name": profile.display_name,
                         "public_role": profile.public_role, "persona": profile.persona,
                         "personal_goal": profile.personal_goal,
                         "shared_goals": profile.shared_goals or self.scene.shared_goals},
            "public_story": self.scene.public_story,
            "grounding_rules": self.scene.grounding_rules,
            "world": self.project_world(world, perspective, grant, events=events),
            "private": self.project_private(private_states, perspective, grant).get(agent_id, {}),
            "messages": [m.model_dump() for m in messages if m.channel == "PUBLIC" or (m.channel == "WHISPER" and (m.sender_id == agent_id or agent_id in m.recipient_ids))],
            "events": self.project_events(events, perspective, grant),
        }
