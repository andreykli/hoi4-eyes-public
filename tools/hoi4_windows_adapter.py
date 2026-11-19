"""Fail-closed queue for manually confirmed HOI4 1.19.x effects.

HOI4's documented ``eval_effect`` command has no documented console-open
accessibility signal or complete command grammar in the bundled reference.
Consequently this module deliberately does not inject keyboard input. It
validates and journals already-approved, generated event packages, supports a
dry-run, and reconciles their exact game-log result markers. The compatibility
range is 1.19.*. Adapter actions have not all been live-tested; the separate
territory-transfer scenario was verified in 1.19.3, and the current 1.19.4
hotfix received static script/GUI checks only.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import re
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from guarded_treaty_effects import EFFECTS as GUARDED_EFFECTS, NEW_ACTIONS, RELATIONS, relation_scope

try:
    from .validate_hoi4_gui import parse_pdx
except ImportError:  # direct script execution
    from validate_hoi4_gui import parse_pdx


SCHEMA_VERSION = 1
HOI4_VERSION = "1.19.3"
HOI4_SUPPORTED_VERSION_RANGE = "1.19.*"
AUTOMATIC_ENV = "EYES_ENABLE_HOI4_CONSOLE_ADAPTER"
COUNTRY_TAG = re.compile(r"^[A-Z0-9]{3}$")
CAMPAIGN_ID = re.compile(r"^[0-9a-f]{32}$")
ALLOWED_EFFECTS = {
    "non_aggression_pact": "diplomatic_relation",
    "guarantee": "give_guarantee",
    "embargo": "send_embargo",
    "lift_embargo": "break_embargo",
    "military_access": "give_military_access",
    "political_annexation": "annex_country",
    "white_peace": "white_peace",
    "faction_join": "add_to_faction",
    "faction_leave": "remove_from_faction",
    **{action: GUARDED_EFFECTS[action][0] for action in NEW_ACTIONS},
}
TERMINAL_STATES = {"state_verified", "error"}
ALLOWED_STATES = {"prepared", "sent", "marker_confirmed", "state_verified", "error"}
DANGEROUS_EFFECTS = {
    "annex_country", "declare_war_on", "create_wargoal", "transfer_state",
    "set_state_owner", "set_state_controller", "add_state_core", "remove_state_core",
    "add_to_faction", "leave_faction", "puppet", "release", "white_peace",
    "force_government", "start_civil_war", "delete_country", "set_capital",
}


class AdapterError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def validate_package(campaign_id: str, request_id: str, package: Mapping[str, Any], *,
                     player_approved: bool) -> dict[str, Any]:
    """Accept only a generated V5 package and an explicit player approval."""
    if not isinstance(campaign_id, str) or not CAMPAIGN_ID.fullmatch(campaign_id):
        raise AdapterError("invalid_campaign_id")
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 512:
        raise AdapterError("invalid_request_id")
    if not isinstance(package, Mapping):
        raise AdapterError("invalid_execution_package")
    if player_approved is not True:
        raise AdapterError("player_approval_required")
    action_id = package.get("action_id")
    if action_id not in ALLOWED_EFFECTS:
        raise AdapterError("effect_not_allowlisted")
    if package.get("status") != "manual_required" or package.get("manual_confirmation_required") is not True:
        raise AdapterError("package_not_manual_confirmed_v5")
    if package.get("execution_request_id") != request_id:
        raise AdapterError("request_id_mismatch")

    game_effect = package.get("game_effect")
    package_effect_id = (
        game_effect.get("effect") if isinstance(game_effect, Mapping)
        and action_id == "political_annexation" else
        game_effect.get("action_id") if isinstance(game_effect, Mapping) else None
    )
    expected_package_effect_id = (
        "annex_country" if action_id == "political_annexation" else action_id
    )
    if not isinstance(game_effect, Mapping) or package_effect_id != expected_package_effect_id:
        raise AdapterError("game_effect_mismatch")
    required_tags = {
        "non_aggression_pact": ("participants",),
        "guarantee": ("guarantor", "beneficiary"),
        "embargo": ("actor", "target"),
        "lift_embargo": ("actor", "target"),
        "military_access": ("grantor", "recipient"),
        "political_annexation": ("initiator_tag", "target_tag"),
        **dict.fromkeys(GUARDED_EFFECTS, ("actor", "target")),
    }[action_id]
    for field in required_tags:
        value = game_effect.get(field)
        values = value if field == "participants" else [value]
        if (not isinstance(values, list) or len(values) != (2 if field == "participants" else 1)
                or any(not isinstance(tag, str) or not COUNTRY_TAG.fullmatch(tag) for tag in values)):
            raise AdapterError("game_effect_parameters_invalid")
    tag_fields = (game_effect.get("participants") if action_id == "non_aggression_pact"
                  else [game_effect.get(field) for field in required_tags])
    if len(set(tag_fields)) != len(tag_fields):
        raise AdapterError("game_effect_participants_not_distinct")
    event_id = package.get("event_id")
    command = package.get("open_event_command")
    if (not isinstance(event_id, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]*\.1", event_id)):
        raise AdapterError("invalid_generated_event_id")
    if not isinstance(command, str) or not re.fullmatch(
            rf"event {re.escape(event_id)} [A-Z0-9]{{3}}", command):
        raise AdapterError("unsafe_manual_event_command")

    result_marker = package.get("result_marker")
    if not isinstance(result_marker, str):
        raise AdapterError("missing_result_marker")
    fields = result_marker.split("|")
    if (len(fields) != 5 or fields[0] != "EYES_HOI4_ACTION_APPLIED_V2"
            or fields[3] != request_id or fields[4] != action_id
            or not COUNTRY_TAG.fullmatch(fields[1]) or not COUNTRY_TAG.fullmatch(fields[2])):
        raise AdapterError("result_marker_mismatch")
    failure_marker = package.get("failure_marker")
    failure_fields = failure_marker.split("|") if isinstance(failure_marker, str) else []
    if (len(failure_fields) != 6 or failure_fields[0] != "EYES_HOI4_ACTION_FAILED_V2"
            or failure_fields[1:5] != fields[1:5]
            or not re.fullmatch(r"[a-z0-9_]{1,80}", failure_fields[5])):
        raise AdapterError("failure_marker_mismatch")
    command_tag = command.rsplit(" ", 1)[-1]
    if command_tag != fields[1]:
        raise AdapterError("event_scope_does_not_match_marker")

    event_file = package.get("event_file")
    if not isinstance(event_file, str) or not event_file.strip():
        raise AdapterError("missing_generated_event_script")
    try:
        tree = parse_pdx(event_file)
    except (ValueError, RecursionError) as exc:
        raise AdapterError("event_script_syntax_invalid") from exc
    required_effect = ALLOWED_EFFECTS[action_id]
    def keys(block):
        for key, value in block.entries:
            yield key
            if hasattr(value, 'entries'): yield from keys(value)
    script_keys = set(keys(tree))
    if required_effect not in script_keys:
        raise AdapterError("expected_effect_missing_from_event")
    other_allowlisted = set(ALLOWED_EFFECTS.values()) - {required_effect}
    forbidden = (DANGEROUS_EFFECTS | other_allowlisted) - {required_effect}
    if script_keys & forbidden:
        raise AdapterError("event_contains_unapproved_effect")
    if action_id == "non_aggression_pact":
        if set(game_effect["participants"]) != {fields[1], fields[2]}:
            raise AdapterError("effect_participants_do_not_match_marker")
        expected_parameters = (
            rf"diplomatic_relation\s*=\s*\{{\s*country\s*=\s*{fields[2]}\s+"
            r"relation\s*=\s*non_aggression_pact\s+active\s*=\s*yes\s*\}"
        )
    elif action_id == "guarantee":
        if {game_effect["beneficiary"], game_effect["guarantor"]} != {fields[1], fields[2]}:
            raise AdapterError("effect_participants_do_not_match_marker")
        expected_parameters = (
            rf"{game_effect['guarantor']}\s*=\s*\{{\s*give_guarantee\s*=\s*{game_effect['beneficiary']}\s*\}}"
        )
    elif action_id in {"embargo", "lift_embargo"}:
        if game_effect["actor"] not in {fields[1], fields[2]}:
            raise AdapterError("effect_participants_do_not_match_marker")
        expected_parameters = (
            rf"{game_effect['actor']}\s*=\s*\{{\s*{required_effect}\s*=\s*"
            rf"{game_effect['target']}\s*\}}"
        )
    elif action_id == "military_access":
        if {game_effect["grantor"], game_effect["recipient"]} != {fields[1], fields[2]}:
            raise AdapterError("effect_participants_do_not_match_marker")
        expected_parameters = (
            rf"{game_effect['grantor']}\s*=\s*\{{\s*give_military_access\s*=\s*"
            rf"{game_effect['recipient']}\s*\}}"
        )
    elif action_id in GUARDED_EFFECTS:
        if list(keys(tree)).count(required_effect) != 1:
            raise AdapterError("effect_must_be_unique")
        if tag_fields != fields[1:3]:
            raise AdapterError("effect_participants_do_not_match_marker")
        actor, target = tag_fields
        if action_id in RELATIONS:
            relation = RELATIONS[action_id][0]
            actor, target = relation_scope(action_id, actor, target)
            value = (rf"\{{\s*country\s*=\s*{target}\s+relation\s*=\s*{relation}"
                     r"\s+active\s*=\s*no\s*\}")
        elif action_id == "white_peace":
            value = rf"\{{\s*tag\s*=\s*{target}\s*\}}"
        elif action_id == "faction_create":
            value = re.escape(package["event_id"].rsplit('.', 1)[0] + '_faction')
        else:
            value = "yes" if action_id == "faction_dismantle" else target
        expected_parameters = rf"{actor}\s*=\s*\{{\s*{required_effect}\s*=\s*{value}\s*\}}"
    else:  # political_annexation is irreversible and requires its explicit approval above.
        if (game_effect["initiator_tag"] != fields[1]
                or game_effect["target_tag"] != fields[2]):
            raise AdapterError("effect_participants_do_not_match_marker")
        expected_parameters = rf"annex_country\s*=\s*{game_effect['target_tag']}"
    if not re.search(expected_parameters, event_file):
        raise AdapterError("effect_parameters_do_not_match_package")
    if result_marker not in event_file:
        raise AdapterError("result_marker_missing_from_event")

    digest = hashlib.sha256(
        f"{campaign_id}|{request_id}|{action_id}".encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "hoi4_version": HOI4_VERSION,
        "hoi4_supported_version_range": HOI4_SUPPORTED_VERSION_RANGE,
        "campaign_id": campaign_id,
        "request_id": request_id,
        "action_id": action_id,
        "action_instance_id": digest,
        "state": "prepared",
        "state_history": ["prepared"],
        "created_at": _now(),
        "updated_at": _now(),
        "event_id": event_id,
        "manual_event_command": command,
        "result_marker": result_marker,
        "failure_marker": failure_marker,
        "game_effect": dict(game_effect),
        "package_digest": hashlib.sha256(event_file.encode("utf-8")).hexdigest(),
        "approval_recorded": True,
        "automatic_send": False,
        "verification": None,
        "error": None,
    }


class WindowsEffectAdapter:
    """Durable effect ledger; automatic console input is intentionally blocked."""

    def __init__(self, queue_path: Path, *, automatic_enabled: bool | None = None,
                 window_state_provider=None):
        self.queue_path = Path(queue_path)
        self.automatic_enabled = (
            os.environ.get(AUTOMATIC_ENV, "0").strip().casefold() in {"1", "true", "yes"}
            if automatic_enabled is None else automatic_enabled is True
        )
        self.window_state_provider = window_state_provider
        self._lock = threading.RLock()
        self._items: dict[str, dict[str, Any]] = {}
        self._load()

    @staticmethod
    def _key(campaign_id: str, request_id: str, action_id: str) -> str:
        return hashlib.sha256(f"{campaign_id}|{request_id}|{action_id}".encode()).hexdigest()

    @contextmanager
    def _cross_process_lock(self):
        """Serialize Bridge and CLI queue updates on the same Windows host."""
        lock_path = self.queue_path.with_name(self.queue_path.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                deadline = time.monotonic() + 10.0
                while True:
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise AdapterError("queue_lock_timeout")
                        time.sleep(0.05)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read_items(self) -> dict[str, dict[str, Any]]:
        if not self.queue_path.exists():
            return {}
        try:
            payload = json.loads(self.queue_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AdapterError("queue_unreadable_preserved") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION or not isinstance(payload.get("items"), list):
            raise AdapterError("queue_schema_invalid_preserved")
        items = {}
        for item in payload["items"]:
            if (not isinstance(item, dict) or item.get("state") not in ALLOWED_STATES
                    or not CAMPAIGN_ID.fullmatch(str(item.get("campaign_id", "")))
                    or item.get("action_id") not in ALLOWED_EFFECTS
                    or not isinstance(item.get("request_id"), str)
                    or not item["request_id"]
                    or len(item["request_id"]) > 512):
                raise AdapterError("queue_item_invalid_preserved")
            key = self._key(item["campaign_id"], item["request_id"], item["action_id"])
            if key in items:
                raise AdapterError("queue_duplicate_key_preserved")
            items[key] = item
        return items

    def _load(self) -> None:
        self._items = self._read_items()

    def _refresh_under_lock(self) -> None:
        self._items = self._read_items()

    def _save(self) -> None:
        _atomic_json(self.queue_path, {
            "schema_version": SCHEMA_VERSION,
            "hoi4_version": HOI4_VERSION,
            "hoi4_supported_version_range": HOI4_SUPPORTED_VERSION_RANGE,
            "items": list(self._items.values()),
        })

    def prepare(self, campaign_id: str, request_id: str, package: Mapping[str, Any], *,
                player_approved: bool, dry_run: bool = False) -> dict[str, Any]:
        entry = validate_package(campaign_id, request_id, package, player_approved=player_approved)
        key = self._key(campaign_id, request_id, entry["action_id"])
        if dry_run:
            return {**entry, "dry_run": True, "persisted": False,
                    "note": "No game input is sent; console syntax/focus are not runtime-verified."}
        with self._lock:
            with self._cross_process_lock():
                self._refresh_under_lock()
                existing = self._items.get(key)
                if existing:
                    if existing["package_digest"] != entry["package_digest"]:
                        raise AdapterError("duplicate_id_payload_conflict")
                    return dict(existing)
                self._items[key] = entry
                self._save()
                return dict(entry)

    def prepare_batch(self, campaign_id: str, requests: list[dict[str, Any]], *,
                      player_approved: bool, dry_run: bool = False) -> list[dict[str, Any]]:
        """Validate the whole confirmed bundle before atomically journaling it.

        This prepares existing allowlisted event packages, never sends console
        input and never treats preparation as an in-game result.
        """
        if not isinstance(requests, list) or not 1 <= len(requests) <= 32:
            raise AdapterError('invalid_action_batch')
        entries = {}
        event_owners = {}
        for row in requests:
            if not isinstance(row, dict) or set(row) != {'request_id', 'package'}:
                raise AdapterError('invalid_action_batch_item')
            item = validate_package(campaign_id, row['request_id'], row['package'], player_approved=player_approved)
            key = self._key(campaign_id, item['request_id'], item['action_id'])
            if item['event_id'] in event_owners and event_owners[item['event_id']] != key:
                raise AdapterError('duplicate_event_identity_in_batch')
            event_owners[item['event_id']] = key
            if key in entries and entries[key]['package_digest'] != item['package_digest']:
                raise AdapterError('duplicate_id_payload_conflict')
            entries[key] = item
        if dry_run:
            return [{**item, 'dry_run': True, 'persisted': False} for item in entries.values()]
        with self._lock:
            with self._cross_process_lock():
                self._refresh_under_lock()
                for key, item in entries.items():
                    existing = self._items.get(key)
                    if existing and existing['package_digest'] != item['package_digest']:
                        raise AdapterError('duplicate_id_payload_conflict')
                    if any(k != key and prior['event_id'] == item['event_id'] for k, prior in self._items.items()):
                        raise AdapterError('event_identity_already_reserved')
                previous = self._items
                self._items = {**previous, **{k: v for k, v in entries.items() if k not in previous}}
                try:
                    self._save()
                except Exception:
                    self._items = previous
                    raise
                return [dict(self._items[key]) for key in entries]

    def attempt_automatic_send(self, campaign_id: str, request_id: str, action_id: str) -> dict[str, Any]:
        """Fail closed: foreground-window detection cannot prove the console is open."""
        del campaign_id, request_id, action_id
        state = self.window_state_provider() if callable(self.window_state_provider) else None
        if not self.automatic_enabled:
            return {"sent": False, "blocked": True, "reason": "adapter_disabled_by_default",
                    "window_state": state}
        if not isinstance(state, dict) or state.get("game_foreground") is not True or state.get("minimized") is True:
            return {"sent": False, "blocked": True, "reason": "hoi4_not_verified_foreground",
                    "window_state": state}
        return {"sent": False, "blocked": True, "reason": "console_visibility_unverifiable",
                "window_state": state}

    def mark_manually_sent(self, campaign_id: str, request_id: str, action_id: str, *,
                           user_confirmed_submission: bool) -> dict[str, Any]:
        if user_confirmed_submission is not True:
            raise AdapterError("manual_submission_confirmation_required")
        key = self._key(campaign_id, request_id, action_id)
        with self._lock:
            with self._cross_process_lock():
                self._refresh_under_lock()
                item = self._items.get(key)
                if item is None:
                    raise AdapterError("prepared_action_not_found")
                if item["state"] != "prepared":
                    raise AdapterError("action_not_resendable")
                item.update(state="sent", updated_at=_now(), dispatch="manual_user_confirmed")
                item.setdefault("state_history", ["prepared"]).append("sent")
                self._save()
                return dict(item)

    def record_marker(self, campaign_id: str, request_id: str, action_id: str, marker: str) -> dict[str, Any]:
        key = self._key(campaign_id, request_id, action_id)
        with self._lock:
            with self._cross_process_lock():
                self._refresh_under_lock()
                item = self._items.get(key)
                if item is None:
                    raise AdapterError("marker_has_no_prepared_action")
                if item["state"] == "marker_confirmed" or item["state"] == "state_verified":
                    if item.get("confirmed_marker") == marker:
                        return dict(item)
                    raise AdapterError("conflicting_duplicate_marker")
                if item["state"] not in {"prepared", "sent"}:
                    raise AdapterError("marker_before_dispatch")
                history = item.setdefault("state_history", ["prepared"])
                if item["state"] == "prepared":
                    # A valid exact in-game marker is itself proof that the event
                    # was dispatched; this closes the race with a human receipt.
                    item["state"] = "sent"
                    item["dispatch"] = "in_game_marker_observed"
                    history.append("sent")
                if marker == item.get("failure_marker"):
                    item.update(state="error", updated_at=_now(), error="game_effect_failed_marker")
                    history.append("error")
                elif marker != item["result_marker"]:
                    if history[-1] == "sent" and item.get("dispatch") == "in_game_marker_observed":
                        history.pop()
                        item["state"] = "prepared"
                        item.pop("dispatch", None)
                    raise AdapterError("marker_mismatch")
                else:
                    item.update(state="marker_confirmed", updated_at=_now(), confirmed_marker=marker)
                    history.append("marker_confirmed")
                self._save()
                return dict(item)

    def verify_game_state(self, campaign_id: str, request_id: str, action_id: str, *,
                          evidence: Mapping[str, Any]) -> dict[str, Any]:
        key = self._key(campaign_id, request_id, action_id)
        with self._lock:
            with self._cross_process_lock():
                self._refresh_under_lock()
                item = self._items.get(key)
                if item is None or item["state"] != "marker_confirmed":
                    raise AdapterError("marker_confirmation_required")
                expected = {"campaign_id": campaign_id, "request_id": request_id,
                            "action_id": action_id, "observed": True}
                if not isinstance(evidence, Mapping) or any(evidence.get(k) != v for k, v in expected.items()):
                    raise AdapterError("game_state_evidence_mismatch")
                if evidence.get("source") not in {"hoi4_state_snapshot", "manual_in_game_inspection"}:
                    raise AdapterError("unsupported_game_state_evidence")
                item.update(state="state_verified", updated_at=_now(), verification=dict(evidence))
                self._save()
                return dict(item)

    def mark_error(self, campaign_id: str, request_id: str, action_id: str, reason: str) -> dict[str, Any]:
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 240:
            raise AdapterError("invalid_error_reason")
        key = self._key(campaign_id, request_id, action_id)
        with self._lock:
            with self._cross_process_lock():
                self._refresh_under_lock()
                item = self._items.get(key)
                if item is None:
                    raise AdapterError("prepared_action_not_found")
                if item["state"] in TERMINAL_STATES:
                    raise AdapterError("action_terminal")
                item.update(state="error", updated_at=_now(), error=reason.strip())
                item.setdefault("state_history", ["prepared"]).append("error")
                self._save()
                return dict(item)

    def status(self) -> list[dict[str, Any]]:
        with self._lock:
            with self._cross_process_lock():
                self._refresh_under_lock()
                return [dict(item) for item in self._items.values()]

    def agreement_news_ready(self, campaign_id: str, request_ids: list[str]) -> bool:
        """News gate: every listed action must be marker- and state-verified."""
        if not request_ids:
            return False
        by_request = {}
        for item in self.status():
            if item['campaign_id'] == campaign_id:
                by_request.setdefault(item['request_id'], []).append(item)
        return all(by_request.get(request_id) and all(item['state'] == 'state_verified'
                   for item in by_request[request_id]) for request_id in request_ids)


def _cli() -> int:
    parser = argparse.ArgumentParser(description="HOI4-EYES fail-closed manual effect queue")
    parser.add_argument("--queue", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare", help="validate and stage an approved V5 package")
    prep.add_argument("--package", type=Path, required=True)
    prep.add_argument("--campaign-id", required=True)
    prep.add_argument("--request-id", required=True)
    prep.add_argument("--approved", action="store_true", help="record explicit player approval")
    prep.add_argument("--dry-run", action="store_true")
    sub.add_parser("status", help="show the durable queue")
    sent = sub.add_parser("mark-sent", help="record that you manually invoked the prepared event")
    sent.add_argument("--campaign-id", required=True)
    sent.add_argument("--request-id", required=True)
    sent.add_argument("--action-id", choices=sorted(ALLOWED_EFFECTS), required=True)
    sent.add_argument("--i-ran-command", action="store_true", required=True)
    verified = sub.add_parser("verify-state", help="record a manual in-game state inspection")
    verified.add_argument("--campaign-id", required=True)
    verified.add_argument("--request-id", required=True)
    verified.add_argument("--action-id", choices=sorted(ALLOWED_EFFECTS), required=True)
    verified.add_argument("--i-verified-in-game", action="store_true", required=True)
    args = parser.parse_args()
    adapter = WindowsEffectAdapter(args.queue)
    if args.command == "status":
        print(json.dumps(adapter.status(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "mark-sent":
        result = adapter.mark_manually_sent(
            args.campaign_id, args.request_id, args.action_id,
            user_confirmed_submission=args.i_ran_command,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "verify-state":
        result = adapter.verify_game_state(
            args.campaign_id, args.request_id, args.action_id,
            evidence={"source": "manual_in_game_inspection", "campaign_id": args.campaign_id,
                      "request_id": args.request_id, "action_id": args.action_id,
                      "observed": args.i_verified_in_game},
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    package = json.loads(args.package.read_text(encoding="utf-8"))
    result = adapter.prepare(args.campaign_id, args.request_id, package,
                             player_approved=args.approved, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
