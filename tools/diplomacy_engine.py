"""Context-aware, rule-based diplomatic negotiation prototype.

The engine is independent of Flask and of HOI4 save parsing. It accepts a
normalized context mapping so a future real-game context adapter can replace
the current mock/save-reader inputs without changing negotiation logic.
"""

from collections import OrderedDict
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import random
import re
import threading
import uuid
from difflib import SequenceMatcher
from typing import Any, Dict, List, Mapping, Optional

from action_registry import ActionRegistry
from guarded_treaty_effects import (EFFECTS as GUARDED_EFFECTS, NEW_ACTIONS, PROPOSAL_CUES,
                                   exact_scope, build_package as build_guarded_treaty)
from execution_markers import matches_prepared_result
from diplomatic_doctrine import (
    assess_doctrine,
)
from diplomatic_planner import build_strategic_plan, build_point_decisions, resolve_point_scopes
from strategic_interests import refresh_interests, leader_style
from voices_adapter import counterpart_brief
from agreement_manager import (
    _TERM_LABELS,
    agreement_confirmation_review,
    build_guarantee_event_package,
    build_embargo_event_package,
    build_military_access_event_package,
    build_non_aggression_event_package,
    build_political_annexation_event_package,
    build_political_ultimatum_rejection_event_package,
    build_diplomatic_refusal_event_package,
    build_acceptance_version,
    consent_fingerprint,
    create_or_update_agreement,
    is_agreement_confirmation_question,
)
from language_detection import (
    SUPPORTED_LANGUAGES, detect_language, matches_output_language, normalize_language,
)
from territory_execution import build_territory_event_package
from political_annexation import (
    AnnexationContext,
    ComputerInitiativePolicy,
    ComputerInitiativeSnapshot,
    IntegrationSnapshot,
    assess_computer_annexation_initiative,
    assess_political_annexation,
    estimate_annexation_probability_bounds,
    project_integration,
)
from structured_intents import analyze_communication_acts, extract_duration
from hoi4_time import MAX_HOI4_ABSOLUTE_DAY
from hoi4_country_tags import COUNTRY_TAG_RE
from hoi4_country_names import load_country_names
from diplomatic_memory import (
    empty_state as empty_diplomatic_memory,
    normalize_state as normalize_diplomatic_memory,
    pair_summary as diplomatic_memory_pair_summary,
    record_confirmed_application as remember_confirmed_application,
    record_verified_game_observation as remember_verified_game_observation,
    record_validated_positions as remember_validated_positions,
)


INTENTS = {
    *NEW_ACTIONS,
    "greeting", "negotiate", "request", "demand", "threaten", "offer",
    "promise", "warn", "alliance", "non_aggression", "trade",
    "military_access", "guarantee", "embargo", "lift_embargo", "docking_rights", "air_base_access",
    "faction_join", "faction_leave", "market_access", "truce", "white_peace", "peace_conference",
    "peace", "ceasefire", "declare_war",
    "insult", "information", "refuse", "accept", "counter_offer",
    "territory_partition", "political_annexation", "unknown/general",
}

DECISIONS = {
    "accept", "reject", "counter_offer", "defer", "warn", "threaten",
    "request_more_information", "informational_reply",
}

MAX_MESSAGE_LENGTH = 4000
_PROVIDER_LOGGER = logging.getLogger("eyes.response_provider")
_COUNTRY_TAG_RE = COUNTRY_TAG_RE
_CAMPAIGN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
AGREEMENT_RECALCULATION_REVISION = 1
_PRIOR_REFUSAL_REFERENCE_RE = re.compile(
    r"\b(?:previous|prior|earlier|last|former)\s+(?:proposal[’']s?\s+)?(?:refusal|rejection|decline)\b|"
    r"\b(?:refusal|rejection|decline)\s+(?:of\s+)?(?:the\s+)?(?:previous|prior|earlier|last)\b|"
    r"\bwhy\s+(?:did|has)\s+(?:you|it|\w+)\s+(?:previously\s+)?refus(?:e|ed)\b|"
    r"\b(?:reasons?|grounds?)\s+for\s+(?:its|your|their|the)\s+(?:previous\s+|prior\s+|earlier\s+)?"
    r"(?:refusal|rejection|declin\w*)\b|"
    r"\b(?:предыдущ\w*|прошл\w*)\s+(?:отказ\w*|відмов\w*)|"
    r"\b(?:причин\w*|підстав\w*)\s+(?:попередн\w*|прошл\w*)?\s*(?:відмов\w*|отказ\w*)|"
    r"\b(?:refus|rejet|refus antérieur|refus précédent|rejet précédent)\b",
    re.IGNORECASE,
)


def _normalize_context_value(key: str, value: Any) -> str:
    if key in {"war_status", "same_faction", "existing_non_aggression_pact"}:
        if value is True or str(value).strip().lower() in {"yes", "true", "1"}:
            return "yes"
        if value is False or str(value).strip().lower() in {"no", "false", "0"}:
            return "no"
    return str(value).strip().lower()


@dataclass
class DiplomaticProfile:
    aggressiveness: float = 0.5
    trustworthiness: float = 0.5
    risk_tolerance: float = 0.5
    negotiation_willingness: float = 0.5
    values_alliances: float = 0.5
    dislikes_threats: float = 0.5
    territorial_ambition: float = 0.5
    influence_ambition: float = 0.5
    compromise_preference: float = 0.5
    security_priority: float = 0.5


@dataclass
class DiplomaticRelationship:
    relations: float = 0.0
    trust: float = 0.5
    # Distinct from chat-level goodwill: this changes only on marker-confirmed
    # agreement application and is therefore a durable promise-keeping signal.
    promise_trust: float = 0.5
    threat: float = 0.0
    opinion: float = 0.0
    war_status: Optional[bool] = None
    alliance_status: bool = False
    same_faction: Optional[bool] = None
    existing_non_aggression_pact: Optional[bool] = None
    recent_conflict: float = 0.0
    economic_dependence: float = 0.0
    # From the responding country's perspective: -1 means much weaker,
    # +1 means much stronger than the sender.
    military_balance: float = 0.0
    diplomatic_history: List[Dict[str, Any]] = field(default_factory=list)
    context_unknowns: List[str] = field(default_factory=list)

    @classmethod
    def from_context(cls, context: Mapping[str, Any]) -> "DiplomaticRelationship":
        relation = _number(context.get("relations", 0.0), 0.0, -100.0, 100.0)
        opinion = _number(context.get("opinion", relation), relation, -100.0, 100.0)
        derived_trust = 0.25 + ((relation + 100.0) / 200.0) * 0.4
        history = context.get("diplomatic_history", [])
        if not isinstance(history, list):
            history = []
        unknown_fields = [key for key in (
            "war_status", "same_faction", "existing_non_aggression_pact",
            "relations", "threat", "ideology_group", "major_power",
        ) if key not in context or context.get(key) is None
            or (isinstance(context.get(key), str)
                and context[key].strip().lower() in {"unknown", "unavailable", "none", ""})]
        same_faction = _as_bool(context.get("same_faction", context.get("faction_status")))
        return cls(
            relations=relation,
            trust=_number(context.get("trust"), derived_trust, 0.0, 1.0),
            threat=_number(context.get("threat", 0.0), 0.0, 0.0, 1.0),
            opinion=opinion,
            war_status=_as_bool(context.get("war_status", context.get("at_war"))),
            alliance_status=_as_bool(context.get("alliance_status", same_faction)) or False,
            same_faction=same_faction,
            existing_non_aggression_pact=_as_bool(context.get("existing_non_aggression_pact")),
            recent_conflict=_number(context.get("recent_conflict", 0.0), 0.0, 0.0, 1.0),
            economic_dependence=_number(context.get("economic_dependence", 0.0), 0.0, 0.0, 1.0),
            military_balance=_number(context.get("military_balance", 0.0), 0.0, -1.0, 1.0),
            diplomatic_history=history[-20:],
            context_unknowns=unknown_fields,
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DiplomaticMessage:
    sender: str
    receiver: str
    timestamp: str
    turn: int
    text: str
    intent: str
    decision: Optional[str]
    diplomatic_significance: str


@dataclass
class Conversation:
    conversation_id: str
    messages: List[DiplomaticMessage] = field(default_factory=list)
    relationship: Optional[DiplomaticRelationship] = None
    negotiation_state: Optional["NegotiationState"] = None
    group_relationships: Dict[str, "DiplomaticRelationship"] = field(default_factory=dict)
    group_negotiations: Dict[str, "NegotiationState"] = field(default_factory=dict)
    turn: int = 0
    response_language: Optional[str] = None


@dataclass
class NegotiationState:
    conversation_id: str
    sender: str
    receiver: str
    current_offer: str = ""
    requested_terms: List[str] = field(default_factory=list)
    unresolved_terms: List[str] = field(default_factory=list)
    round: int = 0
    previous_decision: Optional[str] = None
    trust_delta: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _number(value: Any, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _as_bool(value: Any) -> Optional[bool]:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "at_war", "allied", "same_faction"}:
            return True
        if normalized in {"0", "false", "no", "not_at_war", "not_allied", "different_faction"}:
            return False
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return None


class ChatConflict(ValueError):
    """A valid chat operation that is not allowed in the current turn state."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


_GAME_NEWS_ACTION_LABELS = {
    "en": {
        "non_aggression_pact": "non-aggression pact", "guarantee": "independence guarantee",
        "military_access": "military access", "embargo": "embargo",
        "lift_embargo": "embargo lifted", "political_annexation": "political annexation",
    },
    "ru": {
        "non_aggression_pact": "пакт о ненападении", "guarantee": "гарантия независимости",
        "military_access": "военный доступ", "embargo": "эмбарго",
        "lift_embargo": "отмена эмбарго", "political_annexation": "политическое присоединение",
    },
    "fr": {
        "non_aggression_pact": "pacte de non-agression", "guarantee": "garantie d’indépendance",
        "military_access": "accès militaire", "embargo": "embargo",
        "lift_embargo": "levée de l’embargo", "political_annexation": "annexion politique",
    },
    "uk": {
        "non_aggression_pact": "пакт про ненапад", "guarantee": "гарантія незалежності",
        "military_access": "військовий доступ", "embargo": "ембарго",
        "lift_embargo": "скасування ембарго", "political_annexation": "політичне приєднання",
    },
}


_NEWS_SOURCES = {"hoi4_game_log", "hoi4_state_snapshot"}


def _append_game_news(chat: Dict[str, Any], *, unique_key: str, status: str,
                      marker: str, participants: List[str], action_id: str,
                      source_request_id: Optional[str], source_plan_id: Optional[str] = None,
                      agreement_id: Optional[str] = None,
                      agreement_name: Optional[str] = None,
                      agreement_version: Optional[int] = None, term_id: Optional[str] = None,
                      proposal_id: Optional[str] = None, summary: Optional[str] = None,
                      source: str = "hoi4_game_log",
                      diplomatic_memory: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """Append one durable feed item only after an exact game result marker was verified."""
    news_id = hashlib.sha256(
        f"{chat.get('campaign_id')}:{chat.get('chat_id')}:{unique_key}".encode("utf-8")
    ).hexdigest()
    feed = chat.setdefault("diplomatic_news", [])
    if not isinstance(feed, list):
        feed = chat["diplomatic_news"] = []
    existing = next((item for item in feed if isinstance(item, dict)
                     and item.get("news_id") == news_id), None)
    if existing is not None:
        return existing

    language = chat.get("response_language")
    labels = _GAME_NEWS_ACTION_LABELS.get(language, _GAME_NEWS_ACTION_LABELS["en"])
    action_label = labels.get(action_id, action_id.replace("_", " "))
    country_pair = "–".join(str(tag) for tag in participants)
    name_prefix = f"{agreement_name}: " if isinstance(agreement_name, str) and agreement_name else ""
    if status == "execution_confirmed":
        headline = f"{name_prefix}{country_pair}: HOI4 confirmed the in-game effect — {action_label}."
    elif status == "execution_failed":
        headline = f"{name_prefix}{country_pair}: HOI4 recorded that the action was not applied — {action_label}."
    elif status == "decision_rejected":
        headline = f"{country_pair}: HOI4 recorded the final rejection of the proposal — {action_label}."
    elif status == "territory_rollback_confirmed":
        headline = f"{name_prefix}{country_pair}: HOI4 confirmed a territorial rollback."
    else:
        headline = f"{name_prefix}{country_pair}: HOI4 confirmed a territorial transfer."
    if language in {"ru", "uk", "fr"}:
        # Keep tags and internal IDs intact; only the user-facing sentence is localized.
        if language == "ru":
            headline = (f"{country_pair}: журнал HOI4 подтвердил игровой эффект «{action_label}»."
                        if status == "execution_confirmed" else
                        f"{country_pair}: журнал HOI4 подтвердил, что действие «{action_label}» не применено."
                        if status == "execution_failed" else
                        f"{country_pair}: HOI4 зарегистрировала окончательный отказ от предложения «{action_label}»."
                        if status == "decision_rejected" else
                        f"{country_pair}: журнал HOI4 подтвердил откат передачи территорий."
                        if status == "territory_rollback_confirmed" else
                        f"{country_pair}: журнал HOI4 подтвердил передачу территорий.")
        elif language == "uk":
            headline = (f"{country_pair}: журнал HOI4 підтвердив ігровий ефект «{action_label}»."
                        if status == "execution_confirmed" else
                        f"{country_pair}: журнал HOI4 підтвердив, що дію «{action_label}» не застосовано."
                        if status == "execution_failed" else
                        f"{country_pair}: HOI4 зареєструвала остаточну відмову від пропозиції «{action_label}»."
                        if status == "decision_rejected" else
                        f"{country_pair}: журнал HOI4 підтвердив відкат передачі територій."
                        if status == "territory_rollback_confirmed" else
                        f"{country_pair}: журнал HOI4 підтвердив передачу територій.")
        else:
            headline = (f"{country_pair} : HOI4 a confirmé l’effet en jeu — {action_label}."
                        if status == "execution_confirmed" else
                        f"{country_pair} : HOI4 a confirmé que l’action n’a pas été appliquée — {action_label}."
                        if status == "execution_failed" else
                        f"{country_pair} : HOI4 a enregistré le rejet définitif de la proposition — {action_label}."
                        if status == "decision_rejected" else
                        f"{country_pair} : HOI4 a confirmé le retour territorial."
                        if status == "territory_rollback_confirmed" else
                        f"{country_pair} : HOI4 a confirmé le transfert territorial.")
    if name_prefix and language in {"ru", "uk", "fr"}:
        headline = name_prefix + headline
    if status == "execution_verified":
        # A second, stricter feed item: a marker proves the event ran, this one
        # records that the game state itself says the effect is in force.
        number = {"ru": f"{country_pair}: состояние игры подтверждает, что договор действует — «{action_label}».",
                  "uk": f"{country_pair}: стан гри підтверджує, що договір чинний — «{action_label}».",
                  "fr": f"{country_pair} : l’état du jeu confirme que le traité est en vigueur — {action_label}.",
                  "en": f"{country_pair}: game state confirms the treaty is in force — {action_label}."}
        headline = (name_prefix if name_prefix and language in {"ru", "uk", "fr"} else "") + number.get(language, number["en"])
    if status in {"execution_confirmed", "execution_verified"} and language in {"ru", "en"}:
        # Opt-in: the catalog must never change existing installations' texts.
        try:
            from news_templates import enabled, headline as catalog_headline
            if enabled():
                varied = catalog_headline(status=status, participants=participants,
                                          action_id=action_id, language=language, key=news_id)
                if varied:
                    headline = varied
        except (ImportError, KeyError, TypeError, ValueError, OSError):
            pass
    item = {
        "news_id": news_id, "campaign_id": chat.get("campaign_id"),
        "conversation_id": chat.get("conversation_id"), "participants": list(participants),
        "status": status, "action_id": action_id, "headline": headline,
        "summary": summary, "source": source, "source_request_id": source_request_id,
        "source_plan_id": source_plan_id,
        "confirmation_marker": marker, "agreement_id": agreement_id,
        "agreement_version": agreement_version, "term_id": term_id,
        "proposal_id": proposal_id, "created_at": _utc_now(),
    }
    feed.append(item)
    if status == "execution_confirmed":
        # Text-only reactions to a marker-confirmed event; flag-off is a no-op.
        try:
            from third_country_reactions import auto_react_for_chat, native_reaction_contexts
            contexts = native_reaction_contexts(
                diplomatic_memory or {}, chat['campaign_id'], chat['player_country'], participants,
            )
            auto_react_for_chat(chat, participants, action_id, news_id, observed_contexts=contexts)
        except (ImportError, KeyError, TypeError, ValueError, OSError):
            pass
    return item


def _unenforced_effect_details(term: Mapping[str, Any], action_id: str) -> List[str]:
    """Return negotiated details that the current HOI4 event cannot enforce.

    Agreement terms keep parsed action details inside ``proposal_spec.action``;
    the event builders historically inspected only a few top-level legacy
    fields. Check both representations before generating any event so a
    five-year promise cannot silently become the builder's fixed 365-day NAP.
    """
    proposal = term.get("proposal_spec")
    action_spec = proposal.get("action") if isinstance(proposal, Mapping) else None
    if not isinstance(action_spec, Mapping):
        action_spec = {}

    conditions = term.get("conditions") or action_spec.get("conditions") or []
    exceptions = term.get("exceptions") or action_spec.get("exceptions") or []
    duration = term.get("duration") or action_spec.get("duration")
    unsupported: List[str] = []
    if conditions:
        unsupported.append("conditions")
    if exceptions:
        unsupported.append("exceptions")
    if duration:
        # HOI4's diplomatic_relation effect does not accept a treaty duration.
        # The game expires NAPs using NAP_EXPIRY_MONTHS (48 months in vanilla).
        unsupported.append("duration")
    return unsupported


def _agreement_execution_gaps(terms: Any, registry: ActionRegistry, *,
                              chat: Mapping[str, Any]) -> List[str]:
    """List every agreed clause that cannot be proven and enacted by a game event.

    A treaty must not be marked executed when even one accepted clause is only
    prose, unresolved, or missing exact consent evidence. Each supported clause
    is still executed by its own guarded event.
    """
    if not isinstance(terms, list) or not terms:
        return ["agreement_terms_missing"]
    gaps = []
    for term in terms:
        if not isinstance(term, Mapping):
            gaps.append("invalid_term")
            continue
        term_id = term.get("term_id")
        label = term_id if isinstance(term_id, str) and term_id else "unnamed_term"
        if term.get("status") != "agreed":
            gaps.append(f"{label}:not_agreed")
        from agreement_manager import nap_territory_exclusion
        exclusion = nap_territory_exclusion(term)
        action_id = term.get("action_id")
        action = registry.get(action_id) if isinstance(action_id, str) else None
        if not exclusion and (not action or action.get("supported_by_hoi4") is not True
                or action.get("implemented_in_eyes") is not True
                or action_id == "territory_partition"  # Separate versioned allocation workflow.
                or action.get("eyes_scripted_execution_supported", True) is not True
                or term.get("supported_by_hoi4") is not True):
            gaps.append(f"{label}:no_scripted_game_effect")
            continue
        required = term.get("required_participants")
        confirmed = term.get("confirmed_by")
        request_ids = term.get("confirmed_by_request_ids")
        if (not isinstance(required, list) or not required
                or any(not isinstance(tag, str) for tag in required)
                or not set(required).issubset(set(confirmed if isinstance(confirmed, list) else []))
                or not isinstance(request_ids, Mapping)
                or any(not isinstance(request_ids.get(tag), str) or not request_ids[tag].strip()
                       for tag in required)):
            gaps.append(f"{label}:exact_signatory_confirmation_missing")
        elif not exclusion and (len(required) != 2 or len(set(required)) != 2
                or chat.get("player_country") not in required
                or any(tag not in chat.get("participants", []) for tag in required)):
            gaps.append(f"{label}:no_executor_for_signatory_scope")
        if action_id in GUARDED_EFFECTS and (len(terms) != 1 or not exact_scope(chat,term)):
            gaps.append(f"{label}:exact_standalone_bilateral_proposal_required")
        details = _unenforced_effect_details(term, action_id)
        if details:
            gaps.append(f"{label}:unenforced_{','.join(details)}")
        if term.get("unparsed_execution_details"):
            gaps.append(f"{label}:unparsed_execution_details")
        if term.get("term_id") in {"military_access", "independence_guarantee"}:
            from agreement_manager import directional_parties
            actor, recipient = directional_parties(term)
            if (actor or recipient) and (actor == recipient or {actor, recipient} != set(required or [])):
                gaps.append(f"{label}:direction_outside_signatories")
        from guarded_treaty_effects import reciprocal_direction_unresolved
        if reciprocal_direction_unresolved(term, chat):
            gaps.append(f"{label}:reciprocal_direction_not_enforceable")
    return sorted(set(gaps))


def _prepare_final_refusal_event(chat: Dict[str, Any], source: Any,
                                 response: Dict[str, Any]) -> None:
    """Attach an inert event package only to a final generated structured refusal."""
    if (not isinstance(source, Mapping) or response.get("decision") != "reject"
            or response.get("provider_status") != "generated"
            or response.get("retryable") is True
            or not isinstance(response.get("dialogue_positions"), list)):
        return
    source_id = source.get("request_id")
    linked = [position for position in response["dialogue_positions"]
              if isinstance(position, Mapping)
              and position.get("stance") == "reject"
              and position.get("source_request_id") == source_id
              and isinstance(position.get("action_id"), str)]
    if len(linked) != 1:
        return
    try:
        if linked[0].get("action_id") == "political_annexation":
            source_text = str(source.get("text") or "")
            is_pressure = bool(re.search(
                r"\b(?:ultimatum|political\s+demand|demand\s+annexation|under\s+political\s+pressure)\b|"
                r"ультиматум\w*|политическ\w+ требован|под политическ\w+ давлен",
                source_text, re.IGNORECASE,
            ))
            package = (build_political_ultimatum_rejection_event_package(chat, source, response)
                       if is_pressure else build_diplomatic_refusal_event_package(chat, source, response))
        else:
            package = build_diplomatic_refusal_event_package(chat, source, response)
    except (TypeError, ValueError):
        # Ambiguous or non-official messages remain ordinary chat and must not
        # create a world event or an implied decision.
        return
    response["diplomatic_event_package"] = package


def _country_tag(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} entries must be country tags")
    tag = value.strip().upper()
    if not _COUNTRY_TAG_RE.fullmatch(tag):
        raise ValueError(f"{field_name} entries must be three-character HOI4 country tags")
    return tag


def _validated_state_id_list(value: Any, field_name: str) -> List[int]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list of HOI4 state IDs")
    result = []
    for item in value:
        if isinstance(item, bool):
            raise ValueError(f"{field_name} contains an invalid state ID")
        try:
            state_id = int(item)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{field_name} contains an invalid state ID") from error
        if state_id <= 0 or state_id != item:
            raise ValueError(f"{field_name} state IDs must be positive integers")
        result.append(state_id)
    if len(set(result)) != len(result):
        raise ValueError(f"{field_name} contains duplicate state IDs")
    return result


def _normalize_country_contexts(value: Mapping[str, Any], participants: List[str]) -> Dict[str, Dict[str, Any]]:
    if not isinstance(value, Mapping):
        raise ValueError("country_contexts must be an object keyed by participant tag")
    normalized = {}
    for raw_tag, context in value.items():
        tag = _country_tag(raw_tag, "country_contexts")
        if tag not in participants:
            raise ValueError(f"country_contexts contains non-participant {tag}")
        if not isinstance(context, Mapping):
            raise ValueError(f"country_contexts.{tag} must be an object")
        normalized[tag] = deepcopy(dict(context))
    return normalized


def _validated_message(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("message must be a non-empty string")
    text = value.strip()
    if len(text) > MAX_MESSAGE_LENGTH:
        raise ValueError("message exceeds maximum length")
    return text


def _normalize_territory_target(target: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(target, Mapping):
        raise ValueError("target must be an object")
    scope = str(target.get("scope") or "").strip().lower()
    if scope not in {"states", "region", "country"}:
        raise ValueError("target.scope must be states, region, or country")
    label = target.get("label")
    if not isinstance(label, str) or not label.strip():
        raise ValueError("target.label must be a non-empty string")
    state_ids = _validated_state_id_list(target.get("state_ids"), "target.state_ids")
    if not state_ids:
        raise ValueError("target.state_ids must name the exact HOI4 states under discussion")
    return {"scope": scope, "label": label.strip()[:200], "state_ids": sorted(state_ids)}


def _normalize_territory_allocations(allocations: Mapping[str, Any], state_ids: List[int],
                                     participants: List[str]) -> Dict[str, List[int]]:
    if not isinstance(allocations, Mapping):
        raise ValueError("allocations must map participant tags to state ID lists")
    participant_set = set(participants)
    normalized = {tag: [] for tag in participants}
    seen = set()
    for raw_tag, raw_states in allocations.items():
        tag = _country_tag(raw_tag, "allocations")
        if tag not in participant_set:
            raise ValueError(f"allocations contains non-participant {tag}")
        assigned = _validated_state_id_list(raw_states, f"allocations.{tag}")
        if seen.intersection(assigned):
            raise ValueError("each state may be assigned to only one participant")
        seen.update(assigned)
        normalized[tag] = sorted(assigned)
    expected = set(state_ids)
    if seen != expected:
        missing = sorted(expected - seen)
        extra = sorted(seen - expected)
        raise ValueError(f"allocations must cover each target state exactly once (missing={missing}, extra={extra})")
    return normalized


def allocation_owner(allocations: Mapping[str, List[int]], state_id: int) -> str:
    for tag, states in allocations.items():
        if state_id in states:
            return tag
    raise ValueError(f"state {state_id} is not assigned in this proposal")


class IntentClassifier:
    """Small multilingual keyword/phrase classifier; not an LLM or parser."""

    RULES = [
        ("territory_partition", r"\b(transfer\w*.{0,90}(?:city|region|area|territor)|\bcede\b|переда.{0,80}(?:област|территор)|territorial partition|partition(?:ing)? (?:the )?territor(?:y|ies)|divide (?:the )?territor(?:y|ies)|state transfer|раздел территори[ийю]|разделить территори[юи]|передача территори[ийю]|перераспределени[ея] территори[ийю]|розподіл територі[йї]|поділ територі[йї]|répartition des territoires|répartissons les territoires|partage des territoires)\b"),
        ("lift_embargo", r"\b(lift|remove|end) (?:the )?embargo\b|сним\w* эмбарго|сня\w* эмбарго|отмен\w* эмбарго|скасув\w* ембарго|знят\w* ембарго|lever l'embargo"),
        ("embargo", r"\b(?:impose|place|enact|declare|introduce) (?:an? )?embargo\b|\bembargo (?:against|on)\b|ввод\w* эмбарго|введ\w* эмбарго|объяв\w* эмбарго|ембарго проти|встанов\w* ембарго|imposer un embargo|embargo contre"),
        ("declare_war", r"\b(declare war|declaration of war|объявля(?:ю|ем|йте) войну|начинаем войну)\b"),
        ("threaten", r"\b(if you|unless you|or else|there will be consequences|we will attack|угрож|иначе будут последствия|если вы не|последствия)\b"),
        ("market_access", r"\b(market access|access to the market|доступ к рынку)\b"),
        ("docking_rights", r"\b(docking rights|naval basing rights|права стоянки|доступ в порт)\b"),
        ("air_base_access", r"\b(air.?base access|airfield access|доступ к авиабазам|доступ на авиабазу)\b"),
        ("faction_join", r"\b(join (our|the|your) faction|admitting.{0,25}faction|принять.{0,25}фракц|admettre.{0,25}faction|вступить в (наш )?альянс|вступайте в фракцию)\b"),
        ("white_peace", r"\b(white peace|status quo peace|белый мир|paix blanche)\b"),
        ("faction_leave", r"\b(removing.{0,25}faction|исключить.{0,25}фракц|exclure.{0,25}faction)\b"),
        ("peace_conference", r"\b(peace conference|мирная конференция)\b"),
        ("truce", r"\b(truce|armistice|перемири[ея])\b"),
        ("insult", r"\b(insult|coward|fool|шут|трус|глупц|ничтож)\b"),
            ("insult", r"\b(insult|cowards?|fools?|шут|трус|глупц|ничтож)\b"),
        ("refuse", r"\b(refuse|reject|decline|cannot accept|will not accept|отказыва|отверга|не согласны|нет,?|відмовля|відхиля|не погоджуємось|refuser|rejeter|déclinons|ne pouvons pas accepter)\b"),
        ("political_annexation", r"\b(?:peaceful|voluntary|political)\s+(?:annexation|incorporation|unification|union)\b|\bannexation\b|peaceful.{0,25}(?:annex|incorporat|unif)|voluntary.{0,25}(?:annex|incorporat|unif)|мирн\w*.{0,25}(?:аннекс|присоедин|объедин)|добровольн\w*.{0,25}(?:присоедин|объедин)|политическ\w*.{0,25}(?:аннекс|присоедин|объедин)|аннекс\w*|присоедин\w*|annexion\s+pacifique|rattachement\s+(?:volontaire|pacifique)|incorporation\s+pacifique|unification\s+pacifique"),
        ("counter_offer", r"\b(counter.?offer|in exchange|instead,? we|взамен|встречное предложение|вместо этого|натомість пропонуємо|зустрічна пропозиція|contre-proposition|en échange|nous proposons plutôt)\b"),
        ("ceasefire", r"\b(cease.?fire|armistice|stop fighting|прекращени[ея] огня|перемири[ея])\b"),
        ("peace", r"\b(peace treaty|make peace|peace offer|мирный договор|предлагаем мир|мирное соглашение)\b"),
        ("non_aggression", r"\b(non.?aggression|non.?aggression pact|пакт о ненападении|договор о ненападении|ненападени|пакт про ненапад|угода про ненапад|pacte de non-agression|non-agression)\b"),
        ("military_access", r"\b(military access|access through|right of passage|военный доступ|проход войск|пропустите войска)\b"),
        ("guarantee", r"\b(guarantee|guarantee our independence|гаранти(?:руйте|я)|гарантия независимости)\b"),
        ("alliance", r"\b(alliance|ally with|defensive pact|союз|вступайте в альянс|оборонительный пакт)\b"),
        ("trade", r"\b(trade agreement|trade deal|trade|commercial agreement|торгов(?:ое соглашение|ля|ый договор)|торговл)\b"),
        ("promise", r"\b(promise|pledge|we will not|we shall not|обеща|обязуемся)\b"),
        ("warn", r"\b(warn|warning|be advised|предупрежда|предупреждение)\b"),
        ("demand", r"\b(demand|must give|must withdraw|hand over|surrender|требу(?:ем|ю)|передайте нам|отведите войска|уступите территори)\b"),
        ("request", r"\b(request|please|could you|would you|we ask|просим|пожалуйста|не могли бы)\b"),
        ("offer", r"\b(offer|propose|proposal|we are prepared to|предлагаем|предлагаю|предложение|готовы предложить|пропоную|пропонуємо|пропозиція|nous proposons|je propose|proposition)\b"),
        ("negotiate", r"\b(negotiate|negotiations|talks|discuss terms|переговор|обсудим условия|давайте обсудим|переговори|обговорімо умови|négocier|négociations|discuter des conditions)\b"),
        ("greeting", r"\b(hello|hi|greetings|good morning|bonjour|salut|привет|здравствуй|добрый день|вітаю|привіт)\b"),
        ("information", r"\b(why|what|when|where|how|explain|clarify|почему|что вы|когда|где|как|объясните|уточните|чому|що саме|коли|де|як|поясніть|уточніть|pourquoi|que|quoi|quand|où|comment|expliquez|précisez|quelles?)\b|\?"),
    ]

    CLARIFICATION_REQUEST = re.compile(
        r"\b(list|name|specify|explain|clarify|what exactly|which specific|"
        r"three (?:specific )?(?:terms|conditions)|do you require|any territorial|"
        r"перечислите|назовите|сформулируйте|объясните|уточните|"
        r"что именно|какие именно|какие конкретные|три конкретн\w*|"
        r"требуете ли|есть ли территори|"
        r"перелічіть|назвіть|сформулюйте|поясніть|уточніть|"
        r"що саме|які саме|які конкретні|три конкретн\w*|"
        r"вимагаєте чи|територіальн\w* вимог|"
        r"précisez|expliquez|énumérez|quelles? conditions|quels? termes|"
        r"exigez-vous|conditions territoriales)\b",
        re.IGNORECASE,
    )
    # A real offer often ends with a request such as “state your position” or
    # “explain your reasoning”. That request is subordinate to the concrete
    # proposal at the start of the same message. Keep open clarification turns
    # informational, but let an explicit country-authored proposal reach the
    # action classifier instead of being downgraded before strategy selection.
    EXPLICIT_PROPOSAL_ACT = re.compile(
        r"\b(?:i|we|nous|je|мы|я|ми)\s+(?:(?:hereby|formally)\s+)?"
        r"(?:propose|proposes|offer|offers|предлагаю|предлагаем|предлагает|"
        r"пропоную|пропонуємо|proposons|offrons)\b|"
        r"\b(?!you\b|they\b|he\b|she\b)[\w.'’\-]+"
        r"(?:\s+[\w.'’\-]+){0,2}\s+"
        r"(?:proposes|propose|offers|offer|demands|demand|"
        r"proposent|proposez|offre|offrent|предлагает|предлагают|"
        r"пропонує|пропонують)\b",
        re.IGNORECASE,
    )

    # Open questions about national aims often contain proposal vocabulary
    # (“what terms are you prepared to offer?”). Detect the speech act before
    # the ordered action rules so that a question cannot become a treaty offer.
    NATIONAL_POSITION_GOALS = re.compile(
        r"\b(?:what\s+(?:do\s+you\s+want\s+to\s+achieve|does\s+\w+\s+want\s+to\s+achieve)|"
        r"what\s+are\s+(?:your|\w+'s)\s+(?:aims?|goals?|objectives?|interests?)|"
        r"what\s+(?:are|is)\s+(?:the\s+)?(?:aims?|goals?|objectives?|interests?)\s+of\s+\w+|"
        r"what\s+does\s+\w+\s+(?:want|seek|aim)\s+to\s+(?:achieve|gain|secure)|"
        r"quels?\b.{0,100}\b(?:objectifs?|intérêts?)\b|"
        r"que\s+souhaitez-vous\s+(?:obtenir|atteindre)|"
        r"quels?\s+sont\s+les\s+(?:objectifs?|intérêts?)\s+de\s+\w+|"
        r"чего\s+вы\s+(?:хотите|стремитесь)\s+(?:добиться|достичь|получить)|"
        r"чего\s+вы\s+(?:хотите|стремитесь|намерены)\s+(?:добиться|достичь|обеспечить).{0,140}\bотношени\w*|"
        r"каких\s+(?:именно\s+)?цел\w*\s+вы\s+(?:хотите|стремитесь|намерены)\s+"
        r"(?:добиться|достичь|обеспечить).{0,140}\bотношени\w*|"
        r"каков\w*\s+(?:ваш\w*\s+)?(?:цел\w*|интерес\w*|позици\w*)|"
        r"какие\s+(?:ваши\s+)?(?:цел\w*|интерес\w*|позици\w*)|"
        r"(?:каков\w*|какие)\s+(?:основн\w*\s+)?цел\w*\s+(?:у\s+)?(?:германи\w*|франци\w*|\w+)|"
        r"цел\w*\s+(?:германи\w*|франци\w*|\w+)\s+(?:и\s+)?(?:услови\w*|способ\w*)\s+улучш\w*\s+отношени\w*|"
        r"услови\w*\s+улучш\w*\s+(?:наши\s+)?отношени\w*|"
        r"что\s+(?:германи\w*|франци\w*|\w+)\s+(?:хочет|стремится|готов\w*)\s+.{0,100}\bотношени\w*|"
        r"какие\s+услови\w*\s+(?:нужн\w*|необходим\w*|готов\w*)\s+.{0,100}\bотношени\w*|"
        r"чего\s+германи\w*\s+(?:хочет|добивается)|"
        r"яких\s+(?:ваших\s+)?(?:ціл\w*|інтерес\w*|позиці\w*)|"
        r"чого\s+ви\s+(?:хочете|прагнете)\s+(?:досягти|отримати))\b",
        re.IGNORECASE | re.DOTALL,
    )
    NATIONAL_POSITION_TERMS = re.compile(
        r"\b(?:what\s+(?:terms?|conditions?)\s+(?:would|could|will)\s+you\s+(?:offer|propose)|"
        r"quelles?\s+(?:conditions?|termes?)\s+proposeriez-vous|"
        r"какие\s+услови\w*\s+готов\w*\s+предложить|"
        r"що\s+ви\s+готов\w*\s+запропонувати)\b",
        re.IGNORECASE,
    )
    RELATION_IMPROVEMENT = re.compile(
        r"улучш\w*\s+(?:наши\s+)?отношени\w*|укреп\w*\s+(?:наши\s+)?отношени\w*|"
        r"improv\w*\s+(?:our\s+)?relations?|strengthen\w*\s+(?:our\s+)?relationship|"
        r"amélior\w*\s+(?:nos\s+)?relations?|renforc\w*\s+(?:nos\s+)?relations?",
        re.IGNORECASE,
    )

    @classmethod
    def is_national_position_request(cls, text: str) -> bool:
        if not isinstance(text, str) or not re.search(r"[?？]", text):
            return False
        return bool(cls.NATIONAL_POSITION_GOALS.search(text) or (
            cls.NATIONAL_POSITION_TERMS.search(text)
            and cls.RELATION_IMPROVEMENT.search(text)
        ))

    @staticmethod
    def is_prior_terms_comparison(text: str) -> bool:
        """Recognize a question comparing terms just discussed, not a fresh agenda request."""
        if not isinstance(text, str) or not re.search(r"[?？]", text):
            return False
        return bool(re.search(
            r"(?:из|среди)\s+(?:(?:этих|предложенн\w*|названн\w*|упомянут\w*)\s+"
            r"(?:вами\s+)?(?:услови\w*|мер\w*|шаг\w*)|"
            r"(?:услови\w*|мер\w*|шаг\w*)\s*,?\s*котор\w*\s+вы\s+назвал\w*)|"
            r"какое\s+из\s+(?:этих|предложенн\w*)\s+(?:услови\w*|мер\w*)|"
            r"which\s+of\s+(?:these|those)\s+(?:terms?|conditions?|measures?)|"
            r"among\s+(?:these|those)\s+(?:terms?|conditions?|measures?)|"
            r"parmi\s+ces\s+(?:conditions?|mesures?|termes?)|"
            r"entre\s+ces\s+(?:conditions?|mesures?|termes?)",
            text, re.IGNORECASE,
        ))

    def classify(self, text: str) -> str:
        normalized = text.lower().replace("ё", "е")
        if self.is_national_position_request(normalized):
            return "information"
        # Agreement-status questions can list words such as "accept" as
        # answer choices. Treat the whole request as a question, not as the
        # player's acceptance of those terms.
        if is_agreement_confirmation_question(normalized):
            return "information"
        if re.search(
                r"\b(?:agree|agrees|willing)\s+to\s+(?:consider|review|discuss)\b|"
                r"согласн\w*\s+(?:рассмотреть|обсудить)|готов\w*\s+(?:рассмотреть|обсудить)",
                normalized, re.I):
            return "information"
        if (self.CLARIFICATION_REQUEST.search(normalized)
                and not self.EXPLICIT_PROPOSAL_ACT.search(normalized)):
            return "information"
        if re.match(r"\s*(why|what|when|where|how|почему|что|когда|где|как|чому|що|коли|де|як|pourquoi|que|quoi|quand|où|comment|quelles?)\b", normalized):
            return "information"
        for action, cue in PROPOSAL_CUES.items():
            if re.search(cue, normalized, re.I):
                return action
        # A stray "agree" inside a condition (for example, "after both
        # countries agree") is not the speaker accepting the offer. Require a
        # first-person acceptance statement and keep conditional acceptance
        # out of the unconditional accept path.
        explicit_acceptance = re.search(
            r"\b(?:i|we)\s+(?:(?:hereby|formally)\s+)?(?:accept\w*|agree|approve\w*)\b|"
            r"\b(?:nous)\s+(?:acceptons|approuvons|sommes d'accord)\b|"
            r"\b(?:wir|ich)\s+(?:akzeptieren|stimme(?:n)? zu)\b|"
            r"\b(?:мы|я)\s+(?:принима\w+|согласн\w+|одобря\w+)\b|"
            r"\b(?:ми)\s+(?:прийма\w+|погоджуєм\w+|схвалю\w+)\b|"
            r"\b(?:nós)\s+(?:aceitamos|concordamos)\b|"
            r"\b(?:aceptamos|estamos de acuerdo)\b|"
            r"\b(?:zgadzam(?:y)? się|przyjmujemy|akceptujemy)\b|"
            r"^\s*deal\s*[.!]?$",
            normalized, re.I,
        )
        if explicit_acceptance:
            conditional_cue = re.search(
                r"\b(?:if|unless|provided that|subject to|only if|except when)\b|"
                r"\b(?:при условии|только если|если|за умови|лише якщо|якщо)\b|"
                r"\b(?:à condition que|sous réserve de|seulement si|à moins que)\b|"
                r"\b(?:wenn|sofern|unter der bedingung)\b|"
                r"\b(?:se|desde que|somente se|pod warunkiem)\b",
                normalized, re.I,
            )
            return "counter_offer" if conditional_cue else "accept"
        for intent, pattern in self.RULES:
            if re.search(pattern, normalized, flags=re.IGNORECASE):
                return intent
        return "unknown/general"


class DiplomacyEngine:
    # Retry is a bounded recovery path for technical/semantic generation
    # failures. Two retries after the initial response are enough to recover
    # transient local-model faults without leaving a permanent UI loop.
    MAX_RESPONSE_RETRIES = 2

    """In-memory diplomatic engine with bounded per-conversation history."""

    def __init__(self, max_history: int = 40, max_conversations: int = 500,
                 seed: Optional[int] = None, response_text_provider=None):
        self.max_history = max(2, int(max_history))
        self.max_conversations = max(1, int(max_conversations))
        self.classifier = IntentClassifier()
        self.action_registry = ActionRegistry()
        self.response_text_provider = response_text_provider
        # In-process capability: only advance_chat may designate a persisted
        # Bridge observation as trusted for engine-owned factual summaries.
        self._verified_observation_capability = object()
        self._verified_memory_capability = object()
        self.diplomatic_memory = empty_diplomatic_memory()
        self.save_memories = {}
        self.save_references = {}
        self.active_save_by_campaign = {}
        self.direct_point_policy_state = {"engine_point_policies": {}, "engine_point_policy_versions": {}}
        self.conversations: "OrderedDict[str, Conversation]" = OrderedDict()
        # Group chat state is intentionally unbounded by conversation eviction:
        # each chat owns a child engine and its own transcript until it is closed.
        self.chats: Dict[str, Dict[str, Any]] = {}
        self._chat_engines: Dict[str, "DiplomacyEngine"] = {}
        self._chat_retry_locks: Dict[str, threading.Lock] = {}
        # Autonomous computer-to-computer initiatives are a separate opt-in.
        # Old V5 snapshots omit this setting and must remain safely disabled.
        self.autonomous_initiatives_enabled = False
        self.random = random.Random(seed)
        # Keep scheduler randomness independent from diplomatic response randomness.
        self.initiative_random = random.Random(None if seed is None else seed + 7877)
        self._agreement_recalculation_lock = threading.RLock()
        self.computer_annexation_initiatives: Dict[str, Any] = {
            "by_evaluation_id": {}, "request_index": {}, "scheduler_by_country": {},
        }
        self.campaign_integration_observations: Dict[str, Any] = {
            "by_campaign_player": {}, "request_index": {},
            "snapshots_by_campaign_player": {}, "snapshot_index": {},
            "latest_snapshot_by_campaign_player": {},
        }

    @staticmethod
    def _conversation_snapshot(conversation: Conversation) -> Dict[str, Any]:
        return asdict(conversation)

    @staticmethod
    def _conversation_from_snapshot(value: Mapping[str, Any]) -> Conversation:
        relationship = value.get("relationship")
        negotiation = value.get("negotiation_state")
        return Conversation(
            conversation_id=value["conversation_id"],
            messages=[DiplomaticMessage(**item) for item in value.get("messages", [])],
            relationship=DiplomaticRelationship(**relationship) if relationship else None,
            negotiation_state=NegotiationState(**negotiation) if negotiation else None,
            group_relationships={key: DiplomaticRelationship(**item) for key, item in
                                 value.get("group_relationships", {}).items()},
            group_negotiations={key: NegotiationState(**item) for key, item in
                                value.get("group_negotiations", {}).items()},
            turn=int(value.get("turn", 0)),
            response_language=value.get("response_language"),
        )

    @staticmethod
    def _tuple_tree(value):
        return tuple(DiplomacyEngine._tuple_tree(item) for item in value) if isinstance(value, list) else value

    def export_chat_state(self) -> Dict[str, Any]:
        """Return JSON-safe chat, proposal, idempotency, and engine history state."""
        return {
            "chats": deepcopy(self.chats),
            "save_memories": deepcopy(self.save_memories),
            "save_references": deepcopy(self.save_references),
            "active_save_by_campaign": deepcopy(self.active_save_by_campaign),
            "direct_point_policy_state": deepcopy(self.direct_point_policy_state),
            "conversations": [self._conversation_snapshot(value) for value in self.conversations.values()],
            "random_state": self.random.getstate(),
            "initiative_random_state": self.initiative_random.getstate(),
            "computer_annexation_initiatives": deepcopy(self.computer_annexation_initiatives),
            "campaign_integration_observations": deepcopy(self.campaign_integration_observations),
            # Optional in older V5 snapshots; keeping it in the existing JSON
            # payload avoids a destructive/new SQL migration.
            "diplomatic_memory": deepcopy(self.diplomatic_memory),
            "runtime_settings": {
                "autonomous_initiatives_enabled": self.autonomous_initiatives_enabled,
            },
            "chat_engines": {
                chat_id: {
                    "conversations": [child._conversation_snapshot(value)
                                      for value in child.conversations.values()],
                    "random_state": child.random.getstate(),
                }
                for chat_id, child in self._chat_engines.items()
            },
        }

    def import_chat_state(self, state: Mapping[str, Any]) -> None:
        """Restore a validated store snapshot, retaining this engine's configured provider."""
        chats = state.get("chats", {})
        if not isinstance(chats, dict):
            raise ValueError("stored chats must be an object")
        self.chats = deepcopy(chats)
        memories = state.get("save_memories", {})
        references = state.get("save_references", {})
        selected = state.get("active_save_by_campaign", {})
        if not all(isinstance(v,dict) for v in (memories,references,selected)):
            raise ValueError('Invalid save history indexes')
        for key,reference in references.items():
            if (not isinstance(key,str) or not re.fullmatch('[a-f0-9]{64}',key)
                    or not isinstance(reference,dict) or reference.get('save_id')!=key
                    or not isinstance(reference.get('name'),str)
                    or any(reference.get(field) is not None and not isinstance(reference[field],str)
                           for field in ('date','filename','path'))):
                raise ValueError('Invalid saved history reference')
        if any(k not in references for k in memories):raise ValueError('Orphan save memory')
        if any(not _CAMPAIGN_ID_RE.fullmatch(str(k)) or v not in references for k,v in selected.items()):
            raise ValueError('Invalid selected save history')
        for chat in chats.values():
            scope=chat.get('save_scope_id')
            if scope is not None and (scope not in references or (chat.get('save_reference') or {}).get('save_id')!=scope):
                raise ValueError('Invalid chat save history')
        self.save_memories = {k: normalize_diplomatic_memory(v) for k,v in memories.items()}
        self.save_references = deepcopy(references)
        self.active_save_by_campaign = deepcopy(selected)
        direct_state = state.get("direct_point_policy_state", {})
        if not isinstance(direct_state, dict):
            raise ValueError("stored direct point policy state must be an object")
        self.direct_point_policy_state = {
            key: deepcopy(direct_state.get(key, {})) for key in (
                "engine_point_policies", "engine_point_policy_versions")}
        if any(not isinstance(value, dict) for value in self.direct_point_policy_state.values()):
            raise ValueError("stored direct point policy index must be an object")
        self.diplomatic_memory = normalize_diplomatic_memory(state.get("diplomatic_memory"))
        runtime_settings = state.get("runtime_settings", {})
        if not isinstance(runtime_settings, dict):
            raise ValueError("stored runtime settings must be an object")
        autonomous_enabled = runtime_settings.get("autonomous_initiatives_enabled", False)
        if not isinstance(autonomous_enabled, bool):
            raise ValueError("stored autonomous_initiatives_enabled setting must be Boolean")
        self.autonomous_initiatives_enabled = autonomous_enabled
        initiative_state = state.get("computer_annexation_initiatives", {})
        if not isinstance(initiative_state, dict):
            raise ValueError("stored computer annexation initiative state must be an object")
        self.computer_annexation_initiatives = {
            "by_evaluation_id": deepcopy(initiative_state.get("by_evaluation_id", {})),
            "request_index": deepcopy(initiative_state.get("request_index", {})),
            "scheduler_by_country": deepcopy(initiative_state.get("scheduler_by_country", {})),
        }
        if any(not isinstance(value, dict) for value in self.computer_annexation_initiatives.values()):
            raise ValueError("stored computer annexation initiative indexes must be objects")
        integration_state = state.get("campaign_integration_observations", {})
        if not isinstance(integration_state, dict):
            raise ValueError("stored campaign integration observations must be an object")
        self.campaign_integration_observations = {
            "by_campaign_player": deepcopy(integration_state.get("by_campaign_player", {})),
            "request_index": deepcopy(integration_state.get("request_index", {})),
            "snapshots_by_campaign_player": deepcopy(
                integration_state.get("snapshots_by_campaign_player", {})
            ),
            "snapshot_index": deepcopy(integration_state.get("snapshot_index", {})),
            "latest_snapshot_by_campaign_player": deepcopy(
                integration_state.get("latest_snapshot_by_campaign_player", {})
            ),
        }
        if any(not isinstance(value, dict)
               for value in self.campaign_integration_observations.values()):
            raise ValueError("stored campaign integration indexes must be objects")
        # Older SQLite snapshots indexed by campaign/player/sequence. Keep those
        # immutable entries readable and derive a current pointer from their
        # observation times. New snapshots use content-addressed identities.
        for key, snapshots in self.campaign_integration_observations[
                "snapshots_by_campaign_player"].items():
            if not isinstance(snapshots, list) or not snapshots:
                continue
            current = self.campaign_integration_observations[
                "latest_snapshot_by_campaign_player"].get(key)
            if current and any(item.get("snapshot_id") == current for item in snapshots):
                continue
            latest = max(enumerate(snapshots), key=lambda pair: (
                str(pair[1].get("observed_at", "")), pair[0],
            ))[1]
            self.campaign_integration_observations[
                "latest_snapshot_by_campaign_player"
            ][key] = latest.get("snapshot_id")
        for chat in self.chats.values():
            chat.setdefault("agreements", {})
            chat.setdefault("game_context_observations", {"by_country": {}, "request_index": {}})
            chat.setdefault("retry_contexts", {})
            chat.setdefault("retry_attempts", {})
            chat.setdefault("interface_language", "en")
            chat.setdefault("dialogue_language", chat.get("response_language") or "en")
            chat.setdefault("response_language", chat.get("dialogue_language") or "en")
        self.conversations = OrderedDict(
            (item["conversation_id"], self._conversation_from_snapshot(item))
            for item in state.get("conversations", [])
        )
        if "random_state" in state:
            self.random.setstate(self._tuple_tree(state["random_state"]))
        if "initiative_random_state" in state:
            self.initiative_random.setstate(self._tuple_tree(state["initiative_random_state"]))
        self._chat_engines = {}
        child_states = state.get("chat_engines", {})
        for chat_id in self.chats:
            child = DiplomacyEngine(
                max_history=max(self.max_history, 10000), max_conversations=1,
                response_text_provider=self.response_text_provider,
            )
            child.diplomatic_memory = self._memory_for_chat(self.chats[chat_id])
            saved = child_states.get(chat_id, {})
            child.conversations = OrderedDict(
                (item["conversation_id"], self._conversation_from_snapshot(item))
                for item in saved.get("conversations", [])
            )
            if "random_state" in saved:
                child.random.setstate(self._tuple_tree(saved["random_state"]))
            self._chat_engines[chat_id] = child
        self._chat_retry_locks = {chat_id: threading.Lock() for chat_id in self.chats}

    def create_chat(self, campaign_id: Optional[str], player_country: str,
                    ai_countries: List[str], country_contexts: Optional[Mapping[str, Any]] = None,
                    response_language: Optional[str] = None,
                    interface_language: Optional[str] = None) -> Dict[str, Any]:
        """Create an isolated group conversation. The player must speak first."""
        campaign_id = str(campaign_id or uuid.uuid4().hex).strip()
        if not _CAMPAIGN_ID_RE.fullmatch(campaign_id):
            raise ValueError("campaign_id must be a 32-character lowercase UUID hex string")
        player = _country_tag(player_country, "player_country")
        if not isinstance(ai_countries, list) or not ai_countries:
            raise ValueError("ai_countries must contain at least one country")
        ai_tags = [_country_tag(tag, "ai_countries") for tag in ai_countries]
        if len(set(ai_tags)) != len(ai_tags) or player in ai_tags:
            raise ValueError("chat participants must be unique and cannot repeat the player")
        if len(ai_tags) + 1 > 6:
            raise ValueError("a diplomatic chat may have at most six participants including the player")
        contexts = _normalize_country_contexts(country_contexts or {}, [player, *ai_tags])
        response_language = normalize_language(response_language) or "en"
        interface_language = normalize_language(interface_language) or "en"
        chat_id = uuid.uuid4().hex
        conversation_id = f"{campaign_id}:chat:{chat_id}"
        now = _utc_now()
        chat = {
            "chat_id": chat_id,
            "campaign_id": campaign_id,
            "conversation_id": conversation_id,
            "player_country": player,
            "participants": [player, *ai_tags],
            "country_contexts": contexts,
            "game_context_observations": {"by_country": {}, "request_index": {}},
            "response_language": response_language,
            "dialogue_language": response_language,
            "interface_language": interface_language,
            "messages": [],
            "state": "awaiting_player",
            "response_queue": [],
            "round_responded": [],
            "round": 0,
            "request_sequence": 1,
            "processed_request_ids": {},
            "retry_contexts": {},
            "retry_attempts": {},
            "territory_proposals": {},
            "agreements": {},
            "agreement_formalization_requests": {},
            "created_at": now,
            "updated_at": now,
        }
        scope = self.active_save_by_campaign.get(campaign_id)
        if scope in self.save_references:
            chat["save_scope_id"] = scope
            chat["save_reference"] = deepcopy(self.save_references[scope])
        self.chats[chat_id] = chat
        self._chat_engines[chat_id] = DiplomacyEngine(
            max_history=max(self.max_history, 10000), max_conversations=1,
            response_text_provider=self.response_text_provider,
        )
        self._chat_engines[chat_id].diplomatic_memory = self._memory_for_chat(chat)
        self._chat_retry_locks[chat_id] = threading.Lock()
        return self._public_chat(chat)

    def _copy_save_context_to(self, other):
        other.save_memories = deepcopy(self.save_memories)
        other.save_references = deepcopy(self.save_references)
        other.active_save_by_campaign = deepcopy(self.active_save_by_campaign)

    def _require_current_save_scope(self, chat):
        selected=self.active_save_by_campaign.get(chat['campaign_id'])
        if selected is not None and chat.get('save_scope_id') != selected:
            raise ChatConflict("Select this chat's save before modifying its history")

    def _memory_for_chat(self, chat):
        scope = chat.get('save_scope_id')
        if scope is None:
            return self.diplomatic_memory
        if scope not in self.save_references:
            raise ValueError('Unknown saved history scope')
        return self.save_memories.setdefault(scope, empty_diplomatic_memory())

    def select_chat_save(self, chat_id, reference, *, confirm=False):
        """Explicitly label an unbound chat, or fork for a different save snapshot.

        File selection is not game evidence. Campaign IDs, approvals, messages
        and effect receipts are never reassigned or copied into another chat.
        """
        if confirm is not True:
            raise ValueError('Confirm the history-to-save association')
        chat = self._chat(chat_id)
        scope = reference.get('save_id')
        if not isinstance(scope, str) or not re.fullmatch('[0-9a-f]{64}', scope):
            raise ValueError('Invalid saved history scope')
        self.save_references[scope] = deepcopy(reference)
        prior = chat.get('save_scope_id')
        if prior is not None and prior != scope:
            self.active_save_by_campaign[chat['campaign_id']] = scope
            existing = [c for c in self.chats.values() if c.get('save_scope_id') == scope
                and c['campaign_id'] == chat['campaign_id'] and c['player_country'] == chat['player_country']
                and set(c['participants']) == set(chat['participants']) and c.get('state') != 'closed']
            if existing:
                return {**self._public_chat(max(existing, key=lambda c:c['updated_at'])), 'history_forked': True}
            result = self.create_chat(chat['campaign_id'], chat['player_country'],
                [p for p in chat['participants'] if p != chat['player_country']],
                response_language=chat.get('response_language'), interface_language=chat.get('interface_language'))
            return {**result, 'history_forked': True}
        chat['save_scope_id'] = scope
        chat['save_reference'] = deepcopy(self.save_references[scope])
        self.active_save_by_campaign[chat['campaign_id']] = scope
        self._chat_engines[chat_id].diplomatic_memory = self._memory_for_chat(chat)
        return {**self._public_chat(chat), 'history_forked': False}

    def update_chat_languages(self, chat_id: str, *, interface_language: Optional[str] = None,
                              dialogue_language: Optional[str] = None) -> Dict[str, Any]:
        """Change presentation/dialogue locales independently without rewriting chat or treaty data."""
        chat = self._chat(chat_id)
        if interface_language is None and dialogue_language is None:
            raise ValueError("provide interface_language and/or dialogue_language")
        if interface_language is not None:
            normalized_interface = normalize_language(interface_language)
            if normalized_interface is None:
                raise ValueError("unsupported interface_language")
            chat["interface_language"] = normalized_interface
        if dialogue_language is not None:
            normalized_dialogue = normalize_language(dialogue_language)
            if normalized_dialogue is None:
                raise ValueError("unsupported dialogue_language")
            chat["dialogue_language"] = normalized_dialogue
            chat["response_language"] = normalized_dialogue
        chat["updated_at"] = _utc_now()
        return self._public_chat(chat)

    def list_chats(self, campaign_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return [
            self._public_chat(chat) for chat in self.chats.values()
            if campaign_id is None or chat["campaign_id"] == campaign_id
        ]

    def get_chat(self, chat_id: str) -> Dict[str, Any]:
        return self._public_chat(self._chat(chat_id))

    def get_chat_diplomatic_memory(self, chat_id: str) -> Dict[str, Any]:
        """Return the bounded pair ledgers for a small in-chat trust/history view."""
        chat = self._chat(chat_id)
        pairs = []
        participants = list(chat.get("participants", []))
        for observer in participants:
            for counterpart in participants:
                if observer == counterpart:
                    continue
                pairs.append(diplomatic_memory_pair_summary(
                    self._memory_for_chat(chat), campaign_id=chat["campaign_id"],
                    observer_tag=observer, counterpart_tag=counterpart,
                ))
        return {
            "chat_id": chat["chat_id"],
            "campaign_id": chat["campaign_id"],
            "memory_schema_version": self._memory_for_chat(chat).get("schema_version", 1),
            "strategic_countries": [deepcopy(row) for row in self._memory_for_chat(chat).get("strategic_countries", {}).values()
                                    if row.get("campaign_id") == chat["campaign_id"]
                                    and row.get("country_tag") in participants],
            "pairs": pairs,
        }

    def prepare_political_ultimatum_rejection(self, chat_id: str,
                                               refusal_request_id: str) -> Dict[str, Any]:
        """Return the automatically prepared event for a linked ultimatum refusal."""
        chat = self._chat(chat_id)
        if not isinstance(refusal_request_id, str) or not refusal_request_id:
            raise ValueError("refusal_request_id is required")
        refusal = next((message for message in chat.get("messages", [])
                        if message.get("request_id") == refusal_request_id), None)
        if refusal is None:
            raise ValueError("refusal request was not found in this chat")
        existing_package = refusal.get("diplomatic_event_package")
        if (isinstance(existing_package, dict)
                and existing_package.get("action_id") == "political_ultimatum_rejection"):
            return deepcopy(existing_package)
        positions = refusal.get("dialogue_positions")
        if not isinstance(positions, list):
            raise ChatConflict("the message has no structured diplomatic position")
        source_ids = {
            position.get("source_request_id") for position in positions
            if isinstance(position, dict)
            and position.get("action_id") == "political_annexation"
            and position.get("stance") == "reject"
            and isinstance(position.get("source_request_id"), str)
        }
        if len(source_ids) != 1:
            raise ChatConflict("a single explicit political-annexation rejection is required")
        source_id = next(iter(source_ids))
        source = next((message for message in chat.get("messages", [])
                       if message.get("request_id") == source_id), None)
        if source is None:
            raise ChatConflict("the original ultimatum message is no longer available")
        return build_political_ultimatum_rejection_event_package(chat, source, refusal)

    def prepare_diplomatic_refusal_event(self, chat_id: str,
                                         refusal_request_id: str) -> Dict[str, Any]:
        """Return an auto-prepared, inert generic bilateral refusal news event."""
        chat = self._chat(chat_id)
        refusal = next((message for message in chat.get("messages", [])
                        if message.get("request_id") == refusal_request_id), None)
        if refusal is None:
            raise ValueError("refusal request was not found in this chat")
        package = refusal.get("diplomatic_event_package")
        if (not isinstance(package, dict)
                or package.get("action_id") == "political_ultimatum_rejection"):
            raise ChatConflict("this message has no prepared ordinary diplomatic refusal event")
        return deepcopy(package)

    def update_chat_participants(self, chat_id: str, add: Optional[List[str]] = None,
                                 remove: Optional[List[str]] = None,
                                 country_contexts: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        chat = self._chat(chat_id)
        if chat["state"] == "closed":
            raise ChatConflict("chat is closed")
        if any(proposal.get("execution_package") or proposal.get("state") in {"GAME_APPLIED", "GAME_ROLLED_BACK"}
               for proposal in chat["territory_proposals"].values()):
            raise ChatConflict("participants are frozen after a territory execution package is prepared")
        add_tags = [_country_tag(tag, "add") for tag in (add or [])]
        remove_tags = [_country_tag(tag, "remove") for tag in (remove or [])]
        if len(set(add_tags)) != len(add_tags) or len(set(remove_tags)) != len(remove_tags):
            raise ValueError("participant changes must not contain duplicate countries")
        if chat["player_country"] in remove_tags:
            raise ValueError("the player cannot be removed from their chat")
        participants = list(chat["participants"])
        for tag in remove_tags:
            if tag not in participants:
                raise ValueError(f"cannot remove non-member {tag}")
            participants.remove(tag)
            chat["response_queue"] = [queued for queued in chat["response_queue"] if queued != tag]
        for tag in add_tags:
            if tag in participants:
                raise ValueError(f"country {tag} is already a participant")
            participants.append(tag)
        if len(participants) > 6:
            raise ValueError("a diplomatic chat may have at most six participants including the player")
        contexts = _normalize_country_contexts(country_contexts or {}, participants)
        for tag, context in contexts.items():
            chat["country_contexts"][tag] = context
        for tag in add_tags:
            if tag not in chat["country_contexts"]:
                chat["country_contexts"][tag] = {}
        for tag in remove_tags:
            chat["country_contexts"].pop(tag, None)
        chat["participants"] = participants
        if chat["state"] == "awaiting_ai":
            queued = set(chat["response_queue"])
            chat["response_queue"].extend(
                tag for tag in participants
                if tag != chat["player_country"] and tag not in queued
                and tag not in chat["round_responded"]
            )
        if not chat["response_queue"] and chat["state"] == "awaiting_ai":
            chat["state"] = "awaiting_player"
        for proposal in chat["territory_proposals"].values():
            proposal["state"] = "NEEDS_RENEGOTIATION"
        chat["updated_at"] = _utc_now()
        return self._public_chat(chat)

    def update_chat_country_context(self, chat_id: str, country: str,
                                    context: Mapping[str, Any]) -> Dict[str, Any]:
        chat = self._chat(chat_id)
        tag = _country_tag(country, "country")
        if tag not in chat["participants"] or tag == chat["player_country"]:
            raise ValueError("context may only be set for an AI participant")
        if not isinstance(context, Mapping):
            raise ValueError("context must be an object")
        if any(proposal.get("execution_package") or proposal.get("state") in {"GAME_APPLIED", "GAME_ROLLED_BACK"}
               for proposal in chat["territory_proposals"].values()):
            raise ChatConflict("context is frozen after a territory execution package is prepared")
        chat["country_contexts"][tag] = deepcopy(dict(context))
        for proposal in chat["territory_proposals"].values():
            version = proposal["versions"][-1]
            previous_vote = version["votes"].pop(tag, None)
            if previous_vote is not None:
                version.setdefault("superseded_votes", []).append({
                    "country": tag, "vote": previous_vote, "reason": "participant_context_updated",
                    "superseded_at": _utc_now(),
                })
                proposal["state"] = "NEEDS_CONTEXT"
        chat["updated_at"] = _utc_now()
        return self._public_chat(chat)

    def record_game_context_observation(self, chat_id: str, country: str, campaign_id: str,
                                       request_id: str, context: Mapping[str, Any],
                                       schema_version: int = 5) -> Dict[str, Any]:
        """Persist a versioned game-log observation with exact request provenance.

        V5 carries limited diplomatic facts; V6 adds documented ownership,
        division, factory, and stability values. Neither schema infers missing
        military strength, guarantees, ideology, or ally counts.
        """
        chat = self._chat(chat_id)
        self._require_current_save_scope(chat)
        tag = _country_tag(country, "country")
        if tag == chat["player_country"] or tag not in chat["participants"]:
            raise ValueError("game observations must identify a participating AI country")
        if campaign_id != chat["campaign_id"]:
            raise ChatConflict("game observation campaign_id does not match this chat")
        if not isinstance(context, Mapping):
            raise ValueError("game observation context must be an object")
        if any(proposal.get("execution_package") or proposal.get("state") in {"GAME_APPLIED", "GAME_ROLLED_BACK"}
               for proposal in chat["territory_proposals"].values()):
            raise ChatConflict("context is frozen after a territory execution package is prepared")

        if (not isinstance(schema_version, int) or isinstance(schema_version, bool)
                or schema_version not in {5, 6, 7, 8, 9}):
            raise ValueError("game observation schema_version must be 5, 6, 7, 8, or 9")
        request_pattern = (
            rf"{re.escape(campaign_id)}:pair-doctrine:[0-9]{{10}}(?::[0-9a-f]{{12}})?"
            if schema_version in {8, 9} else
            rf"{re.escape(campaign_id)}:pair-context:[0-9]{{10}}"
            if schema_version == 7 else rf"{re.escape(campaign_id)}:[0-9]{{4,}}"
        )
        if not isinstance(request_id, str) or not re.fullmatch(request_pattern, request_id):
            raise ValueError("game observation request_id must be namespaced by campaign_id")
        expected_v5 = {
            "date", "war_status", "same_faction", "existing_non_aggression_pact",
            "relations", "threat",
        }
        expected_v6 = expected_v5 | {
            "initiator_exists", "target_exists", "initiator_owned_states", "target_owned_states",
            "initiator_divisions", "target_divisions", "initiator_factories", "target_factories",
            "target_stability",
        }
        expected_v7 = expected_v5 | {"game_day", "game_day_basis", "relations_direction"}
        expected_v8 = expected_v7 | {"ideology_group", "player_ideology_group", "claims"}
        expected_v9 = expected_v8 | {"major_power", "player_major_power"}
        expected = {5: expected_v5, 6: expected_v6, 7: expected_v7,
                    8: expected_v8, 9: expected_v9}[schema_version]
        normalized = dict(context)
        if set(normalized) != expected:
            raise ValueError(f"V{schema_version} game observation must contain exactly the supported V{schema_version} fields")
        boolean_fields = ["war_status", "same_faction", "existing_non_aggression_pact"]
        if schema_version == 6:
            boolean_fields.extend(("initiator_exists", "target_exists"))
        for name in boolean_fields:
            value = normalized[name]
            if not isinstance(value, str) or value.lower() not in {"yes", "no", "unknown"}:
                raise ValueError(f"V{schema_version} {name} must be yes, no, or unknown")
            normalized[name] = value.lower()
        for name in ("date", "relations", "threat"):
            value = normalized[name]
            if not isinstance(value, str) or not value.strip() or len(value) > 100:
                raise ValueError(f"V{schema_version} {name} must be a non-empty string of at most 100 characters")

        if schema_version == 6:
            integer_fields = ("initiator_owned_states", "target_owned_states",
                              "initiator_divisions", "target_divisions")
            for name in integer_fields:
                value = normalized[name]
                if not isinstance(value, str) or len(value) > 100:
                    raise ValueError(f"V6 {name} must be an integer or unknown")
                if value.lower() == "unknown":
                    normalized[name] = "unknown"
                    continue
                try:
                    number = int(value)
                except ValueError as error:
                    raise ValueError(f"V6 {name} must be an integer or unknown") from error
                maximum = 10000 if "owned_states" in name else 100000
                if number < 0 or number > maximum:
                    raise ValueError(f"V6 {name} is outside its supported range")
                normalized[name] = str(number)

            for name in ("initiator_factories", "target_factories", "target_stability"):
                value = normalized[name]
                if not isinstance(value, str) or len(value) > 100:
                    raise ValueError(f"V6 {name} must be numeric or unknown")
                if value.lower() == "unknown":
                    normalized[name] = "unknown"
                    continue
                try:
                    number = float(value)
                except ValueError as error:
                    raise ValueError(f"V6 {name} must be numeric or unknown") from error
                if not math.isfinite(number):
                    raise ValueError(f"V6 {name} must be finite")
                lower, upper = (0.0, 1.0) if name == "target_stability" else (0.0, 100000.0)
                if number < lower or number > upper:
                    raise ValueError(f"V6 {name} must be between {lower:g} and {upper:g}")
                normalized[name] = format(number, ".12g")
            normalized[name] = value.strip()
        if schema_version in {7, 8, 9}:
            if normalized["game_day_basis"] != "global.num_days":
                raise ValueError(f"V{schema_version} game_day_basis must identify the source HOI4 variable")
            if normalized["relations_direction"] != "selected_country_toward_player_country":
                raise ValueError(f"V{schema_version} relations_direction must identify the measured country direction")
            raw_game_day = normalized["game_day"]
            if not isinstance(raw_game_day, str) or len(raw_game_day) > 100:
                raise ValueError(f"V{schema_version} game_day must be a non-negative integer or unknown")
            if raw_game_day.lower() == "unknown":
                normalized["game_day"] = "unknown"
            else:
                try:
                    game_day_value = int(raw_game_day)
                except (TypeError, ValueError, OverflowError) as error:
                    raise ValueError(f"V{schema_version} game_day must be a non-negative integer or unknown") from error
                if str(game_day_value) != raw_game_day or not 0 <= game_day_value <= MAX_HOI4_ABSOLUTE_DAY:
                    raise ValueError(f"V{schema_version} game_day is outside the supported range")
                normalized["game_day"] = str(game_day_value)
        if schema_version in {8, 9}:
            ideology_groups = {"fascism", "democratic", "neutrality", "communism", "unknown"}
            for name in ("ideology_group", "player_ideology_group"):
                value = normalized[name]
                if not isinstance(value, str) or value not in ideology_groups:
                    if (schema_version != 9 or not isinstance(value, str)
                            or not re.fullmatch(r"modded:[a-z][a-z0-9_]{1,59}", value)):
                        raise ValueError(f"V{schema_version} {name} is not a supported observed ideology group")
            if schema_version == 9:
                for name in ("major_power", "player_major_power"):
                    value = normalized[name]
                    if not isinstance(value, str) or value.lower() not in {"yes", "no", "unknown"}:
                        raise ValueError(f"V9 {name} must be yes, no, or unknown")
                    normalized[name] = value.lower()
            raw_claims = normalized["claims"]
            if not isinstance(raw_claims, list) or len(raw_claims) > 1000:
                raise ValueError("V8 claims must be a list with at most 1000 rows")
            claims = []
            seen_state_ids = set()
            for raw_claim in raw_claims:
                expected_claim_fields = {
                    "claimant_tag", "owner_tag", "state_id", "infrastructure_level",
                    "strategic_value_proxy", "value_basis",
                }
                if not isinstance(raw_claim, Mapping) or set(raw_claim) != expected_claim_fields:
                    raise ValueError(f"V{schema_version} claim row has an unsupported schema")
                claimant = _country_tag(raw_claim["claimant_tag"], "claims.claimant_tag")
                owner = _country_tag(raw_claim["owner_tag"], "claims.owner_tag")
                if claimant != tag or owner != chat["player_country"]:
                    raise ValueError(f"V{schema_version} claim row must identify selected-country claims on player-owned territory")
                state_id = raw_claim["state_id"]
                if (not isinstance(state_id, int) or isinstance(state_id, bool)
                        or not 1 <= state_id <= 1_000_000 or state_id in seen_state_ids):
                    raise ValueError(f"V{schema_version} claim state_id must be a unique valid HOI4 state ID")
                seen_state_ids.add(state_id)
                infrastructure = raw_claim["infrastructure_level"]
                proxy = raw_claim["strategic_value_proxy"]
                for name, value, upper in (("infrastructure_level", infrastructure, 10.0),
                                           ("strategic_value_proxy", proxy, 1.0)):
                    if value is not None:
                        if (isinstance(value, bool) or not isinstance(value, (int, float))
                                or not math.isfinite(value) or not 0.0 <= value <= upper):
                            raise ValueError(f"V{schema_version} claim {name} is outside its supported range")
                expected_proxy = None if infrastructure is None else round(float(infrastructure) / 10.0, 3)
                if proxy != expected_proxy:
                    raise ValueError(f"V{schema_version} claim value proxy must be derived from its reported infrastructure level")
                basis = raw_claim["value_basis"]
                if basis != "infrastructure_level_only_not_total_state_value":
                    raise ValueError(f"V{schema_version} claim value_basis must disclose the limited proxy")
                claims.append({
                    "claimant_tag": claimant, "owner_tag": owner, "state_id": state_id,
                    "infrastructure_level": (None if infrastructure is None else float(infrastructure)),
                    "strategic_value_proxy": expected_proxy, "value_basis": basis,
                })
            normalized["claims"] = claims
        for name in ("relations", "threat"):
            if normalized[name].lower() != "unknown":
                try:
                    number = float(normalized[name])
                except ValueError as error:
                    raise ValueError(f"V{schema_version} {name} must be numeric or unknown") from error
                if not math.isfinite(number):
                    raise ValueError(f"V{schema_version} {name} must be finite")
                lower, upper = (-100.0, 100.0) if name == "relations" else (0.0, 1.0)
                if number < lower or number > upper:
                    raise ValueError(f"V{schema_version} {name} must be between {lower:g} and {upper:g}")

        fingerprint = hashlib.sha256(json.dumps({
            "campaign_id": campaign_id, "country": tag, "schema_version": schema_version,
            "context": normalized,
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
        observations = chat.setdefault("game_context_observations", {"by_country": {}, "request_index": {}})
        by_country = observations.setdefault("by_country", {})
        request_index = observations.setdefault("request_index", {})
        previous = request_index.get(request_id)
        if previous is not None:
            if previous.get("fingerprint") != fingerprint or previous.get("country") != tag:
                raise ChatConflict("game observation request_id was already used with different data")
            result = deepcopy(previous["observation"])
            result["idempotent_replay"] = True
            return result

        observation = {
            "campaign_id": campaign_id, "country": tag, "request_id": request_id,
            "schema_version": schema_version,
            "source": f"bridge_parsed_hoi4_game_log_v{schema_version}", "observed_at": _utc_now(),
            "context": normalized, "idempotent_replay": False,
        }
        rows = by_country.setdefault(tag, [])
        rows.append(observation)
        if len(rows) > 100:
            rows.pop(0)
        request_index[request_id] = {
            "country": tag, "fingerprint": fingerprint,
            "observation": deepcopy(observation),
        }
        self.update_chat_country_context(chat_id, tag, normalized)
        remember_verified_game_observation(
            self._memory_for_chat(chat), campaign_id=campaign_id,
            observer_tag=tag, counterpart_tag=chat["player_country"],
            source=observation["source"], schema_version=schema_version,
            request_id=request_id, game_date=str(normalized.get("date") or normalized.get("game_day") or "unknown"),
            facts=normalized,
        )
        chat["updated_at"] = observation["observed_at"]
        if self._memory_for_chat(chat).get('historical_diplomacy', {}).get('conferences'):
            from historical_conferences import Conferences
            conferences = Conferences(self)
            active = conferences.active(chat_id)
            if active and normalized.get('game_day_basis') == 'global.num_days':
                if int(normalized['game_day']) < active['last_day']:
                    active['status'] = 'suspended'
                    active['outcome'] = 'earlier_save_requires_matching_conference_snapshot'
                else:
                    conferences.advance(chat_id, active['conference_id'])
        return deepcopy(observation)

    def record_campaign_integration_snapshot(
        self, campaign_id: str, player_country: str, snapshot_seq: Any,
        game_day: Any, observations: Any, historical_focuses_enabled: Any = None,
        historical_focuses_request_id: Any = None, authenticated_bridge: bool = False,
    ) -> Dict[str, Any]:
        """Atomically store one complete marker batch and make it the readable snapshot."""
        campaign = str(campaign_id or "")
        if not _CAMPAIGN_ID_RE.fullmatch(campaign):
            raise ValueError("campaign_id must be a lowercase UUID hex string")
        player = _country_tag(player_country, "player_country")
        if not any(chat.get("campaign_id") == campaign and chat.get("player_country") == player
                   for chat in self.chats.values()):
            raise ChatConflict("integration snapshots require an existing chat in this campaign")
        sequence = self._integration_metric(snapshot_seq, "snapshot_seq", 1, 10_000_000_000, integer=True)
        day = self._integration_metric(
            game_day, "game_day", 0, MAX_HOI4_ABSOLUTE_DAY, integer=True,
        )
        if historical_focuses_enabled is None and historical_focuses_request_id is None:
            game_rules = None
        else:
            expected_rule_request = f"{campaign}:integration:{sequence}:rules"
            if (not isinstance(historical_focuses_enabled, bool)
                    or historical_focuses_request_id != expected_rule_request):
                raise ValueError("historical-focus evidence must be a Boolean with the exact snapshot request_id")
            game_rules = {
                "historical_focuses_enabled": historical_focuses_enabled,
                "request_id": expected_rule_request,
                "source": (
                    "authenticated_bridge_hoi4_historical_focuses_v1"
                    if authenticated_bridge else "unverified_local_api_historical_focus_marker_v1"
                ),
            }
        if not isinstance(observations, list) or len(observations) > 2000:
            raise ValueError("integration snapshot observations must be a list of at most 2000 states")
        normalized_input = []
        seen_states = set()
        for item in observations:
            if not isinstance(item, Mapping) or set(item) != {"request_id", "context"}:
                raise ValueError("each snapshot item must contain request_id and context")
            request_id, context = item["request_id"], item["context"]
            if not isinstance(context, Mapping):
                raise ValueError("snapshot state context must be an object")
            state_id = self._integration_metric(context.get("state_id"), "state_id", 1, 1_000_000,
                                                integer=True)
            if state_id in seen_states:
                raise ValueError("integration snapshot contains a duplicate state ID")
            seen_states.add(state_id)
            if (not isinstance(request_id, str)
                    or request_id != f"{campaign}:integration:{sequence}:{state_id}"):
                raise ValueError("snapshot request_id does not match campaign, sequence, and state ID")
            if context.get("game_day") != day:
                raise ValueError("all snapshot observations must use the snapshot game day")
            normalized_input.append({"request_id": request_id, "context": deepcopy(dict(context))})
        normalized_input.sort(key=lambda item: item["context"]["state_id"])
        fingerprint = hashlib.sha256(json.dumps({
            "campaign_id": campaign, "player_country": player, "snapshot_seq": sequence,
            "game_day": day, "observations": normalized_input, "game_rules": game_rules,
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
        legacy_snapshot_key = f"{campaign}:{player}:{sequence}"
        snapshot_key = f"{legacy_snapshot_key}:{fingerprint}"
        snapshot_index = self.campaign_integration_observations["snapshot_index"]
        prior = snapshot_index.get(snapshot_key)
        # A pre-upgrade snapshot may still be stored under the old sequence-only
        # key. It is an exact replay only when the full canonical payload agrees.
        if prior is None:
            legacy_prior = snapshot_index.get(legacy_snapshot_key)
            if legacy_prior is not None and legacy_prior.get("fingerprint") == fingerprint:
                prior = legacy_prior
        if prior is not None:
            replay = deepcopy(prior["snapshot"])
            replay["idempotent_replay"] = True
            self.campaign_integration_observations[
                "latest_snapshot_by_campaign_player"
            ][f"{campaign}:{player}"] = replay.get("snapshot_id")
            snapshot_index[snapshot_key] = deepcopy(prior)
            return replay

        # Roll back the in-memory engine snapshot if any row validation fails midway.
        before = deepcopy(self.campaign_integration_observations)
        try:
            observation_request_ids = []
            source_marker_request_ids = []
            for item in normalized_input:
                source_request_id = item["request_id"]
                state_id = item["context"]["state_id"]
                # The marker's V1 request_id contains a save-local sequence and
                # therefore can be reused after loading an older save. A digest
                # adds immutable branch identity without changing the in-game
                # marker or losing its source reference.
                storage_request_id = (
                    f"{campaign}:integration:{sequence}:{state_id}:{fingerprint[:16]}"
                )
                self.record_campaign_integration_observation(
                    campaign, player, storage_request_id, item["context"],
                    source_marker_request_id=source_request_id,
                )
                observation_request_ids.append(storage_request_id)
                source_marker_request_ids.append(source_request_id)
            snapshot = {
                "campaign_id": campaign, "player_country": player,
                "snapshot_id": f"{campaign}:integration-snapshot:{sequence}:{fingerprint[:16]}",
                "snapshot_seq": sequence, "game_day": day,
                "expected_count": len(normalized_input),
                "state_ids": [item["context"]["state_id"] for item in normalized_input],
                "observation_request_ids": observation_request_ids,
                "source_marker_request_ids": source_marker_request_ids,
                "game_rules": deepcopy(game_rules),
                "complete": True, "idempotent_replay": False, "source": "bridge_complete_state_snapshot_v1",
                "observed_at": _utc_now(),
            }
            campaign_player_key = f"{campaign}:{player}"
            snapshots = self.campaign_integration_observations["snapshots_by_campaign_player"].setdefault(
                campaign_player_key, [],
            )
            snapshots.append(deepcopy(snapshot))
            if len(snapshots) > 1000:
                del snapshots[:-1000]
            snapshot_index[snapshot_key] = {
                "fingerprint": fingerprint, "snapshot": deepcopy(snapshot),
            }
            self.campaign_integration_observations[
                "latest_snapshot_by_campaign_player"
            ][campaign_player_key] = snapshot["snapshot_id"]
            return deepcopy(snapshot)
        except Exception:
            self.campaign_integration_observations = before
            raise

    @staticmethod
    def _integration_metric(value: Any, name: str, minimum: float, maximum: float,
                            *, integer: bool = False):
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError(f"integration {name} must be numeric or unknown")
        if isinstance(value, str) and value.strip().lower() in {"", "unknown", "unavailable"}:
            return None
        try:
            number = int(value) if integer else float(value)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError(f"integration {name} must be numeric or unknown") from error
        if not math.isfinite(float(number)) or not minimum <= number <= maximum:
            raise ValueError(f"integration {name} is outside its supported range")
        if integer and (float(value) != number):
            raise ValueError(f"integration {name} must be an integer")
        return number

    def record_campaign_integration_observation(
        self, campaign_id: str, player_country: str, request_id: str,
        context: Mapping[str, Any], source_marker_request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Store one Bridge-parsed, state-specific HOI4 observation; never execute effects."""
        campaign = str(campaign_id or "")
        if not _CAMPAIGN_ID_RE.fullmatch(campaign):
            raise ValueError("campaign_id must be a lowercase UUID hex string")
        player = _country_tag(player_country, "player_country")
        if not any(chat.get("campaign_id") == campaign and chat.get("player_country") == player
                   for chat in self.chats.values()):
            raise ChatConflict("integration observations require an existing chat in this campaign")
        if not isinstance(request_id, str):
            raise ValueError("integration request_id must include campaign, snapshot sequence, and state ID")
        request_match = re.fullmatch(
            rf"{re.escape(campaign)}:integration:([0-9]{{1,10}}):([0-9]{{1,7}})(?::([0-9a-f]{{16}}))?",
            request_id,
        )
        if request_match is None:
            raise ValueError("integration request_id must include campaign, snapshot sequence, and state ID")
        if not isinstance(context, Mapping):
            raise ValueError("integration context must be an object")
        expected = {
            "state_id", "owner_country", "controller_country", "game_day",
            "owner_since_day", "resistance", "compliance", "infrastructure_level",
            "stability", "is_core",
        }
        if set(context) != expected:
            raise ValueError("integration context must contain exactly the supported V1 state fields")
        state_id = self._integration_metric(context["state_id"], "state_id", 1, 1_000_000, integer=True)
        if int(request_match.group(2)) != state_id:
            raise ValueError("integration request_id state ID does not match its context")
        source_marker_request_id = source_marker_request_id or request_id
        if (not isinstance(source_marker_request_id, str)
                or not re.fullmatch(
                    rf"{re.escape(campaign)}:integration:{request_match.group(1)}:{state_id}",
                    source_marker_request_id,
                )):
            raise ValueError("source marker request_id must match the campaign, sequence, and state")
        game_day = self._integration_metric(
            context["game_day"], "game_day", 0, MAX_HOI4_ABSOLUTE_DAY, integer=True,
        )
        owner_since_day = self._integration_metric(
            context["owner_since_day"], "owner_since_day", 0,
            MAX_HOI4_ABSOLUTE_DAY, integer=True,
        )
        if owner_since_day is not None and owner_since_day > game_day:
            raise ValueError("integration owner_since_day cannot be later than game_day")
        owner = _country_tag(context["owner_country"], "owner_country")
        controller = _country_tag(context["controller_country"], "controller_country")
        if owner != player:
            raise ChatConflict("state observation owner does not match the player country")
        if context["is_core"] is not False:
            raise ValueError("core states are outside gradual non-core integration tracking")
        normalized = {
            "state_id": state_id, "owner_country": owner, "controller_country": controller,
            "game_day": game_day, "owner_since_day": owner_since_day,
            "resistance": self._integration_metric(context["resistance"], "resistance", 0, 100),
            "compliance": self._integration_metric(context["compliance"], "compliance", 0, 100),
            # HOI4 documents infrastructure_level as a state building level (0..10).
            # Keep the sourced raw value; normalize only when projecting a stage.
            "infrastructure_level": self._integration_metric(
                context["infrastructure_level"], "infrastructure_level", 0, 10,
            ),
            "stability": self._integration_metric(context["stability"], "stability", 0, 1),
            "is_core": False,
        }
        fingerprint = hashlib.sha256(json.dumps({
            "campaign_id": campaign, "player_country": player, "context": normalized,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        state = self.campaign_integration_observations
        previous = state["request_index"].get(request_id)
        if previous is not None:
            if (previous.get("fingerprint") != fingerprint
                    or previous.get("campaign_id") != campaign
                    or previous.get("player_country") != player):
                raise ChatConflict("integration request_id was already used with different state data")
            replay = deepcopy(previous["observation"])
            replay["idempotent_replay"] = True
            return replay

        observation = {
            "campaign_id": campaign, "player_country": player, "request_id": request_id,
            "source_marker_request_id": source_marker_request_id,
            "schema_version": 1, "source": "bridge_parsed_hoi4_state_integration_v1",
            "observed_at": _utc_now(), "context": normalized, "idempotent_replay": False,
        }
        key = f"{campaign}:{player}"
        campaign_state = state["by_campaign_player"].setdefault(key, {
            "campaign_id": campaign, "player_country": player, "by_state_id": {},
        })
        rows = campaign_state["by_state_id"].setdefault(str(state_id), [])
        rows.append(deepcopy(observation))
        if len(rows) > 1000:
            rows.pop(0)
        state["request_index"][request_id] = {
            "campaign_id": campaign, "player_country": player,
            "fingerprint": fingerprint, "observation": deepcopy(observation),
        }
        return deepcopy(observation)

    def list_campaign_state_integration(self, campaign_id: str, player_country: str) -> Dict[str, Any]:
        """Project integration stages only from persisted, per-state Bridge observations."""
        campaign = str(campaign_id or "")
        if not _CAMPAIGN_ID_RE.fullmatch(campaign):
            raise ValueError("campaign_id must be a lowercase UUID hex string")
        player = _country_tag(player_country, "player_country")
        if not any(chat.get("campaign_id") == campaign and chat.get("player_country") == player
                   for chat in self.chats.values()):
            raise ChatConflict("integration lookup requires an existing chat in this campaign")
        key = f"{campaign}:{player}"
        campaign_state = self.campaign_integration_observations["by_campaign_player"].get(key, {})
        snapshots = self.campaign_integration_observations["snapshots_by_campaign_player"].get(key, [])
        latest_snapshot = self._latest_campaign_integration_snapshot(campaign, player, snapshots)
        if latest_snapshot is None:
            return {
                "campaign_id": campaign, "player_country": player, "states": [], "count": 0,
                "latest_snapshot": None, "source": "no_complete_bridge_state_snapshot",
                "game_effect_confirmed": False,
            }
        results = []
        latest_request_ids = set(latest_snapshot["observation_request_ids"])
        for raw_state_id in sorted(latest_snapshot["state_ids"]):
            history = campaign_state.get("by_state_id", {}).get(str(raw_state_id), [])
            if not history:
                continue
            observation = next((deepcopy(item) for item in reversed(history)
                                if item.get("request_id") in latest_request_ids), None)
            if observation is None:
                continue
            facts = observation["context"]
            days_since_transfer = (
                facts["game_day"] - facts["owner_since_day"]
                if facts.get("owner_since_day") is not None else None
            )
            source = f"{observation['source']}:{observation['request_id']}"
            sources = {}
            available = {
                "transfer_confirmed": facts.get("owner_since_day") is not None,
                "days_since_transfer": days_since_transfer is not None,
                "resistance": facts.get("resistance") is not None,
                "cooperation": facts.get("compliance") is not None,
                "infrastructure": facts.get("infrastructure_level") is not None,
                "stability": facts.get("stability") is not None,
            }
            for name, present in available.items():
                if present:
                    sources[name] = source
            if "cooperation" in sources:
                sources["cooperation"] += ":HOI4_compliance_mapping"
            if "infrastructure" in sources:
                sources["infrastructure"] += ":infrastructure_level_0_to_10_scaled_to_0_to_100"
            snapshot = IntegrationSnapshot(
                transfer_confirmed=(facts.get("owner_since_day") is not None),
                days_since_transfer=days_since_transfer,
                resistance=facts.get("resistance"),
                cooperation=facts.get("compliance"),
                administrative_capacity=None,
                investment=None,
                infrastructure=(facts["infrastructure_level"] * 10.0
                                if facts.get("infrastructure_level") is not None else None),
                stability=(facts.get("stability") * 100.0
                           if facts.get("stability") is not None else None),
                evidence_sources=sources,
            )
            projection = project_integration(snapshot)
            if facts["controller_country"] != player:
                projection = {
                    **projection, "status": "blocked",
                    "reason": "state_is_not_fully_controlled_by_current_owner",
                    "game_effect_confirmed": False,
                }
            results.append({
                "state_id": int(raw_state_id), "campaign_id": campaign,
                "player_country": player, "latest_observation": observation,
                "days_since_confirmed_ownership_change": days_since_transfer,
                "projection": projection,
                "execution_status": "observation_only",
                "core_creation_authorized": False,
            })
        return {
            "campaign_id": campaign, "player_country": player,
            "states": results, "count": len(results),
            "latest_snapshot": deepcopy(latest_snapshot),
            "source": "persisted_bridge_state_observations",
            "game_effect_confirmed": False,
        }

    def _latest_campaign_integration_snapshot(
        self, campaign_id: str, player_country: str, snapshots: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        key = f"{campaign_id}:{player_country}"
        snapshots = snapshots if snapshots is not None else self.campaign_integration_observations[
            "snapshots_by_campaign_player"
        ].get(key, [])
        if not snapshots:
            return None
        snapshot_id = self.campaign_integration_observations[
            "latest_snapshot_by_campaign_player"
        ].get(key)
        if snapshot_id:
            match = next((item for item in reversed(snapshots)
                          if item.get("snapshot_id") == snapshot_id), None)
            if match is not None:
                return match
        return max(enumerate(snapshots), key=lambda pair: (
            str(pair[1].get("observed_at", "")), pair[0],
        ))[1]

    def chat_annexation_readiness(self, chat_id: str, target_tag: str) -> Dict[str, Any]:
        """Report versioned game observations without inventing missing annexation facts."""
        chat = self._chat(chat_id)
        target = _country_tag(target_tag, "target_tag")
        if target == chat["player_country"] or target not in chat["participants"]:
            raise ValueError("target_tag must be a participating AI country")
        rows = chat.get("game_context_observations", {}).get("by_country", {}).get(target, [])
        required = {
            "initiator_exists", "target_exists", "initiator_owned_states", "target_owned_states",
            "at_war", "conflicting_treaty", "initiator_military", "target_military",
            "initiator_factories", "target_factories", "relations", "ideology_compatible",
            "target_stability", "target_has_guarantee", "target_ally_count", "world_tension",
        }
        if not rows:
            return {
                "chat_id": chat_id, "campaign_id": chat["campaign_id"], "target_tag": target,
                "status": "blocked_no_v5_game_observation", "can_calculate_probability": False,
                "known_game_facts": {}, "missing_facts": sorted(required),
                "operation_started": False, "action_prepared": False,
                "game_effect_confirmed": False,
            }

        observation = deepcopy(rows[-1])
        context = observation["context"]
        schema_version = observation.get("schema_version", 5)
        known = {}
        if str(context.get("date", "unknown")).lower() != "unknown":
            known["game_date"] = {
                "value": context["date"], "source_request_id": observation["request_id"],
            }
        if schema_version in {7, 8, 9} and context.get("game_day") != "unknown":
            known["game_day"] = {
                "value": int(context["game_day"]),
                "source_request_id": observation["request_id"],
                "source_variable": context["game_day_basis"],
            }
        for name in ("same_faction", "existing_non_aggression_pact"):
            if context[name] in {"yes", "no"}:
                known[name] = {
                    "value": context[name] == "yes",
                    "source_request_id": observation["request_id"],
                }
        if context["war_status"] in {"yes", "no"}:
            known["at_war"] = {"value": context["war_status"] == "yes",
                                "source_request_id": observation["request_id"]}
        if context["relations"].lower() != "unknown":
            known["relations"] = {
                "value": float(context["relations"]),
                "source_request_id": observation["request_id"],
                **({"direction": context["relations_direction"]} if schema_version in {7, 8, 9} else {}),
            }
        if schema_version == 9:
            for field_name in ("major_power", "player_major_power"):
                if context[field_name] in {"yes", "no"}:
                    known[field_name] = {
                        "value": context[field_name] == "yes",
                        "source_request_id": observation["request_id"],
                        "country": tag if field_name == "major_power" else chat["player_country"],
                    }
        if context["threat"].lower() != "unknown":
            known["world_tension"] = {
                "value": round(float(context["threat"]) * 100.0, 2),
                "source_request_id": observation["request_id"],
                "raw_scale": "HOI4 global.threat in [0,1]",
            }
        if schema_version == 6:
            for field_name in ("initiator_exists", "target_exists"):
                value = context[field_name]
                if value in {"yes", "no"}:
                    known[field_name] = {
                        "value": value == "yes",
                        "source_request_id": observation["request_id"],
                    }
            for field_name in ("initiator_owned_states", "target_owned_states",
                               "initiator_divisions", "target_divisions"):
                value = context[field_name]
                if value != "unknown":
                    known[field_name] = {
                        "value": int(value),
                        "source_request_id": observation["request_id"],
                        "unit": "states" if field_name.endswith("owned_states") else "divisions",
                    }
            for field_name in ("initiator_factories", "target_factories"):
                value = context[field_name]
                if value != "unknown":
                    known[field_name] = {
                        "value": float(value),
                        "source_request_id": observation["request_id"],
                        "unit": "factories; HOI4 num_of_factories",
                    }
            value = context["target_stability"]
            if value != "unknown":
                known["target_stability"] = {
                    "value": round(float(value) * 100.0, 2),
                    "source_request_id": observation["request_id"],
                    "raw_value": float(value),
                    "raw_scale": "HOI4 stability in [0,1], converted to percent",
                }
        missing = sorted(required - set(known))
        bounded_forecast = None
        if schema_version == 6:
            source_prefix = f"bridge_parsed_hoi4_game_log_v6:{observation['request_id']}"
            raw_context = context

            def marker_bool(name: str) -> bool | None:
                value = str(raw_context.get(name, "unknown")).lower()
                return True if value == "yes" else False if value == "no" else None

            def marker_number(name: str, *, integer: bool = False, scale: float = 1.0):
                value = str(raw_context.get(name, "unknown")).lower()
                if value == "unknown":
                    return None
                try:
                    return int(value) if integer else float(value) * scale
                except (TypeError, ValueError, OverflowError):
                    return None

            same_faction = marker_bool("same_faction")
            existing_nap = marker_bool("existing_non_aggression_pact")
            conflict_value = True if same_faction is True or existing_nap is True else (
                False if same_faction is False and existing_nap is False else None
            )
            annexation_facts = {
                "initiator_exists": marker_bool("initiator_exists"),
                "target_exists": marker_bool("target_exists"),
                "initiator_owned_states": marker_number("initiator_owned_states", integer=True),
                "target_owned_states": marker_number("target_owned_states", integer=True),
                "at_war": marker_bool("war_status"),
                "conflicting_treaty": conflict_value,
                # HOI4's num_divisions is a count, not a strength/readiness score.
                # The estimate labels it as a coarse proxy and never uses it for an action gate.
                "initiator_military": marker_number("initiator_divisions"),
                "target_military": marker_number("target_divisions"),
                "initiator_factories": marker_number("initiator_factories"),
                "target_factories": marker_number("target_factories"),
                "relations": marker_number("relations"),
                "ideology_compatible": None,
                "target_stability": marker_number("target_stability", scale=100.0),
                "target_has_guarantee": None,
                "target_ally_count": None,
                "world_tension": marker_number("threat", scale=100.0),
            }
            source_fields = (
                "initiator_exists", "target_exists", "initiator_owned_states",
                "target_owned_states", "at_war", "conflicting_treaty",
                "initiator_military", "target_military", "initiator_factories",
                "target_factories", "relations", "target_stability", "world_tension",
            )
            sources = {name: source_prefix for name in source_fields
                       if annexation_facts.get(name) is not None}
            if annexation_facts["conflicting_treaty"] is not None:
                sources["conflicting_treaty"] = (
                    source_prefix + ":same-faction-and-NAP-only; other treaty types are not observed"
                )
            for name in ("initiator_military", "target_military"):
                if annexation_facts[name] is not None:
                    sources[name] = source_prefix + ":num_divisions count proxy"
            annexation_context = AnnexationContext(
                operation_id="readiness-" + hashlib.sha256(
                    f"{chat['campaign_id']}:{target}:{observation['request_id']}".encode("utf-8")
                ).hexdigest()[:24],
                initiator_tag=chat["player_country"], target_tag=target,
                approach="voluntary_offer", evidence_sources=sources, **annexation_facts,
            )
            bounded_forecast = estimate_annexation_probability_bounds(annexation_context)
        return {
            "chat_id": chat_id, "campaign_id": chat["campaign_id"], "target_tag": target,
            "status": f"blocked_incomplete_v{schema_version}_game_context",
            "can_calculate_probability": False,
            "can_show_probability_range": bool(
                bounded_forecast and bounded_forecast.get("status") == "bounded_preview"
            ),
            "bounded_forecast": bounded_forecast,
            "latest_observation": observation, "known_game_facts": known,
            "missing_facts": missing, "operation_started": False,
            "action_prepared": False, "game_effect_confirmed": False,
            "model_notice": f"V{schema_version} supplies sourced game observations; probability remains blocked until every required fact is available.",
        }

    def send_chat_message(self, chat_id: str, message: str, request_id: str) -> Dict[str, Any]:
        chat = self._chat(chat_id)
        self._require_current_save_scope(chat)
        cached = self._cached_chat_request(chat, request_id)
        if cached is not None:
            return cached
        if chat["state"] == "closed":
            raise ChatConflict("chat is closed")
        if chat["state"] != "awaiting_player":
            raise ChatConflict("wait for each participant's turn before sending another player message")
        text = _validated_message(message)
        prior_language = chat.get("response_language")
        if prior_language not in SUPPORTED_LANGUAGES:
            for old_message in reversed(chat.get("messages", [])):
                if old_message.get("speaker") == chat["player_country"]:
                    prior_language = detect_language(old_message.get("text", ""), prior_language)
                    if prior_language in SUPPORTED_LANGUAGES:
                        break
        from contextual_consent import short_reply
        chat["response_language"] = ((prior_language if short_reply(text) else None)
                                     or detect_language(text, prior_language) or prior_language or "en")
        chat["dialogue_language"] = chat["response_language"]
        chat["round"] += 1
        entry = {
            "speaker": chat["player_country"], "text": text,
            "timestamp": _utc_now(), "round": chat["round"], "kind": "player_message",
            "request_id": request_id,
        }
        entry["communication_acts"] = analyze_communication_acts(text)
        player_intent = self.classifier.classify(text)
        player_action = self.action_registry.for_intent(player_intent)
        if player_intent in INTENTS:
            entry["intent"] = player_intent
        if player_action is not None:
            entry["action"] = {
                **player_action,
                "status": "PROPOSED",
                "reason": "player_message_is_proposal_only",
            }
        from native_leadership import attribution
        entry['representative']=attribution(self,chat)
        chat["messages"].append(entry)
        if self._memory_for_chat(chat).get('historical_diplomacy', {}).get('conferences'):
            from historical_conferences import Conferences
            Conferences(self).review_player_text(chat_id, entry)
        chat["round_responded"] = []
        chat["response_queue"] = [
            tag for tag in chat["participants"] if tag != chat["player_country"]
        ]
        chat["state"] = "awaiting_ai" if chat["response_queue"] else "awaiting_player"
        chat["updated_at"] = _utc_now()
        chat["request_sequence"] += 1
        result = {**deepcopy(entry), "next_request_id": self._public_chat(chat)["next_request_id"]}
        chat["processed_request_ids"][request_id] = deepcopy(result)
        return result

    @staticmethod
    def _active_agreement_action_participant_context(
            chat: Mapping[str, Any], agreements: Optional[Mapping[str, Any]] = None
            ) -> Dict[str, List[Dict[str, Any]]]:
        """Project draft source/version metadata into a country's reply context.

        A retry may pass the immutable agreement snapshot captured with the
        failed response. This keeps its contract version fixed while restoring
        metadata omitted by older persisted retry payloads.
        """
        active: Dict[str, List[Dict[str, Any]]] = {}
        agreement_map = agreements if isinstance(agreements, Mapping) else chat.get("agreements", {})
        if not isinstance(agreement_map, Mapping):
            return active
        participants = chat.get("participants", [])
        participant_set = {str(tag).upper() for tag in participants if isinstance(tag, str)}
        for agreement in agreement_map.values():
            if not isinstance(agreement, Mapping) or agreement.get("status") not in {
                    "draft", "awaiting_approval"}:
                continue
            versions = agreement.get("versions")
            if not isinstance(versions, list) or not versions:
                continue
            latest_version = versions[-1]
            terms = latest_version.get("terms", [])
            if not isinstance(terms, list):
                continue
            for term in terms:
                if not isinstance(term, Mapping):
                    continue
                # Separate diplomatic clauses are addressed by their stable
                # term_id but still belong to the parent registered action
                # (e.g. advance_notice is a clause on non_aggression_pact).
                # Project them under that action so the reply model can see
                # the exact current-version clause independently.
                action_id = term.get("action_id") or term.get("term_id")
                required = term.get("required_participants")
                if (not isinstance(action_id, str) or not isinstance(required, list)
                        or not ActionRegistry().get(action_id)):
                    continue
                party_tags = list(dict.fromkeys(
                    str(tag).upper() for tag in required
                    if isinstance(tag, str) and str(tag).upper() in participant_set
                ))
                if not party_tags:
                    continue
                proposal_spec = term.get("proposal_spec")
                proposal_action = (
                    proposal_spec.get("action")
                    if isinstance(proposal_spec, Mapping) else None
                )
                if not isinstance(proposal_action, Mapping):
                    proposal_action = {}
                duration = term.get("duration") or proposal_action.get("duration")
                term_clause = (
                    proposal_spec.get("term_clause")
                    if isinstance(proposal_spec, Mapping) else None
                )
                row = {
                    "agreement_id": agreement.get("agreement_id"),
                    "agreement_version": latest_version.get("version"),
                    "term_id": term.get("term_id"),
                    "term_text": term.get("text"),
                    "category": term.get("category"),
                    "proposal_source_request_id": term.get("proposal_source_request_id"),
                    "duration": duration,
                    "term_clause": term_clause,
                    "proposal_spec": deepcopy(dict(proposal_spec))
                    if isinstance(proposal_spec, Mapping) else None,
                    "required_participants": party_tags,
                    "source_request_ids": list(latest_version.get("source_request_ids", [])),
                }
                rows = active.setdefault(action_id, [])
                if row not in rows:
                    rows.append(row)
        return active

    def advance_chat(self, chat_id: str, request_id: str) -> Dict[str, Any]:
        chat = self._chat(chat_id)
        self._require_current_save_scope(chat)
        cached = self._cached_chat_request(chat, request_id)
        if cached is not None:
            return cached
        if chat["state"] == "closed":
            raise ChatConflict("chat is closed")
        conversation_messages = [m for m in chat['messages'] if m.get('kind') != 'initiative_notice']
        if not conversation_messages or conversation_messages[0]['speaker'] != chat['player_country']:
            raise ChatConflict("the player must send the first message")
        if not chat["response_queue"]:
            raise ChatConflict("no country is waiting to respond")
        country = chat["response_queue"].pop(0)
        chat["round_responded"].append(country)
        previous = chat["messages"][-1]
        player_source = next((item for item in reversed(chat["messages"])
                              if item.get("kind") == "player_message"
                              and str(item.get("speaker", "")).upper() == chat["player_country"]), None)
        verified_game_context: Dict[str, Any] = {}
        game_context_provenance = None
        # Prefer the latest campaign-matched V5 observation when it exists.
        # This is the only source here that carries Bridge provenance; ordinary
        # chat context may be user supplied and must not be promoted to game fact.
        context_observations = chat.get("game_context_observations", {}).get("by_country", {})
        country_observations = context_observations.get(country, []) if isinstance(
            context_observations, Mapping,
        ) else []
        if not isinstance(country_observations, list):
            country_observations = []
        latest_observation = next((item for item in reversed(country_observations)
                                   if isinstance(item, Mapping)
                                   and item.get("campaign_id") == chat["campaign_id"]
                                   and item.get("country") == country
                                   and str(item.get("source", "")).startswith(
                                       "bridge_parsed_hoi4_game_log_v")
                                   and isinstance(item.get("context"), Mapping)), None)
        if latest_observation is not None:
            verified_game_context = deepcopy(dict(latest_observation["context"]))
            game_context_provenance = {
                "source": latest_observation["source"],
                "request_id": latest_observation["request_id"],
                "schema_version": latest_observation["schema_version"],
                "observed_at": latest_observation["observed_at"],
            }
        participant_country_names = {
            tag: self._country_name(tag) for tag in chat.get("participants", [])
        }
        active_agreement_action_participants = self._active_agreement_action_participant_context(chat)
        if active_agreement_action_participants:
            _PROVIDER_LOGGER.info(
                "group_agreement_scope_context status=loaded action_ids=%s chat_id=%s request_id=%s",
                ",".join(sorted(active_agreement_action_participants)), chat_id, request_id,
            )
        chat_engine = self._chat_engines[chat_id]
        chat_engine._point_plan_sink = lambda key, points: self._store_point_policy(chat, key, points)
        chat_engine._point_plan_history = chat.setdefault("engine_point_policies", {})
        from phrase_usage_memory import history as phrase_history
        from native_leadership import attribution as phrase_attribution
        phrase_person=phrase_attribution(self,chat,country).get('person_id')
        response_payload = {
            "sender": previous["speaker"],
            "receiver": country,
            "message": previous["text"],
            "conversation_id": chat["conversation_id"],
            "campaign_id": chat["campaign_id"],
            "chat_id": chat["chat_id"],
            "request_id": request_id,
            "game_context": verified_game_context,
            # Only Bridge-validated observations are presented as game facts;
            # editable country_contexts are not proof of live HOI4 state.
            "verified_game_context": verified_game_context,
            "game_context_provenance": game_context_provenance,
            "_verified_observation_capability": (
                chat_engine._verified_observation_capability
                if latest_observation is not None else None
            ),
            "group_chat": True,
            "recent_messages": deepcopy(chat["messages"][-8:]),
            "phrase_recent": phrase_history(chat,country,phrase_person),
            "phrase_person_id":phrase_person,
            # Group participants may answer the immediately preceding country
            # turn, while any proposal analysis and item positions remain
            # anchored to the player's latest source message and its request ID.
            "latest_utterance_text": previous.get("text"),
            "latest_utterance_speaker": previous.get("speaker"),
            "latest_utterance_request_id": previous.get("request_id"),
            "player_source_text": player_source.get("text") if player_source else previous.get("text"),
            "player_source_request_id": player_source.get("request_id") if player_source else previous.get("request_id"),
            "player_source_analysis": deepcopy(player_source.get("message_analysis"))
            if player_source and isinstance(player_source.get("message_analysis"), Mapping) else None,
            "player_country_tag": chat["player_country"],
            "response_language": chat.get("response_language", "en"),
            "interface_language": chat.get("interface_language", "en"),
            "diplomatic_memory": diplomatic_memory_pair_summary(
                self._memory_for_chat(chat), campaign_id=chat["campaign_id"],
                observer_tag=country, counterpart_tag=str(previous.get("speaker") or "").upper(),
            ),
            "_verified_memory_capability": chat_engine._verified_memory_capability,
            "participant_country_names": participant_country_names,
            "active_agreement_action_participants": active_agreement_action_participants,
            "agreement_confirmation_review": agreement_confirmation_review(
                chat, country,
                str(player_source.get("text", "") if player_source else previous.get("text", "")),
            ),
        }
        # Keep the exact source payload and pre-generation model state. A retry
        # restores both and replaces this response slot instead of replaying a
        # player turn or advancing the negotiation/version sequence.
        child_state_before = chat_engine.export_chat_state()
        response_payload_for_retry = deepcopy(response_payload)
        response_payload_for_retry.pop("_verified_observation_capability", None)
        response_payload_for_retry.pop("_verified_memory_capability", None)
        retry_context = {
            "response_payload": response_payload_for_retry,
            "verified_observation": latest_observation is not None,
            "child_state_before": child_state_before,
            "request_id": request_id,
            "speaker": country,
            "source_request_id": player_source.get("request_id") if player_source else previous.get("request_id"),
            "preexisting_agreement_versions": {
                agreement_id: {
                    "current_version": item.get("current_version"),
                    "status": item.get("status"),
                }
                for agreement_id, item in chat.get("agreements", {}).items()
                if isinstance(item, Mapping)
            },
        }
        from contextual_consent import plan_reply, engine_result, commit_reply
        contextual = plan_reply(chat, player_source, country, request_id)
        result = engine_result(contextual, _utc_now(), request_id) if contextual else chat_engine.respond(response_payload)
        source_analysis = result.get("message_analysis")
        if (player_source and isinstance(source_analysis, Mapping)
                and not isinstance(player_source.get("message_analysis"), Mapping)
                and source_analysis.get("source_request_id") == player_source.get("request_id")):
            # Persist the source-grounded analysis once on the player message.
            # Every later country turn in this same round then uses the same
            # actor/target mapping rather than reinterpreting it from the last
            # AI speaker's country.
            player_source["message_analysis"] = deepcopy(dict(source_analysis))
        entry = {
            "speaker": country,
            "recipient": previous["speaker"],
            "text": result["response"],
            "timestamp": result["timestamp"],
            "round": chat["round"],
            "kind": "country_response",
            "request_id": request_id,
            "campaign_id": chat["campaign_id"],
            "conversation_id": chat["conversation_id"],
            "player_source_request_id": player_source.get("request_id") if player_source else None,
            "intent": result["intent"],
            "decision": result["decision"],
            "diplomatic_decision": deepcopy(result.get("diplomatic_decision", {})),
            "diplomatic_expression": deepcopy(result.get("diplomatic_expression", {})),
            "strategic_plan": deepcopy(result.get("strategic_plan", {})),
            "reasoning_factors": result["reasoning_factors"],
            "action": result["action"],
            "communication_acts": deepcopy(result.get("communication_acts", {})),
        }
        if isinstance(result.get("message_analysis"), Mapping):
            entry["message_analysis"] = deepcopy(result["message_analysis"])
        if isinstance(result.get("dialogue_positions"), list):
            entry["dialogue_positions"] = deepcopy(result["dialogue_positions"])
            entry["dialogue_position_status"] = result.get("dialogue_position_status", "unavailable")
        if isinstance(result.get("provider_diagnostic"), Mapping):
            diagnostic = result["provider_diagnostic"]
            entry["provider_status"] = diagnostic.get("status")
            entry["dialogue_reply_status"] = (
                "verified_context_summary" if diagnostic.get("reason") == "verified_snapshot_summary"
                else "deduplicated_fallback" if diagnostic.get("dialogue_override")
                == "engine_deduplicated_response"
                else "quality_guard_fallback" if diagnostic.get("dialogue_override")
                == "engine_quality_guard_fallback"
                else "provider_fallback" if diagnostic.get("status") != "generated"
                else "partial_safe" if entry.get("dialogue_position_status")
                in {"partial_safe", "safe_unresolved"}
                else diagnostic.get("status")
            )
            if diagnostic.get("provider"):
                entry["provider_name"] = diagnostic["provider"]
            if diagnostic.get("request_id") == request_id:
                entry["provider_request_id"] = diagnostic["request_id"]
            if diagnostic.get("reason"):
                entry["provider_reason"] = diagnostic["reason"]
            if diagnostic.get("expression_error"):
                entry["provider_expression_error"] = diagnostic["expression_error"]
            if isinstance(diagnostic.get('conversation_progress'),Mapping):
                entry['conversation_progress']=deepcopy(diagnostic['conversation_progress'])
            if diagnostic.get("previous_status"):
                entry["provider_previous_status"] = diagnostic["previous_status"]
            if diagnostic.get("dialogue_override"):
                entry["dialogue_override"] = diagnostic["dialogue_override"]
            if diagnostic.get("dialogue_reason"):
                entry["dialogue_reason"] = diagnostic["dialogue_reason"]
            if isinstance(diagnostic.get("elapsed_seconds"), (int, float)):
                entry["provider_elapsed_seconds"] = diagnostic["elapsed_seconds"]
            if isinstance(diagnostic.get("quality_repairs"), list):
                entry["provider_quality_repairs"] = list(diagnostic["quality_repairs"])
            if (diagnostic.get("provider") == "ollama"
                    and isinstance(diagnostic.get("diagnostic_text"), str)
                    and diagnostic["diagnostic_text"].strip()):
                entry["provider_diagnostic_text"] = diagnostic["diagnostic_text"][:4000]
            if (diagnostic.get("provider") == "ollama"
                    and diagnostic.get("status") == "generated"
                    and entry.get("dialogue_reply_status") in {"partial_safe", "safe_unresolved"}
                    and any(isinstance(position, Mapping)
                            and position.get("origin") == "safety_fallback"
                            for position in entry.get("dialogue_positions", []))):
                # The model completed the HTTP generation, but the validated
                # answer still contains a synthesized unresolved position.
                # Surface Retry as a semantic-quality issue, never as assent.
                entry["provider_reason"] = "semantic_mismatch"
            entry["retryable"] = self._is_retryable_generation_error(entry)
        if isinstance(result.get("agreement_review"), Mapping):
            entry["agreement_review"] = deepcopy(result["agreement_review"])
            entry["agreement_context_request_ids"] = list(
                result["agreement_review"].get("context_request_ids", [])
            )
        _prepare_final_refusal_event(chat, player_source, entry)
        from phrase_usage_memory import commit as commit_phrase_usage
        commit_phrase_usage(chat,entry,(result.get('provider_diagnostic') or {}).get('expression_choices',[]),phrase_person)
        from native_leadership import attribution
        entry['representative']=attribution(self,chat,entry.get('speaker'))
        chat["messages"].append(entry)
        chat.setdefault("retry_contexts", {})
        chat.setdefault("retry_attempts", {})
        if entry.get("retryable"):
            retry_context["agreement_snapshot"] = deepcopy(chat.get("agreements", {}))
            chat["retry_contexts"][request_id] = retry_context
        # Provider failures and quality-guard fallbacks are diagnostic only.
        # They can never create agreement positions or count as assent.
        agreement = None if (
            contextual or entry.get("retryable")
            or entry.get("provider_status") in {"fallback", "timeout", "error", "invalid_json"}
        ) else create_or_update_agreement(chat, entry)
        if contextual:
            commit_reply(contextual, entry)
        if agreement is not None:
            entry["agreement_id"] = agreement["agreement_id"]
            entry["agreement_version"] = agreement["current_version"]
            entry["agreement_status"] = agreement["status"]
        elif isinstance(result.get("agreement_review"), Mapping):
            # A sourced status summary refers to the existing draft but must
            # not create a new agreement version or change its lifecycle.
            agreement_id = result["agreement_review"].get("agreement_id")
            existing = chat.get("agreements", {}).get(agreement_id)
            if isinstance(existing, Mapping):
                entry["agreement_id"] = agreement_id
                entry["agreement_version"] = existing.get("current_version")
                entry["agreement_status"] = existing.get("status")
        if (entry.get("provider_name") == "ollama"
                and entry.get("provider_status") == "generated"
                and entry.get("dialogue_position_status") == "complete"
                and isinstance(entry.get("dialogue_positions"), list)):
            version = entry.get("agreement_version")
            if isinstance(version, bool) or not isinstance(version, int):
                version = None
            position_source_request_id = entry.get("player_source_request_id")
            if not isinstance(position_source_request_id, str):
                position_source_request_id = next((
                    position.get("source_request_id")
                    for position in entry["dialogue_positions"]
                    if isinstance(position, Mapping)
                    and isinstance(position.get("source_request_id"), str)
                ), request_id)
            remember_validated_positions(
                self._memory_for_chat(chat),
                campaign_id=chat["campaign_id"], observer_tag=country,
                counterpart_tag=str(entry.get("recipient") or "").upper(),
                source_request_id=position_source_request_id,
                response_request_id=request_id, version=version,
                positions=entry["dialogue_positions"],
            )
        if not chat["response_queue"]:
            chat["state"] = "awaiting_player"
        chat["updated_at"] = _utc_now()
        chat["request_sequence"] += 1
        response = {"message": deepcopy(entry), "chat": self._public_chat(chat)}
        chat["processed_request_ids"][request_id] = deepcopy(response)
        return response

    @staticmethod
    def _is_retryable_generation_error(entry: Mapping[str, Any]) -> bool:
        if entry.get("provider_name") != "ollama":
            return False
        status = str(entry.get("provider_status", "")).lower()
        reply_status = str(entry.get("dialogue_reply_status", "")).lower()
        if (status == "generated" and reply_status in {"partial_safe", "safe_unresolved"}
                and any(isinstance(position, Mapping)
                        and position.get("origin") == "safety_fallback"
                        for position in entry.get("dialogue_positions", []))):
            return True
        if status == "generated" and reply_status not in {
                "quality_guard_fallback", "provider_fallback", "deduplicated_fallback"}:
            return False
        return (status in {"fallback", "timeout", "error", "invalid_json", "semantic_mismatch"}
                or reply_status in {"quality_guard_fallback", "provider_fallback"}
                or str(entry.get("provider_reason", "")).lower() in {
                    "timeout", "wrong_language", "invalid_json", "semantic_mismatch",
                    "fallback", "generation_error", "empty_response",
                    "strategic_position_mismatch",
                })

    def retry_chat_response(self, chat_id: str, response_request_id: str,
                            attempt_id: str) -> Dict[str, Any]:
        """Regenerate one failed Ollama response in place with a distinct attempt ID."""
        if not isinstance(attempt_id, str) or not re.fullmatch(r"[0-9a-f]{32}", attempt_id):
            raise ValueError("attempt_id must be a 32-character lowercase UUID hex string")
        if not isinstance(response_request_id, str) or not response_request_id.strip():
            raise ValueError("response_request_id is required")
        chat = self._chat(chat_id)
        self._require_current_save_scope(chat)
        chat.setdefault("retry_attempts", {})
        cached = chat["retry_attempts"].get(attempt_id)
        if cached is not None:
            if cached.get("request_id") != response_request_id:
                raise ChatConflict("attempt_id is already bound to another response")
            return {"message": deepcopy(cached.get("message")), "chat": self._public_chat(chat),
                    "idempotent_replay": True, "attempt_id": attempt_id,
                    "request_id": response_request_id}
        lock = self._chat_retry_locks.setdefault(chat_id, threading.Lock())
        if not lock.acquire(blocking=False):
            raise ChatConflict("a response retry is already running for this chat")
        try:
            # Recheck after acquiring the per-chat lock to make duplicate calls
            # with the same attempt ID idempotent under direct engine use too.
            cached = chat["retry_attempts"].get(attempt_id)
            if cached is not None:
                if cached.get("request_id") != response_request_id:
                    raise ChatConflict("attempt_id is already bound to another response")
                return {"message": deepcopy(cached.get("message")), "chat": self._public_chat(chat),
                        "idempotent_replay": True, "attempt_id": attempt_id,
                        "request_id": response_request_id}
            messages = chat.get("messages", [])
            if (chat.get("state") != "awaiting_player" or not messages
                    or messages[-1].get("kind") != "country_response"
                    or messages[-1].get("request_id") != response_request_id):
                raise ChatConflict("only the latest completed country response can be retried")
            failed_entry = messages[-1]
            if not self._is_retryable_generation_error(failed_entry):
                raise ChatConflict("the latest country response has no confirmed retryable generation error")
            retry_count = sum(
                1 for attempt in chat["retry_attempts"].values()
                if isinstance(attempt, Mapping) and attempt.get("request_id") == response_request_id
            )
            if retry_count >= self.MAX_RESPONSE_RETRIES:
                raise ChatConflict("the bounded retry limit for this response has been reached")
            context = chat.get("retry_contexts", {}).get(response_request_id)
            if not isinstance(context, Mapping):
                raise ChatConflict("the original generation context is unavailable")
            # Do not retry after any draft was approved/prepared or after its
            # version changed since the failed response.
            expected_versions = context.get("preexisting_agreement_versions", {})
            for agreement_id, expected in expected_versions.items():
                agreement = chat.get("agreements", {}).get(agreement_id)
                if (not isinstance(agreement, Mapping)
                        or agreement.get("current_version") != expected.get("current_version")
                        or agreement.get("status") != expected.get("status")
                        or agreement.get("status") != "draft"):
                    raise ChatConflict("agreement state changed since the failed generation")
            if any(item.get("status") in {"awaiting_approval", "approved", "prepared", "executed",
                                          "manual_required", "execution_confirmed", "failed", "blocked"}
                   for item in chat.get("agreements", {}).values() if isinstance(item, Mapping)):
                raise ChatConflict("a formalized agreement cannot be changed by retry")

            chat_engine = self._chat_engines[chat_id]
            chat_engine._point_plan_sink = lambda key, points: self._store_point_policy(chat, key, points)
            chat_engine._point_plan_history = chat.setdefault("engine_point_policies", {})
            live_child_state = chat_engine.export_chat_state()
            from contextual_consent import plan_reply, engine_result, commit_reply
            player_source = next((item for item in reversed(messages)
                                  if item.get("request_id") == context.get("source_request_id")), None)
            contextual = plan_reply(chat, player_source, failed_entry.get("speaker"), response_request_id)
            if not contextual:
                chat_engine.import_chat_state(context["child_state_before"])
            # Child restore reconstructs an independent JSON copy; keep it
            # attached to the root campaign ledger for this bounded retry.
            chat_engine.diplomatic_memory = self._memory_for_chat(chat)
            retry_payload = deepcopy(context["response_payload"])
            agreement_snapshot = context.get("agreement_snapshot")
            if isinstance(agreement_snapshot, Mapping):
                # V5 retry contexts can outlive a server update. Rebuild this
                # derived prompt projection from the same immutable draft
                # snapshot when an older payload omitted term/source fields.
                # The version guard above ensures the live draft has not
                # changed; this only restores metadata and grants no consent.
                retry_payload["active_agreement_action_participants"] = (
                    self._active_agreement_action_participant_context(chat, agreement_snapshot)
                )
            if context.get("verified_observation"):
                retry_payload["_verified_observation_capability"] = chat_engine._verified_observation_capability
            if isinstance(retry_payload.get("diplomatic_memory"), Mapping):
                retry_payload["_verified_memory_capability"] = chat_engine._verified_memory_capability
            retry_payload["request_id"] = response_request_id
            try:
                result = (engine_result(contextual, _utc_now(), response_request_id)
                          if contextual else chat_engine.respond(retry_payload))
            except Exception:
                chat_engine.import_chat_state(live_child_state)
                raise

            # A direct caller may race a later mutation while the provider is
            # generating. Discard the stale result and preserve current state.
            if (not chat.get("messages") or chat["messages"][-1].get("request_id") != response_request_id
                    or chat["messages"][-1].get("kind") != "country_response"
                    or chat.get("state") != "awaiting_player"):
                chat_engine.import_chat_state(live_child_state)
                raise ChatConflict("the response became stale while retry generation was running")

            replacement = deepcopy(failed_entry)
            replacement.update({
                "text": result["response"], "timestamp": result["timestamp"],
                "intent": result["intent"], "decision": result["decision"],
                "diplomatic_decision": deepcopy(result.get("diplomatic_decision", {})),
                "diplomatic_expression": deepcopy(result.get("diplomatic_expression", {})),
                "reasoning_factors": deepcopy(result["reasoning_factors"]),
                "action": deepcopy(result["action"]),
            })
            for key in ("message_analysis", "dialogue_positions", "dialogue_position_status",
                        "agreement_review", "agreement_context_request_ids", "provider_status",
                        "dialogue_reply_status", "provider_name", "provider_request_id",
                        "provider_reason", "provider_previous_status", "dialogue_override",
                        "provider_expression_error",
                        "dialogue_reason", "provider_elapsed_seconds", "provider_quality_repairs",
                        "provider_diagnostic_text", "retryable", "diplomatic_event_package"):
                replacement.pop(key, None)
            if isinstance(result.get("message_analysis"), Mapping):
                replacement["message_analysis"] = deepcopy(result["message_analysis"])
            if isinstance(result.get("dialogue_positions"), list):
                replacement["dialogue_positions"] = deepcopy(result["dialogue_positions"])
                replacement["dialogue_position_status"] = result.get("dialogue_position_status", "unavailable")
            diagnostic = result.get("provider_diagnostic")
            if isinstance(diagnostic, Mapping):
                replacement["provider_status"] = diagnostic.get("status")
                replacement["dialogue_reply_status"] = (
                    "quality_guard_fallback" if diagnostic.get("dialogue_override") == "engine_quality_guard_fallback"
                    else "deduplicated_fallback" if diagnostic.get("dialogue_override") == "engine_deduplicated_response"
                    else "provider_fallback" if diagnostic.get("status") != "generated"
                    else "partial_safe" if replacement.get("dialogue_position_status") in {"partial_safe", "safe_unresolved"}
                    else diagnostic.get("status")
                )
                if diagnostic.get("provider"):
                    replacement["provider_name"] = diagnostic["provider"]
                if diagnostic.get("reason"):
                    replacement["provider_reason"] = diagnostic["reason"]
                if diagnostic.get("expression_error"):
                    replacement["provider_expression_error"] = diagnostic["expression_error"]
                if isinstance(diagnostic.get("elapsed_seconds"), (int, float)):
                    replacement["provider_elapsed_seconds"] = diagnostic["elapsed_seconds"]
                if isinstance(diagnostic.get("quality_repairs"), list):
                    replacement["provider_quality_repairs"] = list(diagnostic["quality_repairs"])
                if (diagnostic.get("provider") == "ollama"
                        and isinstance(diagnostic.get("diagnostic_text"), str)
                        and diagnostic["diagnostic_text"].strip()):
                    replacement["provider_diagnostic_text"] = diagnostic["diagnostic_text"][:4000]
                if (diagnostic.get("provider") == "ollama"
                        and diagnostic.get("status") == "generated"
                        and replacement.get("dialogue_reply_status") in {"partial_safe", "safe_unresolved"}
                        and any(isinstance(position, Mapping)
                                and position.get("origin") == "safety_fallback"
                                for position in replacement.get("dialogue_positions", []))):
                    replacement["provider_reason"] = "semantic_mismatch"
                replacement["retryable"] = self._is_retryable_generation_error(replacement)
            replacement["retry_attempt_id"] = attempt_id
            completed_retry_count = retry_count + 1
            replacement["retry_count"] = completed_retry_count
            if replacement.get("retryable") and completed_retry_count >= self.MAX_RESPONSE_RETRIES:
                replacement["retryable"] = False
                replacement["retry_exhausted"] = True
            source_request_id = context.get("source_request_id")
            source_message = next((item for item in chat.get("messages", [])
                                   if item.get("request_id") == source_request_id
                                   and item.get("kind") == "player_message"), None)
            _prepare_final_refusal_event(chat, source_message, replacement)

            # Replace the old failed turn in place. A failed generation never
            # created or revised an agreement; on success update a matching
            # draft's latest version in place rather than incrementing it.
            old_agreements = deepcopy(chat.get("agreements", {}))
            chat["messages"][-1] = replacement
            # Existing agreements are immutable under retry. In particular, do
            # not revise a term, term_id, approval state, or version as a side
            # effect of regenerating a chat turn.
            agreement = None
            if (not replacement.get("retryable") and not old_agreements
                    and replacement.get("provider_status") not in {
                        "fallback", "timeout", "error", "invalid_json",
                    }):
                agreement = create_or_update_agreement(chat, replacement)
            if contextual:
                commit_reply(contextual, replacement)
            elif agreement is not None:
                replacement["agreement_id"] = agreement["agreement_id"]
                replacement["agreement_version"] = chat["agreements"][agreement["agreement_id"]]["current_version"]
                replacement["agreement_status"] = chat["agreements"][agreement["agreement_id"]]["status"]
            else:
                chat["agreements"] = old_agreements
            if (replacement.get("provider_name") == "ollama"
                    and replacement.get("provider_status") == "generated"
                    and replacement.get("dialogue_position_status") == "complete"
                    and isinstance(replacement.get("dialogue_positions"), list)):
                version = replacement.get("agreement_version")
                if isinstance(version, bool) or not isinstance(version, int):
                    version = None
                position_source_request_id = replacement.get("player_source_request_id")
                if not isinstance(position_source_request_id, str):
                    position_source_request_id = next((
                        position.get("source_request_id")
                        for position in replacement["dialogue_positions"]
                        if isinstance(position, Mapping)
                        and isinstance(position.get("source_request_id"), str)
                    ), response_request_id)
                remember_validated_positions(
                    self._memory_for_chat(chat),
                    campaign_id=chat["campaign_id"],
                    observer_tag=str(replacement.get("speaker") or "").upper(),
                    counterpart_tag=str(replacement.get("recipient") or "").upper(),
                    source_request_id=position_source_request_id,
                    response_request_id=response_request_id, version=version,
                    positions=replacement["dialogue_positions"],
                )
            chat["updated_at"] = _utc_now()
            response = {"message": deepcopy(replacement), "chat": self._public_chat(chat),
                        "attempt_id": attempt_id, "request_id": response_request_id}
            chat["retry_attempts"][attempt_id] = {
                "request_id": response_request_id,
                "previous_message": {
                    key: deepcopy(failed_entry[key])
                    for key in (
                        "speaker", "recipient", "text", "timestamp", "request_id",
                        "provider_status", "provider_reason", "provider_name",
                        "provider_elapsed_seconds", "provider_diagnostic_text",
                        "dialogue_reply_status", "dialogue_position_status",
                        "dialogue_positions", "retryable",
                    ) if key in failed_entry
                },
                "message": deepcopy(replacement),
                "completed_at": chat["updated_at"],
            }
            # Keep original V5 advance idempotency slot pointed at the current
            # replacement while preserving its request identity.
            if response_request_id in chat.get("processed_request_ids", {}):
                chat["processed_request_ids"][response_request_id] = deepcopy(response)
            return response
        finally:
            lock.release()

    def list_agreements(self, chat_id: str) -> List[Dict[str, Any]]:
        return [deepcopy(value) for value in self._chat(chat_id).setdefault("agreements", {}).values()]

    def list_chat_news(self, chat_id: str) -> Dict[str, Any]:
        """Return only durable news items backed by a verified game-log marker."""
        chat = self._chat(chat_id)
        news = [deepcopy(item) for item in chat.get("diplomatic_news", [])
                if isinstance(item, dict) and item.get("source") in _NEWS_SOURCES]
        news.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return {"chat_id": chat["chat_id"], "campaign_id": chat["campaign_id"], "news": news}

    def list_world_news(self, campaign_id: Optional[str] = None) -> Dict[str, Any]:
        """Aggregate game-confirmed outcomes, optionally isolated by campaign."""
        if campaign_id is not None and not _CAMPAIGN_ID_RE.fullmatch(str(campaign_id)):
            raise ValueError("campaign_id must be a lowercase UUID hex string")
        news = []
        for chat in self.chats.values():
            if campaign_id is not None and chat.get("campaign_id") != campaign_id:
                continue
            news.extend(deepcopy(item) for item in chat.get("diplomatic_news", [])
                        if isinstance(item, dict) and item.get("source") in _NEWS_SOURCES)
        news.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return {"campaign_id": campaign_id, "news": news}

    def record_agreement_state_verification(self, *, campaign_id: str, request_id: str,
                                            evidence: Mapping[str, Any]) -> Dict[str, Any]:
        """Add the stricter "effect is in force" news item after a marker was seen.

        A game-log marker proves the event ran. It does not prove the relation
        exists. This records a separate, campaign-scoped feed entry only when an
        independent observation (state snapshot or explicit in-game inspection)
        says the agreed effect is present. It never changes the agreement status.
        """
        if not _CAMPAIGN_ID_RE.fullmatch(str(campaign_id)):
            raise ValueError("campaign_id must be a lowercase UUID hex string")
        if not isinstance(request_id, str) or not request_id or len(request_id) > 512:
            raise ValueError("execution request_id is required")
        if not isinstance(evidence, Mapping) or evidence.get("observed") is not True:
            raise ValueError("verification evidence must state observed=True")
        if evidence.get("source") not in {"hoi4_state_snapshot", "manual_in_game_inspection"}:
            raise ValueError("unsupported state verification source")
        if evidence.get("campaign_id") not in (None, campaign_id):
            raise ValueError("verification evidence belongs to another campaign")
        found = None
        for chat in self.chats.values():
            if chat.get("campaign_id") != campaign_id:
                continue
            for agreement in (chat.get("agreements") or {}).values():
                if isinstance(agreement, dict) and agreement.get("execution_request_id") == request_id:
                    found = (chat, agreement)
                    break
            if found:
                break
        if found is None:
            raise KeyError("no agreement waits for this execution request")
        chat, agreement = found
        if agreement.get("status") != "execution_confirmed":
            raise ChatConflict("marker confirmation is required before state verification")
        before = len(chat.get("diplomatic_news") or [])
        item = _append_game_news(
            chat,
            unique_key=f"agreement:{agreement['agreement_id']}:v{agreement.get('current_version')}:verified",
            status="execution_verified",
            marker=str(evidence.get("marker") or (agreement.get("execution_result") or {}).get("marker") or ""),
            participants=agreement.get("participants", []),
            action_id=agreement.get("action_id", "unknown"),
            source_request_id=request_id, agreement_id=agreement["agreement_id"],
            agreement_name=agreement.get("name"),
            agreement_version=agreement.get("current_version"),
            summary=f"Independent {evidence['source']} confirms the agreed effect is in force.",
            source="hoi4_state_snapshot",
        )
        chat["updated_at"] = _utc_now()
        return {"recorded": True, "duplicate": len(chat.get("diplomatic_news") or []) == before,
                "news_id": item["news_id"], "headline": item["headline"]}

    def record_treaty_declined(self, *, agreement_id: str, version: Any, request_id: str,
                               marker: str) -> Dict[str, Any]:
        """Record the player declining the preloaded decision inside HOI4.

        Without this the in-game "Not now" option was silent: EYES kept waiting
        for an application that could never arrive. The agreement status is not
        rewritten here; only the player's in-game decision and one news item are.
        """
        if not isinstance(agreement_id, str) or not re.fullmatch(r"[a-f0-9]{32}", agreement_id):
            raise ValueError("agreement_id must be a lowercase UUID hex string")
        if type(version) is not int or version < 1:
            raise ValueError("agreement version must be a positive integer")
        if not isinstance(request_id, str) or not request_id or len(request_id) > 512:
            raise ValueError("execution request_id is required")
        if not isinstance(marker, str) or not marker or len(marker) > 512:
            raise ValueError("decline marker is required")
        found = None
        for chat in self.chats.values():
            agreement = (chat.get("agreements") or {}).get(agreement_id)
            if isinstance(agreement, dict):
                found = (chat, agreement)
                break
        if found is None:
            raise KeyError("agreement not found")
        chat, agreement = found
        if agreement.get("current_version") != version:
            raise ChatConflict("decline refers to a different treaty version")
        if agreement.get("execution_request_id") != request_id:
            raise ChatConflict("decline refers to a different prepared execution")
        if not agreement.get("execution_package"):
            raise ChatConflict("no prepared in-game package was declined")
        previous = agreement.get("in_game_decision")
        if isinstance(previous, dict) and previous.get("decision") == "declined":
            return {"recorded": True, "duplicate": True, "agreement_id": agreement_id,
                    "status": agreement.get("status")}
        agreement["in_game_decision"] = {"decision": "declined", "version": version,
                                         "request_id": request_id, "marker": marker,
                                         "recorded_at": _utc_now()}
        _append_game_news(
            chat,
            unique_key=f"agreement:{agreement_id}:v{version}:declined",
            status="decision_rejected", marker=marker,
            participants=agreement.get("participants", []),
            action_id=agreement.get("action_id", "unknown"),
            source_request_id=request_id, agreement_id=agreement_id,
            agreement_name=agreement.get("name"), agreement_version=version,
            summary="The player declined the prepared treaty decision inside HOI4.",
        )
        chat["updated_at"] = _utc_now()
        return {"recorded": True, "duplicate": False, "agreement_id": agreement_id,
                "status": agreement.get("status")}

    def formalize_latest_acceptance(self, chat_id: str) -> Dict[str, Any]:
        chat = self._chat(chat_id)
        chat.setdefault("agreement_formalization_requests", {})
        accepted = next((item for item in reversed(chat.get("messages", []))
                         if item.get("kind") == "country_response"
                         and item.get("decision") == "accept"), None)
        if accepted is None:
            raise ChatConflict("there is no accepted country response to formalize")
        source_request_id = accepted.get("request_id")
        if not source_request_id:
            raise ChatConflict("accepted response has no source request_id")
        idempotency_key = f"formalize:{source_request_id}"
        if idempotency_key in chat["agreement_formalization_requests"]:
            return deepcopy(chat["agreement_formalization_requests"][idempotency_key])
        agreement = create_or_update_agreement(chat, accepted)
        if agreement is None:
            raise ChatConflict("accepted response does not reference a supported action")
        chat["agreement_formalization_requests"][idempotency_key] = deepcopy(agreement)
        chat["updated_at"] = _utc_now()
        return agreement

    def recalculate_agreement_confirmations(
            self, chat_id: str, agreement_id: str, *, expected_version: int,
            source_request_ids: List[str]) -> Dict[str, Any]:
        """Re-evaluate saved consent evidence without formalizing or preparing an action.

        This is deliberately separate from normal formalization idempotency. The
        operation is scoped to an exact pair of persisted messages and a policy
        revision, and is serialized so concurrent retries cannot append twice.
        """
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version must be a positive integer")
        if (not isinstance(source_request_ids, list) or len(source_request_ids) != 2
                or any(not isinstance(value, str) or not value.strip()
                       for value in source_request_ids)):
            raise ValueError("source_request_ids must contain the two exact source request IDs")

        with self._agreement_recalculation_lock:
            chat = self._chat(chat_id)
            agreement = self._agreement(chat, agreement_id)
            digest = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{agreement_id}|{AGREEMENT_RECALCULATION_REVISION}|"
                f"{'|'.join(source_request_ids)}",
            ).hex
            operation_key = f"recalculate:v{AGREEMENT_RECALCULATION_REVISION}:{digest}"
            chat.setdefault("agreement_recalculation_requests", {})
            cached = chat["agreement_recalculation_requests"].get(operation_key)
            if cached is not None:
                replay = deepcopy(cached)
                replay["agreement"] = deepcopy(agreement)
                replay["changed"] = False
                replay["idempotent_replay"] = True
                return replay
            if agreement.get("status") not in {"draft", "awaiting_approval"}:
                raise ChatConflict("only a draft agreement can be recalculated")
            if agreement.get("current_version") != expected_version:
                raise ChatConflict("agreement version changed; reload before recalculating")
            if agreement.get("execution_package") or agreement.get("execution_request_id"):
                raise ChatConflict("agreement with a prepared game action cannot be recalculated")
            if agreement.get("approval_requests"):
                raise ChatConflict("agreement with an approval record cannot be recalculated")

            messages = chat.get("messages", [])
            by_request = {item.get("request_id"): item for item in messages
                          if item.get("request_id")}
            source_messages = [by_request.get(value) for value in source_request_ids]
            if any(item is None for item in source_messages):
                raise ChatConflict("one or more source messages are no longer present")
            if source_messages != messages[-2:]:
                raise ChatConflict("source messages must be the latest two saved messages")
            player_message, accepted = source_messages
            player = chat.get("player_country")
            if (player_message.get("speaker") != player
                    or player_message.get("kind") != "player_message"
                    or accepted.get("speaker") not in chat.get("participants", [])
                    or accepted.get("speaker") == player
                    or accepted.get("kind") != "country_response"
                    or accepted.get("decision") != "accept"):
                raise ChatConflict("source messages do not form a player message followed by country acceptance")
            if accepted.get("agreement_id") not in {None, agreement_id}:
                raise ChatConflict("accepted response references a different agreement")
            if accepted.get("agreement_version") not in {None, expected_version}:
                raise ChatConflict("accepted response references a different proposal version")
            if agreement.get("action_id") != "non_aggression_pact":
                raise ChatConflict("this recalculation operation only supports the saved NAP agreement")

            previous = agreement.get("versions", [])[-1] if agreement.get("versions") else None
            if not isinstance(previous, dict) or previous.get("version") != expected_version:
                raise ChatConflict("latest immutable agreement version is unavailable")
            # Reuse the action metadata already stored on this agreement. This
            # analysis does not query Action Registry or prepare any execution.
            registry_snapshot = {
                "supported_by_hoi4": any(
                    item.get("supported_by_hoi4") is True
                    for item in previous.get("terms", [])
                ),
                "implemented_in_eyes": False,
            }
            candidate = build_acceptance_version(
                chat, accepted, expected_version + 1,
                registry_action=registry_snapshot,
            )
            if candidate.get("action_id") != agreement.get("action_id"):
                raise ChatConflict("recalculated evidence resolves to a different action")

            changed = consent_fingerprint(previous) != consent_fingerprint(candidate)
            if changed:
                agreement["versions"].append(candidate)
                agreement["current_version"] = candidate["version"]
                agreement["status"] = candidate["status"]
                agreement["updated_at"] = _utc_now()
                chat["updated_at"] = agreement["updated_at"]
            result = {
                "agreement": deepcopy(agreement),
                "changed": changed,
                "created_version": candidate["version"] if changed else None,
                "source_request_ids": list(source_request_ids),
                "idempotency_key": operation_key,
                "idempotent_replay": False,
            }
            chat["agreement_recalculation_requests"][operation_key] = deepcopy(result)
            return result

    def get_agreement(self, chat_id: str, agreement_id: str) -> Dict[str, Any]:
        return deepcopy(self._agreement(self._chat(chat_id), agreement_id))

    def approve_agreement(self, chat_id: str, agreement_id: str, *, version: int,
                          request_id: str, confirm: bool) -> Dict[str, Any]:
        chat = self._chat(chat_id)
        self._require_current_save_scope(chat)
        agreement = self._agreement(chat, agreement_id)
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id is required for idempotent approval")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise ValueError("version must be a positive integer")
        processed = agreement.setdefault("approval_requests", {})
        if request_id in processed:
            return deepcopy(processed[request_id])
        from historical_conferences import Conferences
        Conferences(self).ensure_official(chat_id)
        if confirm is not True:
            raise ChatConflict("explicit player confirmation is required")
        if agreement.get("status") != "awaiting_approval":
            raise ChatConflict("agreement is not ready for player approval")
        if version != agreement.get("current_version"):
            raise ChatConflict("approval version is stale; review the current agreement")
        from agreement_manager import directional_parties
        for term in agreement["versions"][version - 1]["terms"]:
            if term.get("term_id") not in {"military_access", "independence_guarantee"} or not term.get("supported_by_hoi4"):
                continue
            actor, recipient = directional_parties(term)
            required = set(term.get("required_participants", []))
            if (term.get("status") != "agreed" or set(term.get("confirmed_by", [])) != required
                    or not required <= set(chat["participants"])
                    or any(not term.get("confirmed_by_request_ids", {}).get(tag) for tag in required)
                    or term.get("unparsed_execution_details")
                    or ((actor or recipient) and (actor == recipient or {actor, recipient} != required))):
                raise ChatConflict("The exact direction and signatory confirmations must be verified before signing.")
        approval_country = chat['player_country']
        origin = chat.get('autonomous_origin')
        if origin:
            from v6_autonomy import status as autonomy_status
            observer = origin.get('human_observer')
            snapshot = autonomy_status(self._memory_for_chat(chat), chat['campaign_id']).get('snapshot')
            if (not snapshot or snapshot.get('player_country') != observer
                    or snapshot.get('countries', {}).get(observer, {}).get('ai_controlled') is not False):
                raise ChatConflict('current human observer must explicitly authorize the AI agreement')
            approval_country = observer
        agreement['policy_validation'] = self._check_independent_acceptance(chat, agreement)
        agreement["status"] = "approved"
        agreement["approved_version"] = version
        agreement["approved_by"] = approval_country
        if origin:
            agreement['approval_authority'] = 'human_observer'
        agreement["approved_at"] = _utc_now()
        result = deepcopy(agreement)
        processed[request_id] = result
        chat["updated_at"] = _utc_now()
        return result

    def prepare_agreement_execution(self, chat_id: str, agreement_id: str, *,
                                    request_id: str,
                                    _execution_request_id: Optional[str] = None) -> Dict[str, Any]:
        chat = self._chat(chat_id)
        self._require_current_save_scope(chat)
        agreement = self._agreement(chat, agreement_id)
        preparations = agreement.setdefault("preparation_requests", {})
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id is required for idempotent preparation")
        if request_id in preparations:
            return deepcopy(preparations[request_id])
        if agreement.get("status") != "approved":
            raise ChatConflict("player must approve the current agreement before action preparation")
        if agreement.get("approved_version") != agreement.get("current_version"):
            raise ChatConflict("approved agreement version is stale")
        self._revalidate_signed_policy(chat, agreement)

        version_number = agreement["current_version"]
        execution_request_id = _execution_request_id or f"agreement:{agreement_id}:v{version_number}"
        if not re.fullmatch(
                rf"agreement:{re.escape(agreement_id)}:v{version_number}(?::term:[a-z][a-z0-9_]*)?",
                execution_request_id):
            raise ValueError("execution_request_id does not match the approved agreement version")

        current_version = agreement["versions"][agreement["current_version"] - 1]
        execution_gaps = _agreement_execution_gaps(
            current_version.get("terms"), self.action_registry, chat=chat,
        )
        if execution_gaps:
            agreement["status"] = "blocked"
            agreement["block_reason"] = "agreement_contains_unenforceable_terms:" + ";".join(execution_gaps)
            agreement["execution_package"] = None
            agreement["execution_request_id"] = None
            result = deepcopy(agreement)
            preparations[request_id] = deepcopy(result)
            agreement["updated_at"] = _utc_now()
            chat["updated_at"] = agreement["updated_at"]
            return result
        executable_terms = [
            term for term in current_version.get("terms", [])
            if isinstance(term, dict)
            and term.get("supported_by_hoi4") is True
            and isinstance(term.get("action_id"), str)
            and (self.action_registry.get(term["action_id"]) or {}).get("supported_by_hoi4") is True
        ]
        if len(executable_terms) > 1 or (
                len(executable_terms) == 1
                and executable_terms[0].get("action_id") != agreement.get("action_id")):
            # A single-event package/result record cannot represent multiple
            # independently revalidated HOI4 effects. Keep the approved
            # agreement intact and require explicit per-term preparation.
            agreement["status"] = "manual_required"
            agreement["block_reason"] = "per_term_execution_required"
            agreement["supported_action_term_ids"] = [term.get("term_id") for term in executable_terms]
            agreement["execution_package"] = None
            agreement["execution_request_id"] = None
            result = deepcopy(agreement)
            preparations[request_id] = result
            agreement["updated_at"] = _utc_now()
            chat["updated_at"] = agreement["updated_at"]
            return result
        if not executable_terms:
            root_action = self.action_registry.get(agreement.get("action_id"))
            if root_action and root_action.get("supported_by_hoi4") is True:
                executable_terms = [{"term_id": agreement.get("action_id"),
                                     "action_id": agreement.get("action_id")}]

        root_action_id = agreement.get("action_id")
        if root_action_id == "non_aggression_pact":
            nap_participants = agreement.get("participants", [])
            if (not isinstance(nap_participants, list)
                    or len(nap_participants) != 2
                    or any(not isinstance(tag, str) or not COUNTRY_TAG_RE.fullmatch(tag)
                           for tag in nap_participants)
                    or len(set(nap_participants)) != 2
                    or chat.get("player_country") not in nap_participants):
                agreement["status"] = "blocked"
                agreement["block_reason"] = "non_aggression_pact_requires_exactly_two_signatories"
                agreement["execution_package"] = None
                agreement["execution_request_id"] = None
                result = deepcopy(agreement)
                preparations[request_id] = deepcopy(result)
                agreement["updated_at"] = _utc_now()
                chat["updated_at"] = agreement["updated_at"]
                return result
        effect_term = next((term for term in current_version.get("terms", [])
                            if isinstance(term, dict)
                            and (term.get("action_id") == root_action_id
                                 or term.get("term_id") == root_action_id)), None)
        if effect_term is not None:
            unsupported_details = _unenforced_effect_details(effect_term, root_action_id)
            if unsupported_details:
                agreement["status"] = "blocked"
                agreement["block_reason"] = (
                    "approved_effect_contains_unenforced_details:"
                    + ",".join(unsupported_details)
                )
                agreement["execution_package"] = None
                agreement["execution_request_id"] = None
                result = deepcopy(agreement)
                preparations[request_id] = result
                agreement["updated_at"] = _utc_now()
                chat["updated_at"] = agreement["updated_at"]
                return result

        action = self.action_registry.get(agreement["action_id"])
        if not action or not action.get("supported_by_hoi4"):
            agreement["status"] = "blocked"
            agreement["block_reason"] = (action or {}).get("reason_if_unsupported", "unsupported_action")
            result = deepcopy(agreement)
            preparations[request_id] = result
            return result

        if agreement["action_id"] == "guarantee" and len(chat.get("participants", [])) != 2:
            agreement["status"] = "blocked"
            agreement["block_reason"] = "guarantee_execution_requires_exactly_two_participants"
            result = deepcopy(agreement)
            preparations[request_id] = result
            return result

        if agreement["action_id"] == "military_access":
            if len(chat.get("participants", [])) != 2:
                agreement["status"] = "blocked"
                agreement["block_reason"] = "military_access_requires_exactly_two_participants"
                result = deepcopy(agreement)
                preparations[request_id] = result
                return result
            current_version = agreement["versions"][agreement["current_version"] - 1]
            access_term = next((term for term in current_version.get("terms", [])
                                if term.get("term_id") == "military_access"), None)
            if (not access_term or access_term.get("status") != "agreed"
                    or access_term.get("unparsed_execution_details")
                    or access_term.get("conditions") or access_term.get("exceptions")
                    or access_term.get("duration")):
                agreement["status"] = "blocked"
                agreement["block_reason"] = "military_access_has_unenforceable_or_unagreed_terms"
                result = deepcopy(agreement)
                preparations[request_id] = result
                return result

        annexation_term = None
        if agreement["action_id"] == "political_annexation":
            current_version = agreement["versions"][agreement["current_version"] - 1]
            annexation_term = next((term for term in current_version.get("terms", [])
                                    if term.get("term_id") == "political_annexation"), None)
            if (len(agreement.get("participants", [])) != 2
                    or set(agreement.get("participants", [])) != set(chat.get("participants", []))
                    or not annexation_term or annexation_term.get("status") != "agreed"
                    or annexation_term.get("unparsed_execution_details")):
                agreement["status"] = "blocked"
                agreement["block_reason"] = "political_annexation_requires_two_explicit_signatories_and_one_fully_agreed_target"
                agreement["execution_package"] = None
                result = deepcopy(agreement)
                preparations[request_id] = result
                return result

        required_context = action.get("required_context", [])
        if root_action_id in {"guarantee", "military_access"}:
            # Country-pair reports identify opposite counterparts. The
            # approved typed clause determines the actual recipient.
            required_context = [key for key in required_context if key != "target_country"]
        context, conflicts = self._agreement_context(chat, required_context)
        if agreement["action_id"] == "guarantee":
            spec = ((effect_term or {}).get("proposal_spec") or {}).get("action") or {}
            context["target_country"] = ((effect_term or {}).get("action_recipient")
                                         or spec.get("recipient_country_tag") or spec.get("target_country_tag")
                                         or chat["player_country"])
        if agreement["action_id"] == "military_access":
            context["target_country"] = (effect_term or {}).get("action_recipient") or chat["player_country"]
        if agreement["action_id"] == "political_annexation":
            target_country = annexation_term.get("target_country_tag")
            if (not isinstance(target_country, str)
                    or target_country not in agreement.get("participants", [])
                    or target_country == chat["player_country"]
                    or annexation_term.get("initiator_country_tag") != chat["player_country"]):
                agreement["status"] = "blocked"
                agreement["block_reason"] = "political_annexation_target_does_not_match_approved_signatories"
                agreement["execution_package"] = None
                result = deepcopy(agreement)
                preparations[request_id] = result
                return result
            context["target_country"] = target_country
            context["all_required_signatories_accepted"] = True
        if agreement["action_id"] in {"embargo", "lift_embargo"}:
            version_record = agreement["versions"][agreement["current_version"] - 1]
            embargo_term = next((term for term in version_record.get("terms", [])
                                 if term.get("term_id") == "embargo_target"), None)
            context["target_country"] = (embargo_term or {}).get("target_country_tag")
            if (len(chat.get("participants", [])) != 2 or not embargo_term
                    or not embargo_term.get("action_actor")
                    or embargo_term.get("unparsed_execution_details")
                    or embargo_term.get("conditions") or embargo_term.get("exceptions")
                    or embargo_term.get("duration")):
                agreement["status"] = "blocked"
                agreement["block_reason"] = "embargo_clause_has_unenforceable_or_unparsed_details"
                agreement["execution_package"] = None
                result = deepcopy(agreement)
                preparations[request_id] = result
                return result
        assessment = self.action_registry.assess(agreement["action_id"], "accept", context)
        if conflicts:
            assessment = {"action_id": agreement["action_id"], "status": "BLOCKED",
                          "reason": "conflicting_game_context: " + ", ".join(conflicts)}
        if assessment.get("status") == "BLOCKED":
            agreement["status"] = "blocked"
            agreement["block_reason"] = assessment.get("reason")
            agreement["execution_gate"] = assessment
            result = deepcopy(agreement)
        elif assessment.get("reason", "").startswith("required_context_unknown:"):
            agreement["status"] = "manual_required"
            agreement["execution_gate"] = assessment
            agreement["execution_package"] = None
            result = deepcopy(agreement)
        elif agreement["action_id"] in GUARDED_EFFECTS:
            agreement["execution_package"] = build_guarded_treaty(chat, agreement, current_version, execution_request_id)
            agreement["status"] = "manual_required"
            agreement["execution_request_id"] = execution_request_id
            agreement["execution_gate"] = assessment
            result = deepcopy(agreement)
        elif agreement["action_id"] == "non_aggression_pact":
            version_record = agreement["versions"][agreement["current_version"] - 1]
            package = build_non_aggression_event_package(
                chat, agreement, version_record, execution_request_id,
            )
            agreement["status"] = "manual_required"
            agreement["execution_request_id"] = execution_request_id
            agreement["execution_gate"] = assessment
            agreement["execution_package"] = package
            result = deepcopy(agreement)
        elif agreement["action_id"] == "guarantee":
            version_record = agreement["versions"][agreement["current_version"] - 1]
            package = build_guarantee_event_package(
                chat, agreement, version_record, execution_request_id,
            )
            agreement["status"] = "manual_required"
            agreement["execution_request_id"] = execution_request_id
            agreement["execution_gate"] = assessment
            agreement["execution_package"] = package
            result = deepcopy(agreement)
        elif agreement["action_id"] in {"embargo", "lift_embargo"}:
            version_record = agreement["versions"][agreement["current_version"] - 1]
            package = build_embargo_event_package(
                chat, agreement, version_record, execution_request_id,
            )
            agreement["status"] = "manual_required"
            agreement["execution_request_id"] = execution_request_id
            agreement["execution_gate"] = assessment
            agreement["execution_package"] = package
            result = deepcopy(agreement)
        elif agreement["action_id"] == "military_access":
            version_record = agreement["versions"][agreement["current_version"] - 1]
            package = build_military_access_event_package(
                chat, agreement, version_record, execution_request_id,
            )
            agreement["status"] = "manual_required"
            agreement["execution_request_id"] = execution_request_id
            agreement["execution_gate"] = assessment
            agreement["execution_package"] = package
            result = deepcopy(agreement)
        elif agreement["action_id"] == "political_annexation":
            version_record = agreement["versions"][agreement["current_version"] - 1]
            package = build_political_annexation_event_package(
                chat, agreement, version_record, execution_request_id,
            )
            agreement["status"] = "manual_required"
            agreement["execution_request_id"] = execution_request_id
            agreement["execution_gate"] = assessment
            agreement["execution_package"] = package
            result = deepcopy(agreement)
        else:
            agreement["status"] = "manual_required"
            agreement["execution_gate"] = assessment
            agreement["execution_package"] = None
            result = deepcopy(agreement)
        agreement["updated_at"] = _utc_now()
        preparations[request_id] = deepcopy(result)
        chat["updated_at"] = _utc_now()
        return result

    def prepare_agreement_term_execution(self, chat_id: str, agreement_id: str, *,
                                         term_id: str, request_id: str) -> Dict[str, Any]:
        """Prepare one approved game-effect term, using the existing action gates.

        Only one generated term event may be pending at a time. The contract
        remains manual_required between terms and is confirmed only after every
        supported term receives its own exact V2 marker.
        """
        chat = self._chat(chat_id)
        self._require_current_save_scope(chat)
        agreement = self._agreement(chat, agreement_id)
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
            raise ValueError("request_id is required for idempotent term preparation")
        if not isinstance(term_id, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", term_id):
            raise ValueError("term_id must be a lowercase agreement term identifier")

        prep_requests = agreement.setdefault("term_preparation_requests", {})
        prior_request = prep_requests.get(request_id)
        if prior_request is not None:
            if prior_request.get("term_id") != term_id:
                raise ChatConflict("request_id was already used for a different agreement term")
            return deepcopy(prior_request["response"])
        if agreement.get("status") not in {"approved", "manual_required"}:
            raise ChatConflict("player must approve the current agreement before term preparation")
        if agreement.get("approved_version") != agreement.get("current_version"):
            raise ChatConflict("approved agreement version is stale")
        self._revalidate_signed_policy(chat, agreement)
        if (agreement.get("execution_package") or {}).get("bundle_term_ids"):
            raise ChatConflict("this agreement already has a combined native review")
        version_number = agreement["current_version"]
        version = agreement["versions"][version_number - 1]
        term = next((item for item in version.get("terms", [])
                     if isinstance(item, dict) and item.get("term_id") == term_id), None)
        if term is None:
            raise ValueError("term_id is not present in the approved agreement version")

        execution_gaps = _agreement_execution_gaps(version.get("terms"), self.action_registry, chat=chat)
        if execution_gaps:
            record = {
                "term_id": term_id, "action_id": term.get("action_id"),
                "status": "blocked",
                "reason": "agreement_contains_unenforceable_terms:" + ";".join(execution_gaps),
                "execution_request_id": None, "execution_package": None,
            }
            agreement.setdefault("term_executions", {})[term_id] = record
            agreement["status"] = "blocked"
            agreement["block_reason"] = record["reason"]
            agreement["updated_at"] = _utc_now()
            chat["updated_at"] = agreement["updated_at"]
            response = deepcopy(agreement)
            response["prepared_term"] = deepcopy(record)
            prep_requests[request_id] = {"term_id": term_id, "response": deepcopy(response)}
            return response

        term_executions = agreement.setdefault("term_executions", {})
        if term_id in term_executions:
            raise ChatConflict("this agreement term already has a prepared or terminal execution")
        if any(record.get("status") == "manual_required" for record in term_executions.values()):
            raise ChatConflict("confirm or cancel the pending agreement term before preparing another")
        if agreement.get("status") == "manual_required" and agreement.get("block_reason") not in {
                None, "per_term_execution_required"}:
            raise ChatConflict("agreement execution is blocked")

        action_id = term.get("action_id")
        action = self.action_registry.get(action_id) if isinstance(action_id, str) else None
        required_participants = term.get("required_participants")
        confirmations = term.get("confirmed_by")
        confirmation_request_ids = term.get("confirmed_by_request_ids")
        if (term.get("status") != "agreed"
                or not isinstance(required_participants, list) or not required_participants
                or not isinstance(confirmations, list)
                or not set(required_participants).issubset(set(confirmations))
                or not isinstance(confirmation_request_ids, dict)
                or any(not confirmation_request_ids.get(tag) for tag in required_participants)):
            raise ChatConflict("each required signatory must explicitly confirm this exact term")
        if any(tag not in chat.get("participants", []) for tag in required_participants):
            raise ChatConflict("term signatories are not all participants in this chat")
        if chat["player_country"] not in required_participants:
            raise ChatConflict("only terms involving the player's country can be executed by this event adapter")
        if not action or action.get("supported_by_hoi4") is not True:
            raise ChatConflict("agreement term has no supported HOI4 action")
        if term.get("supported_by_hoi4") is not True:
            raise ChatConflict("agreement term is not marked as a supported HOI4 effect")
        if term.get("unparsed_execution_details"):
            raise ChatConflict("agreement term contains unresolved execution details")
        unsupported_details = _unenforced_effect_details(term, action_id)
        if unsupported_details:
            record = {
                "term_id": term_id, "action_id": action_id, "status": "blocked",
                "reason": "approved_effect_contains_unenforced_details:"
                         + ",".join(unsupported_details),
                "execution_request_id": None, "execution_package": None,
            }
            agreement["term_executions"][term_id] = record
            agreement["status"] = "blocked"
            agreement["block_reason"] = record["reason"]
            agreement["updated_at"] = _utc_now()
            chat["updated_at"] = agreement["updated_at"]
            response = deepcopy(agreement)
            response["prepared_term"] = deepcopy(record)
            prep_requests[request_id] = {"term_id": term_id, "response": deepcopy(response)}
            return response

        execution_request_id = (
            f"agreement:{agreement_id}:v{version_number}:term:{term_id}"
        )
        # Reuse the exact single-action validation and event builders by
        # presenting a temporary one-term view. The real agreement/version is
        # never rewritten by this preparation path.
        term_chat = deepcopy(chat)
        term_chat["participants"] = list(required_participants)
        term_chat["country_contexts"] = {
            tag: deepcopy(chat.get("country_contexts", {}).get(tag, {}))
            for tag in required_participants
        }
        term_agreement = deepcopy(agreement)
        term_agreement["action_id"] = action_id
        term_agreement["participants"] = list(required_participants)
        term_agreement["status"] = "approved"
        term_agreement["approved_version"] = version_number
        term_agreement["execution_package"] = None
        term_agreement["execution_request_id"] = None
        term_agreement["preparation_requests"] = {}
        # A term event may announce the full named treaty only when all the
        # other executable terms have already received their exact HOI4 result
        # markers. Earlier term events report only their individual effect.
        prior_terms_confirmed = all(
            item.get("term_id") == term_id
            or agreement.get("term_executions", {}).get(item.get("term_id"), {}).get("status")
                == "execution_confirmed"
            for item in version.get("terms", []) if isinstance(item, dict)
        )
        # A multi-term treaty is announced only by the last executable event,
        # after all the other terms have exact HOI4 result markers.
        term_agreement["_publish_effect_news"] = (
            len(version.get("terms", [])) <= 1 or prior_terms_confirmed
        )
        if len(version.get("terms", [])) > 1 and not prior_terms_confirmed:
            term_agreement["name"] = None
        term_chat["agreements"] = {agreement_id: term_agreement}
        term_version = term_agreement["versions"][version_number - 1]
        term_version["terms"] = [deepcopy(term)]

        temporary_engine = object.__new__(DiplomacyEngine)
        self._copy_save_context_to(temporary_engine)
        temporary_engine.chats = {chat_id: term_chat}
        temporary_engine.action_registry = self.action_registry
        temporary_engine.diplomatic_memory = deepcopy(self._memory_for_chat(chat))
        temporary_result = DiplomacyEngine.prepare_agreement_execution(
            temporary_engine, chat_id, agreement_id,
            request_id=f"term-prepare:{term_id}",
            _execution_request_id=execution_request_id,
        )
        package = deepcopy(temporary_result.get("execution_package"))
        if temporary_result.get("status") != "manual_required" or not isinstance(package, dict):
            record = {
                "term_id": term_id, "action_id": action_id, "status": "blocked",
                "reason": temporary_result.get("block_reason", "term_execution_not_preparable"),
                "execution_request_id": None, "execution_package": None,
            }
            agreement["term_executions"][term_id] = record
            agreement["status"] = "blocked"
            agreement["block_reason"] = record["reason"]
            agreement["updated_at"] = _utc_now()
            chat["updated_at"] = agreement["updated_at"]
            response = deepcopy(agreement)
            response["prepared_term"] = deepcopy(record)
            prep_requests[request_id] = {"term_id": term_id, "response": deepcopy(response)}
            return response

        deferred_terms = [
            item.get("term_id") for item in version.get("terms", [])
            if isinstance(item, dict) and item.get("term_id") != term_id
            and agreement.get("term_executions", {}).get(item.get("term_id"), {}).get("status")
                != "execution_confirmed"
            and item.get("supported_by_hoi4") is True
            and isinstance(item.get("action_id"), str)
            and (self.action_registry.get(item["action_id"]) or {}).get("supported_by_hoi4") is True
            and (self.action_registry.get(item["action_id"]) or {}).get("eyes_scripted_execution_supported", True) is True
        ]
        unenforced_terms = [
            item.get("term_id") for item in version.get("terms", [])
            if isinstance(item, dict) and item.get("term_id") != term_id
            and agreement.get("term_executions", {}).get(item.get("term_id"), {}).get("status")
                != "execution_confirmed"
            and (item.get("supported_by_hoi4") is not True
                 or not isinstance(item.get("action_id"), str)
                 or (self.action_registry.get(item["action_id"]) or {}).get("supported_by_hoi4") is not True
                 or (self.action_registry.get(item["action_id"]) or {}).get("eyes_scripted_execution_supported", True) is not True)
        ]
        package["deferred_executable_term_ids"] = deferred_terms
        package["unenforced_term_ids"] = unenforced_terms
        package["contract_execution_scope"] = {
            "agreement_version": version_number,
            "prepared_term_id": term_id,
            "deferred_executable_term_ids": deferred_terms,
            "unenforced_term_ids": unenforced_terms,
        }
        if isinstance(package.get("game_effect"), dict):
            package["game_effect"]["deferred_executable_term_ids"] = deferred_terms
            package["game_effect"]["unenforced_term_ids"] = unenforced_terms
        record = {
            "term_id": term_id, "action_id": action_id, "status": "manual_required",
            "participants": list(required_participants), "execution_request_id": execution_request_id,
            "execution_package": package, "execution_gate": deepcopy(temporary_result.get("execution_gate")),
            "prepared_at": _utc_now(), "execution_result": None,
        }
        agreement["term_executions"][term_id] = record
        agreement["status"] = "manual_required"
        agreement["block_reason"] = None
        agreement["execution_request_id"] = None
        agreement["execution_package"] = None
        agreement["execution_gate"] = None
        agreement["updated_at"] = _utc_now()
        chat["updated_at"] = agreement["updated_at"]
        response = deepcopy(agreement)
        response["prepared_term"] = deepcopy(record)
        prep_requests[request_id] = {"term_id": term_id, "response": deepcopy(response)}
        return response

    def pending_agreement_execution(self, request_id: str) -> Optional[Dict[str, Any]]:
        for chat in self.chats.values():
            for agreement in chat.get("agreements", {}).values():
                if agreement.get("execution_request_id") == request_id and agreement.get("status") == "manual_required":
                    return deepcopy(agreement)
                for term_id, term_execution in agreement.get("term_executions", {}).items():
                    if (term_execution.get("execution_request_id") == request_id
                            and term_execution.get("status") == "manual_required"):
                        return {
                            "chat_id": agreement.get("chat_id"),
                            "agreement_id": agreement.get("agreement_id"),
                            "campaign_id": agreement.get("campaign_id"),
                            "participants": deepcopy(term_execution.get("participants", [])),
                            "action_id": term_execution.get("action_id"),
                            "execution_request_id": request_id,
                            "execution_package": deepcopy(term_execution.get("execution_package")),
                            "term_id": term_id,
                            "agreement_version": agreement.get("current_version"),
                        }
        return None

    def record_agreement_execution_result(self, chat_id: str, agreement_id: str, *,
                                          request_id: str, status: str, marker: str,
                                          reason: Optional[str] = None) -> Dict[str, Any]:
        chat = self._chat(chat_id)
        agreement = self._agreement(chat, agreement_id)
        if status not in {"applied", "failed"}:
            raise ValueError("execution result status must be applied or failed")
        if agreement.get("execution_request_id") != request_id:
            term_executions = agreement.get("term_executions", {})
            matched = next((record for record in term_executions.values()
                            if record.get("execution_request_id") == request_id), None)
            if matched is None:
                raise ChatConflict("V2 marker request_id does not match a prepared agreement action")
            terminal_term = "execution_confirmed" if status == "applied" else "failed"
            previous = matched.get("execution_result")
            marker_key = "result_marker" if status == "applied" else "failure_marker"
            expected = (matched.get("execution_package") or {}).get(marker_key)
            if matched.get("status") == terminal_term and previous:
                if previous.get("marker") != marker or not matches_prepared_result(matched.get("execution_package"), status, marker):
                    raise ChatConflict("duplicate V2 marker does not match the confirmed agreement term")
                return {"recorded": True, "duplicate": True, "agreement_id": agreement_id,
                        "term_id": matched.get("term_id"), "request_id": request_id,
                        "status": agreement.get("status")}
            if matched.get("status") != "manual_required" or not expected:
                raise ChatConflict("agreement term has no pending generated game event")
            if not matches_prepared_result(matched.get("execution_package"), status, marker):
                raise ChatConflict("game log marker does not exactly match the prepared term V2 marker")
            matched["status"] = terminal_term
            matched["execution_result"] = {
                "source": "hoi4_game_log", "request_id": request_id,
                "status": terminal_term, "marker": marker, "reason": reason,
                "confirmed_at": _utc_now(),
            }
            if status == "failed":
                agreement["status"] = "failed"
                agreement["execution_result"] = deepcopy(matched["execution_result"])
            else:
                current_version = agreement["versions"][agreement["current_version"] - 1]
                executable_terms = [
                    term.get("term_id") for term in current_version.get("terms", [])
                    if isinstance(term, dict) and term.get("supported_by_hoi4") is True
                    and isinstance(term.get("term_id"), str)
                    and isinstance(term.get("action_id"), str)
                    and (self.action_registry.get(term["action_id"]) or {}).get("supported_by_hoi4") is True
                ]
                confirmed_terms = [term_id for term_id, record in term_executions.items()
                                   if record.get("status") == "execution_confirmed"]
                if set(executable_terms).issubset(set(confirmed_terms)):
                    agreement["status"] = "execution_confirmed"
                    if not agreement.get("signed_at"):
                        agreement["signed_at"] = _utc_now()
                    unenforced_terms = [
                        term.get("term_id") for term in current_version.get("terms", [])
                        if isinstance(term, dict) and term.get("term_id") not in executable_terms
                    ]
                    agreement["execution_result"] = {
                        "source": "hoi4_game_log", "status": "execution_confirmed",
                        "confirmed_term_ids": sorted(set(confirmed_terms)),
                        "unenforced_term_ids": unenforced_terms,
                        "last_request_id": request_id, "last_marker": marker,
                        "confirmed_at": _utc_now(),
                    }
                    _append_game_news(
                        chat,
                        unique_key=(f"agreement:{agreement_id}:v{agreement.get('current_version')}:signed"),
                        status="execution_confirmed", marker=marker,
                        participants=agreement.get("participants", []),
                        action_id=agreement.get("action_id", "unknown"),
                        source_request_id=request_id, agreement_id=agreement_id,
                        agreement_name=agreement.get("name"),
                        agreement_version=agreement.get("current_version"),
                        summary="All executable agreement terms were confirmed by exact HOI4 markers.",
                        diplomatic_memory=self._memory_for_chat(chat),
                    )
                    self._record_agreement_promise_trust(agreement, request_id)
                else:
                    agreement["status"] = "manual_required"
            agreement["updated_at"] = _utc_now()
            chat["updated_at"] = agreement["updated_at"]
            return {"recorded": True, "duplicate": False, "agreement_id": agreement_id,
                    "term_id": matched.get("term_id"), "request_id": request_id,
                    "status": agreement["status"]}

        terminal = "execution_confirmed" if status == "applied" else "failed"
        current = agreement.get("execution_result")
        if agreement.get("status") == terminal and current and current.get("request_id") == request_id:
            marker_key = "result_marker" if status == "applied" else "failure_marker"
            if (current.get("marker") != marker
                    or not matches_prepared_result(agreement.get("execution_package"), status, marker)):
                raise ChatConflict("duplicate game log marker does not match the confirmed result")
            return {"recorded": True, "duplicate": True, "agreement_id": agreement_id,
                    "request_id": request_id, "status": terminal}
        if agreement.get("status") != "manual_required" or not agreement.get("execution_package"):
            raise ChatConflict("agreement has no pending generated game event")
        marker_key = "result_marker" if status == "applied" else "failure_marker"
        expected = agreement["execution_package"].get(marker_key)
        if not matches_prepared_result(agreement.get("execution_package"), status, marker):
            raise ChatConflict("game log marker does not exactly match the prepared V2 marker")
        bundle_terms = agreement["execution_package"].get("bundle_term_ids")
        if bundle_terms and status == "failed":
            reason = marker.rsplit("|", 1)[-1]
        agreement["status"] = terminal
        if status == "applied":
            if not agreement.get("signed_at"):
                agreement["signed_at"] = _utc_now()
        agreement["execution_result"] = {
            "source": "hoi4_game_log", "request_id": request_id,
            "status": terminal, "marker": marker, "reason": reason,
            "confirmed_at": _utc_now(),
        }
        if bundle_terms:
            agreement["execution_result"]["confirmed_term_ids"] = list(bundle_terms) if status == "applied" else []
        if status == "applied":
            _append_game_news(
                chat,
                unique_key=(f"agreement:{agreement_id}:v{agreement.get('current_version')}:signed"),
                status="execution_confirmed", marker=marker,
                participants=agreement.get("participants", []),
                action_id=agreement.get("action_id", "unknown"), source_request_id=request_id,
                agreement_id=agreement_id, agreement_name=agreement.get("name"),
                agreement_version=agreement.get("current_version"),
                summary="HOI4 confirmed the approved agreement effect.",
                diplomatic_memory=self._memory_for_chat(chat),
            )
            self._record_agreement_promise_trust(agreement, request_id)
        agreement["updated_at"] = _utc_now()
        chat["updated_at"] = _utc_now()
        return {"recorded": True, "duplicate": False, "agreement_id": agreement_id,
                "request_id": request_id, "status": terminal}

    def _record_agreement_promise_trust(self, agreement: Mapping[str, Any], request_id: str) -> None:
        """Record one pairwise reliability event after the final applied marker only."""
        version = agreement.get("current_version")
        if isinstance(version, bool) or not isinstance(version, int):
            return
        remember_confirmed_application(
            self.diplomatic_memory,
            campaign_id=str(agreement.get("campaign_id") or ""),
            participants=agreement.get("participants"),
            agreement_id=str(agreement.get("agreement_id") or ""),
            agreement_version=version,
            action_id=str(agreement.get("action_id") or "unknown"),
            evidence_request_id=request_id,
        )

    def record_diplomatic_refusal_event(self, *, campaign_id: str, chat_id: str,
                                        refusal_request_id: str, marker: str) -> Dict[str, Any]:
        """Record one news event only from its exact campaign-scoped HOI4 marker."""
        if not _CAMPAIGN_ID_RE.fullmatch(str(campaign_id)):
            raise ValueError("campaign_id must be a lowercase UUID hex string")
        chat = self._chat(chat_id)
        if chat.get("campaign_id") != campaign_id:
            raise ChatConflict("refusal event marker belongs to a different campaign")
        message = next((item for item in chat.get("messages", [])
                        if item.get("request_id") == refusal_request_id), None)
        if message is None:
            raise KeyError("refusal request was not found in this chat")
        package = message.get("diplomatic_event_package")
        if (not isinstance(package, dict) or package.get("news_marker") != marker
                or package.get("campaign_id", campaign_id) != campaign_id
                or package.get("chat_id", chat_id) != chat_id):
            raise ChatConflict("game marker does not match this prepared diplomatic refusal event")
        if package.get("status") == "news_published":
            return {"recorded": True, "duplicate": True, "campaign_id": campaign_id,
                    "chat_id": chat_id, "request_id": refusal_request_id,
                    "status": "news_published"}
        if package.get("status") not in {"prepared", "manual_required"}:
            raise ChatConflict("diplomatic refusal event is not pending in-game publication")
        player, target, action_id = chat.get("player_country"), message.get("speaker"), package.get("action_id")
        if (not isinstance(player, str) or not isinstance(target, str)
                or not isinstance(action_id, str) or marker.split("|")[:2]
                != ["EYES_HOI4_DIPLOMATIC_REFUSAL_V1", campaign_id]):
            raise ChatConflict("refusal event provenance is incomplete")
        package["status"] = "news_published"
        package["published_at"] = _utc_now()
        _append_game_news(
            chat,
            unique_key=f"refusal:{campaign_id}:{chat_id}:{refusal_request_id}",
            status="decision_rejected", marker=marker,
            participants=[player, target], action_id=action_id,
            source_request_id=package.get("source_request_id"),
            agreement_name=message.get("proposed_agreement_name"),
            summary=message.get("text"),
        )
        chat["updated_at"] = _utc_now()
        return {"recorded": True, "duplicate": False, "campaign_id": campaign_id,
                "chat_id": chat_id, "request_id": refusal_request_id,
                "status": "news_published"}

    @staticmethod
    def _agreement(chat: Dict[str, Any], agreement_id: str) -> Dict[str, Any]:
        try:
            return chat.setdefault("agreements", {})[str(agreement_id)]
        except KeyError as error:
            raise KeyError("unknown agreement_id") from error

    def _independent_inputs(self, chat, speaker):
        contexts = chat.get('country_contexts', {})
        facts = deepcopy(contexts.get(speaker) or {})
        rows = chat.get('game_context_observations', {}).get('by_country', {}).get(speaker, [])
        native = next((r for r in reversed(rows) if r.get('campaign_id')==chat['campaign_id']
                       and r.get('country')==speaker
                       and str(r.get('source','')).startswith('bridge_parsed_hoi4_game_log_v')), None)
        for key in ('ideology','ideology_group','player_ideology_group','claims','_bridge_verified'):
            facts.pop(key,None)
        if native:
            facts.update(native.get('context', {}))
            if native.get('schema_version') in (8,9):facts['_bridge_verified']=True
        for key in ('war_status','same_faction','existing_non_aggression_pact'):
            values=[_normalize_context_value(key,row[key]) for row in contexts.values()
                    if isinstance(row,dict) and key in row]
            if len(set(values))>1:facts.pop(key,None)
        from strategic_interests import select_context
        return select_context(facts)[0]

    def _independent_vote(self, chat, speaker, point):
        from treaty_interest_gate import decision
        from diplomatic_planner import build_strategic_plan
        from diplomatic_doctrine import assess_doctrine
        from strategic_interests import refresh_interests
        facts = self._independent_inputs(chat,speaker)
        other = chat.get('player_country')
        action = self.action_registry.get(point.get('action_id')) or {}
        doctrine = assess_doctrine(speaker,facts,other,'',{'intent':action.get('intent')})
        memory = diplomatic_memory_pair_summary(self._memory_for_chat(chat),
            campaign_id=chat['campaign_id'],observer_tag=speaker,counterpart_tag=other)
        memory.pop('prior_negotiation_positions',None)
        interests = refresh_interests(deepcopy(self._memory_for_chat(chat)),chat['campaign_id'],speaker,facts)
        relationship = DiplomaticRelationship.from_context(facts)
        relationship.promise_trust = _number((memory.get('promise_trust') or {}).get('value'),0.5,0.0,1.0)
        plan = build_strategic_plan(intent=action.get('intent','unknown/general'),engine_decision='defer',
            relationship=relationship.to_dict(), game_context=facts,
            doctrine=doctrine,profile=doctrine.get('profile',{}),memory=memory,interests=interests,
            campaign_id=chat['campaign_id'],allow_override=True)
        return decision(point,facts,speaker,plan), facts

    def _check_independent_acceptance(self, chat, agreement):
        from agreement_manager import directional_parties
        inputs={}
        for term in agreement['versions'][agreement['current_version']-1]['terms']:
            actor,recipient=directional_parties(term)
            spec=(term.get('proposal_spec') or {}).get('action') or {}
            point={**spec,
                   'action_id':term.get('action_id'),'actors':[actor], 'recipient':recipient,
                   'conditions':term.get('conditions',spec.get('conditions')),
                   'exceptions':term.get('exceptions',spec.get('exceptions')),
                   'unresolved_fields': ['execution_details'] if term.get('unparsed_execution_details') else []}
            for speaker in chat.get('participants',[]):
                if speaker==chat.get('player_country'):continue
                if actor and recipient and speaker not in {actor,recipient}:continue
                vote,facts=self._independent_vote(chat,speaker,point)
                inputs[speaker]=facts
                if vote['decision']!='accept':
                    raise ChatConflict('independent_policy_requires_review:'+vote['decision']+':'+vote['reason']+':'+str(term.get('term_id')))
        return {'schema':1,'version':agreement['current_version'],'facts':inputs}

    def _revalidate_signed_policy(self, chat, agreement):
        # Old signatures are not recreated from today's advisory recommendation.
        # Existing source/signatory and native execution guards still apply.
        receipt=agreement.get('policy_validation')
        if receipt is None:
            # Keep historic consent, but never guess an issuer/beneficiary when
            # preparing a new native effect for a legacy directed obligation.
            from agreement_manager import directional_parties
            for term in agreement['versions'][agreement['current_version']-1]['terms']:
                if term.get('action_id') not in {'guarantee','military_access'}:
                    continue
                actor,recipient=directional_parties(term)
                if (not actor or actor==recipient
                        or {actor,recipient} != set(term.get('required_participants',[]))):
                    raise ChatConflict('independent_policy_requires_review:defer:insufficient_context:legacy_direction_requires_review')
            return
        if not isinstance(receipt,dict) or receipt.get('schema')!=1 or receipt.get('version')!=agreement.get('current_version'):
            raise ChatConflict('independent_policy_requires_review:stale_policy_receipt')
        self._check_independent_acceptance(chat,agreement)

    @staticmethod
    def _agreement_context(chat: Dict[str, Any], required: List[str]) -> tuple[dict, list]:
        contexts = chat.get("country_contexts", {})
        output, conflicts = {}, []
        for key in required:
            values = [context[key] for context in contexts.values()
                      if isinstance(context, dict) and key in context and context[key] is not None]
            normalized_values = [_normalize_context_value(key, value) for value in values]
            unknown = {"", "unknown", "unavailable", "none", "null", "n/a"}
            normalized = {value for value in normalized_values if value not in unknown}
            if len(normalized) > 1:
                conflicts.append(key)
            elif normalized:
                selected = next(value for value in normalized_values if value in normalized)
                output[key] = selected
        return output, conflicts

    def close_chat(self, chat_id: str) -> Dict[str, Any]:
        chat = self._chat(chat_id)
        from historical_conferences import Conferences
        conferences = Conferences(self)
        active = conferences.active(chat_id)
        if active:
            conferences.close(chat_id, active['conference_id'], reason='chat_closed')
        chat["state"] = "closed"
        chat["response_queue"] = []
        chat["updated_at"] = _utc_now()
        return self._public_chat(chat)

    def assess_chat_political_annexation(
        self, chat_id: str, *, operation_id: str, request_id: str,
        target_tag: str, approach: str, facts: Mapping[str, Any],
        evidence_sources: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Attach a sourced-but-unverified annexation estimate to an existing chat.

        The chat supplies campaign and initiator identity. This assessment is
        informational only and cannot create an agreement, action, or game effect.
        """
        chat = self._chat(chat_id)
        if chat["state"] == "closed":
            raise ChatConflict("chat is closed")
        if not isinstance(operation_id, str) or not re.fullmatch(r"[0-9a-f]{32}", operation_id):
            raise ValueError("operation_id must be a 32-character lowercase UUID hex string")
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
            raise ValueError("request_id must be a non-empty string of at most 200 characters")
        target = _country_tag(target_tag, "target_tag")
        if target == chat["player_country"] or target not in chat["participants"]:
            raise ValueError("target_tag must be a participating AI country in this chat")
        if not isinstance(approach, str) or approach not in {"voluntary_offer", "ultimatum"}:
            raise ValueError("approach must be voluntary_offer or ultimatum")
        if not isinstance(facts, Mapping) or not isinstance(evidence_sources, Mapping):
            raise ValueError("facts and evidence_sources must be JSON objects")

        fact_names = (
            "initiator_exists", "target_exists", "initiator_owned_states", "target_owned_states",
            "at_war", "conflicting_treaty", "initiator_military", "target_military",
            "initiator_factories", "target_factories", "relations", "ideology_compatible",
            "target_stability", "target_has_guarantee", "target_ally_count", "world_tension",
        )
        unknown = sorted((set(facts) | set(evidence_sources)) - set(fact_names))
        if unknown:
            raise ValueError("unknown fact fields: %s" % ", ".join(unknown))
        normalized_facts = {name: facts.get(name) for name in fact_names}
        if any(value is not None and not isinstance(value, (bool, int, float, str))
               for value in normalized_facts.values()):
            raise ValueError("fact values must be scalar JSON values or null")
        normalized_sources: Dict[str, str] = {}
        for name, source in evidence_sources.items():
            if not isinstance(source, str) or len(source) > 500:
                raise ValueError("evidence source labels must be strings of at most 500 characters")
            normalized_sources[name] = source.strip()

        fingerprint_payload = {
            "target_tag": target, "approach": approach,
            "facts": normalized_facts, "evidence_sources": normalized_sources,
        }
        fingerprint = hashlib.sha256(json.dumps(
            fingerprint_payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        stored = chat.setdefault("political_annexation_assessments", {
            "by_operation_id": {}, "request_index": {},
        })
        by_operation = stored.setdefault("by_operation_id", {})
        request_index = stored.setdefault("request_index", {})

        indexed_operation = request_index.get(request_id)
        if indexed_operation is not None:
            record = by_operation.get(indexed_operation)
            if indexed_operation != operation_id or not record or record["fingerprint"] != fingerprint:
                raise ChatConflict("request_id was already used for a different annexation assessment")
            return deepcopy(record["response"])

        record = by_operation.get(operation_id)
        if record is not None:
            if record["fingerprint"] != fingerprint:
                raise ChatConflict("operation_id already exists with different assessment inputs")
            record["request_ids"].append(request_id)
            request_index[request_id] = operation_id
            replay = deepcopy(record["response"])
            replay["request_id"] = request_id
            replay["idempotent_replay"] = True
            chat["updated_at"] = _utc_now()
            return replay

        context_values = dict(normalized_facts)
        context_values.update({
            "operation_id": operation_id,
            "initiator_tag": chat["player_country"],
            "target_tag": target,
            "approach": approach,
            "evidence_sources": normalized_sources,
        })
        assessment = assess_political_annexation(AnnexationContext(**context_values))
        response = {
            "chat_id": chat["chat_id"], "campaign_id": chat["campaign_id"],
            "conversation_id": chat["conversation_id"], "operation_id": operation_id,
            "request_id": request_id, "initiator_tag": chat["player_country"],
            "target_tag": target, "fact_verification": "caller_supplied_unverified",
            "assessment": assessment, "operation_started": False,
            "action_prepared": False, "game_effect_confirmed": False,
            "created_at": _utc_now(), "idempotent_replay": False,
        }
        by_operation[operation_id] = {
            "fingerprint": fingerprint, "request_ids": [request_id],
            "response": deepcopy(response),
        }
        request_index[request_id] = operation_id
        chat["updated_at"] = response["created_at"]
        return deepcopy(response)

    def list_chat_political_annexation_assessments(self, chat_id: str) -> Dict[str, Any]:
        chat = self._chat(chat_id)
        records = chat.get("political_annexation_assessments", {}).get("by_operation_id", {})
        return {
            "chat_id": chat["chat_id"], "campaign_id": chat["campaign_id"],
            "assessments": [deepcopy(record["response"]) for record in records.values()],
        }

    def assess_chat_computer_annexation_initiative(
        self, chat_id: str, *, request_id: str, target_tag: str,
        snapshot: Mapping[str, Any], facts: Mapping[str, Any],
        evidence_sources: Mapping[str, Any],
        snapshot_evidence_sources: Mapping[str, Any],
        approach: str = "voluntary_offer",
    ) -> Dict[str, Any]:
        """Persist a fail-closed AI initiative assessment for an existing chat.

        The current Bridge does not provide all initiative facts. All snapshot
        and assessment facts supplied through this API are therefore explicitly
        unverified. The policy result is exposed only as a preview; live
        eligibility remains blocked until a trusted game observation exists.
        """
        chat = self._chat(chat_id)
        if chat["state"] == "closed":
            raise ChatConflict("chat is closed")
        campaign_id = chat["campaign_id"]
        if not isinstance(request_id, str) or not re.fullmatch(
                rf"{re.escape(campaign_id)}:[0-9]{{4,}}", request_id):
            raise ValueError("request_id must be namespaced by this chat's campaign_id")
        target = _country_tag(target_tag, "target_tag")
        if not isinstance(snapshot, Mapping) or not isinstance(facts, Mapping):
            raise ValueError("snapshot and facts must be JSON objects")
        if not isinstance(evidence_sources, Mapping) or not isinstance(snapshot_evidence_sources, Mapping):
            raise ValueError("evidence_sources must be JSON objects")
        if approach != "voluntary_offer":
            raise ValueError("computer initiative assessments only support a voluntary offer")

        snapshot_fields = {
            "campaign_id", "country_tag", "campaign_day", "ai_controlled",
            "ideology", "historical_focuses_enabled",
        }
        unknown_snapshot = sorted(set(snapshot) - snapshot_fields)
        missing_snapshot = sorted(snapshot_fields - set(snapshot))
        if unknown_snapshot or missing_snapshot:
            raise ValueError("snapshot fields mismatch (missing=%s, unknown=%s)" % (
                ",".join(missing_snapshot), ",".join(unknown_snapshot),
            ))
        if snapshot.get("campaign_id") != campaign_id:
            raise ChatConflict("initiative snapshot campaign_id does not match this chat")
        initiator = _country_tag(snapshot.get("country_tag"), "snapshot.country_tag")
        if initiator == chat["player_country"] or initiator not in chat["participants"]:
            raise ValueError("initiator must be a computer-controlled participant, not the player")
        if target == chat["player_country"] or target not in chat["participants"] or target == initiator:
            raise ValueError("target must be a different computer-controlled participant in this chat")
        day = snapshot.get("campaign_day")
        if not isinstance(day, int) or isinstance(day, bool) or day < 1:
            raise ValueError("snapshot.campaign_day must be a positive integer")
        for key in ("ai_controlled", "historical_focuses_enabled"):
            if snapshot.get(key) is not None and not isinstance(snapshot.get(key), bool):
                raise ValueError("snapshot.%s must be a Boolean or null" % key)
        ideology = snapshot.get("ideology")
        if ideology is not None and (not isinstance(ideology, str) or len(ideology) > 80):
            raise ValueError("snapshot.ideology must be a short string or null")

        fact_names = (
            "initiator_exists", "target_exists", "initiator_owned_states", "target_owned_states",
            "at_war", "conflicting_treaty", "initiator_military", "target_military",
            "initiator_factories", "target_factories", "relations", "ideology_compatible",
            "target_stability", "target_has_guarantee", "target_ally_count", "world_tension",
        )
        unknown_facts = sorted((set(facts) | set(evidence_sources)) - set(fact_names))
        if unknown_facts:
            raise ValueError("unknown annexation fact fields: %s" % ", ".join(unknown_facts))
        for field_name, source in list(evidence_sources.items()) + list(snapshot_evidence_sources.items()):
            if not isinstance(field_name, str) or not isinstance(source, str) or len(source) > 500:
                raise ValueError("evidence source labels must be strings of at most 500 characters")
        normalized_facts = {name: facts.get(name) for name in fact_names}
        if any(value is not None and not isinstance(value, (bool, int, float, str))
               for value in normalized_facts.values()):
            raise ValueError("annexation fact values must be scalar JSON values or null")
        normalized_sources = {name: str(source).strip() for name, source in evidence_sources.items()}
        normalized_snapshot_sources = {
            name: str(source).strip() for name, source in snapshot_evidence_sources.items()
        }

        # The game-rule flag is campaign-global and comes only from a parsed
        # HOI4 log marker. Caller JSON and caller source labels never authorize
        # the computer initiative gate.
        integration_key = f"{campaign_id}:{chat['player_country']}"
        saved_snapshots = self.campaign_integration_observations[
            "snapshots_by_campaign_player"
        ].get(integration_key, [])
        latest_snapshot = self._latest_campaign_integration_snapshot(
            campaign_id, chat["player_country"], saved_snapshots,
        )
        saved_rules = latest_snapshot.get("game_rules") if isinstance(latest_snapshot, Mapping) else None
        saved_rule = None
        if (isinstance(saved_rules, Mapping)
                and isinstance(saved_rules.get("historical_focuses_enabled"), bool)
                and saved_rules.get("source") == "authenticated_bridge_hoi4_historical_focuses_v1"
                and saved_rules.get("request_id")
                == f"{campaign_id}:integration:{latest_snapshot.get('snapshot_seq')}:rules"):
            saved_rule = deepcopy(dict(saved_rules))

        normalized_snapshot_sources.pop("historical_focuses_enabled", None)
        if saved_rule is not None:
            normalized_snapshot_sources["historical_focuses_enabled"] = (
                f"{saved_rule['source']}:{saved_rule['request_id']}"
            )

        normalized_snapshot = {
            "campaign_id": campaign_id, "country_tag": initiator, "campaign_day": day,
            "ai_controlled": snapshot.get("ai_controlled"), "ideology": ideology,
            "historical_focuses_enabled": (
                saved_rule["historical_focuses_enabled"] if saved_rule is not None else None
            ),
        }
        fingerprint_payload = {
            "chat_id": chat_id, "snapshot": normalized_snapshot,
            "autonomous_initiatives_enabled": self.autonomous_initiatives_enabled,
            "snapshot_evidence_sources": normalized_snapshot_sources,
            "target_tag": target, "approach": approach,
            "facts": normalized_facts, "evidence_sources": normalized_sources,
        }
        fingerprint = hashlib.sha256(json.dumps(
            fingerprint_payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

        state = self.computer_annexation_initiatives
        by_evaluation = state["by_evaluation_id"]
        request_index = state["request_index"]
        scheduler = state["scheduler_by_country"]
        indexed_evaluation = request_index.get(request_id)
        if indexed_evaluation is not None:
            record = by_evaluation.get(indexed_evaluation)
            if not record or record["fingerprint"] != fingerprint:
                raise ChatConflict("request_id was already used for different initiative inputs")
            return deepcopy(record["response"])

        evaluation_key = "%s|%s|%s|%s" % (campaign_id, initiator, target, day)
        for evaluation_id, record in by_evaluation.items():
            if record.get("evaluation_key") == evaluation_key:
                if record["fingerprint"] != fingerprint:
                    raise ChatConflict("initiative evaluation already exists for this campaign day with different inputs")
                record["request_ids"].append(request_id)
                request_index[request_id] = evaluation_id
                replay = deepcopy(record["response"])
                replay["request_id"] = request_id
                replay["idempotent_replay"] = True
                return replay

        scheduler_key = "%s|%s" % (campaign_id, initiator)
        scheduler_record = scheduler.get(scheduler_key, {})
        last_checked_day = scheduler_record.get("last_checked_day")
        internal_sources = {
            "active_operation_count": "initiative-ledger:no-executable-operations-supported",
            "last_initiative_day": "initiative-ledger:no-confirmed-initiative",
        }
        if last_checked_day is not None:
            internal_sources["last_checked_day"] = "initiative-ledger:persisted-scheduler-state"
        initiative_snapshot = ComputerInitiativeSnapshot(
            campaign_id=campaign_id, country_tag=initiator, campaign_day=day,
            ai_controlled=normalized_snapshot["ai_controlled"],
            ideology=normalized_snapshot["ideology"],
            historical_focuses_enabled=normalized_snapshot["historical_focuses_enabled"],
            active_operation_count=int(scheduler_record.get("active_operation_count", 0)),
            last_initiative_day=scheduler_record.get("last_initiative_day"),
            last_checked_day=last_checked_day,
            evidence_sources={**normalized_snapshot_sources, **internal_sources},
        )
        operation_id = hashlib.sha256(("initiative|" + evaluation_key).encode("utf-8")).hexdigest()[:32]
        annexation_context = AnnexationContext(
            operation_id=operation_id, initiator_tag=initiator, target_tag=target,
            approach=approach, evidence_sources=normalized_sources, **normalized_facts,
        )
        result = assess_computer_annexation_initiative(
            initiative_snapshot, annexation_context,
            roll=self.initiative_random.random(), policy=ComputerInitiativePolicy(),
        )
        policy_preview = deepcopy(result)
        live_result = deepcopy(result)
        if normalized_snapshot["historical_focuses_enabled"] is True:
            live_result = {
                "status": "blocked", "reason": "historical_focuses_enabled",
                "eligible": False, "initiative_probability": None,
                "operation_started": False, "action_prepared": False,
                "game_effect_confirmed": False, "next_scheduler_state": None,
            }
        elif normalized_snapshot["historical_focuses_enabled"] is None:
            live_result = {
                "status": "blocked", "reason": "historical_focus_setting_unverified",
                "eligible": False, "initiative_probability": None,
                "operation_started": False, "action_prepared": False,
                "game_effect_confirmed": False, "next_scheduler_state": None,
            }
        elif not self.autonomous_initiatives_enabled:
            live_result = {
                "status": "blocked", "reason": "autonomous_initiatives_disabled",
                "eligible": False, "initiative_probability": None,
                "operation_started": False, "action_prepared": False,
                "game_effect_confirmed": False, "next_scheduler_state": None,
            }
        elif result.get("status") in {"candidate_for_review", "checked_no_initiative"}:
            # The API currently has no authenticated Bridge observation for these
            # country/operation scheduler gates. A caller-provided source label is not proof.
            live_result = {
                "status": "blocked",
                "reason": "trusted_hoi4_initiative_snapshot_unavailable",
                "eligible": False,
                "initiative_probability": None,
                "operation_started": False,
                "action_prepared": False,
                "game_effect_confirmed": False,
                "next_scheduler_state": None,
            }
        response = {
            "chat_id": chat_id, "campaign_id": campaign_id,
            "conversation_id": chat["conversation_id"], "request_id": request_id,
            "evaluation_id": result["evaluation_id"], "initiator_tag": initiator,
            "target_tag": target, "fact_verification": "caller_supplied_unverified",
            "autonomous_initiatives_enabled": self.autonomous_initiatives_enabled,
            "historical_focus_setting_verification": (
                {"status": "verified_by_authenticated_bridge",
                 "enabled": saved_rule["historical_focuses_enabled"],
                 "request_id": saved_rule["request_id"], "source": saved_rule["source"]}
                if saved_rule is not None else {"status": "unknown", "enabled": None}
            ),
            "initiative": live_result, "policy_preview": policy_preview,
            "policy_preview_is_non_executable": True,
            "policy_preview_notice": "Preview uses caller-supplied facts and is not a live-game eligibility decision.",
            "operation_started": False,
            "action_prepared": False, "game_effect_confirmed": False,
            "created_at": _utc_now(), "idempotent_replay": False,
        }
        evaluation_id = result["evaluation_id"]
        by_evaluation[evaluation_id] = {
            "evaluation_key": evaluation_key, "fingerprint": fingerprint,
            "request_ids": [request_id], "response": deepcopy(response),
        }
        request_index[request_id] = evaluation_id
        next_state = live_result.get("next_scheduler_state")
        if isinstance(next_state, dict) and isinstance(next_state.get("last_checked_day"), int):
            scheduler[scheduler_key] = {
                "last_checked_day": next_state["last_checked_day"],
                "active_operation_count": int(scheduler_record.get("active_operation_count", 0)),
                "last_initiative_day": scheduler_record.get("last_initiative_day"),
                "last_evaluation_id": evaluation_id,
            }
        return deepcopy(response)

    def list_chat_computer_annexation_initiatives(self, chat_id: str) -> Dict[str, Any]:
        chat = self._chat(chat_id)
        records = self.computer_annexation_initiatives["by_evaluation_id"]
        evaluations = [deepcopy(record["response"]) for record in records.values()
                       if record["response"].get("chat_id") == chat_id]
        return {"chat_id": chat_id, "campaign_id": chat["campaign_id"], "evaluations": evaluations}

    def get_autonomous_initiative_settings(self) -> Dict[str, Any]:
        """Return the opt-in and independently enforced HOI4 game-rule gate."""
        return {
            "autonomous_initiatives_enabled": self.autonomous_initiatives_enabled,
            "default_enabled": False,
            "setting_scope": "installation",
            "historical_ai_focuses_required": "off",
            "unknown_historical_ai_focuses_blocks": True,
            "automatic_negotiation_scheduler_available": False,
            "notice": (
                "This opt-in does not start negotiations or apply game effects. "
                "The current Bridge supports fail-closed initiative assessment only."
            ),
        }

    def set_autonomous_initiatives_enabled(self, enabled: Any) -> Dict[str, Any]:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a Boolean")
        self.autonomous_initiatives_enabled = enabled
        return self.get_autonomous_initiative_settings()

    def create_territory_proposal(self, chat_id: str, target: Mapping[str, Any],
                                  allocations: Mapping[str, Any], rationale: str = "") -> Dict[str, Any]:
        chat = self._chat(chat_id)
        if len(chat["participants"]) < 2:
            raise ChatConflict("at least one AI country must participate in a territory proposal")
        if chat["state"] != "awaiting_player" or not chat["messages"]:
            raise ChatConflict("a territory proposal can be opened after the player starts the chat and the current round finishes")
        normalized_target = _normalize_territory_target(target)
        normalized_allocations = _normalize_territory_allocations(
            allocations, normalized_target["state_ids"], chat["participants"]
        )
        proposal_id = uuid.uuid4().hex
        version = self._territory_version(
            1, chat["player_country"], normalized_target, normalized_allocations,
            rationale, chat["participants"],
        )
        proposal = {
            "proposal_id": proposal_id,
            "chat_id": chat_id,
            "campaign_id": chat["campaign_id"],
            "state": "NEGOTIATING",
            "current_version": 1,
            "versions": [version],
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "execution_assessment": self.action_registry.assess(
                "territory_partition", "accept", {"agreement_reached": False}
            ),
        }
        chat["territory_proposals"][proposal_id] = proposal
        chat["updated_at"] = _utc_now()
        return deepcopy(proposal)

    def revise_territory_proposal(self, chat_id: str, proposal_id: str,
                                  based_on_version: int, allocations: Mapping[str, Any],
                                  rationale: str = "") -> Dict[str, Any]:
        chat = self._chat(chat_id)
        proposal = self._territory_proposal(chat, proposal_id)
        if proposal.get("execution_package") or proposal["state"] in {
            "AGREED_PENDING_GAME_VALIDATION", "GAME_APPLIED", "GAME_ROLLED_BACK",
        }:
            raise ChatConflict("an execution-ready or executed proposal is immutable; create a new proposal to renegotiate")
        if based_on_version != proposal["current_version"]:
            raise ChatConflict("proposal version changed; refresh before submitting a revision")
        current = proposal["versions"][-1]
        normalized = _normalize_territory_allocations(
            allocations, current["target"]["state_ids"], chat["participants"]
        )
        version = self._territory_version(
            proposal["current_version"] + 1, chat["player_country"],
            current["target"], normalized, rationale, chat["participants"],
        )
        proposal["versions"].append(version)
        proposal["current_version"] = version["version"]
        proposal["state"] = "NEGOTIATING"
        proposal["execution_assessment"] = self.action_registry.assess(
            "territory_partition", "accept", {"agreement_reached": False}
        )
        proposal["updated_at"] = _utc_now()
        return deepcopy(proposal)

    def evaluate_territory_vote(self, chat_id: str, proposal_id: str,
                                country: Optional[str] = None) -> Dict[str, Any]:
        chat = self._chat(chat_id)
        if chat["state"] != "awaiting_player":
            raise ChatConflict("finish the current chat speaking order before evaluating a territory proposal")
        proposal = self._territory_proposal(chat, proposal_id)
        if proposal.get("execution_package") or proposal.get("state") in {"GAME_APPLIED", "GAME_ROLLED_BACK"}:
            raise ChatConflict("an execution-ready or executed proposal cannot be re-voted")
        version = proposal["versions"][-1]
        if version.get("participants") != chat["participants"]:
            raise ChatConflict("participant list changed; submit a new proposal version")
        ai_members = [tag for tag in chat["participants"] if tag != chat["player_country"]]
        if country is None:
            country = next((tag for tag in ai_members if tag not in version["votes"]), None)
        else:
            country = _country_tag(country, "country")
        if country not in ai_members:
            raise ValueError("country must be an AI participant in this chat")
        if country in version["votes"] and version["votes"][country]["decision"] != "defer":
            raise ChatConflict("this participant already voted on the current version")
        vote = self._evaluate_territory_country(chat, version, country)
        version["votes"][country] = vote
        message = {
            "speaker": country,
            "recipient": "ALL",
            "text": vote["response"],
            "timestamp": _utc_now(),
            "round": chat["round"],
            "kind": "territory_vote",
            "proposal_id": proposal_id,
            "proposal_version": version["version"],
            "decision": vote["decision"],
        }
        chat["messages"].append(message)
        self._refresh_territory_state(chat, proposal)
        proposal["updated_at"] = _utc_now()
        chat["updated_at"] = _utc_now()
        return deepcopy({"vote": vote, "message": message, "proposal": proposal})

    def adopt_territory_counteroffer(self, chat_id: str, proposal_id: str, country: str,
                                     based_on_version: int) -> Dict[str, Any]:
        chat = self._chat(chat_id)
        proposal = self._territory_proposal(chat, proposal_id)
        if proposal.get("execution_package") or proposal.get("state") in {"GAME_APPLIED", "GAME_ROLLED_BACK"}:
            raise ChatConflict("an execution-ready or executed proposal cannot adopt a counter-offer")
        if based_on_version != proposal["current_version"]:
            raise ChatConflict("proposal version changed; refresh before adopting a counter-offer")
        version = proposal["versions"][-1]
        if version.get("participants") != chat["participants"]:
            raise ChatConflict("participant list changed; submit a new proposal version")
        tag = _country_tag(country, "country")
        vote = version["votes"].get(tag)
        if not vote or vote["decision"] != "counter_offer" or not vote.get("counter_allocations"):
            raise ValueError("no structured counter-offer is available from this participant")
        new_version = self._territory_version(
            proposal["current_version"] + 1, tag, version["target"],
            vote["counter_allocations"], vote["reason"], chat["participants"],
        )
        proposal["versions"].append(new_version)
        proposal["current_version"] = new_version["version"]
        proposal["state"] = "NEGOTIATING"
        proposal["execution_assessment"] = self.action_registry.assess(
            "territory_partition", "accept", {"agreement_reached": False}
        )
        proposal["updated_at"] = _utc_now()
        return deepcopy(proposal)

    def cast_territory_player_vote(self, chat_id: str, proposal_id: str,
                                   decision: str) -> Dict[str, Any]:
        chat = self._chat(chat_id)
        proposal = self._territory_proposal(chat, proposal_id)
        if proposal.get("execution_package") or proposal.get("state") in {"GAME_APPLIED", "GAME_ROLLED_BACK"}:
            raise ChatConflict("an execution-ready or executed proposal cannot be re-voted")
        if decision not in {"accept", "reject"}:
            raise ValueError("player decision must be accept or reject; revise allocations to make a counter-offer")
        version = proposal["versions"][-1]
        if version.get("participants") != chat["participants"]:
            raise ChatConflict("participant list changed; submit a new proposal version")
        if decision == "accept" and any(
            version["votes"].get(tag, {}).get("decision") != "accept"
            for tag in chat["participants"] if tag != chat["player_country"]
        ):
            raise ChatConflict("the player can confirm only after every AI participant accepts this version")
        version["votes"][chat["player_country"]] = {
            "decision": decision,
            "reason": "player_confirmation" if decision == "accept" else "player_rejected",
            "context_source": "player",
        }
        self._refresh_territory_state(chat, proposal)
        proposal["updated_at"] = _utc_now()
        return deepcopy(proposal)

    def prepare_territory_execution(self, chat_id: str, proposal_id: str) -> Dict[str, Any]:
        """Prepare a guarded event artifact after agreement; never install or execute it."""
        chat = self._chat(chat_id)
        proposal = self._territory_proposal(chat, proposal_id)
        if proposal.get("state") != "AGREED_PENDING_GAME_VALIDATION":
            raise ChatConflict("all participants must approve the same proposal version first")
        registry_gate = self.action_registry.assess(
            "territory_partition", "accept", {"agreement_reached": True},
        )
        if registry_gate.get("status") != "MANUAL_REQUIRED":
            raise ChatConflict("Action Registry did not authorize a manual territory action")
        assessment = self._assess_territory_transfer_plan(chat, proposal)
        assessment["registry_assessment"] = registry_gate
        proposal["execution_assessment"] = assessment
        package = build_territory_event_package(chat, proposal, assessment)
        proposal["execution_package"] = deepcopy(package)
        proposal["updated_at"] = _utc_now()
        return deepcopy(package)

    def record_territory_execution_result(self, campaign_id: str, proposal_id: str,
                                          plan_id: str, status: str,
                                          summary: str) -> Dict[str, Any]:
        """Record one result marker only for the exact persisted manual event package."""
        if status not in {"applied", "failed", "rolled_back", "rollback_failed"}:
            raise ValueError("unsupported territory execution result")
        matches = [chat for chat in self.chats.values()
                   if chat.get("campaign_id") == campaign_id
                   and proposal_id in chat.get("territory_proposals", {})]
        if len(matches) != 1:
            raise KeyError("unknown territory execution plan")
        chat = matches[0]
        proposal = chat["territory_proposals"][proposal_id]
        package = proposal.get("execution_package")
        if (not isinstance(package, dict) or package.get("status") != "MANUAL_REQUIRED"
                or package.get("action_id") != "territory_partition"
                or package.get("plan_id") != plan_id
                or package.get("result_marker_prefix") !=
                f"EYES_HOI4_TERRITORY_RESULT_V1|{campaign_id}|{proposal_id}|{plan_id}|"):
            raise ChatConflict("result does not match a persisted manual execution package")
        transfers = package.get("transfers")
        if not isinstance(transfers, list) or not transfers:
            raise ChatConflict("stored territory package has no transfers")
        if status in {"applied", "failed"}:
            expected = ", ".join(
                f"{item['state_id']}: {item['expected_current_owner']} -> {item['new_owner']}"
                for item in sorted(transfers, key=lambda item: item["state_id"])
            )
            previous = proposal.get("execution_result")
            if status == "failed":
                allowed_failure_summaries = {
                    f"pre_apply_guard_failed|{expected}",
                    f"post_apply_owner_check_failed|{expected}",
                }
                if (previous is not None
                        and previous.get("status") == "failed"
                        and previous.get("summary") == summary
                        and summary in allowed_failure_summaries):
                    return {"recorded": True, "duplicate": True, "status": "failed",
                            "campaign_id": campaign_id, "proposal_id": proposal_id,
                            "plan_id": plan_id}
                if (proposal.get("state") != "AGREED_PENDING_GAME_VALIDATION"
                        or previous is not None or summary not in allowed_failure_summaries):
                    raise ChatConflict("failed territory result does not match the approved plan")
                proposal["execution_result"] = {
                    "status": "failed", "summary": summary, "plan_id": plan_id,
                    "recorded_at": _utc_now(),
                }
                proposal["state"] = "GAME_EXECUTION_FAILED"
                proposal["updated_at"] = _utc_now()
                return {"recorded": True, "duplicate": False, "status": "failed",
                        "campaign_id": campaign_id, "proposal_id": proposal_id,
                        "plan_id": plan_id}
            if previous is not None:
                if previous.get("status") == "applied" and previous.get("summary") == summary == expected:
                    return {"recorded": True, "duplicate": True, "status": "applied",
                            "campaign_id": campaign_id, "proposal_id": proposal_id,
                            "plan_id": plan_id}
                raise ChatConflict("territory execution already has a different result")
            if proposal.get("state") != "AGREED_PENDING_GAME_VALIDATION" or summary != expected:
                raise ChatConflict("applied result is inconsistent with the approved transfer plan")
            proposal["execution_result"] = {"status": "applied", "summary": summary,
                                            "plan_id": plan_id, "recorded_at": _utc_now()}
            proposal["state"] = "GAME_APPLIED"
        else:
            expected = ", ".join(
                f"{item['state_id']}: {item['new_owner']} -> {item['expected_current_owner']}"
                for item in sorted(transfers, key=lambda item: item["state_id"])
            )
            previous = proposal.get("execution_result")
            if status == "rollback_failed":
                allowed_failure_summaries = {
                    f"pre_rollback_guard_failed|{expected}",
                    f"post_rollback_owner_check_failed|{expected}",
                }
                prior_rollback = proposal.get("rollback_result")
                if (isinstance(previous, dict) and previous.get("status") == "applied"
                        and isinstance(prior_rollback, dict)
                        and prior_rollback.get("status") == "failed"
                        and prior_rollback.get("summary") == summary
                        and summary in allowed_failure_summaries):
                    return {"recorded": True, "duplicate": True, "status": "rollback_failed",
                            "campaign_id": campaign_id, "proposal_id": proposal_id,
                            "plan_id": plan_id}
                if (not isinstance(previous, dict) or previous.get("status") != "applied"
                        or previous.get("plan_id") != plan_id or prior_rollback is not None
                        or proposal.get("state") != "GAME_APPLIED"
                        or summary not in allowed_failure_summaries):
                    raise ChatConflict("failed rollback result does not match the applied plan")
                proposal["rollback_result"] = {
                    "status": "failed", "summary": summary, "plan_id": plan_id,
                    "recorded_at": _utc_now(),
                }
                proposal["state"] = "GAME_ROLLBACK_FAILED"
                proposal["updated_at"] = _utc_now()
                return {"recorded": True, "duplicate": False, "status": "rollback_failed",
                        "campaign_id": campaign_id, "proposal_id": proposal_id,
                        "plan_id": plan_id}
            if (not isinstance(previous, dict) or previous.get("status") != "applied"
                    or previous.get("plan_id") != plan_id or summary != expected):
                raise ChatConflict("rollback result requires the matching applied result and plan")
            if proposal.get("rollback_result") is not None:
                if proposal["rollback_result"].get("summary") == summary:
                    return {"recorded": True, "duplicate": True, "status": "rolled_back",
                            "campaign_id": campaign_id, "proposal_id": proposal_id,
                            "plan_id": plan_id}
                raise ChatConflict("territory rollback already has a different result")
            proposal["rollback_result"] = {"status": "rolled_back", "summary": summary,
                                            "plan_id": plan_id, "recorded_at": _utc_now()}
            proposal["state"] = "GAME_ROLLED_BACK"
        marker_status = "applied" if status == "applied" else "rolled_back"
        confirmation_marker = package["result_marker_prefix"] + marker_status + "|" + summary
        _append_game_news(
            chat,
            unique_key=f"territory:{proposal_id}:{plan_id}:{status}",
            status=("territory_transfer_confirmed" if status == "applied"
                    else "territory_rollback_confirmed"),
            marker=confirmation_marker,
            participants=proposal.get("participants", chat.get("participants", [])),
            action_id="territory_partition",
            source_request_id=None, source_plan_id=plan_id,
            proposal_id=proposal_id, summary=summary,
        )
        proposal["updated_at"] = _utc_now()
        return {"recorded": True, "duplicate": False, "status": status,
                "campaign_id": campaign_id, "proposal_id": proposal_id, "plan_id": plan_id}

    @staticmethod
    def _territory_version(number: int, created_by: str, target: Dict[str, Any],
                           allocations: Dict[str, List[int]], rationale: str,
                           participants: List[str]) -> Dict[str, Any]:
        return {
            "version": number,
            "created_by": created_by,
            "participants": list(participants),
            "created_at": _utc_now(),
            "target": deepcopy(target),
            "allocations": deepcopy(allocations),
            "rationale": str(rationale or "").strip()[:1000],
            "votes": {},
        }

    def _evaluate_territory_country(self, chat: Dict[str, Any], version: Dict[str, Any],
                                    country: str) -> Dict[str, Any]:
        context = chat["country_contexts"].get(country, {})
        target_ids = set(version["target"]["state_ids"])
        required = (
            "war_status", "current_state_owners", "claims", "interested_states", "allies",
            "military_balance", "relations", "threat", "prior_agreements",
        )
        missing = [field for field in required if field not in context or context[field] is None or
                   (isinstance(context[field], str) and context[field].lower() == "unknown")]
        at_war = _as_bool(context.get("war_status"))
        if at_war is None and "war_status" not in missing:
            missing.append("war_status")
        owners = context.get("current_state_owners")
        if isinstance(owners, Mapping) and any(
            str(state_id) not in owners and state_id not in owners for state_id in target_ids
        ):
            missing.append("current_state_owners_for_all_target_states")
        elif isinstance(owners, Mapping):
            for state_id in target_ids:
                owner = owners.get(str(state_id), owners.get(state_id))
                if not isinstance(owner, str) or not _COUNTRY_TAG_RE.fullmatch(owner.strip().upper()):
                    missing.append("known_owner_for_all_target_states")
                    break
                if owner.strip().upper() not in chat["participants"]:
                    missing.append("all_current_state_owners_in_chat")
                    break
        if at_war is True:
            war_participants = context.get("war_participants")
            if not isinstance(war_participants, list):
                missing.append("war_participants")
            else:
                normalized_war_participants = [_country_tag(tag, "war_participants") for tag in war_participants]
                if any(tag not in chat["participants"] for tag in normalized_war_participants):
                    missing.append("all_war_participants_in_chat")
        if not isinstance(context.get("prior_agreements"), list):
            missing.append("prior_agreements_must_be_a_list")
        if missing:
            reason = "required_territory_context_unknown: " + ", ".join(dict.fromkeys(missing))
            template = f"{country} cannot assess this partition yet; verified context is missing: {', '.join(dict.fromkeys(missing))}."
            return self._territory_vote_text(country, "defer", reason, template)

        try:
            balance = float(context["military_balance"])
        except (TypeError, ValueError):
            balance = float("nan")
        if balance != balance or not -1.0 <= balance <= 1.0:
            return self._territory_vote_text(
                country, "defer", "military_balance_unknown_or_invalid",
                f"{country} defers until its current military position is supplied as a value from -1 to 1.",
            )
        try:
            relations = float(context["relations"])
            threat = float(context["threat"])
        except (TypeError, ValueError):
            relations, threat = float("nan"), float("nan")
        if relations != relations or not -100.0 <= relations <= 100.0:
            return self._territory_vote_text(
                country, "defer", "bilateral_relations_unknown_or_invalid",
                f"{country} defers until bilateral relations with the proposer are known.",
            )
        if threat != threat or not 0.0 <= threat <= 1.0:
            return self._territory_vote_text(
                country, "defer", "threat_level_unknown_or_invalid",
                f"{country} defers until its current threat assessment is known.",
            )
        if threat >= 0.85 or relations <= -80.0:
            reason = "security_guarantees_required_for_hostile_or_high_threat_context"
            template = f"{country} defers because the supplied threat and bilateral relationship context require security guarantees before borders can be agreed."
            return self._territory_vote_text(country, "defer", reason, template)
        allocation = version["allocations"]
        own_claims = set(_validated_state_id_list(context["claims"], "claims"))
        own_interests = set(_validated_state_id_list(context["interested_states"], "interested_states"))
        red_lines = set(_validated_state_id_list(context.get("red_line_states", []), "red_line_states"))
        blocked = [state_id for state_id in red_lines & target_ids if country not in allocation_owner(allocation, state_id)]
        if blocked:
            reason = f"red_line_states_not_assigned: {sorted(blocked)}"
            template = f"{country} rejects this version because it assigns states {', '.join(map(str, sorted(blocked)))} against explicit national red lines."
            return self._territory_vote_text(country, "reject", reason, template)

        desired = own_claims | own_interests
        proposed = deepcopy(allocation)
        moved = []
        for state_id in sorted(desired & target_ids):
            current_owner = allocation_owner(proposed, state_id)
            if current_owner != country:
                proposed[current_owner].remove(state_id)
                proposed[country].append(state_id)
                moved.append(state_id)

        for agreement in context["prior_agreements"]:
            if not isinstance(agreement, Mapping):
                return self._territory_vote_text(
                    country, "defer", "prior_agreements_invalid",
                    f"{country} defers because a prior territorial agreement is not structured enough to verify.",
                )
            if str(agreement.get("status", "active")).lower() not in {"active", "confirmed"}:
                continue
            try:
                state_id = int(agreement.get("state_id"))
            except (TypeError, ValueError):
                return self._territory_vote_text(
                    country, "defer", "prior_agreements_invalid",
                    f"{country} defers because a prior territorial agreement has no valid state ID.",
                )
            beneficiary = _country_tag(agreement.get("beneficiary"), "prior_agreements.beneficiary")
            if state_id not in target_ids or allocation_owner(proposed, state_id) == beneficiary:
                continue
            if beneficiary not in chat["participants"]:
                reason = f"prior_agreement_beneficiary_not_in_chat: {beneficiary}:{state_id}"
                template = f"{country} defers because a previous agreement concerns state {state_id}; invite {beneficiary} before changing that allocation."
                return self._territory_vote_text(country, "defer", reason, template)
            current_owner = allocation_owner(proposed, state_id)
            proposed[current_owner].remove(state_id)
            proposed[beneficiary].append(state_id)
            moved.append(state_id)

        ally_claims = context.get("ally_claims", {})
        if not isinstance(ally_claims, Mapping):
            ally_claims = {}
        for ally in context.get("allies", []):
            ally = _country_tag(ally, "allies")
            if ally not in ally_claims:
                reason = f"ally_claims_unknown_for_{ally}"
                template = f"{country} defers until the territorial interests of ally {ally} are documented."
                return self._territory_vote_text(country, "defer", reason, template)
            if ally not in chat["participants"]:
                allied_states = set(_validated_state_id_list(ally_claims[ally], f"ally_claims.{ally}"))
                disputed = [state_id for state_id in sorted(allied_states & target_ids)
                            if allocation_owner(proposed, state_id) != ally]
                if disputed:
                    reason = f"external_ally_claim_requires_participation: {ally}:{disputed}"
                    template = f"{country} defers because the proposal assigns states claimed by external ally {ally}; invite that country before revising these borders."
                    return self._territory_vote_text(country, "defer", reason, template)
                continue
            for state_id in _validated_state_id_list(ally_claims[ally], f"ally_claims.{ally}"):
                if state_id in target_ids:
                    current_owner = allocation_owner(proposed, state_id)
                    if current_owner != ally:
                        proposed[current_owner].remove(state_id)
                        proposed[ally].append(state_id)
                        moved.append(state_id)

        if moved:
            reason = f"national_or_allied_interests_require_reallocation: {sorted(set(moved))}"
            template = f"{country} proposes a revision assigning states {', '.join(map(str, sorted(set(moved))))} in line with the documented claims and allied interests."
            vote = self._territory_vote_text(country, "counter_offer", reason, template)
            vote["counter_allocations"] = {tag: sorted(states) for tag, states in proposed.items()}
            vote["factors"] = [
                "claims_checked", "allied_interests_checked", "prior_agreements_checked",
                f"military_balance={balance:+.2f}", f"relations={relations:+.1f}", f"threat={threat:.2f}",
            ]
            return vote

        reason = "documented_context_aligns_with_current_allocation"
        template = f"{country} accepts this version based on the supplied claims, allied interests, military balance, and current ownership context."
        vote = self._territory_vote_text(country, "accept", reason, template)
        vote["factors"] = [
            "claims_checked", "allied_interests_checked", "prior_agreements_checked",
            f"military_balance={balance:+.2f}", f"relations={relations:+.1f}", f"threat={threat:.2f}",
        ]
        return vote

    def _territory_vote_text(self, country: str, decision: str, reason: str,
                             template: str) -> Dict[str, Any]:
        response = template
        if self.response_text_provider is not None:
            try:
                generated = self.response_text_provider.generate("territory_partition", decision, template)
                if isinstance(generated, str) and generated.strip():
                    response = generated.strip()
            except Exception:
                _PROVIDER_LOGGER.info("provider_result provider=%s status=fallback reason=provider_exception",
                                      getattr(self.response_text_provider, "diagnostic_name", "custom"))
        return {
            "country": country,
            "decision": decision,
            "reason": reason,
            "response": response,
            "context_source": "caller_provided; not live-verified",
        }

    def _refresh_territory_state(self, chat: Dict[str, Any], proposal: Dict[str, Any]) -> None:
        version = proposal["versions"][-1]
        votes = version["votes"]
        ai_members = [tag for tag in chat["participants"] if tag != chat["player_country"]]
        decisions = [votes[tag]["decision"] for tag in ai_members if tag in votes]
        if "reject" in decisions or votes.get(chat["player_country"], {}).get("decision") == "reject":
            proposal["state"] = "REJECTED_CURRENT_VERSION"
        elif len(decisions) == len(ai_members) and all(item == "accept" for item in decisions):
            if votes.get(chat["player_country"], {}).get("decision") == "accept":
                proposal["state"] = "AGREED_PENDING_GAME_VALIDATION"
                proposal["execution_assessment"] = self._assess_territory_transfer_plan(chat, proposal)
            else:
                proposal["state"] = "AWAITING_PLAYER_CONFIRMATION"
        elif "defer" in decisions:
            proposal["state"] = "NEEDS_CONTEXT"
        else:
            proposal["state"] = "NEGOTIATING"

    @staticmethod
    def _assess_territory_transfer_plan(chat: Dict[str, Any], proposal: Dict[str, Any]) -> Dict[str, Any]:
        """Build a non-executable transfer plan only from unanimous, explicit context."""
        version = proposal["versions"][-1]
        participants = list(chat["participants"])
        if version.get("participants") != participants or any(
            version.get("votes", {}).get(tag, {}).get("decision") != "accept"
            for tag in participants
        ):
            return {
                "action_id": "territory_partition", "status": "BLOCKED",
                "reason": "all_participants_must_accept_the_same_current_version",
            }

        state_ids = list(version["target"]["state_ids"])
        allocation = version["allocations"]
        if set(allocation) != set(participants):
            return {
                "action_id": "territory_partition", "status": "BLOCKED",
                "reason": "allocation_participants_do_not_match_chat",
            }
        assigned = [state_id for tag in participants for state_id in allocation[tag]]
        if sorted(assigned) != sorted(state_ids) or len(assigned) != len(set(assigned)):
            return {
                "action_id": "territory_partition", "status": "BLOCKED",
                "reason": "every_target_state_must_have_exactly_one_agreed_owner",
            }

        owner_snapshots = []
        for tag in participants:
            context = chat["country_contexts"].get(tag, {})
            war_status = _as_bool(context.get("war_status"))
            if war_status is True:
                return {
                    "action_id": "territory_partition", "status": "BLOCKED",
                    "reason": f"active_war_for_participant:{tag}",
                }
            if war_status is not False:
                return {
                    "action_id": "territory_partition", "status": "MANUAL_REQUIRED",
                    "reason": f"war_status_unknown_for_participant:{tag}",
                }
            raw_owners = context.get("current_state_owners")
            if not isinstance(raw_owners, Mapping):
                return {
                    "action_id": "territory_partition", "status": "MANUAL_REQUIRED",
                    "reason": f"current_state_owners_unknown_for_participant:{tag}",
                }
            normalized = {}
            for state_id in state_ids:
                owner = raw_owners.get(str(state_id), raw_owners.get(state_id))
                if not isinstance(owner, str) or not _COUNTRY_TAG_RE.fullmatch(owner.strip().upper()):
                    return {
                        "action_id": "territory_partition", "status": "MANUAL_REQUIRED",
                        "reason": f"current_owner_unknown_for_state:{state_id}",
                    }
                owner = owner.strip().upper()
                if owner not in participants:
                    return {
                        "action_id": "territory_partition", "status": "BLOCKED",
                        "reason": f"current_owner_not_a_participant:{state_id}:{owner}",
                    }
                normalized[state_id] = owner
            owner_snapshots.append(normalized)

        if any(snapshot != owner_snapshots[0] for snapshot in owner_snapshots[1:]):
            return {
                "action_id": "territory_partition", "status": "MANUAL_REQUIRED",
                "reason": "participants_report_conflicting_current_owners",
            }

        current_owners = owner_snapshots[0]
        transfers = []
        for state_id in sorted(state_ids):
            new_owner = next(tag for tag in participants if state_id in allocation[tag])
            old_owner = current_owners[state_id]
            if old_owner != new_owner:
                transfers.append({
                    "state_id": state_id,
                    "expected_current_owner": old_owner,
                    "new_owner": new_owner,
                    "candidate_effect": f"transfer_state_to = {new_owner}",
                })
        if not transfers:
            return {
                "action_id": "territory_partition", "status": "NO_ACTION_REQUIRED",
                "reason": "agreed_allocation_already_matches_reported_owners",
                "live_verified": False,
            }
        return {
            "action_id": "territory_partition",
            "status": "MANUAL_REQUIRED",
            "reason": "transfer_plan_prepared_but_live_owner_recheck_and_game_event_are_not_implemented",
            "live_verified": False,
            "context_source": "caller_provided; not live-verified",
            "manual_confirmation_required": True,
            "requires_live_owner_recheck": True,
            "reported_current_owners": {str(state_id): current_owners[state_id]
                                         for state_id in sorted(state_ids)},
            "transfers": transfers,
            "execution_command": None,
        }

    def _chat(self, chat_id: str) -> Dict[str, Any]:
        try:
            return self.chats[str(chat_id)]
        except KeyError as error:
            raise KeyError("unknown chat_id") from error

    @staticmethod
    def _public_chat(chat: Dict[str, Any]) -> Dict[str, Any]:
        from diplomatic_inbox import unread
        result = deepcopy(chat)
        result.setdefault("agreements", {})
        result.pop("processed_request_ids", None)
        result.pop("retry_contexts", None)
        result.pop("retry_attempts", None)
        result['unread_count'] = len(unread(chat))
        result["next_request_id"] = (
            f"{result['campaign_id']}:chat:{result['chat_id']}:{result['request_sequence']:06d}"
        )
        result.pop("request_sequence", None)
        return result

    def _cached_chat_request(self, chat: Dict[str, Any], request_id: str) -> Optional[Dict[str, Any]]:
        if not isinstance(request_id, str) or not request_id.strip():
            raise ValueError("request_id is required for an idempotent chat operation")
        if request_id in chat["processed_request_ids"]:
            replay = deepcopy(chat["processed_request_ids"][request_id])
            # Expose that this is a cached replay, without mutating the stored
            # message or invoking the provider again.
            replay["idempotent_replay"] = True
            replay["replayed_request_id"] = request_id
            return replay
        expected = self._public_chat(chat)["next_request_id"]
        if request_id != expected:
            raise ChatConflict(f"request_id does not match the next request slot: expected {expected}")
        return None

    @staticmethod
    def _territory_proposal(chat: Dict[str, Any], proposal_id: str) -> Dict[str, Any]:
        try:
            return chat["territory_proposals"][str(proposal_id)]
        except KeyError as error:
            raise KeyError("unknown proposal_id") from error

    @staticmethod
    def _group_provider_fallback_text(country_name: str, language: str) -> str:
        language = normalize_language(language) or "en"
        text = {
            "en": "Our position remains open on these points; no agreement is confirmed.",
            "fr": "Notre position sur ces points reste ouverte ; aucun accord n’est confirmé.",
            "ru": "Наша позиция по этим пунктам остаётся открытой; согласие не подтверждено.",
            "uk": "Наша позиція щодо цих пунктів залишається відкритою; згоду не підтверджено.",
            "de": "Unsere Position zu diesen Punkten bleibt offen; eine Zustimmung ist nicht bestätigt.",
            "es": "Nuestra posición sobre estos puntos sigue abierta; no se ha confirmado ningún acuerdo.",
            "pt": "A nossa posição sobre estes pontos continua em aberto; nenhum acordo foi confirmado.",
            "pl": "Nasze stanowisko w tych punktach pozostaje otwarte; zgody nie potwierdzono.",
            "ja": "これらの項目に関する立場は未確定であり、合意は確認されていません。",
            "ko": "이 항목들에 대한 입장은 아직 열려 있으며 합의가 확인되지 않았습니다.",
            "zh": "这些条款的立场仍未确定，尚未确认任何协议。",
        }
        return text.get(language, text["en"])

    @staticmethod
    def _information_unavailable_text(country_name: str, language: str) -> str:
        language = normalize_language(language) or "en"
        texts = {
            "en": "A reliable substantive answer could not be prepared this time; no agreement or new term was recorded.",
            "fr": "Aucune réponse de fond fiable n’a pu être préparée cette fois ; aucun accord ni nouvelle condition n’a été enregistré.",
            "ru": "В этот раз не удалось подготовить надёжный содержательный ответ; соглашений и новых условий не зафиксировано.",
            "uk": "Цього разу не вдалося підготувати надійну змістовну відповідь; угод і нових умов не зафіксовано.",
        }
        # Open questions are not constrained to four interface languages; keep
        # the safe fallback understandable without inventing a country stance.
        return texts.get(language, texts["en"])

    @staticmethod
    def _refusal_followup_fallback_text(speaker_name: str, counterpart_name: str,
                                       requirements: Mapping[str, Any], language: str) -> str:
        """Answer a refusal inquiry from stored facts without manufacturing a vote."""
        language = normalize_language(language) or "en"
        requirements = requirements if isinstance(requirements, Mapping) else {}
        record_available = requirements.get("refusal_record_available") is True
        actor = str(requirements.get("refusal_actor_name") or speaker_name)
        target = str(requirements.get("refusal_target_name") or counterpart_name)
        refusal_text = re.sub(r"\s+", " ", str(requirements.get("source_refusal_text") or "")).strip()
        facts = requirements.get("verified_game_facts", {})
        facts = facts if isinstance(facts, Mapping) else {}
        fact_items = []
        relations = facts.get("relations")
        if isinstance(relations, (int, float)) and not isinstance(relations, bool):
            fact_items.append(f"the latest recorded relation value was {relations:g}")
        war = facts.get("war_status")
        if war in {"yes", "no"}:
            fact_items.append("the latest recorded context showed an active war" if war == "yes"
                              else "the latest recorded context showed no active war")
        same_faction = facts.get("same_faction")
        if same_faction in {"yes", "no"}:
            fact_items.append("the countries were recorded in the same faction" if same_faction == "yes"
                              else "the countries were recorded outside the same faction")
        if language == "ru":
            if not record_available:
                opening = "Доступные сведения не подтверждают проверяемого отказа по конкретному предложению, поэтому я не могу достоверно назвать отказавшую сторону или причину."
            elif requirements.get("reason_recorded_in_transcript") is True:
                opening = f"Переписка подтверждает, что сторона {actor} отклонила предложение стороны {target}; в записи об отказе указано: «{refusal_text[:360]}»."
            else:
                opening = f"Переписка подтверждает отказ стороны {actor} от предложения стороны {target}. Причина в записи не указана, поэтому фактический мотив неизвестен."
            context_line = (" Последний зафиксированный игровой контекст содержит " + "; ".join(fact_items)
                            + ". Эти сведения могут подсказать темы для обсуждения безопасности и доверия, но не доказывают причину отказа.") if fact_items else " Доступные проверенные сведения не устанавливают мотив отказа."
            return (opening + context_line + " Возможной необязательной альтернативой для обсуждения могли бы стать регулярные двусторонние консультации по безопасности и взаимное предварительное уведомление о значимых военных мерах. "
                    "Это только идея для разговора, а не встречное предложение или согласованный пункт договора.")
        if language == "fr":
            if not record_available:
                opening = "Les éléments disponibles ne confirment pas de refus vérifiable lié à une proposition précise; je ne peux donc pas en attribuer l'auteur ou la raison."
            elif requirements.get("reason_recorded_in_transcript") is True:
                opening = f"La conversation confirme que la partie {actor} a refusé la proposition de {target}; le refus indiquait : « {refusal_text[:360]} »."
            else:
                opening = f"La conversation confirme le refus par {actor} de la proposition de {target}. Aucune raison n'a été consignée; le motif réel reste inconnu."
            context_line = (" Le dernier contexte de jeu enregistré indique " + "; ".join(fact_items)
                            + ". Ces éléments peuvent orienter une discussion sur la sécurité et la confiance, mais ne prouvent pas le motif du refus.") if fact_items else " Les données vérifiées disponibles n'établissent pas le motif du refus."
            return (opening + context_line + " Une alternative non contraignante à discuter pourrait être des consultations bilatérales régulières sur la sécurité, accompagnées d'un préavis réciproque pour les principales mesures militaires. "
                    "Ce n'est qu'une idée de discussion, pas une contre-proposition ni une clause acceptée.")
        if language == "uk":
            if not record_available:
                opening = "Доступні відомості не підтверджують перевіреної відмови щодо конкретної пропозиції, тому я не можу достовірно назвати сторону, що відмовила, або причину."
            elif requirements.get("reason_recorded_in_transcript") is True:
                opening = f"Переписка підтверджує відмову сторони {actor} від пропозиції сторони {target}; у записі зазначено: «{refusal_text[:360]}»."
            else:
                opening = f"Переписка підтверджує відмову сторони {actor} від пропозиції сторони {target}. Причину в записі не зазначено, тому фактичний мотив невідомий."
            context_line = (" Останній зафіксований ігровий контекст містить " + "; ".join(fact_items)
                            + ". Це може підказати теми безпеки й довіри для обговорення, але не доводить причину відмови.") if fact_items else " Доступні перевірені дані не встановлюють мотив відмови."
            return (opening + context_line + " Можливою необов'язковою альтернативою для обговорення могли б стати регулярні двосторонні консультації з безпеки та взаємне попереднє повідомлення про важливі військові заходи. "
                    "Це лише ідея для розмови, а не зустрічна пропозиція чи погоджений пункт угоди.")
        if not record_available:
            opening = "The available record does not confirm a verifiable refusal tied to a specific proposal, so I cannot reliably attribute or explain one."
        elif requirements.get("reason_recorded_in_transcript") is True:
            opening = f"The record confirms that {actor} declined {target}'s proposal; the refusal stated: “{refusal_text[:360]}”"
        else:
            opening = f"The record confirms that {actor} declined {target}'s proposal. The refusal stated no reason, so its actual motive is unknown from the record."
        context_line = (" The latest recorded game context states " + "; ".join(fact_items)
                        + ". These facts may inform a discussion about security and trust, but they do not prove the motive for refusal.") if fact_items else " The available verified facts do not establish the motive for refusal."
        return (opening + context_line + " A possible nonbinding alternative for discussion is regular bilateral security consultations with reciprocal advance notice of major military measures. "
                "This is only an idea, not a counteroffer or agreed treaty term.")

    @staticmethod
    def _group_decision_from_positions(positions: list[dict]) -> str:
        """Derive a country turn label from validated positions, never from an engine guess."""
        party_rows = [
            row for row in positions
            if isinstance(row, dict) and row.get("participant_role") == "party"
        ]
        if not party_rows:
            return "defer"
        stances = [row.get("stance") for row in party_rows]
        if all(stance == "accept" for stance in stances):
            return "accept"
        if any(stance == "counter_offer" for stance in stances):
            return "counter_offer"
        if all(stance == "reject" for stance in stances):
            return "reject"
        return "defer"

    @staticmethod
    def _unresolved_group_positions(analysis, previous_positions, response_request_id, language):
        """Replace a deduplicated response's stale assent with source-linked unresolved positions."""
        actions = analysis.get("actions", []) if isinstance(analysis, Mapping) else []
        if not isinstance(actions, list):
            return []
        old_by_action = {
            item.get("action_id"): item for item in previous_positions
            if isinstance(item, Mapping) and isinstance(item.get("action_id"), str)
        } if isinstance(previous_positions, list) else {}
        rows = []
        for index, action in enumerate(actions):
            if not isinstance(action, Mapping) or not isinstance(action.get("action_id"), str):
                continue
            old = old_by_action.get(action.get("action_id"), {})
            rows.append({
                "point": index,
                "action_id": action["action_id"],
                "source_excerpt": action.get("source_excerpt", ""),
                "source_request_id": action.get("source_request_id")
                or analysis.get("source_request_id"),
                "response_request_id": response_request_id,
                "participant_role": old.get("participant_role", "unresolved"),
                "party_tags": list(old.get("party_tags", [])),
                "stance": "defer",
                "text": DiplomacyEngine._group_provider_fallback_text("", language),
                "origin": "engine_deduplication",
                "validation_reason": "repeated_reply_not_reused_as_new_consent",
                "consent_status": "unconfirmed",
                "authority": "advisory_only",
            })
        return rows

    @staticmethod
    def _latest_explicit_offer_refusal(messages, refusing_tag: str, proposer_tag: str,
                                       classifier: IntentClassifier):
        """Return the latest rejection of an actual offer, not an informational answer.

        Older V5 turns can contain ``decision=reject`` on an information request
        because a small model once interpreted the word "refusal" as a new vote.
        Such a reply is not evidence that the other country rejected the offer.
        Require a proposal-classified player message immediately before the
        rejecting country response and preserve that exact source pair.
        """
        proposal_intents = {
            "offer", "negotiate", "demand", "alliance", "non_aggression", "trade",
            "peace", "ceasefire", "military_access", "guarantee", "embargo",
            "lift_embargo", "counter_offer", "docking_rights", "air_base_access",
            "faction_join", "faction_leave", "market_access", "territory_partition", "truce",
            "white_peace", "peace_conference",
        }

        def field(item, name, default=None):
            if isinstance(item, Mapping):
                return item.get(name, default)
            return getattr(item, name, default)

        rows = list(messages) if isinstance(messages, (list, tuple)) else []
        for index in range(len(rows) - 1, 0, -1):
            refusal = rows[index]
            source = rows[index - 1]
            refusal_speaker = str(field(refusal, "speaker", field(refusal, "sender", ""))).upper()
            refusal_recipient = str(field(
                refusal, "recipient", field(refusal, "receiver", proposer_tag),
            )).upper()
            decision = str(field(refusal, "decision", "")).lower()
            refusal_intent = str(field(refusal, "intent", "")).lower()
            refusal_text = field(refusal, "text", "")
            refusal_kind = field(refusal, "kind")
            if refusal_kind is not None and refusal_kind != "country_response":
                continue
            if (refusal_speaker != refusing_tag or refusal_recipient != proposer_tag
                    or decision not in {"reject", "rejected"}
                    or refusal_intent not in proposal_intents
                    or not isinstance(refusal_text, str) or not refusal_text.strip()):
                continue

            source_speaker = str(field(source, "speaker", field(source, "sender", ""))).upper()
            source_recipient = str(field(
                source, "recipient", field(source, "receiver", refusing_tag),
            )).upper()
            source_kind = field(source, "kind")
            source_text = field(source, "text", "")
            source_intent = str(field(source, "intent", "") or "").lower()
            if not source_intent and isinstance(source_text, str):
                source_intent = classifier.classify(source_text)
            if (source_kind is not None and source_kind != "player_message"):
                continue
            if (source_speaker != proposer_tag or source_recipient != refusing_tag
                    or source_intent not in proposal_intents
                    or not isinstance(source_text, str) or not source_text.strip()):
                continue
            return ({
                "speaker": refusal_speaker, "recipient": refusal_recipient,
                "request_id": field(refusal, "request_id"),
                "intent": refusal_intent, "decision": decision,
                "text": refusal_text.strip(),
            }, {
                "speaker": source_speaker, "recipient": source_recipient,
                "request_id": field(source, "request_id"),
                "intent": source_intent, "text": source_text.strip(),
            })
        return None, None

    def _store_point_policy(self, chat, key, points):
        plans = chat.setdefault("engine_point_policies", {})
        version_bindings = chat.setdefault("engine_point_policy_versions", {})
        current_versions = {
            agreement_id: {"version": agreement.get("current_version"),
                           "content_hash": next((version.get("content_hash") for version in agreement.get("versions", [])
                                                 if version.get("version") == agreement.get("current_version")), None)}
            for agreement_id, agreement in chat.get("agreements", {}).items()
        }
        if key in plans:
            # Retry uses the original immutable policy, never a fresh vote.
            old = plans[key]
            if version_bindings.get(key, current_versions) != current_versions:
                raise ChatConflict("agreement version changed since the original decision plan")
            if [p.get("scope_fingerprint") for p in old] != [p.get("scope_fingerprint") for p in points]:
                raise ChatConflict("point scope changed since the original generation")
            return deepcopy(old)
        plans[key] = deepcopy(points)
        arguments=chat.setdefault('argument_memory',[])
        if not any(r.get('request_id')==key for r in arguments):
            arguments.append({'request_id':key,'positions':[{'action_id':p.get('action_id'),'point':p.get('point'),'stance':p.get('stance'),'stage':p.get('conversation_stage'),'open_questions':deepcopy(p.get('open_questions',[])),'next_step':p.get('next_step'),'basis':deepcopy(p.get('expression_basis',{}))} for p in points]})
        chat['argument_memory']=arguments[-64:]
        version_bindings[key] = deepcopy(current_versions)
        if chat.get('chat_id') and self._memory_for_chat(chat).get('historical_diplomacy', {}).get('conferences'):
            from historical_conferences import Conferences
            for country in dict.fromkeys(p.get('speaker_tag') for p in points if p.get('speaker_tag')):
                Conferences(self).review_plan(chat['chat_id'], country, key,
                                             [p for p in points if p.get('speaker_tag') == country])
        persist = getattr(self, "_before_expression_persist", None)
        if callable(persist):
            persist()
        return deepcopy(points)

    def respond(self, request: Mapping[str, Any]) -> Dict[str, Any]:
        payload = dict(request)
        game_state = payload.get("game_context") or payload.get("context") or payload.get("game_state") or {}
        if not isinstance(game_state, Mapping):
            raise ValueError("game_context/context/game_state must be an object")
        context = dict(game_state)
        for key in ("relations", "trust", "threat", "opinion", "war_status", "at_war",
                    "alliance_status", "recent_conflict", "economic_dependence", "military_balance",
                    "diplomatic_history", "same_faction", "faction_status", "date"):
            if key in payload and key not in context:
                context[key] = payload[key]

        sender = str(payload.get("sender") or payload.get("player_country") or context.get("player_country_tag") or "PLAYER").strip().upper()
        receiver = str(payload.get("receiver") or payload.get("ai_country") or context.get("ai_country_tag") or "AI").strip().upper()
        text = payload.get("message", payload.get("player_message", ""))
        if not isinstance(text, str) or not text.strip():
            raise ValueError("message must be a non-empty string")
        text = text.strip()
        if len(text) > MAX_MESSAGE_LENGTH:
            raise ValueError("message exceeds maximum length")

        conversation_id = str(payload.get("conversation_id") or "").strip() or str(uuid.uuid4())
        conversation = self._conversation(conversation_id, payload.get("previous_messages", []), sender, receiver)
        prior_language = conversation.response_language
        language = payload.get("response_language")
        if language not in SUPPORTED_LANGUAGES:
            language = detect_language(text, prior_language)
        if language not in SUPPORTED_LANGUAGES:
            language = prior_language if prior_language in SUPPORTED_LANGUAGES else "en"
        conversation.response_language = language
        # The local model renders the approved English seed in any newly
        # supported HOI4 locale. Keep decision logic independent of locale.
        template_language = language if language in {"en", "fr", "ru", "uk"} else "en"
        group_pair_key = f"{receiver}:{sender}" if payload.get("group_chat") is True else None
        if group_pair_key is not None:
            relationship = conversation.group_relationships.get(group_pair_key)
            if relationship is None:
                relationship = DiplomaticRelationship.from_context(context)
                conversation.group_relationships[group_pair_key] = relationship
            negotiation = conversation.group_negotiations.get(group_pair_key)
            if negotiation is None:
                negotiation = NegotiationState(
                    conversation_id=conversation_id,
                    sender=sender,
                    receiver=receiver,
                )
                conversation.group_negotiations[group_pair_key] = negotiation
        else:
            if conversation.relationship is None:
                conversation.relationship = DiplomaticRelationship.from_context(context)
            relationship = conversation.relationship
            if conversation.negotiation_state is None:
                conversation.negotiation_state = NegotiationState(
                    conversation_id=conversation_id,
                    sender=sender,
                    receiver=receiver,
                )
            negotiation = conversation.negotiation_state
        trusted_memory = None
        memory_candidate = payload.get("diplomatic_memory")
        if (payload.get("group_chat") is True
                and payload.get("_verified_memory_capability") is self._verified_memory_capability
                and isinstance(memory_candidate, Mapping)
                and memory_candidate.get("campaign_id") == payload.get("campaign_id")
                and str(memory_candidate.get("observer_tag") or "").upper() == receiver
                and str(memory_candidate.get("counterpart_tag") or "").upper() == sender
                and memory_candidate.get("authority")
                == "persistent_campaign_scoped_negotiation_memory"):
            trusted_memory = deepcopy(dict(memory_candidate))
            trust_summary = trusted_memory.get("promise_trust")
            if isinstance(trust_summary, Mapping):
                relationship.promise_trust = _number(trust_summary.get("value"), 0.5, 0.0, 1.0)
        trust_before = relationship.trust
        intent = self.classifier.classify(text)
        communication_analysis = analyze_communication_acts(text)
        refusal_record, refusal_offer_record = (None, None)
        if payload.get("group_chat") is True:
            refusal_record, refusal_offer_record = self._latest_explicit_offer_refusal(
                payload.get("recent_messages", []), receiver, sender, self.classifier,
            )
        if refusal_record is None:
            refusal_record, refusal_offer_record = self._latest_explicit_offer_refusal(
                conversation.messages, receiver, sender, self.classifier,
            )
        prior_refusal_recorded = refusal_record is not None
        prior_refusal_mentioned = bool(_PRIOR_REFUSAL_REFERENCE_RE.search(text))
        requests_refusal_followup = any(
            act in {"request_explanation", "request_alternative"}
            for act in communication_analysis.get("acts", [])
        )
        refusal_followup = bool(
            communication_analysis.get("contract_actionable") is False
            and (
                "acknowledge_refusal" in communication_analysis.get("acts", [])
                or ((prior_refusal_recorded or prior_refusal_mentioned)
                    and requests_refusal_followup)
            )
        )
        if refusal_followup:
            # Acknowledging an earlier rejection and asking about reasons or
            # alternatives is ordinary discussion, never a new vote on the
            # closed offer. Keep the transcript for Ollama, but remove the old
            # proposal from this turn's decision and draft-update path.
            intent = "information"
        # Keep an explicit national-aims question informational even if a
        # secondary action phrase (for example, “what terms would you offer”)
        # happens to match the proposal vocabulary. This is a discussion turn,
        # not a treaty offer, vote, or action request.
        national_position_question = self.classifier.is_national_position_request(text)
        if national_position_question:
            intent = "information"
        agreement_review = payload.get("agreement_confirmation_review")
        if not isinstance(agreement_review, Mapping) or not agreement_review.get("terms"):
            agreement_review = None
        doctrine_context = dict(context)
        doctrine_provenance = payload.get("game_context_provenance")
        doctrine_is_verified = bool(
            payload.get("group_chat") is True
            and payload.get("_verified_observation_capability") is self._verified_observation_capability
            and isinstance(doctrine_provenance, Mapping)
            and doctrine_provenance.get("schema_version") in {8, 9}
            and doctrine_provenance.get("source") == (
                f"bridge_parsed_hoi4_game_log_v{doctrine_provenance.get('schema_version')}"
            )
            and isinstance(doctrine_provenance.get("request_id"), str)
            and doctrine_provenance["request_id"].startswith(f"{payload.get('campaign_id')}:pair-doctrine:")
        )
        # Do not allow request/context JSON to self-assert Bridge provenance
        # or inject an unvalidated personality profile.
        doctrine_context.pop("_bridge_verified", None)
        doctrine_context.pop("diplomatic_profile", None)
        if doctrine_is_verified:
            doctrine_context["_bridge_verified"] = True
        else:
            # Editable chat context and player prose cannot establish a country's
            # ideology, major-power status or claims. Only a Bridge game-log observation
            # may activate those inputs in a live response.
            for field_name in ("ideology", "ideology_group", "player_ideology_group",
                               "major_power", "player_major_power", "claims"):
                doctrine_context.pop(field_name, None)
        doctrine = assess_doctrine(
            receiver, doctrine_context, sender, text, {**payload, "intent": intent},
        )
        # Only Bridge-attested game context may supply ideology inputs that
        # adjust the neutral negotiation prior. A country tag itself is not
        # evidence of that state's interests.
        profile = self._profile(receiver, doctrine_context, doctrine)
        country_name = self._localized_country_name(
            receiver, str(context.get("ai_country_name") or self._country_name(receiver)), template_language
        )
        sender_name = self._localized_country_name(
            sender, str(context.get("player_country_name") or self._country_name(sender)), template_language
        )
        participant_country_names = payload.get("participant_country_names", {})
        participant_country_names = (dict(participant_country_names)
                                     if isinstance(participant_country_names, Mapping) else {})
        participant_country_names[sender] = sender_name
        participant_country_names[receiver] = country_name
        participant_aliases = dict(participant_country_names)
        prior_offer = self._latest_unresolved_offer(conversation, sender)
        if refusal_followup and prior_offer is None and payload.get("group_chat") is True:
            # The V5 chat owns the durable transcript; its per-chat engine may
            # be empty after a process restart. Recover the last player-authored
            # proposal from that transcript for explanation context only. The
            # closed proposal is deliberately not passed to _decide as a live vote.
            proposal_intents = {
                "offer", "negotiate", "demand", "alliance", "non_aggression", "trade",
                "peace", "ceasefire", "military_access", "guarantee", "embargo",
                "lift_embargo", "counter_offer", "docking_rights", "air_base_access",
                "faction_join", "faction_leave", "market_access", "territory_partition", "truce",
                "white_peace", "peace_conference",
            }
            for old in reversed(payload.get("recent_messages", [])):
                if (not isinstance(old, Mapping)
                        or str(old.get("speaker") or "").upper() != sender
                        or old.get("kind") != "player_message"):
                    continue
                old_text = old.get("text")
                old_intent = str(old.get("intent") or self.classifier.classify(old_text or ""))
                if old_intent not in proposal_intents or not isinstance(old_text, str):
                    continue
                prior_offer = DiplomaticMessage(
                    sender=sender, receiver=receiver,
                    timestamp=str(old.get("timestamp") or ""),
                    turn=int(old.get("round", 0) or 0), text=old_text,
                    intent=old_intent, decision=old.get("decision"),
                    diplomatic_significance="persisted_v5_closed_offer_context",
                )
                break
        decision_prior_offer = None if refusal_followup else prior_offer
        previous_country_response = next((message for message in reversed(conversation.messages)
                                          if message.sender == receiver and message.receiver == sender), None)
        previous_country_response_record = None
        if payload.get("group_chat") is True:
            for old in reversed(payload.get("recent_messages", [])):
                if not isinstance(old, Mapping):
                    continue
                if (str(old.get("speaker", "")).upper() == receiver
                        and str(old.get("recipient", "")).upper() == sender
                        and old.get("kind") == "country_response"
                        and isinstance(old.get("text"), str)):
                    previous_country_response = DiplomaticMessage(
                        sender=receiver, receiver=sender,
                        timestamp=str(old.get("timestamp", "")),
                        turn=int(old.get("round", 0) or 0), text=old["text"],
                        intent=str(old.get("intent", "unknown/general")),
                        decision=old.get("decision"),
                        diplomatic_significance="persisted_group_chat_reply",
                    )
                    previous_country_response_record = old
                    break

        if refusal_followup:
            # Do not treat a historical informational reply mislabeled as
            # decision=reject (for example, V5 request 000006) as the refusal
            # being discussed. Bind the explanation to the latest refusal of
            # an actual proposal and its immediately preceding source offer.
            if refusal_record is not None:
                previous_country_response_record = refusal_record
                previous_country_response = DiplomaticMessage(
                    sender=receiver, receiver=sender,
                    timestamp=str(refusal_record.get("timestamp") or ""),
                    turn=int(refusal_record.get("round", 0) or 0),
                    text=str(refusal_record.get("text") or ""),
                    intent=str(refusal_record.get("intent") or "unknown/general"),
                    decision=refusal_record.get("decision"),
                    diplomatic_significance="verified_offer_refusal_context",
                )
                if refusal_offer_record is not None:
                    prior_offer = DiplomaticMessage(
                        sender=sender, receiver=receiver,
                        timestamp=str(refusal_offer_record.get("timestamp") or ""),
                        turn=int(refusal_offer_record.get("round", 0) or 0),
                        text=str(refusal_offer_record.get("text") or ""),
                        intent=str(refusal_offer_record.get("intent") or "unknown/general"),
                        decision=refusal_offer_record.get("decision"),
                        diplomatic_significance="source_offer_for_recorded_refusal",
                    )

        agenda_requirements = (
            self._discussion_topic_requirements(text, template_language, country_name)
            if intent == "information" else None
        )
        prior_terms = self._extract_prior_discussion_terms(
            previous_country_response.text if previous_country_response else "",
            template_language,
            previous_country_response_record.get("request_id")
            if isinstance(previous_country_response_record, Mapping) else None,
        )
        prior_terms_comparison = bool(
            previous_country_response is not None
            and isinstance(previous_country_response_record, Mapping)
            and previous_country_response_record.get("provider_status") == "generated"
            and self.classifier.is_prior_terms_comparison(text)
            and prior_terms
        )
        prior_terms_requirements = ({
            "request_type": "prior_terms_comparison",
            "candidates": prior_terms,
            "source_reply_request_id": previous_country_response_record.get("request_id"),
            "authority": "nonbinding_preference_only",
        } if prior_terms_comparison else None)
        national_position_requirements = (
            self._national_position_requirements(text, template_language, country_name, sender_name)
            if (intent == "information" and national_position_question
                and not prior_terms_comparison) else None
        )
        observation_provenance = payload.get("game_context_provenance")
        observation_sources = {
            5: "bridge_parsed_hoi4_game_log_v5",
            6: "bridge_parsed_hoi4_game_log_v6",
            7: "bridge_parsed_hoi4_game_log_v7",
            8: "bridge_parsed_hoi4_game_log_v8",
            9: "bridge_parsed_hoi4_game_log_v9",
        }
        internally_verified_observation = bool(
            payload.get("group_chat") is True
            and payload.get("_verified_observation_capability") is self._verified_observation_capability
            and isinstance(observation_provenance, Mapping)
            and observation_sources.get(observation_provenance.get("schema_version"))
                == observation_provenance.get("source")
            and isinstance(observation_provenance.get("request_id"), str)
            and isinstance(observation_provenance.get("observed_at"), str)
            and bool(context)
        )
        snapshot_requirements = (
            self._verified_snapshot_requirements(text, template_language, context)
            if intent == "information" and internally_verified_observation else None
        )
        decision, factors, confidence = self._decide(
            intent, text, relationship, profile, conversation, decision_prior_offer,
            doctrine=doctrine,
        )
        if refusal_followup:
            decision = "informational_reply"
            factors = list(dict.fromkeys(
                factors + ["refusal_followup_is_nonbinding"]
                + (["clarification_of_existing_offer"] if prior_offer else [])
                + [f"communication_act_{act}" for act in communication_analysis.get("acts", [])]
            ))
        if (agenda_requirements or snapshot_requirements or national_position_requirements) and not agreement_review:
            # A fully specified request for a country's proposed discussion
            # topic is an answer request, not a request for missing information
            # and not a diplomatic vote.
            decision = "informational_reply"
            factor = (
                "direct_snapshot_question" if snapshot_requirements else
                "direct_national_position_question" if national_position_requirements else
                "direct_agenda_question"
            )
            factors = list(dict.fromkeys(factors + [factor]))
        if agreement_review:
            # A request to summarize recorded positions is informational. It
            # must not turn old conditions into a new counter-offer or assent.
            decision = "defer"
            factors = list(dict.fromkeys(factors + ["explicit_agreement_term_review"]))
        participant_tags = set()
        if isinstance(participant_country_names, Mapping):
            participant_tags.update(str(tag).upper() for tag in participant_country_names)
        participant_tags.update((sender, receiver))
        active_terms = payload.get("active_agreement_action_participants", {})
        active_terms_exist = isinstance(active_terms, Mapping) and bool(active_terms)
        strategic_override_allowed = bool(
            intent in {
                "offer", "negotiate", "demand", "alliance", "non_aggression", "trade",
                "military_access", "guarantee", "embargo", "lift_embargo", "docking_rights",
                "air_base_access", "faction_join", "faction_leave", "market_access", "territory_partition",
                "political_annexation", "truce", "white_peace", "peace", "ceasefire",
            }
            and not prior_offer
            and not agreement_review
            and not refusal_followup
            and not active_terms_exist
            and len(participant_tags) <= 2
        )
        strategic_interests = refresh_interests(
            self.diplomatic_memory, str(payload.get("campaign_id") or ""), receiver, context,
        )
        strategic_plan = build_strategic_plan(
            intent=intent,
            engine_decision=decision,
            relationship=relationship.to_dict(),
            game_context=context,
            doctrine=doctrine,
            profile=asdict(profile),
            memory=trusted_memory,
            campaign_id=str(payload.get("campaign_id") or ""),
            conversation_id=conversation_id,
            request_id=str(payload.get("request_id") or ""),
            allow_override=strategic_override_allowed,
            interests=strategic_interests,
        )
        if strategic_plan.get("applied_to_engine_decision") is True:
            decision = str(strategic_plan["selected_action"])
            factors = list(dict.fromkeys(
                factors + [f"strategic_planner_selected_{decision}"]
            ))
        strategic_plan["counterpart_context"] = counterpart_brief(
            receiver, str(payload.get("player_country_tag") or sender).upper(), context, doctrine)
        action = self.action_registry.for_intent(intent)
        action_result = None
        if action is not None:
            action_result = self.action_registry.assess(action["action_id"], decision, context)
        refusal_fallback_requirements = None
        if refusal_followup:
            refusal_text = str((refusal_record or {}).get("text") or "")
            refusal_fallback_requirements = {
                "refusal_record_available": refusal_record is not None,
                "refusal_actor_name": (
                    participant_aliases.get(str(refusal_record.get("speaker") or "").upper())
                    or country_name
                ) if refusal_record else None,
                "refusal_target_name": (
                    participant_aliases.get(str(refusal_record.get("recipient") or "").upper())
                    or sender_name
                ) if refusal_record else None,
                "source_refusal_text": refusal_text,
                "reason_recorded_in_transcript": self._has_recorded_refusal_reason(refusal_text),
                "requested_communication_acts": list(communication_analysis.get("acts", [])),
                "verified_game_facts": {},
            }
        template_response = (
            self._refusal_followup_fallback_text(
                country_name, sender_name, refusal_fallback_requirements, template_language,
            )
            if refusal_followup else self._compose_response(
                intent, decision, country_name, sender_name, text, prior_offer, profile, relationship,
                language=template_language, previous_country_response=previous_country_response,
            )
        )
        if agreement_review:
            template_response = self._compose_agreement_confirmation_review(
                country_name, agreement_review, template_language,
            )
        response_text = template_response
        if national_position_requirements and self.response_text_provider is None:
            response_text = self._national_position_unavailable_text(template_language)
        elif agenda_requirements and self.response_text_provider is None:
            response_text = self._information_unavailable_text(country_name, template_language)
        provider_diagnostic = None
        message_analysis = None
        dialogue_positions = None
        dialogue_position_status = None
        if snapshot_requirements and internally_verified_observation and not agreement_review:
            response_text = self._compose_verified_snapshot_summary(
                context, template_language, sender, receiver,
            )
            source_request_id = next((item.get("request_id")
                                      for item in reversed(payload.get("recent_messages", []))
                                      if isinstance(item, Mapping)
                                      and str(item.get("speaker", "")).upper() == sender), None)
            message_analysis = {
                "source_request_id": source_request_id,
                "actions": [], "relationships": [], "unresolved_clauses": [],
                "clarification_targets": [], "completeness": "complete",
                "interpretation_mode": "verified_snapshot_summary",
                "authority": "advisory_only",
            }
            dialogue_positions = []
            dialogue_position_status = "not_applicable"
            provider_diagnostic = {
                "provider": "engine", "status": "not_required",
                "reason": "verified_snapshot_summary",
                "source_request_id": observation_provenance.get("request_id"),
            }
            _PROVIDER_LOGGER.info(
                "dialogue_reply status=verified_context_summary fields=%d request_id=%s",
                len(snapshot_requirements.get("requested_answer_fields", [])),
                payload.get("request_id", "unknown"),
            )
        elif agreement_review and self.response_text_provider is not None:
            provider_name = getattr(self.response_text_provider, "diagnostic_name", "custom")
            if language not in {"en", "fr", "ru", "uk"}:
                # For locales without built-in review wording, Ollama only
                # renders the already-composed, source-linked summary. It does
                # not decide or change any term status.
                contextual_generate = getattr(self.response_text_provider, "generate_dialogue", None)
                if callable(contextual_generate):
                    try:
                        generated_text = contextual_generate(
                            "information", "defer", template_response,
                            dialogue_context={
                                "speaking_country": country_name,
                                "speaking_country_tag": receiver,
                                "other_country": sender_name,
                                "other_country_tag": sender,
                                "request_id": payload.get("request_id"),
                                "recent_messages": payload.get("recent_messages", []),
                                "known_game_context": {},
                                "approved_response_points": [template_response, dict(agreement_review)],
                                "response_requirements": {
                                    "render_existing_agreement_review_in_target_language": True,
                                    "preserve_each_term_status_and_source_request_id": True,
                                    "do_not_change_agreement_or_execution_status": True,
                                },
                            },
                            language=language,
                        )
                    except Exception:
                        _PROVIDER_LOGGER.exception(
                            "provider_result provider=%s status=fallback reason=agreement_localization_error",
                            provider_name,
                        )
                        generated_text = None
                    if isinstance(generated_text, str) and generated_text.strip():
                        response_text = generated_text.strip()
                    get_diagnostic = getattr(self.response_text_provider, "last_result", None)
                    if callable(get_diagnostic):
                        diagnostic = get_diagnostic()
                        if isinstance(diagnostic, Mapping):
                            provider_diagnostic = dict(diagnostic)
                else:
                    _PROVIDER_LOGGER.info(
                        "provider_result provider=%s status=skipped reason=agreement_translation_unavailable",
                        provider_name,
                    )
            else:
                _PROVIDER_LOGGER.info(
                    "provider_result provider=%s status=skipped reason=engine_owned_agreement_confirmation",
                    provider_name,
                )
        elif self.response_text_provider is not None:
            try:
                contextual_generate = getattr(self.response_text_provider, "generate_dialogue", None)
                structured_generate = getattr(
                    self.response_text_provider, "generate_dialogue_with_analysis", None,
                )
                # Open national-position and prior-term preference questions
                # are discussion turns, not action requests. Running extraction
                # here adds latency and can turn them into contract-status fallbacks.
                if national_position_requirements or prior_terms_comparison or refusal_followup:
                    structured_generate = None
                if callable(structured_generate) or callable(contextual_generate):
                    recent_messages = payload.get("recent_messages")
                    if not isinstance(recent_messages, list):
                        recent_messages = [
                            {"speaker": item.sender, "text": item.text, "intent": item.intent,
                             "decision": item.decision}
                            for item in conversation.messages[-8:]
                        ]
                    normalized_history = []
                    for item in recent_messages[-8:]:
                        if not isinstance(item, Mapping):
                            continue
                        speaker = str(item.get("speaker") or item.get("sender") or "UNKNOWN").upper()
                        message_text = item.get("text", "")
                        record = {
                            "speaker": speaker,
                            "kind": str(item.get("kind") or "dialogue"),
                        }
                        for metadata in ("intent", "decision"):
                            if item.get(metadata) is not None:
                                record[metadata] = str(item[metadata])
                        if isinstance(item.get("request_id"), str):
                            record["request_id"] = item["request_id"]
                        if isinstance(message_text, str) and message_text.strip():
                            prior_message_language = detect_language(message_text)
                            language_is_clean = (
                                matches_output_language(message_text, prior_message_language)
                                if prior_message_language else not (
                                    re.search(r"[a-z]{2,}", message_text, re.IGNORECASE)
                                    and re.search(r"[а-яёіїєґ]", message_text, re.IGNORECASE)
                                )
                            )
                            if language_is_clean and (
                                    prior_message_language is None or prior_message_language == language):
                                record["text"] = message_text.strip()[-1200:]
                            else:
                                record["text_omitted_language"] = prior_message_language or "mixed_or_unknown"
                        if "text" in record or "decision" in record or "intent" in record:
                            normalized_history.append(record)
                    if not normalized_history or normalized_history[-1].get("text") != text:
                        normalized_history.append({"speaker": sender, "text": text, "kind": "player_message"})
                    fact_keys = (
                        # V5 pair facts first, followed by V6 values. This fixed
                        # order keeps critical date/war/faction/NAP/relations data
                        # inside the provider's bounded prompt context.
                        "date", "war_status", "same_faction", "existing_non_aggression_pact",
                        "relations", "threat", "initiator_exists", "target_exists",
                        "initiator_owned_states", "target_owned_states", "initiator_divisions",
                        "target_divisions", "initiator_factories", "target_factories", "target_stability",
                        "game_date", "faction_status", "opinion", "military_balance", "alliance_status",
                        "current_state_owners", "claims", "ideology", "ideology_group",
                        "player_ideology_group", "major_power", "player_major_power",
                    )
                    known_game_context = {
                        key: context[key] for key in fact_keys
                        if key in context and context[key] is not None
                        and not (isinstance(context[key], str) and context[key].strip().lower() == "unknown")
                    }
                    dialogue_context = {
                        "phrase_recent": payload.get("phrase_recent", []),
                        "group_chat": payload.get("group_chat") is True,
                        "campaign_id": payload.get("campaign_id"),
                        "chat_id": payload.get("chat_id"),
                        "conversation_id": conversation_id,
                        "speaking_country": country_name,
                        "speaking_country_tag": receiver,
                        "other_country": sender_name,
                        "other_country_tag": sender,
                        "participant_country_names": participant_country_names,
                        "request_id": payload.get("request_id"),
                        "latest_player_message_text": (
                            payload.get("player_source_text")
                            if isinstance(payload.get("player_source_text"), str)
                            else text
                        ),
                        "latest_player_request_id": (
                            payload.get("player_source_request_id")
                            if isinstance(payload.get("player_source_request_id"), str)
                            else next((item.get("request_id")
                                for item in reversed(payload.get("recent_messages", []))
                                if isinstance(item, Mapping)
                                and str(item.get("speaker") or item.get("sender") or "").upper() == sender
                                and isinstance(item.get("request_id"), str)),
                                payload.get("request_id"))
                        ),
                        "latest_player_country_tag": (
                            str(payload.get("player_country_tag", "")).upper()
                            if isinstance(payload.get("player_country_tag"), str)
                            and payload.get("player_country_tag")
                            else sender
                        ),
                        "grounded_player_analysis": (
                            payload.get("player_source_analysis")
                            if isinstance(payload.get("player_source_analysis"), Mapping)
                            else None
                        ),
                        "leader_expression_style": leader_style(context),
                        "active_agreement_action_participants": (
                            payload.get("active_agreement_action_participants", {})
                            if isinstance(payload.get("active_agreement_action_participants", {}), Mapping)
                            else {}
                        ),
                        "latest_utterance_text": payload.get("latest_utterance_text", text),
                        "latest_utterance_speaker": payload.get("latest_utterance_speaker", sender),
                        "latest_utterance_request_id": payload.get("latest_utterance_request_id"),
                        "recent_messages": normalized_history[-8:],
                        "known_game_context": known_game_context,
                        "diplomatic_memory": trusted_memory,
                        "interface_language": payload.get("interface_language", "en"),
                        "game_context_provenance": (
                            dict(payload["game_context_provenance"])
                            if isinstance(payload.get("game_context_provenance"), Mapping) else None
                        ),
                        "known_country_tags": context.get("known_country_tags", []),
                        "country_tag_aliases": participant_aliases,
                        "unknown_game_context_fields": relationship.context_unknowns,
                        "engine_reasoning_factors": factors,
                        "strategic_plan": strategic_plan,
                        "communication_acts": communication_analysis,
                        "diplomatic_doctrine": {
                            "ideology_group": doctrine["ideology_group"],
                            "sender_ideology_group": doctrine.get("sender_ideology_group", "unknown"),
                            "ideology_observed": doctrine["ideology_observed"],
                            "major_power": context.get("major_power", "unknown"),
                            "sender_major_power": context.get("player_major_power", "unknown"),
                            "strategic_style": doctrine["strategic_style"],
                            "profile": doctrine["profile"],
                            "territorial_claims": doctrine["claims"][:12],
                            "territorial_offer": doctrine["territorial_offer"],
                            "authority": doctrine["authority"],
                        },
                        "negotiation_round": negotiation.round + 1,
                        "new_negotiation_input": self._has_materially_new_player_input(
                            payload.get("recent_messages", []),
                            str(payload.get("player_source_text") or text),
                            str(payload.get("player_country_tag") or sender).upper(),
                        ),
                    }
                    if refusal_followup and not agreement_review:
                        prior_refusal = refusal_record or {}
                        refusal_actor_tag = str(prior_refusal.get("speaker") or receiver).upper()
                        refusal_target_tag = str(prior_refusal.get("recipient") or sender).upper()
                        verified_refusal_facts = {}
                        if internally_verified_observation:
                            for fact_name in (
                                    "date", "war_status", "same_faction",
                                    "existing_non_aggression_pact", "relations", "threat"):
                                value = context.get(fact_name)
                                if value is not None and str(value).strip().lower() not in {
                                        "", "unknown", "unavailable"}:
                                    verified_refusal_facts[fact_name] = value
                        verified_refusal_doctrine = {}
                        if doctrine_is_verified:
                            for fact_name in ("ideology", "ideology_group", "player_ideology_group",
                                              "major_power", "player_major_power"):
                                value = context.get(fact_name)
                                if value is not None and str(value).strip().lower() not in {
                                        "", "unknown", "unavailable"}:
                                    verified_refusal_doctrine[fact_name] = value
                        dialogue_context["response_requirements"] = {
                            "request_type": "refusal_followup_nonbinding",
                            "source_refusal_request_id": prior_refusal.get("request_id"),
                            "source_offer_request_id": (
                                refusal_offer_record.get("request_id")
                                if isinstance(refusal_offer_record, Mapping) else None
                            ),
                            "source_offer_intent": (
                                str(refusal_offer_record.get("intent") or "")
                                if isinstance(refusal_offer_record, Mapping) else ""
                            ),
                            "refusal_record_available": bool(refusal_record),
                            "refusal_actor_tag": refusal_actor_tag if refusal_record else None,
                            "refusal_actor_name": (
                                participant_aliases.get(refusal_actor_tag)
                                or (country_name if refusal_actor_tag == receiver else sender_name)
                            ) if refusal_record else None,
                            "refusal_target_tag": refusal_target_tag if refusal_record else None,
                            "refusal_target_name": (
                                participant_aliases.get(refusal_target_tag)
                                or (sender_name if refusal_target_tag == sender else country_name)
                            ) if refusal_record else None,
                            "source_refusal_text": str(prior_refusal.get("text") or "")[:600],
                            "closed_offer_text": (
                                str(refusal_offer_record.get("text") or "")[:600]
                                if isinstance(refusal_offer_record, Mapping) else ""
                            ),
                            "refusal_is_closed_for_this_turn": True,
                            "must_not_create_contract_position": True,
                            "answer_both_requests_if_present": True,
                            "requested_communication_acts": list(
                                communication_analysis.get("acts", [])
                            ),
                            "reason_recorded_in_transcript": self._has_recorded_refusal_reason(
                                str(prior_refusal.get("text") or "")
                            ),
                            "verified_game_facts": verified_refusal_facts,
                            "verified_state_doctrine": verified_refusal_doctrine,
                            "reasoning_policy": (
                                "Explain only reasons already stated or supported by verified game facts. "
                                "If the refusal gave no reason, say that no reason was recorded; any new "
                                "assessment must be clearly framed as a concern, not a game fact."
                            ),
                            "alternative_policy": (
                                "Offer one nonbinding discussion option if requested. Do not present it as "
                                "an accepted counteroffer, signed term, or changed treaty version."
                            ),
                        }
                    elif prior_terms_requirements and not agreement_review:
                        dialogue_context["response_requirements"] = prior_terms_requirements
                    elif national_position_requirements and not agreement_review:
                        dialogue_context["response_requirements"] = national_position_requirements
                    elif snapshot_requirements and not agreement_review:
                        dialogue_context["response_requirements"] = snapshot_requirements
                    elif intent == "information" and prior_offer:
                        dialogue_context["response_requirements"] = self._clarification_requirements(
                            prior_offer.intent, previous_country_response
                        )
                    elif intent == "information" and agenda_requirements:
                        dialogue_context["response_requirements"] = agenda_requirements
                    def engine_point_policy(points, negotiated=None):
                        points = resolve_point_scopes(points,
                            payload.get("active_agreement_action_participants", {}),
                            str(dialogue_context.get("latest_player_message_text") or ""))
                        from diplomatic_planner import point_policy_context_hash
                        context_hash = point_policy_context_hash(context)
                        policy_history = getattr(self, "_point_plan_history", self.direct_point_policy_state["engine_point_policies"])
                        history = [row for rows in policy_history.values() for row in rows
                                   if row.get("speaker_tag") == receiver and row.get("context_hash") == context_hash
                                   and row.get("strategic_goals_id") == (strategic_interests or {}).get("goals_id")
                                   and row.get("campaign_id") == payload.get("campaign_id")
                                   and row.get("conversation_id") == conversation_id]
                        planned = []
                        policy_relationship = DiplomaticRelationship.from_context(context)
                        if trusted_memory:
                            policy_relationship.promise_trust = _number(
                                (trusted_memory.get('promise_trust') or {}).get('value'),0.5,0.0,1.0)
                        for index, point in enumerate(points):
                            catalog_action = self.action_registry.get(str(point.get("action_id") or ""))
                            point_strategy = build_strategic_plan(
                                intent=(catalog_action or {}).get("intent", "unknown/general"),
                                engine_decision=decision, relationship=policy_relationship.to_dict(),
                                game_context=context, doctrine=doctrine, profile=asdict(profile),
                                memory={k:v for k,v in (trusted_memory or {}).items()
                                        if k != 'prior_negotiation_positions'}, allow_override=True,
                                interests=strategic_interests,
                                campaign_id=str(payload.get("campaign_id") or ""),
                                conversation_id=conversation_id, request_id=str(payload.get("request_id") or ""),
                            )
                            row = build_point_decisions([point], point_strategy, history)[0]
                            if negotiated is not None:
                                binding, model_rows = negotiated
                                item = model_rows[index]
                                from treaty_interest_gate import decision as interest_decision
                                independent=interest_decision(point,context,receiver,point_strategy)
                                item={**item,**independent}
                                row['independent_policy']=deepcopy(independent)
                                from negotiation_progress import mark_progress
                                row.update(stance={'counteroffer':'counter_offer','defer':'discuss'}.get(
                                    item['decision'], item['decision']), clarification_reasons=item['questions'],
                                    negotiated_term=deepcopy(item), negotiation_binding=deepcopy(binding),
                                    negotiated_reason=item['reason'], counter_change=item['counter'])
                                row.pop("proposed_duration_years", None)
                                mark_progress(row, history)
                            row["strategic_goals_id"] = (strategic_interests or {}).get("goals_id")
                            row["point"] = index
                            row['conference_active']=any(c.get('chat_id')==payload.get('chat_id')
                                and c.get('campaign_id')==payload.get('campaign_id') and c.get('status')=='active'
                                for c in self.diplomatic_memory.get('historical_diplomacy',{}).get('conferences',{}).values())
                            row['pressure_in_source']=bool(re.search(r'ultimatum|or else|угроз|ультиматум|иначе.*(?:войн|напад)',
                                str(dialogue_context.get('latest_player_message_text') or ''),re.I))
                            source_text_for_topics=str(dialogue_context.get('latest_player_message_text') or '')
                            row['source_discussion_topics']=[topic for topic,pattern in {
                                'invitation':r'invit|пригла|предварительн\w* встреч',
                                'confidential':r'secret|private|confidential|leak|rumou?r|секрет|закрыт|конфиденц|утечк|слух',
                                'mediation':r'mediat|intermediar|посредник|посреднич'}.items()
                                if re.search(pattern,source_text_for_topics,re.I)]
                            from grounded_expression import basis as expression_basis
                            proof=(payload.get('_verified_observation_capability') is self._verified_observation_capability and 'critical_context_stale_conflicting_or_unverified' not in row.get('clarification_reasons',[]))
                            row['expression_basis']=expression_basis(payload.get('game_context') or {},verified=proof,person_id=payload.get('phrase_person_id'),action_id=row.get('action_id'))
                            if row.get('stance')=='clarify':
                                row['expression_basis']['facts']=[] # Answer missing terms before unrelated argumentation.
                            row['expression_basis']['source_request_id']=(payload.get('game_context_provenance') or {}).get('request_id')
                            planned.append(row)
                        for row, point in zip(planned, points):
                            row["scope_fingerprint"] = hashlib.sha256(json.dumps(
                                {"point": point, "source_request_id": dialogue_context.get("latest_player_request_id"),
                                 "source_text": dialogue_context.get("latest_player_message_text")},
                                sort_keys=True, ensure_ascii=False, default=str,
                            ).encode("utf-8")).hexdigest()
                            row["policy_request_id"] = payload.get("request_id")
                            row["speaker_tag"] = receiver
                            row["context_hash"] = context_hash
                            row["campaign_id"] = payload.get("campaign_id")
                            row["conversation_id"] = conversation_id
                        sink = getattr(self, "_point_plan_sink", None)
                        if callable(sink):
                            return sink(str(payload.get("request_id") or ""), planned)
                        direct_key = f"{payload.get('campaign_id')}:{conversation_id}:{payload.get('request_id')}"
                        return self._store_point_policy(self.direct_point_policy_state, direct_key, planned)
                    if getattr(self.response_text_provider, "supports_engine_point_policy", False) is True:
                        dialogue_context["engine_point_policy"] = engine_point_policy
                        from diplomatic_planner import point_policy_context_hash
                        saved_history=getattr(self,'_point_plan_history',self.direct_point_policy_state['engine_point_policies'])
                        saved_batches=[[deepcopy(p) for p in rows if p.get('speaker_tag')==receiver
                                        and p.get('campaign_id')==payload.get('campaign_id')
                                        and p.get('conversation_id')==conversation_id
                                        and p.get('context_hash')==point_policy_context_hash(context)]
                                       for rows in saved_history.values()]
                        dialogue_context['saved_point_positions']=next((rows for rows in reversed(saved_batches) if rows),[])
                    if callable(structured_generate):
                        structured_result = structured_generate(
                            intent, decision, template_response, dialogue_context=dialogue_context,
                            language=language,
                        )
                        if isinstance(structured_result, Mapping):
                            generated_text = structured_result.get("reply")
                            if isinstance(structured_result.get("message_analysis"), Mapping):
                                message_analysis = dict(structured_result["message_analysis"])
                            if isinstance(structured_result.get("dialogue_positions"), list):
                                dialogue_positions = deepcopy(structured_result["dialogue_positions"])
                            if isinstance(structured_result.get("dialogue_position_status"), str):
                                dialogue_position_status = structured_result["dialogue_position_status"]
                            # One favorable package-level heuristic cannot
                            # accept several independent treaty clauses. The
                            # provider may only tighten this to a neutral
                            # package decision after source-grounded analysis;
                            # point positions remain advisory and unconfirmed.
                            if decision == "accept" and structured_result.get("effective_decision") == "defer":
                                decision = "defer"
                                factors = list(dict.fromkeys(
                                    factors + ["compound_offer_requires_pointwise_review"]
                                ))
                                if action is not None:
                                    action_result = self.action_registry.assess(
                                        action["action_id"], decision, context,
                                    )
                                template_response = self._compose_response(
                                    intent, decision, country_name, sender_name, text,
                                    prior_offer, profile, relationship,
                                    language=template_language,
                                    previous_country_response=previous_country_response,
                                )
                        else:
                            generated_text = None
                    else:
                        generated_text = contextual_generate(
                            intent, decision, template_response, dialogue_context=dialogue_context,
                            language=language,
                        )
                else:
                    generated_text = self.response_text_provider.generate(
                        intent, decision, template_response
                    )
                if isinstance(generated_text, str) and generated_text.strip():
                    response_text = generated_text.strip()
                    get_diagnostic = getattr(self.response_text_provider, "last_result", None)
                    if callable(get_diagnostic):
                        diagnostic = get_diagnostic()
                        if isinstance(diagnostic, Mapping):
                            provider_diagnostic = dict(diagnostic)
                elif generated_text is not None:
                    provider_name = getattr(self.response_text_provider, "diagnostic_name", "custom")
                    _PROVIDER_LOGGER.info(
                        "provider_result provider=%s status=fallback reason=invalid_result",
                        provider_name,
                    )
                    provider_diagnostic = {
                        "provider": provider_name, "status": "fallback", "reason": "invalid_result",
                    }
                else:
                    get_diagnostic = getattr(self.response_text_provider, "last_result", None)
                    if callable(get_diagnostic):
                        diagnostic = get_diagnostic()
                        if isinstance(diagnostic, Mapping):
                            provider_diagnostic = dict(diagnostic)
            except Exception as provider_error:
                # The deterministic engine response remains the fallback.
                provider_name = getattr(self.response_text_provider, "diagnostic_name", "custom")
                _PROVIDER_LOGGER.exception(
                    "provider_result provider=%s status=fallback reason=provider_exception exception_type=%s",
                    provider_name, type(provider_error).__name__,
                )
                provider_diagnostic = {
                    "provider": provider_name, "status": "fallback", "reason": "provider_exception",
                }
        group_reply_override = None
        if payload.get("group_chat") is True and isinstance(provider_diagnostic, Mapping):
            if (provider_diagnostic.get("status") != "generated"
                    and provider_diagnostic.get("reason") != "verified_snapshot_summary"):
                # Do not turn an engine heuristic into an apparent country
                # counter-offer when Ollama failed. Keep the recorded point
                # positions unresolved and state that no terms were confirmed.
                has_source_actions = bool(
                    isinstance(message_analysis, Mapping)
                    and isinstance(message_analysis.get("actions"), list)
                    and message_analysis["actions"]
                )
                if intent == "information" and not has_source_actions:
                    # An unavailable answer to an open question is not a vote
                    # on a treaty. Preserve the informational classification.
                    # Do not invent a national position as a “safe” generated
                    # agenda item after rejecting Ollama output. Report that no
                    # reliable position was produced and leave all treaty state untouched.
                    response_requirements = dialogue_context.get("response_requirements", {})
                    if refusal_followup and isinstance(response_requirements, Mapping):
                        response_text = self._refusal_followup_fallback_text(
                            country_name, sender_name, response_requirements, template_language,
                        )
                    else:
                        response_text = (
                            self._national_position_unavailable_text(template_language)
                            if national_position_requirements else
                            self._information_unavailable_text(country_name, template_language)
                        )
                    group_reply_override = "provider_fallback_noncommittal"
                    provider_diagnostic = {
                        **provider_diagnostic,
                        "dialogue_override": group_reply_override,
                        "dialogue_reason": "no_reliable_informational_reply",
                    }
                    factors = list(dict.fromkeys(
                        factors + ["local_reply_unavailable_no_position_recorded"]
                    ))
                else:
                    decision = "defer"
                    if provider_diagnostic.get("status") == "invalid_json":
                        response_text = {
                            'ru':'Ollama не вернула проверяемый ответ по договору. Позиции сторон сохранены. Повторите запрос.',
                            'fr':'Ollama n’a pas fourni de réponse vérifiable. Les positions sont conservées. Réessayez.',
                        }.get(template_language, 'Ollama did not return a verifiable treaty response. Positions were preserved. Please retry.')
                    else:
                        response_text = self._group_provider_fallback_text(country_name, template_language)
                    group_reply_override = "provider_fallback_noncommittal"
                    factors = list(dict.fromkeys(
                        factors + ["local_reply_unavailable_no_position_recorded"]
                    ))
                if action is not None:
                    action_result = self.action_registry.assess(action["action_id"], decision, context)
                _PROVIDER_LOGGER.info(
                    "dialogue_reply status=%s reason=%s request_id=%s",
                    group_reply_override, provider_diagnostic.get("reason", "unknown"),
                    payload.get("request_id", "unknown"),
                )
            elif isinstance(dialogue_positions, list) and dialogue_positions:
                decision = self._group_decision_from_positions(dialogue_positions)
                if action is not None:
                    action_result = self.action_registry.assess(action["action_id"], decision, context)
            elif (isinstance(message_analysis, Mapping)
                  and isinstance(message_analysis.get("actions"), list)
                  and message_analysis["actions"]):
                # A compound proposal with no validated point positions is
                # unresolved. An empty list for an ordinary information turn
                # is not a package vote and must leave its question decision intact.
                decision = "defer"
                factors = list(dict.fromkeys(factors + ["action_positions_unavailable"]))
                dialogue_positions = self._unresolved_group_positions(
                    message_analysis, [], str(payload.get("request_id") or ""), template_language,
                )
                dialogue_position_status = "safe_unresolved"
                if action is not None:
                    action_result = self.action_registry.assess(action["action_id"], decision, context)
        elif (isinstance(dialogue_positions, list) and dialogue_positions
              and isinstance(provider_diagnostic, Mapping)
              and provider_diagnostic.get("status") == "generated"
              and dialogue_position_status in {"complete", "reclassified"}
              and isinstance(message_analysis, Mapping)
              and isinstance(message_analysis.get("actions"), list)
              and message_analysis["actions"]):
            # The legacy two-party response route used to retain its package
            # heuristic (sometimes "accept") even when Ollama's validated
            # point positions unanimously rejected the offer. Keep the scalar
            # V5 decision consistent with the structured positions on every
            # route; agreement signing remains separately gated by the
            # agreement manager and player confirmation.
            decision = self._group_decision_from_positions(dialogue_positions)
            if action is not None:
                action_result = self.action_registry.assess(action["action_id"], decision, context)
        previous_country_replies = []
        if payload.get("group_chat") is True:
            for old in reversed(payload.get("recent_messages", [])):
                if (isinstance(old, Mapping)
                        and str(old.get("speaker", "")).upper() == receiver
                        and str(old.get("recipient", "")).upper() == sender
                        and old.get("kind") == "country_response"
                        and isinstance(old.get("text"), str)
                        and old.get("text").strip()):
                    previous_country_replies.append(old["text"].strip())
                    # Only the immediately preceding country turn is a useful
                    # repetition baseline. Older positions may be appropriate
                    # again after the player changes or revisits a term.
                    break
        if not previous_country_replies and previous_country_response and previous_country_response.text:
            previous_country_replies.append(previous_country_response.text)
        matching_score = max((self._duplicate_similarity(response_text, prior_text)
                              for prior_text in previous_country_replies
                              if self._is_near_duplicate(response_text, prior_text)),
                             default=0.0)
        new_negotiation_input = bool(
            self._has_materially_new_player_input(
                payload.get("recent_messages", []),
                str(payload.get("player_source_text") or text),
                str(payload.get("player_country_tag") or sender).upper(),
            )
        )
        # An exact repeat is never a useful answer. Near-identical wording may
        # still be a legitimate return to a country's terms when the player has
        # introduced a substantive new point in this turn.
        allow_position_revisit = bool(new_negotiation_input and matching_score < 1.0)
        if (not (provider_diagnostic or {}).get("engine_policy")
                and not agreement_review and matching_score and not allow_position_revisit
                and group_reply_override is None):
            response_text = self._non_repeating_clarification(
                intent, decision, country_name, template_language, prior_offer,
                previous_country_replies,
                current_player_message=str(payload.get("player_source_text") or text),
                materially_new_input=new_negotiation_input,
            )
            _PROVIDER_LOGGER.info(
                "dialogue_reply status=deduplicated_fallback similarity=%.3f compared_replies=%d prior_provider_status=%s",
                matching_score, len(previous_country_replies),
                provider_diagnostic.get("status", "unknown")
                if isinstance(provider_diagnostic, Mapping) else "unknown",
            )
            if provider_diagnostic is not None:
                provider_diagnostic = {
                    **provider_diagnostic,
                    # `status` records what Ollama actually did. The engine
                    # replacement is reported separately so generated text is
                    # not mislabeled as a provider fallback.
                    "dialogue_override": "engine_deduplicated_response",
                    "dialogue_reason": "repeated_country_reply",
                }
            if payload.get("group_chat") is True:
                decision = "defer"
                dialogue_positions = self._unresolved_group_positions(
                    message_analysis, dialogue_positions,
                    str(payload.get("request_id") or ""), template_language,
                )
                dialogue_position_status = "deduplicated_unconfirmed"
                if action is not None:
                    action_result = self.action_registry.assess(action["action_id"], decision, context)
        elif matching_score and allow_position_revisit:
            # A changed offer/question can legitimately call for returning to
            # the country's previously stated position. Preserve the generated
            # turn instead of replacing it with a generic anti-repeat response.
            _PROVIDER_LOGGER.info(
                "dialogue_reply status=repeat_position_allowed reason=new_player_input",
            )
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        conversation.turn += 1
        significance = self._significance(intent, decision)
        conversation.messages.append(DiplomaticMessage(
            sender=sender, receiver=receiver, timestamp=timestamp, turn=conversation.turn,
            text=text, intent=intent, decision=decision, diplomatic_significance=significance,
        ))
        conversation.messages.append(DiplomaticMessage(
            sender=receiver, receiver=sender, timestamp=timestamp, turn=conversation.turn,
            text=response_text, intent=intent, decision=decision,
            diplomatic_significance="engine_response",
        ))
        if len(conversation.messages) > self.max_history:
            conversation.messages = conversation.messages[-self.max_history:]
        self._update_relationship(relationship, decision, intent)
        negotiation.round += 1
        negotiation.previous_decision = decision
        negotiation.trust_delta = round(relationship.trust - trust_before, 4)
        proposal_intents = {
            "offer", "negotiate", "demand", "alliance", "non_aggression", "trade",
            "peace", "ceasefire", "military_access", "guarantee", "embargo", "lift_embargo", "counter_offer",
            "docking_rights", "air_base_access", "faction_join", "faction_leave", "market_access", "territory_partition",
            "truce", "white_peace", "peace_conference", *NEW_ACTIONS,
        }
        if intent in proposal_intents:
            negotiation.requested_terms = list(dict.fromkeys(negotiation.requested_terms + [intent]))
            if intent != "counter_offer" or not negotiation.current_offer:
                negotiation.current_offer = text
            if intent not in negotiation.unresolved_terms:
                negotiation.unresolved_terms.append(intent)
        if decision == "counter_offer":
            negotiation.current_offer = response_text if payload.get("group_chat") is True else template_response
        elif decision == "accept":
            negotiation.unresolved_terms.clear()
        relationship.diplomatic_history.append({
            "turn": conversation.turn,
            "sender": sender,
            "receiver": receiver,
            "intent": intent,
            "decision": decision,
            "significance": significance,
        })
        relationship.diplomatic_history = relationship.diplomatic_history[-20:]
        self.conversations.move_to_end(conversation_id)

        if strategic_plan.get("mode") == "proposal_strategy":
            strategic_plan["final_engine_outcome"] = decision
            strategic_plan["final_outcome_is_binding"] = False
            if isinstance(dialogue_positions, list) and dialogue_positions:
                strategic_plan["final_outcome_source"] = "validated_pointwise_positions"
            elif (isinstance(provider_diagnostic, Mapping)
                  and provider_diagnostic.get("status") != "generated"):
                strategic_plan["final_outcome_source"] = "engine_safe_fallback"
            elif decision != strategic_plan.get("selected_action"):
                strategic_plan["final_outcome_source"] = "later_engine_validation"
            else:
                strategic_plan["final_outcome_source"] = "initial_engine_strategy"

        return {
            "intent": intent,
            "decision": decision,
            "diplomatic_decision": {
                "intent": intent,
                "outcome": decision,
                "authority": "diplomatic_engine_with_validated_positions",
                "strategic_plan_id": strategic_plan.get("plan_id"),
                "position_status": dialogue_position_status or "unavailable",
                "positions": deepcopy(dialogue_positions or []),
                "binding": False,
                "requires_agreement_confirmation": True,
            },
            "diplomatic_expression": {
                "text": response_text,
                "language": language,
                "provider": (provider_diagnostic.get("provider")
                             if isinstance(provider_diagnostic, Mapping) else "engine"),
                "text_origin": (provider_diagnostic or {}).get("text_origin", "legacy"),
                "expression_error": (provider_diagnostic or {}).get("expression_error"),
                "provider_status": (provider_diagnostic.get("status")
                                    if isinstance(provider_diagnostic, Mapping) else "not_required"),
            },
            "action": ({**action, **action_result} if action_result else None),
            "confidence": round(confidence, 2),
            "reasoning_factors": factors,
            "strategic_plan": deepcopy(strategic_plan),
            "communication_acts": communication_analysis,
            "response": response_text,
            "message": response_text,
            "diplomatic_tone": self._tone(decision),
            "timestamp": timestamp,
            "conversation_id": conversation_id,
            "game_context": dict(game_state),
            "context_source": "hoi4_game_log" if "request_id" in payload else "provided_context",
            "sender": receiver,
            "receiver": sender,
            "relationship_state": relationship.to_dict(),
            "negotiation_state": negotiation.to_dict(),
            "history_size": len(conversation.messages),
            **({"message_analysis": message_analysis} if message_analysis else {}),
            **({"dialogue_positions": dialogue_positions,
                "dialogue_position_status": dialogue_position_status or "unavailable"}
               if dialogue_positions is not None else {}),
            **({"provider_diagnostic": provider_diagnostic} if provider_diagnostic else {}),
            **({"agreement_review": deepcopy(dict(agreement_review))} if agreement_review else {}),
        }

    @staticmethod
    def _compose_agreement_confirmation_review(country: str, review: Mapping[str, Any],
                                              language: str) -> str:
        phrases = {
            "ru": {
                "accepted": "Франция безусловно подтверждает этот пункт.",
                "conditional_acceptance": "Франция согласна на этот пункт только при выполнении условий, указанных в пунктах ниже.",
                "required": "Франция требует этот пункт как условие своего предложения; это не означает, что обязательство уже исполнено.",
                "proposed": "Франция ранее предложила этот пункт; отдельного безусловного согласия пока нет.",
                "rejected": "Франция отклоняет этот пункт.",
                "mentioned": "В сохранённой переписке явная позиция Франции по этому пункту не зафиксирована; согласие не подтверждено.",
                "closing": "Это сводка сохранённых позиций, а не новое согласие. Условные и неподтверждённые пункты не считаются принятыми; проект договора остаётся черновиком.",
            },
            "uk": {
                "accepted": "Франція безумовно підтверджує цей пункт.",
                "conditional_acceptance": "Франція погоджується на цей пункт лише за виконання умов, указаних у пунктах нижче.",
                "required": "Франція вимагає цей пункт як умову своєї пропозиції; це не означає, що зобов'язання вже виконано.",
                "proposed": "Франція раніше запропонувала цей пункт; окремої безумовної згоди поки немає.",
                "rejected": "Франція відхиляє цей пункт.",
                "mentioned": "У збереженому листуванні чітку позицію Франції щодо цього пункту не зафіксовано; згоду не підтверджено.",
                "closing": "Це підсумок збережених позицій, а не нова згода. Умовні й непідтверджені пункти не вважаються прийнятими; проєкт договору залишається чернеткою.",
            },
            "fr": {
                "accepted": "La France confirme ce point sans condition.",
                "conditional_acceptance": "La France n'accepte ce point que si les conditions indiquées dans les points ci-dessous sont remplies.",
                "required": "La France exige ce point comme condition de sa proposition; cela ne signifie pas que l'engagement est exécuté.",
                "proposed": "La France a proposé ce point auparavant; aucun accord inconditionnel distinct n'est enregistré.",
                "rejected": "La France rejette ce point.",
                "mentioned": "La correspondance conservée ne contient pas de position explicite de la France sur ce point; son accord n'est pas confirmé.",
                "closing": "Ce résumé reprend les positions enregistrées; il ne constitue pas un nouvel accord. Les points conditionnels ou non confirmés ne sont pas acceptés; le projet reste à l'état de brouillon.",
            },
            "en": {
                "accepted": "France confirms this term unconditionally.",
                "conditional_acceptance": "France agrees to this term only if the conditions listed in the points below are met.",
                "required": "France requires this term as a condition of its proposal; this does not mean the obligation has been performed.",
                "proposed": "France proposed this term earlier; no separate unconditional acceptance is recorded.",
                "rejected": "France rejects this term.",
                "mentioned": "The saved transcript has no explicit French position on this term; consent is not confirmed.",
                "closing": "This summarizes recorded positions; it is not new assent. Conditional or unconfirmed terms are not treated as accepted; the agreement remains a draft.",
            },
        }
        localized = phrases.get(language, phrases["en"])
        heading = {
            "ru": f"{country} отвечает отдельно по каждому пункту:",
            "uk": f"{country} відповідає окремо щодо кожного пункту:",
            "fr": f"{country} répond séparément à chaque point :",
            "en": f"{country} answers each term separately:",
        }.get(language, f"{country} answers each term separately:")
        lines = [heading]
        for index, term in enumerate(review.get("terms", []), start=1):
            if not isinstance(term, Mapping):
                continue
            stance = str(term.get("stance", "mentioned"))
            status = localized.get(stance, localized["mentioned"])
            term_id = str(term.get("term_id") or "")
            stored_label = str(term.get("text") or term_id).strip()
            stored_language = detect_language(stored_label)
            localized_label = _TERM_LABELS.get(term_id, {}).get(language)
            label = (stored_label if stored_language == language
                     else str(localized_label or stored_label))
            source_id = term.get("source_request_id")
            source = (f" [источник: {source_id}]" if source_id and language == "ru"
                      else f" [source: {source_id}]" if source_id
                      else " [источник не зафиксирован]" if language == "ru"
                      else " [source not recorded]")
            lines.append(f"{index}. {label} — {status}{source}")
        lines.append(localized["closing"])
        return "\n".join(lines)

    def _conversation(self, conversation_id: str, previous: Any, sender: str, receiver: str) -> Conversation:
        if conversation_id in self.conversations:
            return self.conversations[conversation_id]
        conversation = Conversation(conversation_id=conversation_id)
        if isinstance(previous, list):
            for item in previous[-self.max_history:]:
                if not isinstance(item, Mapping):
                    continue
                old_text = item.get("text", item.get("message", ""))
                if not isinstance(old_text, str) or not old_text:
                    continue
                old_sender = str(item.get("sender", item.get("from", sender))).upper()
                old_receiver = str(item.get("receiver", item.get("to", receiver))).upper()
                intent = str(item.get("intent") or self.classifier.classify(old_text))
                conversation.messages.append(DiplomaticMessage(
                    sender=old_sender, receiver=old_receiver,
                    timestamp=str(item.get("timestamp", item.get("date", ""))),
                    turn=int(item.get("turn", len(conversation.messages) // 2 + 1)),
                    text=old_text, intent=intent, decision=item.get("decision"),
                    diplomatic_significance=str(item.get("diplomatic_significance", "imported_history")),
                ))
            conversation.turn = max((message.turn for message in conversation.messages), default=0)
            conversation.messages = conversation.messages[-self.max_history:]
        self.conversations[conversation_id] = conversation
        while len(self.conversations) > self.max_conversations:
            self.conversations.popitem(last=False)
        return conversation

    @staticmethod
    def _profile(country: str, context: Optional[Mapping[str, Any]] = None,
                 doctrine: Optional[Mapping[str, Any]] = None) -> DiplomaticProfile:
        doctrine_profile = doctrine.get("profile") if isinstance(doctrine, Mapping) else None
        if (isinstance(doctrine_profile, Mapping)
                and doctrine.get("ideology_observed") is True):
            return DiplomaticProfile(**{
                field_name: _number(doctrine_profile.get(field_name), 0.5, 0.0, 1.0)
                for field_name in asdict(DiplomaticProfile())
            })
        # Do not infer a country's personality from its tag, a model summary,
        # or an editable context object. Verified ideology supplies a broad
        # transparent prior; without it, keep every country on neutral priors.
        return DiplomaticProfile()

    @staticmethod
    def _country_name(tag: str) -> str:
        normalized_tag = str(tag or "").upper()
        known = {"FRA": "France", "FRANCE": "France", "GER": "Germany", "GERMANY": "Germany"}
        if normalized_tag in known:
            return known[normalized_tag]
        name = load_country_names("en").get(normalized_tag)
        return name if name else (str(tag) if tag else "")

    @staticmethod
    def _localized_country_name(tag: str, supplied_name: str, language: str) -> str:
        normalized_tag = str(tag or "").upper()
        language = normalize_language(language) or "en"
        known = {
            "FRA": {"ru": "Франция", "uk": "Франція", "fr": "France", "en": "France"},
            "FRANCE": {"ru": "Франция", "uk": "Франція", "fr": "France", "en": "France"},
            "GER": {"ru": "Германия", "uk": "Німеччина", "fr": "Allemagne", "en": "Germany"},
            "GERMANY": {"ru": "Германия", "uk": "Німеччина", "fr": "Allemagne", "en": "Germany"},
        }
        if language in known.get(normalized_tag, {}):
            return known[normalized_tag][language]
        name = load_country_names(language).get(normalized_tag)
        if name:
            return name
        if supplied_name and supplied_name.strip().upper() != normalized_tag:
            return supplied_name
        return str(tag or supplied_name or "")

    @staticmethod
    def _clarification_requirements(intent: str, previous_country_response: Optional[DiplomaticMessage]) -> Dict[str, Any]:
        if intent == "non_aggression" and previous_country_response is not None:
            return {
                "answer_all_requested_parts": True,
                "proposed_conditions": [
                    "mutual written commitment not to attack the other party",
                    "advance notice and consultation on measures the other party identifies as materially affecting its security",
                    "the same notification, consultation, and review procedure for both parties",
                ],
                "expected_benefit_per_condition": [
                    "formal reciprocity and less uncertainty about the other party's intentions",
                    "a chance to raise concerns before a measure is taken, reducing misunderstanding and escalation risk",
                    "protection against one-sided obligations through the same rules and review for both parties",
                ],
                "negotiating_rationale": "these are plausible proposed interests for the French negotiating position, not verified country policy or game data",
                "territorial_position": "no territorial transfer is included in this counter-offer",
                "status": "non-binding proposal for discussion; no agreement or game effect has occurred",
            }
        return {
            "answer_all_requested_parts": True,
            "avoid_repeating_prior_country_reply": True,
            "distinguish_proposed_terms_from_verified_game_facts": True,
        }

    @staticmethod
    def _duplicate_similarity(left: str, right: str) -> float:
        normalize = lambda value: re.sub(r"\W+", " ", value.casefold(), flags=re.UNICODE).strip()
        a, b = normalize(left), normalize(right)
        return SequenceMatcher(None, a, b).ratio() if a and b else 0.0

    @staticmethod
    def _has_materially_new_player_input(recent_messages: Any, current_text: str,
                                         player_tag: str) -> bool:
        """Detect a concrete change in the player's proposal or question."""
        if not isinstance(recent_messages, list):
            return False
        player_tag = str(player_tag).upper()
        prior_player_texts = [
            item.get("text", "").strip()
            for item in recent_messages
            if isinstance(item, Mapping)
            and str(item.get("speaker", item.get("sender", ""))).upper() == player_tag
            and isinstance(item.get("text"), str)
            and item.get("text", "").strip()
        ]
        normalize = lambda value: re.sub(r"\W+", " ", value.casefold(), flags=re.UNICODE).strip()
        if prior_player_texts and normalize(prior_player_texts[-1]) == normalize(current_text):
            prior_player_texts.pop()
        if not prior_player_texts:
            return False
        previous_text = prior_player_texts[-1]
        numbers = lambda value: set(re.findall(r"\d+(?:[.,]\d+)?", value.casefold()))
        current_numbers = numbers(current_text)
        if current_numbers - numbers(previous_text):
            return True

        stopwords = set("""
            the and for with from that this your you are was were have has had will would could should
            please we our their about into after before under between france germany french german
            les des une pour avec dans que qui est sont nous vous notre cette ces mais alors la de du au aux et ou par sur
            что это как или для при если чтобы тогда этот эта эти между также стороны германия франция
            германии франции немецкий французский можно ли бы уже так но
            що це як або для при якщо щоб тоді цей ця ці між також сторони німеччина франція
            німеччини франції німецький французький можна чи вже так але
        """.split())

        def content_tokens(value: str) -> set[str]:
            tokens = re.findall(r"\d+(?:[.,]\d+)?|[^\W\d_]+", value.casefold(), flags=re.UNICODE)
            return {token for token in tokens if token not in stopwords and (token.isdigit() or len(token) >= 3)}

        current_tokens = content_tokens(current_text)
        previous_tokens = content_tokens(previous_text)
        novel = current_tokens - previous_tokens
        # A new term, rationale, or direct question commonly introduces at least
        # two content words; short acknowledgements and paraphrased repeats do not.
        return len(novel) >= 2 and (
            not current_tokens or len(novel) / max(1, len(current_tokens)) >= 0.25
        )

    @classmethod
    def _is_near_duplicate(cls, left: str, right: str) -> bool:
        normalize = lambda value: re.sub(r"\W+", " ", value.casefold(), flags=re.UNICODE).strip()
        a, b = normalize(left), normalize(right)
        if not a or not b:
            return False
        if a == b:
            return True
        # Character-level similarity alone overweights shared diplomatic boilerplate.
        # Require both near-verbatim wording and shared three-word phrases.
        ratio = SequenceMatcher(None, a, b).ratio()
        a_tokens, b_tokens = a.split(), b.split()
        if min(len(a_tokens), len(b_tokens)) < 3:
            return ratio >= 0.96
        a_shingles = set(zip(a_tokens, a_tokens[1:], a_tokens[2:]))
        b_shingles = set(zip(b_tokens, b_tokens[1:], b_tokens[2:]))
        union = a_shingles | b_shingles
        overlap = len(a_shingles & b_shingles) / len(union) if union else 0.0
        return ratio >= 0.96 and overlap >= 0.80

    @staticmethod
    def _non_repeating_clarification(intent: str, decision: str, country: str,
                                     language: str, prior_offer: Optional[DiplomaticMessage],
                                     previous_text: Any, *, current_player_message: str = "",
                                     materially_new_input: bool = False) -> str:
        previous_texts = ([previous_text] if isinstance(previous_text, str)
                          else [text for text in previous_text
                                if isinstance(text, str)] if isinstance(previous_text, (list, tuple))
                          else [])
        def repeats_history(candidate: str) -> bool:
            return any(DiplomacyEngine._is_near_duplicate(candidate, prior)
                       for prior in previous_texts)

        if (materially_new_input and decision != "accept"
                and re.search(
                    r"\b\d+(?:[.,]\d+)?\s*(?:days?|months?|years?|jours?|mois|ans?|"
                    r"дн(?:я|ей)?|месяц(?:а|ев)?|лет|год(?:а|ов)?|днів|місяц(?:ів|я)?|рок(?:и|ів))\b",
                    current_player_message or "", re.IGNORECASE,
                )):
            revised_duration = extract_duration(current_player_message or "")
            duration = re.search(
                r"\b(\d+(?:[.,]\d+)?)\s*(days?|months?|years?|jours?|mois|ans?|"
                r"дн(?:я|ей)?|месяц(?:а|ев)?|лет|год(?:а|ов)?|днів|місяц(?:ів|я)?|рок(?:и|ів))\b",
                revised_duration or current_player_message or "", re.IGNORECASE,
            )
            changed_term = None
            if duration:
                amount = duration.group(1)
                source_unit = duration.group(2).casefold()
                unit_kind = ("day" if source_unit.startswith(("day", "jour", "дн", "днів")) else
                             "month" if source_unit.startswith(("month", "mois", "месяц", "місяц")) else
                             "year")
                if language == "fr":
                    unit = {"day": "jour" + ("s" if amount not in {"1", "1.0", "1,0"} else ""),
                            "month": "mois", "year": "an" + ("s" if amount not in {"1", "1.0", "1,0"} else "")}[unit_kind]
                elif language == "ru":
                    value = int(float(amount.replace(",", "."))) if re.fullmatch(r"\d+(?:[.,]0+)?", amount) else 0
                    last_two, last = value % 100, value % 10
                    if unit_kind == "day":
                        unit = "день" if last == 1 and last_two != 11 else "дня" if 2 <= last <= 4 and not 12 <= last_two <= 14 else "дней"
                    elif unit_kind == "month":
                        unit = "месяц" if last == 1 and last_two != 11 else "месяца" if 2 <= last <= 4 and not 12 <= last_two <= 14 else "месяцев"
                    else:
                        unit = "год" if last == 1 and last_two != 11 else "года" if 2 <= last <= 4 and not 12 <= last_two <= 14 else "лет"
                elif language == "uk":
                    value = int(float(amount.replace(",", "."))) if re.fullmatch(r"\d+(?:[.,]0+)?", amount) else 0
                    last_two, last = value % 100, value % 10
                    if unit_kind == "day":
                        unit = "день" if last == 1 and last_two != 11 else "дні" if 2 <= last <= 4 and not 12 <= last_two <= 14 else "днів"
                    elif unit_kind == "month":
                        unit = "місяць" if last == 1 and last_two != 11 else "місяці" if 2 <= last <= 4 and not 12 <= last_two <= 14 else "місяців"
                    else:
                        unit = "рік" if last == 1 and last_two != 11 else "роки" if 2 <= last <= 4 and not 12 <= last_two <= 14 else "років"
                else:
                    unit = {"day": "day" + ("s" if amount not in {"1", "1.0", "1,0"} else ""),
                            "month": "month" + ("s" if amount not in {"1", "1.0", "1,0"} else ""),
                            "year": "year" + ("s" if amount not in {"1", "1.0", "1,0"} else "")}[unit_kind]
                changed_term = f"{amount} {unit}"
            if decision == "reject":
                templates = {
                    "en": f"{country} rejects the revised {changed_term or 'term'} as proposed; no replacement term is stated in this reply.",
                    "fr": f"{country} rejette le {changed_term or 'terme'} révisé tel qu'il est proposé; aucune durée de remplacement n'est formulée ici.",
                    "ru": f"{country} отклоняет предложенный вариант{(' со сроком ' + changed_term) if changed_term else ''}; в этом ответе замена ему не предложена.",
                    "uk": f"{country} відхиляє запропонований варіант{(' зі строком ' + changed_term) if changed_term else ''}; у цій відповіді заміну не запропоновано.",
                }
            else:
                templates = {
                    "en": f"{country} has noted the revised {changed_term or 'term'}. Its position remains unresolved; this reply neither accepts nor rejects it.",
                    "fr": f"{country} a pris note du {changed_term or 'terme'} révisé. Sa position reste indéterminée; cette réponse ne vaut ni acceptation ni refus.",
                    "ru": f"{country} приняла к сведению изменённый пункт{(' со сроком ' + changed_term) if changed_term else ''}. Позиция по нему пока не определена: этот ответ не означает ни согласия, ни отказа.",
                    "uk": f"{country} взяла до відома змінений пункт{(' зі строком ' + changed_term) if changed_term else ''}. Позиція щодо нього ще не визначена: ця відповідь не є ні згодою, ні відмовою.",
                }
            candidate = templates[language]
            if not repeats_history(candidate):
                return candidate

        improvement_patterns = {
            "ru": r"улучш\w*|укреп\w*|налаж\w*|развив\w*|сближ\w*|отношени\w*",
            "uk": r"поліпш\w*|покращ\w*|зміцн\w*|налагод\w*|розвив\w*|відносин\w*|стосунк\w*",
            "fr": r"amélior\w*|renforc\w*|rétabl\w*|relations?",
            "en": r"improv\w*|strengthen\w*|repair\w*|relations?|relationship",
        }
        suggestion_patterns = {
            "ru": r"могли\s+бы|можно|что\s+сделать|предлож\w*|идеи|шаг\w*|мер\w*",
            "uk": r"могли\s+б|можна|що\s+зробити|пропозиц\w*|іде\w*|крок\w*|заход\w*",
            "fr": r"pourr\w*|peuv\w*|faire|propos\w*|idées?|étapes?|mesures?",
            "en": r"could|can|should|what\s+to\s+do|suggest\w*|ideas?|steps?|measures?",
        }
        improvement_pattern = improvement_patterns.get(language)
        suggestion_pattern = suggestion_patterns.get(language)
        preference_patterns = {
            "ru": r"предпочт\w*|приоритет\w*|из\s+этих\s+вариант\w*|какой\s+вариант|с\s+чего\s+начал\w*",
            "uk": r"переваг\w*|пріоритет\w*|з\s+цих\s+варіант\w*|який\s+варіант|з\s+чого\s+почал\w*",
            "fr": r"préfér\w*|priorit\w*|quelle\s+option|par\s+quoi\s+commencer",
            "en": r"prefer\w*|priorit\w*|which\s+(?:of\s+these\s+)?options?|which\s+would\s+you\s+start",
        }
        preference_pattern = preference_patterns.get(language)
        if (intent == "information" and current_player_message and preference_pattern
                and re.search(preference_pattern, current_player_message, re.I)):
            preferences = {
                "ru": ("Мы предпочли бы начать с регулярных консультаций: они дают обеим сторонам канал для "
                       "обсуждения спорных вопросов; предварительные уведомления можно добавить следом, поскольку "
                       "без доверия они могут восприниматься как формальность. Это гипотетическое предпочтение, "
                       "а не согласованное обязательство."),
                "uk": ("Ми воліли б почати з регулярних консультацій: вони дають обом сторонам канал для "
                       "обговорення спірних питань; попередні повідомлення можна додати згодом, адже без довіри "
                       "вони можуть сприйматися як формальність. Це гіпотетична перевага, а не погоджене "
                       "зобов'язання."),
                "fr": ("Nous privilégierions d'abord des consultations régulières, qui offriraient un moyen de traiter "
                       "les désaccords; les notifications préalables pourraient suivre, car sans confiance elles "
                       "risqueraient de rester formelles. C'est une préférence hypothétique, pas un engagement."),
                "en": ("We would start with regular consultations, which give both sides a channel to address disputes; "
                       "advance notices could follow, since without trust they may become a formality. That is a "
                       "hypothetical preference, not an agreed commitment."),
            }
            if not repeats_history(preferences[language]):
                return preferences[language]

        if (intent == "information" and current_player_message
                and improvement_pattern and suggestion_pattern
                and re.search(improvement_pattern, current_player_message, re.I)
                and re.search(suggestion_pattern, current_player_message, re.I)):
            # Last-resort answer for a repeated or unavailable model draft.
            # Keep this explicitly hypothetical: it is not a country vote,
            # accepted treaty term, or game action.
            alternatives = {
                "ru": ("Мы могли бы предложить регулярные встречи дипломатов и заранее уведомлять друг друга "
                       "о шагах, затрагивающих безопасность. Это помогло бы снизить риск недопонимания, "
                       "но потребовало бы взаимности и не решило бы все разногласия. Пока это идеи для обсуждения, "
                       "а не условия или обязательства."),
                "uk": ("Ми могли б запропонувати регулярні зустрічі дипломатів і завчасно повідомляти одне одного "
                       "про кроки, що стосуються безпеки. Це зменшило б ризик непорозумінь, але потребувало б "
                       "взаємності й не усунуло б усіх розбіжностей. Поки це лише ідеї для обговорення, "
                       "а не умови чи зобов'язання."),
                "fr": ("Nous pourrions proposer des consultations diplomatiques régulières et nous prévenir à l'avance "
                       "des mesures touchant à la sécurité. Cela réduirait le risque de malentendu, mais exigerait "
                       "de la réciprocité et ne réglerait pas tous les désaccords. Ce sont des idées à discuter, "
                       "pas des engagements."),
                "en": ("We could propose regular diplomatic consultations and advance notice of steps affecting security. "
                       "That could reduce misunderstandings, but it would require reciprocity and would not resolve "
                       "every disagreement. These are ideas for discussion, not commitments."),
            }
            if not repeats_history(alternatives[language]):
                return alternatives[language]

        if intent == "information" and prior_offer:
            candidate = DiplomacyEngine._clarify_counter_offer(prior_offer.intent, country, language)
            if not repeats_history(candidate):
                return candidate
            alternatives = {
                "ru": "В развитие встречного предложения: обе стороны письменно обязуются не нападать; заранее уведомляют друг друга о мерах, которые могут затронуть безопасность, и проводят консультации; одинаковые правила действуют для обеих сторон. Франция обосновывает эти условия взаимностью и предсказуемостью, чтобы снизить риск эскалации. Передача территории в условия не входит. Это предложение для обсуждения, а не подтверждённый факт или действующий договор.",
                "uk": "Уточнюючи зустрічну пропозицію: обидві сторони письмово зобов'язуються не нападати; завчасно повідомляють одна одну про заходи, що можуть вплинути на безпеку, і проводять консультації; однакові правила діють для обох сторін. Франція обґрунтовує ці умови взаємністю та передбачуваністю, щоб знизити ризик ескалації. Передача території до умов не входить. Це лише пропозиція для обговорення, а не підтверджений факт чи чинний договір.",
                "fr": "Pour préciser notre contre-proposition : chaque partie s'engagerait par écrit à ne pas attaquer l'autre, notifierait à l'avance les mesures susceptibles d'affecter la sécurité et demanderait des consultations; les mêmes règles s'appliqueraient aux deux. La France présente la réciprocité et la prévisibilité comme des moyens de réduire le risque d'escalade. Aucun transfert territorial n'est inclus. Ce sont des termes à discuter, pas un accord en vigueur.",
                "en": "To make the counter-offer concrete: each side would commit in writing not to attack the other, give advance notice of measures that could affect security, and consult; identical procedures would apply to both. France offers reciprocity and predictability as reasons to reduce escalation risk. No territorial transfer is included. These are proposed terms, not an agreement in force.",
            }
            if not repeats_history(alternatives[language]):
                return alternatives[language]
        generic = {
            "ru": "В сохранённой переписке нет новых подтверждённых условий или игровых сведений, поэтому прежняя позиция остаётся без изменений. Я не буду выдавать повтор формулировки за новое решение.",
            "uk": "У збереженому листуванні немає нових підтверджених умов або ігрових даних, тому попередня позиція не змінилася. Я не видаватиму повтор формулювання за нове рішення.",
            "fr": "La correspondance conservée ne contient aucune nouvelle condition confirmée ni donnée de jeu; la position précédente reste donc inchangée. Je ne présenterai pas une répétition comme une nouvelle décision.",
            "en": "The saved correspondence contains no newly confirmed terms or game facts, so the previous position remains unchanged. I will not present a repeated wording as a new decision.",
        }
        return generic[language]

    @staticmethod
    def _latest_unresolved_offer(conversation: Conversation, sender: str) -> Optional[DiplomaticMessage]:
        for message in reversed(conversation.messages):
            if message.sender == sender and message.intent in {
                "offer", "negotiate", "demand", "alliance", "non_aggression", "trade",
                "peace", "ceasefire", "military_access", "guarantee", "embargo", "lift_embargo", "counter_offer",
                "docking_rights", "air_base_access", "faction_join", "faction_leave", "market_access", "territory_partition",
                "truce", "white_peace", "peace_conference",
            }:
                return message
        return None

    def _decide(self, intent: str, text: str, relationship: DiplomaticRelationship,
                profile: DiplomaticProfile, conversation: Conversation,
                prior_offer: Optional[DiplomaticMessage],
                doctrine: Optional[Mapping[str, Any]] = None) -> tuple:
        factors = []
        doctrine = doctrine if isinstance(doctrine, Mapping) else {}
        doctrine_adjustment = _number(doctrine.get("score_adjustment"), 0.0, -0.24, 0.24)
        factors.extend(item for item in doctrine.get("factors", [])
                       if isinstance(item, str) and item)
        relation = relationship.relations
        trust = relationship.trust
        balance = relationship.military_balance
        hostility = (
            (100.0 - relation) / 200.0 * 0.21
            + (100.0 - relationship.opinion) / 200.0 * 0.06
            + (1.0 - trust) * 0.24
            + relationship.threat * 0.15
            + relationship.recent_conflict * 0.14
            + max(0.0, -balance) * 0.13
            + profile.aggressiveness * 0.07
        )
        hostility = max(0.0, min(1.0, hostility))
        proposal_hostility = max(0.0, min(1.0, hostility - doctrine_adjustment))
        self._add_context_factors(factors, relationship)
        if trust < 0.35:
            factors.append("low_trust")
        if relationship.promise_trust < 0.35:
            factors.append("low_marker_confirmed_promise_trust")
        if relation < -25:
            factors.append("negative_relations")
        if balance < -0.35:
            factors.append("military_disadvantage")

        if intent == "greeting":
            decision = "accept"
        elif intent == "information":
            # A factual question is answerable as an information request. Keep
            # clarification of an unresolved offer distinct from asking the
            # country to repeat information that is already present in context.
            # This decision is informational only and never executes an offer.
            decision = "counter_offer" if prior_offer else "informational_reply"
            if prior_offer:
                factors.append("clarification_of_existing_offer")
            else:
                factors.append("direct_information_request")
        elif intent == "insult":
            decision = "warn" if profile.dislikes_threats > 0.65 else "reject"
            factors.append("hostile_language")
        elif intent == "declare_war":
            decision = "threaten" if balance > 0.25 else "warn"
            factors.append("war_escalation")
        elif intent == "threaten":
            prior_threats = sum(message.intent == "threaten" for message in conversation.messages[-12:])
            explicit_attack_threat = bool(re.search(
                r"\b(?:or|otherwise|unless)\b.{0,48}\b(?:attack|invade|start a war|declare war)\b|"
                r"\b(?:attack|invade|start a war|declare war)\b.{0,48}\b(?:unless|if you do not|if you don't)\b",
                text, re.IGNORECASE,
            ))
            if prior_threats:
                factors.append("repeated_threat")
            if balance < -0.45 and (trust < 0.45 or relationship.war_status):
                decision = "defer" if profile.risk_tolerance < 0.5 else "counter_offer"
            elif balance > 0.4 and (prior_threats or explicit_attack_threat
                                    or profile.aggressiveness > 0.6):
                decision = "threaten"
                if explicit_attack_threat:
                    factors.append("credible_deterrence_after_explicit_attack_threat")
            else:
                decision = "warn"
            factors.append("threat_received")
        elif intent == "demand":
            demand_openness = (
                trust * 0.38
                + ((relation + 100.0) / 200.0) * 0.28
                + ((relationship.opinion + 100.0) / 200.0) * 0.08
                + profile.negotiation_willingness * 0.14
                + profile.compromise_preference * 0.12
                + (relationship.promise_trust - 0.5) * 0.05
                - relationship.threat * 0.14
                - relationship.recent_conflict * 0.12
                - (0.10 if relationship.war_status else 0.0)
            )
            if proposal_hostility > 0.72:
                decision = "reject"
                factors.append("demand_risk_exceeds_negotiation_space")
            elif proposal_hostility > 0.48:
                decision = "counter_offer" if demand_openness >= 0.34 else "reject"
                factors.append("demand_assessed_from_relations_and_security_context")
            else:
                decision = "counter_offer" if demand_openness >= 0.35 else "defer"
                factors.append("demand_assessed_from_relations_and_security_context")
        elif intent in {"refuse", "accept", "counter_offer", "promise"}:
            if intent == "accept":
                decision = "accept"
            elif intent == "counter_offer":
                decision = "counter_offer"
            elif intent == "promise":
                decision = "accept" if trust > 0.45 else "defer"
            else:
                decision = "defer" if prior_offer else "reject"
            if prior_offer:
                factors.append("prior_negotiation_reference")
        elif intent in {"warn"}:
            decision = "warn" if balance >= -0.4 else "defer"
            factors.append("warning_received")
        else:
            decision = self._decide_proposal(
                intent, relationship, profile, proposal_hostility, factors,
            )

        # Policy assessment is deterministic for an identical game snapshot.
        # Random jitter made borderline treaty decisions change across Retry
        # and process restarts despite unchanged facts.
        jitter = 0.0
        if intent in {"offer", "negotiate", "alliance", "non_aggression", "trade",
                      "military_access", "guarantee", "peace", "ceasefire"}:
            willingness = (
                trust * 0.34 + ((relation + 100.0) / 200.0) * 0.20
                + ((relationship.opinion + 100.0) / 200.0) * 0.04
                + profile.negotiation_willingness * 0.18
                + profile.risk_tolerance * 0.08
                + relationship.economic_dependence * 0.06
                + (0.10 if relationship.alliance_status else 0.0)
                - relationship.threat * 0.14
                - relationship.recent_conflict * 0.12
                - (0.12 if relationship.war_status and intent not in {"peace", "ceasefire"} else 0.0)
                + ((relationship.promise_trust - 0.5) * 0.12)
            )
            if relationship.promise_trust != 0.5:
                factors.append("marker_confirmed_promise_trust_adjustment")
            if intent == "alliance":
                willingness += profile.values_alliances * 0.15
                if relationship.same_faction is True:
                    willingness += 0.1
                    factors.append("already_same_faction")
            if intent == "non_aggression" and relationship.war_status is False:
                willingness += 0.24
                factors.append("verified_peacetime_supports_non_aggression")
            if intent in {"peace", "ceasefire"} and relationship.war_status:
                willingness += max(0.0, -balance) * 0.12 + 0.08
            adjusted = willingness + jitter + doctrine_adjustment
            if decision not in {"counter_offer", "reject"}:
                if adjusted >= 0.61:
                    decision = "accept"
                elif adjusted >= 0.36:
                    decision = "counter_offer"
                elif profile.negotiation_willingness < 0.3:
                    decision = "reject"
                else:
                    decision = "defer"
            if willingness < 0.38:
                factors.append("proposal_risk_exceeds_reward")
            elif willingness >= 0.61:
                factors.append("proposal_aligns_with_interests")

        if intent == "non_aggression" and relationship.war_status is True:
            decision = "counter_offer"
            factors.append("active_war_requires_ceasefire_first")
        elif intent == "non_aggression" and relationship.existing_non_aggression_pact is True:
            decision = "defer"
            factors.append("non_aggression_pact_already_active")
        elif intent == "alliance" and relationship.same_faction is True:
            decision = "defer"
            factors.append("already_same_faction")

        confidence = min(0.96, max(0.56, 0.68 + abs(hostility - 0.5) * 0.25 + abs(jitter) * 0.6))
        return decision, list(dict.fromkeys(factors)), confidence

    @staticmethod
    def _add_context_factors(factors: List[str], relationship: DiplomaticRelationship) -> None:
        if relationship.war_status:
            factors.append("active_war")
        elif relationship.war_status is None:
            factors.append("war_status_unknown")
        if relationship.alliance_status:
            factors.append("existing_alliance")
        if relationship.same_faction is True:
            factors.append("same_faction")
        elif relationship.same_faction is False:
            factors.append("different_faction")
        else:
            factors.append("faction_status_unknown")
        if relationship.existing_non_aggression_pact is True:
            factors.append("existing_non_aggression_pact")
        elif relationship.existing_non_aggression_pact is None:
            factors.append("existing_non_aggression_pact_unknown")
        factors.extend(f"{field}_unknown" for field in relationship.context_unknowns)
        if relationship.recent_conflict >= 0.5:
            factors.append("recent_conflict")
        if relationship.threat >= 0.5:
            factors.append("high_threat_level")
        if relationship.economic_dependence >= 0.6:
            factors.append("economic_interdependence")

    @staticmethod
    def _discussion_topic_requirements(text: str, language: str,
                                       country_name: str) -> Optional[Dict[str, Any]]:
        """Constrain open requests for a national agenda item without inventing game facts."""
        patterns = {
            "ru": (
                r"\b(?:назовите|назови|укажите|укажи|предложите|предложи)\b"
                r".{0,120}\b(?:(?:один|одна|одну|одного|одной)\s+)?"
                r"(?:конкретн\w*\s+)?(?:вопрос\w*|тему|пункт\w*)\b",
                r"\b(?:почему|объясните|объясни|важ\w*|считаете.{0,40}важн\w*)\b",
            ),
            "en": (
                r"\b(?:name|identify|suggest|propose)\b.{0,120}\b"
                r"(?:one\s+)?(?:(?:concrete|specific)\s+)?(?:issue|topic|question)\b",
                r"\b(?:why|explain|matters?\s+to|consider.{0,30}important|important)\b",
            ),
            "fr": (
                r"\b(?:nommez|indiquez|proposez)\b.{0,120}\b"
                r"(?:un\s+|une\s+)?(?:sujet|point|question)\b",
                r"\b(?:pourquoi|expliquez|important\w*|estimez.{0,30}important\w*)\b",
            ),
        }
        pair = patterns.get(language)
        if not pair or not re.search(pair[0], text, re.IGNORECASE | re.DOTALL):
            return None
        if not re.search(pair[1], text, re.IGNORECASE):
            return None
        year_match = re.search(r"\b(?:18|19|20)\d{2}\b", text)
        return {
            "request_type": "one_diplomatic_topic_with_rationale",
            **({"historical_year": int(year_match.group(0))} if year_match else {}),
            "requested_answer_fields": [
                "one concrete topic for international consultations",
                f"why this proposed topic matters to {country_name}",
            ],
            "answer_voice": (
                f"State {country_name}'s own proposed agenda in declarative form; "
                "do not address the speaker or ask them to discuss it."
            ),
            "topic_scope": (
                "Keep the topic relevant to international relations in the HOI4 setting: "
                "security, treaties, alliances, borders, trade/resources, or military policy. "
                "Use another domain only if the player explicitly raises it or verified game facts support it."
            ),
            "factuality_policy": (
                "Known game facts are only those listed in known_game_facts. If no relevant fact is listed, "
                "frame the topic as a proposed diplomatic priority, never as a current threat, event, or treaty."
            ),
        }

    @staticmethod
    def _extract_prior_discussion_terms(text: str, language: str,
                                       source_request_id: Optional[str]) -> List[Dict[str, Any]]:
        """Extract only verbatim, explicitly hypothetical proposals from the prior country turn."""
        if not isinstance(text, str) or not text.strip():
            return []
        proposal_markers = {
            "ru": r"могли\s+бы|готов\w*\s+(?:обсужд\w*|рассмотр\w*)|предлага\w*|"
                  r"можем\s+(?:обсужд\w*|рассмотр\w*)|готов\w*\s+предоставить",
            "uk": r"могли\s+б|готов\w*\s+(?:обговор\w*|розглян\w*)|пропону\w*|"
                  r"можемо\s+(?:обговор\w*|розглян\w*)",
            "fr": r"pourrions|nous proposons|nous offrons|prêts? à (?:discuter|examiner)|"
                  r"nous pourrions",
            "en": r"could\s+(?:discuss|consider|offer|provide)|would\s+(?:discuss|offer)|"
                  r"we\s+(?:propose|offer)|(?:ready|prepared)\s+to\s+discuss",
        }.get(normalize_language(language) or "")
        if not proposal_markers:
            return []
        sentences = re.split(r"(?<=[.!?])\s+|\n+", text.strip())
        terms = []
        for sentence in sentences:
            candidate = re.sub(r"\s+", " ", sentence).strip()
            if not candidate or len(candidate) > 360:
                continue
            if not re.search(proposal_markers, candidate, re.IGNORECASE):
                continue
            terms.append({
                "term_id": f"term_{len(terms) + 1}",
                "text": candidate,
                "source_request_id": source_request_id,
            })
            if len(terms) >= 4:
                break
        return terms

    @staticmethod
    def _national_position_requirements(text: str, language: str, country_name: str,
                                        counterpart_name: str) -> Dict[str, Any]:
        """Specify an open, non-binding request for the country's own aims and terms."""
        requested_fields = [
            f"one or two plausible strategic aims of {country_name} in relations with {counterpart_name}",
            "two concrete reciprocal terms or diplomatic steps the country would be willing to discuss",
            "a short national-interest reason for those terms",
            "one limited concession or area of flexibility, if compatible with the known context",
        ]
        return {
            "request_type": "national_position_and_terms",
            "requested_answer_fields": requested_fields,
            "answer_voice": f"Speak as {country_name}, in first person, about its own interests.",
            "topic_scope": "Bilateral diplomatic discussion only; no treaty vote or game action requested.",
            "factuality_policy": (
                "Distinguish verified current game observations from inference. Ideology is only a broad tendency. "
                "Where no current fact supports a claim, offer a plausible hypothetical negotiating position, "
                "not an official policy, existing demand, promise, agreement, or executed action."
            ),
            "authority": "nonbinding_discussion_only",
        }

    @staticmethod
    def _national_position_unavailable_text(language: str) -> str:
        language = normalize_language(language) or "en"
        return {
            "ru": "Сейчас не удалось подготовить надёжный ответ о целях и возможных условиях Германии. Это не означает согласия или отказа по какому-либо договору.",
            "en": "A reliable statement of Germany's aims and possible terms could not be prepared this time. This is not consent to or rejection of any agreement.",
            "fr": "Il n’a pas été possible de formuler une position fiable sur les objectifs et conditions possibles de l’Allemagne. Cela ne vaut ni accord ni refus d’un traité.",
            "uk": "Зараз не вдалося підготувати надійну відповідь про цілі та можливі умови Німеччини. Це не означає згоди чи відмови щодо будь-якої угоди.",
        }.get(language, "A reliable national position could not be prepared this time; no agreement status was changed.")

    @staticmethod
    def _verified_snapshot_requirements(text: str, language: str,
                                        game_context: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """Detect direct questions about stored game observations, not treaty terms."""
        patterns = {
            "ru": (
                r"сним\w*|наблюдени\w*|записанн\w*\s+(?:состояни\w*|данн\w*)",
                r"что\s+(?:известно|зафиксировано|подтверждено)|что\s+оста[её]тся\s+неподтвержд\w*|"
                r"перечисл\w*|укаж\w*|какие\s+(?:факты|данные|показател\w*)",
            ),
            "en": (
                r"snapshot|recorded\s+(?:observation|state|facts?)|game\s+observation",
                r"what\s+(?:is|was)\s+(?:known|recorded|confirmed)|what\s+remains\s+unconfirmed|"
                r"summari[sz]e|list\s+(?:the\s+)?(?:known|recorded)",
            ),
            "fr": (
                r"instantan[eé]|observation\s+enregistr[eé]e|[eé]tat\s+enregistr[eé]",
                r"que\s+(?:sait-on|est\s+connu|a\s+[eé]t[eé]\s+enregistr[eé])|"
                r"quelles?\s+(?:donn[eé]es|informations)|r[eé]sume[zr]?|indique[zr]?",
            ),
            "uk": (
                r"знім\w*|спостереженн\w*|записан\w*\s+(?:стан\w*|дан\w*)",
                r"що\s+(?:відомо|зафіксовано|підтверджено)|що\s+залишається\s+непідтвердж\w*|"
                r"переліч\w*|вкаж\w*|які\s+(?:факти|дані|показник\w*)",
            ),
        }
        pair = patterns.get(language)
        if pair is None or not re.search(pair[0], text, re.I) or not re.search(pair[1], text, re.I):
            return None
        allowed_fields = (
            "date", "game_date", "war_status", "same_faction", "faction_status",
            "existing_non_aggression_pact", "relations", "opinion", "threat",
            "military_balance", "alliance_status", "current_state_owners", "claims",
            "ideology", "ideology_group", "initiator_exists", "target_exists",
            "initiator_owned_states", "target_owned_states", "initiator_divisions",
            "target_divisions", "initiator_factories", "target_factories", "target_stability",
        )
        present = [key for key in allowed_fields
                   if key in game_context and game_context[key] is not None
                   and not (isinstance(game_context[key], str)
                            and game_context[key].strip().lower() == "unknown")]
        return {
            "request_type": "verified_snapshot_summary",
            "requested_answer_fields": present,
            "answer_voice": (
                "Answer as a concise factual report about the recorded game observation, not as a treaty position."
            ),
            "factuality_policy": (
                "State every available field listed in known_game_facts with its supplied value and observation date. "
                "Only fields absent from known_game_facts are unknown. A recorded value is not proof of a later state."
            ),
        }

    @staticmethod
    def _compose_verified_snapshot_summary(game_context: Mapping[str, Any], language: str,
                                           initiator_tag: str, target_tag: str) -> str:
        """Report only exact fields from an internally verified Bridge observation."""
        language = normalize_language(language) or "en"
        labels = {
            "ru": {
                "prefix": "По последнему зафиксированному игровому снимку HOI4-EYES",
                "date": "дата",
                "relations": "значение отношений между сторонами",
                "threat": "значение мировой угрозы",
                "war_yes": "между сторонами отмечена война",
                "war_no": "война между сторонами не отмечена",
                "war_unknown": "состояние войны неизвестно",
                "faction_yes": "стороны указаны в одной фракции",
                "faction_no": "общая фракция не указана",
                "faction_unknown": "сведения об общей фракции неизвестны",
                "nap_yes": "пакт о ненападении отмечен как действующий",
                "nap_no": "действующий пакт о ненападении не отмечен",
                "nap_unknown": "наличие пакта неизвестно",
                "unknown": "неизвестно",
                "states": "штатов",
                "divisions": "дивизий",
                "factories": "фабрик",
                "stability": "стабильность цели",
                "initiator": "инициатор",
                "target": "цель",
                "exists_yes": "существует",
                "exists_no": "не существует",
                "tail": "Это данные только на указанное время; они не подтверждают более позднее состояние.",
                "empty": "Снимок не содержит доступных для ответа значений.",
            },
            "uk": {
                "prefix": "За останнім зафіксованим ігровим знімком HOI4-EYES",
                "date": "дата",
                "relations": "значення відносин між сторонами",
                "threat": "значення світової напруги",
                "war_yes": "між сторонами зафіксовано війну",
                "war_no": "війну між сторонами не зафіксовано",
                "war_unknown": "стан війни невідомий",
                "faction_yes": "сторони вказані в одній фракції",
                "faction_no": "спільну фракцію не вказано",
                "faction_unknown": "дані про спільну фракцію невідомі",
                "nap_yes": "пакт про ненапад позначений як чинний",
                "nap_no": "чинний пакт про ненапад не зафіксовано",
                "nap_unknown": "наявність пакту невідома",
                "unknown": "невідомо",
                "states": "штатів",
                "divisions": "дивізій",
                "factories": "фабрик",
                "stability": "стабільність цілі",
                "initiator": "ініціатор",
                "target": "ціль",
                "exists_yes": "існує",
                "exists_no": "не існує",
                "tail": "Це дані лише на вказаний час; вони не підтверджують пізніший стан.",
                "empty": "Знімок не містить доступних для відповіді значень.",
            },
            "fr": {
                "prefix": "Selon le dernier relevé de jeu HOI4-EYES enregistré",
                "date": "date",
                "relations": "valeur des relations entre les deux pays",
                "threat": "valeur de la tension mondiale",
                "war_yes": "une guerre entre les deux pays est enregistrée",
                "war_no": "aucune guerre entre les deux pays n’est enregistrée",
                "war_unknown": "l’état de guerre est inconnu",
                "faction_yes": "les deux pays sont indiqués dans la même faction",
                "faction_no": "aucune faction commune n’est indiquée",
                "faction_unknown": "les données de faction commune sont inconnues",
                "nap_yes": "un pacte de non-agression est indiqué comme actif",
                "nap_no": "aucun pacte de non-agression actif n’est indiqué",
                "nap_unknown": "la présence d’un pacte est inconnue",
                "unknown": "inconnu",
                "states": "États",
                "divisions": "divisions",
                "factories": "usines",
                "stability": "stabilité de la cible",
                "initiator": "initiateur",
                "target": "cible",
                "exists_yes": "existe",
                "exists_no": "n’existe pas",
                "tail": "Ces données ne décrivent que la date indiquée et ne confirment aucun état ultérieur.",
                "empty": "Le relevé ne contient aucune valeur disponible pour cette réponse.",
            },
            "en": {
                "prefix": "According to the latest recorded HOI4-EYES game snapshot",
                "date": "date",
                "relations": "bilateral relations value",
                "threat": "world tension value",
                "war_yes": "a war between the countries is recorded",
                "war_no": "no war between the countries is recorded",
                "war_unknown": "war status is unknown",
                "faction_yes": "the countries are recorded in the same faction",
                "faction_no": "no shared faction is recorded",
                "faction_unknown": "shared faction status is unknown",
                "nap_yes": "a non-aggression pact is recorded as active",
                "nap_no": "no active non-aggression pact is recorded",
                "nap_unknown": "pact status is unknown",
                "unknown": "unknown",
                "states": "states",
                "divisions": "divisions",
                "factories": "factories",
                "stability": "target stability",
                "initiator": "initiator",
                "target": "target",
                "exists_yes": "exists",
                "exists_no": "does not exist",
                "tail": "These values apply only to the shown date and do not confirm a later state.",
                "empty": "The snapshot contains no values available for this answer.",
            },
        }[language]

        lines = [labels["prefix"]]
        if game_context.get("date") is not None:
            lines.append(f"{labels['date']}: {game_context['date']}.")
        if game_context.get("relations") is not None:
            lines.append(f"{labels['relations']}: {game_context['relations']}.")
        if game_context.get("threat") is not None:
            lines.append(f"{labels['threat']}: {game_context['threat']}.")
        for field, yes_key, no_key, unknown_key in (
            ("war_status", "war_yes", "war_no", "war_unknown"),
            ("same_faction", "faction_yes", "faction_no", "faction_unknown"),
            ("existing_non_aggression_pact", "nap_yes", "nap_no", "nap_unknown"),
        ):
            value = str(game_context.get(field, "unknown")).strip().lower()
            lines.append(labels[yes_key] if value == "yes" else
                         labels[no_key] if value == "no" else labels[unknown_key])
        for field, noun in (
            ("initiator_owned_states", "states"), ("target_owned_states", "states"),
            ("initiator_divisions", "divisions"), ("target_divisions", "divisions"),
            ("initiator_factories", "factories"), ("target_factories", "factories"),
        ):
            if game_context.get(field) is not None:
                side = "initiator" if field.startswith("initiator_") else "target"
                tag = initiator_tag if side == "initiator" else target_tag
                lines.append(f"{labels[side]} {tag}: {game_context[field]} {labels[noun]}.")
        for field, side in (("initiator_exists", "initiator"), ("target_exists", "target")):
            if game_context.get(field) is not None:
                value = str(game_context[field]).strip().lower()
                existence = labels["exists_yes"] if value == "yes" else (
                    labels["exists_no"] if value == "no" else labels["unknown"]
                )
                tag = initiator_tag if side == "initiator" else target_tag
                lines.append(f"{labels[side]} {tag} {existence}.")
        if game_context.get("target_stability") is not None:
            lines.append(f"{labels['stability']}: {game_context['target_stability']}.")
        if len(lines) == 1:
            lines.append(labels["empty"])
        lines.append(labels["tail"])
        return " ".join(lines)

    def _decide_proposal(self, intent: str, relationship: DiplomaticRelationship,
                         profile: DiplomaticProfile, hostility: float,
                         factors: List[str]) -> str:
        if intent in {"unknown/general", "request"}:
            factors.append("intent_needs_clarification")
            return "request_more_information"
        if intent == "greeting":
            return "accept"
        if hostility > 0.78:
            return "reject"
        if hostility > 0.58:
            return "counter_offer" if profile.negotiation_willingness >= 0.55 else "defer"
        return "counter_offer" if profile.negotiation_willingness < 0.35 else "accept"

    def _compose_response(self, intent: str, decision: str, country: str, sender: str,
                          original_text: str, prior_offer: Optional[DiplomaticMessage],
                          profile: DiplomaticProfile, relationship: DiplomaticRelationship,
                          language: str = "en",
                          previous_country_response: Optional[DiplomaticMessage] = None) -> str:
        variants = self._responses_for_language(language)
        if intent == "greeting":
            return {
                "ru": f"{country} приветствует вас и готова продолжить прямой дипломатический диалог.",
                "uk": f"{country} вітає вас і готова продовжити прямий дипломатичний діалог.",
                "fr": f"{country} vous salue et souhaite poursuivre un dialogue diplomatique direct.",
                "en": f"{country} welcomes you and is ready to continue direct diplomatic talks.",
            }[language]
        if intent == "information" and prior_offer:
            if previous_country_response and previous_country_response.decision == "counter_offer":
                return self._clarify_counter_offer(prior_offer.intent, country, language)
            if previous_country_response and previous_country_response.decision == "reject":
                return {
                    "ru": "Наш предыдущий отказ относился к требованию передать территорию. Мы не принимаем это условие, но готовы обсуждать взаимоприемлемую альтернативу.",
                    "uk": "Наша попередня відмова стосувалася вимоги передати територію. Ми не приймаємо цю умову, але готові обговорити взаємоприйнятну альтернативу.",
                    "fr": "Notre refus précédent concernait la demande de céder un territoire. Nous ne pouvons pas accepter cette condition, mais restons prêts à discuter d'une solution réciproque.",
                    "en": "Our earlier refusal concerned the demand to transfer territory. We cannot accept that condition, but remain willing to discuss a reciprocal alternative.",
                }[language]
            return {
                "ru": f"{country} готова разъяснить своё предложение. Укажите, пожалуйста, какой именно пункт требует уточнения; отсутствующие игровые сведения мы не будем считать подтверждёнными.",
                "uk": f"{country} готова пояснити свою пропозицію. Уточніть, будь ласка, який саме пункт незрозумілий; непідтверджені ігрові відомості не вважатимемо фактами.",
                "fr": f"{country} est prête à préciser sa proposition. Indiquez le point à clarifier; nous ne présenterons pas les informations de jeu manquantes comme des faits établis.",
                "en": f"{country} is ready to clarify its proposal. Please identify the point in question; missing game information will not be treated as established fact.",
            }[language]
        if intent == "information":
            if decision == "informational_reply":
                return {
                    "ru": f"{country} отвечает на вопрос одной предлагаемой дипломатической темой и объясняет её значение для национальных интересов; это не согласие по договору.",
                    "uk": f"{country} відповідає однією запропонованою дипломатичною темою та пояснює її значення для національних інтересів; це не згода щодо договору.",
                    "fr": f"{country} répond par un sujet diplomatique proposé et explique son importance pour ses intérêts nationaux; cela ne vaut pas accord sur un traité.",
                    "en": f"{country} answers with one proposed diplomatic topic and explains its importance to national interests; this is not agreement to a treaty.",
                }[language]
            return variants["request_more_information"].format(country=country, sender=sender)

        if decision == "counter_offer":
            if intent == "non_aggression" and relationship.war_status is True:
                return {
                    "ru": f"{country} не может обсуждать пакт о ненападении, пока стороны находятся в состоянии войны. Сначала нужно согласовать прекращение огня.",
                    "uk": f"{country} не може обговорювати пакт про ненапад, поки сторони перебувають у стані війни. Спершу потрібно узгодити припинення вогню.",
                    "fr": f"{country} ne peut pas négocier un pacte de non-agression tant que les pays sont en guerre. Il faut d'abord convenir d'un cessez-le-feu.",
                    "en": f"{country} cannot negotiate a non-aggression pact while the countries are at war. A ceasefire must be agreed first.",
                }[language]
            return self._counter_offer(intent, country, language, profile)
        if decision == "accept":
            if intent == "threaten":
                decision = "defer"
            else:
                return variants["accept"].format(country=country, sender=sender, subject=self._subject(intent, language))
        if decision == "reject":
            return variants["reject"].format(country=country, sender=sender, subject=self._subject(intent, language))
        if decision == "defer":
            return variants["defer"].format(country=country, sender=sender, subject=self._subject(intent, language))
        if decision == "warn":
            return variants["warn"].format(country=country, sender=sender)
        if decision == "threaten":
            return variants["threaten"].format(country=country, sender=sender)
        return variants["request_more_information"].format(country=country, sender=sender)

    @staticmethod
    def _compose_refusal_followup(country: str, language: str) -> str:
        """Provide a nonbinding seed for a question about a prior refusal."""
        phrases = {
            "en": (f"{country} acknowledges the earlier refusal. No reason for that refusal was recorded "
                   "in the exchange; a nonbinding alternative may still be discussed. This does not reopen "
                   "or accept the rejected offer."),
            "ru": (f"{country} подтверждает, что прежнее предложение было отклонено. Причина отказа "
                   "в переписке не указана; необязательную альтернативу можно обсудить отдельно. "
                   "Это не возобновляет и не принимает отклонённое предложение."),
            "uk": (f"{country} підтверджує, що попередню пропозицію було відхилено. Причину відмови "
                   "у листуванні не зазначено; необов'язкову альтернативу можна обговорити окремо. "
                   "Це не поновлює й не приймає відхилену пропозицію."),
            "fr": (f"{country} confirme son refus antérieur. Aucun motif n'a été consigné dans l'échange ; "
                   "une solution non contraignante peut être discutée séparément. Ce message ne rouvre ni "
                   "n'accepte l'offre rejetée."),
        }
        return phrases.get(language, phrases["en"])

    @staticmethod
    def _has_recorded_refusal_reason(text: str) -> bool:
        """Recognize an explicit rationale in the stored refusal, not an offer term by itself."""
        normalized = re.sub(r"\s+", " ", str(text or "")).casefold()
        if not normalized or re.search(r"without assuming (?:an? )?ideal", normalized):
            return False
        return bool(re.search(
            r"\b(?:because|due to|owing to|our reason is|the reason is|based on our|"
            r"concern(?:s|ed)? about|risk of|fear of|threat to|principle of|"
            r"not in (?:our|france'?s|germany'?s) interest)\b|"
            r"(?:потому что|из-за|в связи с|причин\w* отказ\w*|опаса\w* за|угроз\w* для)|"
            r"(?:parce que|en raison de|notre raison|motif du refus|préoccupation)\b|"
            r"(?:тому що|через|причин\w* відмов\w*|занепокоєн\w* щодо)",
            normalized, re.IGNORECASE,
        ))

    @staticmethod
    def _clarify_counter_offer(intent: str, country: str, language: str) -> str:
        if intent == "non_aggression":
            return {
                "ru": (f"{country} предлагает обсудить три взаимных условия. (1) Письменное обязательство обеих сторон не нападать друг на друга: предполагаемая выгода для Франции — взаимность и меньше неопределённости относительно намерений партнёра. "
                       "(2) Предварительные уведомления и консультации о мерах, которые другая сторона считает существенными для безопасности: это даёт возможность поднять опасения до принятия меры и снижает риск недопонимания. "
                       "(3) Одинаковые правила уведомления и пересмотра для обеих сторон: так условия не становятся односторонними. "
                       "Это предложенные мотивы и условия, а не подтверждённая оценка текущих угроз или официальная политика Франции. "
                       "Территориальных уступок в этой встречной оферте Франция не требует. Это условия для обсуждения, не действующий договор и не свершившееся игровое действие."),
                "uk": (f"{country} пропонує обговорити три взаємні умови. (1) Письмове зобов'язання обох сторін не нападати одна на одну: очікувана вигода для Франції — взаємність і менша невизначеність щодо намірів партнера. "
                       "(2) Завчасні повідомлення й консультації щодо заходів, які інша сторона вважає важливими для безпеки: це дає змогу висловити занепокоєння до їх ухвалення та зменшує ризик непорозуміння. "
                       "(3) Однакові правила повідомлення й перегляду для обох сторін, щоб зобов'язання не були односторонніми. "
                       "Це запропоноване обґрунтування, а не підтверджена оцінка поточних загроз чи офіційна політика Франції. "
                       "Територіальних поступок у цій зустрічній пропозиції Франція не вимагає. Це умови для обговорення, а не чинний договір чи здійснена ігрова дія."),
                "fr": (f"{country} propose trois conditions réciproques. (1) Un engagement écrit de non-agression des deux côtés : l'intérêt proposé pour la France serait la réciprocité et une moindre incertitude sur les intentions du partenaire. "
                       "(2) Un préavis et des consultations sur les mesures que l'autre partie juge importantes pour sa sécurité : cela permettrait de soulever les préoccupations avant leur adoption et de réduire les malentendus. "
                       "(3) Les mêmes règles de notification et de réexamen pour éviter des obligations à sens unique. "
                       "Ce sont des justifications proposées, pas une évaluation vérifiée des menaces ni une politique officielle française. "
                       "Aucune concession territoriale n'est demandée dans cette contre-proposition. Ce sont des termes à négocier, pas un traité en vigueur ni une action exécutée."),
                "en": (f"{country} proposes three reciprocal terms. (1) A written no-attack commitment by both sides: the proposed benefit for France is reciprocity and less uncertainty about the other party's intentions. "
                       "(2) Advance notice and consultation on measures the other side considers important to its security: this allows concerns to be raised before action and may reduce misunderstanding. "
                       "(3) The same notification and review rules for both sides, preventing one-sided obligations. "
                       "These are proposed rationales, not a verified assessment of current threats or official French policy. "
                       "This counter-offer asks for no territorial concession. These are negotiating terms, not an active treaty or completed game action."),
            }[language]
        return {
            "ru": f"{country} готова пояснить встречное предложение: {country} предлагает уточнить взаимные условия и порядок консультаций до принятия решения.",
            "uk": f"{country} готова пояснити зустрічну пропозицію: {country} пропонує узгодити взаємні умови та порядок консультацій до ухвалення рішення.",
            "fr": f"{country} précise sa contre-proposition : {country} propose de définir les conditions réciproques et les consultations avant toute décision.",
            "en": f"{country} clarifies its counter-offer: {country} proposes defining reciprocal terms and consultations before a decision is made.",
        }[language]

    @staticmethod
    def _responses_for_language(language: str) -> Dict[str, str]:
        if language == "ru":
            return DiplomacyEngine._russian_responses()
        if language == "en":
            return DiplomacyEngine._english_responses()
        common = {
            "uk": {
                "accept": "{country} готова прийняти {subject} за умови дотримання погоджених зобов'язань.",
                "reject": "{country} не може прийняти {subject}: запропоновані умови не відповідають нашим інтересам.",
                "defer": "{country} поки не готова вирішити питання про {subject}. Нам потрібно ретельно оцінити умови та наслідки.",
                "warn": "{country} застерігає {sender}: подальша ескалація ускладнить дипломатичне врегулювання.",
                "threaten": "{country} захищатиме свої інтереси, якщо {sender} продовжить цей курс. Ми все ще віддаємо перевагу дипломатичному рішенню.",
                "request_more_information": "{country} готова розглянути пропозицію, але потребує конкретних умов і гарантій.",
            },
            "fr": {
                "accept": "{country} est prête à accepter {subject}, sous réserve du respect des engagements convenus.",
                "reject": "{country} ne peut pas accepter {subject}; les conditions proposées ne servent pas nos intérêts.",
                "defer": "{country} ne peut pas encore décider au sujet de {subject}. Nous devons examiner attentivement les conditions et leurs conséquences.",
                "warn": "{country} met {sender} en garde : toute nouvelle escalade compliquerait un règlement diplomatique.",
                "threaten": "{country} défendra ses intérêts si {sender} poursuit cette voie. Nous préférons toujours un règlement diplomatique.",
                "request_more_information": "{country} est prête à étudier la proposition, mais demande des conditions et garanties précises.",
            },
        }
        return common[language]

    @staticmethod
    def _subject(intent: str, language: str) -> str:
        subjects = {
            "alliance": ("союз", "an alliance"),
            "non_aggression": ("договор о ненападении", "a non-aggression pact"),
            "trade": ("торговое соглашение", "a trade agreement"),
            "military_access": ("военный доступ", "military access"),
            "guarantee": ("гарантии", "guarantees"),
            "peace": ("мирное соглашение", "a peace agreement"),
            "ceasefire": ("перемирие", "a ceasefire"),
            "demand": ("это требование", "this demand"),
            "offer": ("ваше предложение", "your proposal"),
            "negotiate": ("переговоры", "negotiations"),
            "request": ("вашу просьбу", "your request"),
        }
        localized = {
            "uk": {"alliance": "союз", "non_aggression": "пакт про ненапад", "trade": "торговельну угоду",
                   "guarantee": "гарантії", "peace": "мирну угоду", "ceasefire": "припинення вогню",
                   "demand": "цю вимогу", "offer": "вашу пропозицію", "negotiate": "переговори",
                   "request": "ваше прохання"},
            "fr": {"alliance": "une alliance", "non_aggression": "un pacte de non-agression", "trade": "un accord commercial",
                   "guarantee": "des garanties", "peace": "un accord de paix", "ceasefire": "un cessez-le-feu",
                   "demand": "cette demande", "offer": "votre proposition", "negotiate": "les négociations",
                   "request": "votre demande"},
        }
        if language in {"ru", "en"}:
            pair = subjects.get(intent, ("это предложение", "this proposal"))
            return pair[0] if language == "ru" else pair[1]
        return localized[language].get(intent, "цю пропозицію" if language == "uk" else "cette proposition")

    @staticmethod
    def _counter_offer(intent: str, country: str, language: str, profile: DiplomaticProfile) -> str:
        if language == "ru":
            if intent == "demand":
                return f"{country} не может принять это требование в предложенном виде. Вместо уступки мы готовы обсудить взаимные гарантии или торговое соглашение."
            if intent == "non_aggression":
                return f"{country} готова обсуждать договор о ненападении при условии взаимных гарантий, прозрачного контроля и равных обязательств."
            if intent in {"peace", "ceasefire"}:
                return f"{country} готова обсуждать прекращение боевых действий при немедленном прекращении огня и согласованном механизме контроля."
            if intent == "military_access":
                return f"{country} может рассмотреть ограниченный и временный доступ по заранее согласованному маршруту."
            if intent == "trade":
                return f"{country} готова обсуждать торговлю на взаимной основе с конкретными объёмами и гарантиями поставок."
            if intent == "alliance":
                return f"{country} предпочитает начать с консультаций и оборонной координации, прежде чем заключать полный союз."
            return f"{country} готова продолжить переговоры, если стороны уточнят условия и взаимные гарантии."
        if language == "uk":
            if intent == "demand":
                return f"{country} не може прийняти цю вимогу в запропонованому вигляді. Натомість ми готові обговорити взаємні гарантії або торгівлю."
            if intent == "non_aggression":
                return f"{country} готова обговорювати пакт про ненапад за умови взаємних гарантій, прозорих консультацій та однакових зобов'язань."
            if intent in {"peace", "ceasefire"}:
                return f"{country} готова обговорити припинення бойових дій із негайним припиненням вогню та спільно погодженим контролем."
            if intent == "military_access":
                return f"{country} може розглянути обмежений у часі доступ узгодженим заздалегідь маршрутом."
            if intent == "trade":
                return f"{country} готова обговорити взаємну торгівлю з визначеними обсягами та гарантіями постачання."
            if intent == "alliance":
                return f"{country} пропонує почати з консультацій та оборонної координації, перш ніж розглядати повний союз."
            return f"{country} готова продовжити переговори, якщо сторони уточнять умови та взаємні гарантії."
        if language == "fr":
            if intent == "demand":
                return f"{country} ne peut pas accepter cette demande en l'état. Nous sommes prêts à discuter de garanties réciproques ou d'un accord commercial."
            if intent == "non_aggression":
                return f"{country} est prête à négocier un pacte de non-agression assorti de garanties réciproques, de consultations transparentes et d'obligations égales."
            if intent in {"peace", "ceasefire"}:
                return f"{country} est prête à discuter d'une fin des hostilités, avec un cessez-le-feu immédiat et un mécanisme de suivi convenu ensemble."
            if intent == "military_access":
                return f"{country} peut envisager un accès limité dans le temps, par un itinéraire convenu à l'avance."
            if intent == "trade":
                return f"{country} est ouverte à des échanges réciproques avec des volumes définis et des garanties d'approvisionnement."
            if intent == "alliance":
                return f"{country} propose de commencer par des consultations et une coordination défensive avant d'envisager une alliance complète."
            return f"{country} est prête à poursuivre les négociations si les deux parties précisent les conditions et les garanties réciproques."
        if intent == "demand":
            return f"{country} cannot accept this demand as stated. We are prepared to discuss reciprocal guarantees or a trade agreement instead."
        if intent == "non_aggression":
            return f"{country} is prepared to discuss a non-aggression pact, provided it includes reciprocal guarantees, transparent verification, and equal obligations."
        if intent in {"peace", "ceasefire"}:
            return f"{country} is prepared to discuss ending hostilities, beginning with an immediate ceasefire and a mutually agreed monitoring process."
        if intent == "military_access":
            return f"{country} could consider limited, temporary access along a route agreed in advance."
        if intent == "trade":
            return f"{country} is open to reciprocal trade talks with defined volumes and reliable supply guarantees."
        if intent == "alliance":
            return f"{country} proposes beginning with consultations and defensive coordination before considering a full alliance."
        return f"{country} is willing to continue negotiations if both sides clarify the terms and reciprocal guarantees."

    @staticmethod
    def _english_responses() -> Dict[str, str]:
        return {
            "accept": "{country} is prepared to accept {subject}, subject to the agreed terms being respected.",
            "reject": "{country} cannot accept {subject}. The current terms do not serve our national interests.",
            "defer": "{country} will not decide on {subject} yet. We need to review the consequences and the terms with care.",
            "warn": "{country} advises {sender} not to escalate this matter. Further pressure would damage the prospects for a settlement.",
            "threaten": "{country} will defend its interests if {sender} continues this course. We still prefer a diplomatic settlement.",
            "request_more_information": "{country} is willing to consider this, but needs specific terms and guarantees before taking a position.",
        }

    @staticmethod
    def _russian_responses() -> Dict[str, str]:
        return {
            "accept": "{country} готова принять: {subject}, при условии соблюдения согласованных обязательств.",
            "reject": "{country} не может принять: {subject}. Предложенные условия не отвечают нашим национальным интересам.",
            "defer": "{country} пока не готова принять решение по вопросу: {subject}. Нам нужно оценить последствия и условия.",
            "warn": "{country} предупреждает {sender}: дальнейшая эскалация осложнит достижение соглашения.",
            "threaten": "{country} будет защищать свои интересы, если {sender} продолжит этот курс. Мы всё ещё предпочитаем дипломатическое решение.",
            "request_more_information": "{country} готова рассмотреть вопрос, но сначала нужны конкретные условия и гарантии.",
        }

    @staticmethod
    def _significance(intent: str, decision: str) -> str:
        if intent in {"threaten", "declare_war", "peace", "ceasefire", "truce", "white_peace", "peace_conference", "territory_partition"}:
            return "high"
        if intent in {"demand", "alliance", "non_aggression", "guarantee", "military_access",
                      "docking_rights", "air_base_access", "faction_join", "faction_leave", "market_access"}:
            return "medium"
        return "low"

    @staticmethod
    def _tone(decision: str) -> str:
        return {
            "accept": "constructive", "reject": "firm", "counter_offer": "pragmatic",
            "defer": "cautious", "warn": "warning", "threaten": "firm",
            "request_more_information": "inquisitive",
            "informational_reply": "neutral",
        }.get(decision, "neutral")

    @staticmethod
    def _update_relationship(relationship: DiplomaticRelationship, decision: str, intent: str) -> None:
        if decision == "accept":
            relationship.trust = min(1.0, relationship.trust + 0.04)
            relationship.relations = min(100.0, relationship.relations + 3.0)
            if intent == "alliance":
                relationship.alliance_status = True
            if intent in {"peace", "ceasefire"}:
                relationship.war_status = False
                relationship.recent_conflict = max(0.0, relationship.recent_conflict - 0.2)
        elif decision == "counter_offer":
            relationship.trust = min(1.0, relationship.trust + 0.01)
        elif decision == "reject":
            relationship.relations = max(-100.0, relationship.relations - 2.0)
        elif decision == "warn":
            relationship.threat = min(1.0, relationship.threat + 0.04)
            relationship.relations = max(-100.0, relationship.relations - 1.0)
        elif decision == "threaten":
            relationship.threat = min(1.0, relationship.threat + 0.12)
            relationship.relations = max(-100.0, relationship.relations - 4.0)
            relationship.recent_conflict = min(1.0, relationship.recent_conflict + 0.05)


__all__ = ["DECISIONS", "INTENTS", "DiplomacyEngine", "DiplomaticMessage", "DiplomaticProfile", "DiplomaticRelationship", "IntentClassifier"]
