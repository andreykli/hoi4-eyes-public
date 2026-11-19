"""Bounded strategic memory and context provenance; no model or game execution.

Original EYES implementation, informed by the Voices of History research.
No upstream code/assets or new dependencies are incorporated.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re
from collections.abc import Mapping

AUTHORITY = "engine_country_interests"
FACTS = frozenset({"war_status", "same_faction", "existing_non_aggression_pact",
    "relations", "opinion", "threat", "military_balance", "economic_dependence",
    "claims", "independent", "ideology", "major_power", "player_major_power", "country_at_war"})
SOURCES = {"authenticated_bridge", "bridge", "verified_game_marker", "save_snapshot"}


def _day(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _flag(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return {"yes": True, "true": True, "no": False, "false": False}.get(value.lower())
    return None


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), default=str).encode()).hexdigest()[:24]


def select_context(context):
    """Legacy fields remain labelled legacy; explicit bad provenance fails closed.

    This checks metadata, not its authenticity. The caller must authenticate the
    Bridge as before; user/LLM supplied metadata cannot grant that capability.
    """
    result = deepcopy(dict(context))
    if "campaign_day" not in result and context.get("game_day_basis") == "global.num_days":
        raw_day = str(context.get("game_day", ""))
        if raw_day.isascii() and raw_day.isdecimal() and len(raw_day) <= 7:
            result["campaign_day"] = int(raw_day)
    metadata = context.get("fact_metadata", {})
    now = _day(result.get("campaign_day"))
    evidence, excluded = {}, {}
    for field in sorted(FACTS):
        if field not in result:
            continue
        info = metadata.get(field) if isinstance(metadata, Mapping) else None
        declared = field in metadata if isinstance(metadata, Mapping) else True
        reason = None
        if declared:
            if not isinstance(info, Mapping) or info.get("source") not in SOURCES:
                reason = "unverified_source"
            elif info.get("conflicting") is True:
                reason = "conflicting_observations"
            else:
                observed = _day(info.get("observed_day"))
                if now is None or observed is None:
                    reason = "freshness_unknown"
                elif observed > now:
                    reason = "future_observation"
                elif now - observed > 7:
                    reason = "stale_observation"
            evidence[field] = deepcopy(dict(info)) if isinstance(info, Mapping) else {}
        else:
            evidence[field] = {"source": "legacy_context", "freshness": "not_attested"}
        if reason:
            result.pop(field, None)
            excluded[field] = reason
    return result, {"fields": evidence, "excluded": excluded}


def normalize_countries(value):
    result = {}
    if not isinstance(value, Mapping):
        return result
    for key, row in value.items():
        if not isinstance(row, Mapping):
            continue
        campaign, tag = row.get("campaign_id"), row.get("country_tag")
        if (not isinstance(campaign, str) or not re.fullmatch(r"[a-f0-9]{32}", campaign)
                or not isinstance(tag, str) or not re.fullmatch(r"[A-Z][A-Z0-9]{2}", tag)
                or key != f"{campaign}:{tag}" or row.get("authority") != AUTHORITY):
            continue
        # Reconstruct goals from engine inputs; persisted prose cannot become a rule.
        inputs = row.get("inputs", {})
        if not isinstance(inputs, Mapping):
            continue
        inputs = {k: deepcopy(v) for k, v in inputs.items() if k in FACTS}
        goals = _goals(inputs)
        result[key] = {"campaign_id": campaign, "country_tag": tag, "authority": AUTHORITY,
            "inputs": inputs, "goals": goals, "goals_id": digest(goals),
            "reviewed_day": _day(row.get("reviewed_day")),
            "last_initiative_day": _day(row.get("last_initiative_day")),
            "revision": max(1, int(row.get("revision", 1))) if type(row.get("revision", 1)) is int else 1,
            "revisions": deepcopy(row.get("revisions", [])[-8:]) if isinstance(row.get("revisions"), list) else []}
    return result


def _goals(inputs):
    # Policy preferences, explicitly not observations about the game world.
    rows = [{"goal": "preserve_independence", "weight": 0.9,
             "basis": "engine_policy_preference"},
            {"goal": "border_security", "weight": 0.65, "basis": "engine_policy_preference"},
            {"goal": "avoid_costly_war", "weight": 0.6, "basis": "engine_policy_preference"}]
    if _flag(inputs.get("country_at_war")) is True:
        rows.append({"goal": "reduce_active_conflict", "weight": 0.9, "basis": "country_at_war"})
    threat = _number(inputs.get("threat"))
    if threat is not None and threat >= 0.65:
        rows.append({"goal": "limit_exposure", "weight": 0.85, "basis": "threat"})
    # Pair-specific faction and economic inputs affect a round, not a global goal.
    # This avoids oscillating a country's agenda when different partners speak.
    if _flag(inputs.get("major_power")) is True:
        rows.append({"goal": "maintain_influence", "weight": 0.55, "basis": "major_power"})
    return rows


def refresh_interests(memory, campaign_id, country_tag, context):
    """Update the existing memory object only for valid campaign/country scopes."""
    if (not re.fullmatch(r"[a-f0-9]{32}", campaign_id or "")
            or not re.fullmatch(r"[A-Z][A-Z0-9]{2}", country_tag or "")):
        return None
    countries = memory.setdefault("strategic_countries", {})
    key = f"{campaign_id}:{country_tag}"
    old = countries.get(key)
    clean, provenance = select_context(context)
    day = _day(clean.get("campaign_day"))
    if old and day is not None and old.get("reviewed_day") is not None and day < old["reviewed_day"]:
        # Retain historical records, but never project future goals into an old save.
        goals = _goals({})
        return {"authority": AUTHORITY, "goals": goals, "goals_id": digest(goals),
                "review_reason": "timeline_context_requires_refresh", "provenance": provenance}
    inputs = deepcopy(old.get("inputs", {})) if old else {}
    # Only world/country-level facts belong here. Pair facts remain round-local.
    global_fields = {"country_at_war", "major_power"}
    for field in global_fields:
        if field in clean:
            inputs[field] = deepcopy(clean[field])
        elif field in provenance["excluded"]:
            inputs.pop(field, None)
    goals = _goals(inputs)
    goal_id = digest(goals)
    material = not old or old.get("inputs") != inputs
    weekly = old and day is not None and (old.get("reviewed_day") is None or day - old["reviewed_day"] >= 7)
    if material or weekly:
        changed = not old or old.get("goals_id") != goal_id
        revision = (old.get("revision", 1) + int(changed)) if old else 1
        history = deepcopy(old.get("revisions", [])) if old else []
        if changed:
            history.append({"revision": revision, "day": day, "goals_id": goal_id,
                            "reason": "initial_policy" if not old else "changed_confirmed_context"})
        countries[key] = {"campaign_id": campaign_id, "country_tag": country_tag,
            "authority": AUTHORITY, "inputs": inputs, "goals": goals, "goals_id": goal_id,
            "reviewed_day": day, "revision": revision, "revisions": history[-8:],
            "last_initiative_day": old.get("last_initiative_day") if old else None}
    result = deepcopy(countries[key])
    result["provenance"] = provenance
    from voices_adapter import review_history
    result["recent_reviews"] = review_history(memory, campaign_id, country_tag, day)
    result["review_memory_authority"] = "review_summaries_are_not_confirmed_game_facts"
    return result


def goal_adjustments(interests, intent, context):
    adjustments = {"accept": 0.0, "reject": 0.0, "counter_offer": 0.0, "defer": 0.0}
    reasons = []
    if not isinstance(interests, Mapping) or interests.get("authority") != AUTHORITY:
        return adjustments, reasons
    goals = {r.get("goal") for r in interests.get("goals", []) if isinstance(r, Mapping)}
    if "preserve_independence" in goals and intent in {"political_annexation", "demand", "territory_partition"}:
        adjustments["reject"] += 0.06
        reasons.append("preserve_independence")
    if "avoid_costly_war" in goals and intent in {"non_aggression", "ceasefire", "white_peace", "truce", "peace"}:
        adjustments["accept"] += 0.025
        reasons.append("avoid_costly_war")
    if "reduce_active_conflict" in goals and intent in {"ceasefire", "white_peace", "truce", "peace"}:
        adjustments["accept"] += 0.04
        reasons.append("reduce_active_conflict")
    if _flag(context.get("same_faction")) is True and intent == "guarantee":
        adjustments["accept"] += 0.06
        reasons.append("support_observed_ally")
    return adjustments, reasons


def leader_style(context):
    """Only attested current-leader style codes, never historical guesses or prose."""
    row = context.get("leader_style")
    if not isinstance(row, Mapping) or not context.get("leader_id") or row.get("leader_id") != context["leader_id"]:
        return []
    day, observed = _day(context.get("campaign_day")), _day(row.get("observed_day"))
    if row.get("source") not in SOURCES or day is None or observed is None or not 0 <= day - observed <= 7:
        return []
    allowed = {"formal", "concise", "cautious", "direct", "conciliatory"}
    return sorted({v for v in row.get("styles", []) if isinstance(v, str) and v in allowed})[:2] if isinstance(row.get("styles"), list) else []


def initiative_review(memory, *, campaign_id, country_tag, campaign_day,
                      historical, enabled=False, verified=False, ai_controlled=False,
                      context=None, active_count=0):
    """Offline proposal scheduler only. No chat, model call or game effect is sent."""
    blocked = {"status": "blocked", "reason": "historical_or_unverified_context"}
    if (historical is not False or enabled is not True or verified is not True
            or ai_controlled is not True or _day(campaign_day) is None
            or type(active_count) is not int or active_count != 0):
        return blocked
    clean, provenance = select_context({**(context or {}), "campaign_day": campaign_day})
    if (provenance["excluded"] or _flag(clean.get("war_status")) is None
            or _flag(clean.get("same_faction")) is None):
        return blocked
    state = refresh_interests(memory, campaign_id, country_tag, clean)
    if not state or state.get("review_reason"):
        return blocked
    row = memory["strategic_countries"][f"{campaign_id}:{country_tag}"]
    last = row.get("last_initiative_day")
    from voices_adapter import schedule_reviews, finish_review
    # Migrate only the prior cooldown, never fabricate a past negotiation.
    ledger = memory.setdefault("strategic_reviews", {}).setdefault(campaign_id,
        {"baselines": {}, "reviews": [], "previous": None})
    if last is not None and not any(r["actor"] == country_tag for r in ledger["reviews"]):
        ledger["baselines"][country_tag] = last
        ledger["reviews"].append({"actor": country_tag, "day": last, "signature": "week:" + str(last // 7),
            "reason": "Imported EYES review cooldown", "summary": "", "status": "legacy_migrated"})
    scheduled = schedule_reviews(memory, campaign_id=campaign_id, player_tag=None,
        campaign_day=campaign_day, historical=historical, enabled=enabled, verified=verified,
        active_count=active_count, countries=[country_tag], initial_review=True,
        world={country_tag: {"context": clean, "day": campaign_day,
                            "verified": verified, "ai_controlled": ai_controlled}})
    if scheduled["status"] != "prepared_review":
        return scheduled
    finish_review(memory, scheduled["review"], json.dumps({"goals_id": state["goals_id"],
                  "status": "nonbinding_candidate", "delivered": False}, sort_keys=True))
    row["last_initiative_day"] = campaign_day
    return {"status": "candidate_for_review", "country_tag": country_tag,
            "goals_id": state["goals_id"], "review": scheduled["review"],
            "binding": False, "delivered": False}
