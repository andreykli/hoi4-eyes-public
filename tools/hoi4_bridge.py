"""File-based HOI4 to Python Bridge for campaign-scoped diplomacy.

HOI4 cannot issue HTTP requests or read arbitrary response files from script.
The mod emits a single marker through the documented `log` effect. This process
tails game.log, calls the existing /respond API, saves the JSON response, and
optionally displays it in a separate always-on-top window over HOI4.
"""

from ui_locale import tr
from user_storage import runtime_dir

import argparse
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import queue
import threading
import uuid
from typing import Any, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ai_provider import AIProvider
from campaign_identity_registry import CampaignIdentityRegistry
from hoi4_time import MAX_HOI4_ABSOLUTE_DAY
from hoi4_country_tags import COUNTRY_TAG_RE
from hoi4_campaign_identity import (
    SAVE_CAMPAIGN_IDENTITY_FORMAT, campaign_id_from_save_token,
)
from hoi4_windows_adapter import ALLOWED_EFFECTS, AdapterError, WindowsEffectAdapter


REQUEST_MARKER_V1 = "EYES_HOI4_REQUEST_V1|"
REQUEST_MARKER_V2 = "EYES_HOI4_REQUEST_V2|"
REQUEST_MARKER_V3 = "EYES_HOI4_REQUEST_V3|"
REQUEST_MARKER_V4 = "EYES_HOI4_REQUEST_V4|"
REQUEST_MARKER_V5 = "EYES_HOI4_REQUEST_V5|"
REQUEST_MARKER_V6 = "EYES_HOI4_REQUEST_V6|"
DEFAULT_HOI4_DIR = Path.home() / "Documents" / "Paradox Interactive" / "Hearts of Iron IV"
DEFAULT_GAME_LOG = Path(os.getenv("EYES_GAME_LOG") or DEFAULT_HOI4_DIR / "logs" / "game.log")
DEFAULT_RESPONSE_FILE = runtime_dir() / "response.json"
REQUEST_TAG_RE = COUNTRY_TAG_RE
APPLIED_MARKER_V1 = "EYES_HOI4_ACTION_APPLIED_V1|"
APPLIED_MARKER_V2 = "EYES_HOI4_ACTION_APPLIED_V2|"
FAILED_MARKER_V2 = "EYES_HOI4_ACTION_FAILED_V2|"
DIPLOMATIC_REFUSAL_MARKER_V1 = "EYES_HOI4_DIPLOMATIC_REFUSAL_V1|"
TREATY_DECLINED_MARKER_V1 = "EYES_HOI4_TREATY_DECLINED_V1|"
TERRITORY_RESULT_MARKER_V1 = "EYES_HOI4_TERRITORY_RESULT_V1|"
COUNTRY_SELECTION_MARKER_V1 = "EYES_HOI4_COUNTRY_SELECTED_V1|"
COUNTRY_SELECTION_MARKER_V3 = "EYES_HOI4_COUNTRY_SELECTED_V3|"
FRESH_CAMPAIGN_SELECTION_MARKER_V1 = "EYES_HOI4_CAMPAIGN_FRESH_V1|"
COUNTRY_PAIR_CONTEXT_MARKER_V1 = "EYES_HOI4_PAIR_CONTEXT_V1|"
COUNTRY_PAIR_CONTEXT_MARKER_V2 = "EYES_HOI4_PAIR_CONTEXT_V2|"
COUNTRY_PAIR_IDEOLOGY_MATCH_MARKER_V1 = "EYES_HOI4_IDEOLOGY_MATCH_V1|"
COUNTRY_PAIR_MAJOR_POWER_MATCH_MARKER_V1 = "EYES_HOI4_MAJOR_POWER_MATCH_V1|"
COUNTRY_PAIR_CLAIM_MARKER_V1 = "EYES_HOI4_PAIR_CLAIM_V1|"
COUNTRY_PAIR_CONTEXT_END_V2 = "EYES_HOI4_PAIR_CONTEXT_END_V2|"
ANNEXATION_PROPOSAL_MARKER_V1 = "EYES_HOI4_ANNEXATION_PROPOSAL_V1|"
STATE_INTEGRATION_MARKER_V1 = "EYES_HOI4_STATE_INTEGRATION_V1|"
STATE_INTEGRATION_MARKER_V2 = "EYES_HOI4_STATE_INTEGRATION_V2|"
STATE_INTEGRATION_SNAPSHOT_END_V1 = "EYES_HOI4_STATE_INTEGRATION_SNAPSHOT_END_V1|"
STATE_INTEGRATION_SNAPSHOT_END_V2 = "EYES_HOI4_STATE_INTEGRATION_SNAPSHOT_END_V2|"
HISTORICAL_FOCUSES_MARKER_V1 = "EYES_HOI4_HISTORICAL_FOCUSES_V1|"
HISTORICAL_FOCUSES_MARKER_V2 = "EYES_HOI4_HISTORICAL_FOCUSES_V2|"
BRIDGE_UI_LOG_ENV = "EYES_BRIDGE_UI_LOG"
_BRIDGE_UI_LOG_LOCK = threading.Lock()


def compute_bridge_source_revision() -> str:
    """Fingerprint Bridge code so the launcher can reject stale instances."""
    try:
        root = Path(__file__).resolve().parent
        payload = b''.join((root / name).read_bytes() for name in (
            'hoi4_bridge.py', 'ai_provider.py', 'autonomy_game_transport.py', 'world_snapshot.py', 'cabinet_roster.py', 'native_recognition.py', 'world_freshness.py', 'v6_autonomy.py', 'treaty_monitor.py', 'treaty_lifecycle.py', 'execution_markers.py', 'window_session_policy.py', 'bridge_instance.py', 'stack_readiness.py', 'hub_control.py', 'overlay_control.py', 'chat_widgets.py', 'group_negotiation.py', 'single_mod.py', 'treaty_exit_queue.py'))
    except OSError:
        return "unavailable"
    return hashlib.sha256(payload).hexdigest()[:16]


def write_bridge_runtime_status(path: Path, *, pid: int, response_file: Path,
                                game_log: Path, endpoint: str, overlay_enabled: bool,
                                request_timeout_seconds: int = 165,
                                ollama_timeout_seconds: float = 150.0,
                                source_revision: Optional[str] = None) -> Dict[str, Any]:
    status = {
        "pid": int(pid),
        "source_revision": source_revision or compute_bridge_source_revision(),
        "response_file": str(response_file.resolve()),
        "game_log": str(game_log.resolve()),
        "endpoint": str(endpoint),
        "overlay_enabled": bool(overlay_enabled),
        "request_timeout_seconds": int(request_timeout_seconds),
        "ollama_timeout_seconds": float(ollama_timeout_seconds),
        "started_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{pid}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(status, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    return status


def clear_bridge_runtime_status(path: Path, pid: int) -> bool:
    """Remove only the status record owned by this Bridge process."""
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(status, dict) and status.get("pid") == int(pid):
            path.unlink()
            return True
    except (OSError, ValueError, TypeError):
        pass
    return False


def log_bridge_ui_event(event: str, **details) -> None:
    """Append a small, content-free diagnostic record for pair-chat visibility.

    This deliberately records IDs, country tags, and window state only; message
    text, prompts, API tokens, and model output never go into this log.
    """
    try:
        configured_path = os.getenv(BRIDGE_UI_LOG_ENV)
        if configured_path:
            log_path = Path(configured_path)
        else:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
            log_path = runtime_dir() / "logs" / f"bridge_ui_{stamp}.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "pid": os.getpid(),
            "event": str(event),
            **details,
        }
        with _BRIDGE_UI_LOG_LOCK:
            with log_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    except (OSError, TypeError, ValueError, RuntimeError):
        # Diagnostics must never prevent a country chat from opening. A
        # RuntimeError covers an undeterminable profile directory.
        return


def _hoi4_window_pid(pids, foreground_pid, visible_pids):
    if foreground_pid in pids:
        return foreground_pid
    if len(pids) == 1:
        return next(iter(pids))
    visible = pids & visible_pids
    return next(iter(visible)) if len(visible) == 1 else None


def get_hoi4_window_state(overlay_hwnd=None):
    """Return HOI4 and owned-overlay focus state, or None when unavailable."""
    if os.name != "nt":
        return None
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        return None
    pids = set()
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                if entry.szExeFile.casefold() == "hoi4.exe":
                    pids.add(int(entry.th32ProcessID))
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snapshot)

    foreground = user32.GetForegroundWindow()
    foreground_pid = wintypes.DWORD()
    if foreground:
        user32.GetWindowThreadProcessId(foreground, ctypes.byref(foreground_pid))
    minimized = bool(foreground and user32.IsIconic(foreground))
    foreground_value = int(foreground or 0)
    foreground_root = user32.GetAncestor(foreground, 2) if foreground else None  # GA_ROOT
    foreground_root_value = int(foreground_root or foreground_value)
    overlay_root = (
        user32.GetAncestor(overlay_hwnd, 2) if overlay_hwnd else None
    )  # Tk may provide a child HWND; compare top-level windows.
    overlay_root_value = int(overlay_root or (overlay_hwnd or 0))
    overlay_foreground = bool(
        overlay_hwnd and foreground_root_value and foreground_root_value == overlay_root_value
    )
    game_foreground = int(foreground_pid.value) in pids
    visible_pids = set()
    if len(pids) > 1 and not game_foreground:
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        user32.IsWindowVisible.argtypes = [wintypes.HWND]

        @callback_type
        def collect_window(hwnd, _):
            if user32.IsWindowVisible(hwnd):
                owner = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
                if owner.value in pids:
                    visible_pids.add(owner.value)
            return True

        user32.EnumWindows(collect_window, 0)
    game_pid = _hoi4_window_pid(pids, int(foreground_pid.value), visible_pids)
    return {
        "running": bool(pids),
        "foreground": game_foreground,
        "game_foreground": game_foreground,
        "overlay_foreground": overlay_foreground,
        "minimized": minimized,
        "pid": game_pid,
        "pids": sorted(pids),
    }
COUNTRY_SELECTION_MARKER_V2 = "EYES_HOI4_COUNTRY_SELECTED_V2|"


