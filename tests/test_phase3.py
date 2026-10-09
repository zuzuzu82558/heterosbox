import time
import uuid
from heterosbox.models import EventVisibility, Perspective, PrivateAgentState, SessionGrant, SimEvent
from heterosbox.projection import ProjectionService


def _view(agent):
    return Perspective(kind="AGENT", agent_id=agent, principal_role="PARTICIPANT"), SessionGrant(session_id="x", principal_id=agent, role="PARTICIPANT", allowed_perspectives=[f"AGENT:{agent}"], audit_level="NONE")


def test_canary_isolation(scene):
    projection = ProjectionService(scene)
    a, ag = _view("a")
    b, bg = _view("b")
    assert projection.project_world(scene.initial_world_state, a, ag)["secret"] == "CANARY"
    assert "CANARY" not in str(projection.build_agent_prompt_view("b", scene.initial_world_state, {"a": PrivateAgentState(beliefs={"x": "CANARY"}), "b": PrivateAgentState()}, [], []))
    assert "secret" not in projection.project_world(scene.initial_world_state, b, bg)


def test_delta_filtered_and_reveal_not_retroactive(scene):
    p = ProjectionService(scene)
    b, grant = _view("b")
    def event(seq, kind, payload, audience="ALL_AGENTS"):
        return SimEvent(event_id=str(uuid.uuid4()), session_id="x", branch_id="main", event_seq=seq, parent_event_id=None, round_index=0, turn_index=0, actor_id=None, type=kind, payload=payload, visibility=EventVisibility(audience=audience), created_at=time.time())
    events = [
        event(1, "world_state_delta", {"changes": {"gold": 10, "secret": "CANARY"}}),
        event(2, "visibility_revealed", {"field_path": "secret", "new_visibility": {"audience": "ALL_AGENTS"}, "duration": "PERMANENT", "retroactive": False}),
        event(3, "world_state_delta", {"changes": {"secret": "REVEALED"}}),
    ]
    seen = p.project_events(events, b, grant)
    assert seen[0]["payload"]["changes"] == {"gold": 10}
    assert seen[-1]["payload"]["changes"] == {"secret": "REVEALED"}
    assert "CANARY" not in str(seen)


def test_explicit_retroactive_reveal_only_after_reveal_event(scene):
    p = ProjectionService(scene)
    b, grant = _view("b")
    old = SimEvent(event_id="old", session_id="x", branch_id="main", event_seq=1, parent_event_id=None,
        round_index=0, turn_index=0, actor_id=None, type="world_state_delta", payload={"changes": {"secret": "CANARY"}},
        visibility=EventVisibility(audience="ALL_AGENTS"), created_at=time.time())
    reveal = SimEvent(event_id="reveal", session_id="x", branch_id="main", event_seq=2, parent_event_id="old",
        round_index=0, turn_index=0, actor_id=None, type="visibility_revealed",
        payload={"field_path": "secret", "new_visibility": {"audience": "ALL_AGENTS"},
                 "duration": "PERMANENT", "retroactive": True, "retroactive_from_event_seq": 1},
        visibility=EventVisibility(audience="ALL_AGENTS"), created_at=time.time())
    assert p.project_events([old, reveal], b, grant, at_seq=1) == []
    assert p.project_events([old, reveal], b, grant, at_seq=2)[0]["payload"]["changes"]["secret"] == "CANARY"
