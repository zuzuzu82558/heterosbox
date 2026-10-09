"""Append only branch log and live event fanout."""
import asyncio
import copy
import time
import uuid
from collections import defaultdict

from .models import EventVisibility, SimEvent
from .registry import EventType, EVENT_REGISTRY


class EventStore:
    def __init__(self, session_id: str, branch_id: str = "main", repository=None):
        self.session_id = session_id
        self.branch_id = branch_id
        self.repository = repository
        self.events: list[SimEvent] = []
        self.listeners: set[asyncio.Queue] = set()
        self.lock = asyncio.Lock()

    @property
    def last_seq(self):
        return self.events[-1].event_seq if self.events else 0

    async def append(self, name: str, payload: dict, *, round_index: int = 0, turn_index: int = 0,
                     actor_id: str | None = None, visibility: EventVisibility | None = None) -> SimEvent:
        async with self.lock:
            event = SimEvent(event_id=str(uuid.uuid4()), session_id=self.session_id, branch_id=self.branch_id,
                event_seq=self.last_seq + 1, parent_event_id=self.events[-1].event_id if self.events else None,
                round_index=round_index, turn_index=turn_index, actor_id=actor_id,
                type=EventType(name), payload=copy.deepcopy(payload),
                visibility=visibility or EventVisibility(audience=EVENT_REGISTRY[name].default_visibility), created_at=time.time())
            if self.repository is not None:
                self.repository.save_event(event)
            self.events.append(event)
            for queue in tuple(self.listeners):
                queue.put_nowait(event)
            return event

    async def append_batch(self, drafts: list[SimEvent]) -> list[SimEvent]:
        if not drafts:
            return []
        async with self.lock:
            committed = []
            parent = self.events[-1].event_id if self.events else None
            for draft in drafts:
                event = SimEvent(event_id=str(uuid.uuid4()), session_id=self.session_id, branch_id=self.branch_id,
                    event_seq=self.last_seq + len(committed) + 1, parent_event_id=parent,
                    round_index=draft.round_index, turn_index=draft.turn_index, actor_id=draft.actor_id,
                    type=draft.type, payload=copy.deepcopy(draft.payload), visibility=draft.visibility,
                    created_at=draft.created_at)
                committed.append(event)
                parent = event.event_id
            if self.repository is not None:
                self.repository.save_events_batch(committed)
            self.events.extend(committed)
            for queue in tuple(self.listeners):
                for event in committed:
                    queue.put_nowait(event)
            return committed

    async def subscribe(self, after_seq: int = 0):
        queue = asyncio.Queue()
        async with self.lock:
            backlog = [event for event in self.events if event.event_seq > after_seq]
            self.listeners.add(queue)
        try:
            for event in backlog:
                yield event
            while True:
                yield await queue.get()
        finally:
            self.listeners.discard(queue)


class BufferedEventStore:
    """Turn-local events become visible together at the committed turn boundary."""
    def __init__(self, committed: EventStore):
        self.committed = committed
        self.session_id = committed.session_id
        self.branch_id = committed.branch_id
        self.events = list(committed.events)
        self.staged: list[SimEvent] = []

    @property
    def last_seq(self):
        return self.events[-1].event_seq if self.events else 0

    async def append(self, name: str, payload: dict, *, round_index: int = 0, turn_index: int = 0,
                     actor_id: str | None = None, visibility: EventVisibility | None = None) -> SimEvent:
        event = SimEvent(event_id=str(uuid.uuid4()), session_id=self.session_id, branch_id=self.branch_id,
            event_seq=self.last_seq + 1, parent_event_id=self.events[-1].event_id if self.events else None,
            round_index=round_index, turn_index=turn_index, actor_id=actor_id,
            type=EventType(name), payload=copy.deepcopy(payload),
            visibility=visibility or EventVisibility(audience=EVENT_REGISTRY[name].default_visibility), created_at=time.time())
        self.events.append(event)
        self.staged.append(event)
        return event

    async def commit(self) -> dict[int, int]:
        actual_events = await self.committed.append_batch(self.staged)
        mapping = {draft.event_seq: actual.event_seq for draft, actual in zip(self.staged, actual_events)}
        self.staged.clear()
        return mapping
