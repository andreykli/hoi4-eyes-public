"""Versioned, atomic SQLite storage for Flask group-chat state."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path


class ChatStateStore:
    SCHEMA_VERSION = 1

    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        if not self.path.exists():
            return None
        with closing(sqlite3.connect(self.path)) as connection:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chat_state'"
            ).fetchone()
            if table is None:
                return None
            row = connection.execute(
                "SELECT schema_version, payload FROM chat_state WHERE singleton = 1"
            ).fetchone()
        if row is None:
            return None
        version, payload = row
        if version != self.SCHEMA_VERSION:
            raise RuntimeError(f"unsupported chat state schema version: {version}")
        try:
            state = json.loads(payload)
        except (TypeError, json.JSONDecodeError) as error:
            raise RuntimeError("stored chat state is invalid JSON") from error
        if not isinstance(state, dict):
            raise RuntimeError("stored chat state must be a JSON object")
        return state

    def save(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("""CREATE TABLE IF NOT EXISTS chat_state (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version INTEGER NOT NULL,
                    payload TEXT NOT NULL
                )""")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO chat_state(singleton, schema_version, payload) VALUES(1, ?, ?) "
                    "ON CONFLICT(singleton) DO UPDATE SET schema_version=excluded.schema_version, "
                    "payload=excluded.payload",
                    (self.SCHEMA_VERSION, payload),
                )

    def archive_transcript(self, conversation_id, payload, provenance):
        """Store a legacy transcript separately so normal engine saves cannot erase it."""
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            raise ValueError("conversation_id must be non-empty")
        if not isinstance(payload, dict) or not isinstance(provenance, str) or not provenance.strip():
            raise ValueError("archive payload and provenance are required")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute("""CREATE TABLE IF NOT EXISTS archived_transcripts (
                    conversation_id TEXT PRIMARY KEY,
                    provenance TEXT NOT NULL,
                    payload TEXT NOT NULL
                )""")
                existing = connection.execute(
                    "SELECT provenance, payload FROM archived_transcripts WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()
                if existing is not None:
                    if existing == (provenance, serialized):
                        return False
                    raise RuntimeError("archived transcript already exists with different contents")
                connection.execute(
                    "INSERT INTO archived_transcripts(conversation_id, provenance, payload) VALUES(?, ?, ?)",
                    (conversation_id, provenance, serialized),
                )
        return True

    def load_archived_transcript(self, conversation_id):
        if not self.path.exists():
            return None
        with closing(sqlite3.connect(self.path)) as connection:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='archived_transcripts'"
            ).fetchone()
            if table is None:
                return None
            row = connection.execute(
                "SELECT provenance, payload FROM archived_transcripts WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        if row is None:
            return None
        provenance, serialized = row
        try:
            payload = json.loads(serialized)
        except json.JSONDecodeError as error:
            raise RuntimeError("archived transcript is invalid JSON") from error
        return {"conversation_id": conversation_id, "provenance": provenance, "payload": payload}