def game_console_action_for(response: Dict[str, Any], request: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Never turn a chat acceptance into a game command.

    Game effects must go through the versioned agreement approval and execution
    APIs, which verify both signatories, the exact accepted terms, the current
    campaign, and the player's explicit approval. This compatibility hook only
    receives a model response and a chat request, so it cannot establish those
    guarantees and must not expose the old hard-coded GER--FRA event.
    """
    return None


def _normalize_marker_number(value: str, *, integer: bool = False,
                             minimum: float, maximum: float) -> str:
    """Keep unresolved game substitutions unknown instead of failing the request."""
    raw = str(value).strip()
    if not raw or raw.lower() in {"unknown", "unavailable"}:
        return "unknown"
    try:
        number = float(raw)
    except (TypeError, ValueError, OverflowError):
        return "unknown"
    if not (number == number and abs(number) != float("inf") and minimum <= number <= maximum):
        return "unknown"
    if integer:
        if not number.is_integer():
            return "unknown"
        return str(int(number))
    return format(number, ".12g")


def parse_state_integration_line(line: str) -> Optional[Dict[str, Any]]:
    """Parse state facts emitted by the explicit, read-only HOI4 integration probe."""
    marker_v2 = line.find(STATE_INTEGRATION_MARKER_V2)
    marker_version = 2 if marker_v2 >= 0 else 1
    marker = STATE_INTEGRATION_MARKER_V2 if marker_version == 2 else STATE_INTEGRATION_MARKER_V1
    marker_at = marker_v2 if marker_version == 2 else line.find(marker)
    if marker_at < 0:
        return None
    fields = line[marker_at + len(marker):].rstrip("\r\n").split("|", 12)
    if len(fields) != 13:
        raise ValueError(f"Malformed HOI4 state integration V{marker_version} marker")
    (raw_campaign, player, raw_request_id, raw_state_id, owner, controller,
     raw_game_day, raw_owner_since, raw_resistance, raw_compliance,
     raw_infrastructure, raw_stability, raw_is_core) = fields
    if marker_version == 2:
        campaign_id = campaign_id_from_save_token(raw_campaign)
        match = re.fullmatch(
            rf"{re.escape(raw_campaign)}:integration:([0-9]{{1,10}}):([0-9]{{1,7}})",
            raw_request_id,
        )
        if not match:
            raise ValueError("State integration V2 request_id is not save-campaign scoped")
        request_id = f"{campaign_id}:integration:{match.group(1)}:{match.group(2)}"
    else:
        campaign_id = raw_campaign
        request_id = raw_request_id
    if not re.fullmatch(r"[0-9a-f]{32}", campaign_id):
        raise ValueError("State integration marker requires a campaign UUID")
    if not REQUEST_TAG_RE.fullmatch(player) or not REQUEST_TAG_RE.fullmatch(owner) \
            or not REQUEST_TAG_RE.fullmatch(controller):
        raise ValueError("State integration marker contains an invalid country tag")
    if owner != player:
        raise ValueError("State integration marker owner does not match its player scope")
    if not re.fullmatch(rf"{re.escape(campaign_id)}:integration:[0-9]{{1,10}}:[0-9]{{1,7}}", request_id):
        raise ValueError("State integration request_id is not campaign and state scoped")

    def integer(value, name, minimum, maximum, *, optional=False):
        if optional and str(value).strip().lower() in {"", "unknown", "unavailable"}:
            return None
        normalized = _normalize_marker_number(
            value, integer=True, minimum=minimum, maximum=maximum,
        )
        if normalized == "unknown":
            raise ValueError(f"State integration {name} is not a valid integer")
        return int(normalized)

    def numeric(value, minimum, maximum):
        raw = str(value).strip()
        if raw.lower() in {"unknown", "unavailable"}:
            return None
        try:
            number = float(raw)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("State integration metric is not numeric or unknown") from error
        if not (number == number and abs(number) != float("inf") and minimum <= number <= maximum):
            raise ValueError("State integration metric is outside its documented range")
        return number

    state_id = integer(raw_state_id, "state_id", 1, 1_000_000)
    game_day = integer(raw_game_day, "game_day", 0, MAX_HOI4_ABSOLUTE_DAY)
    owner_since_day = integer(
        raw_owner_since, "owner_since_day", 0, MAX_HOI4_ABSOLUTE_DAY, optional=True,
    )
    if owner_since_day is not None and owner_since_day > game_day:
        raise ValueError("State integration owner_since_day is later than game_day")
    if raw_is_core.lower() not in {"yes", "no"}:
        raise ValueError("State integration is_core must be yes or no")
    if raw_is_core.lower() != "no":
        raise ValueError("Core states are not part of integration tracking")
    context = {
        "state_id": state_id, "owner_country": owner, "controller_country": controller,
        "game_day": game_day, "owner_since_day": owner_since_day,
        "resistance": numeric(raw_resistance, 0, 100),
        "compliance": numeric(raw_compliance, 0, 100),
        # The installed 1.19.3 documentation defines infrastructure_level as 0..10.
        "infrastructure_level": numeric(raw_infrastructure, 0, 10),
        "stability": numeric(raw_stability, 0, 1), "is_core": False,
    }
    return {
        "campaign_id": campaign_id, "player_country": player,
        "request_id": request_id, "context": context,
        "source_marker": f"EYES_HOI4_STATE_INTEGRATION_V{marker_version}",
        **({"source_campaign_token": raw_campaign} if marker_version == 2 else {}),
    }


def parse_state_integration_snapshot_end_line(line: str) -> Optional[Dict[str, Any]]:
    marker_v2 = line.find(STATE_INTEGRATION_SNAPSHOT_END_V2)
    marker_version = 2 if marker_v2 >= 0 else 1
    marker = (STATE_INTEGRATION_SNAPSHOT_END_V2 if marker_version == 2
              else STATE_INTEGRATION_SNAPSHOT_END_V1)
    marker_at = marker_v2 if marker_version == 2 else line.find(marker)
    if marker_at < 0:
        return None
    fields = line[marker_at + len(marker):].rstrip("\r\n").split("|", 4)
    if len(fields) != 5:
        raise ValueError("Malformed HOI4 state integration snapshot completion marker")
    raw_campaign, player, raw_seq, raw_count, raw_day = fields
    if marker_version == 2:
        campaign = campaign_id_from_save_token(raw_campaign)
    else:
        campaign = raw_campaign
    if not re.fullmatch(r"[0-9a-f]{32}", campaign) or not REQUEST_TAG_RE.fullmatch(player):
        raise ValueError("State integration snapshot marker has an invalid campaign or country")
    def number(value, name, minimum, maximum):
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError(f"State integration snapshot {name} is not an integer") from error
        if str(parsed) != value or not minimum <= parsed <= maximum:
            raise ValueError(f"State integration snapshot {name} is outside its supported range")
        return parsed
    sequence = number(raw_seq, "sequence", 1, 10_000_000_000)
    count = number(raw_count, "count", 0, 2000)
    game_day = number(raw_day, "game day", 0, MAX_HOI4_ABSOLUTE_DAY)
    return {"campaign_id": campaign, "player_country": player,
            "snapshot_seq": sequence, "expected_count": count, "game_day": game_day,
            "source_marker": f"EYES_HOI4_STATE_INTEGRATION_SNAPSHOT_END_V{marker_version}",
            **({"source_campaign_token": raw_campaign} if marker_version == 2 else {})}


def parse_historical_focuses_line(line: str) -> Optional[Dict[str, Any]]:
    """Parse a campaign-scoped read-only marker for HOI4's history-focus rule."""
    marker_v2 = line.find(HISTORICAL_FOCUSES_MARKER_V2)
    marker_version = 2 if marker_v2 >= 0 else 1
    marker = (HISTORICAL_FOCUSES_MARKER_V2 if marker_version == 2
              else HISTORICAL_FOCUSES_MARKER_V1)
    marker_at = marker_v2 if marker_version == 2 else line.find(marker)
    if marker_at < 0:
        return None
    fields = line[marker_at + len(marker):].rstrip("\r\n").split("|", 5)
    if len(fields) != 5:
        raise ValueError(f"Malformed HOI4 historical-focus V{marker_version} marker")
    raw_campaign, player, raw_request_id, raw_day, raw_enabled = fields
    if marker_version == 2:
        campaign = campaign_id_from_save_token(raw_campaign)
        match = re.fullmatch(
            rf"{re.escape(raw_campaign)}:integration:([0-9]{{1,10}}):rules", raw_request_id,
        )
        request_id = (f"{campaign}:integration:{match.group(1)}:rules" if match else "")
    else:
        campaign, request_id = raw_campaign, raw_request_id
    if not re.fullmatch(r"[0-9a-f]{32}", campaign) or not REQUEST_TAG_RE.fullmatch(player):
        raise ValueError("Historical-focus marker has an invalid campaign or country")
    match = re.fullmatch(rf"{re.escape(campaign)}:integration:([0-9]{{1,10}}):rules", request_id)
    if not match:
        raise ValueError("Historical-focus marker request_id is not campaign-scoped")
    try:
        game_day = int(raw_day)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("Historical-focus marker day is not an integer") from error
    if str(game_day) != raw_day or not 0 <= game_day <= MAX_HOI4_ABSOLUTE_DAY:
        raise ValueError("Historical-focus marker day is outside its supported range")
    if raw_enabled not in {"yes", "no"}:
        raise ValueError("Historical-focus marker value must be yes or no")
    return {
        "campaign_id": campaign, "player_country": player,
        "snapshot_seq": int(match.group(1)), "request_id": request_id,
        "game_day": game_day, "historical_focuses_enabled": raw_enabled == "yes",
        "source_marker": f"EYES_HOI4_HISTORICAL_FOCUSES_V{marker_version}",
        **({"source_campaign_token": raw_campaign} if marker_version == 2 else {}),
    }


def parse_request_line(line: str) -> Optional[Dict[str, str]]:
    """Parse one HOI4 request marker; legacy versions remain readable."""
    marker_versions = (
        (REQUEST_MARKER_V6, 6),
        (REQUEST_MARKER_V5, 5),
        (REQUEST_MARKER_V4, 4),
        (REQUEST_MARKER_V3, 3),
        (REQUEST_MARKER_V2, 2),
        (REQUEST_MARKER_V1, 1),
    )
    marker_at = -1
    marker = ""
    version = 0
    for candidate, candidate_version in marker_versions:
        marker_at = line.find(candidate)
        if marker_at >= 0:
            marker = candidate
            version = candidate_version
            break
    if marker_at < 0:
        return None

    split_limit = {1: 3, 2: 4, 3: 9, 4: 10, 5: 11, 6: 20}[version]
    expected_fields = {1: 4, 2: 5, 3: 10, 4: 11, 5: 12, 6: 21}[version]
    fields = line[marker_at + len(marker):].rstrip("\r\n").split("|", split_limit)
    if len(fields) != expected_fields:
        raise ValueError(f"Malformed EYES_HOI4_REQUEST_V{version} marker")

    game_context = None
    campaign_id = None
    if version in {5, 6}:
        (campaign_id, sender, receiver, request_id, conversation_id, game_date,
         *context_fields, message) = fields
        if version == 5:
            war_status, same_faction, existing_nap, relations, threat = context_fields
            game_context = {
                "date": game_date,
                "war_status": war_status,
                "same_faction": same_faction,
                "existing_non_aggression_pact": existing_nap,
                "relations": _normalize_marker_number(
                    relations, minimum=-100, maximum=100,
                ),
                "threat": _normalize_marker_number(
                    threat, minimum=0, maximum=1,
                ),
            }
        else:
            (initiator_exists, target_exists, war_status, same_faction, existing_nap,
             relations, threat, initiator_owned_states, target_owned_states,
             initiator_divisions, target_divisions, initiator_factories, target_factories,
             target_stability) = context_fields
            game_context = {
                "date": game_date,
                "initiator_exists": initiator_exists,
                "target_exists": target_exists,
                "war_status": war_status,
                "same_faction": same_faction,
                "existing_non_aggression_pact": existing_nap,
                "relations": _normalize_marker_number(
                    relations, minimum=-100, maximum=100,
                ),
                "threat": _normalize_marker_number(
                    threat, minimum=0, maximum=1,
                ),
                "initiator_owned_states": _normalize_marker_number(
                    initiator_owned_states, integer=True, minimum=0, maximum=10000,
                ),
                "target_owned_states": _normalize_marker_number(
                    target_owned_states, integer=True, minimum=0, maximum=10000,
                ),
                "initiator_divisions": _normalize_marker_number(
                    initiator_divisions, integer=True, minimum=0, maximum=100000,
                ),
                "target_divisions": _normalize_marker_number(
                    target_divisions, integer=True, minimum=0, maximum=100000,
                ),
                "initiator_factories": _normalize_marker_number(
                    initiator_factories, minimum=0, maximum=100000,
                ),
                "target_factories": _normalize_marker_number(
                    target_factories, minimum=0, maximum=100000,
                ),
                "target_stability": _normalize_marker_number(
                    target_stability, minimum=0, maximum=1,
                ),
            }
    elif version == 4:
        (sender, receiver, request_id, conversation_id, game_date,
         war_status, same_faction, existing_nap, relations, threat, message) = fields
        game_context = {
            "date": game_date,
            "war_status": war_status,
            "same_faction": same_faction,
            "existing_non_aggression_pact": existing_nap,
            "relations": relations,
            "threat": threat,
        }
    elif version == 3:
        (sender, receiver, request_id, conversation_id, game_date,
         war_status, same_faction, relations, threat, message) = fields
        game_context = {
            "date": game_date,
            "war_status": war_status,
            "same_faction": same_faction,
            "relations": relations,
            "threat": threat,
        }
    elif version == 2:
        sender, receiver, request_id, conversation_id, message = fields
    else:
        sender, receiver, conversation_id, message = fields
        request_id = f"legacy:{conversation_id}:{sender}:{receiver}"

    if not REQUEST_TAG_RE.fullmatch(sender) or not REQUEST_TAG_RE.fullmatch(receiver):
        raise ValueError("Marker sender and receiver must be three-character HOI4 country tags")
    if version in {5, 6}:
        if not re.fullmatch(r"[0-9a-f]{32}", campaign_id):
            raise ValueError(f"V{version} marker requires a 32-character lowercase hex campaign_id")
        if not re.fullmatch(rf"{re.escape(campaign_id)}:[0-9]{{4,}}", request_id):
            raise ValueError(f"V{version} request_id must be namespaced by campaign_id and a sequence")
        if conversation_id != f"{campaign_id}:{sender}-{receiver}":
            raise ValueError(f"V{version} conversation_id must be isolated to its campaign and country pair")
    if not request_id or not conversation_id or not message.strip():
        raise ValueError("Marker requires request_id, conversation_id, and message")

    request = {
        "sender": sender,
        "receiver": receiver,
        "request_id": request_id,
        "conversation_id": conversation_id,
        "message": message.strip(),
    }
    if campaign_id is not None:
        request["campaign_id"] = campaign_id
    if version == 6:
        request["game_context_schema_version"] = 6
    if game_context is not None:
        request["game_context"] = game_context
    return request


def parse_country_selection_line(line: str) -> Optional[Dict[str, str]]:
    """Parse a harmless in-game country selection marker; never treats it as a request."""
    marker_at = line.find(COUNTRY_SELECTION_MARKER_V3)
    if marker_at >= 0:
        fields = line[marker_at + len(COUNTRY_SELECTION_MARKER_V3):].rstrip("\r\n").split("|")
        if len(fields) != 3:
            return None
        player_country, selected_country, campaign_token = fields
        if (not REQUEST_TAG_RE.fullmatch(player_country)
                or not REQUEST_TAG_RE.fullmatch(selected_country)
                or player_country == selected_country):
            return None
        try:
            campaign_id = campaign_id_from_save_token(campaign_token)
        except ValueError:
            return None
        return {"player_country": player_country, "selected_country": selected_country,
                "campaign_id": campaign_id, "campaign_identity_source": "hoi4_save_seed_v1",
                "source_campaign_token": campaign_token,
                "source_marker": "EYES_HOI4_COUNTRY_SELECTED_V3"}
    marker_at = line.find(COUNTRY_SELECTION_MARKER_V2)
    if marker_at >= 0:
        fields = line[marker_at + len(COUNTRY_SELECTION_MARKER_V2):].rstrip("\r\n").split("|")
        if len(fields) != 3:
            return None
        player_country, selected_country, campaign_id = fields
        if (not REQUEST_TAG_RE.fullmatch(player_country)
                or not REQUEST_TAG_RE.fullmatch(selected_country)
                or player_country == selected_country
                or not re.fullmatch(r"[0-9a-f]{32}", campaign_id)):
            return None
        return {"player_country": player_country, "selected_country": selected_country,
                "campaign_id": campaign_id}
    marker_at = line.find(COUNTRY_SELECTION_MARKER_V1)
    if marker_at < 0:
        return None
    fields = line[marker_at + len(COUNTRY_SELECTION_MARKER_V1):].rstrip("\r\n").split("|")
    if len(fields) != 2:
        return None
    player_country, selected_country = fields
    if (not REQUEST_TAG_RE.fullmatch(player_country)
            or not REQUEST_TAG_RE.fullmatch(selected_country)
            or player_country == selected_country):
        return None
    return {"player_country": player_country, "selected_country": selected_country}


def parse_fresh_campaign_selection_line(line: str) -> Optional[Dict[str, str]]:
    """Recognize the first selected counterpart in a save without EYES identity."""
    marker_at = line.find(FRESH_CAMPAIGN_SELECTION_MARKER_V1)
    if marker_at < 0:
        return None
    fields = line[marker_at + len(FRESH_CAMPAIGN_SELECTION_MARKER_V1):].rstrip("\r\n").split("|")
    if (len(fields) != 2 or not REQUEST_TAG_RE.fullmatch(fields[0])
            or not REQUEST_TAG_RE.fullmatch(fields[1]) or fields[0] == fields[1]):
        return None
    prefix = line[:marker_at]
    timestamp = re.search(r"\[(\d{2}:\d{2}:\d{2})\](?:\[([^\]]+)\])?", prefix)
    if timestamp is None:
        return None
    game_date = timestamp.group(2) or "unknown-date"
    return {
        "player_country": fields[0], "selected_country": fields[1],
        "session_key": f"{game_date}|{timestamp.group(1)}|{fields[0]}|{fields[1]}",
    }


def parse_annexation_proposal_line(line: str) -> Optional[Dict[str, str]]:
    """Parse a UI request to open an annexation discussion; it is not an action."""
    marker_at = line.find(ANNEXATION_PROPOSAL_MARKER_V1)
    if marker_at < 0:
        return None
    fields = line[marker_at + len(ANNEXATION_PROPOSAL_MARKER_V1):].rstrip("\r\n").split("|")
    if len(fields) != 4:
        return None
    player, target, campaign_token, method = fields
    if (not REQUEST_TAG_RE.fullmatch(player) or not REQUEST_TAG_RE.fullmatch(target)
            or player == target or method not in {"voluntary", "political_pressure"}):
        return None
    try:
        campaign_id = campaign_id_from_save_token(campaign_token)
    except ValueError:
        return None
    return {"event": "annexation_proposal", "player_country": player,
            "selected_country": target, "campaign_id": campaign_id,
            "campaign_identity_source": "hoi4_save_seed_v1",
            "source_campaign_token": campaign_token, "annexation_method": method,
            "source_marker": "EYES_HOI4_ANNEXATION_PROPOSAL_V1"}


def logged_game_date(line: str) -> str:
    """Read the native calendar prefix; never infer dates from a day counter."""
    match = re.match(r'^\[[^\]\r\n]+\]\[(\d{4})\.(\d{1,2})\.(\d{1,2})\.\d{1,2}\]:', line)
    if not match: return 'unknown'
    try: return datetime(*(int(value) for value in match.groups())).date().isoformat()
    except ValueError: return 'unknown'


def parse_country_pair_context_line(line: str) -> Optional[Dict[str, Any]]:
    """Parse a selection-scoped, read-only snapshot of bilateral game facts."""
    marker_at = line.find(COUNTRY_PAIR_CONTEXT_MARKER_V1)
    if marker_at < 0:
        return None
    payload = line[marker_at + len(COUNTRY_PAIR_CONTEXT_MARKER_V1):].rstrip("\r\n")
    # Unexpanded Clausewitz variables can contain the marker separator inside
    # their [?scope.variable|precision] form. Treat the entire token as unknown
    # before splitting so later fields cannot shift into a false fact.
    payload = re.sub(r"\[\?[^\]]+\]", "unknown", payload)
    fields = payload.split("|")
    if len(fields) != 10:
        return None
    (player_country, selected_country, campaign_token, raw_sequence, raw_game_day,
     raw_relations, raw_war, raw_faction, raw_nap, raw_threat) = fields
    if (not REQUEST_TAG_RE.fullmatch(player_country)
            or not REQUEST_TAG_RE.fullmatch(selected_country)
            or player_country == selected_country):
        return None
    try:
        campaign_id = campaign_id_from_save_token(campaign_token)
        sequence = int(raw_sequence)
    except (ValueError, TypeError, OverflowError):
        return None
    if str(sequence) != raw_sequence or not 1 <= sequence <= 9_999_999_999:
        return None

    def boolean(value: str) -> str:
        return {"1": "yes", "yes": "yes", "0": "no", "no": "no"}.get(
            str(value).strip().lower(), "unknown",
        )

    request_id = f"{campaign_id}:pair-context:{sequence:010d}"
    return {
        "sender": player_country,
        "receiver": selected_country,
        "campaign_id": campaign_id,
        # Keep the native seed token until Bridge has rebound this V1 marker
        # to the per-save campaign identity. Without it, the read-only game
        # context would be persisted under the legacy hash even after the
        # corresponding country-selection marker received a fresh UUID.
        "source_campaign_token": campaign_token,
        "campaign_identity_source": "hoi4_save_seed_v1",
        "request_id": request_id,
        "conversation_id": f"{campaign_id}:{player_country}-{selected_country}",
        "game_context_schema_version": 7,
        "source_marker": "EYES_HOI4_PAIR_CONTEXT_V1",
        "game_context": {
            "date": logged_game_date(line),
            "game_day": _normalize_marker_number(
                raw_game_day, integer=True, minimum=0, maximum=MAX_HOI4_ABSOLUTE_DAY,
            ),
            "game_day_basis": "global.num_days",
            "war_status": boolean(raw_war),
            "same_faction": boolean(raw_faction),
            "existing_non_aggression_pact": boolean(raw_nap),
            "relations": _normalize_marker_number(
                raw_relations, minimum=-100, maximum=100,
            ),
            "relations_direction": "selected_country_toward_player_country",
            "threat": _normalize_marker_number(
                raw_threat, minimum=0, maximum=1,
            ),
        },
    }


def _pair_context_v2_identity(fields, expected_length, marker_name):
    if len(fields) != expected_length:
        raise ValueError(f"Malformed {marker_name} marker")
    player, selected, campaign_token, raw_sequence = fields[:4]
    if (not REQUEST_TAG_RE.fullmatch(player) or not REQUEST_TAG_RE.fullmatch(selected)
            or player == selected):
        raise ValueError(f"{marker_name} has invalid participant tags")
    try:
        campaign_id = campaign_id_from_save_token(campaign_token)
        sequence = int(raw_sequence)
    except (ValueError, TypeError, OverflowError) as error:
        raise ValueError(f"{marker_name} has an invalid campaign or sequence") from error
    if str(sequence) != raw_sequence or not 1 <= sequence <= 9_999_999_999:
        raise ValueError(f"{marker_name} sequence is outside the supported range")
    return player, selected, campaign_token, campaign_id, sequence


def parse_country_pair_context_v2_line(line: str) -> Optional[Dict[str, Any]]:
    """Parse a versioned ideology and bilateral-context header; claims arrive separately."""
    marker_at = line.find(COUNTRY_PAIR_CONTEXT_MARKER_V2)
    if marker_at < 0:
        return None
    payload = line[marker_at + len(COUNTRY_PAIR_CONTEXT_MARKER_V2):].rstrip("\r\n")
    payload = re.sub(r"\[\?[^\]]+\]", "unknown", payload)
    fields = payload.split("|")
    if len(fields) != 12:
        raise ValueError("Malformed HOI4 pair-context V2 marker")
    player, selected, token, campaign, sequence = _pair_context_v2_identity(
        fields, 12, "HOI4 pair-context V2",
    )
    (_, _, _, _, raw_game_day, raw_relations, raw_war, raw_faction, raw_nap,
     raw_threat, raw_player_ideology, raw_selected_ideology) = fields
    ideology_names = {"0": "unknown", "1": "fascism", "2": "democratic",
                      "3": "neutrality", "4": "communism"}
    if raw_player_ideology not in ideology_names or raw_selected_ideology not in ideology_names:
        raw_player_ideology = raw_player_ideology if raw_player_ideology in ideology_names else "0"
        raw_selected_ideology = raw_selected_ideology if raw_selected_ideology in ideology_names else "0"

    def boolean(value: str) -> str:
        return {"1": "yes", "yes": "yes", "0": "no", "no": "no"}.get(
            str(value).strip().lower(), "unknown",
        )

    return {
        "sender": player, "receiver": selected, "campaign_id": campaign,
        "campaign_token": token, "sequence": sequence,
        "request_id": f"{campaign}:pair-doctrine:{sequence:010d}",
        "conversation_id": f"{campaign}:{player}-{selected}",
        "game_context_schema_version": 8,
        "source_marker": "EYES_HOI4_PAIR_CONTEXT_V2",
        "game_context": {
            "date": logged_game_date(line),
            "game_day": _normalize_marker_number(
                raw_game_day, integer=True, minimum=0, maximum=MAX_HOI4_ABSOLUTE_DAY,
            ),
            "game_day_basis": "global.num_days",
            "war_status": boolean(raw_war),
            "same_faction": boolean(raw_faction),
            "existing_non_aggression_pact": boolean(raw_nap),
            "relations": _normalize_marker_number(raw_relations, minimum=-100, maximum=100),
            "relations_direction": "selected_country_toward_player_country",
            "threat": _normalize_marker_number(raw_threat, minimum=0, maximum=1),
            "ideology_group": ideology_names[raw_selected_ideology],
            "player_ideology_group": ideology_names[raw_player_ideology],
            "claims": [],
        },
    }


def parse_country_pair_claim_v1_line(line: str) -> Optional[Dict[str, Any]]:
    """Parse one state claim row scoped to the selected/player country pair."""
    marker_at = line.find(COUNTRY_PAIR_CLAIM_MARKER_V1)
    if marker_at < 0:
        return None
    payload = line[marker_at + len(COUNTRY_PAIR_CLAIM_MARKER_V1):].rstrip("\r\n")
    payload = re.sub(r"\[\?[^\]]+\]", "unknown", payload)
    fields = payload.split("|")
    player, selected, token, campaign, sequence = _pair_context_v2_identity(
        fields, 8, "HOI4 pair claim V1",
    )
    (_, _, _, _, claimant, owner, raw_state_id, raw_infrastructure) = fields
    if claimant != selected or owner != player:
        raise ValueError("HOI4 pair claim does not describe selected-country claims on player-owned territory")
    state_id = _normalize_marker_number(raw_state_id, integer=True, minimum=1, maximum=1_000_000)
    if state_id == "unknown":
        raise ValueError("HOI4 pair claim has an invalid state ID")
    infrastructure = _normalize_marker_number(raw_infrastructure, minimum=0, maximum=10)
    proxy = None if infrastructure == "unknown" else round(float(infrastructure) / 10.0, 3)
    return {
        "campaign_id": campaign, "player_country": player, "selected_country": selected,
        "sequence": sequence,
        "claim": {
            "claimant_tag": claimant, "owner_tag": owner,
            "state_id": int(state_id),
            "infrastructure_level": None if infrastructure == "unknown" else float(infrastructure),
            "strategic_value_proxy": proxy,
            "value_basis": "infrastructure_level_only_not_total_state_value",
        },
    }


def parse_country_pair_ideology_match_v1_line(line: str) -> Optional[Dict[str, Any]]:
    """Parse an ideology trigger match emitted directly inside its HOI4 scope.

    This is independent of the temporary-variable getter in the V2 summary. A
    match is accepted only for one of the two marker participants and one of
    HOI4's four government groups; no country-tag inference is performed.
    """
    marker_at = line.find(COUNTRY_PAIR_IDEOLOGY_MATCH_MARKER_V1)
    if marker_at < 0:
        return None
    fields = re.sub(
        r"\[\?[^\]]+\]", "unknown",
        line[marker_at + len(COUNTRY_PAIR_IDEOLOGY_MATCH_MARKER_V1):].rstrip("\r\n"),
    ).split("|")
    player, selected, token, campaign, sequence = _pair_context_v2_identity(
        fields, 6, "HOI4 pair ideology match V1",
    )
    role, ideology = fields[4], fields[5]
    if role not in {"player", "selected"}:
        raise ValueError("HOI4 ideology match has an unsupported participant role")
    if (ideology not in {"fascism", "democratic", "neutrality", "communism"}
            and not re.fullmatch(r"modded:[a-z][a-z0-9_]{1,59}", ideology)):
        raise ValueError("HOI4 ideology match has an unsupported government group")
    return {
        "campaign_id": campaign, "campaign_token": token,
        "player_country": player, "selected_country": selected,
        "sequence": sequence, "role": role, "ideology_group": ideology,
    }


def parse_country_pair_major_power_match_v1_line(line: str) -> Optional[Dict[str, Any]]:
    """Parse a scoped ``is_major`` fact for one participant in a V2 snapshot."""
    marker_at = line.find(COUNTRY_PAIR_MAJOR_POWER_MATCH_MARKER_V1)
    if marker_at < 0:
        return None
    fields = re.sub(
        r"\[\?[^\]]+\]", "unknown",
        line[marker_at + len(COUNTRY_PAIR_MAJOR_POWER_MATCH_MARKER_V1):].rstrip("\r\n"),
    ).split("|")
    player, selected, token, campaign, sequence = _pair_context_v2_identity(
        fields, 6, "HOI4 pair major-power match V1",
    )
    role, status = fields[4], fields[5].strip().lower()
    if role not in {"player", "selected"}:
        raise ValueError("HOI4 major-power marker has an unsupported participant role")
    if status not in {"yes", "no"}:
        raise ValueError("HOI4 major-power marker must be yes or no")
    return {
        "campaign_id": campaign, "campaign_token": token,
        "player_country": player, "selected_country": selected,
        "sequence": sequence, "role": role, "is_major": status,
    }


def parse_country_pair_context_end_v2_line(line: str) -> Optional[Dict[str, Any]]:
    marker_at = line.find(COUNTRY_PAIR_CONTEXT_END_V2)
    if marker_at < 0:
        return None
    fields = line[marker_at + len(COUNTRY_PAIR_CONTEXT_END_V2):].rstrip("\r\n").split("|")
    player, selected, token, campaign, sequence = _pair_context_v2_identity(
        fields, 5, "HOI4 pair-context end V2",
    )
    raw_count = fields[4]
    try:
        count = int(raw_count)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("HOI4 pair-context end V2 count is not an integer") from error
    if str(count) != raw_count or not 0 <= count <= 1000:
        raise ValueError("HOI4 pair-context end V2 count is outside the supported range")
    return {"campaign_id": campaign, "campaign_token": token,
            "player_country": player, "selected_country": selected,
            "sequence": sequence, "expected_claim_count": count}


def open_country_selection_chat(bridge, selection: Dict[str, str]) -> Dict[str, Any]:
    """Turn a harmless native-GUI selection into the matching persisted chat."""
    if not isinstance(selection, dict) or not selection.get("campaign_id"):
        return {"event": "country_selection_chat_error", "error": "missing_campaign_id",
                "detail": "Selection marker has no campaign_id; no chat was opened."}
    chat = bridge.open_or_create_chat(selection)
    if chat.get("error"):
        return {"event": "country_selection_chat_error", **chat}
    return {
        "event": "country_selection_chat_ready", "chat_id": chat["chat_id"],
        "campaign_id": chat["campaign_id"], "conversation_id": chat["conversation_id"],
        "player_country": chat["player_country"], "selected_country": selection["selected_country"],
        "participants": chat["participants"],
    }


def recover_startup_country_selection(bridge, game_log_path, game_running):
    """Resume the last selected campaign chat when Bridge starts mid-session.

    The game-log tail begins at EOF, so a V5 country-selection marker written
    before Bridge starts would otherwise be missed. Only recover while HOI4 is
    actually running; this avoids reopening a stale selection from an old log.
    """
    if not game_running:
        return None
    recovered = bridge.recover_latest_country_selection_from_log(game_log_path)
    if recovered.get("event") != "country_selection_chat_ready":
        return None
    return {
        "player_country": recovered["player_country"],
        "selected_country": recovered["selected_country"],
        "campaign_id": recovered["campaign_id"],
    }


def write_response(path: Path, response: Dict[str, Any]) -> None:
    """Atomically write an external diagnostic/journal (not game-readable input)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


class Hoi4LogBridge:
    def __init__(self, endpoint: str, response_path: Path, timeout: int = 165):
        self.endpoint = endpoint.rstrip("/")
        self.response_path = response_path
        self.provider = AIProvider(base_url=self.endpoint.rsplit("/", 1)[0], timeout=timeout)
        self.campaign_identity_registry = CampaignIdentityRegistry(
            self.response_path.with_name(self.response_path.name + ".campaign-identities.json")
        )
        self._pending_fresh_campaign = None
        self._campaign_id_by_seed_token = {}
        from treaty_monitor import ObservationQueue
        self.treaty_observations = ObservationQueue(self.response_path.with_name(self.response_path.name + '.treaty-observations.json'))
        from autonomy_game_transport import SnapshotCollector
        self.autonomy_collector = SnapshotCollector()
        self.autonomy_pending_path = self.response_path.with_name(self.response_path.name + '.autonomy-pending.json')
        self.autonomy_pending = []
        if self.autonomy_pending_path.exists():
            from v6_autonomy import validate_snapshot
            recovered = json.loads(self.autonomy_pending_path.read_text(encoding='utf-8'))
            if not isinstance(recovered, list) or len(recovered) > 16:
                raise ValueError('invalid autonomy pending journal')
            for row in recovered:
                if not isinstance(row, dict) or not re.fullmatch(r'[a-f0-9]{32}', row.get('campaign_id', '')):
                    raise ValueError('invalid autonomy pending identity')
                validate_snapshot(row['snapshot'])
            self.autonomy_pending = recovered
        self._autonomy_retry_after = 0.0
        self.processed_request_ids = set()
        self.pending_nap_request_ids = []
        self.pending_territory_results = []
        self.pending_agreement_markers = []
        self.pending_agreement_results = []
        self.pending_state_operation_results = []
        self.pending_diplomatic_refusal_results = []
        self.pending_state_integration_observations = []
        self.pending_state_integration_snapshots = []
        self.state_integration_snapshot_buffers = {}
        self.state_integration_game_rules_buffers = {}
        self.country_pair_context_buffers = {}
        self._state_integration_journal_has_truncated_tail = False
        self._pending_execution_lock = threading.RLock()
        self.pending_execution_results_path = self.response_path.with_name(
            self.response_path.name + ".execution-results-pending.json"
        )
        self.approved_effects_queue_path = self.response_path.with_name(
            self.response_path.name + ".approved-effects.json"
        )
        self.windows_effect_adapter = WindowsEffectAdapter(self.approved_effects_queue_path)
        self.state_integration_queue_path = self.response_path.with_name(
            self.response_path.name + ".state-integration-pending.json"
        )
        self.state_integration_journal_path = self.response_path.with_name(
            self.response_path.name + ".state-integration-inflight.jsonl"
        )
        self._load_pending_execution_results()
        self._recover_pending_nap_from_response()
        self._load_pending_state_integration_observations()
        self._load_state_integration_journal()
        self._chat_open_lock = threading.Lock()
        self.territory_result_retry_interval = 2.0
        self._territory_retry_after = 0.0
        self._agreement_retry_after = 0.0
        self._state_integration_retry_after = 0.0

    def _resolve_campaign_payload(self, payload, *, fresh_campaign=None):
        """Rebase token-derived V5 identifiers to the selected save identity."""
        if not isinstance(payload, dict):
            return payload
        # Parsers built from the scoped V2 markers expose ``campaign_token``;
        # selection/request markers historically use ``source_campaign_token``.
        # Resolve either name to the active save identity before persistence.
        token = payload.get("source_campaign_token") or payload.get("campaign_token")
        if not isinstance(token, str):
            return payload
        old_id = payload.get("campaign_id")
        if fresh_campaign is not None:
            campaign_id = self.campaign_identity_registry.register_fresh_campaign(
                token, fresh_campaign["session_key"],
            )
        else:
            campaign_id = self._campaign_id_by_seed_token.get(token)
            if campaign_id is None:
                campaign_id = self.campaign_identity_registry.resolve_saved_campaign(token)
        if not isinstance(campaign_id, str) or not re.fullmatch(r"[0-9a-f]{32}", campaign_id):
            raise ValueError("campaign identity registry returned an invalid campaign_id")
        payload["campaign_id"] = campaign_id
        if isinstance(old_id, str) and old_id != campaign_id:
            for key in ("request_id", "conversation_id"):
                value = payload.get(key)
                if isinstance(value, str) and value.startswith(old_id + ":"):
                    payload[key] = campaign_id + value[len(old_id):]
        self._campaign_id_by_seed_token[token] = campaign_id
        return payload

    @staticmethod
    def _valid_pending_agreement_marker(status, marker):
        if not isinstance(status, str) or status not in {"applied", "failed"} or not isinstance(marker, str):
            return False
        fields = marker.split("|")
        expected_count = 4 if status == "applied" else 5
        return (
            len(fields) == expected_count
            and REQUEST_TAG_RE.fullmatch(fields[0]) is not None
            and REQUEST_TAG_RE.fullmatch(fields[1]) is not None
            and fields[2].startswith("agreement:")
            and fields[3] in ALLOWED_EFFECTS
        )

    def _load_pending_execution_results(self):
        """Restore unacknowledged game-result receipts before the log tail starts at EOF."""
        path = self.pending_execution_results_path
        if not path.exists():
            return
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RuntimeError(f"cannot load pending execution results: {path}") from error
        if (not isinstance(document, dict) or document.get("schema_version") != 1
                or not isinstance(document.get("items"), list)):
            raise RuntimeError("pending execution results have an invalid schema; preserved for review")

        seen = set()
        for item in document["items"]:
            if not isinstance(item, dict):
                raise RuntimeError("pending execution results have an invalid item; preserved for review")
            kind = item.get("kind")
            if kind == "territory_result":
                payload = item.get("payload")
                if (not isinstance(payload, dict)
                        or not re.fullmatch(r"[0-9a-f]{32}", str(payload.get("campaign_id", "")))
                        or not re.fullmatch(r"[0-9a-f]{32}", str(payload.get("proposal_id", "")))
                        or not re.fullmatch(r"[0-9a-f]{12}", str(payload.get("plan_id", "")))
                        or not isinstance(payload.get("status"), str)
                        or payload.get("status") not in {"applied", "failed", "rolled_back", "rollback_failed"}
                        or not isinstance(payload.get("summary"), str)):
                    raise RuntimeError("pending territory result has an invalid schema; preserved for review")
                key = (kind, payload["campaign_id"], payload["proposal_id"],
                       payload["plan_id"], payload["status"])
                target, value = self.pending_territory_results, payload
            elif kind == "agreement_marker":
                status, marker = item.get("status"), item.get("marker")
                if not self._valid_pending_agreement_marker(status, marker):
                    raise RuntimeError("pending agreement marker is invalid; preserved for review")
                key = (kind, status, marker)
                target, value = self.pending_agreement_markers, (status, marker)
            elif kind == "agreement_result":
                payload = item.get("payload")
                if (not isinstance(payload, dict)
                        or not all(isinstance(payload.get(field), str) and payload[field]
                                   for field in ("chat_id", "agreement_id", "request_id", "action_id", "marker"))
                        or not isinstance(payload.get("status"), str)
                        or payload.get("status") not in {"applied", "failed"}
                        or not isinstance(payload.get("action_id"), str)
                        or payload.get("action_id") not in ALLOWED_EFFECTS
                        or (payload.get("reason") is not None and not isinstance(payload.get("reason"), str))):
                    raise RuntimeError("pending agreement result has an invalid schema; preserved for review")
                key = (kind, payload["request_id"])
                target, value = self.pending_agreement_results, payload
            elif kind == "diplomatic_refusal_result":
                payload = item.get("payload")
                if (not isinstance(payload, dict)
                        or not all(isinstance(payload.get(field), str) and payload[field]
                                   for field in ("campaign_id", "chat_id", "refusal_request_id", "marker"))
                        or not re.fullmatch(r"[0-9a-f]{32}", payload["campaign_id"])
                        or not re.fullmatch(r"[0-9a-f]{32}", payload["chat_id"])):
                    raise RuntimeError("pending diplomatic refusal result has an invalid schema; preserved for review")
                key = (kind, payload["campaign_id"], payload["chat_id"], payload["refusal_request_id"])
                target, value = self.pending_diplomatic_refusal_results, payload
            elif kind == "state_operation":
                from state_operations import decode_result
                payload = item.get("payload")
                try:
                    decode_result(payload["marker"])
                    if not re.fullmatch(r"[0-9a-f]{32}",payload["campaign"]):
                        raise ValueError("campaign")
                except (KeyError, TypeError, ValueError) as error:
                    raise RuntimeError("invalid pending state operation result") from error
                key = (kind, payload["campaign"], payload["marker"])
                target, value = self.pending_state_operation_results, payload
            elif kind == "nap_request_id":
                request_id = item.get("request_id")
                if not isinstance(request_id, str) or not request_id or len(request_id) > 512:
                    raise RuntimeError("pending NAP request ID is invalid; preserved for review")
                key = (kind, request_id)
                target, value = self.pending_nap_request_ids, request_id
            else:
                raise RuntimeError("pending execution results contain an unknown kind; preserved for review")
            if key not in seen:
                target.append(value)
                seen.add(key)

    def _persist_pending_execution_results(self):
        """Atomically store unacknowledged result markers and request bindings."""
        path = self.pending_execution_results_path
        path.parent.mkdir(parents=True, exist_ok=True)
        items = (
            [{"kind": "territory_result", "payload": payload}
             for payload in self.pending_territory_results]
            + [{"kind": "agreement_marker", "status": status, "marker": marker}
               for status, marker in self.pending_agreement_markers]
            + [{"kind": "agreement_result", "payload": payload}
               for payload in self.pending_agreement_results]
            + [{"kind": "diplomatic_refusal_result", "payload": payload}
               for payload in self.pending_diplomatic_refusal_results]
            + [{"kind": "state_operation", "payload": payload}
               for payload in self.pending_state_operation_results]
            + [{"kind": "nap_request_id", "request_id": request_id}
               for request_id in self.pending_nap_request_ids]
        )
        temp_path = path.with_suffix(path.suffix + ".tmp")
        with temp_path.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump({"schema_version": 1, "items": items}, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)

    def _queue_pending_execution_item(self, target, value):
        with self._pending_execution_lock:
            if value not in target:
                target.append(value)
            self._persist_pending_execution_results()

    def _remove_pending_execution_item(self, target, value):
        with self._pending_execution_lock:
            if value in target:
                target.remove(value)
                self._persist_pending_execution_results()

    def _recover_pending_nap_from_response(self):
        """Migrate an old MANUAL_REQUIRED response into durable correlation state.

        Legacy only: this recognises the single GER-FRA demo round that older
        builds wrote to response.json. It is not a general recovery path — new
        rounds are correlated through the prepared package and its request ID.
        """
        if not self.response_path.exists():
            return
        try:
            result = json.loads(self.response_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(result, dict) or result.get("source") != "hoi4_game_log":
            return
        request_id = result.get("request_id")
        request = result.get("request")
        response = result.get("response")
        game_action = result.get("game_action")
        if not isinstance(request_id, str) or not request_id or len(request_id) > 512:
            return
        if (not isinstance(request, dict) or request.get("request_id") != request_id
                or request.get("sender") != "GER" or request.get("receiver") != "FRA"):
            return
        if (not isinstance(response, dict) or not isinstance(response.get("action"), dict)
                or response["action"].get("action_id") != "non_aggression_pact"
                or response["action"].get("status") != "MANUAL_REQUIRED"):
            return
        if (not isinstance(game_action, dict)
                or game_action.get("action_id") != "non_aggression_pact"
                or game_action.get("status") != "MANUAL_REQUIRED"
                or not (game_action.get("requires_user_confirmation") is True
                        or (isinstance(game_action.get("requires_user_confirmation"), str)
                            and bool(game_action["requires_user_confirmation"].strip())))
                or result.get("execution_status") is not None
                or result.get("confirmation") is not None):
            return
        self._queue_pending_execution_item(self.pending_nap_request_ids, request_id)

    def _load_pending_state_integration_observations(self):
        """Restore unacknowledged observations before tailing new game-log lines."""
        if not self.state_integration_queue_path.exists():
            return
        try:
            rows = json.loads(self.state_integration_queue_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RuntimeError(
                f"cannot load pending state integration queue: {self.state_integration_queue_path}"
            ) from error
        if not isinstance(rows, list):
            raise RuntimeError("pending state integration queue has an invalid schema; preserved for review")
        seen = set()
        for row in rows:
            kind = row.get("kind", "observation") if isinstance(row, dict) else None
            payload = row.get("payload", row) if isinstance(row, dict) else None
            if kind == "snapshot" and isinstance(payload, dict) and isinstance(payload.get("observations"), list):
                request_id = self._state_integration_snapshot_key(payload)
                target = self.pending_state_integration_snapshots
            elif (kind == "observation" and isinstance(payload, dict)
                  and isinstance(payload.get("request_id"), str)
                  and isinstance(payload.get("context"), dict)):
                request_id = payload["request_id"]
                target = self.pending_state_integration_observations
            else:
                raise RuntimeError("pending state integration queue has an invalid schema; preserved for review")
            if request_id not in seen:
                target.append(payload)
                seen.add(request_id)

    @staticmethod
    def _state_integration_key(campaign_id, player_country, snapshot_seq):
        return campaign_id, player_country, int(snapshot_seq)

    @staticmethod
    def _state_integration_snapshot_key(payload):
        """Identify a durable batch by its full contents, not its save-local counter."""
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return (f"{payload.get('campaign_id')}:{payload.get('player_country')}:"
                f"{payload.get('snapshot_seq')}:{digest}")

    @staticmethod
    def _validate_compacted_state_row(campaign_id, player_country, snapshot_seq, row):
        if not isinstance(row, dict) or not isinstance(row.get("request_id"), str):
            raise RuntimeError("compacted state integration row is invalid; preserved for review")
        context = row.get("context")
        expected_context_keys = {
            "state_id", "owner_country", "controller_country", "game_day", "owner_since_day",
            "resistance", "compliance", "infrastructure_level", "stability", "is_core",
        }
        if not isinstance(context, dict) or set(context) != expected_context_keys:
            raise RuntimeError("compacted state integration facts are invalid; preserved for review")

        def raw(value):
            return "unknown" if value is None else str(value)

        fields = [
            campaign_id, player_country, row["request_id"], raw(context["state_id"]),
            raw(context["owner_country"]), raw(context["controller_country"]), raw(context["game_day"]),
            raw(context["owner_since_day"]), raw(context["resistance"]), raw(context["compliance"]),
            raw(context["infrastructure_level"]), raw(context["stability"]),
            "yes" if context["is_core"] is True else "no",
        ]
        parsed = parse_state_integration_line(STATE_INTEGRATION_MARKER_V1 + "|".join(fields))
        sequence = int(parsed["request_id"].split(":")[2])
        expected_id = f"{campaign_id}:integration:{snapshot_seq}:{parsed['context']['state_id']}"
        if (sequence != snapshot_seq or parsed["request_id"] != expected_id
                or parsed["campaign_id"] != campaign_id or parsed["player_country"] != player_country
                or parsed["context"] != context):
            raise RuntimeError("compacted state integration row does not match its snapshot; preserved for review")
        return {"request_id": parsed["request_id"], "context": parsed["context"]}

    @staticmethod
    def _validate_compacted_historical_rule(rule):
        if not isinstance(rule, dict):
            raise RuntimeError("compacted historical-focus rule is invalid; preserved for review")
        campaign_id, player_country = rule.get("campaign_id"), rule.get("player_country")
        sequence, game_day = rule.get("snapshot_seq"), rule.get("game_day")
        if (not isinstance(campaign_id, str) or not re.fullmatch(r"[0-9a-f]{32}", campaign_id)
                or not isinstance(player_country, str) or not REQUEST_TAG_RE.fullmatch(player_country)
                or not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1
                or not isinstance(game_day, int) or isinstance(game_day, bool)
                or not 0 <= game_day <= MAX_HOI4_ABSOLUTE_DAY
                or not isinstance(rule.get("historical_focuses_enabled"), bool)):
            raise RuntimeError("compacted historical-focus rule is invalid; preserved for review")
        request_id = f"{campaign_id}:integration:{sequence}:rules"
        marker = (HISTORICAL_FOCUSES_MARKER_V1 +
                  f"{campaign_id}|{player_country}|{request_id}|{game_day}|"
                  f"{'yes' if rule['historical_focuses_enabled'] else 'no'}")
        parsed = parse_historical_focuses_line(marker)
        if parsed != rule:
            raise RuntimeError("compacted historical-focus rule does not match its marker; preserved for review")
        return parsed

    def _append_state_integration_journal(self, item):
        """Append one parsed marker so a partial snapshot survives Bridge restarts."""
        path = self.state_integration_journal_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if self._state_integration_journal_has_truncated_tail:
            raise RuntimeError(f"state integration journal has an incomplete trailing record; preserved at {path}")
        encoded = (json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with path.open("ab") as stream:
            stream.write(encoded)
            stream.flush()

    def _load_state_integration_journal(self):
        """Rebuild only unfinished read-only snapshot buffers from the append journal."""
        path = self.state_integration_journal_path
        if not path.exists():
            return
        try:
            raw = path.read_bytes()
        except OSError as error:
            raise RuntimeError(f"cannot read state integration journal: {path}") from error
        lines = raw.splitlines(keepends=True)
        seen_rows = set()
        for index, raw_line in enumerate(lines):
            # A process may be interrupted during the final append. Keep that
            # partial evidence untouched and recover all preceding complete rows.
            if not raw_line.endswith((b"\n", b"\r")):
                if index == len(lines) - 1:
                    self._state_integration_journal_has_truncated_tail = True
                    break
                raise RuntimeError("state integration journal has a truncated interior record; preserved for review")
            try:
                item = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as error:
                raise RuntimeError("state integration journal is invalid; preserved for review") from error
            if not isinstance(item, dict):
                raise RuntimeError("state integration journal record is invalid; preserved for review")
            kind = item.get("kind")
            if kind in {"state_row", "snapshot_row"}:
                payload = item.get("payload")
                if not isinstance(payload, dict) or not isinstance(payload.get("line"), str):
                    raise RuntimeError("state integration journal row is invalid; preserved for review")
                parsed = parse_state_integration_line(payload["line"])
                if parsed is None:
                    raise RuntimeError("state integration journal row has no valid marker; preserved for review")
                sequence = int(parsed["request_id"].split(":")[2])
                if sequence < 1:
                    raise RuntimeError("state integration journal sequence is invalid; preserved for review")
                key = self._state_integration_key(parsed["campaign_id"], parsed["player_country"], sequence)
                row = {"request_id": parsed["request_id"], "context": parsed["context"]}
                row_key = (key, row["request_id"])
                rows = self.state_integration_snapshot_buffers.setdefault(key, [])
                previous = next((value for value in rows if value.get("request_id") == row["request_id"]), None)
                if previous is not None and previous != row:
                    raise RuntimeError("state integration journal has conflicting duplicate rows; preserved for review")
                if previous is None and row_key not in seen_rows:
                    rows.append(row)
                    seen_rows.add(row_key)
            elif kind in {"historical_focus", "game_rule"}:
                payload = item.get("payload")
                if not isinstance(payload, dict):
                    raise RuntimeError("state integration journal rule is invalid; preserved for review")
                line = payload.get("line")
                if isinstance(line, str):
                    parsed = parse_historical_focuses_line(line)
                else:
                    parsed = payload.get("rule")
                if not isinstance(parsed, dict):
                    raise RuntimeError("state integration journal rule is invalid; preserved for review")
                key = self._state_integration_key(parsed.get("campaign_id"), parsed.get("player_country"),
                                                  parsed.get("snapshot_seq"))
                previous = self.state_integration_game_rules_buffers.get(key)
                if previous is not None and previous != parsed:
                    raise RuntimeError("state integration journal has conflicting game-rule markers; preserved for review")
                self.state_integration_game_rules_buffers[key] = parsed
            elif kind in {"snapshot_terminal", "snapshot_complete", "snapshot_discard"}:
                campaign_id = item.get("campaign_id")
                player_country = item.get("player_country")
                snapshot_seq = item.get("snapshot_seq")
                if (not isinstance(campaign_id, str) or not re.fullmatch(r"[0-9a-f]{32}", campaign_id)
                        or not isinstance(player_country, str) or not REQUEST_TAG_RE.fullmatch(player_country)
                        or not isinstance(snapshot_seq, int) or isinstance(snapshot_seq, bool) or snapshot_seq < 1
                        or item.get("outcome") not in {"complete", "discard"}):
                    raise RuntimeError("state integration journal terminal record is invalid; preserved for review")
                key = self._state_integration_key(campaign_id, player_country, snapshot_seq)
                self.state_integration_snapshot_buffers.pop(key, None)
                self.state_integration_game_rules_buffers.pop(key, None)
            elif kind == "snapshot_buffer":
                campaign_id = item.get("campaign_id")
                player_country = item.get("player_country")
                snapshot_seq = item.get("snapshot_seq")
                observations = item.get("observations")
                if (not isinstance(campaign_id, str) or not re.fullmatch(r"[0-9a-f]{32}", campaign_id)
                        or not isinstance(player_country, str) or not REQUEST_TAG_RE.fullmatch(player_country)
                        or not isinstance(snapshot_seq, int) or isinstance(snapshot_seq, bool) or snapshot_seq < 1
                        or not isinstance(observations, list) or len(observations) > 2000):
                    raise RuntimeError("compacted state integration buffer is invalid; preserved for review")
                key = self._state_integration_key(campaign_id, player_country, snapshot_seq)
                self.state_integration_snapshot_buffers[key] = [
                    self._validate_compacted_state_row(campaign_id, player_country,
                                                       snapshot_seq, observation)
                    for observation in observations
                ]
            elif kind == "historical_focus_buffer":
                rule = item.get("rule")
                if not isinstance(rule, dict):
                    raise RuntimeError("compacted historical-focus buffer is invalid; preserved for review")
                parsed_rule = self._validate_compacted_historical_rule(rule)
                key = self._state_integration_key(parsed_rule["campaign_id"], parsed_rule["player_country"],
                                                  parsed_rule["snapshot_seq"])
                self.state_integration_game_rules_buffers[key] = parsed_rule
            else:
                raise RuntimeError("state integration journal has an unknown record; preserved for review")

    def _compact_state_integration_journal(self):
        """Replace the append journal with only current unfinished buffers."""
        path = self.state_integration_journal_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if self._state_integration_journal_has_truncated_tail:
            raise RuntimeError(f"state integration journal has an incomplete trailing record; preserved at {path}")
        temp_path = path.with_suffix(path.suffix + ".tmp")
        with temp_path.open("wb") as stream:
            for (campaign_id, player_country, snapshot_seq), observations in self.state_integration_snapshot_buffers.items():
                item = {"kind": "snapshot_buffer", "campaign_id": campaign_id,
                        "player_country": player_country, "snapshot_seq": snapshot_seq,
                        "observations": observations}
                stream.write((json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
            for rule in self.state_integration_game_rules_buffers.values():
                item = {"kind": "historical_focus_buffer", "rule": rule}
                stream.write((json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)

    def _finish_state_integration_buffer(self, key, outcome):
        """Mark a snapshot complete/discarded only after its durable queue entry exists."""
        campaign_id, player_country, snapshot_seq = key
        self._append_state_integration_journal({
            "kind": "snapshot_terminal", "campaign_id": campaign_id,
            "player_country": player_country, "snapshot_seq": snapshot_seq, "outcome": outcome,
        })
        self.state_integration_snapshot_buffers.pop(key, None)
        self.state_integration_game_rules_buffers.pop(key, None)
        self._compact_state_integration_journal()

    def recover_pending_state_integration_snapshots_from_log(self, game_log_path):
        """Complete a buffered snapshot if its end marker was logged before Bridge stopped."""
        if not (self.state_integration_snapshot_buffers or self.state_integration_game_rules_buffers):
            return []
        path = Path(game_log_path)
        if not path.is_file():
            return [{"event": "state_integration_error", "error": "game_log_unavailable",
                     "detail": "Buffered state rows were retained; no snapshot was recovered."}]
        recovered = []
        try:
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        marker = parse_state_integration_snapshot_end_line(line)
                    except ValueError:
                        continue
                    if marker is None:
                        continue
                    key = self._state_integration_key(
                        marker["campaign_id"], marker["player_country"], marker["snapshot_seq"],
                    )
                    if key not in self.state_integration_snapshot_buffers \
                            and key not in self.state_integration_game_rules_buffers:
                        continue
                    try:
                        result = self.process_line(line)
                    except (RuntimeError, ValueError) as error:
                        recovered.append({
                            "event": "state_integration_error",
                            "error": "snapshot_recovery_blocked",
                            "campaign_id": marker["campaign_id"],
                            "player_country": marker["player_country"],
                            "snapshot_seq": marker["snapshot_seq"],
                            "detail": str(error),
                        })
                        continue
                    if result is not None:
                        recovered.append(result)
        except OSError as error:
            return [{"event": "state_integration_error", "error": "game_log_unavailable",
                     "detail": str(error)}]
        return recovered

    def recover_country_selection_from_log(self, game_log_path, campaign_id):
        """Replay one unambiguous non-diplomatic country selection from an existing log."""
        if not isinstance(campaign_id, str) or not re.fullmatch(r"[0-9a-f]{32}", campaign_id):
            return {"event": "country_selection_chat_error", "error": "invalid_campaign_id"}
        path = Path(game_log_path)
        if not path.is_file():
            return {"event": "country_selection_chat_error", "error": "game_log_unavailable"}
        selections = {}
        try:
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        selection = parse_country_selection_line(line)
                    except ValueError:
                        continue
                    if selection and selection.get("campaign_id") == campaign_id:
                        pair = (selection["player_country"], selection["selected_country"])
                        selections[pair] = selection
        except OSError as error:
            return {"event": "country_selection_chat_error", "error": "game_log_unavailable",
                    "detail": str(error)}
        if not selections:
            return {"event": "country_selection_chat_error", "error": "selection_not_found",
                    "campaign_id": campaign_id}
        if len(selections) != 1:
            return {"event": "country_selection_chat_error", "error": "ambiguous_selection",
                    "campaign_id": campaign_id,
                    "detail": "The log contains multiple country pairs for this campaign; no chat was opened."}
        selection = next(iter(selections.values()))
        return open_country_selection_chat(self, selection)

    def recover_latest_country_selection_from_log(self, game_log_path):
        """Open only the last valid save-scoped country selection in an existing log."""
        path = Path(game_log_path)
        if not path.is_file():
            return {"event": "country_selection_chat_error", "error": "game_log_unavailable"}
        last_selection_line = None
        last_selection = None
        pending_fresh_campaign = None
        pair_contexts = {}
        pair_context_v2_batches = {}
        selection_markers = (
            COUNTRY_SELECTION_MARKER_V3,
            COUNTRY_SELECTION_MARKER_V2,
            COUNTRY_SELECTION_MARKER_V1,
        )
        try:
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    if "[[ Launching SINGLEPLAYER-game ]]" in line:
                        # Never resume a country selection from the previous
                        # game instance before this launch boundary.
                        last_selection_line = None
                        last_selection = None
                        pending_fresh_campaign = None
                        pair_contexts.clear()
                        pair_context_v2_batches.clear()
                        self._campaign_id_by_seed_token.clear()
                    fresh = parse_fresh_campaign_selection_line(line)
                    if fresh is not None:
                        pending_fresh_campaign = fresh
                    if any(marker in line for marker in selection_markers):
                        last_selection_line = line
                        try:
                            candidate = parse_country_selection_line(line)
                        except ValueError:
                            candidate = None
                        if candidate is not None:
                            matching_fresh = pending_fresh_campaign
                            if (matching_fresh is not None
                                    and matching_fresh["player_country"] == candidate.get("player_country")
                                    and matching_fresh["selected_country"] == candidate.get("selected_country")
                                    and candidate.get("source_campaign_token")):
                                self._resolve_campaign_payload(candidate, fresh_campaign=matching_fresh)
                                pending_fresh_campaign = None
                            else:
                                self._resolve_campaign_payload(candidate)
                            last_selection = candidate
                    context = parse_country_pair_context_line(line)
                    if context is not None:
                        self._resolve_campaign_payload(context)
                        key = (context["campaign_id"], context["sender"], context["receiver"])
                        pair_contexts[key] = context
                    try:
                        header = parse_country_pair_context_v2_line(line)
                    except ValueError:
                        header = None
                    if header is not None:
                        self._resolve_campaign_payload(header)
                        key = (header["campaign_id"], header["sender"], header["receiver"],
                               header["sequence"])
                        batch = pair_context_v2_batches.setdefault(key, {
                            "header": None, "claims": {}, "ideology_matches": {},
                            "ideology_conflicts": set(), "major_power_matches": {},
                            "major_power_conflicts": set(),
                        })
                        batch["header"] = header
                    try:
                        ideology_match = parse_country_pair_ideology_match_v1_line(line)
                    except ValueError:
                        ideology_match = None
                    if ideology_match is not None:
                        self._resolve_campaign_payload(ideology_match)
                        key = (ideology_match["campaign_id"], ideology_match["player_country"],
                               ideology_match["selected_country"], ideology_match["sequence"])
                        batch = pair_context_v2_batches.setdefault(key, {
                            "header": None, "claims": {}, "ideology_matches": {},
                            "ideology_conflicts": set(), "major_power_matches": {},
                            "major_power_conflicts": set(),
                        })
                        role, value = ideology_match["role"], ideology_match["ideology_group"]
                        previous = batch["ideology_matches"].get(role)
                        if previous is not None and previous != value:
                            batch["ideology_conflicts"].add(role)
                            batch["ideology_matches"].pop(role, None)
                        elif role not in batch["ideology_conflicts"]:
                            batch["ideology_matches"][role] = value
                    try:
                        major_match = parse_country_pair_major_power_match_v1_line(line)
                    except ValueError:
                        major_match = None
                    if major_match is not None:
                        self._resolve_campaign_payload(major_match)
                        key = (major_match["campaign_id"], major_match["player_country"],
                               major_match["selected_country"], major_match["sequence"])
                        batch = pair_context_v2_batches.setdefault(key, {
                            "header": None, "claims": {}, "ideology_matches": {},
                            "ideology_conflicts": set(), "major_power_matches": {},
                            "major_power_conflicts": set(),
                        })
                        role, value = major_match["role"], major_match["is_major"]
                        previous = batch["major_power_matches"].get(role)
                        if previous is not None and previous != value:
                            batch["major_power_conflicts"].add(role)
                            batch["major_power_matches"].pop(role, None)
                        elif role not in batch["major_power_conflicts"]:
                            batch["major_power_matches"][role] = value
                    try:
                        claim_row = parse_country_pair_claim_v1_line(line)
                    except ValueError:
                        claim_row = None
                    if claim_row is not None:
                        self._resolve_campaign_payload(claim_row)
                        key = (claim_row["campaign_id"], claim_row["player_country"],
                               claim_row["selected_country"], claim_row["sequence"])
                        batch = pair_context_v2_batches.get(key)
                        if batch is not None:
                            state_id = claim_row["claim"]["state_id"]
                            previous = batch["claims"].get(state_id)
                            if previous is None or previous == claim_row["claim"]:
                                batch["claims"][state_id] = claim_row["claim"]
                    try:
                        end = parse_country_pair_context_end_v2_line(line)
                    except ValueError:
                        end = None
                    if end is not None:
                        self._resolve_campaign_payload(end)
                        key = (end["campaign_id"], end["player_country"],
                               end["selected_country"], end["sequence"])
                        batch = pair_context_v2_batches.pop(key, None)
                        if batch is not None and len(batch["claims"]) == end["expected_claim_count"]:
                            v2_context = batch["header"]
                            if not isinstance(v2_context, dict):
                                continue
                            v2_context["game_context"]["claims"] = [
                                batch["claims"][state_id] for state_id in sorted(batch["claims"])
                            ]
                            ideology_matches = batch.get("ideology_matches", {})
                            ideology_conflicts = batch.get("ideology_conflicts", set())
                            if "player" in ideology_conflicts:
                                v2_context["game_context"]["player_ideology_group"] = "unknown"
                            elif ideology_matches.get("player"):
                                v2_context["game_context"]["player_ideology_group"] = ideology_matches["player"]
                            if "selected" in ideology_conflicts:
                                v2_context["game_context"]["ideology_group"] = "unknown"
                            elif ideology_matches.get("selected"):
                                v2_context["game_context"]["ideology_group"] = ideology_matches["selected"]
                            major_matches = batch.get("major_power_matches", {})
                            major_conflicts = batch.get("major_power_conflicts", set())
                            v2_context["game_context"]["major_power"] = (
                                major_matches.get("selected", "unknown")
                                if "selected" not in major_conflicts else "unknown"
                            )
                            v2_context["game_context"]["player_major_power"] = (
                                major_matches.get("player", "unknown")
                                if "player" not in major_conflicts else "unknown"
                            )
                            v2_context["game_context_schema_version"] = 9
                            v2_context["source_marker"] = "EYES_HOI4_PAIR_CONTEXT_V2"
                            pair_contexts[(end["campaign_id"], end["player_country"],
                                           end["selected_country"])] = v2_context
        except OSError as error:
            return {"event": "country_selection_chat_error", "error": "game_log_unavailable",
                    "detail": str(error)}
        if last_selection_line is None:
            return {"event": "country_selection_chat_error", "error": "selection_not_found"}
        selection = last_selection
        if not isinstance(selection, dict) or not selection.get("campaign_id"):
            return {"event": "country_selection_chat_error", "error": "invalid_latest_selection",
                    "detail": "The last country selection has no valid campaign identity; no chat was opened."}
        result = open_country_selection_chat(self, selection)
        if result.get("event") == "country_selection_chat_ready":
            result["recovered_from_existing_game_log"] = True
            key = (selection["campaign_id"], selection["player_country"], selection["selected_country"])
            context_request = pair_contexts.get(key)
            if context_request is not None:
                persisted = self._persist_v5_game_context(context_request)
                if persisted.get("error"):
                    result["pair_context_recovery_error"] = persisted
                else:
                    result["pair_context_recovered"] = True
                    result["pair_context_request_id"] = context_request["request_id"]
        return result

    def replay_state_integration_snapshot_from_log(self, game_log_path, campaign_id, snapshot_seq):
        """Replay only one explicit, complete, read-only integration snapshot from game.log."""
        if not isinstance(campaign_id, str) or not re.fullmatch(r"[0-9a-f]{32}", campaign_id):
            return {"event": "state_integration_error", "error": "invalid_campaign_id"}
        if (not isinstance(snapshot_seq, int) or isinstance(snapshot_seq, bool)
                or snapshot_seq < 1 or snapshot_seq > 10_000_000_000):
            return {"event": "state_integration_error", "error": "invalid_snapshot_sequence"}
        path = Path(game_log_path)
        if not path.is_file():
            return {"event": "state_integration_error", "error": "game_log_unavailable"}
        counts = {"state": 0, "rules": 0, "end": 0}
        result = None
        try:
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        if STATE_INTEGRATION_MARKER_V1 in line:
                            parsed = parse_state_integration_line(line)
                            sequence = int(parsed["request_id"].split(":")[2])
                            if parsed["campaign_id"] == campaign_id and sequence == snapshot_seq:
                                counts["state"] += 1
                                result = self.process_line(line)
                        elif HISTORICAL_FOCUSES_MARKER_V1 in line:
                            parsed = parse_historical_focuses_line(line)
                            if parsed["campaign_id"] == campaign_id and parsed["snapshot_seq"] == snapshot_seq:
                                counts["rules"] += 1
                                result = self.process_line(line)
                        elif STATE_INTEGRATION_SNAPSHOT_END_V1 in line:
                            parsed = parse_state_integration_snapshot_end_line(line)
                            if parsed["campaign_id"] == campaign_id and parsed["snapshot_seq"] == snapshot_seq:
                                counts["end"] += 1
                                result = self.process_line(line)
                    except (RuntimeError, ValueError) as error:
                        return {"event": "state_integration_error", "error": "snapshot_replay_parse",
                                "campaign_id": campaign_id, "snapshot_seq": snapshot_seq,
                                "detail": str(error)}
        except OSError as error:
            return {"event": "state_integration_error", "error": "game_log_unavailable",
                    "detail": str(error)}
        if counts["end"] != 1 or counts["rules"] != 1:
            return {"event": "state_integration_error", "error": "snapshot_replay_incomplete",
                    "campaign_id": campaign_id, "snapshot_seq": snapshot_seq, "marker_counts": counts}
        if result is None or result.get("event") != "state_integration_snapshot_recorded":
            return (result if isinstance(result, dict) else {
                "event": "state_integration_error", "error": "snapshot_replay_not_recorded",
                "campaign_id": campaign_id, "snapshot_seq": snapshot_seq,
                "marker_counts": counts,
            })
        return {**result, "recovered_from_existing_game_log": True, "marker_counts": counts}

    def _persist_pending_state_integration_observations(self):
        """Atomically retain network-unacknowledged markers across Bridge restarts."""
        path = self.state_integration_queue_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        rows = ([{"kind": "observation", "payload": item}
                 for item in self.pending_state_integration_observations]
                + [{"kind": "snapshot", "payload": item}
                   for item in self.pending_state_integration_snapshots])
        temp_path.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(path)

    def _queue_pending_state_integration_snapshot(self, payload):
        key = self._state_integration_snapshot_key(payload)
        existing = next((item for item in self.pending_state_integration_snapshots
                         if self._state_integration_snapshot_key(item) == key), None)
        if existing is None:
            self.pending_state_integration_snapshots.append(payload)
            self._persist_pending_state_integration_observations()
        return None

    def _submit_state_integration_observation(self, payload, *, queue_on_network=True):
        record = getattr(self.provider, "record_state_integration_observation", None)
        if not callable(record):
            return {"error": "schema", "detail": "Flask provider cannot persist state integration observations."}
        acknowledgement = record(payload)
        if acknowledgement.get("error") == "network" and queue_on_network:
            if all(item.get("request_id") != payload.get("request_id")
                   for item in self.pending_state_integration_observations):
                self.pending_state_integration_observations.append(payload)
            self._persist_pending_state_integration_observations()
            self._state_integration_retry_after = time.monotonic() + self.territory_result_retry_interval
            return {"event": "state_integration_pending", "request_id": payload["request_id"]}
        if acknowledgement.get("error") or acknowledgement.get("recorded") is not True:
            return acknowledgement if acknowledgement.get("error") else {
                "error": "schema", "detail": "Flask did not confirm durable state integration storage.",
            }
        # Flask may have committed before its response was lost. Same request_id is
        # safe to replay because the API enforces idempotency on the full observation.
        self.pending_state_integration_observations = [
            item for item in self.pending_state_integration_observations
            if item.get("request_id") != payload.get("request_id")
        ]
        self._persist_pending_state_integration_observations()
        return {
            "event": "state_integration_recorded", "campaign_id": payload["campaign_id"],
            "request_id": payload["request_id"], "state_id": payload["context"]["state_id"],
            "idempotent_replay": acknowledgement.get("observation", {}).get("idempotent_replay", False),
        }

    def _submit_state_integration_snapshot(self, payload, *, queue_on_network=True):
        record = getattr(self.provider, "record_state_integration_snapshot", None)
        if not callable(record):
            return {"event": "state_integration_error", "error": "schema",
                    "detail": "Flask provider cannot persist complete state snapshots."}
        conflict = self._queue_pending_state_integration_snapshot(payload)
        if conflict is not None:
            return conflict
        acknowledgement = record(payload)
        snapshot_key = self._state_integration_snapshot_key(payload)
        if acknowledgement.get("error") == "network" and queue_on_network:
            self._state_integration_retry_after = time.monotonic() + self.territory_result_retry_interval
            return {"event": "state_integration_pending", "snapshot_id": snapshot_key}
        if acknowledgement.get("error") or acknowledgement.get("recorded") is not True:
            self._state_integration_retry_after = time.monotonic() + self.territory_result_retry_interval
            return {"event": "state_integration_error", **acknowledgement,
                    "snapshot_id": snapshot_key}
        self.pending_state_integration_snapshots = [
            item for item in self.pending_state_integration_snapshots
            if self._state_integration_snapshot_key(item) != snapshot_key
        ]
        self._persist_pending_state_integration_observations()
        return {
            "event": "state_integration_snapshot_recorded",
            "campaign_id": payload["campaign_id"], "snapshot_id": acknowledgement.get("snapshot_id"),
            "snapshot_seq": payload["snapshot_seq"],
            "state_count": len(payload["observations"]),
            "idempotent_replay": acknowledgement.get("snapshot", {}).get("idempotent_replay", False),
        }

    def retry_pending_autonomy_snapshots(self):
        if not self.autonomy_pending or time.monotonic() < self._autonomy_retry_after:
            return []
        self._autonomy_retry_after = time.monotonic() + 2.0
        row = self.autonomy_pending[0]
        result = self.provider.deliver_autonomy_snapshot(row['campaign_id'], row['snapshot'])
        if result.get('error'):
            if result['error'] not in {'network', 'server_busy', 'state_persistence'}:
                # Preserve rejected evidence separately; one invalid/old
                # snapshot must not block fresh campaigns forever.
                rejected_path = self.autonomy_pending_path.with_suffix('.rejected.json')
                rejected = json.loads(rejected_path.read_text(encoding='utf-8')) if rejected_path.exists() else []
                if not isinstance(rejected, list) or len(rejected) >= 16:
                    return [{'event': 'autonomy_snapshot_pending', 'error': 'rejection_journal_full'}]
                write_response(rejected_path, [*rejected, {'payload': row, 'result': result}])
                self.autonomy_pending.pop(0)
                write_response(self.autonomy_pending_path, self.autonomy_pending)
                return [{'event': 'autonomy_snapshot_rejected', **result}]
            return [{'event': 'autonomy_snapshot_pending', **result}]
        self.autonomy_pending.pop(0)
        write_response(self.autonomy_pending_path, self.autonomy_pending)
        return [{'event': 'autonomy_snapshot_recorded', **result}]

    def retry_pending_state_integration_observations(self):
        self.retry_pending_autonomy_snapshots()
        treaty_results = self.treaty_observations.retry(self.provider)
        now = time.monotonic()
        if (not self.pending_state_integration_observations and not self.pending_state_integration_snapshots
                or now < self._state_integration_retry_after):
            return treaty_results
        completed = treaty_results
        for payload in list(self.pending_state_integration_observations):
            result = self._submit_state_integration_observation(payload, queue_on_network=False)
            if not result.get("error"):
                self.pending_state_integration_observations = [
                    item for item in self.pending_state_integration_observations
                    if item.get("request_id") != payload.get("request_id")
                ]
                completed.append(result)
                self._persist_pending_state_integration_observations()
        for payload in list(self.pending_state_integration_snapshots):
            result = self._submit_state_integration_snapshot(payload, queue_on_network=False)
            if result.get("event") == "state_integration_snapshot_recorded":
                self.pending_state_integration_snapshots = [
                    item for item in self.pending_state_integration_snapshots
                    if self._state_integration_snapshot_key(item)
                    != self._state_integration_snapshot_key(payload)
                ]
                completed.append(result)
                self._persist_pending_state_integration_observations()
        self._state_integration_retry_after = (
            time.monotonic() + self.territory_result_retry_interval
            if self.pending_state_integration_observations or self.pending_state_integration_snapshots else 0.0
        )
        return completed

    def open_or_create_chat(self, selection: Dict[str, str]) -> Dict[str, Any]:
        """Reuse the open country-pair chat for this campaign or create it via Flask."""
        with self._chat_open_lock:
            return self._open_or_create_chat(selection)

    def _open_or_create_chat(self, selection: Dict[str, str]) -> Dict[str, Any]:
        player = selection.get("player_country")
        selected = selection.get("selected_country")
        campaign_id = selection.get("campaign_id")
        if (not isinstance(player, str) or not REQUEST_TAG_RE.fullmatch(player)
                or not isinstance(selected, str) or not REQUEST_TAG_RE.fullmatch(selected)
                or player == selected or not isinstance(campaign_id, str)
                or not re.fullmatch(r"[0-9a-f]{32}", campaign_id)):
            return {"error": "request", "detail": "Selection lacks a valid campaign_id and country pair."}

        listing = self.provider.list_chats(campaign_id)
        if listing.get("error"):
            return listing
        selected_save = listing.get("active_save_context")
        matches = [chat for chat in listing["chats"]
                   if chat.get("campaign_id") == campaign_id
                   and chat.get("player_country") == player
                   and set(chat.get("participants", [])) == {player, selected}
                   and chat.get("state") != "closed"
                   and (selected_save is None or chat.get("save_scope_id") == selected_save)]
        if matches:
            chosen = max(matches, key=lambda item: item.get("updated_at", ""))
            chat = self.provider.get_chat(chosen["chat_id"])
        else:
            chat = self.provider.create_chat(campaign_id, player, selected)
        if chat.get("error"):
            return chat
        if (chat.get("campaign_id") != campaign_id or chat.get("player_country") != player
                or set(chat.get("participants", [])) != {player, selected}
                or not isinstance(chat.get("conversation_id"), str)):
            return {"error": "schema", "detail": "Flask returned a chat for a different campaign or participants."}
        return chat

    def _persist_v5_game_context(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Attach versioned game-log facts to the matching persistent chat."""
        campaign_id = request.get("campaign_id")
        context = request.get("game_context")
        if not isinstance(campaign_id, str) or not isinstance(context, dict):
            return {"error": "request", "detail": "Versioned marker is missing campaign context."}
        schema_version = request.get("game_context_schema_version", 5)
        if schema_version not in {5, 6, 7, 8, 9}:
            return {"error": "request", "detail": "Unsupported game-context schema version."}
        chat = self.open_or_create_chat({
            "player_country": request.get("sender"),
            "selected_country": request.get("receiver"),
            "campaign_id": campaign_id,
        })
        if chat.get("error"):
            return chat
        observation_payload = {
            "campaign_id": campaign_id, "request_id": request["request_id"],
            "country": request["receiver"], "context": context,
            "schema_version": schema_version,
        }
        record = getattr(self.provider, "record_game_observation", None)
        if callable(record):
            persisted = record(chat["chat_id"], observation_payload)
            if persisted.get("error") == "not_found":
                # A pre-provenance Flask can still retain the context, but it
                # cannot provide the auditable observation record.
                persisted = None
            elif isinstance(persisted, dict) and persisted.get("error"):
                return persisted
            elif isinstance(persisted, dict):
                observation = persisted.get("observation", {})
                if (persisted.get("campaign_id") != campaign_id
                        or persisted.get("country") != request["receiver"]
                        or persisted.get("request_id") != request["request_id"]
                        or observation.get("source") != f"bridge_parsed_hoi4_game_log_v{schema_version}"
                        or observation.get("context") != context):
                    return {"error": "schema", "detail": "Flask stored a different game observation."}
                return {"chat_id": chat["chat_id"], "campaign_id": campaign_id,
                        "country": request["receiver"], "request_id": request["request_id"],
                        "game_context": context, "observation_persisted": True}

        update = getattr(self.provider, "update_chat_context", None)
        if not callable(update):
            return {"error": "schema", "detail": "Flask provider cannot persist game context."}
        legacy_persisted = update(chat["chat_id"], request["receiver"], context)
        if legacy_persisted.get("error"):
            return legacy_persisted
        if (legacy_persisted.get("chat_id") != chat["chat_id"]
                or legacy_persisted.get("campaign_id") != campaign_id
                or request["receiver"] not in legacy_persisted.get("participants", [])):
            return {"error": "schema", "detail": "Flask persisted game context to a different chat."}
        stored = legacy_persisted.get("country_contexts", {}).get(request["receiver"])
        if stored != context:
            return {"error": "schema", "detail": "Flask did not retain the game facts."}
        return {"chat_id": chat["chat_id"], "campaign_id": campaign_id,
                "country": request["receiver"], "request_id": request["request_id"],
                "game_context": stored, "observation_persisted": False}

    supports_chat_progress = True

    def send_chat_message(self, chat_id: str, message: str, on_progress=None) -> Dict[str, Any]:
        """Use the persistent chat's request sequence and existing idempotent endpoints."""
        chat = self.provider.get_chat(chat_id)
        if chat.get("error"):
            return chat
        request_id = chat.get("next_request_id")
        if not isinstance(request_id, str) or chat.get("state") != "awaiting_player":
            return {"error": "conflict", "detail": "Chat is not ready for a player message."}
        posted = self.provider.post_chat_message(chat_id, message, request_id)
        if posted.get("error") == "network":
            # Retry only with the same idempotency key if Flask committed before a timeout.
            posted = self.provider.post_chat_message(chat_id, message, request_id)
        if posted.get("error"):
            return posted

        chat = self.provider.get_chat(chat_id)
        if chat.get("error"):
            return chat
        for _ in range(6):
            if chat.get("state") != "awaiting_ai":
                return chat
            advance_id = chat.get("next_request_id")
            if not isinstance(advance_id, str):
                return {"error": "schema", "detail": "Chat is missing the next request_id."}
            advanced = self.provider.advance_chat(chat_id, advance_id)
            if advanced.get("error") == "network":
                advanced = self.provider.advance_chat(chat_id, advance_id)
            if advanced.get("error"):
                return advanced
            chat = advanced.get("chat")
            if isinstance(chat,dict) and callable(on_progress):on_progress(chat)
            if not isinstance(chat, dict):
                return {"error": "schema", "detail": "Chat advance response is invalid."}
        if chat.get("state") == "awaiting_ai":
            return {"error": "conflict", "detail": "Chat response queue exceeded the participant limit."}
        return chat

    def retry_chat_response(self, chat_id: str, response_request_id: str,
                            attempt_id: Optional[str] = None) -> Dict[str, Any]:
        """Retry one failed Ollama turn without posting another player message."""
        attempt_id = attempt_id or uuid.uuid4().hex
        result = self.provider.retry_chat_response(chat_id, response_request_id, attempt_id)
        if result.get("error") == "network":
            # The server caches attempt IDs, so a lost response is safe to retry.
            result = self.provider.retry_chat_response(chat_id, response_request_id, attempt_id)
        if result.get("error"):
            return result
        chat = result.get("chat")
        if (not isinstance(chat, dict) or chat.get("chat_id") != chat_id
                or result.get("request_id") != response_request_id
                or result.get("attempt_id") != attempt_id):
            return {"error": "schema", "detail": "Flask returned a mismatched diplomatic retry result."}
        return chat

    def _submit_territory_result(self, payload):
        # Persist before POST: Flask may commit and then lose its response, or
        # Bridge may be restarted while waiting. The API treats this key idempotently.
        self._queue_pending_execution_item(self.pending_territory_results, payload)
        acknowledgement = self.provider.send_territory_result(payload)
        if acknowledgement.get("error") or acknowledgement.get("recorded") is not True:
            if acknowledgement.get("error") == "network":
                self._territory_retry_after = time.monotonic() + self.territory_result_retry_interval
            else:
                self._remove_pending_execution_item(self.pending_territory_results, payload)
            print("Territory result marker not recorded: Flask did not confirm the stored plan", file=sys.stderr)
            return None
        self._remove_pending_execution_item(self.pending_territory_results, payload)
        result = {
            "source": "hoi4_game_log", "campaign_id": payload["campaign_id"],
            "proposal_id": payload["proposal_id"], "plan_id": payload["plan_id"],
            "action_id": "territory_partition", "execution_status": (
                {"applied": "EXECUTION_CONFIRMED", "failed": "EXECUTION_FAILED",
                 "rolled_back": "EXECUTION_ROLLED_BACK",
                 "rollback_failed": "ROLLBACK_FAILED"}[payload["status"]]
            ), "status": payload["status"], "duplicate": acknowledgement.get("duplicate", False),
            "confirmation": TERRITORY_RESULT_MARKER_V1,
        }
        write_response(self.response_path, result)
        return result

    def retry_pending_territory_results(self):
        now = time.monotonic()
        if not self.pending_territory_results or now < self._territory_retry_after:
            return []
        confirmed = []
        for payload in list(self.pending_territory_results):
            result = self._submit_territory_result(payload)
            if result is not None:
                confirmed.append(result)
        if self.pending_territory_results:
            self._territory_retry_after = time.monotonic() + self.territory_result_retry_interval
        else:
            self._territory_retry_after = 0.0
        return confirmed

    def _record_agreement_marker(self, fields, status):
        expected_count = 4 if status == "applied" else 5
        if len(fields) != expected_count:
            return None
        sender, receiver, request_id, action_id = fields[:4]
        reason = fields[4] if status == "failed" else None
        if (not REQUEST_TAG_RE.fullmatch(sender) or not REQUEST_TAG_RE.fullmatch(receiver)
                or action_id not in ALLOWED_EFFECTS
                or not request_id.startswith("agreement:")):
            return None
        lookup = getattr(self.provider, "get_pending_agreement_execution", None)
        submit = getattr(self.provider, "send_agreement_execution_result", None)
        if not callable(lookup) or not callable(submit):
            return None
        raw_marker = "|".join(fields)
        queued_marker = (status, raw_marker)
        # A V2 marker is the only durable evidence of an in-game effect. Save it
        # before looking up the prepared action because startup tails from EOF.
        self._queue_pending_execution_item(self.pending_agreement_markers, queued_marker)
        pending = lookup(request_id)
        if pending.get("error") == "network":
            self._agreement_retry_after = time.monotonic() + self.territory_result_retry_interval
            return None
        if (pending.get("error") or pending.get("action_id") != action_id
                or pending.get("execution_request_id") != request_id
                or set(pending.get("participants", [])) != {sender, receiver}):
            self._remove_pending_execution_item(self.pending_agreement_markers, queued_marker)
            return None
        package = pending.get("execution_package") or {}
        marker_key = "result_marker" if status == "applied" else "failure_marker"
        from execution_markers import matches_prepared_result
        actual_marker = (APPLIED_MARKER_V2 if status == "applied" else FAILED_MARKER_V2) + "|".join(fields)
        if not matches_prepared_result(package, status, actual_marker):
            self._remove_pending_execution_item(self.pending_agreement_markers, queued_marker)
            return None
        # The experimental adapter only reconciles already-staged, manually
        # dispatched entries. It never sends input and a V2 marker alone does
        # not mark the effect state-verified.
        campaign_id = pending.get("campaign_id")
        if isinstance(campaign_id, str):
            try:
                self.windows_effect_adapter.record_marker(
                    campaign_id, request_id, action_id, actual_marker,
                )
            except AdapterError as error:
                # The ledger refusing the marker must not block the agreement
                # update, but it must leave evidence: a silent divergence
                # between game.log and the effect queue is undiagnosable later.
                log_bridge_ui_event(
                    "adapter_marker_rejected", campaign_id=campaign_id,
                    request_id=request_id, action_id=action_id,
                    marker=actual_marker, reason=str(error),
                )
        payload = {
            "chat_id": pending["chat_id"], "agreement_id": pending["agreement_id"],
            "request_id": request_id, "action_id": action_id, "status": status,
            "marker": actual_marker, "reason": reason,
        }
        # _submit_agreement_result persists the validated payload before removing
        # the raw marker, closing the crash window between lookup and POST.
        result = self._submit_agreement_result(payload)
        self._remove_pending_execution_item(self.pending_agreement_markers, queued_marker)
        return result

    def _submit_agreement_result(self, payload):
        submit = getattr(self.provider, "send_agreement_execution_result", None)
        if not callable(submit):
            return None
        self._queue_pending_execution_item(self.pending_agreement_results, payload)
        acknowledgement = submit(payload)
        if acknowledgement.get("error") or acknowledgement.get("recorded") is not True:
            if acknowledgement.get("error") == "network":
                self._agreement_retry_after = time.monotonic() + self.territory_result_retry_interval
            else:
                self._remove_pending_execution_item(self.pending_agreement_results, payload)
            return None
        self._remove_pending_execution_item(self.pending_agreement_results, payload)
        execution_status = "EXECUTION_CONFIRMED" if payload["status"] == "applied" else "EXECUTION_FAILED"
        result = {
            "source": "hoi4_game_log", "agreement_id": payload["agreement_id"],
            "request_id": payload["request_id"], "action_id": payload["action_id"],
            "execution_status": execution_status, "confirmation": payload["marker"],
            "duplicate": acknowledgement.get("duplicate", False),
        }
        write_response(self.response_path, result)
        return result

    def retry_pending_agreement_results(self):
        now = time.monotonic()
        if now < self._agreement_retry_after:
            return []
        completed = []
        for status, marker in list(self.pending_agreement_markers):
            fields = marker.split("|")
            result = self._record_agreement_marker(fields, status)
            if result is not None:
                completed.append(result)
        for payload in list(self.pending_agreement_results):
            result = self._submit_agreement_result(payload)
            if result is not None:
                completed.append(result)
        for payload in list(self.pending_diplomatic_refusal_results):
            result = self._submit_diplomatic_refusal_result(payload)
            if result is not None:
                completed.append(result)
        for payload in list(self.pending_state_operation_results):
            result = self._submit_state_operation_result(payload)
            if result is not None:
                completed.append(result)
        if (self.pending_agreement_markers or self.pending_agreement_results
                or self.pending_diplomatic_refusal_results or self.pending_state_operation_results):
            self._agreement_retry_after = time.monotonic() + self.territory_result_retry_interval
        else:
            self._agreement_retry_after = 0.0
        return completed

    def _submit_state_operation_result(self, payload):
        self._queue_pending_execution_item(self.pending_state_operation_results, payload)
        reply = self.provider._chat_api('POST','/world/state-operation-result',payload,timeout=5)
        if reply.get('recorded') is True:
            self._remove_pending_execution_item(self.pending_state_operation_results, payload)
            return {'event':'state_operation_result','result':reply}
        if reply.get('error') == 'network':
            self._agreement_retry_after = time.monotonic() + self.territory_result_retry_interval
        else:
            self._remove_pending_execution_item(self.pending_state_operation_results, payload)
            return {'event':'state_operation_error','error':reply.get('error') or 'result_not_acknowledged'}
        return None

    def _submit_diplomatic_refusal_result(self, payload):
        submit = getattr(self.provider, "send_diplomatic_refusal_result", None)
        if not callable(submit):
            return None
        self._queue_pending_execution_item(self.pending_diplomatic_refusal_results, payload)
        acknowledgement = submit(payload)
        if acknowledgement.get("error") or acknowledgement.get("recorded") is not True:
            if acknowledgement.get("error") == "network":
                self._agreement_retry_after = time.monotonic() + self.territory_result_retry_interval
            else:
                self._remove_pending_execution_item(self.pending_diplomatic_refusal_results, payload)
            return None
        self._remove_pending_execution_item(self.pending_diplomatic_refusal_results, payload)
        result = {
            "source": "hoi4_game_log", "campaign_id": payload["campaign_id"],
            "chat_id": payload["chat_id"], "request_id": payload["refusal_request_id"],
            "status": acknowledgement.get("status", "news_published"),
            "duplicate": acknowledgement.get("duplicate", False),
            "confirmation": "EYES_HOI4_DIPLOMATIC_REFUSAL_V1",
        }
        write_response(self.response_path, result)
        return result

    def _record_diplomatic_refusal_marker(self, fields):
        if len(fields) != 8:
            return None
        campaign_id, chat_id, player, target, source_request_id, refusal_request_id, action_id = fields[1:]
        if (not re.fullmatch(r"[0-9a-f]{32}", campaign_id)
                or not re.fullmatch(r"[0-9a-f]{32}", chat_id)
                or not REQUEST_TAG_RE.fullmatch(player) or not REQUEST_TAG_RE.fullmatch(target)
                or player == target
                or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", source_request_id)
                or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", refusal_request_id)
                or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", action_id)):
            return None
        marker = "|".join(fields)
        payload = {"campaign_id": campaign_id, "chat_id": chat_id,
                   "refusal_request_id": refusal_request_id, "marker": marker}
        return self._submit_diplomatic_refusal_result(payload)

    def process_line(self, line: str) -> Optional[Dict[str, Any]]:
        if 'EYES_LIVE_QA_V1|' in line:
            from developer_access import enabled
            if not enabled():
                return None
            from live_qa import LiveQA
            try:
                if LiveQA().record_bridge(line):
                    return {'event':'native_qa_marker'}
            except OSError:
                return {'event':'native_qa_bridge_write_failed'}
            return None
        from state_operations import MARKER as STATE_OPERATION_MARKER, decode_result
        if STATE_OPERATION_MARKER in line:
            try:
                marker = line.split(STATE_OPERATION_MARKER, 1)[1].strip()
                result = self._resolve_campaign_payload(decode_result(marker))
                return self._submit_state_operation_result({'marker':marker,'campaign':result['campaign_id']})
            except (ValueError, TypeError, KeyError) as error:
                return {'event':'state_operation_error','error':str(error)}
        maker_marker = 'EYES_COUNTRY_MAKER_V1|'
        if maker_marker in line:
            try:
                identifier, token, actor, status, tag, day, date = line.split(maker_marker, 1)[1].strip().split('|')
                from hoi4_campaign_identity import campaign_id_from_save_token
                campaign_id_from_save_token(token)
                result = {'campaign_token': token, 'id': identifier, 'actor': actor,
                          'status': status, 'new_tag': tag, 'day': int(day), 'date': date.strip()}
                self._resolve_campaign_payload(result)
                result['campaign'] = result.pop('campaign_id')
                return {'event': 'country_maker_result', 'result': self.provider._chat_api(
                    'POST', '/world/country-result', result, timeout=5)}
            except (ValueError, TypeError, KeyError) as error:
                return {'event': 'country_maker_error', 'error': str(error)}
        from world_snapshot import MARKER as WORLD_STATE_MARKER, Collector as WorldCollector, recent_native_line
        if WORLD_STATE_MARKER in line:
            try:
                if not hasattr(self, '_world_collector'): self._world_collector = WorldCollector()
                completed = self._world_collector.feed(line)
                if completed is None: return None
                self._resolve_campaign_payload(completed)
                payload = {'campaign': completed['campaign_id'], 'snapshot': completed['snapshot'],
                           'save_token': completed['campaign_token']}
                if recent_native_line(line):
                    game = get_hoi4_window_state()
                    if (game and game.get('running') and game.get('pid')
                            and len(game.get('pids', ()))<=1):
                        payload['live_game_pid'] = game['pid']
                        payload['save_token'] = completed['campaign_token']
                        observation = (game['pid'], completed['campaign_id'], completed['sequence'], completed['snapshot']['day'])
                        previous = getattr(self, '_world_session_observation', None)
                        if (previous is None or observation[:2] != previous[:2]
                                or observation[2] <= previous[2] or observation[3] < previous[3]):
                            self._world_session_id = uuid.uuid4().hex
                        self._world_session_observation = observation
                        payload['session_id'] = self._world_session_id
                        if getattr(self,'game_log_path',None):
                            from world_freshness import capture
                            payload['live_proof'] = capture(self.game_log_path,line,game['pid'])
                result = self.provider._chat_api('POST', '/world/snapshots', payload, timeout=5)
                if result.get('error'):
                    print('Atlas snapshot:', result.get('detail', result['error']), flush=True)
                return {'event': 'world_snapshot_result', 'result': result}
            except (ValueError, TypeError, KeyError) as error:
                return {'event': 'world_snapshot_error', 'error': str(error)}
        from native_leadership import MARKER as LEADER_MARKER, parse as parse_leader
        if LEADER_MARKER in line:
            try:
                row=parse_leader(line);self._resolve_campaign_payload(row)
                result=self.provider.record_native_leader(row.pop('campaign_id'),row)
                return {'event':'native_leader_observation','result':result}
            except (ValueError,TypeError,KeyError) as error:
                return {'event':'native_leader_error','error':str(error)}
        from treaty_monitor import MARKER as TREATY_MARKER, parse_marker
        if TREATY_MARKER in line:
            try:
                observation = parse_marker(line)
                self._resolve_campaign_payload(observation)
                observation.pop('campaign_token')
                self.treaty_observations.enqueue(observation)
                result = self.treaty_observations.retry(self.provider)
                return result[-1] if result else {'event': 'treaty_observation_pending'}
            except (ValueError, TypeError, KeyError) as error:
                return {'event': 'treaty_observation_error', 'error': str(error)}
        from autonomy_game_transport import MARKER, WORLD_MARKER
        if MARKER in line or WORLD_MARKER in line:
            try:
                completed = self.autonomy_collector.feed(line)
                if completed is None:
                    return None
                self._resolve_campaign_payload(completed)
                row = {'campaign_id': completed['campaign_id'], 'snapshot': completed['snapshot']}
                if row not in self.autonomy_pending:
                    if len(self.autonomy_pending) >= 16:
                        return {'event': 'autonomy_snapshot_error', 'error': 'pending_queue_full'}
                    self.autonomy_pending.append(row)
                    write_response(self.autonomy_pending_path, self.autonomy_pending)
                result = self.retry_pending_autonomy_snapshots()
                return result[-1] if result else {'event': 'autonomy_snapshot_pending'}
            except (ValueError, TypeError, KeyError) as error:
                return {'event': 'autonomy_snapshot_error', 'error': str(error)}
        fresh_campaign = parse_fresh_campaign_selection_line(line)
        if fresh_campaign is not None:
            # The mod emits this immediately before its first selection marker
            # in a new save, before it creates any EYES campaign seed values.
            self._pending_fresh_campaign = fresh_campaign
            return None
        annexation_proposal = parse_annexation_proposal_line(line)
        if annexation_proposal is not None:
            self._resolve_campaign_payload(annexation_proposal)
            return {"source": "hoi4_game_log", **annexation_proposal}
        if DIPLOMATIC_REFUSAL_MARKER_V1 in line:
            fields = ["EYES_HOI4_DIPLOMATIC_REFUSAL_V1"] + line.split(
                DIPLOMATIC_REFUSAL_MARKER_V1, 1
            )[1].rstrip("\r\n").split("|")
            return self._record_diplomatic_refusal_marker(fields)
        try:
            pair_context_v2 = parse_country_pair_context_v2_line(line)
        except ValueError as error:
            if COUNTRY_PAIR_CONTEXT_MARKER_V2 in line:
                return {"source": "hoi4_game_log", "event": "country_pair_context_error",
                        "error": "invalid_v2_header", "detail": str(error)}
            pair_context_v2 = None
        if pair_context_v2 is not None:
            self._resolve_campaign_payload(pair_context_v2)
            now = time.monotonic()
            self.country_pair_context_buffers = {
                key: value for key, value in self.country_pair_context_buffers.items()
                if now - value.get("started_at", now) <= 600.0
            }
            if len(self.country_pair_context_buffers) >= 64:
                oldest = min(self.country_pair_context_buffers,
                             key=lambda item: self.country_pair_context_buffers[item].get("started_at", now))
                self.country_pair_context_buffers.pop(oldest, None)
            key = (pair_context_v2["campaign_id"], pair_context_v2["sender"],
                   pair_context_v2["receiver"], pair_context_v2["sequence"])
            existing = self.country_pair_context_buffers.get(key)
            if existing is not None and existing.get("header") not in (None, pair_context_v2):
                return {"source": "hoi4_game_log", "event": "country_pair_context_error",
                        "error": "conflicting_v2_header", "request_id": pair_context_v2["request_id"]}
            if existing is None:
                self.country_pair_context_buffers[key] = {
                    "header": pair_context_v2, "claims": {}, "ideology_matches": {},
                    "ideology_conflicts": set(), "major_power_matches": {},
                    "major_power_conflicts": set(),
                    "started_at": time.monotonic(),
                }
            else:
                # HOI4 may emit scoped branch markers before the V2 summary.
                # Attach the header to that uncommitted evidence batch instead
                # of discarding the already-validated participant/sequence data.
                existing["header"] = pair_context_v2
            # Headers and claim rows are read-only evidence. Persist only after
            # the matching end marker confirms a complete batch.
            return None

        try:
            ideology_match = parse_country_pair_ideology_match_v1_line(line)
        except ValueError as error:
            if COUNTRY_PAIR_IDEOLOGY_MATCH_MARKER_V1 in line:
                return {"source": "hoi4_game_log", "event": "country_pair_context_error",
                        "error": "invalid_ideology_match", "detail": str(error)}
            ideology_match = None
        if ideology_match is not None:
            self._resolve_campaign_payload(ideology_match)
            key = (ideology_match["campaign_id"], ideology_match["player_country"],
                   ideology_match["selected_country"], ideology_match["sequence"])
            batch = self.country_pair_context_buffers.get(key)
            if batch is None:
                # The mod emits these actual-scope matches before its V2
                # summary. Stage them by the fully validated campaign/pair/
                # sequence key; they are not persisted until the matching end.
                now = time.monotonic()
                self.country_pair_context_buffers = {
                    buffered_key: value for buffered_key, value in self.country_pair_context_buffers.items()
                    if now - value.get("started_at", now) <= 600.0
                }
                if len(self.country_pair_context_buffers) >= 64:
                    oldest = min(self.country_pair_context_buffers,
                                 key=lambda item: self.country_pair_context_buffers[item].get("started_at", now))
                    self.country_pair_context_buffers.pop(oldest, None)
                batch = {
                    "header": None, "claims": {}, "ideology_matches": {},
                    "ideology_conflicts": set(), "major_power_matches": {},
                    "major_power_conflicts": set(), "started_at": now,
                }
                self.country_pair_context_buffers[key] = batch
            if batch.get("header") is not None and (
                    ideology_match["player_country"] != batch["header"]["sender"]
                    or ideology_match["selected_country"] != batch["header"]["receiver"]):
                return {"source": "hoi4_game_log", "event": "country_pair_context_error",
                        "error": "ideology_match_participant_mismatch"}
            matches = batch["ideology_matches"]
            role = ideology_match["role"]
            previous = matches.get(role)
            if previous is not None and previous != ideology_match["ideology_group"]:
                batch["ideology_conflicts"].add(role)
                matches.pop(role, None)
                return {"source": "hoi4_game_log", "event": "country_pair_context_error",
                        "error": "conflicting_ideology_match", "role": role}
            if role in batch.get("ideology_conflicts", set()):
                return {"source": "hoi4_game_log", "event": "country_pair_context_error",
                        "error": "conflicting_ideology_match", "role": role}
            matches[role] = ideology_match["ideology_group"]
            return None

        try:
            major_match = parse_country_pair_major_power_match_v1_line(line)
        except ValueError as error:
            if COUNTRY_PAIR_MAJOR_POWER_MATCH_MARKER_V1 in line:
                return {"source": "hoi4_game_log", "event": "country_pair_context_error",
                        "error": "invalid_major_power_match", "detail": str(error)}
            major_match = None
        if major_match is not None:
            self._resolve_campaign_payload(major_match)
            key = (major_match["campaign_id"], major_match["player_country"],
                   major_match["selected_country"], major_match["sequence"])
            batch = self.country_pair_context_buffers.get(key)
            if batch is None:
                now = time.monotonic()
                self.country_pair_context_buffers = {
                    buffered_key: value for buffered_key, value in self.country_pair_context_buffers.items()
                    if now - value.get("started_at", now) <= 600.0
                }
                if len(self.country_pair_context_buffers) >= 64:
                    oldest = min(self.country_pair_context_buffers,
                                 key=lambda item: self.country_pair_context_buffers[item].get("started_at", now))
                    self.country_pair_context_buffers.pop(oldest, None)
                batch = {
                    "header": None, "claims": {}, "ideology_matches": {},
                    "ideology_conflicts": set(), "major_power_matches": {},
                    "major_power_conflicts": set(), "started_at": now,
                }
                self.country_pair_context_buffers[key] = batch
            if batch.get("header") is not None and (
                    major_match["player_country"] != batch["header"]["sender"]
                    or major_match["selected_country"] != batch["header"]["receiver"]):
                return {"source": "hoi4_game_log", "event": "country_pair_context_error",
                        "error": "major_power_match_participant_mismatch"}
            matches = batch.setdefault("major_power_matches", {})
            conflicts = batch.setdefault("major_power_conflicts", set())
            role = major_match["role"]
            previous = matches.get(role)
            if previous is not None and previous != major_match["is_major"]:
                conflicts.add(role)
                matches.pop(role, None)
                return {"source": "hoi4_game_log", "event": "country_pair_context_error",
                        "error": "conflicting_major_power_match", "role": role}
            if role in conflicts:
                return {"source": "hoi4_game_log", "event": "country_pair_context_error",
                        "error": "conflicting_major_power_match", "role": role}
            matches[role] = major_match["is_major"]
            return None

        try:
            pair_claim = parse_country_pair_claim_v1_line(line)
        except ValueError as error:
            if COUNTRY_PAIR_CLAIM_MARKER_V1 in line:
                return {"source": "hoi4_game_log", "event": "country_pair_context_error",
                        "error": "invalid_claim_row", "detail": str(error)}
            pair_claim = None
        if pair_claim is not None:
            self._resolve_campaign_payload(pair_claim)
            key = (pair_claim["campaign_id"], pair_claim["player_country"],
                   pair_claim["selected_country"], pair_claim["sequence"])
            batch = self.country_pair_context_buffers.get(key)
            if batch is None:
                return {"source": "hoi4_game_log", "event": "country_pair_context_error",
                        "error": "claim_without_v2_header", "campaign_id": pair_claim["campaign_id"],
                        "sequence": pair_claim["sequence"]}
            claim = pair_claim["claim"]
            state_key = claim["state_id"]
            previous = batch["claims"].get(state_key)
            if previous is not None and previous != claim:
                return {"source": "hoi4_game_log", "event": "country_pair_context_error",
                        "error": "conflicting_claim_row", "state_id": state_key}
            if previous is None and len(batch["claims"]) >= 1000:
                return {"source": "hoi4_game_log", "event": "country_pair_context_error",
                        "error": "claim_row_limit", "campaign_id": pair_claim["campaign_id"]}
            batch["claims"][state_key] = claim
            return None

        try:
            pair_context_end = parse_country_pair_context_end_v2_line(line)
        except ValueError as error:
            if COUNTRY_PAIR_CONTEXT_END_V2 in line:
                return {"source": "hoi4_game_log", "event": "country_pair_context_error",
                        "error": "invalid_v2_end", "detail": str(error)}
            pair_context_end = None
        if pair_context_end is not None:
            self._resolve_campaign_payload(pair_context_end)
            key = (pair_context_end["campaign_id"], pair_context_end["player_country"],
                   pair_context_end["selected_country"], pair_context_end["sequence"])
            batch = self.country_pair_context_buffers.pop(key, None)
            if batch is None or batch.get("header") is None:
                return {"source": "hoi4_game_log", "event": "country_pair_context_error",
                        "error": "end_without_v2_header", "campaign_id": pair_context_end["campaign_id"],
                        "sequence": pair_context_end["sequence"]}
            expected = pair_context_end["expected_claim_count"]
            claims = batch["claims"]
            if len(claims) != expected:
                return {"source": "hoi4_game_log", "event": "country_pair_context_error",
                        "error": "incomplete_claim_batch", "campaign_id": pair_context_end["campaign_id"],
                        "sequence": pair_context_end["sequence"], "expected_claim_count": expected,
                        "received_claim_count": len(claims), "observation_persisted": False}
            request = batch["header"]
            request["game_context"]["claims"] = [claims[state_id] for state_id in sorted(claims)]
            summary_codes = {
                "player": request["game_context"]["player_ideology_group"],
                "selected": request["game_context"]["ideology_group"],
            }
            # Prefer a literal match logged inside the actual country scope to
            # the numeric temporary-variable summary. The summary remains a
            # diagnostic and backward-compatible fallback; missing matches
            # remain unknown rather than being inferred from tags.
            ideology_matches = batch.get("ideology_matches", {})
            ideology_conflicts = batch.get("ideology_conflicts", set())
            player_match = ideology_matches.get("player")
            selected_match = ideology_matches.get("selected")
            if player_match and "player" not in ideology_conflicts:
                request["game_context"]["player_ideology_group"] = player_match
            elif "player" in ideology_conflicts:
                request["game_context"]["player_ideology_group"] = "unknown"
            if selected_match and "selected" not in ideology_conflicts:
                request["game_context"]["ideology_group"] = selected_match
            elif "selected" in ideology_conflicts:
                request["game_context"]["ideology_group"] = "unknown"
            major_matches = batch.get("major_power_matches", {})
            major_conflicts = batch.get("major_power_conflicts", set())
            request["game_context_schema_version"] = 9
            request["game_context"]["major_power"] = (
                major_matches.get("selected", "unknown")
                if "selected" not in major_conflicts else "unknown"
            )
            request["game_context"]["player_major_power"] = (
                major_matches.get("player", "unknown")
                if "player" not in major_conflicts else "unknown"
            )
            # The in-save sequence can roll back when a player reloads an
            # earlier save. Keep exact retries idempotent, but give changed
            # observations from the same rolled-back sequence a distinct key.
            context_fingerprint = hashlib.sha256(json.dumps(
                request["game_context"], ensure_ascii=True, sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")).hexdigest()[:12]
            request["request_id"] = (
                f"{request['campaign_id']}:pair-doctrine:{request['sequence']:010d}:"
                f"{context_fingerprint}"
            )
            persisted = self._persist_v5_game_context(request)
            if persisted.get("error"):
                return {"source": "hoi4_game_log", "event": "country_pair_context_error",
                        "campaign_id": request["campaign_id"], "request_id": request["request_id"],
                        "error": persisted, "observation_persisted": False}
            return {
                "source": "hoi4_game_log", "event": "country_pair_context_recorded",
                "campaign_id": request["campaign_id"], "request_id": request["request_id"],
                "player_country": request["sender"], "selected_country": request["receiver"],
                "chat_id": persisted.get("chat_id"), "game_context": persisted.get("game_context"),
                "game_context_schema_version": request["game_context_schema_version"],
                "observation_persisted": persisted.get("observation_persisted", False),
                "claim_count": len(claims), "diplomatic_request_sent": False,
                "ideology_matches": ideology_matches,
                "ideology_conflicts": sorted(ideology_conflicts),
                "major_power_matches": major_matches,
                "major_power_conflicts": sorted(major_conflicts),
                "ideology_summary_codes": summary_codes,
                "game_action_prepared": False,
            }

        pair_context = parse_country_pair_context_line(line)
        if pair_context is not None:
            self._resolve_campaign_payload(pair_context)
            persisted = self._persist_v5_game_context(pair_context)
            if persisted.get("error"):
                return {
                    "source": "hoi4_game_log", "event": "country_pair_context_error",
                    "campaign_id": pair_context["campaign_id"],
                    "request_id": pair_context["request_id"], "error": persisted,
                }
            return {
                "source": "hoi4_game_log", "event": "country_pair_context_recorded",
                "campaign_id": pair_context["campaign_id"],
                "request_id": pair_context["request_id"],
                "player_country": pair_context["sender"],
                "selected_country": pair_context["receiver"],
                "chat_id": persisted.get("chat_id"),
                "game_context": persisted.get("game_context", pair_context["game_context"]),
                "observation_persisted": persisted.get("observation_persisted", False),
                "diplomatic_request_sent": False,
                "game_action_prepared": False,
            }

        historical_focuses = parse_historical_focuses_line(line)
        if historical_focuses is not None:
            self._resolve_campaign_payload(historical_focuses)
            key = (historical_focuses["campaign_id"], historical_focuses["player_country"],
                   historical_focuses["snapshot_seq"])
            previous = self.state_integration_game_rules_buffers.get(key)
            if previous is not None and previous != historical_focuses:
                return {"event": "state_integration_error", "error": "conflicting_game_rule_marker",
                        "request_id": historical_focuses["request_id"]}
            if previous is None:
                self._append_state_integration_journal({
                    "kind": "historical_focus", "payload": {"line": line},
                })
            self.state_integration_game_rules_buffers[key] = historical_focuses
            return None

        snapshot_end = parse_state_integration_snapshot_end_line(line)
        if snapshot_end is not None:
            self._resolve_campaign_payload(snapshot_end)
            buffer_key = self._state_integration_key(
                snapshot_end["campaign_id"], snapshot_end["player_country"], snapshot_end["snapshot_seq"],
            )
            observations = list(self.state_integration_snapshot_buffers.get(buffer_key, []))
            historical_rule = self.state_integration_game_rules_buffers.get(buffer_key)
            expected = snapshot_end["expected_count"]
            if len(observations) != expected:
                self._finish_state_integration_buffer(buffer_key, "discard")
                return {
                    "event": "state_integration_error", "error": "incomplete_snapshot",
                    "campaign_id": snapshot_end["campaign_id"],
                    "snapshot_seq": snapshot_end["snapshot_seq"],
                    "expected_count": expected, "received_count": len(observations),
                    "detail": "Snapshot marker count does not match parsed state rows; no snapshot was stored.",
                }
            if historical_rule is not None and historical_rule["game_day"] != snapshot_end["game_day"]:
                self._finish_state_integration_buffer(buffer_key, "discard")
                return {
                    "event": "state_integration_error", "error": "game_rule_day_mismatch",
                    "campaign_id": snapshot_end["campaign_id"],
                    "snapshot_seq": snapshot_end["snapshot_seq"],
                    "detail": "Historical-focus marker and complete state snapshot report different game days.",
                }
            if any(item["context"]["game_day"] != snapshot_end["game_day"]
                   for item in observations):
                self._finish_state_integration_buffer(buffer_key, "discard")
                return {
                    "event": "state_integration_error", "error": "mixed_snapshot_days",
                    "campaign_id": snapshot_end["campaign_id"],
                    "snapshot_seq": snapshot_end["snapshot_seq"],
                    "detail": "State rows and snapshot completion marker report different game days.",
                }
            payload = {
                "campaign_id": snapshot_end["campaign_id"],
                "player_country": snapshot_end["player_country"],
                "snapshot_seq": snapshot_end["snapshot_seq"],
                "game_day": snapshot_end["game_day"], "observations": observations,
            }
            if historical_rule is not None:
                payload["historical_focuses_enabled"] = historical_rule["historical_focuses_enabled"]
                payload["historical_focuses_request_id"] = historical_rule["request_id"]
            conflict = self._queue_pending_state_integration_snapshot(payload)
            if conflict is not None:
                return conflict
            # The complete snapshot is durable before the in-flight rows are
            # retired, so a crash can safely replay the exact same sequence.
            self._finish_state_integration_buffer(buffer_key, "complete")
            return self._submit_state_integration_snapshot(payload)

        integration = parse_state_integration_line(line)
        if integration is not None:
            self._resolve_campaign_payload(integration)
            pieces = integration["request_id"].split(":")
            sequence = int(pieces[2])
            if sequence < 1:
                return {"event": "state_integration_error", "error": "invalid_snapshot_sequence",
                        "request_id": integration["request_id"]}
            key = (integration["campaign_id"], integration["player_country"], sequence)
            payload = {"request_id": integration["request_id"], "context": integration["context"]}
            rows = self.state_integration_snapshot_buffers.setdefault(key, [])
            previous = next((row for row in rows if row["request_id"] == payload["request_id"]), None)
            if previous is not None and previous != payload:
                return {"event": "state_integration_error", "error": "conflicting_duplicate_row",
                        "request_id": payload["request_id"]}
            if previous is None:
                if len(rows) >= 2000:
                    return {"event": "state_integration_error", "error": "snapshot_row_limit",
                            "campaign_id": integration["campaign_id"], "snapshot_seq": sequence,
                            "detail": "Snapshot exceeded the 2000-state safety limit; it was not extended."}
                self._append_state_integration_journal({
                    "kind": "state_row", "payload": {"line": line},
                })
                rows.append(payload)
            return None
        country_selection = parse_country_selection_line(line)
        if country_selection is not None:
            fresh = self._pending_fresh_campaign
            if fresh is not None and (
                    fresh["player_country"] == country_selection.get("player_country")
                    and fresh["selected_country"] == country_selection.get("selected_country")
                    and country_selection.get("source_campaign_token")):
                self._resolve_campaign_payload(country_selection, fresh_campaign=fresh)
                self._pending_fresh_campaign = None
            else:
                self._resolve_campaign_payload(country_selection)
            return {
                "source": "hoi4_game_log",
                "event": "country_selection",
                **country_selection,
            }
        if TERRITORY_RESULT_MARKER_V1 in line:
            fields = line.split(TERRITORY_RESULT_MARKER_V1, 1)[1].rstrip("\r\n").split("|", 4)
            if len(fields) != 5:
                return None
            campaign_id, proposal_id, plan_id, status, summary = fields
            if (not re.fullmatch(r"[0-9a-f]{32}", campaign_id)
                    or not re.fullmatch(r"[0-9a-f]{32}", proposal_id)
                    or not re.fullmatch(r"[0-9a-f]{12}", plan_id)
                    or status not in {"applied", "failed", "rolled_back", "rollback_failed"}):
                return None
            payload = {"campaign_id": campaign_id, "proposal_id": proposal_id,
                       "plan_id": plan_id, "status": status, "summary": summary}
            return self._submit_territory_result(payload)
        from player_native_actions import SENT_MARKER
        if SENT_MARKER in line:
            marker = SENT_MARKER + line.split(SENT_MARKER, 1)[1].strip()
            fields = marker.split("|")
            if len(fields) != 5 or not re.fullmatch(
                    r"agreement:[0-9a-f]{32}:v[1-9][0-9]*(?::term:[a-z][a-z0-9_]*)?", fields[3]):
                return None
            pending = self.provider.get_pending_agreement_execution(fields[3])
            if (pending.get("error") or pending.get("action_id") != fields[4]
                    or set(pending.get("participants", [])) != set(fields[1:3])):
                return None
            return self.provider._chat_api("POST",
                f"/chats/{pending['chat_id']}/agreements/{pending['agreement_id']}/native-sent",
                {"marker": marker}, timeout=5)
        if TREATY_DECLINED_MARKER_V1 in line:
            fields = line.split(TREATY_DECLINED_MARKER_V1, 1)[1].strip().split("|")
            if len(fields) != 3:
                return None
            agreement_id, raw_version, request_id = fields
            if (not re.fullmatch(r"[a-f0-9]{32}", agreement_id)
                    or not re.fullmatch(r"v[1-9][0-9]*", raw_version)
                    or not request_id):
                return None
            return self.provider._chat_api(
                "POST", "/agreements/treaty-declined",
                {"agreement_id": agreement_id, "version": int(raw_version[1:]),
                 "request_id": request_id,
                 "marker": TREATY_DECLINED_MARKER_V1 + "|".join(fields)},
            )
        if APPLIED_MARKER_V2 in line:
            applied = line.split(APPLIED_MARKER_V2, 1)[1].rstrip("\r\n").split("|")
            # Legacy GER-FRA branch: only reachable for state recovered by
            # _recover_pending_nap_from_response. Every other pair falls
            # through to exact prepared-package validation below.
            if len(applied) == 4 and applied[0] == "GER" and applied[1] == "FRA" and applied[3] == "non_aggression_pact":
                request_id = applied[2]
                if request_id in self.pending_nap_request_ids:
                    self._remove_pending_execution_item(self.pending_nap_request_ids, request_id)
                    result = {
                        "source": "hoi4_game_log", "request_id": request_id,
                        "action_id": "non_aggression_pact", "execution_status": "EXECUTION_CONFIRMED",
                        "confirmation": "EYES_HOI4_ACTION_APPLIED_V2",
                    }
                    write_response(self.response_path, result)
                    return result
            return self._record_agreement_marker(applied, "applied")
        if FAILED_MARKER_V2 in line:
            failed = line.split(FAILED_MARKER_V2, 1)[1].rstrip("\r\n").split("|")
            return self._record_agreement_marker(failed, "failed")
        if APPLIED_MARKER_V1 in line:
            # V1 has no request ID, so it cannot be safely correlated.
            return None
        request_data = parse_request_line(line)
        if request_data is None:
            return None
        self._resolve_campaign_payload(request_data)
        request_id = request_data["request_id"]
        if request_id in self.processed_request_ids:
            print("Duplicate HOI4 request ignored:", request_id)
            return None
        if request_data.get("campaign_id") is not None:
            context_result = self._persist_v5_game_context(request_data)
            if context_result.get("error"):
                raise RuntimeError("HOI4 game context was not saved: " + context_result.get("detail", "unknown error"))
        response = self.provider.send_request(request_data)
        if "error" in response:
            raise RuntimeError("/respond failed: " + str(response.get("detail", response["error"])))
        result = {
            "source": "hoi4_game_log",
            "request_id": request_id,
            "request": request_data,
            "response": response,
        }
        action = game_console_action_for(response, request_data)
        if action is not None:
            result["game_action"] = action
            self._queue_pending_execution_item(self.pending_nap_request_ids, request_id)
        write_response(self.response_path, result)
        self.processed_request_ids.add(request_id)
        return result


class GameLogTail:
    """Incrementally read game.log and recover after replacement or rewrite."""

    def __init__(self, path: Path, poll_interval: float = 0.25):
        self.path = path
        self.poll_interval = poll_interval
        self._file = None
        self._file_identity = None
        self._content_hash = hashlib.sha256()
        self._last_mtime_ns = None

    @staticmethod
    def _identity(stat_result):
        return stat_result.st_dev, stat_result.st_ino

    def _open(self, start_at_end: bool):
        self._file = self.path.open("rb")
        file_stat = os.fstat(self._file.fileno())
        self._file_identity = self._identity(file_stat)
        self._content_hash = hashlib.sha256()
        if start_at_end:
            while True:
                chunk = self._file.read(1024 * 1024)
                if not chunk:
                    break
                self._content_hash.update(chunk)
        else:
            self._file.seek(0)
        self._last_mtime_ns = self.path.stat().st_mtime_ns

    def _hash_prefix(self, length: int):
        digest = hashlib.sha256()
        with self.path.open("rb") as current:
            remaining = length
            while remaining:
                chunk = current.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
        return digest.digest() if remaining == 0 else None

    def _restart_from_beginning(self):
        if self._file is not None:
            self._file.close()
        self._open(start_at_end=False)

    def read_available(self):
        if not self.path.exists():
            return []
        try:
            if self._file is None:
                self._open(start_at_end=True)
                return []

            path_stat = self.path.stat()
            file_position = self._file.tell()
            replaced = self._identity(path_stat) != self._file_identity
            truncated = path_stat.st_size < file_position
            rewritten = False
            if not replaced and not truncated and path_stat.st_size == file_position:
                # Windows filesystems can report the same mtime for a fast
                # truncate/regrow cycle. Check bytes as well as metadata so an
                # equal-size rewrite cannot hide a fresh marker. This is an
                # idle-path read of the already-consumed prefix.
                current_hash = self._hash_prefix(file_position)
                rewritten = current_hash is not None and current_hash != self._content_hash.digest()

            if replaced or truncated or rewritten:
                self._restart_from_beginning()

            raw = self._file.read()
            if raw:
                self._content_hash.update(raw)
            self._last_mtime_ns = self.path.stat().st_mtime_ns
            return raw.decode("utf-8", errors="replace").splitlines(keepends=True)
        except OSError:
            if self._file is not None:
                self._file.close()
            self._file = None
            self._file_identity = None
            self._content_hash = hashlib.sha256()
            self._last_mtime_ns = None
            return []

    def close(self):
        if self._file is not None:
            self._file.close()
            self._file = None
            self._file_identity = None


class ResponseOverlay:
    """Minimal external response window; not an in-game HOI4 event/UI widget."""

    FOCUS_LOSS_GRACE_SECONDS = 0.75

    def __init__(self):
        import tkinter as tk

        self.tk = tk
        try:
            self.ollama_timeout_seconds = max(1, int(float(os.getenv("OLLAMA_TIMEOUT", "150"))))
        except (TypeError, ValueError, OverflowError):
            self.ollama_timeout_seconds = 150
        self.root = tk.Tk()
        self.root.title(tr('HOI4-EYES — Diplomatic consultations'))
        self.root.geometry("620x260+40+40")
        self.root.attributes("-topmost", True)
        self.root.configure(bg="#111820")
        self.root.withdraw()

        self.title = tk.Label(
            self.root, text=tr('HOI4-EYES  /  DIPLOMATIC CHANNEL'), anchor="w",
            bg="#111820", fg="#d1b475", font=("Segoe UI", 12, "bold"),
        )
        self.title.pack(fill="x", padx=16, pady=(14, 8))
        self.summary = tk.Label(
            self.root, text=tr('Waiting for the HOI4 request marker...'), anchor="w",
            bg="#111820", fg="#b9c3c5", font=("Segoe UI", 9), justify="left",
        )
        self.summary.pack(fill="x", padx=16, pady=4)
        self.message = tk.Message(
            self.root, text="", width=580, anchor="w",
            bg="#17212b", fg="#f2f0e8", font=("Segoe UI", 12),
            padx=12, pady=10, relief="flat",
        )
        self.message.pack(fill="both", expand=True, padx=16, pady=(8, 15))
        from chat_widgets import GroupParticipants
        self.group_panel = GroupParticipants(self.root,self.apply_group_participants,self._refresh_chat_controls,self.saved_group_negotiations)
        self.chat_history_frame = tk.Frame(self.root, bg="#1b241c")
        chat_tools=tk.Frame(self.chat_history_frame,bg="#1b241c")
        chat_tools.pack(fill='x')
        self.chat_history_count=tk.Label(chat_tools,text=tr('Negotiation history'),bg='#1b241c',fg='#c8b27c',font=('Segoe UI',9))
        self.chat_history_count.pack(side='left',padx=8,pady=4)
        tk.Button(chat_tools,text=tr('Latest messages'),command=lambda:self.chat_history.see('end'),
                  bg='#35412e',fg='#ead9af',relief='flat',font=('Segoe UI',9)).pack(side='right',padx=5,pady=3)
        self.chat_history = tk.Text(
            self.chat_history_frame, wrap="word", height=18, state="disabled",
            bg="#141d16", fg="#f2f0e8", insertbackground="#f2f0e8",
            font=("Segoe UI", 10), relief="flat", padx=10, pady=8,
            selectbackground="#3a5260", selectforeground="#ffffff",
        )
        self.chat_history.tag_configure(
            "player_message", background="#303b2b", foreground="#f2f0e8",
            lmargin1=36, lmargin2=36, rmargin=12, justify="left", spacing1=4, spacing3=5,
        )
        self.chat_history.tag_configure(
            "country_message", background="#202c22", foreground="#f2f0e8",
            lmargin1=12, lmargin2=12, rmargin=36, justify="left", spacing1=4, spacing3=5,
        )
        self.chat_scrollbar = tk.Scrollbar(
            self.chat_history_frame, command=self.chat_history.yview,
            bg="#26323a", troughcolor="#121a21", activebackground="#d1b475",
            relief="flat", borderwidth=0,
        )
        self.chat_history.configure(yscrollcommand=self.chat_scrollbar.set)
        self.chat_history.pack(side="left", fill="both", expand=True)
        self.chat_scrollbar.pack(side="right", fill="y")
        self.chat_history_frame.pack(fill="both", expand=True, padx=16, pady=(5, 8))
        self.chat_history_frame.pack_forget()
        self.retry_row = tk.Frame(self.root, bg="#111820")
        self.retry_button = tk.Button(
            self.retry_row, text=tr('Retry'), command=self.retry_chat_response,
            bg="#344c4c", fg="#f2f0e8", activebackground="#466361",
            activeforeground="#ffffff", relief="flat", borderwidth=0,
            font=("Segoe UI", 10, "bold"), padx=14, pady=6,
            disabledforeground="#7e8988",
        )
        self.retry_button.pack(side="right")
        self.retry_row.pack(fill="x", padx=16, pady=(0, 8))
        self.retry_row.pack_forget()
        self.chat_input_row = tk.Frame(self.root, bg="#111820")
        from chat_widgets import ChatComposer
        self.chat_input = ChatComposer(
            self.chat_input_row, font=("Segoe UI", 11),
            bg="#202b20", fg="#f2f0e8", insertbackground="#f2f0e8",
            relief="flat", highlightthickness=1,
            highlightbackground="#43504b", highlightcolor="#d1b475",
        )
        self.chat_input.grid(row=0,column=0,sticky='nsew',padx=(0,8))
        self.chat_input_row.columnconfigure(0,weight=1)
        self.chat_input.bind("<Return>", self._on_chat_input_return)
        self.chat_input.bind('<Shift-Return>',lambda event:None)
        self.chat_input.bind('<Control-Return>',self._on_chat_input_return)
        tk.Label(self.chat_input_row,text=tr('Enter: send · Shift+Enter: new line'),bg='#111820',fg='#b6bea9',font=('Segoe UI',8)).grid(row=1,column=0,columnspan=2,sticky='w',pady=3)
        self._install_clipboard_support()
        self.send_button = tk.Button(
            self.chat_input_row, text=tr('Send message'), command=self.send_chat_message,
            bg="#344c4c", fg="#f2f0e8", activebackground="#466361",
            activeforeground="#ffffff", relief="flat", borderwidth=0,
            font=("Segoe UI", 10, "bold"), padx=14, pady=6,
            disabledforeground="#7e8988",
        )
        self.send_button.grid(row=0,column=1,sticky='se')
        self.review_button = tk.Button(self.root, text=tr('Conditions and preparation'), command=self.review_player_agreement,
            bg="#344c4c", fg="#f2f0e8", relief="flat", padx=8)
        self.review_button.pack(fill="x", padx=16, pady=4)
        self.conference_button = tk.Button(self.root, text=tr('Conferences and delegations'), command=self.open_conference_panel,
            bg="#344c4c", fg="#f2f0e8", relief="flat", padx=8)
        self.conference_button.pack(fill="x", padx=16, pady=2)
        self.chat_input_row.pack(fill="x", padx=16, pady=(0, 10))
        self.chat_input_row.pack_forget()
        self.action = tk.Button(
            self.root, text=tr('Copy HOI4 confirmation command'), state="disabled",
            command=self.copy_game_action, bg="#26323a", fg="#d1b475",
            activebackground="#34434c", activeforeground="#f2f0e8",
            relief="flat", borderwidth=0, font=("Segoe UI", 9), pady=6,
        )
        self.action.pack(fill="x", padx=16, pady=(0, 14))
        self.pending_command = None
        self.chat_id = None
        self.active_chat = None
        self._chat_busy = False
        self._chat_busy_chat_id = None
        self._active_retry_attempt_id = None
        self._chat_view_generation = 0
        self._chat_results = queue.Queue()
        self._bridge = None
        self._pending_chat_draft = None
        self._window_state_provider = lambda: get_hoi4_window_state(self._native_window_handle())
        self._chat_open = False
        self._window_visible = False
        self._last_hoi4_pid = None
        self._focus_lost_since = None
        if hasattr(self.root, "protocol"):
            self.root.protocol("WM_DELETE_WINDOW", self.hide)

    def hide(self):
        """Hide the single overlay window until a new country selection arrives."""
        # Invalidate any chat-open result still in flight so it cannot reopen a
        # window the player explicitly closed.
        self._chat_view_generation += 1
        self._chat_open = False
        self.root.withdraw()
        self._window_visible = False
        log_bridge_ui_event("chat_overlay_closed_by_user", chat_id=self.chat_id)

    def _native_window_handle(self):
        try:
            return self.root.winfo_id()
        except (AttributeError, RuntimeError):
            return None

    def _sync_game_window_visibility(self):
        was_visible = self._window_visible
        try:
            state = self._window_state_provider()
        except Exception as error:
            # A failed observation is unknown, not evidence that HOI4 exited.
            # It must not abort selection before the asynchronous chat opens.
            log_bridge_ui_event('window_observation_failed', error_type=type(error).__name__, error=str(error))
            state = None
        if state is None:
            # On unsupported platforms retain the existing explicit overlay behavior.
            should_show = self._chat_open
            self._focus_lost_since = None
        else:
            pid = state.get("pid")
            if pid is not None and self._last_hoi4_pid is not None and pid != self._last_hoi4_pid:
                # A different HOI4 process may be a different campaign. Never
                # auto-show the previous session's transcript without a fresh selection.
                self._chat_view_generation += 1
                self._chat_open = False
                self.chat_id = None
                self.active_chat = None
            if pid is not None:
                self._last_hoi4_pid = pid
            from window_session_policy import policy
            session = policy(self._chat_open, state)
            should_show = session['visible']
            if not session['open'] and self._chat_open:
                self._chat_view_generation += 1
                self._chat_open = False
                self.chat_id = None
                self.active_chat = None
            self._focus_lost_since = None
            self.root.attributes("-topmost", session['topmost'])
        if should_show and not self._window_visible:
            self.root.deiconify()
            self.root.lift()
            self.root.attributes("-topmost", bool(state and (state.get("game_foreground", state.get("foreground")) or state.get("overlay_foreground"))))
            self._window_visible = True
        elif not should_show and self._window_visible:
            self.root.withdraw()
            self._window_visible = False
        if was_visible != self._window_visible:
            if not self._chat_open:
                reason = "no_open_chat"
            elif state is None:
                reason = "window_state_unavailable"
            elif should_show and not (
                state.get("game_foreground", state.get("foreground"))
                or state.get("overlay_foreground")
            ):
                reason = "focus_grace_period"
            elif self._window_visible:
                reason = "game_or_overlay_active"
            elif not state.get("running"):
                reason = "hoi4_not_running"
            elif state.get("minimized"):
                reason = "hoi4_minimized"
            else:
                reason = "another_application_foreground"
            log_bridge_ui_event(
                "overlay_visibility_changed", visible=self._window_visible,
                reason=reason, chat_id=self.chat_id, hoi4_pid=state.get("pid") if state else None,
                game_foreground=bool(state and state.get("game_foreground", state.get("foreground"))),
                overlay_foreground=bool(state and state.get("overlay_foreground")),
                minimized=bool(state and state.get("minimized")),
            )
        return should_show

    def _set_chat_mode(self, enabled: bool):
        if enabled:
            self.message.pack_forget()
            self.action.pack_forget()
            self.chat_input_row.pack(side='bottom',fill="x", padx=16, pady=(0, 10))
            if not self._chat_busy:
                self.retry_row.pack(side='bottom',fill="x", padx=16, pady=(0, 8))
            self.chat_history_frame.pack(fill="both", expand=True, padx=16, pady=(5, 8))
            if getattr(self,'group_panel',None):
                self.group_panel.pack(fill='x',padx=16,pady=4,before=self.chat_history_frame)
            self.root.minsize(680, 460)
            if not getattr(self,'_chat_layout_active',False):self.root.geometry("800x640+40+40")
            self._chat_layout_active=True
        else:
            self._chat_layout_active=False
            if getattr(self,'group_panel',None):self.group_panel.pack_forget()
            self.chat_history_frame.pack_forget()
            self.retry_row.pack_forget()
            self.chat_input_row.pack_forget()
            self.message.pack(fill="both", expand=True, padx=16, pady=(5, 15))
            self.action.pack(fill="x", padx=16, pady=(0, 14))
            self.root.minsize(600, 240)
            self.root.geometry("620x260+40+40")

    def _chat_pair_label(self, chat: Dict[str, Any]) -> str:
        player_country = str(chat.get("player_country") or "?")
        others = [str(tag) for tag in chat.get("participants", []) if tag != player_country]
        count = chat.get('unread_count', 0)
        suffix = f'  •  {count} unread / непрочитано' if count else ''
        from chat_presentation import decorate
        from ui_locale import language
        view = decorate(chat, language())
        return view['display_title']+'\n'+view['save_label']+suffix

    def _on_chat_input_return(self, _event=None):
        self.send_chat_message()
        return "break"

    def _install_clipboard_support(self):
        """Add predictable Windows clipboard shortcuts and per-widget menus."""
        # Bind Tk virtual events rather than relying on one keyboard layout's
        # physical key sequence. Tk maps Ctrl+C/V/X/A to these on Windows.
        for sequence, callback in (
            ("<<Copy>>", self._copy_input_selection),
            ("<<Cut>>", self._cut_input_selection),
            ("<<Paste>>", self._paste_input_clipboard),
            ("<<SelectAll>>", self._select_all_input),
        ):
            self.chat_input.bind(sequence, callback)
        self.chat_input.bind("<Button-1>", self._focus_input_on_click, add="+")
        self.chat_history.bind("<<Copy>>", self._copy_history_selection)
        self.chat_history.bind("<<SelectAll>>", self._select_all_history)

        self.input_context_menu = self.tk.Menu(self.root, tearoff=0)
        self.input_context_menu.add_command(label="Copy", command=self._copy_input_selection)
        self.input_context_menu.add_command(label="Paste", command=self._paste_input_clipboard)
        self.input_context_menu.add_command(label="Cut", command=self._cut_input_selection)
        self.input_context_menu.add_separator()
        self.input_context_menu.add_command(label="Select All", command=self._select_all_input)
        self.history_context_menu = self.tk.Menu(self.root, tearoff=0)
        self.history_context_menu.add_command(label="Copy", command=self._copy_history_selection)
        self.history_context_menu.add_command(label="Paste", state="disabled")
        self.history_context_menu.add_command(label="Cut", state="disabled")
        self.history_context_menu.add_separator()
        self.history_context_menu.add_command(label="Select All", command=self._select_all_history)
        self.chat_input.bind("<Button-3>", lambda event: self._popup_context_menu(event, "input"))
        self.chat_history.bind("<Button-3>", lambda event: self._popup_context_menu(event, "history"))

    def _focus_input_on_click(self, _event=None):
        # focus_set does not force the overlay to the foreground. It keeps the
        # caret in the Entry after a click once Tk already owns application focus.
        self.chat_input.focus_set()

    @staticmethod
    def _entry_has_selection(widget) -> bool:
        try:
            return bool(widget.selection_present())
        except (AttributeError, RuntimeError):
            return False

    def _copy_input_selection(self, _event=None):
        if self._entry_has_selection(self.chat_input):
            selected = self.chat_input.selection_get()
            self.root.clipboard_clear()
            self.root.clipboard_append(selected)
        return "break"

    def _cut_input_selection(self, _event=None):
        if self._entry_has_selection(self.chat_input):
            self._copy_input_selection()
            self.chat_input.delete("sel.first", "sel.last")
        return "break"

    def _paste_input_clipboard(self, _event=None):
        try:
            value = self.root.clipboard_get()
        except (self.tk.TclError, RuntimeError):
            return "break"
        if self._entry_has_selection(self.chat_input):
            self.chat_input.delete("sel.first", "sel.last")
        self.chat_input.insert("insert", value)
        return "break"

    def _select_all_input(self, _event=None):
        self.chat_input.focus_set()
        self.chat_input.selection_range(0, "end")
        self.chat_input.icursor("end")
        return "break"

    def _copy_history_selection(self, _event=None):
        try:
            selected = self.chat_history.get("sel.first", "sel.last")
        except (self.tk.TclError, RuntimeError):
            return "break"
        self.root.clipboard_clear()
        self.root.clipboard_append(selected)
        return "break"

    def _select_all_history(self, _event=None):
        self.chat_history.focus_set()
        self.chat_history.tag_add("sel", "1.0", "end-1c")
        self.chat_history.mark_set("insert", "end-1c")
        return "break"

    def _popup_context_menu(self, event, target: str):
        widget = self.chat_input if target == "input" else self.chat_history
        menu = self.input_context_menu if target == "input" else self.history_context_menu
        widget.focus_set()
        has_selection = (
            self._entry_has_selection(widget) if target == "input"
            else bool(widget.tag_ranges("sel"))
        )
        menu.entryconfigure("Copy", state="normal" if has_selection else "disabled")
        if target == "input":
            menu.entryconfigure("Cut", state="normal" if has_selection else "disabled")
            menu.entryconfigure("Select All", state="normal" if widget.get() else "disabled")
        else:
            menu.entryconfigure("Select All", state="normal" if widget.get("1.0", "end-1c") else "disabled")
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _render_chat(self, chat: Dict[str, Any]):
        render_started=time.monotonic()
        from chat_widgets import message_time,scroll_state
        follow,position=scroll_state(self.chat_history,self.chat_id==chat.get('chat_id'))
        self.active_chat = chat
        self.chat_id = chat.get("chat_id")
        if getattr(self,'group_panel',None):
            self.group_panel.set_chat(chat,chat.get('state')=='awaiting_player' and not self._chat_busy and chat.get('chat_mode')!='ai_to_ai')
        self._chat_open = True
        self.title.config(text=tr('Diplomatic negotiations'))
        player_country = str(chat.get("player_country") or "?")
        self.summary.config(text=self._chat_pair_label(chat))
        self.chat_history.config(state="normal")
        self.chat_history.delete("1.0", "end")
        from bridge_instance import unique_messages
        for item in unique_messages(chat.get("messages", [])):
            speaker = str(item.get("speaker") or "?")
            is_player = speaker == player_country or item.get("kind") in {"player", "player_request"}
            from chat_presentation import decorate
            from ui_locale import language
            speaker = decorate(chat, language())['country_labels'].get(speaker, speaker)
            role = tr('You') if is_player else tr('Country')
            tag = "player_message" if is_player else "country_message"
            representative=item.get('representative') or {}
            if representative.get('person_id') and representative.get('name'):
                speaker += ' / ' + representative['name']
            self.chat_history.insert(
                "end", f"{speaker} · {role}"+(f"  {message_time(item)}" if message_time(item) else '')+f"\n{item.get('text', '')}\n\n", tag,
            )
            diagnostic = item.get("provider_diagnostic_text")
            if (os.getenv("EYES_DEVELOPER_DIAGNOSTICS", "0") == "1"
                    and isinstance(diagnostic, str) and diagnostic.strip()):
                self.chat_history.insert(
                    "end", f"Ollama diagnostic (not a diplomatic position):\n{diagnostic.strip()}\n\n",
                    "country_message",
                )
        self.chat_history.config(state="disabled")
        if follow:self.chat_history.see("end")
        else:self.chat_history.yview_moveto(position)
        if hasattr(self,'chat_history_count'):
            self.chat_history_count.config(text=tr('Negotiation history')+' · '+str(len(unique_messages(chat.get('messages',[])))))
        bridge = getattr(self, '_bridge', None)
        if chat.get('unread_count') and bridge and callable(getattr(bridge.provider, 'mark_chat_read', None)):
            from diplomatic_inbox import unread
            ids = [m['request_id'] for m in unread(chat)][:100]
            self._start_chat_worker('read', lambda: bridge.provider.mark_chat_read(chat['chat_id'], ids),
                                    chat_id=chat['chat_id'])
        self._set_chat_mode(True)
        ready = (chat.get("state") == "awaiting_player" and not self._chat_busy
                 and not (getattr(self,"group_panel",None) and self.group_panel.dirty))
        self.chat_input.config(state="normal" if ready else "disabled")
        self.send_button.config(state="normal" if ready else "disabled")
        pending_draft = getattr(self, "_pending_chat_draft", None)
        if ready and pending_draft:
            self.chat_input.delete(0, "end")
            self.chat_input.insert(0, pending_draft)
            self._pending_chat_draft = None
            self.chat_input.focus_set()
        last_response = next((item for item in reversed(chat.get("messages", []))
                             if item.get("kind") == "country_response"), None)
        retryable = bool(last_response and last_response.get("retryable") is True)
        if retryable and not self._chat_busy:
            self.retry_row.pack(fill="x", padx=16, pady=(0, 8))
            self.retry_button.config(state="normal")
        elif retryable:
            self.retry_row.pack(fill="x", padx=16, pady=(0, 8))
            self.retry_button.config(state="disabled")
        else:
            self.retry_row.pack_forget()
        if last_response and last_response.get("provider_status") == "fallback":
            reason = last_response.get("provider_reason", "unknown")
            self.summary.config(text=(
                f"{self._chat_pair_label(chat)}  •  Safe reply shown ({reason})."
            ))
        elif last_response and last_response.get("provider_status") == "generated":
            detail = ("Diplomatic wording restored from the saved decision."
                      if last_response.get("provider_expression_error") else "Diplomatic reply received.")
            self.summary.config(text=(
                f"{self._chat_pair_label(chat)}  •  {tr(detail)}"
            ))
        self._sync_game_window_visibility()

        log_bridge_ui_event('chat_render_timing',seconds=round(time.monotonic()-render_started,4))

    def _start_chat_worker(self, operation: str, callback, *, generation=None, chat_id=None,
                           attempt_id=None):
        if getattr(self,'_hub_closing',False) is True:return
        if not hasattr(self,'_hub_worker_lock'):self._hub_worker_lock=threading.Lock()
        with self._hub_worker_lock:self._hub_inflight=getattr(self,'_hub_inflight',0)+1
        def run():
            started=time.monotonic()
            try:
                self._chat_results.put((operation, generation, chat_id, callback(), None, attempt_id))
            except Exception as error:
                self._chat_results.put((operation, generation, chat_id, None, str(error), attempt_id))
            finally:
                log_bridge_ui_event('chat_request_timing',operation=operation,
                                    seconds=round(time.monotonic()-started,4))
                with self._hub_worker_lock:self._hub_inflight-=1
        threading.Thread(target=run, daemon=True).start()

    def open_selected_country_chat(self, bridge: Hoi4LogBridge, selection: Dict[str, str]):
        self._hub_game_selection=dict(selection)
        log_bridge_ui_event('country_chat_open_entered',
                            player_country=selection.get('player_country'),
                            selected_country=selection.get('selected_country'))
        self._chat_view_generation += 1
        generation = self._chat_view_generation
        self._bridge = bridge
        mode = selection.get("annexation_method")
        if mode in {"voluntary", "political_pressure"}:
            player, target = selection.get("player_country", ""), selection.get("selected_country", "")
            if mode == "voluntary":
                self._pending_chat_draft = (
                    f"I propose a voluntary political union between {player} and {target}. "
                    "Please assess this proposal on its merits and suggest terms if needed."
                )
            else:
                self._pending_chat_draft = (
                    f"I demand the political incorporation of {target} into {player} under political pressure. "
                    "This is a proposal for negotiation, not an executed game action. "
                    "State your position and any counter-terms."
                )
        # Do not leave the previous chat addressable while a new pair loads.
        self.chat_id = None
        self.active_chat = None
        self._chat_open = True
        self.title.config(text=tr('HOI4 EYES — DIPLOMATIC CHAT'))
        self.summary.config(text=f"Opening diplomatic channel with {selection.get('selected_country')}…")
        self.message.config(text=tr('Connecting to Flask and loading saved chat history…'))
        self._set_chat_mode(False)
        visible = self._sync_game_window_visibility()
        log_bridge_ui_event(
            "country_chat_open_started",
            campaign_id=selection.get("campaign_id"),
            player_country=selection.get("player_country"),
            selected_country=selection.get("selected_country"),
            overlay_visible=bool(visible),
        )
        self._start_chat_worker(
            "open", lambda: bridge.open_or_create_chat(selection), generation=generation,
        )

    def open_conference_panel(self):
        if self.chat_id and self._bridge:
            cid = self.chat_id
            self._start_chat_worker('conference', lambda: self._bridge.provider.conferences(cid), chat_id=cid)

    def _show_conference_panel(self, data):
        import tkinter as tk
        from tkinter import ttk, messagebox
        old = getattr(self, '_conference_dialog', None)
        if old is not None and old.winfo_exists(): old.destroy()
        window = self._conference_dialog = tk.Toplevel(self.root)
        window.title(f"{tr('Conferences and delegations')} — HOI4-EYES")
        text = tk.Text(window, width=90, height=22, wrap='word')
        text.pack(fill='both', expand=True, padx=8, pady=8)
        clock = data.get('observed_clock') or {}
        lines = [f"Наблюдаемый игровой день: {clock.get('day', 'нет снимка')}",
                 'Имена действующих лидеров не подставляются без подтверждения текущей кампании.',
                 'Приглашённые советники не получают права подписания.']
        outcome = data.get('command_result') or {}
        if outcome.get('response'):
            lines.append(f"Приглашение: {outcome['response']}; основание: {outcome.get('reason', 'совещательное участие без полномочий')}")
        active = None
        for conference in data.get('conferences', []):
            if conference.get('status') == 'active': active = conference
            lines.append(f"\n{conference.get('goal', 'Встреча')}: {conference['status']}; дни {conference.get('start_day')}–{conference.get('end_day')}")
            language = (self.active_chat or {}).get('interface_language', 'en')
            phase = conference.get('phase_text', {})
            lines.append(phase.get(language) or phase.get('en', ''))
            for country, delegates in conference.get('delegations', {}).items():
                lines.append(country + ': ' + ', '.join(d['display_name'] for d in delegates))
            for review in conference.get('own_internal_discussions', []):
                lines.append('Внутреннее рассмотрение своей делегации (игровая симуляция):')
                lines.extend(f"{o['display_name']}: {o['personal_stance']}; позиция государства: {o['official_stance']}"
                             for o in review.get('opinions', []))
                for turn in review.get('turns',[]):
                    wording=turn.get('display_text',{})
                    lines.append(f"{turn['person_id']} · {turn['round']}: {wording.get(language) or wording.get('en','')}")
        text.insert('1.0', '\n'.join(lines)); text.config(state='disabled')
        tk.Button(window, text=tr('Refresh details'), command=self.open_conference_panel).pack()
        goals = data.get('goals') or ['non_aggression_pact']
        labels = {g.replace('_', ' ').capitalize():g for g in goals}
        goal = ttk.Combobox(window, values=list(labels), state='readonly'); goal.set(next(iter(labels))); goal.pack(fill='x', padx=8)
        days = tk.Spinbox(window, from_=1, to=7, width=4); days.pack()
        secrecy = ttk.Combobox(window, values=['open','closed','secret'], state='readonly'); secrecy.set('open'); secrecy.pack()
        informal = tk.BooleanVar(value=False)
        tk.Checkbutton(window, text=tr('Informal consultation without signing'), variable=informal).pack()
        cid = self.chat_id
        def command(operation, payload, selected=None):
            self._start_chat_worker('conference', lambda: self._bridge.provider.conference_command(cid, operation, payload, selected), chat_id=cid)
        def create():
            try: duration = int(days.get())
            except ValueError:
                messagebox.showerror('Конференция', 'Срок: от 1 до 7 игровых дней.', parent=window); return
            command('create', {'request_id':uuid.uuid4().hex, 'goal':labels[goal.get()],
                              'duration_days':duration, 'secrecy':secrecy.get(), 'informal':informal.get()})
        tk.Button(window, text=tr('Create meeting'), command=create).pack()
        people = {p['name']:p['id'] for p in data.get('historical_people', [])}
        person = ttk.Combobox(window, values=list(people), state='readonly'); person.pack(fill='x', padx=8)
        if active:
            selected = active['conference_id']
            tk.Button(window, text=tr('Invite adviser'), command=lambda: command('invite', {'person_id':people.get(person.get())}, selected)).pack()
            tk.Button(window, text=tr('Continue discussion'), command=lambda: command('advance', {}, selected)).pack()
            tk.Button(window, text=tr('Send agreement for separate confirmation'), command=lambda: command('handoff', {}, selected)).pack()
            tk.Button(window, text=tr('End meeting and retain agreements'), command=lambda: command('close', {}, selected)).pack()

    def _refresh_chat_controls(self):
        chat = self.active_chat or {}
        if getattr(self,'group_panel',None):
            self.group_panel.set_enabled(chat.get('state')=='awaiting_player' and not self._chat_busy and chat.get('chat_mode')!='ai_to_ai')
        ready = (chat.get("state") == "awaiting_player" and not self._chat_busy
                 and not (getattr(self,"group_panel",None) and self.group_panel.dirty))
        self.chat_input.config(state="normal" if ready else "disabled")
        self.send_button.config(state="normal" if ready else "disabled")
        last_response = next((item for item in reversed(chat.get("messages", []))
                              if item.get("kind") == "country_response"), None)
        retryable = bool(last_response and last_response.get("retryable") is True)
        if retryable:
            self.retry_row.pack(fill="x", padx=16, pady=(0, 8))
        else:
            self.retry_row.pack_forget()
        self.retry_button.config(state="normal" if retryable and not self._chat_busy else "disabled")

    def review_player_agreement(self):
        chat_id, bridge = self.chat_id, self._bridge
        if chat_id and bridge:
            self._start_chat_worker('player_review', lambda: bridge.provider.get_chat(chat_id), chat_id=chat_id)

    def _show_player_agreement(self, chat):
        import tkinter as tk
        from tkinter import messagebox, ttk
        from player_diplomacy import agreement_summary, agreement_title, sign_and_queue
        from player_native_actions import DELIVERY_ACTIONS
        from treaty_bundle import is_pending
        summary = agreement_summary(chat)
        previous = getattr(self, '_treaty_window', None)
        if previous and previous.winfo_exists():
            previous.destroy()
        ready = [a for a in chat.get('agreements', {}).values() if a.get('action_id') in DELIVERY_ACTIONS]
        window = tk.Toplevel(self.root)
        self._treaty_window = window
        window.title(tr('Agreement conditions'))
        window.transient(self.root)
        body = tk.Frame(window)
        body.pack(fill='both', expand=True)
        report = tk.Text(body, width=78, height=19, wrap='word', padx=10, pady=8)
        scroll = ttk.Scrollbar(body, command=report.yview)
        report.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        report.pack(fill='both', expand=True)
        report.insert('1.0', summary)
        report.configure(state='disabled')
        controls = ttk.Frame(window)
        controls.pack(fill='x', padx=8, pady=6)
        ttk.Button(controls, text=tr('Check execution'), command=self.review_player_agreement).pack(side='right')
        if self._chat_busy or not ready or chat.get('chat_mode') == 'ai_to_ai':
            return
        choice = ttk.Combobox(window, state='readonly', values=[
            f"{agreement_title(a)} · v{a['current_version']}" for a in ready])
        choice.current(0)
        choice.pack(fill='x', padx=8, pady=4)

        def prepare():
            a = ready[choice.current()]
            if self._chat_busy or self.chat_id != chat['chat_id']:
                return
            if not a.get('native_delivery') and not messagebox.askyesno(tr('Sign treaty'),
                    tr('Sign this exact version in EYES? Confirm the game effect separately in HOI4.'), parent=window):
                return
            prepare_button.state(['disabled'])
            self._start_chat_worker('player_prepare', lambda: sign_and_queue(
                self._bridge.provider, chat['chat_id'], a['agreement_id'], a['current_version'],
                Path(__file__).resolve().parents[1], runtime_dir()),
                chat_id=chat['chat_id'])

        def copy_confirmation():
            a = ready[choice.current()]
            if self._chat_busy or self.chat_id != chat['chat_id']:
                return
            provider = self._bridge.provider
            route = f"/chats/{chat['chat_id']}/agreements/{a['agreement_id']}/native-confirmation"
            copy_button.state(['disabled'])
            self._start_chat_worker('player_confirmation', lambda: provider._chat_api(
                'POST', route, {'version': a['current_version']}, timeout=10), chat_id=chat['chat_id'])

        prepare_button = ttk.Button(controls, text=tr('Sign treaty'), command=prepare)
        copy_button = ttk.Button(controls, text=tr('Copy HOI4 confirmation command'), command=copy_confirmation)
        prepare_button.pack(side='left', padx=(0, 6))
        copy_button.pack(side='left')

        def selection_changed(_event=None):
            a = ready[choice.current()]
            delivery = a.get('native_delivery') or {}
            pending = (not delivery.get('sent') and (a.get('status') in {'awaiting_approval', 'approved'}
                or (a.get('status') == 'manual_required' and (a.get('execution_package') or is_pending(a)))))
            prepare_button.configure(text=tr('Continue treaty execution' if a.get('approved_version')==a['current_version'] else 'Sign treaty'))
            prepare_button.state(['!disabled'] if pending else ['disabled'])
            bundle = (a.get('execution_package') or {}).get('bundle_term_ids')
            copy_button.state(['!disabled'] if a.get('status') == 'manual_required' and delivery and not bundle else ['disabled'])
        choice.bind('<<ComboboxSelected>>', selection_changed)
        selection_changed()

    def saved_group_negotiations(self):
        if self._chat_busy or not self._bridge or not self.active_chat:return
        chat_id=self.chat_id;generation=self._chat_view_generation
        campaign=self.active_chat['campaign_id'];provider=self._bridge.provider
        self._chat_busy=True;self._chat_busy_chat_id=chat_id;self._refresh_chat_controls()
        self._start_chat_worker('groups',lambda:provider.list_chats(campaign),generation=generation,chat_id=chat_id)

    def switch_group_negotiation(self, target_id):
        if self._chat_busy or not self._bridge or not self.active_chat:return
        current=dict(self.active_chat);chat_id=self.chat_id;provider=self._bridge.provider
        def load():
            result=provider.get_chat(target_id)
            if result.get('error'):return result
            if (result.get('campaign_id')!=current.get('campaign_id') or result.get('player_country')!=current.get('player_country')
                    or result.get('save_scope_id')!=current.get('save_scope_id') or result.get('chat_mode')=='ai_to_ai'):
                return {'error':'conflict','detail':'Choose a negotiation from the current campaign and save.'}
            return result
        self._chat_busy=True;self._chat_busy_chat_id=chat_id;self._refresh_chat_controls()
        self._start_chat_worker('group',load,generation=self._chat_view_generation,chat_id=chat_id)

    def apply_group_participants(self, countries, separate=False):
        if self._chat_busy or not self.chat_id or not self._bridge:return
        chat_id=self.chat_id;generation=self._chat_view_generation;provider=self._bridge.provider
        self._chat_busy=True;self._chat_busy_chat_id=chat_id
        self._refresh_chat_controls()
        self._start_chat_worker('group',lambda:provider._chat_api('POST','/chats/'+chat_id+'/group',
            {'ai_countries':countries,'new_group':separate}),generation=generation,chat_id=chat_id)

    def send_chat_message(self):
        if getattr(self,'group_panel',None) and self.group_panel.dirty:return
        if self._chat_busy or not self.chat_id or not self._bridge:
            return
        chat_id = self.chat_id
        generation = self._chat_view_generation
        bridge = self._bridge
        text = self.chat_input.get().strip()
        if not text:
            return
        self.chat_input.delete(0, "end")
        self._chat_busy = True
        self.chat_input.config(state="disabled")
        self.send_button.config(state="disabled")
        self.retry_button.config(state="disabled")
        self._chat_busy_chat_id = chat_id
        self._chat_wait_started=time.monotonic()
        self.summary.config(
            text=self._chat_pair_label(self.active_chat or {})+'  ·  '+tr('Waiting for AI reply')+'  0:00'
        )
        def send():
            if getattr(bridge,'supports_chat_progress',False):
                return bridge.send_chat_message(chat_id,text,on_progress=lambda value:self._chat_results.put(
                    ('progress',generation,chat_id,value,None,None)))
            return bridge.send_chat_message(chat_id,text)
        self._start_chat_worker(
            "send", send,
            generation=generation, chat_id=chat_id,
        )

    def retry_chat_response(self):
        if self._chat_busy or not self.chat_id or not self._bridge:
            return
        chat = self.active_chat or {}
        response = next((item for item in reversed(chat.get("messages", []))
                         if item.get("kind") == "country_response"), None)
        if not response or response.get("retryable") is not True:
            return
        response_request_id = response.get("request_id")
        if not isinstance(response_request_id, str) or not response_request_id:
            return
        chat_id = self.chat_id
        generation = self._chat_view_generation
        bridge = self._bridge
        attempt_id = uuid.uuid4().hex
        self._active_retry_attempt_id = attempt_id
        self._chat_wait_started=time.monotonic()
        self._chat_busy = True
        self._chat_busy_chat_id = chat_id
        self.retry_button.config(state="disabled")
        self.chat_input.config(state="disabled")
        self.send_button.config(state="disabled")
        self.summary.config(text=self._chat_pair_label(chat)+'  ·  '+tr('Waiting for AI reply')+'  0:00')
        self._start_chat_worker(
            "retry", lambda: bridge.retry_chat_response(
                chat_id, response_request_id, attempt_id,
            ), generation=generation, chat_id=chat_id, attempt_id=attempt_id,
        )

    def _drain_chat_results(self):
        if (getattr(self,'_chat_busy',False) and getattr(self,'_chat_busy_chat_id',None)==self.chat_id
                and hasattr(self,'_chat_wait_started')):
            elapsed=max(0,int(time.monotonic()-self._chat_wait_started))
            self.summary.config(text=self._chat_pair_label(self.active_chat or {})+'  ·  '+tr('Waiting for AI reply')+f'  {elapsed//60}:{elapsed%60:02d}')
        while True:
            try:
                item = self._chat_results.get_nowait()
            except queue.Empty:
                break
            # Accept the original three-field queue shape for third-party test
            # adapters while production workers always include request identity.
            if len(item) == 3:
                operation, result, error = item
                generation = self._chat_view_generation
                chat_id = self.chat_id
                attempt_id = None
            elif len(item) == 5:
                operation, generation, chat_id, result, error = item
                attempt_id = None
            else:
                operation, generation, chat_id, result, error, attempt_id = item
            if operation == 'conference':
                if chat_id == self.chat_id:
                    if error or not isinstance(result, dict) or result.get('error'):
                        from tkinter import messagebox
                        messagebox.showerror('Конференция', error or str((result or {}).get('detail', 'Не удалось загрузить конференцию')), parent=self.root)
                    else:
                        self._show_conference_panel(result)
                continue
            if operation == 'player_prepare':
                from tkinter import messagebox
                from player_diplomacy import preparation_issue
                result = result or {}
                if chat_id != self.chat_id:
                    continue
                if isinstance(result.get('chat'), dict) and not result['chat'].get('error'):
                    self._render_chat(result['chat'])
                    self._show_player_agreement(result['chat'])
                else:
                    self.review_player_agreement()
                title = tr('Sign treaty')
                status = result.get('queue_status')
                if status in {'QUEUED', 'ALREADY_QUEUED'}:
                    detail = tr('Treaty signed in EYES. Keep the main window open and exit HOI4 once to install queued treaties. Then load the matching save and confirm the decision in HOI4.')
                elif status == 'INSTALLED_FOR_REVIEW':
                    detail = tr('The exact treaty is installed. Review its decision in the matching HOI4 save. If the game has not loaded it, exit and reopen HOI4.')
                else:
                    reason = (error or result.get('detail') or result.get('block_reason')
                              or (result.get('execution_gate') or {}).get('reason'))
                    detail = preparation_issue(str(reason) if reason else None)
                messagebox.showinfo(title,detail,parent=self.root)
                continue
            if operation == 'player_confirmation':
                if chat_id != self.chat_id:
                    continue
                from tkinter import messagebox
                result = result or {}
                title = tr('Copy HOI4 confirmation command')
                if error or not result.get('command'):
                    messagebox.showinfo(title, tr(str(error or result.get('detail') or
                        'Prepare this exact game package first.')), parent=self.root)
                else:
                    self.root.clipboard_clear()
                    self.root.clipboard_append(result['command'])
                    messagebox.showinfo(title, tr('Command copied. Open the HOI4 console, paste the command, then confirm the event. Return here and check execution.'), parent=self.root)
                self.review_player_agreement()
                continue
            if operation == 'player_review':
                if chat_id == self.chat_id:
                    if error or not isinstance(result, dict) or result.get('error'):
                        from tkinter import messagebox
                        messagebox.showerror(tr('Agreement conditions'), str(error or (result or {}).get('detail')
                                             or tr('Could not load agreement conditions.')), parent=self.root)
                    else:
                        self._show_player_agreement(result)
                continue
            if operation == 'inbox':
                if not error and isinstance(result, dict) and result.get('campaign_id') == getattr(self, '_inbox_campaign', None):
                    self.title.config(text=f"HOI4-EYES • {result.get('unread_count', 0)} unread / непрочитано")
                continue
            if operation == 'read': continue
            if operation == "open" and generation != self._chat_view_generation:
                continue
            if operation == "retry" and attempt_id != getattr(self, "_active_retry_attempt_id", None):
                continue
            if operation == "open":
                if error or (isinstance(result, dict) and result.get("error")):
                    detail = error or result.get("detail", result.get("error"))
                    self.summary.config(text=f"Chat error: {detail}")
                    log_bridge_ui_event(
                        "country_chat_open_failed", chat_id=chat_id,
                        error_type=type(error).__name__ if error else "BridgeApiError",
                    )
                    continue
                self._render_chat(result)
                if getattr(self,'_hub_open_panel','chat')=='conference':
                    self._hub_open_panel='chat';self.open_conference_panel()
                log_bridge_ui_event(
                    "country_chat_open_ready",
                    chat_id=result.get("chat_id"), campaign_id=result.get("campaign_id"),
                    player_country=result.get("player_country"),
                    participants=result.get("participants", []),
                    saved_message_count=len(result.get("messages", [])),
                    overlay_visible=self._window_visible,
                )
            elif operation == 'groups':
                if chat_id==self._chat_busy_chat_id:self._chat_busy=False;self._chat_busy_chat_id=None
                if (self._chat_open and chat_id==self.chat_id and generation==self._chat_view_generation):
                    if error or (isinstance(result,dict) and result.get('error')):
                        self.summary.config(text=str(error or result.get('detail',result.get('error'))))
                    else:
                        from chat_presentation import decorate
                        current=self.active_chat or {};menu=self.tk.Menu(self.root,tearoff=0)
                        rows=[row for row in result.get('chats',[]) if row.get('campaign_id')==current.get('campaign_id')
                            and row.get('player_country')==current.get('player_country')
                            and row.get('save_scope_id')==current.get('save_scope_id') and row.get('chat_mode')!='ai_to_ai']
                        for row in sorted(rows,key=lambda row:row.get('updated_at',''),reverse=True)[:30]:
                            title=decorate(row,'en')['display_title']
                            if len(title)>90:title=title[:87]+'…'
                            menu.add_command(label=title,command=lambda identifier=row['chat_id']:self.switch_group_negotiation(identifier))
                        if not rows:menu.add_command(label=tr('No diplomatic chats yet. Choose a country in HOI4.'),state='disabled')
                        try:menu.tk_popup(self.root.winfo_rootx()+20,self.root.winfo_rooty()+100)
                        finally:menu.grab_release()
                self._refresh_chat_controls()
            elif operation == 'group':
                if chat_id == self._chat_busy_chat_id:
                    self._chat_busy=False;self._chat_busy_chat_id=None
                current=(self._chat_open and chat_id==self.chat_id and generation==self._chat_view_generation)
                if current:
                    if error or (isinstance(result,dict) and result.get('error')):
                        self.summary.config(text=str(error or result.get('detail',result.get('error'))))
                    else:
                        self._render_chat(result)
                self._refresh_chat_controls()
            elif operation == 'progress':
                if (self._chat_open and chat_id==self.chat_id and generation==self._chat_view_generation
                        and chat_id==self._chat_busy_chat_id and isinstance(result,dict)):
                    self._render_chat(result)
            elif operation == "send":
                if chat_id == self._chat_busy_chat_id:
                    self._chat_busy = False
                    self._chat_busy_chat_id = None
                is_current_chat = self._chat_open and chat_id == self.chat_id
                if error or (isinstance(result, dict) and result.get("error")):
                    if is_current_chat:
                        detail = error or result.get("detail", result.get("error"))
                        self.summary.config(text=f"Chat error: {detail}")
                    self._refresh_chat_controls()
                    continue
                # A reply for FRA must never replace an ENG view. Returning to
                # FRA opens it from Flask again and renders the persisted history.
                if is_current_chat:
                    self._render_chat(result)
                else:
                    self._refresh_chat_controls()
            elif operation == "retry":
                if chat_id == self._chat_busy_chat_id and attempt_id == self._active_retry_attempt_id:
                    self._chat_busy = False
                    self._chat_busy_chat_id = None
                is_current_chat = (self._chat_open and chat_id == self.chat_id
                                   and generation == self._chat_view_generation)
                if error or (isinstance(result, dict) and result.get("error")):
                    if is_current_chat:
                        detail = error or result.get("detail", result.get("error"))
                        self.summary.config(text=f"Retry failed: {detail}")
                    self._refresh_chat_controls()
                    continue
                if is_current_chat:
                    self._render_chat(result)
                else:
                    self._refresh_chat_controls()

    def copy_game_action(self):
        if not self.pending_command:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.pending_command)
        self.summary.config(text="Command copied. Open the HOI4 console, paste it, and confirm the in-game event.")

    def show(self, result: Dict[str, Any]):
        self._chat_open = True
        self._set_chat_mode(False)
        self.title.config(text=tr('HOI4-EYES — DIPLOMATIC RESPONSE'))
        if result.get("execution_status") in {"EXECUTION_CONFIRMED", "EXECUTION_ROLLED_BACK", "EXECUTION_FAILED", "ROLLBACK_FAILED"}:
            if result.get("execution_status") in {"EXECUTION_FAILED", "ROLLBACK_FAILED"}:
                action_label = ("HOI4 action was not applied; a game guard failed"
                                if result.get("execution_status") == "EXECUTION_FAILED"
                                else "HOI4 rollback was incomplete; inspect game state")
            else:
                action_label = ("Territorial transfer applied" if result.get("status") == "applied"
                                else "Territorial transfer rolled back") if result.get("action_id") == "territory_partition" else "HOI4 action confirmed"
            self.summary.config(
                text=(f"{action_label}  |  {result.get('action_id', 'unknown action')}  |  "
                      f"ID: {result.get('proposal_id', result.get('request_id', 'unknown'))}")
            )
            self.message.config(
                text=f"Game log confirmation: {result.get('confirmation', 'EXECUTION_CONFIRMED')}"
            )
            self.pending_command = None
            self.action.config(state="disabled")
        else:
            response = result["response"]
            request_data = result["request"]
            self.summary.config(
                text=(f"{request_data['sender']} -> {request_data['receiver']}  |  "
                      f"intent: {response.get('intent')}  |  decision: {response.get('decision')}  |  "
                      f"confidence: {response.get('confidence')}")
            )
            self.message.config(text=response.get("response", response.get("message", "")))
            action = result.get("game_action")
            if action:
                self.pending_command = action["console_command"]
                self.action.config(state="normal")
            else:
                self.pending_command = None
                self.action.config(state="disabled")
        self._sync_game_window_visibility()

    def run(self, bridge: Hoi4LogBridge, tail: GameLogTail, initial_selection=None):
        # Publish readiness only from the Tk event loop. A live PID/status file
        # alone cannot prove that the overlay is initialized and responding.
        def ui_pulse():
            callback = getattr(self, '_readiness_callback', None)
            if callback:
                try:
                    callback()
                except OSError as error:
                    log_bridge_ui_event('ui_readiness_write_failed', error=str(error))
                self.root.after(1000, ui_pulse)
        if getattr(self, '_readiness_callback', None):
            self.root.after(0, ui_pulse)
        if initial_selection is not None:
            self.open_selected_country_chat(bridge, initial_selection)

        def poll():
            if getattr(self,'_hub_closing',False) is True:return
            try:
                self._drain_chat_results()
                for line in tail.read_available():
                    try:
                        result = bridge.process_line(line)
                        if result:
                            if result.get('event') == 'autonomy_snapshot_recorded':
                                campaign = result.get('campaign_id')
                                if isinstance(campaign, str):
                                    self._inbox_campaign = campaign
                                    self._start_chat_worker('inbox', lambda c=campaign: bridge.provider.campaign_inbox(c))
                            if result.get("event", "").startswith("state_integration_"):
                                print("HOI4 state integration observation:", result)
                                continue
                            if result.get("event") in {"country_selection", "annexation_proposal"}:
                                log_bridge_ui_event(
                                    "country_selection_received",
                                    campaign_id=result.get("campaign_id"),
                                    player_country=result.get("player_country"),
                                    selected_country=result.get("selected_country"),
                                    campaign_identity_source=result.get("campaign_identity_source"),
                                    annexation_method=result.get("annexation_method"),
                                )
                                if not result.get("campaign_id"):
                                    self.summary.config(text="Selection marker has no campaign_id; chat was not opened.")
                                    self.root.deiconify()
                                else:
                                    self.open_selected_country_chat(bridge, result)
                                continue
                            # game.log also carries observation/acknowledgement
                            # events which are not diplomatic replies.  Passing
                            # those to show() indexed response/request
                            # unconditionally and raised KeyError('response') on
                            # every poll for some V5/context markers.
                            if result.get("event"):
                                print("HOI4 bridge event:", result.get("event"))
                                continue
                            has_reply = (
                                isinstance(result.get("request"), dict)
                                and isinstance(result.get("response"), dict)
                            )
                            has_execution_result = result.get("execution_status") in {
                                "EXECUTION_CONFIRMED", "EXECUTION_ROLLED_BACK", "EXECUTION_FAILED", "ROLLBACK_FAILED",
                            }
                            if not (has_reply or has_execution_result):
                                print("HOI4 bridge ignored incomplete result; no response was displayed.")
                                continue
                            self.show(result)
                            request = result.get("request")
                            if request:
                                print("HOI4 request processed:", request["request_id"])
                            elif result.get("execution_status") == "EXECUTION_CONFIRMED":
                                print("HOI4 action confirmed:", result.get("request_id"))
                            if result.get("game_action"):
                                print("User-confirmed HOI4 action available:", result["game_action"]["console_command"])
                    except Exception as error:
                        log_bridge_ui_event('bridge_poll_failed', error_type=type(error).__name__, error=str(error))
                        print("HOI4 bridge poll error:", error, file=sys.stderr)
            finally:
                try:
                    self._sync_game_window_visibility()
                except Exception as error:
                    print("HOI4 window-state check error:", error, file=sys.stderr)
                try:
                    for result in bridge.retry_pending_territory_results():
                        self.show(result)
                except Exception as error:
                    print("HOI4 territory result retry error:", error, file=sys.stderr)
                try:
                    for result in bridge.retry_pending_agreement_results():
                        self.show(result)
                except Exception as error:
                    print("HOI4 agreement result retry error:", error, file=sys.stderr)
                try:
                    for result in bridge.retry_pending_state_integration_observations():
                        print("HOI4 state integration observation:", result)
                except Exception as error:
                    print("HOI4 state integration retry error:", error, file=sys.stderr)
                try:
                    self.root.after(max(50, int(tail.poll_interval * 1000)), poll)
                except Exception as error:
                    print("HOI4 bridge poll scheduling error:", error, file=sys.stderr)

        self.root.after(100, poll)
        try:
            self.root.mainloop()
        finally:
            tail.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Bridge HOI4 game.log requests to the Eyes diplomacy API.")
    parser.add_argument("--game-log", type=Path, default=DEFAULT_GAME_LOG)
    parser.add_argument("--endpoint", default=os.getenv("AI_BRIDGE_URL", "http://127.0.0.1:5000/respond"))
    parser.add_argument("--response-file", type=Path, default=DEFAULT_RESPONSE_FILE)
    parser.add_argument("--status-file", type=Path, default=None,
                        help="write PID, source revision, response path, and overlay state for the stack launcher")
    parser.add_argument("--poll-interval", type=float, default=0.25)
    parser.add_argument(
        "--timeout", type=int, default=None,
        help="Flask request timeout; defaults to at least OLLAMA_TIMEOUT + 15 seconds (default 165s)",
    )
    parser.add_argument("--no-overlay", action="store_true", help="write JSON and print results without a Tk window")
    recovery_options = parser.add_mutually_exclusive_group()
    recovery_options.add_argument(
        "--recover-country-selection", metavar="CAMPAIGN_ID",
        help="open/reuse the one unambiguous country-selection chat in an existing game.log",
    )
    recovery_options.add_argument(
        "--recover-latest-country-selection", action="store_true",
        help="open/reuse the chat for the last valid country selection in game.log",
    )
    parser.add_argument(
        "--replay-state-snapshot", metavar="CAMPAIGN_ID:SEQUENCE",
        help="replay one exact complete state-integration snapshot from game.log",
    )
    args = parser.parse_args()

    if args.poll_interval <= 0:
        parser.error("--poll-interval must be positive")
    try:
        ollama_timeout = float(os.getenv("OLLAMA_TIMEOUT", "150"))
        configured_ai_timeout = int(os.getenv("AI_TIMEOUT", "165"))
    except ValueError:
        parser.error("OLLAMA_TIMEOUT and AI_TIMEOUT must be numeric")
    if ollama_timeout <= 0:
        parser.error("OLLAMA_TIMEOUT must be positive")
    bridge_timeout = args.timeout if args.timeout is not None else max(
        configured_ai_timeout, int(ollama_timeout + 15)
    )
    if bridge_timeout <= ollama_timeout + 5:
        parser.error("--timeout must exceed OLLAMA_TIMEOUT by at least 6 seconds")
    bridge = Hoi4LogBridge(args.endpoint, args.response_file, timeout=bridge_timeout)
    bridge.game_log_path = args.game_log
    tail = GameLogTail(args.game_log, poll_interval=args.poll_interval)
    initial_selection = None
    for recovered in bridge.recover_pending_state_integration_snapshots_from_log(args.game_log):
        print("Recovered buffered HOI4 state snapshot:", json.dumps(recovered, ensure_ascii=False))
    if args.recover_country_selection:
        recovered_selection = bridge.recover_country_selection_from_log(
            args.game_log, args.recover_country_selection,
        )
        print(json.dumps(recovered_selection, ensure_ascii=False))
        if recovered_selection.get("event") != "country_selection_chat_ready":
            tail.close()
            return 2
        initial_selection = {
            "player_country": recovered_selection["player_country"],
            "selected_country": recovered_selection["selected_country"],
            "campaign_id": recovered_selection["campaign_id"],
        }
    if args.recover_latest_country_selection:
        recovered_selection = bridge.recover_latest_country_selection_from_log(args.game_log)
        print(json.dumps(recovered_selection, ensure_ascii=False))
        if recovered_selection.get("event") != "country_selection_chat_ready":
            tail.close()
            return 2
        initial_selection = {
            "player_country": recovered_selection["player_country"],
            "selected_country": recovered_selection["selected_country"],
            "campaign_id": recovered_selection["campaign_id"],
        }
    elif not args.recover_country_selection and not args.no_overlay:
        game_state = get_hoi4_window_state()
        initial_selection = recover_startup_country_selection(
            bridge, args.game_log, bool(game_state and game_state.get("running")),
        )
        if initial_selection is not None:
            print("Recovered the current HOI4 country chat after Bridge startup:",
                  json.dumps(initial_selection, ensure_ascii=False))
    if args.replay_state_snapshot:
        try:
            replay_campaign, replay_sequence_text = args.replay_state_snapshot.split(":", 1)
            replay_sequence = int(replay_sequence_text)
        except (TypeError, ValueError):
            parser.error("--replay-state-snapshot must be CAMPAIGN_ID:SEQUENCE")
        recovered_snapshot = bridge.replay_state_integration_snapshot_from_log(
            args.game_log, replay_campaign, replay_sequence,
        )
        print(json.dumps(recovered_snapshot, ensure_ascii=False))
        if recovered_snapshot.get("event") != "state_integration_snapshot_recorded":
            tail.close()
            return 2
    bridge_revision = compute_bridge_source_revision()
    status_file = args.status_file or Path(os.getenv(
        "EYES_BRIDGE_STATUS_FILE",
        str(runtime_dir() / BRIDGE_RUNTIME_STATUS_NAME),
    ))
    try:
        bridge_status = write_bridge_runtime_status(
            status_file, pid=os.getpid(), response_file=args.response_file,
            game_log=args.game_log, endpoint=args.endpoint,
            overlay_enabled=not args.no_overlay, source_revision=bridge_revision,
            request_timeout_seconds=bridge_timeout,
            ollama_timeout_seconds=ollama_timeout,
        )
    except OSError as error:
        print("Bridge status could not be written:", error, file=sys.stderr)
        tail.close()
        return 2
    print("Watching HOI4 game log:", args.game_log)
    print("Bridge source revision:", bridge_status["source_revision"])
    print("Campaign identity parser:", SAVE_CAMPAIGN_IDENTITY_FORMAT)
    print("POST /respond:", args.endpoint)
    print("Flask request timeout:", bridge_timeout, "seconds; Ollama timeout:", ollama_timeout, "seconds")
    print("Response JSON:", args.response_file)
    print("Choose a country in EYES Diplomacy to open or resume its campaign chat.")
    log_bridge_ui_event(
        "bridge_overlay_started", response_file=str(args.response_file),
        overlay_enabled=not args.no_overlay,
        game_log=str(args.game_log),
        recovery_mode=("latest" if args.recover_latest_country_selection else
                       args.recover_country_selection or None),
        source_revision=bridge_revision,
        status_file=str(status_file.resolve()),
    )

    try:
        if args.no_overlay:
            while True:
                for line in tail.read_available():
                    try:
                        result = bridge.process_line(line)
                        if result:
                            if result.get("event") == "country_selection":
                                print(json.dumps(open_country_selection_chat(bridge, result), ensure_ascii=False))
                                continue
                            print(json.dumps(result, ensure_ascii=False, indent=2))
                    except (RuntimeError, ValueError) as error:
                        print("HOI4 bridge error:", error, file=sys.stderr)
                for result in bridge.retry_pending_territory_results():
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                for result in bridge.retry_pending_agreement_results():
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                for result in bridge.retry_pending_state_integration_observations():
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                time.sleep(args.poll_interval)
        else:
            overlay = ResponseOverlay()
            from stack_readiness import write_ui_pulse
            overlay._readiness_callback = lambda: write_ui_pulse(
                status_file.with_name('bridge_ready.json'), os.getpid(), bridge_revision,
                {'selection':getattr(overlay,'_hub_game_selection',None),'chat_id':overlay.chat_id,
                 'busy':bool(getattr(overlay,'_chat_busy',False) or getattr(overlay,'_hub_inflight',0))})
            from hub_control import CommandReader,atomic_json
            from bridge_auth import load_bridge_token
            from overlay_control import handle as handle_menu_command
            controls=CommandReader(status_file.with_name('bridge_control.json'),os.getpid(),load_bridge_token(),os.getenv('EYES_MENU_OWNER_TOKEN'))
            def menu_poll():
                body=controls.read()
                if body:
                    try:reply=handle_menu_command(overlay,bridge,body)
                    except Exception as error:reply={'error':str(error)}
                    atomic_json(status_file.with_name('bridge_control_reply.json'),{'request_id':body['request_id'],**reply})
                overlay.root.after(200,menu_poll)
            overlay.root.after(200,menu_poll)
            overlay.run(bridge, tail, initial_selection=initial_selection)
    except KeyboardInterrupt:
        print("Bridge stopped.")
    finally:
        tail.close()
        clear_bridge_runtime_status(status_file, os.getpid())
    return 0


if __name__ == "__main__":
    from bridge_instance import bridge_instance_lock
    with bridge_instance_lock(runtime_dir() / 'bridge_instance.lock') as acquired:
        if not acquired:
            print('HOI4-EYES Bridge already owns this channel.')
            raise SystemExit(0)
        raise SystemExit(main())
