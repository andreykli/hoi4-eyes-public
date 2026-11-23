"""Deterministic, explainable strategy selection for HOI4 diplomacy.

The planner compares a small set of permissible conversational actions before
Ollama writes the country's expression. It is intentionally not an agreement
engine: selected positions remain nonbinding, and agreement confirmation,
player approval, game effects, and result markers stay in their existing
systems.

Only values passed by the caller are considered. Missing or malformed facts
stay unknown and increase the value of discussion for proposals whose risk
cannot be assessed.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Mapping, TypedDict, Literal
from strategic_interests import select_context, goal_adjustments
from diplomatic_language import canonical_period, period, localized_period


def point_policy_context_hash(context):
    """Compare substantive observations, not the arrival date of identical facts.

    Freshness/contradiction checks still run before policy selection. A changed
    war, relation, threat, ideology or unknown field changes this fingerprint.
    """
    facts = {k: v for k, v in context.items() if k not in {'date', 'game_day', 'campaign_day', 'observed_day'}}
    return hashlib.sha256(json.dumps(facts, sort_keys=True, default=str).encode()).hexdigest()


class PointDecision(TypedDict, total=False):
    policy_schema_version: int
    point: int
    action_id: str
    term_id: str | None
    term_clause: str
    duration: str | None
    stance: Literal["accept", "reject", "counter_offer", "discuss", "clarify"]
    proposed_duration_years: int
    scope_fingerprint: str
    policy_request_id: str
    campaign_id: str
    conversation_id: str
    speaker_tag: str
    binding: bool
    consent_status: str


PLANNER_VERSION = 2
AUTHORITY = "advisory_engine_strategic_plan"
_OFFER_INTENTS = {
    "offer", "negotiate", "demand", "alliance", "non_aggression", "trade",
    "military_access", "guarantee", "embargo", "lift_embargo", "docking_rights",
    "air_base_access", "faction_join", "faction_leave", "market_access", "territory_partition",
    "political_annexation", "truce", "white_peace", "peace", "ceasefire",
}
_VALID_ACTIONS = {"accept", "counter_offer", "reject", "defer"}


def _known(value: Any) -> bool:
    return value is not None and not (
        isinstance(value, str)
        and value.strip().casefold() in {"", "unknown", "unavailable", "none", "null", "n/a"}
    )


def _number(value: Any, default: float, low: float, high: float) -> float:
    try:
        if isinstance(value, bool):
            return default
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(number):
        return default
    return min(high, max(low, number))


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        key = value.strip().casefold()
        if key in {"yes", "true", "1", "at_war", "allied", "same_faction"}:
            return True
        if key in {"no", "false", "0", "at_peace", "not_allied", "different_faction"}:
            return False
    return None


def _ideology_relation(own: str, other: str, intent: str) -> float:
    """Small issue-specific prior; unknown/modded ideologies are not guessed."""
    known_groups = {"fascism", "democratic", "communism", "neutrality"}
    if own not in known_groups or other not in known_groups:
        return 0.0
    if own == other:
        return 0.08 if intent in {"alliance", "faction_join", "guarantee"} else 0.025
    opposed = {own, other} in ({"democratic", "fascism"}, {"democratic", "communism"},
                               {"fascism", "communism"})
    if intent in {"non_aggression", "peace", "ceasefire", "truce", "white_peace"}:
        return -0.025 if opposed else -0.01
    if intent in {"political_annexation", "faction_join", "alliance"}:
        return -0.10 if opposed else -0.035
    return -0.04 if opposed else -0.015


def _memory_signal(memory: Mapping[str, Any] | None) -> tuple[float, list[str], str]:
    """Use only engine-validated nonbinding positions and marker-confirmed trust."""
    if not isinstance(memory, Mapping) or memory.get("authority") != "persistent_campaign_scoped_negotiation_memory":
        return 0.0, [], "unknown"
    reasons: list[str] = []
    readiness = "unknown"
    ready = memory.get("discussion_readiness")
    if isinstance(ready, Mapping):
        readiness = str(ready.get("state") or "unknown")[:60]
    positions = memory.get("prior_negotiation_positions")
    if isinstance(positions, list):
        latest = next((row for row in reversed(positions)
                       if isinstance(row, Mapping)
                       and row.get("authority") == "engine_validated_negotiation_position"), None)
        if latest:
            stance = str(latest.get("stance") or "")
            if stance == "reject":
                reasons.append("prior_validated_refusal_is_relevant_but_not_permanent"); signal = -0.025
            elif stance in {"accept", "conditional_acceptance"}:
                reasons.append("prior_validated_constructive_position_is_nonbinding"); signal = 0.015
            elif stance == "counter_offer":
                reasons.append("prior_validated_counteroffer_supports_further_bargaining"); signal = 0.01
            else:
                signal = 0.0
            return signal, reasons, readiness
    return 0.0, reasons, readiness


def build_strategic_plan(*, intent: str, engine_decision: str,
                         relationship: Mapping[str, Any],
                         game_context: Mapping[str, Any] | None = None,
                         doctrine: Mapping[str, Any] | None = None,
                         profile: Mapping[str, Any] | None = None,
                         memory: Mapping[str, Any] | None = None,
                         campaign_id: str = "", conversation_id: str = "",
                         request_id: str = "", allow_override: bool = False,
                         interests: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Score accept/counter/reject/discuss, returning a bounded explainable plan.

    ``allow_override`` is used only for a fresh bilateral proposal. In ongoing
    negotiations or group agreements, the existing pointwise/voting machinery
    retains precedence, while the same candidate comparison is still exposed
    as a nonbinding advisory plan.
    """
    intent = str(intent or "unknown/general")
    raw_base = str(engine_decision or "defer")[:40]
    base = raw_base if raw_base in _VALID_ACTIONS else "defer"
    context, provenance = select_context(game_context if isinstance(game_context, Mapping) else {})
    rel = dict(relationship) if isinstance(relationship, Mapping) else {}
    for field in provenance["excluded"]:
        rel.pop(field, None)
    doctrine = doctrine if isinstance(doctrine, Mapping) else {}
    profile = profile if isinstance(profile, Mapping) else {}
    memory_signal, memory_reasons, memory_readiness = _memory_signal(memory)

    fingerprint_source = json.dumps({
        "campaign": campaign_id, "conversation": conversation_id, "request": request_id,
        "intent": intent, "base": raw_base, "context": dict(context),
        "relationship": dict(rel), "doctrine": dict(doctrine), "profile": dict(profile),
        "interests": dict(interests) if isinstance(interests, Mapping) else None,
        "provenance": provenance,
        "trusted_memory": (dict(memory) if isinstance(memory, Mapping)
                           and memory.get("authority") == "persistent_campaign_scoped_negotiation_memory"
                           else None),
    }, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    plan_id = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()[:24]

    if intent not in _OFFER_INTENTS:
        return {
            "planner_version": PLANNER_VERSION, "plan_id": plan_id,
            "authority": AUTHORITY, "mode": "non_proposal",
            "selected_action": raw_base, "engine_decision": raw_base,
            "decision_source": "existing_engine_policy",
            "applied_to_engine_decision": False,
            "candidate_scores": [], "evidence": [], "unknown_inputs": [],
            "memory_signal": memory_readiness,
            "safety": "does_not_create_agreement_or_authorize_game_effects",
        }

    relation_known = _known(context.get("relations"))
    opinion_known = _known(context.get("opinion"))
    trust_known = _known(context.get("trust")) or relation_known
    threat_known = _known(context.get("threat"))
    war = _bool(rel.get("war_status", context.get("war_status", context.get("at_war"))))
    same_faction = _bool(rel.get("same_faction", context.get("same_faction", context.get("faction_status"))))
    existing_nap = _bool(rel.get("existing_non_aggression_pact", context.get("existing_non_aggression_pact")))
    alliance = _bool(rel.get("alliance_status", context.get("alliance_status")))
    relations = _number(rel.get("relations", context.get("relations")), 0.0, -100.0, 100.0)
    opinion = _number(rel.get("opinion", context.get("opinion", relations)), relations, -100.0, 100.0)
    trust = _number(rel.get("trust", context.get("trust")), 0.5, 0.0, 1.0)
    promise_trust = _number(rel.get("promise_trust"), 0.5, 0.0, 1.0)
    threat = _number(rel.get("threat", context.get("threat")), 0.0, 0.0, 1.0)
    conflict = _number(rel.get("recent_conflict", context.get("recent_conflict")), 0.0, 0.0, 1.0)
    dependence = _number(rel.get("economic_dependence", context.get("economic_dependence")), 0.0, 0.0, 1.0)
    balance = _number(rel.get("military_balance", context.get("military_balance")), 0.0, -1.0, 1.0)

    own_ideology = str(doctrine.get("ideology_group") or "unknown")
    other_ideology = str(doctrine.get("sender_ideology_group") or "unknown")
    if doctrine.get("ideology_observed") is not True:
        own_ideology = "unknown"
    ideology_signal = _ideology_relation(own_ideology, other_ideology, intent)
    willingness = _number(profile.get("negotiation_willingness"), 0.5, 0.0, 1.0)
    compromise = _number(profile.get("compromise_preference"), 0.5, 0.0, 1.0)
    security = _number(profile.get("security_priority"), 0.5, 0.0, 1.0)
    doctrine_adjustment = _number(doctrine.get("score_adjustment"), 0.0, -0.24, 0.24)

    cooperation = (
        (relations / 100.0) * 0.16
        + (opinion / 100.0) * 0.06
        + (trust - 0.5) * 0.28
        + (promise_trust - 0.5) * 0.24
        + dependence * 0.08
        + (0.09 if same_faction is True else -0.02 if same_faction is False else 0.0)
        + (0.035 if alliance is True else 0.0)
        + ((willingness - 0.5) * 0.10)
        + ((compromise - 0.5) * 0.06)
        + ideology_signal
        + doctrine_adjustment * 0.45
        + memory_signal
    )
    risk = (
        threat * 0.24
        + conflict * 0.18
        + max(0.0, -balance) * 0.10
        + (0.18 if war is True and intent not in {"peace", "ceasefire", "truce", "white_peace"} else 0.0)
    )
    uncertainty_fields: list[str] = []
    if not relation_known:
        uncertainty_fields.append("relations")
    if not threat_known:
        uncertainty_fields.append("threat")
    if war is None:
        uncertainty_fields.append("war_status")
    if same_faction is None:
        uncertainty_fields.append("same_faction")
    if intent in {"non_aggression", "alliance", "faction_join"} and not doctrine.get("ideology_observed"):
        uncertainty_fields.append("ideology")
    if intent == "non_aggression" and existing_nap is None:
        uncertainty_fields.append("existing_non_aggression_pact")
    if intent in {"trade", "market_access", "embargo", "lift_embargo"} and not _known(context.get("economic_dependence")):
        uncertainty_fields.append("economic_dependence")
    if intent in {"political_annexation", "territory_partition", "demand"} and not _known(context.get("claims")):
        uncertainty_fields.append("claims")
    uncertainty = min(0.75, len(uncertainty_fields) * 0.10)

    benefit = 0.0
    commitment_cost = 0.0
    sovereignty_cost = 0.0
    if intent == "non_aggression":
        benefit = 0.14 + (0.07 if threat >= 0.35 else 0.0)
        commitment_cost = 0.025
        if war is False:
            benefit += 0.035
        if existing_nap is True:
            benefit -= 0.22
        if context.get("major_power") in {True, "yes", "true", 1} and context.get("player_major_power") in {True, "yes", "true", 1}:
            benefit += 0.025
    elif intent in {"peace", "ceasefire", "truce", "white_peace"}:
        benefit = 0.16 if war is True else -0.05
        commitment_cost = 0.015
    elif intent in {"alliance", "faction_join"}:
        benefit = 0.045 + (0.08 if same_faction is True else 0.0)
        commitment_cost = 0.14
    elif intent == "guarantee":
        benefit = 0.055
        commitment_cost = 0.105
        if threat >= 0.35:
            benefit += min(0.06, threat * max(0.2, security) * 0.10)
    elif intent in {"trade", "market_access", "embargo", "lift_embargo"}:
        benefit = 0.055 + dependence * 0.07
        commitment_cost = 0.04
    elif intent in {"military_access", "docking_rights", "air_base_access"}:
        benefit = 0.015
        commitment_cost = 0.18
        risk += 0.04
    elif intent in {"political_annexation", "territory_partition", "demand"}:
        sovereignty_cost = 0.48 if intent == "political_annexation" else 0.30
        if doctrine.get("major_power") in {True, "yes", "true", 1}:
            sovereignty_cost += 0.06
        commitment_cost = sovereignty_cost
        # A precisely verified territorial offer to the receiving state may
        # offset some cost. Do not infer state value from a state name.
        if isinstance(doctrine.get("territorial_offer"), Mapping):
            offer = doctrine["territorial_offer"]
            if str(offer.get("to_tag") or "").upper() == str(context.get("receiver_tag") or "").upper():
                benefit += 0.10
    else:
        benefit = 0.02
        commitment_cost = 0.06

    # The previous deterministic engine assessment remains a small policy prior,
    # while the context-dependent comparison can select a better response.
    prior_bonus = 0.075
    accept_score = 0.46 + cooperation + benefit - risk - commitment_cost
    counter_score = 0.38 + risk * 0.40 + uncertainty * 0.26 + max(0.0, 0.18 - cooperation) * 0.28
    reject_score = (0.16 + risk * 0.78 + sovereignty_cost * 0.80
                    + max(0.0, 0.40 - trust) * 0.35)
    if (intent == "political_annexation"
            and (relations <= -50 or threat >= 0.65 or war is True)):
        reject_score += 0.24
    defer_score = 0.27 + uncertainty * 0.52 + risk * 0.14
    scores = {"accept": accept_score, "counter_offer": counter_score,
              "reject": reject_score, "defer": defer_score}
    adjustments, goal_reasons = goal_adjustments(interests, intent, context)
    for name, adjustment in adjustments.items():
        scores[name] += adjustment
    scores[base] += prior_bonus

    # Hard engine constraints are reflected in the candidate set; the planner
    # cannot recommend acceptance when a binding prerequisite is known false.
    blocked: dict[str, str] = {}
    if intent == "non_aggression" and war is True:
        blocked["accept"] = "active_war_requires_ceasefire_first"
    if intent == "non_aggression" and existing_nap is True:
        blocked["accept"] = "non_aggression_pact_already_active"
    if set(provenance["excluded"]) & {"war_status", "existing_non_aggression_pact", "claims"}:
        blocked["accept"] = "critical_context_stale_conflicting_or_unverified"
    if intent in {"alliance", "faction_join"} and same_faction is True:
        blocked["accept"] = "already_same_faction"
    if any(field in uncertainty_fields for field in ("war_status", "claims")) and intent in {
        "political_annexation", "territory_partition",
    }:
        # Missing sovereignty/ownership facts are a reason to clarify, never to
        # accept an irreversible territorial or political change.
        blocked["accept"] = "required_sovereignty_context_unknown"

    allowed = [name for name in _VALID_ACTIONS if name not in blocked]
    selected = max(allowed, key=lambda name: (scores[name], name == base, name))
    scores_list = [
        {"action": action, "score": round(scores[action], 4),
         **({"blocked_reason": blocked[action]} if action in blocked else {})}
        for action in sorted(_VALID_ACTIONS, key=lambda name: (-scores[name], name))
    ]
    score_gap = scores[selected] - max((scores[name] for name in allowed if name != selected), default=scores[selected])
    evidence = []
    if relation_known:
        evidence.append("recorded_relations")
    if opinion_known:
        evidence.append("recorded_opinion")
    if trust_known:
        evidence.append("relationship_trust_or_relations_prior")
    if threat_known:
        evidence.append("recorded_threat")
    if war is not None:
        evidence.append("recorded_war_status")
    if same_faction is not None:
        evidence.append("recorded_faction_status")
    if doctrine.get("ideology_observed") is True:
        evidence.append("bridge_observed_ideology_prior")
    if doctrine.get("authority") == "advisory_negotiation_prior_only":
        evidence.append("bounded_state_doctrine_adjustment")
    evidence.extend(memory_reasons)
    if not evidence:
        evidence.append("no_country_specific_game_facts_available")

    applied = bool(allow_override and selected != base)
    if not allow_override:
        selected = base
        decision_source = "existing_engine_policy_precedence"
    else:
        decision_source = "strategic_candidate_comparison"
    return {
        "planner_version": PLANNER_VERSION,
        "plan_id": plan_id,
        "authority": AUTHORITY,
        "mode": "proposal_strategy",
        "intent": intent,
        "selected_action": selected,
        "engine_decision": base,
        "decision_source": decision_source,
        "applied_to_engine_decision": applied,
        "candidate_scores": scores_list,
        "selected_score_margin": round(score_gap, 4),
        "evidence": list(dict.fromkeys(evidence))[:10],
        "unknown_inputs": uncertainty_fields[:10],
        "memory_signal": memory_readiness,
        "strategic_interests": dict(interests) if isinstance(interests, Mapping) else None,
        "goal_adjustments": adjustments,
        "goal_reasons": goal_reasons,
        "risk_assessment": {"score": round(risk, 4), "authority": "engine_heuristic_not_game_fact"},
        "context_provenance": provenance,
        "alternatives": [
            {"kind": "clarification", "eligible": bool(uncertainty_fields),
             "reason": "resolve_unknown_inputs", "binding": False},
            {"kind": "limited_duration", "eligible": intent == "non_aggression" and not blocked,
             "reason": "bound_future_commitment_without_changing_other_terms", "binding": False},
            {"kind": "alternative_agreement", "eligible": False,
             "reason": "requires_separate_extracted_proposal_and_authorization", "binding": False},
        ],
        "nonbinding_policy": True,
        "safety": "does_not_create_agreement_or_authorize_game_effects",
    }


__all__ = ["AUTHORITY", "PLANNER_VERSION", "build_strategic_plan"]


def duration_alternatives(point, strategic_plan):
    """Enumerate bounded amendments to a *known* NAP duration, not new clauses."""
    import re
    if point.get("term_id") or point.get("action_id") != "non_aggression_pact" or point.get("unresolved_fields"):
        return []
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
             "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
    parsed = period(point.get("duration"))
    if not parsed or parsed[1] != "year":
        return []
    original = parsed[0]
    if not 2 <= original <= 10:
        return []
    risk = strategic_plan.get("risk_assessment", {})
    high_risk = isinstance(risk, Mapping) and _number(risk.get("score"), 0, 0, 1) >= 0.65
    preferred = 1 if high_risk else min(3, original - 1)
    candidates = list(range(1, original))
    return [{"duration_years": years, "score": round(1 - abs(years - preferred) * 0.1, 3),
             "reason": "shorter_bounded_commitment", "binding": False} for years in candidates]


def build_point_decisions(points, strategic_plan, previous_positions=()) -> list[PointDecision]:
    """Engine-owned conversational policy; never treaty consent or execution."""
    import copy
    stance = {"accept": "accept", "reject": "reject", "counter_offer": "counter_offer",
              "defer": "discuss"}.get(strategic_plan.get("selected_action"), "discuss")
    decisions = []
    for index, point in enumerate(points):
        from negotiation_progress import prepare_point, mark_progress
        point = prepare_point({**point, "duration": canonical_period(point.get("duration"))})
        row = copy.deepcopy(point)
        chosen = stance if not point.get("term_id") else "discuss"
        # A precisely sourced reciprocal notice commitment can be supported
        # independently of the pact. It remains unenforceable game-side.
        if (point.get("term_id") == "advance_notice" and point.get("term_clause")
                and not point.get("unresolved_fields")
                and not point.get("conditions") and not point.get("exceptions")):
            chosen = "accept"
        if point.get("unresolved_fields") or point.get("speaking_role") not in {None, "party"}:
            chosen = "clarify"
        alternatives = duration_alternatives(point, strategic_plan)
        row["evaluated_amendments"] = alternatives
        if (chosen == 'counter_offer' and not alternatives and not point.get('term_id')
                and point.get('action_id') in {'guarantee','military_access','embargo','lift_embargo','alliance','faction_join'}):
            # An abstract willingness to bargain is not a concrete amendment.
            # Re-rank the feasible decisions; never ask the sender to repair
            # the engine's missing counteroffer implementation.
            feasible = [c for c in strategic_plan.get('candidate_scores', [])
                        if c.get('action') in {'accept', 'reject', 'defer'}
                        and not c.get('blocked_reason') and isinstance(c.get('score'), (int, float))]
            selected = max(feasible, key=lambda c: (c['score'], c['action']))['action'] if feasible else 'defer'
            chosen = {'defer':'discuss'}.get(selected, selected)
            row['candidate_feasibility'] = {'excluded':'counter_offer', 'reason':'no_concrete_amendment', 'selected':selected}
        if chosen == "counter_offer" and alternatives:
            row["proposed_duration_years"] = max(alternatives, key=lambda r: (r["score"], -r["duration_years"]))["duration_years"]
            row["proposal_basis"] = "bounded_shorter_commitment_candidate"
        for previous in reversed(list(previous_positions)):
            if (previous.get("action_id") != point.get("action_id")
                    or point.get('new_proposal')
                    or (point.get('action_id') not in {'non_aggression_pact','truce'} and
                        (previous.get('actors') != point.get('actors')
                         or previous.get('target') != point.get('target')
                         or previous.get('recipient') != point.get('recipient')))
                    or previous.get("term_id") != point.get("term_id")
                    or previous.get("conditions", []) != point.get("conditions", [])
                    or previous.get("exceptions", []) != point.get("exceptions", [])):
                continue
            if not point.get("term_id") and previous.get("proposed_duration_years") in range(1, 10):
                duration = str(point.get("duration") or "").lower()
                years = previous["proposed_duration_years"]
                word = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine"}[years]
                if duration in {f"{word} years", f"{years} years", f"{word}-year", f"{years}-year",
                                f"{word} year", f"{years} year"}:
                    chosen = "accept"
                    row.pop("proposed_duration_years", None)
                elif duration == str(previous.get("duration") or "").lower():
                    chosen = "counter_offer"
                    row["proposed_duration_years"] = years
                    row["negotiation_progress"] = "awaiting_response_to_existing_counteroffer"
                    prior_count = previous.get("unchanged_offer_count", 0)
                    row["unchanged_offer_count"] = min(20, prior_count + 1) if type(prior_count) is int else 1
            elif (not point.get("term_id") and canonical_period(previous.get("duration")) == point.get("duration")
                  and previous.get("stance") in {"accept", "reject"}):
                chosen = previous["stance"]
            elif (point.get("term_id") and previous.get("term_clause") == point.get("term_clause")
                  and previous.get("stance") in {"accept", "reject", "discuss", "clarify"}):
                chosen = previous["stance"]
            break
        # This exact clause narrows what the pact itself does. It does not
        # promise future ownership or apply to a separate territorial deal.
        if (stance == "accept" and chosen == "discuss" and point.get("action_id") == "non_aggression_pact"
                and point.get("term_id") == "no_territorial_changes"
                and not any(point.get(key) for key in ("conditions", "exceptions", "duration"))
                and re.fullmatch(r"(?:no territorial changes|без территориальных изменений|"
                                 r"aucun changement territorial)[.!]?",
                                 str(point.get("term_clause") or "").strip(), re.I)):
            chosen = "accept"
        if point.get("unresolved_fields"):
            chosen = "clarify"
        accept_blocked = any(candidate.get("action") == "accept" and candidate.get("blocked_reason")
                             for candidate in strategic_plan.get("candidate_scores", []))
        if (accept_blocked and (chosen == 'accept' or point.get('action_id') == 'non_aggression_pact')) or point.get("speaking_role") not in {None, "party"}:
            chosen = "clarify"
            row.pop("proposed_duration_years", None)
        if chosen == "counter_offer" and not row.get("proposed_duration_years"):
            chosen = "clarify"  # No concrete engine-selected amendment exists.
        row["decision_reasons"] = list(strategic_plan.get("goal_reasons", []))[:4]
        # Goal adjustments are optional, not a complete explanation of a vote.
        # Preserve the actual comparison separately from public stylistic facts.
        from copy import deepcopy
        row['decision_audit'] = deepcopy({k: strategic_plan.get(k) for k in (
            'plan_id', 'decision_source', 'selected_action', 'engine_decision',
            'candidate_scores', 'evidence', 'unknown_inputs', 'risk_assessment',
            'context_provenance', 'goal_reasons')})
        row['decision_audit']['final_point_stance'] = chosen
        row["clarification_reasons"] = list(point.get("unresolved_fields", []))
        if chosen=='clarify' and point.get('speaking_role')=='observer':
            row['clarification_reasons'].append('signing_authority')
        if chosen == "clarify" and not row["clarification_reasons"]:
            row["clarification_reasons"] = [candidate["blocked_reason"] for candidate in strategic_plan.get("candidate_scores", [])
                                           if candidate.get("action") == "accept" and candidate.get("blocked_reason")]
            if not row["clarification_reasons"]:
                row["clarification_reasons"] = ["no_supported_amendment"]
        row.update(policy_schema_version=1, point=index, stance=chosen, authority="engine_point_policy",
                   binding=False, consent_status="unconfirmed")
        mark_progress(row, list(previous_positions))
        decisions.append(row)
    return decisions


def render_point_policy(row, country, other, language="en"):
    """Canonical operative wording from immutable policy."""
    from negotiation_progress import progress_text
    progress = progress_text(row, country, other, language)
    if progress is not None:
        return progress
    stance = row.get("stance", "clarify")
    actors = row.get('actors', row.get('actor_country_tags', []))
    target = row.get('target', row.get('target_country_tag'))
    if (language=='fr' and row.get('action_id')=='territory_partition' and stance=='clarify'
            and 'verified_state_allocation' in set(row.get('clarification_reasons',[])+row.get('unresolved_fields',[]))):
        return 'Précisez les États concernés, leurs propriétaires actuels et le destinataire proposé. Les territoires environnants ne définissent pas une répartition vérifiée ; aucune cession n’est acceptée.'
    if language == 'fr':
        action = row.get('action_id')
        if row.get('conversation_stage') == 'confirmation_needed':
            return "Notre accord porte sur les conditions déjà présentées. Nous attendons votre confirmation de cette rédaction."
        if row.get('conversation_stage') == 'refusal_maintained':
            return "Notre refus demeure. Si vous souhaitez poursuivre, indiquez la clause que vous proposez de revoir."
        if stance == 'clarify':
            reasons = set(row.get('clarification_reasons', []))
            if action == 'guarantee' and reasons & {'actor', 'protected_country', 'recipient'}:
                return "Quel pays donnerait la garantie et à quel État s’appliquerait-elle ?"
            if action == 'military_access' and reasons & {'actor', 'recipient'}:
                return "Quel pays ouvrirait son territoire et à quelles forces proposez-vous d’accorder le passage ?"
            return "Pouvez-vous préciser l’engagement que vous demandez à chaque partie ?"
        if stance == 'counter_offer' and row.get('proposed_duration_years') in range(1, 10):
            duration = localized_period(f"{row['proposed_duration_years']} years", 'fr')
            prior = localized_period(row.get('duration'), 'fr')
            return f"Nous proposons un pacte de non-agression de {duration} au lieu de {prior}, afin de limiter la durée de notre engagement."
        subject = str(row.get('term_clause') or row.get('source_excerpt') or 'la proposition').rstrip('.')
        if action == 'non_aggression_pact' and not row.get('term_id'):
            duration = localized_period(row.get('duration'), 'fr')
            subject = 'le pacte de non-agression' + (f' pour {duration}' if duration else '')
        if action == 'guarantee' and len(actors or []) == 1 and target and not row.get('conditions') and not row.get('exceptions'):
            from diplomatic_language import localized_country
            issuer = localized_country(actors[0], actors[0], 'fr')
            beneficiary = localized_country(target, target, 'fr')
            subject = f'la garantie donnée par {issuer} pour protéger l’indépendance du pays suivant : {beneficiary}'
        verb = {'accept': 'Nous acceptons', 'reject': 'Nous refusons',
                'discuss': 'Nous sommes disposés à discuter de'}.get(stance, 'Nous souhaitons préciser')
        return f"{verb} {subject}."
    if (row.get('action_id') == 'guarantee' and isinstance(actors, list) and len(actors) == 1 and target
            and not row.get('conditions') and not row.get('exceptions')
            and stance in {'accept', 'reject', 'discuss'} and language in {'ru', 'en'}):
        from diplomatic_language import localized_country
        issuer = localized_country(actors[0], actors[0], language)
        beneficiary = localized_country(target, target, language)
        if language == 'ru':
            if issuer.casefold() == country.casefold():
                if stance == 'accept':
                    return f'{country} согласна предоставить гарантию независимости государству «{beneficiary}».'
                if stance == 'reject':
                    return f'{country} не согласна предоставить гарантию независимости государству «{beneficiary}».'
                return f'{country} готова обсуждать гарантию независимости государства «{beneficiary}», но пока не принимает обязательств.'
            verb = {'accept': 'принимает', 'reject': 'отклоняет', 'discuss': 'готова обсудить без принятия'}[stance]
            return (f'{country} {verb} предложение о гарантии независимости. '
                    f'Гарантирующая сторона: {issuer}; защищаемая сторона: {beneficiary}.')
        verb = {'accept': 'accepts', 'reject': 'rejects', 'discuss': 'is willing to discuss without accepting'}[stance]
        return f'{country} {verb} the proposal that {issuer} guarantee the independence of {beneficiary}.'
    if language == "ru":
        if row.get("term_id"):
            subject = str(row.get("term_clause") or "неуточнённый пункт").rstrip(".")
        elif row.get("action_id") == "non_aggression_pact":
            duration = localized_period(row.get("duration"), "ru")
            subject = "пакт о ненападении" + (f" сроком на {duration}" if duration else "")
        else:
            subject = str(row.get("source_excerpt") or "это предложение").rstrip(".")
        parties = f" Стороны: {country} и {other}."
        if stance == "counter_offer" and row.get("proposed_duration_years") in range(1, 10):
            proposed = localized_period(f"{row['proposed_duration_years']} years", "ru")
            if row.get("negotiation_progress") == "awaiting_response_to_existing_counteroffer":
                return (f"{country} сохраняет встречное предложение: пакт о ненападении сроком на {proposed} "
                        f"вместо {localized_period(row.get('duration'), 'ru')}. "
                        "Просим ответить на предложенный срок или назвать конкретное изменение." + parties)
            return (f"{country} предлагает пакт о ненападении сроком на {proposed} вместо "
                    f"{localized_period(row.get('duration'), 'ru')}. Более короткий срок ограничивает обязательства." + parties)
        if stance == "clarify":
            reasons = set(row.get('clarification_reasons', []))
            if reasons & {'summary_confirmation', 'source_analysis_incomplete', 'protected_country', 'actor', 'recipient'}:
                return (f"{country} просит уточнить, какое государство должно предоставить гарантию "
                        "и чью независимость она должна защищать. Пока согласие не подтверждено."
                        if row.get('action_id') == 'guarantee' else
                        f"{country} просит изложить спорное условие отдельно: кто принимает обязательство "
                        "и в чём оно состоит? Пока согласие не подтверждено.")
            labels = {"parties": "участников", "duration": "срок", "conditions": "содержание условий",
                      "summary_confirmation": "надёжный разбор исходного предложения",
                      "source_analysis_incomplete": "полный разбор всех пунктов",
                      "no_supported_amendment": "какой конкретный пункт предлагается изменить",
                      "active_war_requires_ceasefire_first": "возможность прекращения войны до пакта",
                      "critical_context_stale_conflicting_or_unverified": "актуальные непротиворечивые игровые сведения"}
            missing = ", ".join(dict.fromkeys(labels.get(r, "содержание спорного пункта") for r in row.get("clarification_reasons", [])))
            return f"{country} просит уточнить: {missing or 'содержание пункта'}." + parties
        verb = {"accept": "принимает", "reject": "отклоняет", "discuss": "готова обсуждать без принятия"}.get(stance, "просит уточнить")
        return f"{country} {verb}: {subject}." + parties
    if row.get("term_id"):
        subject = str(row.get("term_clause") or row.get("source_excerpt") or "the unspecified clause").strip().rstrip(".")
    elif row.get("action_id") == "non_aggression_pact":
        duration = str(row.get("duration") or "").strip()
        adjective = duration.replace(" years", "-year").replace(" months", "-month")
        subject = "a " + (adjective + " " if adjective else "") + "non-aggression pact with " + other
    else:
        subject = str(row.get("source_excerpt") or row.get("action_id") or "unspecified proposal").strip().rstrip(".")
    if stance == "counter_offer" and row.get("proposed_duration_years") in range(1, 10):
        years = row["proposed_duration_years"]
        word = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine"}[years]
        if row.get("negotiation_progress") == "awaiting_response_to_existing_counteroffer":
            return f"{country} maintains its counteroffer of a {word}-year non-aggression pact with {other}. Please respond to that duration or specify an amendment."
        return f"{country} proposes a {word}-year non-aggression pact with {other} instead of {row.get('duration')}, to limit its commitment."
    verb = {"accept": "accepts", "reject": "rejects", "discuss": "is willing to discuss without accepting",
            "clarify": "requests clarification of", "counter_offer": "requests a concrete revision of"}.get(stance, "requests clarification of")
    if stance == "clarify":
        if set(row.get('clarification_reasons', [])) & {'summary_confirmation', 'source_analysis_incomplete', 'protected_country', 'actor', 'recipient'}:
            return (f"{country} asks which country would provide the guarantee and whose independence it would protect. No acceptance is confirmed."
                    if row.get('action_id') == 'guarantee' else
                    f"{country} asks you to state the disputed condition separately: who would undertake which obligation? No acceptance is confirmed.")
        labels = {"parties": "the participating countries", "duration": "the exact duration",
                  "source_analysis_incomplete": "a complete interpretation of the proposal",
                  "summary_confirmation": "a reliable interpretation of the proposal",
                  "no_supported_amendment": "which specific clause should change",
                  "active_war_requires_ceasefire_first": "a ceasefire before a non-aggression pact",
                  "critical_context_stale_conflicting_or_unverified": "fresh, consistent game facts"}
        reasons = list(dict.fromkeys(labels.get(r, "the unresolved clause") for r in row.get("clarification_reasons", [])))
        return f"{country} requests clarification of {', '.join(reasons) or subject}."
    return f"{country} {verb} {subject}."


def expression_matches_policy(row, text, canonical, country, other, language="en"):
    import re
    if not isinstance(text, str) or len(text) > 600 or not text.strip().endswith((".", "!", "?")):
        return False
    if text == canonical:
        return True
    from diplomatic_phrases import matches
    if matches(text, canonical, row, language):
        return True
    if (row.get('conversation_stage') in {'confirmation_needed','refusal_maintained'}
            or row.get('action_id')=='territory_partition'):
        return False # Do not let a paraphrase remove the next step or missing facts.
    if language == "ru":
        from language_detection import matches_output_language
        from structured_intents import extract_duration
        if (not matches_output_language(text, "ru") or row.get("term_id")
                or row.get("action_id") != "non_aggression_pact"):
            return False
        if (country.casefold() not in text.casefold() and not re.search(r"\bмы\b", text, re.I)):
            return False
        if re.search(r"если|при условии|подпис\w*|исполн\w*|вступил\w*|вступает|заключ[её]н\w*", text, re.I):
            return False
        if re.search(re.escape(other) + r"\s+(?:принима\w*|соглас\w*|отклон\w*)", text, re.I):
            return False
        stance = row.get("stance")
        positive = bool(re.search(r"принима\w*|согласн\w*", text, re.I))
        negative = bool(re.search(r"отклон\w*|отказ\w*|не\s+(?:принима\w*|согласн\w*)", text, re.I))
        counter = bool(re.search(r"предлага\w*|встречн\w*\s+предлож\w*", text, re.I))
        if stance == "accept" and (not positive or negative or counter):
            return False
        if stance == "reject" and (not negative or counter):
            return False
        if stance == "counter_offer" and (not counter or positive or negative):
            return False
        if stance not in {"accept", "reject", "counter_offer"} or "ненапад" not in text.casefold():
            return False
        expected = period(f"{row.get('proposed_duration_years')} years") if stance == "counter_offer" else period(row.get("duration"))
        if expected and period(extract_duration(text, action_id="non_aggression_pact")) != expected:
            return False
        allowed = {str(expected[0])} if expected else set()
        if stance == "counter_offer" and period(row.get("duration")):
            allowed.add(str(period(row["duration"])[0]))
        return set(re.findall(r"\d+", text)) <= allowed
    if language != "en":
        return False
    folded = text.casefold()
    if country.casefold() not in folded and not re.search(r"\bwe\b", folded):
        return False
    if re.search(r"\b(?:if|unless|provided|signed|executed|ratified)\b", folded):
        return False
    if re.search(re.escape(other) + r"(?:'s)?\s+(?:accepts?|agrees?|rejects?|promises?)\b", text, re.I):
        return False
    stance = row.get("stance")
    if stance == "accept" and (not re.search(r"\baccepts?\b", folded) or re.search(r"\b(?:not|rejects?|refuses?|proposes?)\b", folded)):
        return False
    if stance == "reject" and not re.search(r"\b(?:rejects?|refuses?)\b", folded):
        return False
    if stance in {"discuss", "clarify"}:
        return False  # No unapproved commitment can enter an unresolved clause.
    if stance == "counter_offer":
        years = row.get("proposed_duration_years")
        if type(years) is not int or years not in range(1, 10):
            return False
        word = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine"}[years]
        if not re.search(r"\b(?:" + str(years) + "|" + word + r")[- ]years?\b", folded) or not re.search(r"\b(?:proposes?|counter)\b", folded):
            return False
        if re.search(r"\b(?:accepts?|rejects?|refuses?)\b", folded):
            return False
    elif row.get("duration"):
        duration = str(row["duration"]).casefold()
        if duration not in folded and duration.replace(" years", "-year") not in folded:
            return False
    if row.get("term_id") == "advance_notice":
        return "notice" in folded and "30" in folded
    if row.get("term_id"):
        return str(row.get("term_clause") or "").casefold() in folded
    return "non-aggression" in folded if row.get("action_id") == "non_aggression_pact" else False


def resolve_point_scopes(points, active_actions, source_text):
    """Resolve explicit references against one active version; do not guess revisions."""
    import copy, re
    resolved = copy.deepcopy(points)
    for point in resolved:
        term_id = point.get("term_id")
        if not term_id:
            continue
        candidates = [row for row in active_actions.get(point.get("action_id"), [])
                      if isinstance(row, dict) and row.get("term_id") == term_id]
        if not candidates:
            continue
        if len(candidates) != 1:
            point.setdefault("unresolved_fields", []).append("ambiguous_active_term")
            continue
        prior = candidates[0]
        spec = prior.get("proposal_spec") or {}
        clause = prior.get("term_clause") or spec.get("term_clause")
        current = str(point.get("term_clause") or "")
        if not clause:
            continue
        if term_id == "advance_notice":
            current_days = re.findall(r"\b(\d+)[- ]days?\b", current, re.I)
            previous_days = re.findall(r"\b(\d+)[- ]days?\b", clause, re.I)
            message_days = re.findall(r"\b(\d+)[- ]days?\b", source_text, re.I)
            if not current_days and previous_days:
                if message_days and set(message_days) != set(previous_days):
                    point.setdefault("unresolved_fields", []).append("changed_notice_duration_not_grounded")
                elif re.search(r"\b(?:existing|current|unchanged|remains?|confirm|accept)\w*\b", source_text, re.I):
                    point["term_clause"] = clause
                    point["source_excerpt"] = clause
                    point["agreement_version"] = prior.get("agreement_version")
    return resolved
