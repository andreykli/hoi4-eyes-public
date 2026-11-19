"""Fail-closed assessment prototype for political annexation and integration.

The assessment functions are called by the existing Flask/SQLite engine, but
their present inputs are caller-supplied and unverified. The scheduler ledger
is persisted with chat state. Neither this module nor its API can approve,
start, or execute an annexation or an integration effect. The default weights
and stage thresholds are illustrative and need game calibration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from itertools import product
import math
import re
from typing import Mapping
from hoi4_country_tags import COUNTRY_TAG_RE


_TAG_RE = COUNTRY_TAG_RE
_CAMPAIGN_ID_RE = re.compile(r"^[0-9a-f]{32}$")

_ASSESSMENT_FACTS = (
    "initiator_exists",
    "target_exists",
    "initiator_owned_states",
    "target_owned_states",
    "at_war",
    "conflicting_treaty",
    "initiator_military",
    "target_military",
    "initiator_factories",
    "target_factories",
    "relations",
    "ideology_compatible",
    "target_stability",
    "target_has_guarantee",
    "target_ally_count",
    "world_tension",
)


@dataclass(frozen=True)
class AnnexationContext:
    operation_id: str
    initiator_tag: str
    target_tag: str
    initiator_exists: bool | None
    target_exists: bool | None
    initiator_owned_states: int | None
    target_owned_states: int | None
    at_war: bool | None
    conflicting_treaty: bool | None
    initiator_military: float | None
    target_military: float | None
    initiator_factories: float | None
    target_factories: float | None
    relations: float | None
    ideology_compatible: bool | None
    target_stability: float | None
    target_has_guarantee: bool | None
    target_ally_count: int | None
    world_tension: float | None
    approach: str = "voluntary_offer"
    evidence_sources: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AnnexationPolicy:
    """Transparent illustrative weights; not calibrated against HOI4 outcomes."""

    max_target_share_of_initiator: float = 0.25
    consent_intercept: float = -0.10
    consent_weights: Mapping[str, float] = field(default_factory=lambda: {
        "relative_size": 0.20,
        "military_balance": 0.90,
        "industrial_balance": 0.45,
        "relations": 0.65,
        "ideology_compatibility": 0.35,
        "target_stability": -0.45,
        "world_tension": -0.55,
        "target_guarantee": -0.90,
        "allied_support": -0.65,
        "voluntary_approach": 0.45,
    })
    crisis_intercept: float = -3.0
    crisis_weights: Mapping[str, float] = field(default_factory=lambda: {
        "world_tension": 2.20,
        "target_size_share": 0.75,
        "target_guarantee": 1.20,
        "allied_support": 1.00,
        "hostile_relations": 0.85,
        "ultimatum": 1.20,
    })


@dataclass(frozen=True)
class IntegrationSnapshot:
    """Sourced post-transfer values for a non-executing integration projection."""

    transfer_confirmed: bool | None
    days_since_transfer: int | None
    resistance: float | None
    cooperation: float | None
    administrative_capacity: float | None
    investment: float | None
    infrastructure: float | None
    stability: float | None
    evidence_sources: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class IntegrationPolicy:
    transition_days: int = 30
    partial_days: int = 180
    advanced_days: int = 365
    core_review_days: int = 730
    partial_max_resistance: float = 65.0
    partial_min_cooperation: float = 25.0
    advanced_max_resistance: float = 40.0
    advanced_min_cooperation: float = 50.0
    advanced_min_administration: float = 40.0
    advanced_min_investment: float = 30.0
    advanced_min_infrastructure: float = 25.0
    advanced_min_stability: float = 40.0
    core_review_max_resistance: float = 15.0
    core_review_min_cooperation: float = 75.0
    core_review_min_administration: float = 70.0
    core_review_min_investment: float = 60.0
    core_review_min_infrastructure: float = 50.0
    core_review_min_stability: float = 60.0


@dataclass(frozen=True)
class ComputerInitiativeSnapshot:
    """Sourced campaign gates for the isolated computer-country prototype.

    `last_checked_day` is scheduler state persisted by the engine's SQLite
    payload. `None` for `last_initiative_day` means an explicit ledger record
    that no confirmed initiative has started in this campaign.
    """

    campaign_id: str
    country_tag: str
    campaign_day: int | None
    ai_controlled: bool | None
    ideology: str | None
    historical_focuses_enabled: bool | None
    active_operation_count: int | None
    last_initiative_day: int | None
    last_checked_day: int | None = None
    evidence_sources: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ComputerInitiativePolicy:
    """Conservative prototype cadence; values are not HOI4 balance data."""

    check_interval_days: int = 30
    cooldown_days: int = 730
    max_active_operations: int = 1
    initiative_probability: float = 0.005


def assess_computer_annexation_initiative(
    snapshot: ComputerInitiativeSnapshot,
    annexation: AnnexationContext,
    *,
    roll: float,
    policy: ComputerInitiativePolicy = ComputerInitiativePolicy(),
) -> dict:
    """Return a rare, gated AI initiative candidate; never start an operation.

    This prototype intentionally fails closed when historical-focus settings
    or any campaign gate are unavailable. The caller must persist
    ``next_scheduler_state`` and must separately implement approval, diplomacy,
    and game execution. A deterministic evaluation ID lets such a caller
    deduplicate a scheduler tick without making an operation here.
    """

    def result(status: str, reason: str, *, eligible: bool = False, probability: float | None = None,
               record_check: bool = False) -> dict:
        day = snapshot.campaign_day
        scheduled_day = day if isinstance(day, int) and not isinstance(day, bool) else None
        key = "|".join((
            str(snapshot.campaign_id or ""), str(snapshot.country_tag or ""),
            str(annexation.target_tag or ""), str(scheduled_day if scheduled_day is not None else "unknown"),
        ))
        evaluation_id = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
        return {
            "status": status,
            "reason": reason,
            "campaign_id": snapshot.campaign_id,
            "country_tag": snapshot.country_tag,
            "target_tag": annexation.target_tag,
            "evaluation_id": evaluation_id,
            "scheduled_day": scheduled_day,
            "eligible": eligible,
            "initiative_probability": probability,
            "operation_started": False,
            "action_prepared": False,
            "game_effect_confirmed": False,
            "next_scheduler_state": {"last_checked_day": scheduled_day}
            if record_check and scheduled_day is not None else None,
            "model_notice": "Isolated scheduler prototype; no game operation or action is created.",
        }

    if not isinstance(snapshot.campaign_id, str) or not _CAMPAIGN_ID_RE.fullmatch(snapshot.campaign_id):
        return result("blocked", "campaign_id_must_be_32_character_lowercase_uuid_hex")
    if not _TAG_RE.fullmatch(snapshot.country_tag or ""):
        return result("blocked", "invalid_country_tag")
    if snapshot.country_tag != annexation.initiator_tag:
        return result("blocked", "initiative_country_must_match_assessment_initiator")
    if not isinstance(annexation.target_tag, str) or not _TAG_RE.fullmatch(annexation.target_tag):
        return result("blocked", "invalid_target_tag")

    if (not isinstance(policy.check_interval_days, int) or isinstance(policy.check_interval_days, bool)
            or policy.check_interval_days < 1):
        return result("blocked", "invalid_check_interval")
    if (not isinstance(policy.cooldown_days, int) or isinstance(policy.cooldown_days, bool)
            or policy.cooldown_days < 1):
        return result("blocked", "invalid_cooldown")
    if (not isinstance(policy.max_active_operations, int) or isinstance(policy.max_active_operations, bool)
            or policy.max_active_operations < 1):
        return result("blocked", "invalid_active_operation_limit")
    if (isinstance(policy.initiative_probability, bool)
            or not isinstance(policy.initiative_probability, (int, float))
            or not 0.0 <= policy.initiative_probability <= 1.0):
        return result("blocked", "invalid_initiative_probability")

    required = (
        "campaign_day", "ai_controlled", "ideology", "historical_focuses_enabled",
        "active_operation_count", "last_initiative_day",
    )
    missing = [name for name in required if getattr(snapshot, name) is None
               and name != "last_initiative_day"]
    missing.extend(
        f"evidence_sources.{name}" for name in required
        if not str(snapshot.evidence_sources.get(name, "")).strip()
    )
    if snapshot.last_checked_day is not None and not str(
        snapshot.evidence_sources.get("last_checked_day", "")
    ).strip():
        missing.append("evidence_sources.last_checked_day")
    if missing:
        reason = "historical_focus_setting_unknown" if snapshot.historical_focuses_enabled is None else "initiative_gate_unknown_or_unsourced"
        outcome = result("incomplete", reason)
        outcome["missing_facts"] = sorted(set(missing))
        return outcome

    if (not isinstance(snapshot.campaign_day, int) or isinstance(snapshot.campaign_day, bool)
            or snapshot.campaign_day < 1):
        return result("blocked", "invalid_campaign_day")
    if not isinstance(snapshot.ai_controlled, bool):
        return result("blocked", "invalid_ai_controlled_fact")
    if not isinstance(snapshot.historical_focuses_enabled, bool):
        return result("blocked", "invalid_historical_focus_setting")
    if not isinstance(snapshot.ideology, str):
        return result("blocked", "invalid_ideology_fact")
    if (not isinstance(snapshot.active_operation_count, int) or isinstance(snapshot.active_operation_count, bool)
            or snapshot.active_operation_count < 0):
        return result("blocked", "invalid_active_operation_count")
    if (snapshot.last_initiative_day is not None
            and (not isinstance(snapshot.last_initiative_day, int)
                 or isinstance(snapshot.last_initiative_day, bool)
                 or snapshot.last_initiative_day < 0
                 or snapshot.last_initiative_day > snapshot.campaign_day)):
        return result("blocked", "invalid_last_initiative_day")
    if (snapshot.last_checked_day is not None
            and (not isinstance(snapshot.last_checked_day, int)
                 or isinstance(snapshot.last_checked_day, bool)
                 or snapshot.last_checked_day < 0
                 or snapshot.last_checked_day > snapshot.campaign_day)):
        return result("blocked", "invalid_last_checked_day")

    if not snapshot.ai_controlled:
        return result("blocked", "country_not_ai_controlled")
    if snapshot.ideology.strip().casefold() != "fascism":
        return result("blocked", "ideology_is_not_fascist")
    if snapshot.historical_focuses_enabled:
        return result("blocked", "historical_focuses_enabled")
    if snapshot.active_operation_count >= policy.max_active_operations:
        return result("blocked", "active_operation_limit_reached")
    if snapshot.last_initiative_day is not None:
        if snapshot.campaign_day - snapshot.last_initiative_day < policy.cooldown_days:
            return result("blocked", "initiative_cooldown_active")

    ordinary_assessment = assess_political_annexation(annexation)
    if ordinary_assessment.get("status") != "estimate_ready_for_player_review":
        outcome = result("blocked", "ordinary_annexation_requirements_not_met")
        outcome["assessment_status"] = ordinary_assessment.get("status")
        outcome["assessment_reason"] = ordinary_assessment.get("reason")
        outcome["assessment_missing_facts"] = ordinary_assessment.get("missing_facts", [])
        return outcome

    if snapshot.last_checked_day is None:
        is_due = snapshot.campaign_day % policy.check_interval_days == 0
    else:
        is_due = snapshot.campaign_day - snapshot.last_checked_day >= policy.check_interval_days
    if not is_due:
        return result("not_due", "periodic_check_not_due")
    if (isinstance(roll, bool) or not isinstance(roll, (int, float))
            or not math.isfinite(float(roll)) or not 0.0 <= roll < 1.0):
        return result("blocked", "roll_must_be_in_half_open_unit_interval")

    if roll >= policy.initiative_probability:
        return result("checked_no_initiative", "random_check_did_not_trigger", eligible=True,
                      probability=policy.initiative_probability, record_check=True)
    return result("candidate_for_review", "rare_ai_initiative_triggered", eligible=True,
                  probability=policy.initiative_probability, record_check=True)


def _sigmoid(value: float) -> float:
    # Keep exp() stable even for caller-supplied extreme policy weights.
    value = max(-40.0, min(40.0, value))
    return 1.0 / (1.0 + math.exp(-value))


def _bounded(value: float, low: float, high: float) -> bool:
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(numeric) and low <= numeric <= high


def _finite_policy_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _block(context: AnnexationContext, reason: str, missing: list[str] | None = None) -> dict:
    return {
        "operation_id": context.operation_id,
        "initiator_tag": context.initiator_tag,
        "target_tag": context.target_tag,
        "status": "blocked" if missing is None else "incomplete",
        "reason": reason,
        "missing_facts": missing or [],
        "consent_probability": None,
        "refusal_probability": None,
        "crisis_probability_if_refused": None,
        "crisis_probability_overall": None,
        "execution": "not_prepared",
        "requires_player_confirmation": True,
    }


def assess_political_annexation(
    context: AnnexationContext,
    policy: AnnexationPolicy = AnnexationPolicy(),
) -> dict:
    """Return an estimate only; unknown/unsourced facts fail closed.

    The returned probabilities are an explicitly uncalibrated planning model,
    not a prediction supplied by HOI4 and not a decision to annex a country.
    """
    if not isinstance(context.operation_id, str) or not context.operation_id.strip():
        return _block(context, "operation_id_required")
    if not _TAG_RE.fullmatch(context.initiator_tag or "") or not _TAG_RE.fullmatch(context.target_tag or ""):
        return _block(context, "invalid_country_tag")
    if context.initiator_tag == context.target_tag:
        return _block(context, "initiator_and_target_must_differ")
    if context.approach not in {"voluntary_offer", "ultimatum"}:
        return _block(context, "unsupported_approach")
    if (not _finite_policy_number(policy.max_target_share_of_initiator)
            or not 0 < policy.max_target_share_of_initiator <= 1):
        return _block(context, "invalid_small_state_threshold")
    if (not _finite_policy_number(policy.consent_intercept)
            or not _finite_policy_number(policy.crisis_intercept)
            or not isinstance(policy.consent_weights, Mapping)
            or not isinstance(policy.crisis_weights, Mapping)
            or any(not _finite_policy_number(value) for value in policy.consent_weights.values())
            or any(not _finite_policy_number(value) for value in policy.crisis_weights.values())):
        return _block(context, "invalid_probability_policy")

    hard_facts = (
        "initiator_exists", "target_exists", "initiator_owned_states", "target_owned_states",
        "at_war", "conflicting_treaty",
    )
    missing_hard = [name for name in hard_facts if getattr(context, name) is None]
    missing_hard += [f"evidence_sources.{name}" for name in hard_facts
                     if not str(context.evidence_sources.get(name, "")).strip()]
    if missing_hard:
        return _block(context, "required_game_facts_unknown_or_unsourced", missing_hard)

    if context.initiator_exists is not True or context.target_exists is not True:
        return _block(context, "country_does_not_exist")
    if not isinstance(context.initiator_owned_states, int) or isinstance(context.initiator_owned_states, bool):
        return _block(context, "invalid_initiator_state_count")
    if not isinstance(context.target_owned_states, int) or isinstance(context.target_owned_states, bool):
        return _block(context, "invalid_target_state_count")
    if context.initiator_owned_states < 1 or context.target_owned_states < 1:
        return _block(context, "both_countries_must_own_at_least_one_state")
    if context.at_war is True:
        return _block(context, "countries_are_at_war")
    if context.conflicting_treaty is True:
        return _block(context, "conflicting_treaty_active")

    target_share = context.target_owned_states / context.initiator_owned_states
    if target_share > policy.max_target_share_of_initiator:
        return _block(context, "target_not_small_relative_to_initiator")

    missing_predictors = [name for name in _ASSESSMENT_FACTS if getattr(context, name) is None]
    missing_predictors += [f"evidence_sources.{name}" for name in _ASSESSMENT_FACTS
                          if not str(context.evidence_sources.get(name, "")).strip()]
    if missing_predictors:
        return _block(context, "forecast_facts_unknown_or_unsourced", missing_predictors)

    numeric_ranges = (
        ("initiator_military", context.initiator_military, 0, float("inf")),
        ("target_military", context.target_military, 0, float("inf")),
        ("initiator_factories", context.initiator_factories, 0, float("inf")),
        ("target_factories", context.target_factories, 0, float("inf")),
        ("relations", context.relations, -100, 100),
        ("target_stability", context.target_stability, 0, 100),
        ("target_ally_count", context.target_ally_count, 0, float("inf")),
        ("world_tension", context.world_tension, 0, 100),
    )
    for name, value, low, high in numeric_ranges:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not _bounded(value, low, high):
            return _block(context, f"invalid_numeric_fact:{name}")
    for name in ("initiator_exists", "target_exists", "at_war", "conflicting_treaty",
                 "ideology_compatible", "target_has_guarantee"):
        if not isinstance(getattr(context, name), bool):
            return _block(context, f"invalid_boolean_fact:{name}")
    if not isinstance(context.target_ally_count, int) or isinstance(context.target_ally_count, bool):
        return _block(context, "invalid_numeric_fact:target_ally_count")

    military_balance = math.tanh(math.log((context.initiator_military + 1) /
                                          (context.target_military + 1)) / 2)
    industrial_balance = math.tanh(math.log((context.initiator_factories + 1) /
                                            (context.target_factories + 1)) / 2)
    allied_support = min(context.target_ally_count, 5) / 5.0
    features = {
        "relative_size": max(-1.0, min(1.0, 1.0 - target_share)),
        "military_balance": military_balance,
        "industrial_balance": industrial_balance,
        "relations": context.relations / 100.0,
        "ideology_compatibility": 1.0 if context.ideology_compatible else -1.0,
        "target_stability": (context.target_stability - 50.0) / 50.0,
        "world_tension": context.world_tension / 100.0,
        "target_guarantee": 1.0 if context.target_has_guarantee else 0.0,
        "allied_support": allied_support,
        "voluntary_approach": 1.0 if context.approach == "voluntary_offer" else -1.0,
    }
    consent_logit = policy.consent_intercept + sum(
        policy.consent_weights.get(name, 0.0) * value for name, value in features.items()
    )
    consent = _sigmoid(consent_logit)

    crisis_features = {
        "world_tension": context.world_tension / 100.0,
        "target_size_share": target_share / policy.max_target_share_of_initiator,
        "target_guarantee": 1.0 if context.target_has_guarantee else 0.0,
        "allied_support": allied_support,
        "hostile_relations": max(0.0, -context.relations / 100.0),
        "ultimatum": 1.0 if context.approach == "ultimatum" else 0.0,
    }
    crisis_logit = policy.crisis_intercept + sum(
        policy.crisis_weights.get(name, 0.0) * value for name, value in crisis_features.items()
    )
    refusal = 1.0 - consent
    crisis_if_refused = _sigmoid(crisis_logit)

    return {
        "operation_id": context.operation_id,
        "initiator_tag": context.initiator_tag,
        "target_tag": context.target_tag,
        "status": "estimate_ready_for_player_review",
        "approach": context.approach,
        "target_state_share": round(target_share, 4),
        "consent_probability": round(consent, 4),
        "refusal_probability": round(refusal, 4),
        "crisis_probability_if_refused": round(crisis_if_refused, 4),
        "crisis_probability_overall": round(refusal * crisis_if_refused, 4),
        "factors": {name: round(value, 4) for name, value in features.items()},
        "evidence_sources": {name: context.evidence_sources[name] for name in _ASSESSMENT_FACTS},
        "model_notice": "Illustrative configurable heuristic; not calibrated or read from HOI4 outcome odds.",
        "execution": "not_prepared",
        "requires_player_confirmation": True,
        "game_effect_confirmed": False,
    }


def estimate_annexation_probability_bounds(
    context: AnnexationContext,
    policy: AnnexationPolicy = AnnexationPolicy(),
) -> dict:
    """Return conservative input-bound estimates without filling unknown facts.

    Unlike ``assess_political_annexation``, this helper may produce a range
    when execution-relevant facts are known but forecast factors are missing.
    Every missing or unsourced forecast factor is varied across its allowed
    feature domain. The result is an outer range from the illustrative policy,
    not a statistical confidence interval and never an execution decision.
    """
    baseline = assess_political_annexation(context, policy)
    if baseline.get("status") == "blocked":
        return {
            "status": "blocked", "reason": baseline.get("reason"),
            "missing_facts": baseline.get("missing_facts", []),
            "probability_bounds": None, "operation_started": False,
            "action_prepared": False, "game_effect_confirmed": False,
        }
    if baseline.get("status") == "incomplete" and baseline.get("reason") != "forecast_facts_unknown_or_unsourced":
        return {
            "status": "incomplete", "reason": baseline.get("reason"),
            "missing_facts": baseline.get("missing_facts", []),
            "probability_bounds": None, "operation_started": False,
            "action_prepared": False, "game_effect_confirmed": False,
        }

    # Validate known predictor inputs even when another predictor is missing;
    # otherwise a malformed value could be hidden by the incomplete-data path.
    numeric_ranges = {
        "initiator_military": (0.0, float("inf")),
        "target_military": (0.0, float("inf")),
        "initiator_factories": (0.0, float("inf")),
        "target_factories": (0.0, float("inf")),
        "relations": (-100.0, 100.0),
        "target_stability": (0.0, 100.0),
        "target_ally_count": (0.0, float("inf")),
        "world_tension": (0.0, 100.0),
    }
    for name, (minimum, maximum) in numeric_ranges.items():
        value = getattr(context, name)
        if value is None:
            continue
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not _bounded(value, minimum, maximum)
                or (name == "target_ally_count" and not isinstance(value, int))):
            return {
                "status": "blocked", "reason": f"invalid_numeric_fact:{name}",
                "probability_bounds": None, "operation_started": False,
                "action_prepared": False, "game_effect_confirmed": False,
            }
    for name in ("ideology_compatible", "target_has_guarantee"):
        value = getattr(context, name)
        if value is not None and not isinstance(value, bool):
            return {
                "status": "blocked", "reason": f"invalid_boolean_fact:{name}",
                "probability_bounds": None, "operation_started": False,
                "action_prepared": False, "game_effect_confirmed": False,
            }

    sources = context.evidence_sources
    def sourced(name: str) -> bool:
        return bool(str(sources.get(name, "")).strip())

    known: dict[str, float] = {
        "relative_size": max(-1.0, min(
            1.0, 1.0 - context.target_owned_states / context.initiator_owned_states,
        )),
        "voluntary_approach": 1.0 if context.approach == "voluntary_offer" else -1.0,
        "target_size_share": (
            context.target_owned_states / context.initiator_owned_states
            / policy.max_target_share_of_initiator
        ),
        "ultimatum": 1.0 if context.approach == "ultimatum" else 0.0,
    }
    unknown: dict[str, tuple[float, float]] = {}
    missing_predictors = []

    def predictor(name: str, feature: str, low: float, high: float,
                  transform=lambda value: value) -> None:
        value = getattr(context, name)
        if value is not None and sourced(name):
            known[feature] = transform(value)
        else:
            unknown[feature] = (low, high)
            if value is None:
                missing_predictors.append(name)
            elif not sourced(name):
                missing_predictors.append(f"evidence_sources.{name}")

    if (context.initiator_military is not None and context.target_military is not None
            and sourced("initiator_military") and sourced("target_military")):
        known["military_balance"] = math.tanh(math.log(
            (context.initiator_military + 1) / (context.target_military + 1)
        ) / 2)
    else:
        unknown["military_balance"] = (-1.0, 1.0)
        missing_predictors.extend(name for name in (
            "initiator_military", "target_military",
        ) if getattr(context, name) is None or not sourced(name))

    if (context.initiator_factories is not None and context.target_factories is not None
            and sourced("initiator_factories") and sourced("target_factories")):
        known["industrial_balance"] = math.tanh(math.log(
            (context.initiator_factories + 1) / (context.target_factories + 1)
        ) / 2)
    else:
        unknown["industrial_balance"] = (-1.0, 1.0)
        missing_predictors.extend(name for name in (
            "initiator_factories", "target_factories",
        ) if getattr(context, name) is None or not sourced(name))

    relation_value = context.relations
    if relation_value is not None and sourced("relations"):
        known["relations"] = relation_value / 100.0
        known["hostile_relations"] = max(0.0, -relation_value / 100.0)
    else:
        unknown["relations"] = (-1.0, 1.0)
        unknown["hostile_relations"] = (0.0, 1.0)
        missing_predictors.extend(
            name for name in ("relations",)
            if relation_value is None or not sourced(name)
        )
    predictor("ideology_compatible", "ideology_compatibility", -1.0, 1.0,
              lambda value: 1.0 if value else -1.0)
    predictor("target_stability", "target_stability", -1.0, 1.0,
              lambda value: (value - 50.0) / 50.0)
    predictor("world_tension", "world_tension", 0.0, 1.0, lambda value: value / 100.0)
    predictor("target_has_guarantee", "target_guarantee", 0.0, 1.0,
              lambda value: 1.0 if value else 0.0)
    predictor("target_ally_count", "allied_support", 0.0, 1.0,
              lambda value: min(value, 5) / 5.0)

    range_items = sorted(unknown.items())
    points = product(*[bounds for _, bounds in range_items]) if range_items else [()]
    consent_values = []
    crisis_values = []
    overall_values = []
    for point in points:
        features = dict(known)
        features.update({name: value for (name, _bounds), value in zip(range_items, point)})
        consent_feature_names = {
            "relative_size", "military_balance", "industrial_balance", "relations",
            "ideology_compatibility", "target_stability", "world_tension",
            "target_guarantee", "allied_support", "voluntary_approach",
        }
        consent_logit = policy.consent_intercept + sum(
            policy.consent_weights.get(name, 0.0) * value
            for name, value in features.items()
            if name in consent_feature_names
        )
        crisis_logit = policy.crisis_intercept + sum(
            policy.crisis_weights.get(name, 0.0) * features[name]
            for name in ("world_tension", "target_size_share", "target_guarantee",
                         "allied_support", "ultimatum")
        )
        consent = _sigmoid(consent_logit)
        crisis = _sigmoid(crisis_logit)
        consent_values.append(consent)
        crisis_values.append(crisis)
        overall_values.append((1.0 - consent) * crisis)

    complete = not unknown
    return {
        "status": "bounded_preview",
        "input_completeness": "complete" if complete else "partial",
        "target_state_share": round(
            context.target_owned_states / context.initiator_owned_states, 4,
        ),
        "consent_probability_range": {
            "minimum": round(min(consent_values), 4), "maximum": round(max(consent_values), 4),
        },
        "refusal_probability_range": {
            "minimum": round(1.0 - max(consent_values), 4),
            "maximum": round(1.0 - min(consent_values), 4),
        },
        "crisis_probability_if_refused_range": {
            "minimum": round(min(crisis_values), 4), "maximum": round(max(crisis_values), 4),
        },
        "crisis_probability_overall_range": {
            "minimum": round(min(overall_values), 4), "maximum": round(max(overall_values), 4),
        },
        "unknown_predictors": sorted(set(missing_predictors)),
        "known_predictor_sources": {
            name: str(sources[name]).strip() for name in _ASSESSMENT_FACTS
            if getattr(context, name) is not None and sourced(name)
        },
        "uncertainty_kind": "conservative_input_bounds_not_statistical_confidence_interval",
        "model_notice": (
            "Illustrative, uncalibrated policy heuristic. Missing or unsourced forecast inputs are varied over their allowed ranges; this does not predict HOI4 acceptance and cannot prepare or execute an action."
        ),
        "operation_started": False,
        "action_prepared": False,
        "game_effect_confirmed": False,
        "requires_player_confirmation": True,
    }


def project_integration(
    snapshot: IntegrationSnapshot,
    policy: IntegrationPolicy = IntegrationPolicy(),
) -> dict:
    """Project only the integration stages supported by available evidence.

    Early stages need a confirmed transfer, elapsed days, resistance, and
    compliance. Administrative capacity and investment are not currently
    emitted by the HOI4 probe, so they block advanced readiness only. A
    missing late-stage metric must not erase an earlier, evidence-backed
    stage. This function never creates a core or game effect.
    """
    sources = snapshot.evidence_sources if isinstance(snapshot.evidence_sources, Mapping) else {}

    def sourced(name: str) -> bool:
        return bool(str(sources.get(name, "")).strip())

    base = ("transfer_confirmed", "days_since_transfer")
    missing = [name for name in base if getattr(snapshot, name) is None]
    missing += [f"evidence_sources.{name}" for name in base if not sourced(name)]
    if missing:
        return {
            "status": "incomplete", "stage": None, "next_stage": None,
            "missing_facts": missing, "minimum_days_to_next_stage": None,
            "core_creation_authorized": False, "game_effect_confirmed": False,
        }
    if not isinstance(snapshot.transfer_confirmed, bool):
        return {"status": "blocked", "reason": "invalid_transfer_confirmation", "stage": None,
                "core_creation_authorized": False, "game_effect_confirmed": False}
    if snapshot.transfer_confirmed is not True:
        return {
            "status": "blocked", "reason": "territory_transfer_not_confirmed_in_game",
            "stage": None, "next_stage": None, "minimum_days_to_next_stage": None,
            "core_creation_authorized": False, "game_effect_confirmed": False,
        }
    if (not isinstance(snapshot.days_since_transfer, int)
            or isinstance(snapshot.days_since_transfer, bool)
            or snapshot.days_since_transfer < 0):
        return {"status": "blocked", "reason": "invalid_days_since_transfer", "stage": None,
                "core_creation_authorized": False, "game_effect_confirmed": False}

    metric_names = (
        "resistance", "cooperation", "administrative_capacity", "investment",
        "infrastructure", "stability",
    )
    for name in metric_names:
        value = getattr(snapshot, name)
        if (value is not None and (isinstance(value, bool)
                                   or not isinstance(value, (int, float))
                                   or not _bounded(value, 0, 100))):
            return {"status": "blocked", "reason": f"invalid_integration_fact:{name}", "stage": None,
                    "core_creation_authorized": False, "game_effect_confirmed": False}

    def available(name: str) -> bool:
        return getattr(snapshot, name) is not None and sourced(name)

    def check(name: str, passed: bool, failure: str, blockers: list[str]) -> None:
        if not available(name):
            blockers.append(f"{name}_unknown_or_unsourced")
        elif not passed:
            blockers.append(failure)

    days = snapshot.days_since_transfer
    stage = "newly_acquired"
    next_stage = "transition_administration"
    threshold = policy.transition_days
    blockers: list[str] = []

    if days >= policy.transition_days:
        stage, next_stage, threshold = "transition_administration", "partial_integration", policy.partial_days
        check("resistance", snapshot.resistance is not None
              and snapshot.resistance <= policy.partial_max_resistance,
              "resistance_above_partial_threshold", blockers)
        check("cooperation", snapshot.cooperation is not None
              and snapshot.cooperation >= policy.partial_min_cooperation,
              "cooperation_below_partial_threshold", blockers)

        if days >= policy.partial_days and not blockers:
            stage, next_stage, threshold = "partial_integration", "advanced_integration", policy.advanced_days

        if days >= policy.advanced_days and stage == "partial_integration":
            advanced_checks = (
                ("resistance", snapshot.resistance is not None
                 and snapshot.resistance <= policy.advanced_max_resistance,
                 "resistance_above_advanced_threshold"),
                ("cooperation", snapshot.cooperation is not None
                 and snapshot.cooperation >= policy.advanced_min_cooperation,
                 "cooperation_below_advanced_threshold"),
                ("administrative_capacity", snapshot.administrative_capacity is not None
                 and snapshot.administrative_capacity >= policy.advanced_min_administration,
                 "administration_below_advanced_threshold"),
                ("investment", snapshot.investment is not None
                 and snapshot.investment >= policy.advanced_min_investment,
                 "investment_below_advanced_threshold"),
                ("infrastructure", snapshot.infrastructure is not None
                 and snapshot.infrastructure >= policy.advanced_min_infrastructure,
                 "infrastructure_below_advanced_threshold"),
                ("stability", snapshot.stability is not None
                 and snapshot.stability >= policy.advanced_min_stability,
                 "stability_below_advanced_threshold"),
            )
            advanced_blockers: list[str] = []
            for name, passed, failure in advanced_checks:
                check(name, passed, failure, advanced_blockers)
            if not advanced_blockers:
                stage, next_stage, threshold = "advanced_integration", "core_review_only", policy.core_review_days
                blockers = []
                if days >= policy.core_review_days:
                    core_checks = (
                        ("resistance", snapshot.resistance is not None
                         and snapshot.resistance <= policy.core_review_max_resistance,
                         "resistance_above_core_review_threshold"),
                        ("cooperation", snapshot.cooperation is not None
                         and snapshot.cooperation >= policy.core_review_min_cooperation,
                         "cooperation_below_core_review_threshold"),
                        ("administrative_capacity", snapshot.administrative_capacity is not None
                         and snapshot.administrative_capacity >= policy.core_review_min_administration,
                         "administration_below_core_review_threshold"),
                        ("investment", snapshot.investment is not None
                         and snapshot.investment >= policy.core_review_min_investment,
                         "investment_below_core_review_threshold"),
                        ("infrastructure", snapshot.infrastructure is not None
                         and snapshot.infrastructure >= policy.core_review_min_infrastructure,
                         "infrastructure_below_core_review_threshold"),
                        ("stability", snapshot.stability is not None
                         and snapshot.stability >= policy.core_review_min_stability,
                         "stability_below_core_review_threshold"),
                    )
                    core_blockers: list[str] = []
                    for name, passed, failure in core_checks:
                        check(name, passed, failure, core_blockers)
                    if not core_blockers:
                        stage, next_stage, threshold = "core_review_only", None, None
                        blockers = ["core_effect_requires_separate_verified_implementation"]
                    else:
                        blockers = core_blockers
            else:
                blockers = advanced_blockers

    minimum_days = None if threshold is None else max(0, threshold - days)
    visible_sources = {
        name: str(sources[name]).strip() for name in base + metric_names
        if available(name)
    }
    return {
        "status": "projection_only",
        "stage": stage,
        "next_stage": next_stage,
        "minimum_days_to_next_stage": minimum_days,
        "readiness_blockers": blockers,
        "missing_facts": [item.removesuffix("_unknown_or_unsourced") for item in blockers
                          if item.endswith("_unknown_or_unsourced")],
        "core_creation_authorized": False,
        "game_effect_confirmed": False,
        "evidence_sources": visible_sources,
        "model_notice": "Projection thresholds are configurable prototype values, not HOI4 rules.",
        "prohibited_mechanics": ["forced_population_displacement", "forced_assimilation"],
    }


__all__ = [
    "AnnexationContext", "AnnexationPolicy", "IntegrationSnapshot", "IntegrationPolicy",
    "ComputerInitiativeSnapshot", "ComputerInitiativePolicy",
    "assess_political_annexation", "estimate_annexation_probability_bounds",
    "assess_computer_annexation_initiative", "project_integration",
]
