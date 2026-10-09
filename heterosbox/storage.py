"""SQLite backed immutable event and checkpoint storage."""
import json
import sqlite3
from pathlib import Path

from .models import Checkpoint, SceneRuntimeConfig, SimEvent


class SqliteRepository:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT NOT NULL, branch_id TEXT NOT NULL, scene_json TEXT NOT NULL,
                PRIMARY KEY (session_id, branch_id));
            CREATE TABLE IF NOT EXISTS events (
                session_id TEXT NOT NULL, branch_id TEXT NOT NULL, event_seq INTEGER NOT NULL,
                event_json TEXT NOT NULL, PRIMARY KEY (session_id, branch_id, event_seq));
            CREATE TABLE IF NOT EXISTS checkpoints (
                checkpoint_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, branch_id TEXT NOT NULL,
                event_seq INTEGER NOT NULL, checkpoint_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS visual_settings (
                scene_id TEXT PRIMARY KEY, settings_json TEXT NOT NULL);
        """)
        self.connection.commit()

    def save_scene(self, session_id: str, branch_id: str, scene: SceneRuntimeConfig):
        self.connection.execute("INSERT OR IGNORE INTO sessions VALUES (?, ?, ?)",
            (session_id, branch_id, scene.model_dump_json()))
        self.connection.commit()

    def save_visual_settings(self, scene_id: str, settings: dict):
        self.connection.execute("INSERT INTO visual_settings (scene_id, settings_json) VALUES (?, ?) "
            "ON CONFLICT(scene_id) DO UPDATE SET settings_json=excluded.settings_json",
            (scene_id, json.dumps(settings, ensure_ascii=False)))
        self.connection.commit()

    def load_visual_settings(self, scene_id: str) -> dict | None:
        row = self.connection.execute("SELECT settings_json FROM visual_settings WHERE scene_id=?", (scene_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_event(self, event: SimEvent):
        self.save_events_batch([event])

    def save_events_batch(self, events: list[SimEvent]):
        with self.connection:
            self.connection.executemany("INSERT INTO events VALUES (?, ?, ?, ?)",
                [(event.session_id, event.branch_id, event.event_seq, event.model_dump_json()) for event in events])

    def save_checkpoint(self, checkpoint: Checkpoint):
        self.connection.execute("INSERT INTO checkpoints VALUES (?, ?, ?, ?, ?)",
            (checkpoint.checkpoint_id, checkpoint.session_id, checkpoint.branch_id,
             checkpoint.event_seq, checkpoint.model_dump_json()))
        self.connection.commit()

    def list_sessions(self):
        return [(sid, bid, SceneRuntimeConfig.model_validate_json(scene))
            for sid, bid, scene in self.connection.execute("SELECT session_id, branch_id, scene_json FROM sessions")]

    def load_events(self, session_id: str, branch_id: str) -> list[SimEvent]:
        rows = self.connection.execute("SELECT event_json FROM events WHERE session_id=? AND branch_id=? ORDER BY event_seq",
            (session_id, branch_id))
        return [SimEvent.model_validate_json(row[0]) for row in rows]

    def load_checkpoints(self, session_id: str, branch_id: str) -> list[Checkpoint]:
        rows = self.connection.execute("SELECT checkpoint_json FROM checkpoints WHERE session_id=? AND branch_id=? ORDER BY event_seq, rowid",
            (session_id, branch_id))
        return [Checkpoint.model_validate_json(row[0]) for row in rows]
