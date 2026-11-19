"""Ideology and territorial-interest inputs for diplomatic negotiation.

This module is deliberately advisory. It can bias a country's negotiation score
and voice, but it cannot create assent, approve an agreement, or authorize a
game action. Missing ideology, claims, or state values stay unknown.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping


_TAG = re.compile(r"^[A-Z0-9]{3}$")
_CUSTOM_IDEOLOGY = re.compile(r"^[a-z][a-z0-9_]{1,59}$")

_IDEOLOGY_ALIASES = {
    "fascism": "fascism", "fascist": "fascism", "nazism": "fascism",
    "national_socialism": "fascism", "falangism": "fascism",
    "democratic": "democratic", "democracy": "democratic",
    "liberalism": "democratic", "conservatism": "democratic",
    "neutrality": "neutrality", "neutral": "neutrality", "non_aligned": "neutrality",
    "despotism": "neutrality", "oligarchism": "neutrality", "moderate": "neutrality",
    "communism": "communism", "communist": "communism", "stalinism": "communism",
    "marxism": "communism", "leninism": "communism", "anarchism": "communism",
}


@dataclass(frozen=True)
class DoctrineProfile:
    aggressiveness: float = 0.50
    trustworthiness: float = 0.50
    risk_tolerance: float = 0.50
    negotiation_willingness: float = 0.50
    values_alliances: float = 0.50
    dislikes_threats: float = 0.50
    territorial_ambition: float = 0.50
    influence_ambition: float = 0.50
    compromise_preference: float = 0.50
    security_priority: float = 0.50


_PROFILES = {
    "fascism": DoctrineProfile(
        0.80, 0.38, 0.76, 0.48, 0.50, 0.28,
        territorial_ambition=0.88, influence_ambition=0.56,
        compromise_preference=0.34, security_priority=0.48,
    ),
    "democratic": DoctrineProfile(
        0.24, 0.72, 0.38, 0.76, 0.84, 0.88,
        territorial_ambition=0.20, influence_ambition=0.38,
        compromise_preference=0.84, security_priority=0.82,
    ),
    "neutrality": DoctrineProfile(
        0.42, 0.53, 0.48, 0.60, 0.52, 0.68,
        territorial_ambition=0.34, influence_ambition=0.40,
        compromise_preference=0.60, security_priority=0.64,
    ),
    "communism": DoctrineProfile(
        0.64, 0.43, 0.66, 0.60, 0.72, 0.38,
        territorial_ambition=0.62, influence_ambition=0.88,
        compromise_preference=0.48, security_priority=0.68,
    ),
}


def normalize_ideology(value: Any) -> str | None:
    """Normalize an observed ideology, preserving safe mod-defined groups.

    HOI4's native government trigger has four broad groups, while mods may
    expose an additional ideology label. Unknown labels remain explicitly
    namespaced and receive a neutral prior; they are never guessed from tags.
    """
    if not isinstance(value, str):
        return None
    raw = value.strip()
    key = re.sub(r"[\s-]+", "_", raw.casefold())
    if key in {"", "unknown", "unavailable", "none", "null", "n/a"}:
        return None
    if key.startswith("modded:"):
        custom = key.split(":", 1)[1]
        return key if _CUSTOM_IDEOLOGY.fullmatch(custom) else None
    known = _IDEOLOGY_ALIASES.get(key)
    if known:
        return known
    if not _TAG.fullmatch(raw.upper()) and _CUSTOM_IDEOLOGY.fullmatch(key):
        return f"modded:{key}"
    return None


def ideology_from_context(context: Mapping[str, Any] | None) -> str | None:
    if not isinstance(context, Mapping):
        return None
    return normalize_ideology(context.get("ideology_group") or context.get("ideology"))


def profile_for_ideology(ideology: str | None) -> DoctrineProfile:
    """Return ideology priors or a neutral *unknown* prior without labeling it."""
    return _PROFILES.get(ideology, DoctrineProfile())


def _state_ids(raw: Any) -> set[int]:
    if not isinstance(raw, (list, tuple, set)):
        return set()
    result: set[int] = set()
    for item in raw:
        if isinstance(item, bool):
            continue
        try:
            state_id = int(item)
        except (TypeError, ValueError, OverflowError):
            continue
        if 1 <= state_id <= 10000:
            result.add(state_id)
    return result


def _valid_offer(raw: Any, sender: str, receiver: str) -> dict | None:
    if not isinstance(raw, Mapping):
        return None
    from_tag = str(raw.get("from_tag") or raw.get("from") or "").upper()
    to_tag = str(raw.get("to_tag") or raw.get("to") or "").upper()
    states = _state_ids(raw.get("state_ids"))
    if not states or not _TAG.fullmatch(from_tag) or not _TAG.fullmatch(to_tag):
        return None
    if (from_tag, to_tag) not in {(sender, receiver), (receiver, sender)}:
        return None
    direction = "sender_to_receiver" if from_tag == sender else "receiver_to_sender"
    return {"from_tag": from_tag, "to_tag": to_tag, "state_ids": states,
            "direction": direction, "voluntary": raw.get("voluntary") is True}


def _explicit_numeric_state_offer(text: Any, sender: str, receiver: str) -> dict | None:
    """Recognize only a direct, non-negated offer naming exact state IDs and recipient.

    This narrow rule runs before model analysis so a disputed-state concession can
    inform the first response. State names and IDs never get guessed from prose.
    """
    if not isinstance(text, str) or len(text) > 4000:
        return None
    state_pattern = re.compile(
        r"\b(?:state|province|region|штат|област(?:ь|і|і)|провинц(?:ия|ии)|"
        r"етат|провинц(?:ия|ии)|état|province|région)\s*(?:#|№)?\s*(\d{1,6})\b",
        re.IGNORECASE,
    )
    state_ids = _state_ids(state_pattern.findall(text))
    if not state_ids:
        return None
    offer_pattern = re.compile(
        r"\b(?:i|we)\s+(?:offer|transfer|cede|give|hand\s+over|propose\s+to\s+transfer|"
        r"propose\s+to\s+cede)|\b(?:предлагаю\s+(?:передать|уступить)|"
        r"предлагаем\s+(?:передать|уступить)|передаю|передаём|уступаю|уступаем|отдаю|отдаём)|"
        r"\b(?:je\s+(?:propose\s+de\s+)?(?:céder|transférer)|nous\s+(?:proposons\s+de\s+)?"
        r"(?:céder|transférer)|cède|transfère)|"
        r"\b(?:пропоную\s+передати|пропонуємо\s+передати|передаю|передаємо|поступаюся)",
        re.IGNORECASE,
    )
    match = offer_pattern.search(text)
    if not match:
        return None
    lead = text[max(0, match.start() - 48):match.start()].casefold()
    around = text[max(0, match.start() - 20):min(len(text), match.end() + 45)].casefold()
    if (re.search(r"\b(?:not|never|don't|do not|won't|would not|не|ніколи|не буду|"
                  r"ne|n'|jamais)\b", lead)
            or re.search(r"\b(?:would|could|might|maybe|если бы|мог бы|могла бы|"
                         r"можливо|якби|pourrait|pourrions)\b", around)):
        return None
    recipient_pattern = re.compile(
        r"\b(?:to\s+(?:you|your\s+country)|вам|вашій\s+країні|вашей\s+стране|"
        r"à\s+vous|à\s+votre\s+pays|вас|" + re.escape(receiver) + r")\b",
        re.IGNORECASE,
    )
    if not recipient_pattern.search(text):
        return None
    return {"from_tag": sender, "to_tag": receiver, "state_ids": state_ids,
            "direction": "sender_to_receiver", "voluntary": True}


def _offer_from_payload(payload: Mapping[str, Any], context: Mapping[str, Any],
                        sender: str, receiver: str, message: str = "") -> dict | None:
    offer = _valid_offer(context.get("territorial_offer"), sender, receiver)
    if offer:
        return offer
    offer = _valid_offer(payload.get("territorial_offer"), sender, receiver)
    if offer:
        return offer
    analyses = [payload.get(key) for key in (
        "message_analysis", "player_source_analysis", "grounded_player_analysis",
    )]
    for analysis in analyses:
        if not isinstance(analysis, Mapping):
            continue
        candidate = _valid_offer(analysis.get("territorial_offer"), sender, receiver)
        if candidate:
            return candidate
        actions = analysis.get("actions")
        if not isinstance(actions, list):
            continue
        for action in actions:
            if not isinstance(action, Mapping) or action.get("action_id") not in {
                "territory_partition", "political_annexation",
            }:
                continue
            candidate = _valid_offer(action, sender, receiver)
            if candidate:
                return candidate
            target = action.get("target")
            candidate = _valid_offer(target, sender, receiver)
            if candidate:
                return candidate
    source_text = payload.get("player_source_text")
    if not isinstance(source_text, str):
        source_text = message
    return _explicit_numeric_state_offer(source_text, sender, receiver)


def _claims(context: Mapping[str, Any], sender: str, receiver: str) -> list[dict]:
    raw_claims = context.get("claims")
    if not isinstance(raw_claims, list):
        return []
    result = []
    seen = set()
    for raw in raw_claims[:1000]:
        if not isinstance(raw, Mapping):
            continue
        claimant = str(raw.get("claimant_tag") or "").upper()
        owner = str(raw.get("owner_tag") or "").upper()
        state_ids = _state_ids([raw.get("state_id")])
        if (claimant not in {sender, receiver} or owner not in {sender, receiver}
                or claimant == owner or not state_ids):
            continue
        state_id = next(iter(state_ids))
        key = (claimant, owner, state_id)
        if key in seen:
            continue
        seen.add(key)
        raw_value = raw.get("strategic_value_proxy")
        try:
            value = float(raw_value)
        except (TypeError, ValueError, OverflowError):
            value = None
        if value is not None and (not math.isfinite(value) or not 0.0 <= value <= 1.0):
            value = None
        result.append({
            "claimant_tag": claimant, "owner_tag": owner, "state_id": state_id,
            "strategic_value_proxy": value,
            "value_basis": str(raw.get("value_basis") or "unknown")[:80],
        })
    return result


def assess_doctrine(country_tag: str, context: Mapping[str, Any] | None,
                    sender_tag: str, message: str,
                    payload: Mapping[str, Any] | None = None) -> dict:
    """Build a bounded style prior and evidence-linked territorial adjustment."""
    context = context if isinstance(context, Mapping) else {}
    payload = payload if isinstance(payload, Mapping) else {}
    country = str(country_tag or "").upper()
    sender = str(sender_tag or "").upper()
    ideology = ideology_from_context(context)
    sender_ideology = normalize_ideology(
        context.get("player_ideology_group") or context.get("sender_ideology_group")
    )
    profile = profile_for_ideology(ideology)
    claims = _claims(context, sender, country)
    offer = _offer_from_payload(payload, context, sender, country, message)

    receiver_claims_player = [row for row in claims
                              if row["claimant_tag"] == country and row["owner_tag"] == sender]
    transfer_to_receiver = []
    transfer_from_receiver = []
    if offer:
        for row in claims:
            if row["state_id"] not in offer["state_ids"]:
                continue
            if (offer["direction"] == "sender_to_receiver"
                    and row["claimant_tag"] == country and row["owner_tag"] == sender):
                transfer_to_receiver.append(row)
            elif (offer["direction"] == "receiver_to_sender"
                  and row["owner_tag"] == country and row["claimant_tag"] == sender):
                transfer_from_receiver.append(row)

    score_adjustment = 0.0
    factors: list[str] = []
    if receiver_claims_player:
        factors.append("verified_receiver_claims_on_player_territory")
        if not transfer_to_receiver:
            score_adjustment -= 0.05
            factors.append("unaddressed_territorial_claims_reduce_willingness")
    if transfer_to_receiver:
        average_value = sum(
            row["strategic_value_proxy"] if row["strategic_value_proxy"] is not None else 0.5
            for row in transfer_to_receiver
        ) / len(transfer_to_receiver)
        score_adjustment += min(0.14, 0.05 + 0.09 * average_value)
        factors.append("verified_claimed_territory_offered_to_receiver")
        if any(row["strategic_value_proxy"] is not None
               and row["strategic_value_proxy"] >= 0.70 for row in transfer_to_receiver):
            factors.append("high_value_claimed_territory_offered")
        score_adjustment += (profile.territorial_ambition - 0.5) * 0.08
    if transfer_from_receiver:
        average_value = sum(
            row["strategic_value_proxy"] if row["strategic_value_proxy"] is not None else 0.5
            for row in transfer_from_receiver
        ) / len(transfer_from_receiver)
        score_adjustment -= min(0.12, 0.04 + 0.08 * average_value)
        factors.append("verified_claimed_territory_requested_from_receiver")
        if any(row["strategic_value_proxy"] is not None
               and row["strategic_value_proxy"] >= 0.70 for row in transfer_from_receiver):
            factors.append("high_value_claimed_territory_requested_from_receiver")
        score_adjustment -= (profile.territorial_ambition - 0.5) * 0.07

    intent = str(payload.get("intent") or "").lower()
    # Ideology changes the diplomatic prior; the facts and terms still determine
    # whether this particular proposal is attractive.
    if intent in {"non_aggression", "peace", "ceasefire", "guarantee"}:
        score_adjustment += (profile.security_priority - 0.5) * 0.12
        score_adjustment += (profile.compromise_preference - 0.5) * 0.05
    elif intent in {"demand", "territory_partition", "political_annexation"}:
        score_adjustment -= (profile.compromise_preference - 0.5) * 0.08
        if intent == "political_annexation":
            score_adjustment += (profile.territorial_ambition - 0.5) * 0.18
            score_adjustment += (profile.influence_ambition - 0.5) * 0.08
    elif intent in {"alliance", "faction_join", "military_access"}:
        score_adjustment += (profile.values_alliances - 0.5) * 0.10
        score_adjustment += (profile.security_priority - 0.5) * 0.05
    elif intent in {"offer", "negotiate", "request", "counter_offer"}:
        score_adjustment += (profile.negotiation_willingness - 0.5) * 0.08

    # Compare verified ideologies only when both labels are present. A broad
    # ideological difference raises the political cost of an alliance or a
    # sovereignty transfer, but the smaller NAP penalty leaves room for a
    # pragmatic security pact. Neutral and unknown/modded groups are never
    # treated as automatic enemies.
    known_groups = {"fascism", "democratic", "neutrality", "communism"}
    if ideology and sender_ideology:
        same_known = ideology == sender_ideology and ideology in known_groups
        custom_match = ideology == sender_ideology and ideology.startswith("modded:")
        opposed = (
            (ideology == "democratic" and sender_ideology in {"fascism", "communism"})
            or (sender_ideology == "democratic" and ideology in {"fascism", "communism"})
            or {ideology, sender_ideology} == {"fascism", "communism"}
        )
        if same_known or custom_match:
            if intent in {"alliance", "faction_join"}:
                score_adjustment += 0.07 if same_known else 0.035
                factors.append("ideological_alignment_supports_cooperation")
            elif intent in {"non_aggression", "peace", "ceasefire", "guarantee"}:
                score_adjustment += 0.02 if same_known else 0.01
                factors.append("ideological_alignment_supports_limited_agreement")
            elif intent == "political_annexation":
                score_adjustment += 0.035 if same_known else 0.015
                factors.append("ideological_alignment_lowers_political_friction")
        elif opposed:
            if intent in {"alliance", "faction_join"}:
                score_adjustment -= 0.12
                factors.append("ideological_opposition_raises_alliance_risk")
            elif intent in {"non_aggression", "peace", "ceasefire", "guarantee"}:
                score_adjustment -= 0.055 if ideology == "democratic" else 0.035
                factors.append("ideological_opposition_raises_security_caution")
            elif intent in {"political_annexation", "demand", "territory_partition"}:
                score_adjustment -= 0.14 if intent == "political_annexation" else 0.075
                factors.append("ideological_opposition_resists_sovereignty_change")
        elif ideology.startswith("modded:") or sender_ideology.startswith("modded:"):
            # Two distinct custom labels do not imply hostility; without an
            # explicit relationship rule, leave their compatibility unknown.
            factors.append("modded_ideology_compatibility_unknown")

    def observed_major(name: str) -> bool | None:
        raw = context.get(name)
        if raw is True or str(raw).strip().lower() in {"yes", "true", "1"}:
            return True
        if raw is False or str(raw).strip().lower() in {"no", "false", "0"}:
            return False
        return None

    receiver_major = observed_major("major_power")
    sender_major = observed_major("player_major_power")
    if receiver_major is not None or sender_major is not None:
        factors.append("verified_major_power_status")
        if intent == "political_annexation":
            if receiver_major:
                score_adjustment -= 0.08
                factors.append("major_power_resists_sovereignty_loss")
            if sender_major:
                score_adjustment -= 0.035
                factors.append("major_power_initiator_raises_annexation_risk")
        elif intent in {"demand", "territory_partition"}:
            if receiver_major:
                score_adjustment -= 0.045
                factors.append("major_power_resists_territorial_demand")
        elif intent in {"non_aggression", "peace", "ceasefire"}:
            if receiver_major and sender_major:
                score_adjustment += 0.015
                factors.append("major_power_security_pact_has_mutual_value")
            elif receiver_major or sender_major:
                factors.append("major_power_status_changes_security_calculus")

    # These observations already feed DiplomaticRelationship's shared decision
    # model. Record them as explicit factors here so doctrine does not pretend
    # to replace military balance, current war status, alliances, or relations.
    if context.get("_bridge_verified") is True:
        war_status = str(context.get("war_status", context.get("at_war", "unknown"))).lower()
        if war_status in {"yes", "no", "true", "false", "1", "0"}:
            factors.append("verified_current_war_status")
        same_faction = str(context.get("same_faction", context.get("faction_status", "unknown"))).lower()
        if same_faction in {"yes", "no", "true", "false", "1", "0"}:
            factors.append("verified_current_faction_status")
        for key in ("relations", "threat", "military_balance", "alliance_status",
                    "major_power", "player_major_power", "existing_non_aggression_pact"):
            if key in context and context[key] is not None and str(context[key]).lower() not in {
                    "unknown", "unavailable", "none", ""}:
                factors.append(f"verified_context_{key}")

    score_adjustment = max(-0.24, min(0.24, score_adjustment))
    if ideology and ideology.startswith("modded:"):
        factors.append("observed_modded_ideology_neutral_prior")
    elif ideology:
        factors.append(f"observed_{ideology}_doctrine")
    else:
        factors.append("ideology_unknown_generic_pragmatism")
    strategic_styles = {
        "fascism": "prioritize state strength and territorial leverage; bargain when it serves national advantage",
        "democratic": "prefer negotiated compromise, collective security, and peaceful solutions",
        "neutrality": "favor pragmatic, limited, reciprocal arrangements",
        "communism": "seek security and political influence; consider territorial or client-state arrangements when advantageous",
        "unknown": "use pragmatic diplomacy without assuming an ideology",
    }
    strategic_style = strategic_styles.get(
        ideology or "unknown", "use pragmatic diplomacy; this mod-defined ideology has no configured doctrine prior",
    )
    return {
        "ideology_group": ideology or "unknown",
        "sender_ideology_group": sender_ideology or "unknown",
        "ideology_observed": ideology is not None,
        "profile": asdict(profile),
        "score_adjustment": score_adjustment,
        "factors": list(dict.fromkeys(factors)),
        "claims": claims,
        "territorial_offer": ({
            "from_tag": offer["from_tag"], "to_tag": offer["to_tag"],
            "state_ids": sorted(offer["state_ids"]), "direction": offer["direction"],
        } if offer else None),
        "strategic_style": strategic_style,
        "authority": "advisory_negotiation_prior_only",
    }


__all__ = [
    "DoctrineProfile", "assess_doctrine", "ideology_from_context",
    "normalize_ideology", "profile_for_ideology",
]
