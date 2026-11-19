"""EYES authority/scope adapter for selected MIT Voices implementations.

The imported code prepares context and review work. Neither it nor this module
can choose a binding position, call a model, approve a treaty or send an effect.
"""
from copy import deepcopy
import re
import threading

from voices_history.context import diplomatic_context
from voices_history.strategist import Strategist

_REVIEW_LOCK = threading.RLock()


def counterpart_brief(observer, counterpart, context, doctrine=None):
    """Use the actual Voices formatter, with only EYES's filtered pair facts."""
    from strategic_interests import select_context
    if (not isinstance(observer, str) or not isinstance(counterpart, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9]{2}", observer)
            or not re.fullmatch(r"[A-Z][A-Z0-9]{2}", counterpart) or observer == counterpart):
        return None
    clean, provenance = select_context(context)
    def flag(value):
        if value is True or (isinstance(value, str) and value.lower() in {"yes", "true", "1"}):
            return "1"
        if value is False or (isinstance(value, str) and value.lower() in {"no", "false", "0"}):
            return "0"
        return "unknown"
    nation = {"tag": observer, "name": observer,
              "date": clean.get("date", "unknown"),
              "waryou": flag(clean.get("war_status")),
              "facyou": flag(clean.get("same_faction"))}
    doctrine = doctrine if isinstance(doctrine, dict) else {}
    if doctrine.get("ideology_observed") is True and "ideology" not in provenance["excluded"]:
        nation["ideology_name"] = doctrine.get("ideology_group", "Unknown")
    # Do not promote arbitrary model/player characterization into live facts.
    if context.get("_bridge_verified") is True:
        nation["maj"] = flag(clean.get("major_power"))
        if isinstance(clean.get("leader_name"), str):
            nation["leader"] = clean["leader_name"][:120]
    state = {"tag": counterpart, "name": counterpart, "date": clean.get("date", "unknown")}
    text = diplomatic_context(state, {observer: nation}, {"target": observer, "player": counterpart})
    return {"origin": "voices_history.game_state.diplomatic_context",
            "observer_tag": observer, "counterpart_tag": counterpart,
            "text": text, "provenance": provenance,
            "authority": "context_only_not_a_decision"}


def schedule_reviews(memory, **kwargs):
    with _REVIEW_LOCK:
        return _schedule_reviews(memory, **kwargs)


def _schedule_reviews(memory, *, campaign_id, player_tag, campaign_day, world,
                     historical, enabled=False, verified=False, active_count=0,
                     countries=None, initial_review=False, historical_review_authorized=False):
    """Queue at most one bounded strategic review; no autonomous chat delivery."""
    from strategic_interests import select_context, _flag, _day
    blocked = {"status": "blocked", "reason": "historical_or_unverified_context"}
    historical_allowed = historical is False or (historical is True and historical_review_authorized is True)
    if (not historical_allowed or enabled is not True or verified is not True
            or type(active_count) is not int or active_count != 0
            or not isinstance(campaign_id, str) or not re.fullmatch(r"[a-f0-9]{32}", campaign_id)
            or _day(campaign_day) is None or not isinstance(world, dict)
            or (player_tag is not None and (not isinstance(player_tag, str)
                or not re.fullmatch(r"[A-Z][A-Z0-9]{2}", player_tag)))):
        return blocked
    selected = list(world) if countries is None else countries
    if (not isinstance(selected, list) or not 1 <= len(selected) <= 6
            or any(not isinstance(tag, str) or not re.fullmatch(r"[A-Z][A-Z0-9]{2}", tag) for tag in selected)
            or len(set(selected)) != len(selected)):
        return {"status": "blocked", "reason": "invalid_country_selection"}
    filtered = {}
    for actor in selected:
        nation = world.get(actor)
        if not isinstance(nation, dict) or actor == player_tag:
            continue
        if (nation.get("verified") is not True or nation.get("ai_controlled") is not True
                or type(nation.get("day")) is not int or nation["day"] != campaign_day
                or nation.get("capitulated") is True or not isinstance(nation.get("context"), dict)):
            continue
        clean, provenance = select_context({**nation["context"], "campaign_day": campaign_day})
        if (provenance["excluded"] or _flag(clean.get("war_status")) is None
                or _flag(clean.get("same_faction")) is None):
            continue
        filtered[actor] = {k: deepcopy(nation.get(k)) for k in ("day", "verified", "ai_controlled", "capitulated")}
    if not filtered:
        return {"status": "blocked", "reason": "no_fresh_country_context"}
    scheduler = Strategist(memory.setdefault("strategic_reviews", {}))
    review = scheduler.due({"gid": campaign_id, "day": campaign_day, "tag": player_tag}, filtered,
                           sorted(filtered), initial_review=initial_review)
    if review is None:
        return {"status": "not_due", "reason": "cooldown_pending_or_reload"}
    return {"status": "prepared_review", "review": review, "binding": False, "delivered": False}


def finish_review(memory, review, summary, status="completed"):
    """Complete only the exact pending reservation, not a diplomatic agreement."""
    with _REVIEW_LOCK:
        return Strategist(memory.setdefault("strategic_reviews", {})).finish(review, summary, status)


def review_history(memory, campaign_id, country_tag, campaign_day):
    from strategic_interests import _day
    if _day(campaign_day) is None:
        return []
    with _REVIEW_LOCK:
        return Strategist(memory.get("strategic_reviews", {})).history(campaign_id, country_tag, campaign_day)
