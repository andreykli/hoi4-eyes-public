"""Bridge-side campaign IDs for fresh games whose Clausewitz RNG seeds collide.

The four native HOI4 seed variables remain the stable fallback for old saves.
When the mod marks the first selection in a fresh game, this small sidecar
assigns a distinct UUID and remembers it for later selection markers and Bridge
restarts. It is deliberately separate from the diplomatic SQLite database.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading
import uuid

from hoi4_campaign_identity import campaign_id_from_save_token


class CampaignIdentityRegistry:
    SCHEMA_VERSION = 1

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._entries = self._load()

    def _load(self):
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RuntimeError(f"cannot load campaign identity registry: {self.path}") from error
        if (not isinstance(data, dict) or data.get("schema_version") != self.SCHEMA_VERSION
                or not isinstance(data.get("campaigns"), list)):
            raise RuntimeError("campaign identity registry has an invalid schema; preserved for review")
        entries = []
        for row in data["campaigns"]:
            if (not isinstance(row, dict)
                    or not isinstance(row.get("source_campaign_token"), str)
                    or not isinstance(row.get("session_key"), str)
                    or not isinstance(row.get("campaign_id"), str)
                    or not re.fullmatch(r"[0-9a-f]{32}", row["campaign_id"])):
                raise RuntimeError("campaign identity registry has an invalid row; preserved for review")
            # Validate native tokens before retaining an entry.
            try:
                campaign_id_from_save_token(row["source_campaign_token"])
            except ValueError as error:
                raise RuntimeError("campaign identity registry has an invalid seed token") from error
            entries.append(dict(row))
        return entries

    def _persist(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump({"schema_version": self.SCHEMA_VERSION, "campaigns": self._entries},
                      stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    def register_fresh_campaign(self, token: str, session_key: str) -> str:
        """Return an idempotent unique ID for one fresh-game marker instance."""
        try:
            campaign_id_from_save_token(token)
        except ValueError:
            raise
        if not isinstance(session_key, str) or not session_key or len(session_key) > 256:
            raise ValueError("fresh campaign marker requires a bounded session key")
        with self._lock:
            for row in reversed(self._entries):
                if row["source_campaign_token"] == token and row["session_key"] == session_key:
                    return row["campaign_id"]
            campaign_id = uuid.uuid4().hex
            self._entries.append({
                "source_campaign_token": token,
                "session_key": session_key,
                "campaign_id": campaign_id,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
            self._persist()
            return campaign_id

    def resolve_saved_campaign(self, token: str) -> str:
        """Resume the latest Bridge-assigned ID, or the legacy deterministic ID."""
        try:
            legacy_id = campaign_id_from_save_token(token)
        except ValueError:
            raise
        with self._lock:
            for row in reversed(self._entries):
                if row["source_campaign_token"] == token:
                    return row["campaign_id"]
        return legacy_id

