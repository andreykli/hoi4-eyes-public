"""Campaign-scoped, bounded diplomatic memory backed by the existing chat-state store.

This module stores durable structured records separately from chat transcripts.
It intentionally does not infer motives, country interests, or promise breaches
from generated prose.  Only engine-validated positions and authenticated game
result markers may enter the corresponding ledgers.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import re
from typing import Any, Mapping

from hoi4_country_tags import COUNTRY_TAG_RE
from strategic_interests import normalize_countries
from voices_history.strategist import normalize_ledger
from v6_autonomy import normalize as normalize_autonomy
from historical_conferences import normalize as normalize_historical


SCHEMA_VERSION = 1
MAX_POSITIONS_PER_PAIR = 24
MAX_OUTCOMES_PER_PAIR = 24
MAX_GAME_OBSERVATIONS_PER_PAIR = 20
_CAMPAIGN_RE = re.compile(r"^[0-9a-f]{32}$")
_ALLOWED_STANCES = {
    "accept", "conditional_acceptance", "counter_offer", "reject",
    "discuss", "clarify", "defer",
}


def empty_state() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "pairs": {}, "strategic_countries": {}, "strategic_reviews": {},
            "v6_autonomy": normalize_autonomy(None), "historical_diplomacy": normalize_historical(None)}


def _normalize_tag(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a HOI4 country tag")
    tag = value.strip().upper()
    if not COUNTRY_TAG_RE.fullmatch(tag):
        raise ValueError(f"{field} must be a HOI4 country tag")
    return tag


def _pair_key(campaign_id: str, observer_tag: str, counterpart_tag: str) -> str:
    return f"{campaign_id}:{observer_tag}:{counterpart_tag}"


def normalize_state(value: Any) -> dict[str, Any]:
    """Validate persisted memory; an absent memory object is a V5-compatible migration."""
    if value is None:
        return empty_state()
    if not isinstance(value, Mapping):
        raise ValueError("diplomatic_memory must be an object")
    version = value.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise ValueError("unsupported diplomatic memory schema version")
    pairs = value.get("pairs", {})
    if not isinstance(pairs, Mapping):
        raise ValueError("diplomatic memory pairs must be an object")
    normalized: dict[str, Any] = {}
    for key, row in pairs.items():
        if not isinstance(key, str) or not isinstance(row, Mapping):
            continue
        campaign_id = row.get("campaign_id")
        if not isinstance(campaign_id, str) or not _CAMPAIGN_RE.fullmatch(campaign_id):
            continue
        try:
            observer = _normalize_tag(row.get("observer_tag"), "observer_tag")
            counterpart = _normalize_tag(row.get("counterpart_tag"), "counterpart_tag")
        except ValueError:
            continue
        if observer == counterpart or key != _pair_key(campaign_id, observer, counterpart):
            continue
        try:
            promise_trust = float(row.get("promise_trust", 0.5))
        except (TypeError, ValueError):
            promise_trust = 0.5
        if promise_trust != promise_trust:
            promise_trust = 0.5
        promise_trust = min(1.0, max(0.0, promise_trust))
        positions = row.get("positions", [])
        outcomes = row.get("confirmed_outcomes", [])
        trust_events = row.get("trust_events", [])
        normalized[key] = {
            "campaign_id": campaign_id,
            "observer_tag": observer,
            "counterpart_tag": counterpart,
            "promise_trust": promise_trust,
            "positions": deepcopy(positions[-MAX_POSITIONS_PER_PAIR:])
            if isinstance(positions, list) else [],
            "confirmed_outcomes": deepcopy(outcomes[-MAX_OUTCOMES_PER_PAIR:])
            if isinstance(outcomes, list) else [],
            "verified_game_observations": deepcopy(row.get("verified_game_observations", [])[-MAX_GAME_OBSERVATIONS_PER_PAIR:])
            if isinstance(row.get("verified_game_observations", []), list) else [],
            "trust_events": deepcopy(trust_events[-MAX_OUTCOMES_PER_PAIR:])
            if isinstance(trust_events, list) else [],
            "updated_at": str(row.get("updated_at") or "")[:40],
        }
    return {"schema_version": SCHEMA_VERSION, "pairs": normalized,
            "strategic_countries": normalize_countries(value.get("strategic_countries")),
            "strategic_reviews": normalize_ledger(value.get("strategic_reviews")),
            "v6_autonomy": normalize_autonomy(value.get("v6_autonomy")),
            "historical_diplomacy": normalize_historical(value.get("historical_diplomacy"))}


def _ensure_pair(state: dict[str, Any], campaign_id: str,
                 observer_tag: str, counterpart_tag: str) -> dict[str, Any]:
    if not isinstance(campaign_id, str) or not _CAMPAIGN_RE.fullmatch(campaign_id):
        raise ValueError("campaign_id must be a 32-character lowercase UUID")
    observer = _normalize_tag(observer_tag, "observer_tag")
    counterpart = _normalize_tag(counterpart_tag, "counterpart_tag")
    if observer == counterpart:
        raise ValueError("a country cannot have a diplomatic memory pair with itself")
    key = _pair_key(campaign_id, observer, counterpart)
    return state["pairs"].setdefault(key, {
        "campaign_id": campaign_id,
        "observer_tag": observer,
        "counterpart_tag": counterpart,
        "promise_trust": 0.5,
        "positions": [],
        "confirmed_outcomes": [],
        "verified_game_observations": [],
        "trust_events": [],
        "updated_at": "",
    })


def record_validated_positions(state: dict[str, Any], *, campaign_id: str,
                               observer_tag: str, counterpart_tag: str,
                               source_request_id: str, version: int | None,
                               positions: Any, response_request_id: str | None = None) -> int:
    """Record complete engine positions, linked to both source and answer requests."""
    if not isinstance(source_request_id, str) or not source_request_id.strip():
        return 0
    if not isinstance(positions, list):
        return 0
    response_request_id = response_request_id or source_request_id
    if not isinstance(response_request_id, str) or not response_request_id.strip():
        return 0
    pair = _ensure_pair(state, campaign_id, observer_tag, counterpart_tag)
    existing = {item.get("position_id") for item in pair["positions"] if isinstance(item, Mapping)}
    added = 0
    for index, position in enumerate(positions[:8]):
        if not isinstance(position, Mapping):
            continue
        position_source_request_id = position.get("source_request_id")
        if position_source_request_id is None:
            position_source_request_id = source_request_id
        if (not isinstance(position_source_request_id, str)
                or position_source_request_id.strip() != source_request_id.strip()):
            continue
        stance = str(position.get("stance") or "").strip().lower()
        if stance not in _ALLOWED_STANCES:
            continue
        term_id = position.get("term_id")
        if term_id is not None and (not isinstance(term_id, str) or len(term_id) > 120):
            continue
        point = position.get("point", index)
        if not isinstance(point, (str, int)) or isinstance(point, bool):
            point = index
        scope = str(term_id if term_id else point)
        position_id = f"{response_request_id}:{scope}:v{version or 0}"
        if position_id in existing:
            continue
        text = position.get("text")
        row = {
            "position_id": position_id,
            "source_request_id": position_source_request_id[:160],
            "response_request_id": response_request_id[:160],
            "term_id": term_id,
            "point": point,
            "stance": stance,
            "summary": text.strip()[:280] if isinstance(text, str) else "",
            "agreement_version": version if isinstance(version, int) and not isinstance(version, bool) else None,
            "authority": "engine_validated_negotiation_position",
            "binding": False,
        }
        pair["positions"].append(row)
        existing.add(position_id)
        added += 1
    if added:
        pair["positions"] = pair["positions"][-MAX_POSITIONS_PER_PAIR:]
        pair["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return added


def record_verified_game_observation(state: dict[str, Any], *, campaign_id: str,
                                     observer_tag: str, counterpart_tag: str,
                                     source: str, schema_version: int,
                                     request_id: str, game_date: str,
                                     facts: Mapping[str, Any]) -> bool:
    """Archive a compact Bridge-parsed snapshot without turning unknowns into facts."""
    expected_source = f"bridge_parsed_hoi4_game_log_v{schema_version}"
    if (schema_version not in {5, 6, 7, 8, 9} or source != expected_source
            or not isinstance(request_id, str) or not request_id.strip()
            or not isinstance(game_date, str) or not game_date.strip()
            or not isinstance(facts, Mapping)):
        return False
    allowed = {
        "date", "game_day", "war_status", "same_faction", "existing_non_aggression_pact",
        "relations", "threat", "faction_status", "opinion", "alliance_status",
        "ideology_group", "player_ideology_group", "major_power", "player_major_power",
        "initiator_owned_states", "target_owned_states", "initiator_divisions",
        "target_divisions", "initiator_factories", "target_factories", "target_stability",
        "relations_direction", "claims",
    }
    compact = {}
    for key in allowed:
        value = facts.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            if value.strip().lower() in {"unknown", "unavailable", "none", ""}:
                continue
            compact[key] = value.strip()[:100]
        elif isinstance(value, (bool, int, float)):
            compact[key] = value
        elif key == "claims" and isinstance(value, list):
            rows = []
            for claim in value[:8]:
                if not isinstance(claim, Mapping):
                    continue
                row = {
                    field: claim[field] for field in (
                        "claimant_tag", "owner_tag", "state_id", "strategic_value_proxy",
                    ) if field in claim and isinstance(claim[field], (str, int, float))
                    and not isinstance(claim[field], bool)
                }
                if len(row) >= 3:
                    rows.append(row)
            if rows:
                compact[key] = rows
    if not compact:
        return False
    pair = _ensure_pair(state, campaign_id, observer_tag, counterpart_tag)
    if any(item.get("evidence_request_id") == request_id
           for item in pair["verified_game_observations"]
           if isinstance(item, Mapping)):
        return False
    pair["verified_game_observations"].append({
        "evidence_request_id": request_id[:160],
        "game_date": game_date[:100],
        "schema_version": schema_version,
        "source": source,
        "facts": compact,
        "authority": "bridge_parsed_game_observation",
    })
    pair["verified_game_observations"] = pair["verified_game_observations"][-MAX_GAME_OBSERVATIONS_PER_PAIR:]
    pair["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return True


def record_confirmed_application(state: dict[str, Any], *, campaign_id: str,
                                 participants: Any, agreement_id: str,
                                 agreement_version: int, action_id: str,
                                 evidence_request_id: str) -> int:
    """Increase separate promise-trust only after a final exact HOI4 marker confirms application."""
    if not isinstance(participants, list):
        return 0
    tags = []
    for participant in participants:
        try:
            tag = _normalize_tag(participant, "participant")
        except ValueError:
            continue
        if tag not in tags:
            tags.append(tag)
    if len(tags) < 2 or not evidence_request_id:
        return 0
    changed = 0
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    for observer in tags:
        for counterpart in tags:
            if observer == counterpart:
                continue
            pair = _ensure_pair(state, campaign_id, observer, counterpart)
            event_id = f"{agreement_id}:v{agreement_version}:{evidence_request_id}:{observer}:{counterpart}"
            if any(item.get("event_id") == event_id for item in pair["trust_events"]
                   if isinstance(item, Mapping)):
                continue
            before = pair["promise_trust"]
            after = min(1.0, round(before + 0.025, 4))
            event = {
                "event_id": event_id,
                "type": "agreement_effect_confirmed_in_game",
                "agreement_id": agreement_id,
                "agreement_version": agreement_version,
                "action_id": action_id,
                "evidence_request_id": evidence_request_id,
                "source": "authenticated_bridge_confirmed_hoi4_marker",
                "promise_trust_before": before,
                "promise_trust_after": after,
                "occurred_at": now,
            }
            pair["promise_trust"] = after
            pair["trust_events"].append(event)
            pair["confirmed_outcomes"].append({
                "event_id": event_id,
                "agreement_id": agreement_id,
                "agreement_version": agreement_version,
                "action_id": action_id,
                "status": "applied_in_game",
                "evidence_request_id": evidence_request_id,
                "source": "authenticated_bridge_confirmed_hoi4_marker",
            })
            pair["trust_events"] = pair["trust_events"][-MAX_OUTCOMES_PER_PAIR:]
            pair["confirmed_outcomes"] = pair["confirmed_outcomes"][-MAX_OUTCOMES_PER_PAIR:]
            pair["updated_at"] = now
            changed += 1
    return changed


def pair_summary(state: Mapping[str, Any], *, campaign_id: str,
                 observer_tag: str, counterpart_tag: str) -> dict[str, Any]:
    """Compact prompt-safe view; positions are discussion memory, outcomes are marker-backed facts."""
    observer = _normalize_tag(observer_tag, "observer_tag")
    counterpart = _normalize_tag(counterpart_tag, "counterpart_tag")
    key = _pair_key(campaign_id, observer, counterpart)
    pairs = state.get("pairs", {}) if isinstance(state, Mapping) else {}
    pair = pairs.get(key, {}) if isinstance(pairs, Mapping) else {}
    if not isinstance(pair, Mapping):
        pair = {}
    observations = pair.get("verified_game_observations", [])
    observations = observations if isinstance(observations, list) else []
    latest_observation = next((item for item in reversed(observations)
                               if isinstance(item, Mapping)), None)
    latest_facts = (latest_observation.get("facts", {})
                    if isinstance(latest_observation, Mapping)
                    and isinstance(latest_observation.get("facts"), Mapping) else {})
    crises = []
    if str(latest_facts.get("war_status", "")).lower() == "yes":
        crises.append("active_war")
    try:
        observed_threat = float(latest_facts.get("threat"))
    except (TypeError, ValueError):
        observed_threat = None
    if observed_threat is not None and observed_threat >= 0.6:
        crises.append("verified_high_threat")
    if isinstance(latest_facts.get("claims"), list) and latest_facts["claims"]:
        crises.append("verified_territorial_claims")
    positions = pair.get("positions", [])
    positions = positions if isinstance(positions, list) else []
    latest_position = next((item for item in reversed(positions)
                            if isinstance(item, Mapping)), None)
    stance = latest_position.get("stance") if isinstance(latest_position, Mapping) else None
    readiness = {
        "accept": "constructive_position_nonbinding",
        "conditional_acceptance": "open_with_conditions",
        "counter_offer": "continuing_negotiation",
        "discuss": "continuing_negotiation",
        "clarify": "information_requested",
        "reject": "recent_refusal_recorded",
        "defer": "position_deferred",
    }.get(stance, "no_validated_position_recorded")
    relation_value = latest_facts.get("relations")
    if isinstance(relation_value, str) and relation_value.lower() in {"unknown", "unavailable"}:
        relation_value = None
    return {
        "authority": "persistent_campaign_scoped_negotiation_memory",
        "campaign_id": campaign_id,
        "observer_tag": observer,
        "counterpart_tag": counterpart,
        "promise_trust": {
            "value": round(float(pair.get("promise_trust", 0.5)), 3),
            "verified_evidence_count": len(pair.get("trust_events", []))
            if isinstance(pair.get("trust_events"), list) else 0,
        },
        "prior_negotiation_positions": deepcopy(
            pair.get("positions", [])[-6:] if isinstance(pair.get("positions"), list) else []
        ),
        "verified_game_observations": deepcopy(
            pair.get("verified_game_observations", [])[-4:]
            if isinstance(pair.get("verified_game_observations"), list) else []
        ),
        "confirmed_game_outcomes": deepcopy(
            pair.get("confirmed_outcomes", [])[-4:]
            if isinstance(pair.get("confirmed_outcomes"), list) else []
        ),
        "relationship_observation": ({
            "value": relation_value,
            "direction": "observer_toward_counterpart",
            "game_date": latest_observation.get("game_date"),
            "evidence_request_id": latest_observation.get("evidence_request_id"),
            "source": latest_observation.get("source"),
        } if relation_value is not None and isinstance(latest_observation, Mapping) else None),
        "discussion_readiness": {
            "state": readiness,
            "source_request_id": latest_position.get("source_request_id")
            if isinstance(latest_position, Mapping) else None,
            "binding": False,
        },
        "crisis": {
            "state": "elevated" if crises else (
                "no_elevated_crisis_in_latest_snapshot" if isinstance(latest_observation, Mapping)
                else "unknown"
            ),
            "verified_reasons": crises,
            "evidence_request_id": latest_observation.get("evidence_request_id")
            if isinstance(latest_observation, Mapping) else None,
        },
        "model_hypotheses": [],
        "updated_at": pair.get("updated_at") or None,
        "limits": "Positions are nonbinding discussion history; outcomes require authenticated HOI4 markers. No motive or breach is inferred.",
    }
