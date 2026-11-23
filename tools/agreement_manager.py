"""Conservative diplomatic agreement extraction and execution package helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from copy import deepcopy
import hashlib
import json
import re
import uuid
from collections.abc import Mapping

from action_registry import ActionRegistry
from hoi4_country_tags import COUNTRY_TAG_PATTERN, COUNTRY_TAG_RE
from hoi4_country_names import load_country_names


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def consent_fingerprint(record: dict) -> str:
    """Stable semantic fingerprint for agreement confirmations and their sources."""
    fields = ("status", "confirmed_by", "confirmed_by_request_ids",
              "confirmation_source_request_ids", "inherited_confirmations",
              "proposal_source_request_id", "proposal_spec", "conditional_by",
              "required_by", "rejected_by", "position_request_ids", "participant_positions")
    return json.dumps({
        "terms": [{key: term.get(key) for key in fields}
                  for term in record.get("terms", [])],
        "participant_acceptance_request_ids": record.get("participant_acceptance_request_ids"),
        "participant_term_confirmation_request_ids": record.get(
            "participant_term_confirmation_request_ids"),
        "participant_conditional_acceptance_request_ids": record.get(
            "participant_conditional_acceptance_request_ids"),
        "participant_linked_proposal_request_ids": record.get(
            "participant_linked_proposal_request_ids"),
        "participant_linked_proposal_versions": record.get(
            "participant_linked_proposal_versions"),
        "negotiation_positions": record.get("negotiation_positions"),
        "unresolved_items": record.get("unresolved_items"),
        "interpreted_actions": record.get("interpreted_actions"),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


_CONDITIONAL = re.compile(
    r"\b(if|provided that|subject to|unless)\b|при\s+услови|\bесли\b|"
    r"\bякщо\b|за\s+умови|à\s+condition|sous\s+réserve|"
    r"только\s+условно|\bconditionally\b|\bonly if\b|условн\w*|"
    r"\b(?:agree|agrees|willing)\s+to\s+(?:consider|review|discuss)\b|"
    r"согласн\w*\s+(?:рассмотреть|обсудить)|готов\w*\s+(?:рассмотреть|обсудить)|"
    r"^\s*(?:(?:я|мы)\s+)?(?:соглас(?:ен|на|ны)|принима(?:ю|ем)|подтвержда(?:ю|ем))"
    r"(?:\s+(?:(?:эти|все)\s+)?условия)?\s*[,—-]?\s+но\b|"
    r"^\s*(?:(?:i|we)\s+(?:agree|accept|confirm)(?:\s+(?:the|these)\s+terms)?\s*[,—-]?\s+but\b|"
    r"(?:j['’]accepte|je confirme|nous acceptons|nous confirmons|(?:je suis|nous sommes) d['’]accord)"
    r"(?:\s+les\s+conditions)?\s*[,—-]?\s+mais\b)|"
    r"(?:but|но|mais|але)\s+(?:first\s+)?(?:requires?|requires? that|demands?|требу\w*|сначала\s+требу\w*)",
    re.IGNORECASE,
)
_HEDGED_CONSIDERATION = re.compile(
    r"\b(?:agree|agrees|willing)\s+to\s+(?:consider|review|discuss)\b|"
    r"согласн\w*\s+(?:рассмотреть|обсудить)|готов\w*\s+(?:рассмотреть|обсудить)|"
    r"согласн\w*.{0,35}\bрассмотр\w*|готов\w*.{0,35}\bрассмотр\w*",
    re.IGNORECASE,
)
_PLAYER_ACCEPTANCE = re.compile(
    r"\b(i accept|we accept|germany accepts|accept all|i agree|we agree)\b|"
    r"\b(?:germany|i|we)\s+(?:(?:explicitly|hereby)\s+)?confirm(?:s|ed)?\b|"
    r"\bgermany\s+revises?\s+(?:point|term)\s+\d+\b.{0,140}\band\s+explicitly\s+"
    r"confirms?\s+this\s+(?:revised\s+)?term\b|"
    r"германия\s+принимает|я\s+принимаю|принима(?:ю|ет|ем|ют|ть)|"
    r"согласен|согласна|согласны|погоджуюся|згоден|згодна|згодні",
    re.IGNORECASE,
)


def _player_acceptance_pattern(country_tag: str, language: str = "en") -> re.Pattern:
    """Match first-person assent or the player's own localized country name."""
    names = load_country_names(language)
    tag = str(country_tag or "").upper()
    name = names.get(tag)
    if not name or "\ufffd" in name:
        name = load_country_names("en").get(tag)
    named_assent = (rf"\b{re.escape(name)}\s+(?:explicitly\s+)?(?:accepts?|agrees?\s+to|confirms?)\b"
                    if name else r"(?!)")
    localized_names = {
        "GER": "Германия", "FRA": "Франция", "ITA": "Италия", "ENG": "Британия",
        "SOV": "Советский Союз", "USA": "США", "JAP": "Япония", "AUS": "Австрия",
    }
    localized_name = localized_names.get(tag)
    localized_assent = (
        rf"\b{re.escape(localized_name)}\s+(?:принима\w*|согласн\w*|подтвержда\w*)\b"
        if localized_name else r"(?!)"
    )
    first_person = (
        r"\b(?:i|we)\s+(?:(?:explicitly|hereby)\s+)?(?:accept|agree|confirm)\b|"
        r"\b(?:принимаю|принимаем|согласен|согласна|согласны|подтверждаю|подтверждаем)\b|"
        r"\b(?:погоджуюся|погоджуємося|підтверджую|підтверджуємо|згоден|згодна|згодні)\b"
    )
    return re.compile(rf"(?:{first_person})|(?:{named_assent})|(?:{localized_assent})", re.I)


_PLAYER_ACCEPTS_REFERENCED_TERMS = re.compile(
    r"\b(?:accept|accepts|agree|agrees|confirm|confirms)\b[^.!?]{0,80}"
    r"\b(?:all|these|the listed|the stated|those)\s+(?:of\s+)?"
    r"(?:(?:one|two|three|four|five|\d+)\s+)?(?:conditions?|terms?)\b|"
    r"(?:принима\w*|соглас\w*|подтвержда\w*)[^.!?]{0,80}"
    r"\b(?:все|эти|перечисленн\w*)\s+(?:услови\w*|пункт\w*)\b|"
    r"(?:погодж\w*|підтвердж\w*)[^.!?]{0,80}"
    r"\b(?:всі|ці|перелічен\w*)\s+умов\w*\b",
    re.I,
)
_COUNTRY_ACCEPTANCE = re.compile(
    r"\b(accept(?:s|ed)?|ready\s+to\s+accept|agree(?:s|d)?|confirms?(?: (?:its )?(?:agreement|acceptance))?|"
    r"s'engage|accepte|est d'accord|"
    r"принима(?:ю|ет|ем|ют|ть)|готов\w*\s+принять|подтвержда(?:ю|ет|ем|ют)|соглас(?:ен|на|ны|илась|ились)|"
    r"обязу(?:юсь|ется|емся|ются)|погодж(?:уюсь|ується|уємося)|згод(?:ен|на|ні))\b",
    re.IGNORECASE,
)
_TERM_REJECTION = re.compile(
    r"\b(reject(?:s|ed)?|refuse(?:s|d)?|cannot accept|does not accept|"
    r"отверга(?:ю|ет|ем|ют)|отклоня(?:ю|ет|ем|ют)|отказ(?:ываюсь|ывается|ываемся)|не принима(?:ю|ет|ем|ют)|"
    r"відхиля(?:ю|є|ємо|ють)|не прийма(?:ю|є|ємо|ють))\b",
    re.IGNORECASE,
)
_TERM_REQUIREMENT = re.compile(
    r"\b(require(?:s|d)?|insist(?:s|ed)?|must include|"
    r"требу(?:ю|ет|ем|ют)|настаива(?:ю|ет|ем|ют)|обязательное условие|"
    r"вимага(?:ю|є|ємо|ють)|наполяга(?:ю|є|ємо|ють))\b",
    re.IGNORECASE,
)
_TERM_POSITION_UNKNOWN = re.compile(
    r"(?:нет (?:явной )?(?:позици|заявлени)|не подтвержда\w* как согласован|"
    r"position.{0,20}not recorded|has not stated|not confirmed as agreed)",
    re.IGNORECASE,
)
_TERM_OMISSION = re.compile(
    r"\bwithout\b.{0,60}\b(?:advance|prior)\s+notice\b|"
    r"\b(?:no|not)\s+(?:written\s+)?(?:advance|prior)\s+notice\b|"
    r"без\s+(?:предварительн\w+\s+)?уведом|не\s+уведом\w*.{0,50}заранее|"
    r"sans\s+(?:notification préalable|préavis)", re.I,
)
_AGREEMENT_CONFIRMATION_QUESTION = re.compile(
    r"(?:подтвержда\w*.{0,60}\b(?:все|кажд\w*|эти)\b.{0,40}\bуслови\w*|"
    r"(?:confirm|do you confirm).{0,60}\b(?:all|each|every)\b.{0,40}\b(?:terms?|conditions?)|"
    r"confirmez-vous.{0,60}\b(?:toutes?|chaque)\b.{0,40}\b(?:conditions?|termes?)|"
    r"підтвердж\w*.{0,60}\b(?:всі|кожн\w*|ці)\b.{0,40}\bумов\w*)",
    re.IGNORECASE | re.DOTALL,
)
_AGREEMENT_POSITION_REQUEST = re.compile(
    r"(?:по\s+кажд\w*\s+пункт\w*.{0,100}(?:позици\w*|статус\w*|решени\w*)|"
    r"(?:текущ\w*\s+)?позици\w*.{0,100}(?:по\s+)?(?:всем|кажд\w*)\s+(?:пяти\s+)?пункт\w*|"
    r"(?:по\s+)?(?:всем|кажд\w*)\s+(?:пяти\s+)?пункт\w*.{0,100}(?:позици\w*|статус\w*|решени\w*)|"
    r"(?:give|state|list|what\s+is).{0,80}(?:current\s+)?(?:position|stance).{0,80}(?:all|each|every|five).{0,40}(?:terms?|conditions?|points?)|"
    r"(?:позиці\w*.{0,80}(?:усіх|всіх|кожн\w*)\s+(?:п'яти\s+)?пункт\w*|"
    r"(?:усіх|всіх|кожн\w*)\s+(?:п'яти\s+)?пункт\w*.{0,80}позиці\w*)|"
    r"позици\w*.{0,80}кажд\w*\s+пункт\w*|"
    r"(?:choose|select|state|give).{0,60}(?:status|position).{0,60}(?:each|every)\s+(?:term|point)|"
    r"(?:for|on)\s+each\s+(?:term|point).{0,60}(?:status|position)|"
    r"(?:по\s+каждому\s+из\s+\w+\s+пункт\w*|по\s+(?:всем\s+)?пяти\s+пункт\w*|"
    r"по\s+всем\s+пункт\w*))",
    re.IGNORECASE | re.DOTALL,
)
_AGREEMENT_NAME_PATTERNS = (
    re.compile(r"\b(?:treaty|agreement|pact)\s+name\s*[:=]\s*[\"“«]([^\"”»\r\n]{3,80})[\"”»]", re.I),
    re.compile(r"\bcall\s+(?:this|the)\s+(?:treaty|agreement|pact)\s+[\"“«]([^\"”»\r\n]{3,80})[\"”»]", re.I),
    re.compile(r"назван(?:ие договора|ие соглашения|ие пакта)\s*[:—-]?\s*[\"“«]([^\"”»\r\n]{3,80})[\"”»]", re.I),
    re.compile(r"назов[её]м\s+(?:договор|соглашение|пакт)\s+[\"“«]([^\"”»\r\n]{3,80})[\"”»]", re.I),
)


def _agreement_name_from_sources(chat: dict, version_record: dict) -> tuple[str, str] | None:
    """Read an explicitly quoted treaty name from the player's linked source turn."""
    player = str(chat.get("player_country", "")).upper()
    request_ids = set(version_record.get("source_request_ids", []))
    for message in reversed(chat.get("messages", [])):
        if (not isinstance(message, dict) or message.get("request_id") not in request_ids
                or str(message.get("speaker", "")).upper() != player
                or not isinstance(message.get("text"), str)):
            continue
        for pattern in _AGREEMENT_NAME_PATTERNS:
            match = pattern.search(message["text"])
            if not match:
                continue
            name = re.sub(r"\s+", " ", match.group(1)).strip()
            # HOI4 localisation strings use quotes and square brackets as syntax.
            # Refuse those characters instead of letting a player-supplied title
            # corrupt generated event localisation or inject localisation tokens.
            if (3 <= len(name) <= 80 and name.isprintable()
                    and not re.search(r'["\\\[\]{}]', name)):
                return name, message["request_id"]
    return None


def _default_agreement_name(action_id: str, participants: list[str]) -> str:
    label = action_id.replace("_", " ").strip().title() or "Diplomatic Agreement"
    return f"{label} — {'–'.join(participants)}"
_NEW_NEGOTIATION_ACTION = re.compile(
    r"\b(?:propos\w*|offer\w*|revis\w*|chang\w*|amend\w*|grant\w*|withdraw\w*|"
    r"replace\w*|add|remove|propose\w*|revise\w*|modify\w*|accord\w*|"
    r"предлага\w*|изменя\w*|пересматр\w*|заменя\w*|ввод\w*|предостав\w*|"
    r"пропон\w*|змін\w*|перегляда\w*|замін\w*|нада\w*|"
    r"propos\w*|modifier\w*|révis\w*|offr\w*|accord\w*|retir\w*)\b",
    re.IGNORECASE,
)
_EMBARGO_TARGET = re.compile(
    r"\b(?i:lift\s+(?:the\s+)?embargo|break\s+(?:the\s+)?embargo|embargo|sanction)"
    rf"(?i:\s+(?:against|on|of|to))?\s+({COUNTRY_TAG_PATTERN})\b",
)
_EMBARGO_UNSUPPORTED_DETAIL = re.compile(
    r"\b(?:if|unless|provided that|only when|for\s+\d+\s+(?:days?|months?|years?))\b|"
    r"\bexcept(?:ion|ing)?\b|при\s+условии|если|исключая|кроме|сроком\s+на\s+\d+|"
    r"за\s+умови|якщо|крім|на\s+\d+\s+(?:днів|місяців|років)", re.I,
)

_TERM_PATTERNS = {
    "written_guarantee": re.compile(
        r"(?:письменн.{0,45}(?:гаранти|обязательств)|(?:гаранти|обязательств).{0,45}письменн|"
        r"written.{0,45}(?:guarantee|commitment|undertaking|pledge)|"
        r"(?:guarantee|commitment|undertaking|pledge).{0,45}written|par écrit.{0,45}(?:garant|engag)|"
        r"(?:garant|engag).{0,45}par écrit|письмов.{0,45}(?:гаранті|зобов'яз)|"
        r"(?:гаранті|зобов'яз).{0,45}письмов|гаранти)", re.I),
    "advance_notice": re.compile(
        r"предварительн.{0,30}уведом|заранее уведом|(?:written\s+)?advance[\s-]+notice|"
        r"written\s+advance\s+\d+\s*[- ]?days?\s+(?:notice|notification|period|requirement)|"
        r"\b\d+\s*[- ]?days?\s+written\s+(?:advance\s+)?notice(?:\s+term)?\b|"
        r"\b(?:advance|prior)\s+\d+\s*[- ]?days?\s+written\s+notice\b|prior notice|"
        r"notification préalable|завчасн.{0,30}повідом", re.I),
    "equal_obligations": re.compile(
        r"равн.{0,20}обязательств|одинаков.{0,20}правил|equal obligations|"
        r"identical rules|однаков.{0,20}зобов", re.I),
    "no_territorial_concessions": re.compile(
        r"без территориальн.{0,20}уступ|не требует.{0,30}территориальн|"
        r"территориальн.{0,20}уступ.{0,15}не будет|no territorial concessions|"
        r"исключить территориальн.{0,20}уступ|отсутствие территориальн.{0,20}уступ|"
        r"no territorial transfer|sans concession territoriale|"
        r"без територіальн.{0,20}поступ", re.I),
    "no_territorial_changes": re.compile(
        r"no territorial changes?|without territorial changes?|"
        r"(?:must|shall|should|will|do|does|did|would)\s+not\s+(?:change|alter|modify)\s+(?:any\s+)?(?:territory|territories|borders?)|"
        r"(?:must|shall|should|will)\s+not\s+involve\s+(?:any\s+)?territorial changes?|"
        r"(?:will|would|shall|do|does|did) not involve (?:any )?territorial changes?|"
        r"territorial changes? (?:are|is) not (?:included|permitted|allowed)|"
        r"без территориальн.{0,25}изменени|территориальн.{0,25}изменени.{0,20}не будет|"
        r"без територіальн.{0,25}змін|територіальн.{0,25}змін.{0,20}не буде|"
        r"aucun changement territorial|sans changement territorial|"
        r"keine territorialen Änderungen", re.I),
    "preserve_sovereignty": re.compile(
        r"(?:no|without|not involve|not entail) (?:any )?(?:loss|end) of sovereignty|"
        r"(?:must|shall|should|will|do|does|did|would)\s+not\s+(?:reduce|limit|undermine|violate|compromise)\s+(?:(?:either|each|any|the|our|its)\s+)?(?:(?:country|state|party)(?:'s)?\s+)?sovereignty|"
        r"(?:does|do|did|will|would|shall) not (?:involve|cause|mean) (?:any )?(?:loss of sovereignty|end of sovereignty)|"
        r"not end sovereignty|\bend sovereignty\b|\bloss of sovereignty\b|"
        r"sovereignty (?:will|shall) remain|preserve (?:each party's )?sovereignty|"
        r"без потери суверенитета|без утраты суверенитета|суверенитет.{0,25}не будет затронут|"
        r"збереженн.{0,20}суверенітет|без втрати суверенітету|"
        r"sans perte de souveraineté|souveraineté préservée|"
        r"keinen Verlust der Souveränität", re.I),
    "preserve_existing_alliances": re.compile(
        r"(?:no|without|not require|not involve) (?:the )?(?:dissolution|dissolving|breakup) of (?:any )?(?:existing )?alliances?|"
        r"(?:existing )?alliances? (?:will|shall) not be dissolved|"
        r"(?:do|does|did|will|would|shall) not (?:dissolve|break up) (?:any )?(?:existing )?alliances?|"
        r"not dissolve (?:any )?(?:existing )?alliances?|\bdissolution of (?:any )?(?:existing )?alliances?\b|"
        r"\bdissolve (?:an? )?(?:any )?(?:existing )?alliances?\b|"
        r"без роспуска (?:существующих )?альянс|не требу(?:ет|ют) роспуска (?:существующих )?альянс|"
        r"без розпуску (?:наявних )?альянс|альянси не розпускаються|"
        r"sans dissolution des alliances? existantes|"
        r"keine Auflösung bestehender Bündnisse", re.I),
    "embargo_target": _EMBARGO_TARGET,
    "military_access": re.compile(
        r"military access|военн\w+ доступ|військов\w+ доступ|accès militaire|"
        r"passage\s+(?:militaire|(?:pour|de)\s+(?:(?:ses|les|nos|vos|des)\s+)?troupes)", re.I),
    "political_annexation": re.compile(
        r"peaceful.{0,30}(?:annex|incorporat|unif)|voluntary.{0,30}(?:annex|incorporat|unif)|"
        r"political.{0,30}(?:annex|incorporat|unif)|annex\w*|incorporat\w*|"
        r"мирн\w*.{0,30}(?:аннекс|присоедин|объедин)|добровольн\w*.{0,30}(?:присоедин|объедин)|"
        r"политическ\w*.{0,30}(?:аннекс|присоедин|объедин)|аннекс\w*|присоедин\w*|"
        r"мирн\w*.{0,30}об[’']?єднан|добровільн\w*.{0,30}(?:приєднан|об[’']?єднан)|"
        r"політичн\w*.{0,30}(?:анекс|приєднан|об[’']?єднан)|анекс\w*|приєднан\w*|"
        r"annexion|annex\w*|rattachement|rattacher|unification pacifique|union volontaire",
        re.I,
    ),
}


def _shared_negated_safeguard_clauses(text: str) -> dict[str, str]:
    """Expand a shared negation over coordinated safeguard predicates.

    In English, ``must not change A, reduce B, or dissolve C`` negates all
    three verbs. Returning just the final matched phrase used to lose that
    shared ``not`` and could make the model reverse the requested safeguard.
    Each emitted clause is reconstructed from the source modal and its exact
    coordinated predicate, and remains linked to the source request by callers.
    """
    safeguards = (
        "no_territorial_changes", "preserve_sovereignty",
        "preserve_existing_alliances",
    )
    coordinated_predicate_patterns = {
        "no_territorial_changes": re.compile(
            r"\b(?:change|alter|modify)\s+(?:any\s+)?(?:territory|territories|borders?)\b", re.I,
        ),
        "preserve_sovereignty": re.compile(
            r"\b(?:reduce|limit|undermine|violate|compromise)\s+"
            r"(?:(?:either|each|any|the|our|its)\s+)?"
            r"(?:(?:country|state|party)(?:'s)?\s+)?sovereignty\b", re.I,
        ),
        "preserve_existing_alliances": re.compile(
            r"\b(?:dissolve|break up|terminate)\s+(?:any\s+)?(?:existing\s+)?alliances?\b", re.I,
        ),
    }
    result: dict[str, str] = {}
    sentences = re.split(r"[.!?;\n]+", str(text or ""))
    shared_negation = re.compile(
        r"\b(?P<modal>must|shall|should|will|would|do|does|did)\s+not\s+"
        r"(?P<predicates>[^.!?;]+)", re.I,
    )
    split_predicates = re.compile(
        r"\s*,\s*(?:(?:and|or)\s+)?|\s+\b(?:and|or)\b\s+", re.I,
    )
    for sentence in sentences:
        match = shared_negation.search(sentence)
        if not match:
            continue
        predicates = [part.strip(" ,:") for part in split_predicates.split(match.group("predicates"))
                      if part.strip(" ,:")]
        if len(predicates) < 2:
            continue
        modal = match.group("modal").lower()
        for predicate in predicates:
            for term_id in safeguards:
                if term_id not in result and coordinated_predicate_patterns[term_id].search(predicate):
                    result[term_id] = f"{modal} not {predicate}"
    return result


def extract_known_term_clauses(text: str) -> list[dict[str, str]]:
    """Return source-grounded paper terms that accompany a diplomatic action."""
    allowed = ("written_guarantee", "advance_notice", "equal_obligations",
               "no_territorial_concessions", "no_territorial_changes",
               "preserve_sovereignty", "preserve_existing_alliances")
    clauses = [part.strip() for part in re.split(r"[.!?;\n]+", str(text or "")) if part.strip()]
    found = []
    shared_negated = _shared_negated_safeguard_clauses(text)
    for term_id in allowed:
        pattern = _TERM_PATTERNS[term_id]
        source_clause = None
        for item in clauses:
            match = pattern.search(item)
            if not match:
                if term_id in shared_negated:
                    source_clause = shared_negated[term_id]
                    break
                continue
            if (term_id == "written_guarantee" and not re.search(
                    r"(?:письменн.{0,45}(?:гаранти|обязательств)|(?:гаранти|обязательств).{0,45}письменн|"
                    r"written.{0,45}(?:guarantee|commitment|undertaking|pledge)|"
                    r"(?:guarantee|commitment|undertaking|pledge).{0,45}written|par écrit.{0,45}(?:garant|engag)|"
                    r"(?:garant|engag).{0,45}par écrit|письмов.{0,45}(?:гаранті|зобов'яз)|"
                    r"(?:гаранті|зобов'яз).{0,45}письмов)", item, re.I)):
                continue
            if term_id in shared_negated:
                source_clause = shared_negated[term_id]
                break
            if term_id in {
                    "no_territorial_changes", "preserve_sovereignty",
                    "preserve_existing_alliances", "no_territorial_concessions",
            }:
                # These safeguard patterns identify the complete negated
                # clause (for example, "no loss of sovereignty"). Returning
                # the rest of a comma-separated list made adjacent safeguards
                # overlap and later confused point-to-term validation.
                source_clause = match.group(0).strip(" ,:-")
                break
            start = match.start()
            if term_id == "advance_notice":
                # Keep a nearby duration with its notice clause, but stop at a
                # new coordinated negated safeguard instead of absorbing the
                # rest of the sentence into the notice point.
                amount = re.search(
                    r"\b\d+\s+(?:calendar\s+)?days?\s+of\s+$",
                    item[max(0, start - 45):start], re.I,
                )
                if amount:
                    start = max(0, start - 45) + amount.start()
            prefix = item[:start]
            separators = list(re.finditer(
                r"(?:,\s*)?\b(?:and|also|plus|as well as|и|также|а также|та|також)\b\s*",
                prefix, re.I,
            ))
            if separators:
                start = separators[-1].end()
            end = len(item)
            if term_id == "advance_notice":
                next_safeguard = re.search(
                    r",\s*(?:(?:and|or)\s+)?(?:(?:it|the pact|the agreement|both parties)\s+)?"
                    r"(?:must|shall|should|will|would)\s+not\b",
                    item[match.end():], re.I,
                )
                if next_safeguard:
                    end = match.end() + next_safeguard.start()
            source_clause = item[start:end].strip(" ,:-")
            break
        if source_clause:
            found.append({"term_id": term_id, "source_clause": source_clause[:240]})
    return found


def known_term_is_mentioned(term_id: str, text: str) -> bool:
    """Check whether a reply explicitly addresses a known paper-term idea."""
    value = str(text or "")
    pattern = _TERM_PATTERNS.get(term_id)
    if pattern and pattern.search(value):
        return True
    if term_id == "advance_notice":
        return bool(re.search(
            r"\b(?:notify|notification|inform|information)\w*.{0,45}\b(?:before|prior|advance)\b|"
            r"\b(?:before|prior|in advance)\b.{0,45}\b(?:notify|notification|inform|information)\w*|"
            r"\b\d+\s*[- ]?days?\s+written\s+(?:advance\s+)?notice(?:\s+term)?\b|"
            r"\b(?:advance|prior)\s+\d+\s*[- ]?days?\s+written\s+notice\b|"
            r"заранее\s+уведом|уведом\w*.{0,40}заранее|уведом\w*.{0,40}до\b|"
            r"notification préalable|préavis", value, re.I,
        ))
    return False


def is_separate_known_term_requirement(text: str, terms: list[dict]) -> bool:
    """Tell a listed term requirement apart from a condition precedent to the action."""
    value = str(text or "")
    requirement = _TERM_REQUIREMENT.search(value)
    if not requirement or re.search(
            r"\b(?:if|unless|provided(?:\s+that)?|subject\s+to|only\s+if)\b|"
            r"\b(?:если|якщо|при\s+условии|за\s+умови)\b|à\s+condition|sous\s+réserve",
            value, re.I):
        return False
    for item in terms if isinstance(terms, list) else []:
        if not isinstance(item, dict):
            continue
        term_id = item.get("term_id")
        pattern = _TERM_PATTERNS.get(term_id)
        if pattern and pattern.search(value, requirement.end()):
            return True
    return False


_GUARANTEE_TERM = re.compile(
    r"guarantee.{0,45}(?:independence|sovereignty|security|our independence)|"
    r"гаранти.{0,45}(?:независимост|суверенитет|безопасност)|"
    r"гаранті.{0,45}(?:незалежн|суверенітет|безпек)|"
    r"garant.{0,45}(?:indépendance|souveraineté|sécurité)", re.I,
)
_ORDINARY_COMPLIANCE_COVENANT = re.compile(
    r"при\s+условии\s+соблюдени\w*.{0,50}(?:согласованн\w*|этих|договорн\w*).{0,30}обязательств|"
    r"subject\s+to\s+(?:the\s+)?(?:parties'?\s+)?compliance\s+with\s+(?:the\s+)?(?:agreed|these|contractual)\s+obligations|"
    r"provided\s+(?:that\s+)?(?:both\s+)?parties\s+(?:honou?r|comply\s+with)\s+(?:the\s+)?(?:agreed\s+)?obligations",
    re.IGNORECASE,
)
_ALL_PROPOSED_TERMS = re.compile(
    r"(?:принима\w*|соглас\w*|подтвержда\w*).{0,45}(?:все\s+(?:три|3|\d+)\s+(?:предложенн\w*\s+)?(?:услови\w*|пункт\w*)|"
    r"все\s+предложенн\w*\s+(?:услови\w*|пункт\w*)|"
    r"all\s+(?:three|\d+)\s+(?:proposed\s+)?(?:terms?|conditions?|points?)|"
    r"all\s+proposed\s+(?:terms?|conditions?|points?))|"
    r"(?:прийма\w*|погодж\w*|підтвердж\w*).{0,45}(?:усі\s+(?:три|3|\d+)\s+(?:запропонован\w*\s+)?(?:умов\w*|пункт\w*))",
    re.IGNORECASE | re.DOTALL,
)
_WHOLE_PROPOSAL_ACCEPTANCE = re.compile(
    r"(?:готов\w*\s+принять|принима\w*|принима\w*\s+целиком|соглас\w*\s+на).{0,45}(?:это\s+предложени\w*|весь\s+проект|все\s+услови\w*)|"
    r"(?:ready\s+to\s+accept|accept(?:s|ed)?|agree(?:s|d)?\s+to).{0,45}(?:this\s+proposal|the\s+entire\s+proposal|all\s+terms?)",
    re.IGNORECASE,
)


def _is_condition_precedent(text: str) -> bool:
    """Distinguish an unmet condition for assent from a covenant to honor the deal."""
    if not _CONDITIONAL.search(text):
        return False
    return not bool(_ORDINARY_COMPLIANCE_COVENANT.search(text))


def _numbered_proposal_terms(text: str) -> tuple[int, set[str]]:
    """Return canonical term IDs grouped by explicit numbered proposal points."""
    chunks = re.split(r"(?=(?:^|\s)\(?\d{1,2}[.)]\s+)", str(text or ""))
    numbered = [chunk for chunk in chunks if re.match(r"\s*\(?\d{1,2}[.)]\s+", chunk)]
    point_count = len(numbered)
    term_ids: set[str] = set()
    nap_pattern = re.compile(r"non.aggression|не\s*агресс|ненапад|не\s+нападать|ненападу", re.I)
    for chunk in numbered:
        if nap_pattern.search(chunk):
            term_ids.add("non_aggression_pact")
        for term_id, pattern in _TERM_PATTERNS.items():
            if pattern.search(chunk):
                term_ids.add(term_id)
    return point_count, term_ids


def _proposal_term_ids(text: str) -> set[str]:
    ids = set()
    if re.search(r"non.aggression|не\s*агресс|ненапад|не\s+нападать|ненападу", text, re.I):
        ids.add("non_aggression_pact")
    if _GUARANTEE_TERM.search(text):
        ids.add("independence_guarantee")
    if _EMBARGO_TARGET.search(text):
        ids.add("embargo_target")
    for term_id, pattern in _TERM_PATTERNS.items():
        if pattern.search(text):
            ids.add(term_id)
    return ids


def _linked_acceptance_terms(messages: list[dict], acceptance: dict | None,
                             *, is_player: bool) -> tuple[set[str], str | None]:
    """Resolve an explicit whole-proposal or all-numbered-points assent to its source."""
    if not acceptance or _is_condition_precedent(str(acceptance.get("text", ""))):
        return set(), None
    text = str(acceptance.get("text", ""))
    count_match = _ALL_PROPOSED_TERMS.search(text)
    whole_match = _WHOLE_PROPOSAL_ACCEPTANCE.search(text)
    if not count_match and not whole_match:
        return set(), None
    try:
        index = next(i for i, item in enumerate(messages)
                     if item is acceptance or (acceptance.get("request_id")
                                               and item.get("request_id") == acceptance.get("request_id")))
    except StopIteration:
        return set(), None
    preceding = messages[:index]
    if is_player:
        offer = next((item for item in reversed(preceding)
                      if item.get("kind") == "country_response"
                      and item.get("decision") == "counter_offer"), None)
    else:
        offer = next((item for item in reversed(preceding)
                      if item.get("speaker") == messages[index].get("recipient")
                      and item.get("kind") == "player_message"), None)
        # The saved player message may not carry a recipient; in a two-party chat the
        # immediately preceding player turn is still the offer being accepted.
        if offer is None:
            offer = next((item for item in reversed(preceding)
                          if item.get("kind") == "player_message"), None)
    if not offer or not offer.get("request_id"):
        return set(), None
    proposal_text = str(offer.get("text", ""))
    if count_match:
        count_text = re.search(r"(?:три|three|3|\d+)", count_match.group(0), re.I)
        wanted = ({"три": 3, "three": 3, "3": 3}.get(count_text.group(0).lower())
                  if count_text else None)
        if wanted is None and count_text:
            try:
                wanted = int(count_text.group(0))
            except ValueError:
                pass
        actual_count, term_ids = _numbered_proposal_terms(proposal_text)
        if wanted is None or actual_count != wanted or not term_ids:
            return set(), None
        return term_ids, offer["request_id"]
    term_ids = _proposal_term_ids(proposal_text)
    # A participant may accept the player's message by reference, while that
    # message itself accepts the numbered counter-offer. Carry the exact cited
    # terms through that one explicit link; do not infer assent from old history.
    if not is_player and _ALL_PROPOSED_TERMS.search(proposal_text):
        nested_offer = next((item for item in reversed(preceding)
                             if item.get("kind") == "country_response"
                             and item.get("decision") == "counter_offer"), None)
        if nested_offer:
            nested_count, nested_terms = _numbered_proposal_terms(
                str(nested_offer.get("text", "")))
            nested_match = _ALL_PROPOSED_TERMS.search(proposal_text)
            count_value = re.search(r"(?:три|three|3|\d+)", nested_match.group(0), re.I)
            wanted = ({"три": 3, "three": 3, "3": 3}.get(count_value.group(0).lower())
                      if count_value else None)
            if wanted is None and count_value:
                try:
                    wanted = int(count_value.group(0))
                except ValueError:
                    pass
            if wanted is not None and nested_count == wanted:
                term_ids.update(nested_terms)
    return (term_ids, offer["request_id"]) if term_ids else (set(), None)
_TERM_LABELS = {
    "white_peace": {
        "en": "White peace between the two signatories.",
        "ru": "Белый мир между двумя подписантами.",
        "fr": "Paix blanche entre les deux signataires.",
    },
    "faction_join": {
        "en": "The faction leader admits the named signatory.",
        "ru": "Лидер фракции принимает указанного подписанта.",
        "fr": "Le chef de faction admet le signataire désigné.",
    },
    "faction_leave": {
        "en": "The faction leader removes the named signatory by mutual agreement.",
        "ru": "Лидер фракции исключает указанного подписанта по обоюдному согласию.",
        "fr": "Le chef de faction exclut le signataire désigné d’un commun accord.",
    },
    "military_access": {
        "ru": "Военный доступ",
        "en": "Military access",
        "fr": "Accès militaire",
        "uk": "Військовий доступ",
    },
    "embargo_target": {
        "ru": "Эмбарго в HOI4 против указанной третьей страны.",
        "en": "A HOI4 embargo against the specified third country.",
        "fr": "Un embargo HOI4 contre le pays tiers indiqué.",
        "uk": "Ембарго HOI4 проти зазначеної третьої країни.",
    },
    "non_aggression_pact": {
        "ru": "Взаимный пакт о ненападении между участниками.",
        "en": "A mutual non-aggression pact between the participants.",
        "fr": "Un pacte de non-agression mutuelle entre les participants.",
        "uk": "Взаємний пакт про ненапад між учасниками.",
    },
    "independence_guarantee": {
        "ru": "Гарантия независимости",
        "en": "Independence guarantee",
        "fr": "Garantie d’indépendance",
        "uk": "Гарантія незалежності",
    },
    "embargo_target": {
        "ru": "Эмбарго против указанной третьей страны; HOI4 не задаёт срок действия.",
        "en": "An embargo against the named third country; HOI4 does not set a duration.",
        "fr": "Un embargo contre le pays tiers indiqué ; HOI4 n'en fixe pas la durée.",
        "uk": "Ембарго проти вказаної третьої країни; HOI4 не задає строк дії.",
    },
    "written_guarantee": {
        "ru": "Взаимное письменное обязательство о ненападении.",
        "en": "A mutual written non-aggression commitment.",
        "fr": "Un engagement mutuel écrit de non-agression.",
        "uk": "Взаємне письмове зобов'язання про ненапад.",
    },
    "advance_notice": {
        "ru": "Заранее уведомлять о мерах, способных затронуть безопасность, и проводить консультации.",
        "en": "Give advance notice of measures affecting security and hold consultations.",
        "fr": "Notifier à l'avance les mesures touchant la sécurité et tenir des consultations.",
        "uk": "Завчасно повідомляти про заходи, що можуть вплинути на безпеку, і проводити консультації.",
    },
    "equal_obligations": {
        "ru": "Одинаковые обязательства и процедуры действуют для всех сторон.",
        "en": "The same obligations and procedures apply to every party.",
        "fr": "Les mêmes obligations et procédures s'appliquent à chaque partie.",
        "uk": "Однакові зобов'язання й процедури діють для всіх сторін.",
    },
    "no_territorial_concessions": {
        "ru": "Территориальные уступки в соглашение не входят.",
        "en": "The agreement includes no territorial concessions.",
        "fr": "L'accord ne comprend aucune concession territoriale.",
        "uk": "Територіальні поступки до угоди не входять.",
    },
    "no_territorial_changes": {
        "ru": "Пакт не предусматривает территориальных изменений.",
        "en": "The pact does not provide for territorial changes.",
        "fr": "Le pacte ne prévoit aucun changement territorial.",
        "uk": "Пакт не передбачає територіальних змін.",
    },
    "preserve_sovereignty": {
        "ru": "Соглашение не ограничивает суверенитет ни одной стороны.",
        "en": "The agreement does not restrict either party's sovereignty.",
        "fr": "L'accord ne restreint pas la souveraineté de l'une ou l'autre partie.",
        "uk": "Угода не обмежує суверенітет жодної зі сторін.",
    },
    "preserve_existing_alliances": {
        "ru": "Соглашение не требует роспуска действующих альянсов.",
        "en": "The agreement does not require the dissolution of existing alliances.",
        "fr": "L'accord n'exige pas la dissolution des alliances existantes.",
        "uk": "Угода не вимагає розпуску чинних альянсів.",
    },
    "political_annexation": {
        "ru": "Мирное политическое присоединение {target} к {initiator}.",
        "en": "Peaceful political incorporation of {target} into {initiator}.",
        "fr": "Intégration politique pacifique de {target} à {initiator}.",
        "uk": "Мирне політичне приєднання {target} до {initiator}.",
    },
}


def directional_parties(term):
    spec = (term.get("proposal_spec") or {}).get("action") or {}
    actor = term.get("action_actor") or spec.get("actor_country_tag")
    recipient = (term.get("action_recipient") or spec.get("recipient_country_tag")
                 or spec.get("target_country_tag"))
    return tuple(str(tag or "").upper() for tag in (actor, recipient))


def directional_term_label(term, language):
    labels = _TERM_LABELS.get(term.get("term_id"), {})
    label = labels.get(language, labels.get("en", term.get("term_id", ""))).rstrip(".")
    actor, recipient = directional_parties(term)
    if actor != recipient and {actor, recipient} == set(term.get("required_participants", [])):
        wording = {
            'independence_guarantee': {
                'ru':'{actor} гарантирует независимость {recipient}',
                'en':"{actor} guarantees the independence of {recipient}",
                'fr':'{actor} garantit l’indépendance de {recipient}',
                'uk':'{actor} гарантує незалежність {recipient}',
            },
            'military_access': {
                'ru':'{actor} предоставляет войскам {recipient} доступ на свою территорию',
                'en':'{actor} grants troops of {recipient} access to its territory',
                'fr':'{actor} accorde aux troupes de {recipient} l’accès à son territoire',
                'uk':'{actor} надає військам {recipient} доступ на свою територію',
            },
        }.get(term.get('term_id'))
        if wording:
            label += ': ' + wording.get(language,wording['en']).format(actor=actor,recipient=recipient)
        return f"{label} · {actor} → {recipient}"
    return label


def display_final_text(version, language):
    """Render old signed text without changing the stored revision or its hash."""
    text = version.get('final_text', '')
    for term in version.get('terms', []):
        if term.get('term_id') in {'independence_guarantee','military_access'}:
            old = term.get('text')
            if isinstance(old,str) and old:
                text = text.replace('• '+old, '• '+directional_term_label(term,language),1)
    return text


def agreement_display(agreement, language):
    """Presentation metadata only; authoritative terms and evidence stay intact."""
    result = deepcopy(agreement)
    from player_diplomacy import agreement_title
    result['display_name'] = agreement_title(agreement,language)
    for version in result.get('versions', []):
        version['display_final_text'] = display_final_text(version,language)
        for term in version.get('terms', []):
            term['display_text'] = (directional_term_label(term,language)
                if term.get('term_id') in {'independence_guarantee','military_access'} else term.get('text'))
    return result


def recoverable_draft_terms(chat, agreement, version):
    """Project a draft from its own source messages without altering signatures or history."""
    from hoi4_country_names import load_country_name_aliases
    from structured_intents import explicit_bilateral_offer

    player = chat.get("player_country")
    parties = set(chat.get("participants", []))
    if (agreement.get("status") != "draft" or agreement.get("execution_result")
            or len(parties) != 2 or player not in parties
            or agreement.get("campaign_id") != chat.get("campaign_id")
            or agreement.get("chat_id") != chat.get("chat_id")
            or version.get("version") != agreement.get("current_version")):
        return []
    terms = version.get("terms", [])
    if len(terms) == 3 and all(term.get("proposal_spec") for term in terms):
        return []
    other, = parties - {player}
    ids = set(version.get("source_request_ids", []))
    recovered = {}
    for message in chat.get("messages", []):
        if (message.get("request_id") not in ids or message.get("kind") != "player_message"
                or message.get("speaker") != player
                or message.get("campaign_id", chat.get("campaign_id")) != chat.get("campaign_id")
                or message.get("conversation_id", chat.get("conversation_id")) != chat.get("conversation_id")):
            continue
        parsed = explicit_bilateral_offer(message.get("text") or "", message["request_id"], player, other,
                                          load_country_name_aliases('ru'))
        if parsed:
            identity = tuple(sorted((action["action_id"], action["actor_country_tag"],
                                     action["recipient_country_tag"]) for action in parsed["actions"]))
            recovered[identity] = True
    return list(next(iter(recovered))) if len(recovered) == 1 else []


def _clauses(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"[.!?;\n]+", text) if part.strip()]


def _unconditional_acceptance_clauses(text: str, pattern: re.Pattern) -> list[str]:
    clauses = [part.strip() for part in re.split(r"(?<=[.!?;])\s+|\n", str(text or ""))
               if part.strip()]
    return [clause for clause in clauses
            if pattern.search(clause)
            and not _is_condition_precedent(clause)
            and not _TERM_REJECTION.search(clause)
            and "?" not in clause]


def _term_excerpt(text: str, pattern: re.Pattern, limit: int = 240) -> str:
    match = pattern.search(text)
    if not match:
        return text[:limit]
    start = max(0, match.start() - 75)
    end = min(len(text), max(match.end() + 100, start + limit))
    return text[start:end].strip()[:limit]


def _canonical_spec_value(value):
    """Canonicalize parsed contract fields without inventing equivalence."""
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value.strip().casefold())
    if isinstance(value, Mapping):
        from diplomatic_language import canonical_period
        return {str(key): _canonical_spec_value(canonical_period(item) if key == "duration" else item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        normalized = [_canonical_spec_value(item) for item in value]
        return sorted(normalized, key=lambda item: json.dumps(
            item, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return value


def _action_proposal_spec(analysis: dict | None, action_id: str,
                          required_participants: list[str]) -> dict | None:
    """Build a comparable term identity only from a complete source analysis."""
    if not isinstance(analysis, dict) or not isinstance(analysis.get("actions"), list):
        return None
    source_id = analysis.get("source_request_id")
    if not isinstance(source_id, str) or not source_id:
        return None
    required = sorted({str(tag).upper() for tag in required_participants})
    candidates = []
    for action in analysis["actions"]:
        if not isinstance(action, dict) or action.get("action_id") != action_id:
            continue
        if action.get("source_request_id") != source_id or action.get("parse_status") != "parsed":
            continue
        if action.get("communication_act") in {"question", "rejection_statement"}:
            continue
        unresolved = action.get("unresolved_fields", [])
        if not isinstance(unresolved, list) or unresolved:
            continue
        parties = action.get("negotiation_party_country_tags")
        if (not isinstance(parties, list) and action_id in {"non_aggression_pact", "truce"}
                and isinstance(action.get("actor_country_tags"), list)):
            # For a bilateral NAP/truce the parser's source-grounded actor
            # pair is also the signatory pair when it exactly matches the
            # required parties. A mere chat roster match is not enough.
            source_actors = action["actor_country_tags"]
            normalized_actors = sorted({str(tag).upper() for tag in source_actors})
            parties = normalized_actors if normalized_actors == required else None
        if not isinstance(parties, list) and action_id in {"guarantee", "military_access"}:
            issuer = action.get("actor_country_tag")
            recipient = action.get("recipient_country_tag") or action.get("target_country_tag")
            if isinstance(issuer, str) and isinstance(recipient, str) and sorted({issuer, recipient}) == required:
                parties = required
        if not isinstance(parties, list) or sorted({str(tag).upper() for tag in parties}) != required:
            continue
        actors = action.get("actor_country_tags", [])
        if not isinstance(actors, list):
            continue
        candidate = {
            "action_id": action_id,
            "parties": required,
            "actor_country_tag": str(action.get("actor_country_tag") or "").upper() or None,
            "actor_country_tags": sorted({str(tag).upper() for tag in actors}),
            "recipient_country_tag": str(action.get("recipient_country_tag") or "").upper() or None,
            "target_country_tag": str(action.get("target_country_tag") or "").upper() or None,
            "duration": action.get("duration"),
            "conditions": action.get("conditions", []),
            "exceptions": action.get("exceptions", []),
        }
        candidates.append(_canonical_spec_value(candidate))
    unique = {json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
              for item in candidates}
    if len(unique) != 1:
        return None
    return json.loads(next(iter(unique)))


def _proposal_spec_for_term(analysis: dict | None, action_id: str, term_id: str,
                            category: str, required_participants: list[str],
                            source_text: str = "") -> dict | None:
    action_spec = _action_proposal_spec(analysis, action_id, required_participants)
    if action_spec is None:
        return None
    if category != "hoi4_game_effect":
        pattern = _TERM_PATTERNS.get(term_id)
        if pattern is None:
            return None
        clause = next((item["source_clause"] for item in extract_known_term_clauses(source_text)
                       if item["term_id"] == term_id), None)
        if not clause:
            return None
        # These points are negotiated independently of the executable action
        # parameters. Binding their assent to the whole action spec wrongly
        # revoked an unchanged written guarantee when, for example, only the
        # NAP duration changed. The exact source clause and party set remain
        # the consent identity; if the promise itself changes, consent resets.
        return {
            "action_id": action_spec["action_id"],
            "parties": action_spec["parties"],
            "term_clause": _canonical_spec_value(clause),
        }
    return {"action": action_spec, "term_clause": None}


def _term_clause_detail(clause: str, term_id: str) -> set[str]:
    """Extract content words that distinguish one version of an ancillary clause."""
    words = set(re.findall(r"[a-z0-9]+", str(clause or "").casefold()))
    common = {
        "a", "an", "the", "and", "or", "of", "to", "for", "by", "with", "from",
        "before", "after", "within", "during", "as", "on", "in", "at", "their",
        "our", "both", "each", "all", "mutual", "written", "existing", "current",
        "separate", "clause", "condition", "requirement", "term", "obligation",
        "agreement", "condition", "advance", "notice",
    }
    words.difference_update(common)
    if term_id == "advance_notice":
        words.difference_update({"notification", "notify", "notices", "notified"})
    return words


def _preserve_prior_ancillary_spec(previous_versions: list[dict] | None,
                                    term_id: str, action_id: str,
                                    required_participants: list[str],
                                    candidate: dict | None) -> dict | None:
    """Keep a prior side-clause identity when a newer action revision only names it generically.

    A root-action revision often repeats a side condition as context (for example,
    “confirm the existing 30-day notice condition”). Small-model extraction can
    reduce that reference to “advance-notice condition”; treating that shorthand
    as a new clause would silently change the treaty version.
    """
    if not previous_versions:
        return candidate
    prior_terms = previous_versions[-1].get("terms", [])
    prior = next((item for item in prior_terms
                  if isinstance(item, dict)
                  and item.get("term_id") == term_id
                  and item.get("action_id") == action_id
                  and set(item.get("required_participants") or [])
                  == set(required_participants)), None)
    if not prior:
        return candidate
    prior_spec = prior.get("proposal_spec")
    if not isinstance(prior_spec, dict):
        return candidate
    if not isinstance(candidate, dict):
        return deepcopy(prior_spec)
    prior_clause = str(prior_spec.get("term_clause") or "").strip()
    candidate_clause = str(candidate.get("term_clause") or "").strip()
    if not prior_clause or not candidate_clause:
        return deepcopy(prior_spec)
    prior_parties = {str(tag).upper() for tag in prior_spec.get("parties", [])}
    candidate_parties = {str(tag).upper() for tag in candidate.get("parties", [])}
    if prior_spec.get("action_id") != candidate.get("action_id") \
            or prior_parties != candidate_parties:
        return candidate
    prior_detail = _term_clause_detail(prior_clause, term_id)
    candidate_detail = _term_clause_detail(candidate_clause, term_id)
    # Identical clauses and less-specific references (including generated
    # placeholders such as “advance-notice condition”) retain the exact prior
    # text. A genuinely new value or obligation changes the spec and invalidates
    # inherited assent through the existing full-spec comparison.
    if candidate_clause.casefold() == prior_clause.casefold() \
            or candidate_detail.issubset(prior_detail):
        return deepcopy(prior_spec)
    return candidate


def _existing_term_proposal_spec(chat: dict, version: dict, term: dict) -> dict | None:
    stored = term.get("proposal_spec")
    if isinstance(stored, dict):
        return _canonical_spec_value(stored)
    source_ids = set()
    if isinstance(term.get("proposal_source_request_id"), str):
        source_ids.add(term["proposal_source_request_id"])
    positions = term.get("participant_positions")
    if isinstance(positions, dict):
        for position in positions.values():
            if isinstance(position, dict) and isinstance(position.get("source_request_id"), str):
                source_ids.add(position["source_request_id"])
    if len(source_ids) != 1:
        return None
    source_id = next(iter(source_ids))
    source_message = next((item for item in chat.get("messages", [])
                           if item.get("request_id") == source_id
                           and item.get("kind") == "player_message"), None)
    if not isinstance(source_message, dict):
        return None
    return _proposal_spec_for_term(
        source_message.get("message_analysis"), str(term.get("action_id") or ""),
        str(term.get("term_id") or ""), str(term.get("category") or ""),
        term.get("required_participants") or [], str(source_message.get("text") or ""),
    )


def _valid_prior_confirmation(chat: dict, version: dict, term: dict,
                              participant: str) -> dict | None:
    if participant not in term.get("confirmed_by", []):
        return None
    request_id = term.get("confirmed_by_request_ids", {}).get(participant)
    if not isinstance(request_id, str) or not request_id:
        return None
    binding = term.get("confirmation_source_request_ids", {}).get(participant)
    position = term.get("participant_positions", {}).get(participant)
    if isinstance(position, dict):
        if (position.get("participant_role") != "party"
                or position.get("consent_status") != "confirmed"
                or position.get("response_request_id", position.get("request_id")) != request_id
                or (position.get("origin") != "ollama" and not (
                    position.get("origin") == "engine_policy" and any(
                        message.get("request_id") == request_id and message.get("engine_point_policy_verified") is True
                        for message in chat.get("messages", [])
                    )))):
            return None
        binding = binding or position.get("source_request_id")
    if not isinstance(binding, str) or not binding:
        binding = version.get("participant_linked_proposal_request_ids", {}).get(request_id)
    if not isinstance(binding, str) or not binding:
        return None
    return {"request_id": request_id, "proposal_source_request_id": binding}


def _inherit_unchanged_term_confirmations(chat: dict, previous_versions: list[dict],
                                          terms: list[dict]) -> None:
    """Carry prior point assent across contiguous revisions only if its full spec matches."""
    if not previous_versions:
        return
    for term in terms:
        current_spec = term.get("proposal_spec")
        if not isinstance(current_spec, dict):
            continue
        required = set(term.get("required_participants") or [])
        current_positions = term.get("participant_positions", {})
        blocked = set(term.get("rejected_by", [])) | set(term.get("required_by", [])) \
            | set(term.get("conditional_by", []))
        if isinstance(current_positions, dict):
            for participant, position in current_positions.items():
                if not isinstance(position, dict) or position.get("participant_role") != "party":
                    continue
                if position.get("stance") in {"reject", "counter_offer", "conditional_acceptance"}:
                    blocked.add(participant)
        inherited = {}
        for prior_version in reversed(previous_versions):
            prior_term = next((item for item in prior_version.get("terms", [])
                               if isinstance(item, dict)
                               and item.get("term_id") == term.get("term_id")
                               and item.get("action_id") == term.get("action_id")
                               and set(item.get("required_participants") or []) == required), None)
            if prior_term is None:
                break
            prior_spec = _existing_term_proposal_spec(chat, prior_version, prior_term)
            if prior_spec is None or prior_spec != _canonical_spec_value(current_spec):
                # A changed or ambiguous intermediate proposal breaks the
                # chain; assent from before that change cannot be resurrected.
                break
            prior_positions = prior_term.get("participant_positions", {})
            if isinstance(prior_positions, dict):
                for participant, position in prior_positions.items():
                    if (participant in required and isinstance(position, dict)
                            and position.get("stance") in {
                                "reject", "counter_offer", "conditional_acceptance",
                            }):
                        blocked.add(participant)
                        inherited.pop(participant, None)
            for participant in required - blocked - set(term.get("confirmed_by", [])):
                proof = _valid_prior_confirmation(chat, prior_version, prior_term, participant)
                if proof and participant not in inherited:
                    inherited[participant] = {
                        **proof,
                        "from_version": prior_version.get("version"),
                        "proposal_spec_sha256": hashlib.sha256(json.dumps(
                            current_spec, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")).hexdigest(),
                    }
        if not inherited:
            continue
        term.setdefault("inherited_confirmations", {}).update(inherited)
        term.setdefault("confirmation_source_request_ids", {})
        for participant, proof in inherited.items():
            term["confirmed_by_request_ids"][participant] = proof["request_id"]
            term["confirmation_source_request_ids"][participant] = proof["proposal_source_request_id"]
            if participant not in term["confirmed_by"]:
                term["confirmed_by"].append(participant)
        term["status"] = ("agreed" if required.issubset(set(term["confirmed_by"]))
                          else "unconfirmed")


def _pointwise_term_stance(item: dict, speaker: str, action_id: str | None,
                           source_id: str | None, required_participants: list[str],
                           term_id: str | None = None) -> str | None:
    """Return a source-linked participant position only when its assent is explicit.

    The structured model label is advisory by itself. For negotiation consent we
    additionally require a generated Ollama response, the exact proposal and
    response IDs, the speaker's source-grounded party role, and affirmative text.
    """
    positions = item.get("dialogue_positions")
    if item.get("kind") != "country_response" or not isinstance(positions, list):
        return None
    if item.get("provider_status") != "generated" or not action_id or not source_id:
        return "mentioned"
    request_id = item.get("request_id")
    matching = [position for position in positions
                if isinstance(position, dict)
                and position.get("action_id") == action_id
                and ((position.get("term_id") == term_id) if term_id else not position.get("term_id"))
                and position.get("source_request_id") == source_id
                and (position.get("response_request_id") or request_id) == request_id]
    if not matching:
        return "mentioned"
    # Multiple contradictory rows for the same action/source are ambiguous.
    # Do not select whichever row happens to appear last.
    stances = {str(position.get("stance") or "") for position in matching}
    if len(stances) != 1:
        return "mentioned"
    position = matching[0]
    text = str(position.get("text") or "").strip()
    position_origin = position.get("origin")
    explicitly_validated_separate_term = (
        position_origin == "ollama_reclassified"
        and position.get("validation_reason") in {
            "separate_term_requirement_not_action_condition",
            # The provider assigns one repeated, explicit package acceptance
            # only to the safeguards it independently verified in that exact
            # Ollama sentence. The root action remains conditional and cannot
            # satisfy agreement-wide assent or signing.
            "explicit_shared_package_clause",
            "repeated_package_acceptance_is_conditional",
        }
    )
    if (speaker not in required_participants
            or (position_origin != "ollama" and not explicitly_validated_separate_term
                and not (position_origin == "engine_policy" and item.get("engine_point_policy_verified") is True))
            or not text):
        return "mentioned"
    stance = position.get("stance")
    if stance == "accept":
        if (position_origin == "engine_policy" and item.get("engine_point_policy_verified") is True
                and position.get("validation_reason") == "structured_treaty_decision"):
            return "accepted"
        if (_COUNTRY_ACCEPTANCE.search(text)
                and not _TERM_REJECTION.search(text)
                and not _HEDGED_CONSIDERATION.search(text)):
            if (_is_condition_precedent(text)
                    and not is_separate_known_term_requirement(
                        text, position.get("additional_terms", []),
                    )):
                return "conditional_acceptance"
            return "accepted"
        return "mentioned"
    if stance == "conditional_acceptance":
        if _COUNTRY_ACCEPTANCE.search(text) and not _TERM_REJECTION.search(text):
            return "conditional_acceptance"
        return "mentioned"
    if stance == "reject" and _TERM_REJECTION.search(text):
        return "rejected"
    if stance == "counter_offer":
        return "required" if _TERM_REQUIREMENT.search(text) else "proposed"
    return "mentioned"


def _term_stance(item: dict, speaker: str, player: str, pattern: re.Pattern,
                 *, action_id: str | None = None, source_id: str | None = None,
                 required_participants: list[str] | None = None,
                 language: str = "en") -> str:
    text = str(item.get("text", ""))
    decision = item.get("decision")
    ancillary_term_id = next((term_id for term_id in (
        "written_guarantee", "advance_notice", "equal_obligations",
        "no_territorial_concessions", "no_territorial_changes",
        "preserve_sovereignty", "preserve_existing_alliances",
    ) if pattern is _TERM_PATTERNS.get(term_id)), None)
    if (speaker != player and item.get("kind") == "country_response"
            and isinstance(item.get("dialogue_positions"), list) and ancillary_term_id):
        term_point = next((position for position in item.get("dialogue_positions", [])
                           if isinstance(position, dict)
                           and position.get("action_id") == action_id
                           and position.get("term_id") == ancillary_term_id
                           and (not source_id or position.get("source_request_id") == source_id)), None)
        scoped_text = text
        if term_point is not None:
            # Do not re-interpret the prose alone after the point validator has
            # downgraded it. In particular, a model's imperative "Accept the
            # notice" is not the country's consent, even if the legacy text
            # matcher finds the word "accept". The structured position must
            # itself be source-linked, party-authored, and explicitly
            # affirmative before it can confirm this separate term.
            point_stance = _pointwise_term_stance(
                item, str(speaker or "").upper(), action_id, source_id,
                [str(tag).upper() for tag in (required_participants or [])],
                term_id=ancillary_term_id,
            )
            return point_stance if point_stance in {
                "accepted", "conditional_acceptance", "rejected", "required", "proposed",
            } else "mentioned"
        matching_clauses = [clause for clause in _clauses(scoped_text) if pattern.search(clause)]
        if not matching_clauses:
            # A pointwise acceptance of the pact/action is never inherited by
            # a separate paper clause that the country did not mention.
            return "mentioned"
        clause = matching_clauses[-1]
        term_match = pattern.search(clause)
        if _TERM_POSITION_UNKNOWN.search(clause):
            return "mentioned"
        if _TERM_OMISSION.search(clause):
            return "rejected"
        requirement = _TERM_REQUIREMENT.search(clause)
        rejection = _TERM_REJECTION.search(clause)
        if requirement and term_match.start() >= requirement.end():
            return "required"
        if rejection:
            return "rejected"
        if _HEDGED_CONSIDERATION.search(clause) or _CONDITIONAL.search(clause):
            return "conditional_acceptance"
        scoped_acceptance = bool(
            _COUNTRY_ACCEPTANCE.search(clause)
            and not re.search(r"\b(?:but|however|although|while|но|однако|mais|але)\b", clause, re.I)
        )
        return "accepted" if scoped_acceptance else "mentioned"
    if speaker != player and isinstance(item.get("dialogue_positions"), list):
        stance = _pointwise_term_stance(
            item, str(speaker or "").upper(), action_id, source_id,
            [str(tag).upper() for tag in (required_participants or [])],
            term_id=ancillary_term_id,
        ) or "mentioned"
        if (stance in {"proposed", "required"} and pattern.search(text)
                and _TERM_OMISSION.search(text)):
            return "rejected"
        if stance == "accepted":
            requirement = _TERM_REQUIREMENT.search(text)
            if requirement and pattern.search(text, requirement.end()):
                return "required"
        return stance
    matching_clauses = [clause for clause in _clauses(text) if pattern.search(clause)]
    if not matching_clauses:
        return "mentioned"
    # Resolve each condition from the clause that actually states it. A separate
    # condition elsewhere in the reply must not make every term conditional.
    clause = matching_clauses[-1]
    conditional_match = _is_condition_precedent(clause)
    conditional_position = _CONDITIONAL.search(clause).start() if conditional_match else -1
    acceptance_match = _COUNTRY_ACCEPTANCE.search(clause)
    rejection_match = _TERM_REJECTION.search(clause)
    if _TERM_POSITION_UNKNOWN.search(clause):
        return "mentioned"
    if _HEDGED_CONSIDERATION.search(clause):
        if _TERM_REQUIREMENT.search(clause):
            return "required"
        return "mentioned"
    if speaker == player:
        action = item.get("action")
        if (isinstance(action, dict) and action.get("action_id") in {"embargo", "lift_embargo"}
                and pattern is _TERM_PATTERNS.get("embargo_target")):
            return "proposed"
        explicit_player_assent = _player_acceptance_pattern(
            str(player or "").upper(), language,
        )
        if (_PLAYER_ACCEPTS_REFERENCED_TERMS.search(text)
                and explicit_player_assent.search(text)
                and pattern.search(text)
                and not _TERM_REJECTION.search(clause)
                and not _CONDITIONAL.search(clause)
                and not re.search(r"\b(?:but|however|although|unless|но|однако|але)\b", clause, re.I)):
            return "accepted"
        if any(pattern.search(candidate) for candidate in
                _unconditional_acceptance_clauses(text, explicit_player_assent)):
            return "accepted"
        if pattern is _TERM_PATTERNS["no_territorial_concessions"] and re.search(
                r"без территориальн.{0,20}уступ|территориальн.{0,20}уступ.{0,15}не будет|"
                r"территориальн.{0,20}уступ.{0,15}не предусматрива|no territorial concessions",
                clause, re.I) and not conditional_match:
            return "accepted"
        if re.search(r"\bpropos\w*\b|\boffer\w*\b|\brequir\w*\b|\bпредлага\w*\b|"
                     r"\bтребу(?:ю|ет|ем|ют)\b|\bвношу условие\b", text, re.I):
            return "proposed"
        return "mentioned"
    if item.get("kind") != "country_response":
        return "mentioned"
    if rejection_match:
        return "rejected"
    if _TERM_REQUIREMENT.search(clause):
        return "required"
    if (pattern is _TERM_PATTERNS["no_territorial_concessions"]
            and pattern.search(clause) and re.search(
            r"(?:не требует|does not require|sans concession|без территориальн.{0,20}уступ)",
            clause, re.I) and not conditional_match):
        return "accepted"
    if decision == "accept" or acceptance_match:
        if conditional_match:
            if pattern.search(clause).start() > conditional_position:
                return "required"
            return "conditional_acceptance"
        return "accepted"
    if conditional_match and pattern.search(clause).start() > conditional_position:
        return "required"
    if decision == "counter_offer":
        return "proposed"
    return "mentioned"


def is_agreement_confirmation_question(text: str) -> bool:
    text = str(text or "")
    # A request for per-point positions can also accompany a new proposal or
    # revision. Do not intercept that negotiation turn with the stored-status
    # summary path; it must reach the other signatories and Ollama.
    if _NEW_NEGOTIATION_ACTION.search(text):
        return False
    return bool(_AGREEMENT_CONFIRMATION_QUESTION.search(text)
                or _AGREEMENT_POSITION_REQUEST.search(text))


def agreement_confirmation_review(chat: dict, country: str, player_text: str) -> dict | None:
    """Build a sourced position list for an itemized question or numbered continuation."""
    numbered_continuation = bool(
        re.match(r"\s*\d{1,2}[.)]\s+\S", str(player_text or ""))
        and not _NEW_NEGOTIATION_ACTION.search(str(player_text or ""))
    )
    if not is_agreement_confirmation_question(player_text) and not numbered_continuation:
        return None
    agreements = chat.get("agreements", {})
    agreement = next((item for item in reversed(list(agreements.values()))
                      if item.get("action_id") == "non_aggression_pact"
                      and item.get("status") in {"draft", "awaiting_approval"}
                      and item.get("versions")), None)
    if agreement is None:
        return None
    version = agreement["versions"][agreement.get("current_version", len(agreement["versions"])) - 1]
    messages = chat.get("messages", [])
    # Keep the player's separate requests and IDs, but present the latest
    # same-topic burst as one review context. Numbered short messages are
    # included when an active agreement supplies the topic.
    related_player_messages = []
    player = str(chat.get("player_country", "")).upper()
    term_patterns = [_TERM_PATTERNS[term_id] for term_id in
                     (term.get("term_id") for term in version.get("terms", [])
                      if isinstance(term, dict))
                     if isinstance(term_id, str) and term_id in _TERM_PATTERNS]
    term_patterns.append(re.compile(r"non.aggression|не\s*агресс|ненапад|пакт", re.I))
    for message in messages:
        if str(message.get("speaker", "")).upper() != player or message.get("kind") != "player_message":
            continue
        message_text = str(message.get("text", ""))
        numbered = bool(re.match(r"\s*\d{1,2}[.)]\s+", message_text))
        topical = (is_agreement_confirmation_question(message_text)
                   or any(pattern.search(message_text) for pattern in term_patterns)
                   or bool(re.search(r"договор\w*|соглашен\w*|услови\w*|agreement|contract|terms?", message_text, re.I)))
        if topical or numbered:
            related_player_messages.append({
                "request_id": message.get("request_id"),
                "text": message_text,
            })
    related_player_messages = related_player_messages[-7:]
    reviewed = []
    for term in version.get("terms", []):
        term_id = term.get("term_id")
        if term_id == "non_aggression_pact":
            pattern = re.compile(r"non.aggression|не\s*агресс|ненапад|не\s+нападать|ненападу", re.I)
        else:
            pattern = _TERM_PATTERNS.get(term_id)
        if pattern is None:
            continue
        prior_stance = "mentioned"
        evidence = None
        for message in messages:
            if (str(message.get("speaker", "")).upper() != str(country).upper()
                    or message.get("kind") != "country_response"
                    or not pattern.search(str(message.get("text", "")))):
                continue
            stance = _term_stance(message, country, chat.get("player_country", ""), pattern)
            if stance in {"accepted", "conditional_acceptance", "required", "proposed", "rejected"}:
                prior_stance = stance
                evidence = message
        reviewed.append({
            "term_id": term_id,
            "text": term.get("text", term_id),
            "stance": prior_stance,
            "source_speaker": str(country).upper() if evidence else None,
            "source_request_id": evidence.get("request_id") if evidence else None,
            "source_excerpt": _term_excerpt(str(evidence.get("text", "")), pattern)
            if evidence else None,
        })
    if not reviewed:
        return None
    return {
        "agreement_id": agreement.get("agreement_id"),
        "version": version.get("version", agreement.get("current_version")),
        "country": str(country).upper(),
        "terms": reviewed,
        "player_context": related_player_messages,
        "context_request_ids": [item["request_id"] for item in related_player_messages
                                if item.get("request_id")],
    }


def infer_action_id(chat: dict, current_message: dict) -> str | None:
    analysis = current_message.get("message_analysis")
    source_id = (
        current_message.get("player_source_request_id")
        or (analysis.get("source_request_id") if isinstance(analysis, dict) else None)
    )
    source_message = next((item for item in chat.get("messages", [])
                           if item.get("request_id") == source_id
                           and item.get("kind") == "player_message"), None)
    source_analysis = source_message.get("message_analysis") if isinstance(source_message, dict) else None
    if not isinstance(source_analysis, dict) and isinstance(analysis, dict):
        if analysis.get("source_request_id") == source_id:
            source_analysis = analysis
    if isinstance(source_analysis, dict) and isinstance(source_analysis.get("actions"), list):
        # The proposal author and source action order establish the agreement
        # root. A responding country's local intent must not split a compound
        # proposal into a second agreement.
        for parsed in source_analysis["actions"]:
            if not isinstance(parsed, dict):
                continue
            parsed_action_id = parsed.get("action_id")
            if isinstance(parsed_action_id, str) and ActionRegistry().get(parsed_action_id):
                return parsed_action_id
    if isinstance(source_message, dict):
        source_action = source_message.get("action")
        if isinstance(source_action, dict) and isinstance(source_action.get("action_id"), str):
            if ActionRegistry().get(source_action["action_id"]):
                return source_action["action_id"]
    action = current_message.get("action")
    if isinstance(action, dict) and isinstance(action.get("action_id"), str):
        return action["action_id"]
    if isinstance(analysis, dict) and isinstance(analysis.get("actions"), list):
        # The engine may classify a country turn as information/defer and
        # leave its top-level action empty. Use only a source-grounded,
        # registered action from the same response when building a nonbinding
        # draft; this never grants consent or execution authority.
        for parsed in analysis["actions"]:
            if not isinstance(parsed, dict):
                continue
            parsed_action_id = parsed.get("action_id")
            if isinstance(parsed_action_id, str) and ActionRegistry().get(parsed_action_id):
                return parsed_action_id
    for message in reversed(chat.get("messages", [])[:-1]):
        action = message.get("action")
        if (isinstance(action, dict) and isinstance(action.get("action_id"), str)
                and message.get("decision") in {"counter_offer", "accept"}):
            return action["action_id"]
    for message in reversed(chat.get("messages", [])[:-1]):
        if message.get("kind") == "player_message":
            action = message.get("action")
            if isinstance(action, dict) and action.get("action_id") in {"embargo", "lift_embargo"}:
                return action["action_id"]
    current_text = str(current_message.get("text", ""))
    if _TERM_PATTERNS["military_access"].search(current_text):
        return "military_access"
    if _EMBARGO_TARGET.search(current_text):
        if re.search(r"\b(?:lift|remove|end)\s+(?:the\s+)?embargo|сним\w* эмбарго|сня\w* эмбарго|отмен\w* эмбарго|скасув\w* ембарго|знят\w* ембарго|lever l'embargo", current_text, re.I):
            return "lift_embargo"
        return "embargo"
    if re.search(r"non.aggression|не\s*агресс|ненапад|не\s+нападать|ненападу", current_text, re.I):
        return "non_aggression_pact"
    return None


_REGISTRY_ACTION_UNSET = object()


def build_acceptance_version(chat: dict, current_message: dict, version: int,
                             *, registry_action=_REGISTRY_ACTION_UNSET,
                             action_id_override: str | None = None,
                             previous_versions: list[dict] | None = None) -> dict:
    messages = chat.get("messages", [])
    action_id = action_id_override or infer_action_id(chat, current_message)
    if not action_id:
        raise ValueError("accepted conversation has no registered action to formalize")
    if registry_action is _REGISTRY_ACTION_UNSET:
        registry_action = ActionRegistry().get(action_id)
    if not registry_action:
        raise ValueError("accepted conversation refers to an unknown action")

    prior = messages[:-1]
    last_offer_index = next((i for i in range(len(prior) - 1, -1, -1)
                             if prior[i].get("kind") == "country_response"
                             and prior[i].get("decision") == "counter_offer"
                             and isinstance(prior[i].get("action"), dict)
                             and prior[i]["action"].get("action_id") == action_id), -1)
    window = (prior[last_offer_index:] if last_offer_index >= 0 else prior[-12:]) + [current_message]
    player = chat["player_country"]
    negotiation_participants = list(chat["participants"])
    current_analysis = current_message.get("message_analysis")
    source_id = (
        current_analysis.get("source_request_id")
        if isinstance(current_analysis, dict) else None
    ) or next((position.get("source_request_id")
               for position in current_message.get("dialogue_positions", [])
               if isinstance(position, dict) and position.get("source_request_id")), None)
    # An active draft owns a stable source for its current version. A follow-up
    # player confirmation is a new message, not a replacement proposal; keep
    # positions from both countries attached to that same source/version.
    current_source_message = next((item for item in chat.get("messages", [])
                                   if item.get("request_id") == source_id
                                   and item.get("kind") == "player_message"), None)
    source_actions = ((current_source_message.get("message_analysis") or {}).get("actions", [])
                      if isinstance(current_source_message, dict)
                      and isinstance(current_source_message.get("message_analysis"), dict) else [])
    source_is_new_action_proposal = any(
        isinstance(item, dict) and item.get("action_id") == action_id
        and item.get("communication_act") in {"proposal", "counter_offer", "revision"}
        for item in source_actions
    )
    if previous_versions and action_id == "non_aggression_pact":
        from diplomatic_language import period
        prior_main = next((term for term in previous_versions[-1].get("terms", [])
                           if term.get("term_id") == action_id), {})
        prior_duration = period(((prior_main.get("proposal_spec") or {}).get("action") or {}).get("duration"))
        for parsed in source_actions:
            if (isinstance(parsed, dict) and parsed.get("action_id") == action_id
                    and parsed.get("communication_act") == "acceptance_statement"
                    and parsed.get("parse_status") == "parsed" and not parsed.get("unresolved_fields")
                    and set(parsed.get("actor_country_tags", [])) == set(prior_main.get("required_participants", []))
                    and period(parsed.get("duration")) and prior_duration
                    and period(parsed["duration"]) != prior_duration):
                # Explicit assent to a different, source-visible term is not
                # confirmation of the old version. Keep its own source and
                # obtain all positions for the revised version below.
                source_is_new_action_proposal = True
    if previous_versions and not source_is_new_action_proposal:
        previous_terms = (previous_versions[-1].get("terms", [])
                          if isinstance(previous_versions[-1], dict) else [])
        stable_source = next((term.get("proposal_source_request_id")
                              for term in previous_terms
                              if isinstance(term, dict)
                              and term.get("term_id") == action_id
                              and isinstance(term.get("proposal_source_request_id"), str)
                              and term.get("proposal_source_request_id")), None)
        if stable_source:
            source_id = stable_source
    source_message = next((item for item in chat.get("messages", [])
                           if item.get("request_id") == source_id
                           and isinstance(item.get("message_analysis"), dict)), None)
    response_analysis = current_message.get("message_analysis")
    if source_message:
        # The player-source analysis is canonical across a group's turns. Do
        # not let a later responder's speaking tag rewrite source actor roles.
        current_analysis = deepcopy(source_message["message_analysis"])
        if (isinstance(response_analysis, dict)
                and response_analysis.get("source_request_id") == source_id
                and isinstance(response_analysis.get("actions"), list)):
            # Only accept the separately-provenanced party scope from the
            # response copy; never replace source-grounded direction/targets.
            response_actions = response_analysis["actions"]
            for parsed in current_analysis.get("actions", []):
                if not isinstance(parsed, dict):
                    continue
                matching = next((candidate for candidate in response_actions
                                 if isinstance(candidate, dict)
                                 and candidate.get("action_id") == parsed.get("action_id")
                                 and candidate.get("source_excerpt") == parsed.get("source_excerpt")), None)
                if (matching and isinstance(matching.get("negotiation_party_country_tags"), list)
                        and isinstance(matching.get("party_scope_provenance"), dict)):
                    parsed["negotiation_party_country_tags"] = deepcopy(
                        matching["negotiation_party_country_tags"]
                    )
                    parsed["party_scope_provenance"] = deepcopy(matching["party_scope_provenance"])
    elif (isinstance(response_analysis, dict)
          and response_analysis.get("source_request_id") == source_id
          and isinstance(response_analysis.get("actions"), list)):
        current_analysis = response_analysis
    elif not isinstance(current_analysis, dict) or not isinstance(current_analysis.get("actions"), list):
        current_analysis = None
    action_participants: dict[str, list[str]] = {}
    participant_scope_complete = isinstance(current_analysis, dict)
    if isinstance(current_analysis, dict):
        for parsed in current_analysis.get("actions", []):
            if not isinstance(parsed, dict) or not isinstance(parsed.get("action_id"), str):
                continue
            tags = []
            negotiated_parties = parsed.get("negotiation_party_country_tags")
            if isinstance(negotiated_parties, list):
                tags.extend(str(tag).upper() for tag in negotiated_parties
                            if isinstance(tag, str) and tag.upper() in negotiation_participants)
            else:
                actors = parsed.get("actor_country_tags", [])
                if isinstance(actors, list):
                    tags.extend(str(tag).upper() for tag in actors
                                if isinstance(tag, str) and tag.upper() in negotiation_participants)
                for key in ("recipient_country_tag",):
                    tag = str(parsed.get(key) or "").upper()
                    if tag in negotiation_participants:
                        tags.append(tag)
            if parsed.get("action_id") == "guarantee":
                target_tag = str(parsed.get("target_country_tag") or "").upper()
                if target_tag in negotiation_participants:
                    tags.append(target_tag)
            if tags and player in negotiation_participants:
                # The player authored this proposal and remains a required
                # party to its draft, even when the AI grants a unilateral
                # measure such as access or a guarantee.
                tags.append(player)
            tag_set = set(tags)
            tags = [tag for tag in negotiation_participants if tag in tag_set]
            # Multiple source excerpts can describe the same registered action.
            # Merge their grounded parties; an unscoped duplicate must not erase
            # a valid scope captured from the actual proposal clause.
            action_id_for_scope = parsed["action_id"]
            merged_tags = set(action_participants.get(action_id_for_scope, [])) | set(tags)
            action_participants[action_id_for_scope] = [
                tag for tag in negotiation_participants if tag in merged_tags
            ]
            if not tags:
                participant_scope_complete = False
        if not action_participants:
            participant_scope_complete = False
    else:
        participant_scope_complete = False
    if participant_scope_complete:
        participants = list(dict.fromkeys(
            [player, *(tag for tags in action_participants.values() for tag in tags)]
        ))
        participants = [tag for tag in participants if tag in negotiation_participants]
        # A group observer is not a signatory to a bilateral point. Keep the
        # chat roster separately for transcript/audit purposes.
        if not any(tag != player for tag in participants):
            participant_scope_complete = False
    if not participant_scope_complete:
        # When party roles cannot be source-grounded, require the full chat
        # roster rather than risk approving a draft without a real signatory.
        participants = list(negotiation_participants)
    ai_tags = [tag for tag in participants if tag != player]
    latest_player_message = next((item for item in reversed(window)
                                  if item.get("speaker") == player), None)
    player_assent_pattern = _player_acceptance_pattern(
        str(player or "").upper(), chat.get("response_language") or "en",
    )
    player_accept = (latest_player_message if latest_player_message
                     and _unconditional_acceptance_clauses(
                         str(latest_player_message.get("text", "")), player_assent_pattern)
                     else None)
    conditional_player_acceptance = bool(
        latest_player_message
        and any(player_assent_pattern.search(clause) and _is_condition_precedent(clause)
                for clause in _clauses(str(latest_player_message.get("text", ""))))
    )

    ai_acceptances, conditional_ai = {}, {}
    for item in window:
        tag = item.get("speaker")
        if tag not in ai_tags or item.get("kind") != "country_response":
            continue
        item_text = str(item.get("text", ""))
        explicit_accept = bool(_COUNTRY_ACCEPTANCE.search(item_text)
                               and not _TERM_REJECTION.search(item_text)
                               and not _HEDGED_CONSIDERATION.search(item_text))
        if item.get("decision") == "accept" and explicit_accept:
            ai_acceptances[tag] = item
            if _is_condition_precedent(item_text):
                conditional_ai[tag] = item
            else:
                conditional_ai.pop(tag, None)
        elif item.get("decision") == "accept":
            # Preserve an ambiguous/hedged model label as unresolved, never assent.
            ai_acceptances.pop(tag, None)
            conditional_ai[tag] = item
        else:
            ai_acceptances.pop(tag, None)
            conditional_ai.pop(tag, None)

    analysis_source_ids = {
        analysis.get("source_request_id")
        for item in window
        for analysis in [item.get("message_analysis")]
        if isinstance(analysis, dict) and isinstance(analysis.get("source_request_id"), str)
    }
    source_messages = [item for item in messages
                       if item.get("request_id") in analysis_source_ids
                       and item.get("kind") == "player_message"
                       and isinstance(item.get("text"), str)]
    evidence_messages = []
    seen_evidence_ids = set()
    for item in [*window, *source_messages]:
        request_key = item.get("request_id") or id(item)
        if (request_key not in seen_evidence_ids and isinstance(item.get("text"), str)):
            seen_evidence_ids.add(request_key)
            evidence_messages.append(item)
    message_positions = {item.get("request_id"): index for index, item in enumerate(messages)
                         if item.get("request_id")}
    evidence_messages.sort(key=lambda item: message_positions.get(
        item.get("request_id"), len(messages)))
    linked_terms_by_request: dict[str, set[str]] = {}
    linked_offer_by_request: dict[str, str] = {}
    linked_offer_versions: dict[str, int] = {}
    for acceptance, is_player in [(player_accept, True), *[(item, False)
                                                            for item in ai_acceptances.values()]]:
        if not acceptance or not acceptance.get("request_id"):
            continue
        linked_terms, offer_request_id = _linked_acceptance_terms(
            messages, acceptance, is_player=is_player)
        if linked_terms and offer_request_id:
            linked_terms_by_request[acceptance["request_id"]] = linked_terms
            linked_offer_by_request[acceptance["request_id"]] = offer_request_id
            linked_offer = next((item for item in messages
                                 if item.get("request_id") == offer_request_id), {})
            source_version = linked_offer.get("agreement_version")
            if source_version is None:
                source_version = acceptance.get("agreement_version")
            if isinstance(source_version, int):
                linked_offer_versions[acceptance["request_id"]] = source_version
    terms = []
    proposal_source_text = str(source_message.get("text") or "") if source_message else ""
    from guarded_treaty_effects import reciprocal_direction_unresolved
    bundle_patterns = {
        "non_aggression_pact": re.compile(r"non.aggression|не\s*агресс|ненапад|не\s+нападать|ненападу", re.I),
        "guarantee": _GUARANTEE_TERM,
        "military_access": _TERM_PATTERNS["military_access"],
    }
    bundled_actions = {
        item["action_id"] for item in (current_analysis or {}).get("actions", [])
        if isinstance(item, dict) and item.get("action_id") in bundle_patterns
        and (item.get("parse_status") == "parsed" and not item.get("unresolved_fields")
             or reciprocal_direction_unresolved(
                 {"action_id":item["action_id"], "text":item.get("source_excerpt")}, {}))
        and not item.get("context_reference")
        and item.get("communication_act") not in {"question", "rejection_statement"}
        and item.get("polarity") not in {"negated", "conditional"}
        and item.get("source_excerpt")
        and item["source_excerpt"].casefold() in proposal_source_text.casefold()
    } if action_id in bundle_patterns else set()
    verified_bundle = (isinstance(current_analysis, dict)
                       and current_analysis.get("interpretation_mode") == "explicit_bilateral_clauses"
                       and current_analysis.get("source_request_id") == source_id)
    player_bundle_acceptance = (verified_bundle and current_analysis.get("explicit_revision_acceptance") is True
                                and latest_player_message is not None
                                and latest_player_message.get("request_id") == source_id)
    if player_bundle_acceptance:
        player_accept = latest_player_message

    action_context_cues = {
        "non_aggression_pact": re.compile(
            r"non[ -]?aggression|nonaggression|nenapad|не\s*агресс|пакт\s+о\s+ненапад|"
            r"pacte\s+de\s+non.?agression", re.I,
        ),
        "military_access": re.compile(
            r"military\s+access|troops?\s+(?:to\s+)?(?:enter|transit|pass)|"
            r"военн\w+\s+доступ|военн\w+\s+проход|militärischer\s+zugang", re.I,
        ),
        "guarantee": _GUARANTEE_TERM,
        "political_annexation": _TERM_PATTERNS["political_annexation"],
    }

    def term_belongs_to_action(item: dict, term_pattern: re.Pattern,
                               scoped_action_id: str,
                               scoped_source_id: str | None = None,
                               scoped_term_id: str | None = None) -> bool:
        item_text = str(item.get("text", ""))
        positions = item.get("dialogue_positions")
        if item.get("kind") == "country_response" and isinstance(positions, list):
            # In a compound exchange, conditions in a response belong only to
            # the exact point the country was asked to address.
            matching_positions = [
                position for position in positions
                if isinstance(position, dict)
                and position.get("action_id") == scoped_action_id
                and (not scoped_source_id
                     or position.get("source_request_id") == scoped_source_id)
            ]
            if ((scoped_term_id == "non_aggression_pact" and matching_positions)
                    or ({"independence_guarantee":"guarantee", "military_access":"military_access"}.get(scoped_term_id)
                        == scoped_action_id
                        and scoped_action_id in bundled_actions
                        and any(not position.get("term_id") for position in matching_positions))):
                # A source-linked point can refer to a native clause by its
                # registered action even when the reply says only "the pact
                # term" and does not repeat the full instrument name.
                return True
            if scoped_term_id and item.get("engine_point_policy_verified") is True:
                # Rendered explanations cannot add clauses to the typed policy.
                return any(position.get("term_id") == scoped_term_id
                           or any(term.get("term_id") == scoped_term_id
                                  for term in position.get("additional_terms", [])
                                  if isinstance(term, dict))
                           for position in matching_positions)
            return any(
                position in matching_positions
                and term_pattern.search(
                    str(position.get("source_excerpt") or "") + " "
                    + str(position.get("text") or "")
                )
                for position in matching_positions
            )
        if not term_pattern.search(item_text):
            return False
        if len(action_participants) <= 1:
            return True
        analysis = item.get("message_analysis")
        if isinstance(analysis, dict):
            if any(
                isinstance(parsed, dict)
                and parsed.get("action_id") == scoped_action_id
                and term_pattern.search(str(parsed.get("source_excerpt") or ""))
                for parsed in analysis.get("actions", [])
            ):
                return True
        action_cue = action_context_cues.get(scoped_action_id)
        if action_cue:
            clauses = [part.strip() for part in re.split(r"[.!?;\n]+", item_text) if part.strip()]
            if any(term_pattern.search(clause) and action_cue.search(clause)
                   for clause in clauses):
                return True
            # A single-action proposal often states the executable instrument
            # and its paper conditions in separate clauses (e.g. a NAP plus
            # advance notice). Bind such a condition to that instrument only
            # when the source analysis contains exactly that one action. With
            # multiple actions, require an explicit same-clause/source link.
            analysis_actions = (analysis.get("actions", [])
                                if isinstance(analysis, dict) else [])
            scoped_actions = {
                str(parsed.get("action_id"))
                for parsed in analysis_actions if isinstance(parsed, dict)
                and parsed.get("action_id")
            }
            return bool(
                len(scoped_actions) == 1
                and scoped_action_id in scoped_actions
                and any(term_pattern.search(clause) for clause in clauses)
            )
        return False

    def add_term(term_id: str, pattern: re.Pattern, category: str, supported: bool) -> None:
        term_family = "guarantee" if term_id == "independence_guarantee" else term_id
        term_action_id = term_family if term_family in action_participants else action_id
        term_required_participants = action_participants.get(term_action_id) or participants
        matches = [
            item for item in evidence_messages
            if (
                (term_id in linked_terms_by_request.get(
                    str(item.get("request_id", "")), set())
                 and (not source_id or linked_offer_by_request.get(
                     str(item.get("request_id", ""))) == source_id))
                or term_belongs_to_action(
                    item, pattern, term_action_id, source_id, term_id,
                )
            )
        ]
        latest_by_speaker = {}
        source_ids, evidence = [], []
        for item in matches:
            speaker = item.get("speaker")
            request_key = str(item.get("request_id", ""))
            linked_acceptance = (
                term_id in linked_terms_by_request.get(request_key, set())
                and (not source_id or linked_offer_by_request.get(request_key) == source_id)
            )
            explicit_bundle_term = (player_bundle_acceptance and speaker == player
                                    and request_key == source_id and term_action_id in bundled_actions
                                    and category == "hoi4_game_effect")
            stance = "accepted" if linked_acceptance or explicit_bundle_term else _term_stance(
                item, speaker, player, pattern, action_id=term_action_id,
                source_id=source_id, required_participants=term_required_participants,
                language=chat.get("response_language") or "en",
            )
            request_id = item.get("request_id")
            if speaker in participants and stance in {
                    "accepted", "conditional_acceptance", "required", "proposed", "rejected"}:
                latest_by_speaker[speaker] = (stance, request_id)
            if request_id:
                source_ids.append(request_id)
                evidence.append({
                    "request_id": request_id,
                    "speaker": speaker,
                    "decision": item.get("decision"),
                    "stance": stance,
                    **({"linked_proposal_request_id": linked_offer_by_request[request_id]}
                       if linked_acceptance and request_id in linked_offer_by_request else {}),
                    "excerpt": _term_excerpt(str(item.get("text", "")), pattern),
                })
        confirmed_by = [speaker for speaker, (stance, _) in latest_by_speaker.items()
                        if stance == "accepted"]
        proposed_by = [speaker for speaker, (stance, _) in latest_by_speaker.items()
                       if stance == "proposed"]
        conditional_by = [speaker for speaker, (stance, _) in latest_by_speaker.items()
                          if stance == "conditional_acceptance"]
        required_by = [speaker for speaker, (stance, _) in latest_by_speaker.items()
                       if stance == "required"]
        rejected_by = [speaker for speaker, (stance, _) in latest_by_speaker.items()
                       if stance == "rejected"]
        confirmation_request_ids = {
            speaker: request_id for speaker, (stance, request_id) in latest_by_speaker.items()
            if stance == "accepted" and request_id
        }
        position_request_ids = {
            speaker: request_id for speaker, (_, request_id) in latest_by_speaker.items()
            if request_id
        }
        participant_positions = {}
        for speaker, (stance, request_id) in latest_by_speaker.items():
            position = {
                "stance": stance, "request_id": request_id,
                "participant_role": "party" if speaker in term_required_participants else "observer",
                "required_for_consent": speaker in term_required_participants,
                "consent_status": "unconfirmed", "authority": "advisory_only",
            }
            evidence_item = next((item for item in evidence_messages
                                  if item.get("request_id") == request_id), None)
            response_analysis = (evidence_item.get("message_analysis")
                                 if isinstance(evidence_item, dict) else None)
            linked_source = (response_analysis.get("source_request_id")
                             if isinstance(response_analysis, dict) else None)
            if linked_source == source_id:
                position["source_request_id"] = source_id
                position["response_request_id"] = request_id
            elif speaker == player and request_id and stance == "accepted":
                position["source_request_id"] = source_id
                position["response_request_id"] = request_id
                position["origin"] = "player_explicit"
                position["text"] = str(evidence_item.get("text", ""))[:1200] \
                    if isinstance(evidence_item, dict) else ""
            elif speaker == player and request_id == source_id:
                position["source_request_id"] = source_id
            participant_positions[speaker] = position
        confirmation_source_request_ids = {}
        for speaker, request_id in confirmation_request_ids.items():
            position = participant_positions.get(speaker, {})
            binding = position.get("source_request_id") if isinstance(position, dict) else None
            binding = (binding or linked_offer_by_request.get(request_id)
                       or (source_id if speaker == player else None))
            if isinstance(binding, str) and binding:
                confirmation_source_request_ids[speaker] = binding
        label = _TERM_LABELS.get(term_id, {}).get(
            chat.get("response_language") or "en",
            _TERM_LABELS.get(term_id, {}).get("en", term_id),
        )
        annex_action = next((parsed for parsed in current_analysis.get("actions", [])
                             if isinstance(parsed, dict)
                             and parsed.get("action_id") == "political_annexation"), None) \
            if isinstance(current_analysis, dict) else None
        annex_target = str((annex_action or {}).get("target_country_tag")
                           or (annex_action or {}).get("recipient_country_tag") or "").upper()
        annex_initiator = str((annex_action or {}).get("actor_country_tag") or player).upper()
        if term_id == "political_annexation" and annex_target:
            label = label.format(initiator=annex_initiator, target=annex_target)
        proposal_spec = _proposal_spec_for_term(
            current_analysis, term_action_id, term_id, category,
            term_required_participants, proposal_source_text,
        )
        if category != "hoi4_game_effect":
            proposal_spec = _preserve_prior_ancillary_spec(
                previous_versions, term_id, term_action_id,
                term_required_participants, proposal_spec,
            )
        # Expose the newest structured position for this exact source proposal
        # on the corresponding clause. Explicit source-linked party assent is
        # negotiation consent; `authority=advisory_only` still prevents it from
        # approving or preparing a game action.
        for item in evidence_messages:
            if item.get("kind") != "country_response":
                continue
            speaker = str(item.get("speaker") or "").upper()
            if speaker not in negotiation_participants:
                continue
            for position in item.get("dialogue_positions", []):
                if not isinstance(position, dict) or position.get("action_id") != term_action_id:
                    continue
                point_term_id = (term_id if term_id in {
                    "written_guarantee", "advance_notice", "equal_obligations",
                    "no_territorial_concessions", "no_territorial_changes",
                    "preserve_sovereignty", "preserve_existing_alliances",
                } else None)
                if ((point_term_id and position.get("term_id") != point_term_id)
                        or (not point_term_id and position.get("term_id"))):
                    continue
                position_source_id = position.get("source_request_id")
                if not isinstance(source_id, str) or position_source_id != source_id:
                    continue
                response_request_id = position.get("response_request_id") or item.get("request_id")
                stance = position.get("stance")
                text = position.get("text")
                if (not isinstance(response_request_id, str) or not response_request_id
                        or stance not in {"accept", "conditional_acceptance", "counter_offer",
                                          "reject", "discuss", "clarify", "defer"}
                        or not isinstance(text, str) or not text.strip()):
                    continue
                point_stance = _pointwise_term_stance(
                    item, speaker, term_action_id, source_id,
                    [str(tag).upper() for tag in term_required_participants],
                    term_id=point_term_id,
                )
                participant_positions[speaker] = {
                    "stance": stance,
                    "request_id": response_request_id,
                    "source_request_id": position_source_id,
                    "response_request_id": response_request_id,
                    "participant_role": "party" if speaker in term_required_participants else "observer",
                    "required_for_consent": speaker in term_required_participants,
                    "text": text.strip()[:1200],
                    "consent_status": "confirmed" if point_stance == "accepted" else "unconfirmed",
                    "authority": "advisory_only",
                    "origin": position.get("origin", "ollama"),
                }
        terms.append({
            "term_id": term_id,
            "action_id": term_action_id,
            "category": category,
            "text": label,
            "supported_by_hoi4": supported,
            "required_participants": list(term_required_participants),
            "confirmed_by": confirmed_by,
            "confirmed_by_request_ids": confirmation_request_ids,
            "confirmation_source_request_ids": confirmation_source_request_ids,
            "inherited_confirmations": {},
            "proposal_source_request_id": source_id,
            "proposal_spec": proposal_spec,
            "proposed_by": proposed_by,
            "conditional_by": conditional_by,
            "required_by": required_by,
            "rejected_by": rejected_by,
            "position_request_ids": position_request_ids,
            "participant_positions": participant_positions,
            "source_request_ids": list(dict.fromkeys(source_ids)),
            "evidence": evidence,
            "status": ("rejected" if rejected_by else
                       "agreed" if set(term_required_participants).issubset(confirmed_by) else "unconfirmed"),
        })

    if action_id == "non_aggression_pact" or "non_aggression_pact" in bundled_actions:
        add_term("non_aggression_pact", bundle_patterns["non_aggression_pact"], "hoi4_game_effect", True)
    relation_patterns = {
        "white_peace": r"white peace|белый мир|paix blanche",
        "faction_join": r"admitting|принять.+фракц|admettre.+faction",
        "faction_leave": r"removing|исключить.+фракц|exclure.+faction",
    }
    from guarded_treaty_effects import PROPOSAL_CUES
    relation_patterns.update(PROPOSAL_CUES)
    if action_id in relation_patterns:
        add_term(action_id, re.compile(relation_patterns[action_id], re.I), "hoi4_game_effect", True)
    if action_id == "political_annexation":
        add_term("political_annexation", _TERM_PATTERNS["political_annexation"],
                 "hoi4_game_effect", True)
        annex_term = next((term for term in terms if term.get("term_id") == "political_annexation"), None)
        parsed_annex = next((item for item in (current_analysis or {}).get("actions", [])
                             if isinstance(item, dict)
                             and item.get("action_id") == "political_annexation"), None) \
            if isinstance(current_analysis, dict) else None
        target_tag = str((parsed_annex or {}).get("target_country_tag")
                         or (parsed_annex or {}).get("recipient_country_tag") or "").upper()
        initiator_tag = str((parsed_annex or {}).get("actor_country_tag") or player).upper()
        if annex_term is not None:
            annex_term["target_country_tag"] = target_tag or None
            annex_term["initiator_country_tag"] = initiator_tag or None
            annex_term["action_actor"] = initiator_tag or None
            annex_term["action_recipient"] = target_tag or None
            if (not COUNTRY_TAG_RE.fullmatch(target_tag)
                    or target_tag not in annex_term.get("required_participants", [])
                    or target_tag == initiator_tag
                    or initiator_tag != player):
                annex_term["unparsed_execution_details"] = True
    for term_id, pattern in _TERM_PATTERNS.items():
        if action_id == "political_annexation" and term_id == "political_annexation":
            # This clause is already the registered HOI4 effect above. Do not
            # emit a second, unenforceable paper-only copy of the same term.
            continue
        if action_id in {"embargo", "lift_embargo"} and term_id == "embargo_target":
            # This term is added below as the single real HOI4 effect, not a
            # second unenforced diplomatic promise.
            continue
        if (action_id == "military_access" or "military_access" in bundled_actions) and term_id == "military_access":
            continue
        if action_id == "guarantee" and term_id == "written_guarantee":
            # In a guarantee action, generic mentions of "guarantee" describe
            # the actual HOI4 guarantee effect, not a second paper-only clause.
            continue
        if term_id == "written_guarantee" and "guarantee" in bundled_actions:
            # A native guarantee is one clause. Retain a separate written
            # undertaking only where the source actually requests one.
            pattern = re.compile(r"письменн.{0,45}(?:гаранти|обязательств)|"
                                 r"written.{0,45}(?:guarantee|commitment|undertaking|pledge)|"
                                 r"(?:guarantee|commitment|undertaking|pledge).{0,45}written|"
                                 r"par écrit.{0,45}(?:garant|engag)|(?:garant|engag).{0,45}par écrit", re.I)
        term_action_id = term_id if term_id in action_participants else action_id
        if any(
            (term_id in linked_terms_by_request.get(str(item.get("request_id", "")), set())
             and (not source_id or linked_offer_by_request.get(
                 str(item.get("request_id", ""))) == source_id))
             or term_belongs_to_action(
                 item, pattern, term_action_id, source_id, term_id,
             )
            for item in evidence_messages
        ):
            add_term(term_id, pattern, "diplomatic_commitment", False)
    if action_id == "guarantee" or "guarantee" in bundled_actions:
        add_term("independence_guarantee", _GUARANTEE_TERM, "hoi4_game_effect", True)
        guarantee_term = next(term for term in terms if term["term_id"] == "independence_guarantee")
        if action_id != "guarantee" or guarantee_term.get("proposal_spec"):
            grantor, recipient = directional_parties(guarantee_term)
            if {grantor, recipient} != set(guarantee_term["required_participants"]):
                guarantee_term["unparsed_execution_details"] = True
            else:
                guarantee_term.update(action_actor=grantor, action_recipient=recipient)
        guarantee_term["text"] = directional_term_label(guarantee_term, chat.get("response_language"))
    if action_id == "military_access" or "military_access" in bundled_actions:
        add_term("military_access", _TERM_PATTERNS["military_access"], "hoi4_game_effect", True)
        access_term = next((term for term in terms if term.get("term_id") == "military_access"), None)
        if access_term:
            analysis = current_analysis or next((item.get("message_analysis") for item in reversed(evidence_messages)
                             if isinstance(item.get("message_analysis"), dict)
                             and item.get("message_analysis", {}).get("source_request_id")
                             in access_term.get("source_request_ids", [])), None)
            parsed = next((item for item in analysis.get("actions", [])
                           if item.get("action_id") == "military_access"
                           and item.get("source_request_id") in access_term.get("source_request_ids", [])), None) \
                if isinstance(analysis, dict) else None
            actor, recipient = directional_parties(access_term)
            access_term["action_actor"] = actor or (parsed.get("actor_country_tag") if parsed else None)
            access_term["action_recipient"] = recipient or (parsed.get("recipient_country_tag") if parsed else None)
            access_term["conditions"] = list(parsed.get("conditions", [])) if parsed else []
            access_term["exceptions"] = list(parsed.get("exceptions", [])) if parsed else []
            access_term["duration"] = parsed.get("duration") if parsed else None
            if (not parsed or {access_term["action_actor"], access_term["action_recipient"]}
                    != set(access_term["required_participants"])):
                access_term["unparsed_execution_details"] = True
            access_term["text"] = directional_term_label(access_term, chat.get("response_language"))
    if action_id in {"embargo", "lift_embargo"}:
        add_term("embargo_target", _EMBARGO_TARGET, "hoi4_game_effect", True)
        embargo_term = next((term for term in terms if term.get("term_id") == "embargo_target"), None)
        if embargo_term:
            source_text = next((str(item.get("text", "")) for item in evidence_messages
                                if _EMBARGO_TARGET.search(str(item.get("text", "")))), "")
            target_match = _EMBARGO_TARGET.search(source_text)
            embargo_term["target_country_tag"] = target_match.group(1).upper() if target_match else None
            embargo_term["action_actor"] = None
            analysis = next((item.get("message_analysis") for item in evidence_messages
                             if isinstance(item.get("message_analysis"), dict)
                             and item.get("message_analysis", {}).get("source_request_id")
                             in embargo_term.get("source_request_ids", [])), None)
            parsed = next((item for item in analysis.get("actions", [])
                           if item.get("action_id") == action_id
                           and item.get("source_request_id") in embargo_term.get("source_request_ids", [])), None) \
                if isinstance(analysis, dict) else None
            embargo_term["action_actor"] = parsed.get("actor_country_tag") if parsed else None
            embargo_term["conditions"] = list(parsed.get("conditions", [])) if parsed else []
            embargo_term["exceptions"] = list(parsed.get("exceptions", [])) if parsed else []
            embargo_term["duration"] = parsed.get("duration") if parsed else None
            embargo_term["execution_details_source_request_id"] = (
                parsed.get("source_request_id") if parsed else None
            )
            if (not parsed or not embargo_term.get("action_actor")
                    or (_EMBARGO_UNSUPPORTED_DETAIL.search(source_text)
                    and not parsed) or
                    (_EMBARGO_UNSUPPORTED_DETAIL.search(source_text) and parsed
                     and not (parsed.get("conditions") or parsed.get("exceptions") or parsed.get("duration")))):
                embargo_term["unparsed_execution_details"] = True

    if not terms:
        source = next((item for item in reversed(evidence_messages)
                       if item.get("kind") == "country_response"
                       and item.get("decision") in {"counter_offer", "accept"}), None)
        terms.append({
            "term_id": "unstructured_conditions",
            "category": "diplomatic_commitment",
            "text": source.get("text", "") if source else "No explicit conditions were extracted.",
            "evidence_text": source.get("text", "") if source else None,
            "supported_by_hoi4": bool(registry_action.get("supported_by_hoi4")),
            "implemented_in_eyes": bool(registry_action.get("implemented_in_eyes")),
            "confirmed_by": [], "confirmed_by_request_ids": {}, "proposed_by": [],
            "conditional_by": [], "required_by": [], "rejected_by": [],
            "position_request_ids": {}, "participant_positions": {},
            "source_request_ids": [source["request_id"]] if source and source.get("request_id") else [],
            "evidence": ([{"request_id": source["request_id"], "speaker": source.get("speaker"),
                           "decision": source.get("decision"), "stance": "proposed",
                           "excerpt": str(source.get("text", ""))[:240]}]
                         if source and source.get("request_id") else []),
            "status": "unconfirmed",
        })

    _inherit_unchanged_term_confirmations(
        chat, previous_versions or [], terms,
    )

    explicit_player_acceptance = player_accept is not None
    interpreted_actions = []
    seen_interpretations = set()
    for message in evidence_messages:
        analysis = message.get("message_analysis")
        if not isinstance(analysis, dict):
            continue
        for parsed_action in analysis.get("actions", []):
            if (not isinstance(parsed_action, dict)
                    or parsed_action.get("action_id") == action_id
                    or parsed_action.get("action_id") in action_participants):
                continue
            identity = (parsed_action.get("action_id"), parsed_action.get("source_request_id"),
                        parsed_action.get("source_excerpt"))
            if identity in seen_interpretations:
                continue
            seen_interpretations.add(identity)
            interpreted_actions.append({
                **deepcopy(parsed_action),
                "consent_status": "unconfirmed",
                "execution_status": "not_prepared",
                "authority": "advisory_only",
            })

    unresolved_terms = [term["term_id"] for term in terms if term.get("status") != "agreed"]
    unresolved_terms.extend(
        f"additional_action_requires_separate_agreement:{item['action_id']}"
        for item in interpreted_actions if isinstance(item.get("action_id"), str)
    )
    if not explicit_player_acceptance:
        unresolved_terms.append("player_explicit_acceptance")
    if conditional_player_acceptance:
        unresolved_terms.append(f"conditional_acceptance:{player}")
    # Require an explicit overall acceptance from every participating country
    # as well as pointwise consent. The decision label cannot replace per-term
    # evidence, but a conflicting/non-accept response keeps the draft open.
    if verified_bundle and current_message.get("engine_point_policy_verified") is True:
        speaker = current_message.get("speaker")
        response_id = current_message.get("request_id")
        if (response_id and speaker in ai_tags and speaker not in conditional_ai
                and all(term.get("confirmed_by_request_ids", {}).get(speaker) == response_id for term in terms)):
            ai_acceptances[speaker] = current_message
    if not all(tag in ai_acceptances for tag in ai_tags):
        unresolved_terms.extend(f"acceptance:{tag}" for tag in ai_tags if tag not in ai_acceptances)
    if conditional_ai:
        unresolved_terms.extend(f"conditional_acceptance:{tag}" for tag in conditional_ai)
    unresolved_terms = list(dict.fromkeys(unresolved_terms))

    # Keep model-produced clause positions in the versioned agreement history.
    # Source-linked explicit assent can record negotiation consent, but remains
    # advisory for game execution and never approves or prepares an action.
    negotiation_positions = []
    seen_positions = set()
    relevant_source_ids = {
        position.get("source_request_id")
        for position in current_message.get("dialogue_positions", [])
        if isinstance(position, dict) and isinstance(position.get("source_request_id"), str)
    }
    current_analysis = current_message.get("message_analysis")
    if isinstance(current_analysis, dict) and isinstance(current_analysis.get("source_request_id"), str):
        relevant_source_ids.add(current_analysis["source_request_id"])
    # Group participants can answer one proposal in sequence. Preserve every
    # participant's pointwise position tied to that exact source request, not
    # only the last responder's rows. A later player revision has another
    # request ID and therefore starts a separate pointwise position set.
    position_messages = [*messages, current_message]
    seen_message_ids = set()
    for message in position_messages:
        message_id = message.get("request_id")
        if message_id and message_id in seen_message_ids:
            continue
        if message_id:
            seen_message_ids.add(message_id)
        speaker = str(message.get("speaker") or "").upper()
        for position in message.get("dialogue_positions", []):
            if not isinstance(position, dict):
                continue
            position_action_id = position.get("action_id")
            source_id = position.get("source_request_id")
            response_id = position.get("response_request_id") or message_id
            stance = position.get("stance")
            text = position.get("text")
            if (not isinstance(position_action_id, str)
                    or not ActionRegistry().get(position_action_id)
                    or speaker not in negotiation_participants
                    or source_id not in relevant_source_ids
                    or not isinstance(response_id, str) or not response_id
                    or stance not in {"accept", "conditional_acceptance", "counter_offer",
                                      "reject", "discuss", "clarify", "defer"}
                    or not isinstance(text, str) or not text.strip()):
                continue
            position_term_id = position.get("term_id")
            identity = (position_action_id, source_id, response_id,
                        position_term_id, position.get("point"))
            if identity in seen_positions:
                continue
            seen_positions.add(identity)
            action_parties = action_participants.get(position_action_id, [])
            position_role = (
                "party" if speaker in action_parties else
                "observer" if action_parties and speaker in negotiation_participants else
                "unresolved"
            )
            point_stance = _pointwise_term_stance(
                message, speaker, position_action_id, source_id, action_parties,
                term_id=position_term_id,
            )
            negotiation_positions.append({
                "action_id": position_action_id,
                **({"term_id": position_term_id} if isinstance(position_term_id, str) else {}),
                "point": position.get("point"),
                "source_excerpt": str(position.get("source_excerpt", ""))[:500],
                "source_request_id": source_id,
                "response_request_id": response_id,
                "participant": speaker,
                "participant_role": position_role,
                "required_for_consent": position_role == "party",
                "party_tags": list(action_parties),
                "stance": stance,
                "text": text.strip()[:1200],
                "consent_status": "confirmed" if point_stance == "accepted" else "unconfirmed",
                "execution_status": "not_prepared",
                "authority": "advisory_only",
                "origin": position.get("origin", "ollama"),
            })
    status = "awaiting_approval" if not unresolved_terms else "draft"

    language = chat.get("response_language") or "en"
    title_label = {"ru": "Соглашение", "uk": "Угода", "fr": "Accord", "en": "Agreement"}.get(language, "Agreement")
    title = f"{title_label} {participants[0]}–{' / '.join(participants[1:])}"
    disclaimer = {
        "ru": "Письменные гарантии, уведомления и иные дипломатические обязательства не создаются игровым эффектом HOI4.",
        "en": "Written guarantees, notifications, and other diplomatic commitments are not HOI4 game effects.",
        "fr": "Les garanties écrites, notifications et autres engagements ne sont pas des effets de jeu HOI4.",
        "uk": "Письмові гарантії, повідомлення та інші дипломатичні зобов'язання не є ігровими ефектами HOI4.",
    }.get(language, "Written guarantees, notifications, and other diplomatic commitments are not HOI4 game effects.")
    final_text = title + ":\n" + "\n".join(f"• {term['text']}" for term in terms)
    final_text += "\n" + disclaimer
    if interpreted_actions:
        summary = {"ru": "Дополнительные действия распознаны, но не согласованы и не подготовлены:",
                   "en": "Additional actions were identified but are not agreed or prepared:",
                   "fr": "Des actions supplémentaires ont été repérées mais ne sont ni convenues ni préparées:",
                   "uk": "Додаткові дії розпізнані, але не погоджені й не підготовлені:"}.get(language, "Additional actions were identified but are not agreed or prepared:")
        final_text += "\n" + summary + " " + ", ".join(
            f"{item['action_id']} ({item.get('source_request_id') or 'no source ID'})"
            for item in interpreted_actions
        )
    if unresolved_terms:
        clarification = {"ru": "Нужно уточнить", "uk": "Потрібно уточнити",
                         "fr": "À clarifier", "en": "Clarification required"}.get(language, "Clarification required")
        labels = {
            "ru": ("требуется явное согласие", "условие выдвинула", "согласие остаётся условным для"),
            "uk": ("потрібна явна згода", "умову висунула", "згода залишається умовною для"),
            "fr": ("accord explicite requis", "condition posée par", "accord conditionnel de"),
            "en": ("explicit agreement required", "condition proposed by", "conditional acceptance by"),
        }
        consent_label, required_label, conditional_label = labels.get(language, labels["en"])
        final_text += "\n" + clarification + ":"
        for term in terms:
            if term["status"] == "agreed":
                continue
            required_for_term = term.get("required_participants") or participants
            missing = [tag for tag in required_for_term if tag not in term["confirmed_by"]]
            line = f"\n• {term['text']} — {consent_label}: {', '.join(missing) or ', '.join(required_for_term)}"
            if term.get("required_by"):
                line += f"; {required_label}: {', '.join(term['required_by'])}"
            if term.get("conditional_by"):
                line += f"; {conditional_label}: {', '.join(term['conditional_by'])}"
            final_text += line
        if conditional_ai:
            conditional_summary = {"ru": "Франция пока не подтвердила безусловное согласие.",
                                   "uk": "Франція поки не підтвердила безумовну згоду.",
                                   "fr": "La France n'a pas encore confirmé son accord sans condition.",
                                   "en": "France has not yet confirmed unconditional agreement."}.get(language, "France has not yet confirmed unconditional agreement.")
            final_text += "\n" + conditional_summary

    source_ids = [item.get("request_id") for item in evidence_messages if item.get("request_id")]
    response_ids = {tag: item.get("request_id") for tag, item in ai_acceptances.items() if item.get("request_id")}
    conditional_response_ids = {tag: item.get("request_id") for tag, item in conditional_ai.items() if item.get("request_id")}
    player_acceptance_id = player_accept.get("request_id") if player_accept else None
    payload = {
        "version": version,
        "status": status,
        "action_id": action_id,
        "participants": participants,
        "negotiation_participants": negotiation_participants,
        "participant_scope_status": "source_grounded" if participant_scope_complete else "unresolved",
        "action_participants": deepcopy(action_participants),
        "terms": terms,
        "interpreted_actions": interpreted_actions,
        "final_text": final_text,
        "source_request_ids": list(dict.fromkeys(source_ids)),
        # `source_request_ids` also contains player messages and is therefore
        # not an idempotency key for a country turn. Keep the response IDs
        # separate so later signatories can add their positions to the same
        # immutable agreement version.
        "processed_response_request_ids": list(dict.fromkeys(
            item.get("request_id") for item in evidence_messages
            if item.get("kind") == "country_response"
            and isinstance(item.get("request_id"), str) and item.get("request_id")
        )),
        "participant_acceptance_request_ids": {
            **({player: player_acceptance_id} if player_acceptance_id else {}), **response_ids,
        },
        "participant_term_confirmation_request_ids": {
            term["term_id"]: deepcopy(term.get("confirmed_by_request_ids", {}))
            for term in terms
        },
        "participant_conditional_acceptance_request_ids": conditional_response_ids,
        "participant_linked_proposal_request_ids": {
            request_id: linked_offer_by_request[request_id]
            for request_id in linked_terms_by_request if request_id in linked_offer_by_request
        },
        "participant_linked_proposal_versions": deepcopy(linked_offer_versions),
        "negotiation_positions": negotiation_positions,
        "unresolved_items": unresolved_terms,
        "conditional_acceptance_by": list(conditional_ai),
        "created_at": utc_now(),
        "content_hash": hashlib.sha256(json.dumps({
            "participants": participants, "action_id": action_id, "terms": terms,
            "action_participants": action_participants,
            "interpreted_actions": interpreted_actions,
            "negotiation_positions": negotiation_positions,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
    }
    return payload


def create_or_update_agreement(chat: dict, current_message: dict) -> dict | None:
    policy = chat.get("engine_point_policies", {}).get(current_message.get("request_id"))
    positions = current_message.get("dialogue_positions")
    if policy and isinstance(positions, list):
        current_message["engine_point_policy_verified"] = (
            len(policy) == len(positions) and all(
                row.get("origin") == "engine_policy" and row.get("stance") == saved.get("stance")
                and row.get("action_id") == saved.get("action_id") and row.get("term_id") == saved.get("term_id")
                for row, saved in zip(positions, policy)))
    decision = current_message.get("decision")
    analysis = current_message.get("message_analysis")
    analyzed_action_ids = {
        item.get("action_id") for item in analysis.get("actions", [])
        if isinstance(item, dict) and isinstance(item.get("action_id"), str)
    } if isinstance(analysis, dict) and isinstance(analysis.get("actions"), list) else set()
    source_request_id = analysis.get("source_request_id") if isinstance(analysis, dict) else None
    response_request_id = current_message.get("request_id")
    point_positions = current_message.get("dialogue_positions")
    positioned_proposal = bool(
        isinstance(source_request_id, str) and source_request_id
        and isinstance(response_request_id, str) and response_request_id
        and isinstance(point_positions, list)
        and any(
            isinstance(item, dict)
            and item.get("action_id") in analyzed_action_ids
            and ActionRegistry().get(item.get("action_id"))
            and item.get("source_request_id") == source_request_id
            and item.get("response_request_id") == response_request_id
            and item.get("stance") in {"accept", "conditional_acceptance", "counter_offer",
                                        "reject", "discuss", "clarify", "defer"}
            and item.get("consent_status") == "unconfirmed"
            and item.get("authority") == "advisory_only"
            for item in point_positions
        )
    )
    compound_proposal = bool(
        decision in {"defer", "counter_offer"}
        and isinstance(analysis, dict)
        and isinstance(analysis.get("actions"), list)
        and len([item for item in analysis["actions"]
                 if isinstance(item, dict) and isinstance(item.get("action_id"), str)
                 and ActionRegistry().get(item["action_id"])]) > 1
    )
    action_id = infer_action_id(chat, current_message)
    # A generated clarification/summary can still close the loop on an
    # explicitly confirmed condition list. Bind it only when the same chat has
    # one unambiguous draft and the player names their own assent to those
    # conditions. This refreshes that draft; it does not accept the AI party's
    # position or approve execution.
    agreements = chat.get("agreements", {})
    source_request_id_for_refresh = (
        current_message.get("player_source_request_id")
        or (analysis.get("source_request_id") if isinstance(analysis, dict) else None)
    )
    source_player_message = next((item for item in chat.get("messages", [])
                                  if item.get("request_id") == source_request_id_for_refresh
                                  and item.get("kind") == "player_message"
                                  and str(item.get("speaker", "")).upper()
                                  == str(chat.get("player_country", "")).upper()), None)
    player_text = str(source_player_message.get("text", "")) if source_player_message else ""
    player_tag = str(chat.get("player_country", "")).upper()
    player_assent = _player_acceptance_pattern(
        player_tag, chat.get("response_language") or "en",
    )
    explicit_condition_confirmation = bool(
        player_text
        and player_assent.search(player_text)
        and _PLAYER_ACCEPTS_REFERENCED_TERMS.search(player_text)
        and not _TERM_REJECTION.search(player_text)
        and not _is_condition_precedent(player_text)
        and any(pattern.search(player_text) for term_id, pattern in _TERM_PATTERNS.items()
                if term_id in {"advance_notice", "equal_obligations", "no_territorial_concessions",
                               "no_territorial_changes", "preserve_sovereignty",
                               "preserve_existing_alliances", "written_guarantee"})
    )
    refresh_candidates = [item for item in agreements.values()
                          if isinstance(item, dict) and item.get("status") == "draft"
                          and player_tag in item.get("participants", [])]
    explicit_active_confirmation = (
        explicit_condition_confirmation and len(refresh_candidates) == 1
    )
    if explicit_active_confirmation:
        action_id = action_id or refresh_candidates[0].get("action_id")
    if (decision not in {"accept", "counter_offer", "reject"}
            and not compound_proposal and not positioned_proposal
            and not explicit_active_confirmation):
        return None
    if not action_id or not ActionRegistry().get(action_id):
        return None
    agreements = chat.setdefault("agreements", {})
    matching_active = []
    for item in agreements.values():
        if item.get("status") not in {"draft", "awaiting_approval"}:
            continue
        latest = (item.get("versions") or [{}])[-1]
        term_action_ids = {
            term.get("term_id") for term in latest.get("terms", [])
            if isinstance(term, dict) and isinstance(term.get("term_id"), str)
        }
        if item.get("action_id") == action_id or term_action_ids.intersection(analyzed_action_ids):
            matching_active.append(item)
    exact_root = [item for item in matching_active if item.get("action_id") == action_id]
    if len(exact_root) == 1:
        active = exact_root[0]
    elif len(matching_active) == 1:
        active = matching_active[0]
    elif len(matching_active) > 1:
        # Several open drafts overlap this source. Do not merge or create an
        # accidental duplicate until the player identifies the intended one.
        return None
    else:
        active = None
    if active:
        action_id = active.get("action_id") or action_id
        source_actions = [
            item for item in (analysis.get("actions", []) if isinstance(analysis, dict) else [])
            if isinstance(item, dict) and item.get("action_id") == action_id
        ]
        current_counteroffers = [
            item for item in (point_positions if isinstance(point_positions, list) else [])
            if isinstance(item, dict) and item.get("action_id") == action_id
            and item.get("stance") == "counter_offer"
            and item.get("source_request_id") == source_request_id
        ]
        if (decision != "accept" and source_actions
                and all(item.get("communication_act") == "question" for item in source_actions)
                and not current_counteroffers and not explicit_active_confirmation):
            # A request for the counterpart to formulate a counteroffer does
            # not revise the current contract version. Keep the open version
            # stable until a structured, source-linked counteroffer exists.
            # An explicit player confirmation of the same active draft is a
            # separate case: merge its verified point positions into the draft
            # without treating the question itself as a contract revision.
            return deepcopy(active)
    # Non-acceptance replies may update a draft only with item-level evidence.
    # They can never create or approve an agreement on their own.
    if decision != "accept" and active is None and not compound_proposal and not positioned_proposal:
        return None
    current_response_request_id = current_message.get("request_id")
    if active and active.get("versions") and current_response_request_id:
        latest_version = active["versions"][-1]
        processed_response_ids = latest_version.get("processed_response_request_ids")
        if not isinstance(processed_response_ids, list):
            # Backward compatibility for versions created before this field:
            # only actual country response references count as processed.
            processed_response_ids = [
                position.get("response_request_id")
                for position in latest_version.get("negotiation_positions", [])
                if isinstance(position, dict)
            ]
            processed_response_ids.extend(
                value for value in latest_version.get("participant_acceptance_request_ids", {}).values()
                if isinstance(value, str)
            )
        if current_response_request_id in processed_response_ids:
            return deepcopy(active)
    agreement_id = active["agreement_id"] if active else uuid.uuid4().hex
    version = len(active.get("versions", [])) + 1 if active else 1
    version_record = build_acceptance_version(
        chat, current_message, version, action_id_override=action_id,
        previous_versions=active.get("versions", []) if active else None,
    )
    named_source = _agreement_name_from_sources(chat, version_record)
    if active:
        previous = active["versions"][-1]
        if current_message.get("engine_point_policy_verified") is True:
            def proposal_identity(record):
                return {"action_id": record.get("action_id"),
                    "participants": record.get("participants"),
                    "terms": [{"term_id": term.get("term_id"), "action_id": term.get("action_id"),
                               "required_participants": term.get("required_participants"),
                               "proposal_spec": term.get("proposal_spec")}
                              for term in record.get("terms", [])]}
            if _canonical_spec_value(proposal_identity(previous)) == _canonical_spec_value(proposal_identity(version_record)):
                # Consent/evidence advances inside the same proposal version.
                # Model wording and confirmation request IDs are not treaty amendments.
                version_record["version"] = previous["version"]
                version_record["created_at"] = previous["created_at"]
                version_record["content_hash"] = previous["content_hash"]
                active["versions"][-1] = version_record
                active["status"] = version_record["status"]
                active["updated_at"] = utc_now()
                if named_source:
                    active["name"], active["name_request_id"] = named_source
                    active["name_origin"] = "player"
                return deepcopy(active)
        if consent_fingerprint(previous) == consent_fingerprint(version_record):
            if named_source and active.get("name_request_id") != named_source[1]:
                active["name"], active["name_request_id"] = named_source
                active["name_origin"] = "player"
                active["updated_at"] = utc_now()
            return deepcopy(active)
        active["versions"].append(version_record)
        active["current_version"] = version
        active["status"] = version_record["status"]
        if named_source:
            active["name"], active["name_request_id"] = named_source
            active["name_origin"] = "player"
        active["updated_at"] = utc_now()
        return deepcopy(active)
    participants = list(chat.get("participants", []))
    agreement = {
        "agreement_id": agreement_id,
        "chat_id": chat.get("chat_id"),
        "campaign_id": chat.get("campaign_id"),
        "conversation_id": chat.get("conversation_id"),
        "action_id": action_id,
        "participants": participants,
        "name": named_source[0] if named_source else _default_agreement_name(action_id, participants),
        "name_origin": "player" if named_source else "default",
        "name_request_id": named_source[1] if named_source else None,
        "signed_at": None,
        "status": version_record["status"],
        "current_version": version,
        "versions": [version_record],
        "approval_requests": {},
        "created_at": version_record["created_at"],
        "updated_at": version_record["created_at"],
        "execution_package": None,
        "execution_request_id": None,
        "execution_result": None,
    }
    agreements[agreement_id] = agreement
    return deepcopy(agreement)


def _political_annexation_localisation(namespace: str, player: str, target: str,
                                       non_game_terms: list[str],
                                       agreement_name: str | None = None) -> str:
    """Render the annexation confirmation event in HOI4's installed languages."""
    safe_terms = [value for value in non_game_terms
                  if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value)]
    term_list = ", ".join(safe_terms) if safe_terms else "none"
    safe_name = agreement_name if isinstance(agreement_name, str) and re.fullmatch(
        r"[\w \-–—:,.()]{3,80}", agreement_name, re.UNICODE,
    ) else None
    texts = {
        "l_english": (
            "Review peaceful political annexation",
            "This permanently transfers the entire target country to the initiator and removes the target country tag if the effect succeeds. The apply option is available only while both signatories exist, the target owns territory, and neither is at war. No rollback is provided. Additional agreed terms are not enforced by this effect: {terms}.",
            "Confirm annexation of {target} into {player}",
            "Cancel; make no territorial change",
            "[{player}.GetName] and [?event_target:{namespace}_annexed.GetName] complete a political union",
            "The game has confirmed the peaceful union of {player} and [?event_target:{namespace}_annexed.GetName].",
        ),
        "l_braz_por": (
            "Revisar a anexação política pacífica",
            "Este efeito transfere permanentemente todo o país-alvo para o iniciador e remove a tag do alvo se for bem-sucedido. A opção de aplicar só fica disponível enquanto os dois signatários existirem, o alvo possuir território e nenhum estiver em guerra. Não há reversão. Os demais termos acordados não são aplicados por este efeito: {terms}.",
            "Confirmar a anexação de {target} por {player}",
            "Cancelar; não alterar território",
            "[{player}.GetName] e [?event_target:{namespace}_annexed.GetName] concluem uma união política",
            "O jogo confirmou a união pacífica de {player} e [?event_target:{namespace}_annexed.GetName].",
        ),
        "l_french": (
            "Examiner l’annexion politique pacifique",
            "Cet effet transfère définitivement tout le pays cible à l’initiateur et supprime son tag si l’effet réussit. L’option d’application n’est disponible que si les deux signataires existent, si la cible possède un territoire et si aucun n’est en guerre. Aucun retour en arrière n’est prévu. Les autres clauses convenues ne sont pas appliquées par cet effet : {terms}.",
            "Confirmer l’annexion de {target} par {player}",
            "Annuler ; ne modifier aucun territoire",
            "[{player}.GetName] et [?event_target:{namespace}_annexed.GetName] réalisent une union politique",
            "Le jeu a confirmé l’union pacifique de {player} et de [?event_target:{namespace}_annexed.GetName].",
        ),
        "l_german": (
            "Friedliche politische Annexion prüfen",
            "Dieser Effekt überträgt das gesamte Zielland dauerhaft an den Initiator und entfernt bei Erfolg das Länder-Tag des Ziels. Die Option ist nur verfügbar, wenn beide Unterzeichner existieren, das Zielgebiet besitzt und keiner im Krieg ist. Eine Rücknahme ist nicht vorgesehen. Weitere vereinbarte Klauseln werden durch diesen Effekt nicht umgesetzt: {terms}.",
            "Annexion von {target} durch {player} bestätigen",
            "Abbrechen; keine Gebietsänderung",
            "[{player}.GetName] und [?event_target:{namespace}_annexed.GetName] vollziehen eine politische Union",
            "Das Spiel hat die friedliche Vereinigung von {player} und [?event_target:{namespace}_annexed.GetName] bestätigt.",
        ),
        "l_polish": (
            "Sprawdź pokojową aneksję polityczną",
            "Ten efekt trwale przekazuje cały kraj docelowy inicjatorowi i usuwa tag celu, jeśli operacja się powiedzie. Opcja wykonania jest dostępna tylko wtedy, gdy obaj sygnatariusze istnieją, cel posiada terytorium i żaden z nich nie jest w stanie wojny. Nie przewidziano cofnięcia. Pozostałe uzgodnione warunki nie są realizowane przez ten efekt: {terms}.",
            "Potwierdź aneksję {target} przez {player}",
            "Anuluj; nie zmieniaj terytorium",
            "[{player}.GetName] i [?event_target:{namespace}_annexed.GetName] zawierają unię polityczną",
            "Gra potwierdziła pokojową unię {player} i kraju [?event_target:{namespace}_annexed.GetName].",
        ),
        "l_russian": (
            "Проверить мирное политическое присоединение",
            "Эффект навсегда передаст всю целевую страну инициатору и удалит тег цели при успехе. Вариант применения доступен, только если обе стороны существуют, цель владеет территорией и ни одна сторона не ведёт войну. Откат не предусмотрен. Другие согласованные пункты этим эффектом не исполняются: {terms}.",
            "Подтвердить присоединение {target} к {player}",
            "Отмена; не менять территории",
            "[{player}.GetName] и [?event_target:{namespace}_annexed.GetName] заключили соглашение об объединении",
            "Игра подтвердила мирное объединение {player} и страны [?event_target:{namespace}_annexed.GetName].",
        ),
        "l_spanish": (
            "Revisar la anexión política pacífica",
            "Este efecto transfiere permanentemente todo el país objetivo al iniciador y elimina la etiqueta del objetivo si tiene éxito. La opción solo está disponible si ambos signatarios existen, el objetivo posee territorio y ninguno está en guerra. No se ofrece una reversión. Este efecto no aplica las demás cláusulas acordadas: {terms}.",
            "Confirmar la anexión de {target} por {player}",
            "Cancelar; no cambiar el territorio",
            "[{player}.GetName] y [?event_target:{namespace}_annexed.GetName] completan una unión política",
            "El juego ha confirmado la unión pacífica de {player} y [?event_target:{namespace}_annexed.GetName].",
        ),
        "l_japanese": (
            "平和的な政治的併合を確認",
            "この効果が成功すると、対象国全体が恒久的に実行国へ移管され、対象国のタグが削除されます。実行の選択肢は両署名国が存在し、対象国が領土を保有し、双方が戦争中でない場合に限り使用できます。取り消しはできません。その他の合意条項はこの効果では実行されません: {terms}。",
            "{player}による{target}併合を確認",
            "キャンセル（領土を変更しない）",
            "[{player}.GetName]と[?event_target:{namespace}_annexed.GetName]が政治的に統合されました",
            "ゲームは{player}と[?event_target:{namespace}_annexed.GetName]の平和的統合を確認しました。",
        ),
        "l_simp_chinese": (
            "审阅和平政治吞并",
            "此效果会将目标国整体永久移交给发起国；若执行成功，目标国标签将被移除。仅当双方签署国仍存在、目标国拥有领土且双方均未处于战争时，才能选择执行。此操作不可撤销。其他已同意条款不会由此效果执行：{terms}。",
            "确认由{player}吞并{target}",
            "取消；不改变领土",
            "[{player}.GetName]与[?event_target:{namespace}_annexed.GetName]完成政治联合",
            "游戏已确认{player}与[?event_target:{namespace}_annexed.GetName]的和平联合。",
        ),
        "l_korean": (
            "평화적 정치 합병 검토",
            "이 효과가 성공하면 대상국 전체가 개시국에 영구 이전되고 대상국 태그가 제거됩니다. 양측 서명국이 존재하고 대상국이 영토를 보유하며 어느 쪽도 전쟁 중이 아닐 때만 실행할 수 있습니다. 되돌리기는 제공되지 않습니다. 합의된 나머지 조항은 이 효과로 실행되지 않습니다: {terms}.",
            "{player}의 {target} 합병 확인",
            "취소; 영토 변경 없음",
            "[{player}.GetName]와 [?event_target:{namespace}_annexed.GetName]가 정치적 통합을 완료했습니다",
            "게임에서 {player}와 [?event_target:{namespace}_annexed.GetName]의 평화적 통합을 확인했습니다.",
        ),
    }
    blocks = []
    for language, (title, description, apply, cancel, news_title, news_desc) in texts.items():
        if safe_name:
            news_title = f"{safe_name}: {news_title}"
        blocks.append(
            f'{language}:\n'
            f' {namespace}.1.t:0 "{title}"\n'
            f' {namespace}.1.d:0 "{description.format(terms=term_list)}"\n'
            f' {namespace}.1.a:0 "{apply.format(target=target, player=player)}"\n'
            f' {namespace}.1.b:0 "{cancel}"\n'
            f' {namespace}.2.t:0 "{news_title.format(player=player, namespace=namespace)}"\n'
            f' {namespace}.2.d:0 "{news_desc.format(player=player, namespace=namespace)}"\n'
            f' {namespace}.2.a:0 "Continue"'
        )
    return "\n".join(blocks) + "\n"


def _guard_native_effect_result(event_file: str, *, namespace: str, player: str,
                                effect: str, marker: str, postcondition: str) -> str:
    """Emit success only when the native relation is observable after execution.

    The handled flag lives in the save and prevents replay even if the native
    relation is subsequently cancelled and the same event is opened again.
    This prepares game script; it does not establish that the game ran it.
    """
    marker_line = f'log = "{marker}"'
    if event_file.count(marker_line) != 1 or event_file.count(effect) != 1:
        raise ValueError("native effect requires a unique effect and result marker")
    flag = f"{namespace}_handled"
    event_file = event_file.replace(
        f"tag = {player}",
        f"tag = {player} NOT = {{ has_country_flag = {flag} }}",
    )
    event_file = event_file.replace(effect, f"set_country_flag = {flag}\n        {effect}", 1)
    # A failed precondition consumes this approved attempt too. Otherwise an
    # old event could later apply after Flask already recorded its failure.
    precondition_failure = marker.replace('ACTION_APPLIED_V2', 'ACTION_FAILED_V2') + '|precondition_changed'
    failure_line = f'log = "{precondition_failure}"'
    event_file = event_file.replace(failure_line, f'set_country_flag = {flag}\n        {failure_line}')
    failure = marker.replace("ACTION_APPLIED_V2", "ACTION_FAILED_V2") + "|postcondition_failed"
    return event_file.replace(marker_line, (
        f"if = {{\n            limit = {{ {postcondition} }}\n"
        f"            {marker_line}\n        }}\n"
        f'        else = {{ log = "{failure}" }}'
    ), 1)


_EFFECT_NEWS_TEXT = {
    "cancel_nap": ("{actor} and {other} have ended their non-aggression pact.",
                   "{actor} и {other} расторгли пакт о ненападении."),
    "cancel_guarantee": ("{actor} has withdrawn its guarantee of {other}.",
                         "{actor} отозвала гарантию независимости страны {other}."),
    "cancel_military_access": ("{actor} has withdrawn military access for {other}.",
                               "{actor} отозвала военный доступ страны {other}."),
    "faction_dismantle": ("The faction led by {actor}, with {other} as its other member, has been dissolved.",
                         "Фракция под руководством {actor}, в которую входила {other}, распущена."),
    "faction_create": ("{actor} has created a new faction with the consent of {other}.",
                       "{actor} создала новую фракцию с согласия страны {other}."),
    "non_aggression_pact": (
        "{actor} and {other} have concluded a non-aggression pact.",
        "{actor} и {other} заключили пакт о ненападении.",
    ),
    "guarantee": (
        "{actor} has guaranteed the independence of {other}.",
        "{actor} предоставляет гарантии независимости государству {other}.",
    ),
    "white_peace": ("{actor} and {other} have concluded a white peace.",
                    "Страны {actor} и {other} заключили белый мир."),
    "faction_join": ("{actor} has admitted {other} to its faction.",
                     "Страна {actor} приняла страну {other} в свою фракцию."),
    "faction_leave": ("{other} has left the faction led by {actor}.",
                      "Страна {other} вышла из фракции под руководством страны {actor}."),
    "military_access": (
        "{actor} has granted military access to {other}.",
        "{actor} предоставляет право военного прохода государству {other}.",
    ),
    "embargo": (
        "{actor} has imposed an embargo on {other}.",
        "{actor} вводит эмбарго против государства {other}.",
    ),
    "lift_embargo": (
        "{actor} has lifted its embargo on {other}.",
        "{actor} отменяет эмбарго против государства {other}.",
    ),
}


def _append_effect_news(event_file: str, localisation: str, namespace: str,
                        result_marker: str, actor: str, other: str,
                        agreement_name: str | None = None, *,
                        publish_news: bool = True) -> tuple[str, str, str]:
    """Dispatch one native world event from the existing verified-success branch."""
    if not publish_news:
        return event_file, localisation, ""
    from native_treaty_news import ACK_TEXT
    marker_line = f'log = "{result_marker}"'
    if event_file.count(marker_line) != 1:
        raise ValueError("success news requires one unique post-effect confirmation marker")
    event_file = event_file.replace(marker_line, f'''{marker_line}
            if = {{
                limit = {{ NOT = {{ has_global_flag = eyes_news_disabled }} }}
                news_event = {{ id = {namespace}.2 }}
            }}''', 1)
    event_file += f'''

news_event = {{
    id = {namespace}.2
    title = {namespace}.2.t
    desc = {namespace}.2.d
    picture = GFX_news_event_generic_sign_treaty1
    major = yes
    is_triggered_only = yes
    option = {{
        name = {namespace}.2.a
        if = {{ limit = {{ is_ai = no }} log = "EYES_HOI4_NEWS_ACK_V1|{namespace}.2" }}
    }}
}}
'''
    action = result_marker.rsplit("|", 1)[-1]
    en, ru = (line.format(actor=f"[{actor}.GetName]", other=f"[{other}.GetName]")
              for line in _EFFECT_NEWS_TEXT[action])
    safe_name = agreement_name if isinstance(agreement_name, str) and re.fullmatch(
        r"[\w \-–—:,.()]{3,80}", agreement_name, re.UNICODE,
    ) else None
    title = f"{actor} / {other}"
    if safe_name:
        en = f"{safe_name}\\n\\n{en}"
    english = localisation.rstrip() + f'''
 {namespace}.2.t:0 "{title}"
 {namespace}.2.d:0 "{en}"
 {namespace}.2.a:0 "{ACK_TEXT['english']}"
'''
    russian = f'''l_russian:
 {namespace}.2.t:0 "{actor} / {other}"
 {namespace}.2.d:0 "{ru}"
 {namespace}.2.a:0 "{ACK_TEXT['russian']}"
'''
    return event_file, english, russian


def build_political_annexation_event_package(chat: dict, agreement: dict, version: dict,
                                             execution_request_id: str) -> dict:
    """Prepare a manually confirmed country-annex event after explicit assent.

    This package does not install or execute itself. The event rechecks both
    countries' peace, target existence, and target-owned-state presence, then
    verifies that the target country tag ceased to exist before writing V2.
    No rollback is offered because HOI4's annex effect has no safe inverse.
    """
    participants = list(agreement.get("participants", []))
    player = chat.get("player_country")
    if (agreement.get("status") != "approved"
            or agreement.get("approved_version") != version.get("version")
            or agreement.get("action_id") != "political_annexation"):
        raise ValueError("political annexation package requires player approval of this exact agreement version")
    if (len(participants) != 2 or player not in participants
            or any(not isinstance(tag, str) or not COUNTRY_TAG_RE.fullmatch(tag)
                   for tag in participants)):
        raise ValueError("political annexation package requires exactly two valid signatories")
    term = next((item for item in version.get("terms", [])
                 if isinstance(item, dict) and item.get("term_id") == "political_annexation"), None)
    if (not term or term.get("status") != "agreed" or term.get("unparsed_execution_details")
            or set(term.get("required_participants", [])) != set(participants)):
        raise ValueError("the exact political annexation term must be explicit and accepted by both signatories")
    target = term.get("target_country_tag")
    initiator = term.get("initiator_country_tag")
    if (not isinstance(target, str) or not COUNTRY_TAG_RE.fullmatch(target)
            or target == player or target not in participants
            or initiator != player):
        raise ValueError("annexation target and initiator must match the two signatories")
    if (not isinstance(execution_request_id, str)
            or not re.fullmatch(
                r"agreement:[0-9a-f]{32}:v[1-9][0-9]*(?::term:[a-z][a-z0-9_]*)?",
                execution_request_id,
            )):
        raise ValueError("annexation execution request ID is malformed")

    digest = hashlib.sha256(
        f"{agreement['agreement_id']}:{version['version']}:{execution_request_id}".encode()
    ).hexdigest()[:12]
    namespace = f"eyes_pa_{digest}"
    pending_state_flag = f"{namespace}_ownership_pending"
    non_game_terms = [
        item.get("term_id") for item in version.get("terms", [])
        if item.get("supported_by_hoi4") is not True and isinstance(item.get("term_id"), str)
    ]
    event_file = f'''add_namespace = {namespace}

country_event = {{
    id = {namespace}.1
    title = {namespace}.1.t
    desc = {namespace}.1.d
    is_triggered_only = yes

    trigger = {{ tag = {player} }}

    option = {{
        name = {namespace}.1.a
        trigger = {{
            tag = {player}
            country_exists = {target}
            NOT = {{ has_war = yes }}
            {target} = {{
                has_war = no
                any_owned_state = {{ always = yes }}
            }}
        }}
        # Mark only this target's current states, then stamp the ownership day on
        # states the initiator actually receives after annex_country succeeds.
        # On failure, clear the transient per-event flags without changing territory.
        {target} = {{
            every_owned_state = {{
                set_state_flag = {pending_state_flag}
            }}
            save_event_target_as = {namespace}_annexed
        }}
        annex_country = {{ target = {target} }}
        if = {{
            limit = {{ NOT = {{ country_exists = {target} }} }}
            every_owned_state = {{
                limit = {{ has_state_flag = {pending_state_flag} }}
                set_variable = {{ var = eyes_owner_since_day value = global.num_days }}
                clr_state_flag = {pending_state_flag}
            }}
            log = "EYES_HOI4_ACTION_APPLIED_V2|{player}|{target}|{execution_request_id}|political_annexation"
            if = {{ limit = {{ NOT = {{ has_global_flag = eyes_news_disabled }} }} news_event = {{ id = {namespace}.2 }} }}
        }}
        else = {{
            {target} = {{
                every_owned_state = {{
                    limit = {{ has_state_flag = {pending_state_flag} }}
                    clr_state_flag = {pending_state_flag}
                }}
            }}
            log = "EYES_HOI4_ACTION_FAILED_V2|{player}|{target}|{execution_request_id}|political_annexation|annex_effect_not_confirmed"
        }}
    }}

    option = {{
        name = {namespace}.1.b
        trigger = {{ tag = {player} }}
        log = "EYES_HOI4_ACTION_FAILED_V2|{player}|{target}|{execution_request_id}|political_annexation|cancelled_by_player"
    }}
}}

news_event = {{
    id = {namespace}.2
    title = {namespace}.2.t
    desc = {namespace}.2.d
    picture = GFX_news_event_generic_sign_treaty1
    major = yes
    is_triggered_only = yes
    option = {{ name = {namespace}.2.a }}
}}
    '''
    localisation = _political_annexation_localisation(
        namespace, player, target, non_game_terms, agreement.get("name"),
    )
    applied = f"EYES_HOI4_ACTION_APPLIED_V2|{player}|{target}|{execution_request_id}|political_annexation"
    failed = f"EYES_HOI4_ACTION_FAILED_V2|{player}|{target}|{execution_request_id}|political_annexation|cancelled_by_player"
    return {
        "status": "manual_required", "action_id": "political_annexation",
        "event_id": f"{namespace}.1",
        "event_file_name": f"eyes_agreement_{digest}.txt",
        "localisation_file_name": f"eyes_agreement_{digest}_l_english.yml",
        "execution_request_id": execution_request_id,
        "open_event_command": f"event {namespace}.1 {player}",
        "result_marker": applied, "failure_marker": failed,
        "game_effect": {
            "effect": "annex_country", "initiator_tag": player,
            "target_tag": target, "scope": "whole target country",
            "unenforced_term_ids": non_game_terms, "rollback_available": False,
        },
        "manual_confirmation_required": True,
        "active_mod_install_required": True, "live_verified": False,
        "execution_command": None,
        "event_file": event_file, "localisation_file": localisation,
    }


def build_political_ultimatum_rejection_event_package(
        chat: dict, source_message: dict, refusal_message: dict) -> dict:
    """Prepare a player-triggered, two-choice response to a verified ultimatum refusal.

    Merely preparing this package changes no game state. The event is generated
    only from an Ollama-authored explicit rejection linked to a player's
    political-annexation ultimatum. Its news is emitted when the player invokes
    the event; the war option remains a separate, explicit in-game choice.
    """
    player = chat.get("player_country")
    target = refusal_message.get("speaker")
    request_id = refusal_message.get("request_id")
    positions = refusal_message.get("dialogue_positions")
    source_request_id = source_message.get("request_id")
    source_text = source_message.get("text")
    if (not isinstance(player, str) or not COUNTRY_TAG_RE.fullmatch(player)
            or not isinstance(target, str) or not COUNTRY_TAG_RE.fullmatch(target)
            or player == target
            or player not in chat.get("participants", [])
            or target not in chat.get("participants", [])
            or refusal_message.get("kind") != "country_response"
            or refusal_message.get("recipient") != player
            or refusal_message.get("provider_status") != "generated"
            or refusal_message.get("decision") != "reject"
            or not isinstance(request_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", request_id)
            or not isinstance(source_request_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", source_request_id)
            or source_message.get("kind") != "player_message"
            or source_message.get("speaker") != player
            or source_message.get("request_id") != source_request_id
            or not isinstance(source_text, str)
            or not re.search(r"\b(?:ultimatum|political demand|demand annexation|under political pressure)\b|"
                             r"ультиматум\w*|политическ\w+ требован|под политическ\w+ давлен",
                             source_text, re.IGNORECASE)
            or not isinstance(positions, list)
            or not any(isinstance(position, dict)
                       and position.get("action_id") == "political_annexation"
                       and position.get("stance") == "reject"
                       and position.get("source_request_id") == source_request_id
                       for position in positions)):
        raise ValueError("a generated, source-linked political-annexation ultimatum refusal is required")

    digest = hashlib.sha256(
        f"{chat.get('campaign_id')}:{chat.get('chat_id')}:{request_id}".encode()
    ).hexdigest()[:12]
    namespace = f"eyes_ur_{digest}"
    event_file = f'''add_namespace = {namespace}

# Prepared only after an explicit generated refusal. This file is inert until
# the player manually invokes the event. No agreement is accepted here.
country_event = {{
    id = {namespace}.1
    title = {namespace}.1.t
    desc = {namespace}.1.d
    picture = GFX_report_event_generic
    is_triggered_only = yes
    fire_only_once = yes
    trigger = {{ tag = {player} country_exists = {target} NOT = {{ has_country_flag = {namespace}_handled }} }}

    immediate = {{
        set_country_flag = {namespace}_handled
        add_threat = 2
        if = {{ limit = {{ NOT = {{ has_global_flag = eyes_news_disabled }} }} news_event = {{ id = {namespace}.2 }} }}
        log = "EYES_HOI4_DIPLOMATIC_REFUSAL_V1|{chat.get('campaign_id')}|{chat.get('chat_id')}|{player}|{target}|{source_request_id}|{request_id}|political_annexation"
    }}

    option = {{
        name = {namespace}.1.a
        trigger = {{ tag = {player} country_exists = {target} NOT = {{ has_war_with = {target} }} }}
        create_wargoal = {{ type = annex_everything target = {target} }}
        add_opinion_modifier = {{ target = {target} modifier = eyes_refused_political_ultimatum }}
        {target} = {{ every_owned_state = {{ add_claim_by = {player} }} }}
        log = "EYES_HOI4_ULTIMATUM_RESPONSE_V1|{player}|{target}|{request_id}|pursue_war"
    }}

    option = {{
        name = {namespace}.1.b
        add_threat = -1
        add_war_support = -0.02
        add_stability = 0.03
        log = "EYES_HOI4_ULTIMATUM_RESPONSE_V1|{player}|{target}|{request_id}|withdraw"
    }}
}}

news_event = {{
    id = {namespace}.2
    title = {namespace}.2.t
    desc = {namespace}.2.d
    picture = GFX_news_event_generic_sign_treaty1
    major = yes
    is_triggered_only = yes
    option = {{ name = {namespace}.2.a }}
}}
    '''
    localisation = f'''l_english:
 {namespace}.1.t:0 "Ultimatum rejected"
 {namespace}.1.d:0 "[${player}.GetName] must now decide how to respond to [${target}.GetName]'s refusal. Choosing force will create a war goal and claims; the alternative withdraws the demand. Neither option changes borders immediately."
 {namespace}.1.a:0 "We will not tolerate this insult!"
 {namespace}.1.b:0 "We will return to diplomacy"
 {namespace}.2.t:0 "[${target}.GetName] rejects [${player}.GetAdjective] ultimatum!"
 {namespace}.2.d:0 "[${target}.GetName] has rejected a political ultimatum from [${player}.GetName]. The refusal has been recorded; no agreement was concluded."
 {namespace}.2.a:0 "The diplomatic crisis deepens"
'''.replace("[$", "[")
    localisation += f'''l_russian:
 {namespace}.1.t:0 "Ультиматум отвергнут"
 {namespace}.1.d:0 "Правительству страны [{player}.GetName] предстоит ответить на отказ страны [{target}.GetName]."
 {namespace}.1.a:0 "Предъявить претензии и получить цель войны"
 {namespace}.1.b:0 "Отозвать требования"
 {namespace}.2.t:0 "Дипломатический кризис: {player} / {target}"
 {namespace}.2.d:0 "Страна [{target}.GetName] отвергла политический ультиматум страны [{player}.GetName]. Соглашение не заключено."
 {namespace}.2.a:0 "Принято к сведению"
'''
    return {
        "status": "manual_required", "action_id": "political_ultimatum_rejection",
        "event_id": f"{namespace}.1", "news_event_id": f"{namespace}.2",
        "event_file_name": f"eyes_ultimatum_{digest}.txt",
        "localisation_file_name": f"eyes_ultimatum_{digest}_l_english.yml",
        "open_event_command": f"event {namespace}.1 {player}",
        "source_request_id": source_request_id, "refusal_request_id": request_id,
        "news_marker": (f"EYES_HOI4_DIPLOMATIC_REFUSAL_V1|{chat.get('campaign_id')}|"
                        f"{chat.get('chat_id')}|{player}|{target}|{source_request_id}|"
                        f"{request_id}|political_annexation"),
        "manual_confirmation_required": True, "game_effect_confirmed": False,
        "event_file": event_file, "localisation_file": localisation,
    }


def build_diplomatic_refusal_event_package(
        chat: dict, source_message: dict, refusal_message: dict) -> dict:
    """Prepare one idempotent world-news event for a final, source-linked refusal.

    This package contains no diplomatic game effect. HOI4 news is shown only
    when the player invokes the generated event after the response is final.
    Technical failures, fallback replies, casual chat, counter-offers, and
    unlinked/ambiguous positions cannot produce a package.
    """
    player = chat.get("player_country")
    target = refusal_message.get("speaker")
    request_id = refusal_message.get("request_id")
    source_request_id = source_message.get("request_id")
    positions = refusal_message.get("dialogue_positions")
    if (not isinstance(player, str) or not COUNTRY_TAG_RE.fullmatch(player)
            or not isinstance(target, str) or not COUNTRY_TAG_RE.fullmatch(target)
            or player == target or player not in chat.get("participants", [])
            or target not in chat.get("participants", [])
            or len(chat.get("participants", [])) != 2
            or refusal_message.get("kind") != "country_response"
            or refusal_message.get("recipient") != player
            or refusal_message.get("provider_status") != "generated"
            or refusal_message.get("retryable") is True
            or refusal_message.get("decision") != "reject"
            or not isinstance(request_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", request_id)
            or not isinstance(source_request_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", source_request_id)
            or source_message.get("kind") != "player_message"
            or source_message.get("speaker") != player
            or source_message.get("request_id") != source_request_id
            or not isinstance(positions, list)):
        raise ValueError("a generated final refusal linked to a bilateral proposal is required")

    linked = [position for position in positions if isinstance(position, Mapping)
              and position.get("stance") == "reject"
              and position.get("source_request_id") == source_request_id
              and isinstance(position.get("action_id"), str)
              and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", position["action_id"])]
    if len(linked) != 1:
        raise ValueError("one unambiguous rejected proposal term linked to the source request is required")
    action_id = linked[0]["action_id"]
    voluntary_union_refusal = False
    if action_id == "political_annexation":
        source_text = str(source_message.get("text") or "")
        voluntary_union_refusal = bool(re.search(
            r"\b(?:voluntary\s+political\s+union|offer\s+(?:a\s+)?union|"
            r"voluntary\s+(?:union|annexation))\b|добровольн\w+\s+(?:политическ\w+\s+)?"
            r"(?:объединен\w+|присоединен\w+)|(?:union\s+volontaire|offre\s+d'union)",
            source_text, re.IGNORECASE,
        ))
        if not voluntary_union_refusal:
            raise ValueError("political pressure refusals require the two-choice ultimatum event")

    campaign_id, chat_id = chat.get("campaign_id"), chat.get("chat_id")
    if (not isinstance(campaign_id, str) or not re.fullmatch(r"[0-9a-f]{32}", campaign_id)
            or not isinstance(chat_id, str) or not re.fullmatch(r"[0-9a-f]{32}", chat_id)):
        raise ValueError("refusal event requires valid campaign and conversation identifiers")
    labels = {
        "non_aggression_pact": ("non-aggression pact", "пакт о ненападении"),
        "guarantee": ("independence guarantee", "гарантию независимости"),
        "embargo": ("embargo", "эмбарго"),
        "lift_embargo": ("embargo cancellation", "отмену эмбарго"),
        "military_access": ("military-access agreement", "соглашение о военном доступе"),
        "political_annexation": ("political annexation", "политическое присоединение"),
    }
    label_en, label_ru = labels.get(action_id, (action_id.replace("_", " "), action_id.replace("_", " ")))
    if voluntary_union_refusal:
        label_en, label_ru = "voluntary political union", "добровольный политический союз"
        refusal_detail_en = (
            f"[${target}.GetName] remains an independent country. No political union or annexation has occurred."
        )
        refusal_detail_ru = (
            f"Страна [${target}.GetName] остаётся независимой. Политическое объединение или присоединение не произошло."
        )
    else:
        refusal_detail_en = "No treaty was signed and no military action has been taken."
        refusal_detail_ru = "Договор не подписан, военные действия не начаты."
    digest = hashlib.sha256(
        f"{campaign_id}:{chat_id}:{source_request_id}:{request_id}:{action_id}".encode()
    ).hexdigest()[:12]
    namespace = f"eyes_dr_{digest}"
    marker = (f"EYES_HOI4_DIPLOMATIC_REFUSAL_V1|{campaign_id}|{chat_id}|{player}|"
              f"{target}|{source_request_id}|{request_id}|{action_id}")
    event_file = f'''add_namespace = {namespace}

country_event = {{
    id = {namespace}.1
    title = {namespace}.1.t
    desc = {namespace}.1.d
    picture = GFX_report_event_generic
    is_triggered_only = yes
    fire_only_once = yes
    trigger = {{ tag = {player} country_exists = {target} NOT = {{ has_country_flag = {namespace}_handled }} }}
    immediate = {{
        set_country_flag = {namespace}_handled
        if = {{ limit = {{ NOT = {{ has_global_flag = eyes_news_disabled }} }} news_event = {{ id = {namespace}.2 }} }}
        log = "{marker}"
    }}
    option = {{ name = {namespace}.1.a }}
}}

news_event = {{
    id = {namespace}.2
    title = {namespace}.2.t
    desc = {namespace}.2.d
    picture = GFX_news_event_generic_sign_treaty1
    major = yes
    is_triggered_only = yes
    option = {{ name = {namespace}.2.a }}
}}
'''
    localisation = f'''l_english:
 {namespace}.1.t:0 "Diplomatic proposal rejected"
 {namespace}.1.d:0 "[${target}.GetName] has rejected [${player}.GetAdjective]'s proposal concerning {label_en}. {refusal_detail_en}"
 {namespace}.1.a:0 "Record the refusal"
 {namespace}.2.t:0 "[${target}.GetName] rejects [${player}.GetAdjective]'s proposal!"
 {namespace}.2.d:0 "[${target}.GetName] has rejected [${player}.GetName]'s proposal concerning {label_en}."
 {namespace}.2.a:0 "The proposal is rejected"
l_russian:
 {namespace}.1.t:0 "Дипломатическое предложение отклонено"
 {namespace}.1.d:0 "Страна [${target}.GetName] отклонила предложение страны [${player}.GetName] о {label_ru}. {refusal_detail_ru}"
 {namespace}.1.a:0 "Зафиксировать отказ"
 {namespace}.2.t:0 "Страна [${target}.GetName] отвергла предложение страны [${player}.GetName]!"
 {namespace}.2.d:0 "Предложение страны [${player}.GetName] о {label_ru} отклонено."
 {namespace}.2.a:0 "Предложение отклонено"
'''.replace("[$", "[")
    return {
        "status": "prepared", "action_id": action_id,
        "event_id": f"{namespace}.1", "news_event_id": f"{namespace}.2",
        "event_file_name": f"eyes_refusal_{digest}.txt",
        "localisation_file_name": f"eyes_refusal_{digest}_l_english.yml",
        "open_event_command": f"event {namespace}.1 {player}",
        "campaign_id": campaign_id, "chat_id": chat_id,
        "source_request_id": source_request_id, "refusal_request_id": request_id,
        "news_marker": marker, "manual_confirmation_required": False,
        "event_file": event_file, "localisation_file": localisation,
    }


def nap_territory_exclusion(term):
    """A restriction on the pact's own effects, never a future border promise."""
    if not isinstance(term, Mapping):
        return False
    spec = term.get("proposal_spec") or {}
    return (isinstance(spec, Mapping) and term.get("term_id") == "no_territorial_changes"
            and term.get("action_id") == "non_aggression_pact"
            and re.fullmatch(r"(?:no territorial changes|без территориальных изменений|"
                             r"aucun changement territorial)[.!]?", str(spec.get("term_clause") or ""), re.I) is not None
            and not any(term.get(key) or spec.get(key) for key in
                        ("conditions", "exceptions", "duration", "unparsed_execution_details"))
            and not spec.get("action"))


def build_non_aggression_event_package(chat: dict, agreement: dict, version: dict,
                                       execution_request_id: str) -> dict:
    """Prepare one guarded, player-confirmed HOI4-native non-aggression pact.

    The game supports ``diplomatic_relation`` for this relation type. HOI4
    itself controls expiry (vanilla ``NAP_EXPIRY_MONTHS`` is 48); the event
    refuses custom duration, conditions, exceptions, or unparsed side clauses
    rather than claiming those terms were applied.
    """
    participants = agreement.get("participants")
    player = chat.get("player_country")
    if (not isinstance(participants, list) or len(participants) != 2
            or any(not isinstance(tag, str) or not COUNTRY_TAG_RE.fullmatch(tag)
                   for tag in participants)
            or len(set(participants)) != 2
            or not isinstance(player, str) or not COUNTRY_TAG_RE.fullmatch(player)
            or player not in participants):
        raise ValueError("non-aggression pact package requires exactly two distinct valid signatories including the player")
    agreement_id = agreement.get("agreement_id")
    version_number = version.get("version")
    if (not isinstance(agreement_id, str) or not re.fullmatch(r"[0-9a-f]{32}", agreement_id)
            or isinstance(version_number, bool) or not isinstance(version_number, int)
            or version_number < 1
            or agreement.get("action_id") != "non_aggression_pact"
            or agreement.get("status") != "approved"
            or agreement.get("current_version") != version_number
            or agreement.get("approved_version") != version_number):
        raise ValueError("non-aggression pact package requires approval of the exact agreement version")
    if (not isinstance(execution_request_id, str)
            or not re.fullmatch(
                rf"agreement:{agreement_id}:v{version_number}(?::term:[a-z][a-z0-9_]*)?",
                execution_request_id)):
        raise ValueError("non-aggression pact execution request ID does not match the approved version")
    terms = version.get("terms")
    if not isinstance(terms, list):
        raise ValueError("non-aggression pact agreement version has no term list")
    nap_terms = [item for item in terms if isinstance(item, dict)
                 and not nap_territory_exclusion(item)
                 and (item.get("action_id") == "non_aggression_pact"
                      or item.get("term_id") == "non_aggression_pact")]
    if len(nap_terms) != 1:
        raise ValueError("the approved version must contain exactly one non-aggression pact term")
    term = nap_terms[0]
    exclusions = [item for item in terms if isinstance(item, dict) and nap_territory_exclusion(item)]
    if len(exclusions) > 1:
        raise ValueError("duplicate territorial exclusion")
    for item in exclusions:
        if (item.get("status") != "agreed" or set(item.get("required_participants", [])) != set(participants)
                or set(item.get("confirmed_by", [])) != set(participants)
                or any(not (item.get("confirmed_by_request_ids") or {}).get(tag) for tag in participants)):
            raise ValueError("all signatories must confirm the territorial exclusion")
    required = term.get("required_participants")
    confirmed = term.get("confirmed_by")
    confirmation_ids = term.get("confirmed_by_request_ids")
    if (term.get("status") != "agreed"
            or not isinstance(required, list) or set(required) != set(participants)
            or not isinstance(confirmed, list) or not set(participants).issubset(set(confirmed))
            or not isinstance(confirmation_ids, dict)
            or any(not isinstance(confirmation_ids.get(tag), str)
                   or not confirmation_ids[tag].strip() for tag in participants)):
        raise ValueError("both signatories must explicitly accept this exact non-aggression pact term")
    proposal = term.get("proposal_spec")
    action_spec = proposal.get("action") if isinstance(proposal, Mapping) else None
    if not isinstance(action_spec, Mapping):
        action_spec = {}
    conditions = term.get("conditions") or action_spec.get("conditions") or []
    exceptions = term.get("exceptions") or action_spec.get("exceptions") or []
    duration = term.get("duration") or action_spec.get("duration")
    if conditions or exceptions or duration or term.get("unparsed_execution_details"):
        raise ValueError(
            "HOI4's native non-aggression effect cannot enforce custom duration, conditions, exceptions, or unparsed clauses"
        )

    other = next(tag for tag in participants if tag != player)
    digest = hashlib.sha256(
        f"{agreement_id}:{version_number}:{execution_request_id}:non_aggression_pact".encode()
    ).hexdigest()[:12]
    namespace = f"eyes_na_{digest}"
    failed_conditions = f"""OR = {{
                NOT = {{ {other} = {{ exists = yes }} }}
                has_war_with = {other}
                {other} = {{ has_war_with = {player} }}
                is_in_faction_with = {other}
                has_non_aggression_pact_with = {other}
            }}"""
    event_file = f'''add_namespace = {namespace}

country_event = {{
    id = {namespace}.1
    title = {namespace}.1.t
    desc = {namespace}.1.d
    is_triggered_only = yes
    trigger = {{ tag = {player} NOT = {{ has_country_flag = {namespace}_handled }} }}

    option = {{
        name = {namespace}.1.a
        trigger = {{
            tag = {player}
            NOT = {{ has_country_flag = {namespace}_handled }}
            {other} = {{ exists = yes }}
            NOT = {{ has_war_with = {other} }}
            {other} = {{ NOT = {{ has_war_with = {player} }} }}
            NOT = {{ is_in_faction_with = {other} }}
            NOT = {{ has_non_aggression_pact_with = {other} }}
        }}
        set_country_flag = {namespace}_handled
        diplomatic_relation = {{ country = {other} relation = non_aggression_pact active = yes }}
        if = {{
            limit = {{ has_non_aggression_pact_with = {other} {other} = {{ has_non_aggression_pact_with = {player} }} }}
            log = "EYES_HOI4_ACTION_APPLIED_V2|{player}|{other}|{execution_request_id}|non_aggression_pact"
        }}
        else = {{
            log = "EYES_HOI4_ACTION_FAILED_V2|{player}|{other}|{execution_request_id}|non_aggression_pact|relation_not_confirmed"
        }}
    }}

    option = {{
        name = {namespace}.1.b
        trigger = {{ tag = {player} NOT = {{ has_country_flag = {namespace}_handled }} {failed_conditions} }}
        set_country_flag = {namespace}_handled
        log = "EYES_HOI4_ACTION_FAILED_V2|{player}|{other}|{execution_request_id}|non_aggression_pact|precondition_changed"
    }}
}}
'''
    extra_terms = [item for item in terms if item is not term and item not in exclusions]
    localisation = f'''l_english:
 {namespace}.1.t:0 "Review non-aggression pact"
 {namespace}.1.d:0 "Confirming creates HOI4's native non-aggression relation between {player} and {other}. The game controls its expiry (vanilla default: 48 months). Custom duration, guarantees, notice periods, and other side clauses require separate supported terms and are not applied by this effect."
 {namespace}.1.a:0 "Confirm and create the pact"
 {namespace}.1.b:0 "Conditions changed; do not create the pact"
'''
    applied_marker = (
        f"EYES_HOI4_ACTION_APPLIED_V2|{player}|{other}|{execution_request_id}|non_aggression_pact"
    )
    event_file, localisation, news_ru = _append_effect_news(
        event_file, localisation, namespace, applied_marker, player, other,
        agreement.get("name") if not extra_terms else None, publish_news=agreement.get("_publish_effect_news", True) is True,
    )
    failure_marker = (
        f"EYES_HOI4_ACTION_FAILED_V2|{player}|{other}|{execution_request_id}|"
        "non_aggression_pact|precondition_changed"
    )
    return {
        "status": "manual_required", "action_id": "non_aggression_pact",
        "event_id": f"{namespace}.1",
        "event_file_name": f"eyes_agreement_{digest}.txt",
        "localisation_file_name": f"eyes_agreement_{digest}_l_english.yml",
        "execution_request_id": execution_request_id,
        "open_event_command": f"event {namespace}.1 {player}",
        "result_marker": applied_marker, "failure_marker": failure_marker,
        "game_effect": {
            "action_id": "non_aggression_pact", "participants": list(participants),
            "vanilla_default_expiry_months": 48,
            "enforced_term_ids": [term.get("term_id"), *(item["term_id"] for item in exclusions)],
            "unenforced_term_ids": [item.get("term_id") for item in extra_terms
                                    if isinstance(item, dict)],
        },
        "manual_confirmation_required": True, "active_mod_install_required": True,
        "live_verified": False, "execution_command": None,
        "event_file": event_file, "localisation_file": localisation,
        "news_localisation_ru": news_ru,
    }

def build_guarantee_event_package(chat: dict, agreement: dict, version: dict,
                                 execution_request_id: str) -> dict:
    """Prepare the agreed, directed independence guarantee."""
    participants = agreement.get("participants", [])
    player = chat.get("player_country")
    if (len(participants) != 2 or player not in participants
            or any(not isinstance(tag, str) or not COUNTRY_TAG_RE.fullmatch(tag)
                   for tag in participants)):
        raise ValueError("a guarantee execution requires exactly two valid country participants")
    other = next(tag for tag in participants if tag != player)
    term = next((item for item in version.get("terms", [])
                 if item.get("term_id") == "independence_guarantee"), {})
    guarantor, beneficiary = directional_parties(term)
    guarantor = guarantor or other
    beneficiary = beneficiary or (player if guarantor == other else other)
    if {guarantor, beneficiary} != set(participants) or guarantor == beneficiary:
        raise ValueError("guarantee direction does not match the signatories")
    digest = hashlib.sha256(
        f"{agreement['agreement_id']}:{version['version']}:{execution_request_id}:guarantee".encode()
    ).hexdigest()[:12]
    namespace = f"eyes_gu_{digest}"
    failed_conditions = f"""OR = {{
                NOT = {{ {other} = {{ exists = yes }} }}
                has_war_with = {other}
                {other} = {{ has_war_with = {player} }}
                {guarantor} = {{ has_guaranteed = {beneficiary} }}
            }}"""
    event_file = f'''add_namespace = {namespace}

country_event = {{
    id = {namespace}.1
    title = {namespace}.1.t
    desc = {namespace}.1.d
    is_triggered_only = yes

    trigger = {{ tag = {player} }}

    option = {{
        name = {namespace}.1.a
        trigger = {{
            tag = {player}
            {other} = {{ exists = yes }}
            NOT = {{ has_war_with = {other} }}
            {other} = {{ NOT = {{ has_war_with = {player} }} }}
            {guarantor} = {{ NOT = {{ has_guaranteed = {beneficiary} }} }}
        }}
        {guarantor} = {{ give_guarantee = {beneficiary} }}
        log = "EYES_HOI4_ACTION_APPLIED_V2|{player}|{other}|{execution_request_id}|guarantee"
    }}

    option = {{
        name = {namespace}.1.b
        trigger = {{
            tag = {player}
            {failed_conditions}
        }}
        log = "EYES_HOI4_ACTION_FAILED_V2|{player}|{other}|{execution_request_id}|guarantee|precondition_changed"
    }}
}}
'''
    localisation = f'''l_english:
 {namespace}.1.t:0 "Review independence guarantee"
 {namespace}.1.d:0 "Confirming asks {guarantor} to guarantee {beneficiary}'s independence using HOI4's guarantee mechanic. The game controls the guarantee's duration. The event will refuse if the country no longer exists, the countries are at war, or the guarantee already exists."
 {namespace}.1.a:0 "Confirm and apply the guarantee"
 {namespace}.1.b:0 "Conditions changed; do not apply"
'''
    applied_marker = f"EYES_HOI4_ACTION_APPLIED_V2|{player}|{other}|{execution_request_id}|guarantee"
    event_file = _guard_native_effect_result(
        event_file, namespace=namespace, player=player,
        effect=f"{guarantor} = {{ give_guarantee = {beneficiary} }}", marker=applied_marker,
        postcondition=(f"{guarantor} = {{ exists = yes has_guaranteed = {beneficiary} }} "
                       f"{beneficiary} = {{ exists = yes is_guaranteed_by = {guarantor} }}"),
    )
    event_file, localisation, news_ru = _append_effect_news(
        event_file, localisation, namespace, applied_marker, guarantor, beneficiary,
        agreement.get("name"), publish_news=agreement.get("_publish_effect_news", True) is True,
    )
    return {
        "status": "manual_required",
        "action_id": "guarantee",
        "event_id": f"{namespace}.1",
        "event_file_name": f"eyes_agreement_{digest}.txt",
        "localisation_file_name": f"eyes_agreement_{digest}_l_english.yml",
        "execution_request_id": execution_request_id,
        "open_event_command": f"event {namespace}.1 {player}",
        "result_marker": applied_marker,
        "failure_marker": f"EYES_HOI4_ACTION_FAILED_V2|{player}|{other}|{execution_request_id}|guarantee|precondition_changed",
        "game_effect": {
            "action_id": "guarantee",
            "guarantor": guarantor,
            "beneficiary": beneficiary,
            "duration_days": None,
            "enforced_term_ids": ["independence_guarantee"],
            "unenforced_term_ids": [item.get("term_id") for item in version.get("terms", [])
                                    if item.get("term_id") != "independence_guarantee"],
        },
        "manual_confirmation_required": True,
        "active_mod_install_required": True,
        "live_verified": False,
        "execution_command": None,
        "event_file": event_file,
        "localisation_file": localisation,
        "news_localisation_ru": news_ru,
    }


def build_embargo_event_package(chat: dict, agreement: dict, version: dict,
                                execution_request_id: str) -> dict:
    """Prepare a manually confirmed real embargo effect, revalidated in HOI4."""
    participants = agreement.get("participants", [])
    player = chat.get("player_country")
    if (len(participants) != 2 or player not in participants
            or any(not isinstance(tag, str) or not COUNTRY_TAG_RE.fullmatch(tag)
                   for tag in participants)):
        raise ValueError("embargo execution requires exactly two valid country participants")
    term = next((item for item in version.get("terms", [])
                 if item.get("term_id") == "embargo_target"), None)
    target = term.get("target_country_tag") if isinstance(term, dict) else None
    if not isinstance(target, str) or not COUNTRY_TAG_RE.fullmatch(target):
        raise ValueError("embargo execution requires an explicit target country tag")
    if target in participants:
        raise ValueError("embargo target must be a third country, not a treaty participant")
    if term.get("status") != "agreed":
        raise ValueError("embargo target has not been explicitly agreed by every participant")
    if (term.get("conditions") or term.get("exceptions") or term.get("duration")
            or term.get("unparsed_execution_details")):
        raise ValueError("HOI4 embargo cannot enforce this clause's conditions, exceptions, or duration")
    actor = term.get("action_actor")
    if actor not in participants:
        raise ValueError("the country imposing or lifting the embargo was not explicitly identified")
    action_id = agreement.get("action_id")
    if action_id not in {"embargo", "lift_embargo"}:
        raise ValueError("unsupported embargo action")

    other = next(tag for tag in participants if tag != player)
    digest = hashlib.sha256(
        f"{agreement['agreement_id']}:{version['version']}:{execution_request_id}:{action_id}".encode()
    ).hexdigest()[:12]
    prefix = "eb" if action_id == "embargo" else "el"
    namespace = f"eyes_{prefix}_{digest}"
    if action_id == "embargo":
        precondition = f"NOT = {{ {actor} = {{ is_embargoing = {target} }} }}"
        effect = f"{actor} = {{ send_embargo = {target} }}"
        unavailable = f"{actor} = {{ is_embargoing = {target} }}"
        title, desc, apply, blocked = (
            "Review embargo proposal",
            f"If you confirm, {actor} will impose a real HOI4 embargo on {target}. This requires By Blood Alone and has no treaty-defined duration; the game determines its duration.",
            "Confirm and impose the embargo", "Conditions changed; do not impose the embargo",
        )
    else:
        precondition = f"{actor} = {{ is_embargoing = {target} }}"
        effect = f"{actor} = {{ break_embargo = {target} }}"
        unavailable = f"NOT = {{ {actor} = {{ is_embargoing = {target} }} }}"
        title, desc, apply, blocked = (
            "Review embargo cancellation",
            f"If you confirm, {actor} will lift its real HOI4 embargo on {target}. This requires By Blood Alone.",
            "Confirm and lift the embargo", "Conditions changed; no active embargo to lift",
        )
    failed = f'OR = {{ NOT = {{ has_dlc = "By Blood Alone" }} NOT = {{ {actor} = {{ exists = yes }} }} NOT = {{ {target} = {{ exists = yes }} }} {unavailable} }}'
    event_file = f'''add_namespace = {namespace}

country_event = {{
    id = {namespace}.1
    title = {namespace}.1.t
    desc = {namespace}.1.d
    is_triggered_only = yes
    trigger = {{ tag = {player} }}

    option = {{
        name = {namespace}.1.a
        trigger = {{ tag = {player} has_dlc = "By Blood Alone" {actor} = {{ exists = yes }} {target} = {{ exists = yes }} {precondition} }}
        {effect}
        log = "EYES_HOI4_ACTION_APPLIED_V2|{player}|{other}|{execution_request_id}|{action_id}"
    }}
    option = {{
        name = {namespace}.1.b
        trigger = {{ tag = {player} {failed} }}
        log = "EYES_HOI4_ACTION_FAILED_V2|{player}|{other}|{execution_request_id}|{action_id}|precondition_changed"
    }}
}}
'''
    localisation = f'''l_english:
 {namespace}.1.t:0 "{title}"
 {namespace}.1.d:0 "{desc}"
 {namespace}.1.a:0 "{apply}"
 {namespace}.1.b:0 "{blocked}"
'''
    marker = f"EYES_HOI4_ACTION_APPLIED_V2|{player}|{other}|{execution_request_id}|{action_id}"
    event_file = _guard_native_effect_result(
        event_file, namespace=namespace, player=player, effect=effect, marker=marker,
        postcondition=(f"{actor} = {{ exists = yes is_embargoing = {target} }}"
                       if action_id == "embargo" else
                       f"{actor} = {{ exists = yes NOT = {{ is_embargoing = {target} }} }}"),
    )
    event_file, localisation, news_ru = _append_effect_news(
        event_file, localisation, namespace, marker, actor, target,
        agreement.get("name"), publish_news=agreement.get("_publish_effect_news", True) is True,
    )
    failure = f"EYES_HOI4_ACTION_FAILED_V2|{player}|{other}|{execution_request_id}|{action_id}|precondition_changed"
    return {
        "status": "manual_required", "action_id": action_id,
        "event_id": f"{namespace}.1",
        "event_file_name": f"eyes_agreement_{digest}.txt",
        "localisation_file_name": f"eyes_agreement_{digest}_l_english.yml",
        "execution_request_id": execution_request_id,
        "open_event_command": f"event {namespace}.1 {player}",
        "result_marker": marker, "failure_marker": failure,
        "game_effect": {"action_id": action_id, "actor": actor, "target": target,
                        "enforced_term_ids": ["embargo_target"], "unenforced_term_ids": []},
        "manual_confirmation_required": True, "active_mod_install_required": True,
        "live_verified": False, "execution_command": None,
        "event_file": event_file, "localisation_file": localisation,
        "news_localisation_ru": news_ru,
    }


def build_military_access_event_package(chat: dict, agreement: dict, version: dict,
                                        execution_request_id: str) -> dict:
    """Prepare the bilateral direction supported by the current agreement UI."""
    participants = agreement.get("participants", [])
    player = chat.get("player_country")
    if (len(participants) != 2 or player not in participants
            or any(not isinstance(tag, str) or not COUNTRY_TAG_RE.fullmatch(tag)
                   for tag in participants)):
        raise ValueError("military access execution requires exactly two valid country participants")
    term = next((item for item in version.get("terms", [])
                 if item.get("term_id") == "military_access"), None)
    if not term or term.get("status") != "agreed":
        raise ValueError("military access has not been explicitly agreed by every participant")
    if term.get("conditions") or term.get("exceptions") or term.get("duration"):
        raise ValueError("HOI4 military access cannot enforce custom conditions or duration")
    actor = term.get("action_actor")
    recipient = term.get("action_recipient")
    if actor not in participants or recipient not in participants or actor == recipient:
        raise ValueError("military access direction was not explicitly identified")
    other = next(tag for tag in participants if tag != player)
    digest = hashlib.sha256(
        f"{agreement['agreement_id']}:{version['version']}:{execution_request_id}:military_access".encode()
    ).hexdigest()[:12]
    namespace = f"eyes_ma_{digest}"
    event_file = f'''add_namespace = {namespace}

country_event = {{
    id = {namespace}.1
    title = {namespace}.1.t
    desc = {namespace}.1.d
    is_triggered_only = yes
    trigger = {{ tag = {player} }}

    option = {{
        name = {namespace}.1.a
        trigger = {{ tag = {player} {actor} = {{ exists = yes }} {recipient} = {{ exists = yes NOT = {{ has_military_access_to = {actor} }} }} {recipient} = {{ NOT = {{ has_war_with = {actor} }} }} }}
        {actor} = {{ give_military_access = {recipient} }}
        log = "EYES_HOI4_ACTION_APPLIED_V2|{player}|{other}|{execution_request_id}|military_access"
    }}
    option = {{
        name = {namespace}.1.b
        trigger = {{ tag = {player} OR = {{ NOT = {{ {actor} = {{ exists = yes }} }} NOT = {{ {recipient} = {{ exists = yes }} }} {recipient} = {{ has_military_access_to = {actor} }} {recipient} = {{ has_war_with = {actor} }} }} }}
        log = "EYES_HOI4_ACTION_FAILED_V2|{player}|{other}|{execution_request_id}|military_access|precondition_changed"
    }}
}}
'''
    localisation = f'''l_english:
 {namespace}.1.t:0 "Review military access"
 {namespace}.1.d:0 "Confirming grants {recipient} military access to {actor}'s territory. HOI4 controls the duration. The apply option is disabled if the counterpart is gone or access already exists."
 {namespace}.1.a:0 "Confirm and grant military access"
 {namespace}.1.b:0 "Conditions changed; do not grant access"
'''
    applied_marker = f"EYES_HOI4_ACTION_APPLIED_V2|{player}|{other}|{execution_request_id}|military_access"
    event_file = _guard_native_effect_result(
        event_file, namespace=namespace, player=player,
        effect=f"{actor} = {{ give_military_access = {recipient} }}", marker=applied_marker,
        postcondition=f"{recipient} = {{ exists = yes has_military_access_to = {actor} }}",
    )
    event_file, localisation, news_ru = _append_effect_news(
        event_file, localisation, namespace, applied_marker, actor, recipient,
        agreement.get("name"), publish_news=agreement.get("_publish_effect_news", True) is True,
    )
    return {
        "status": "manual_required", "action_id": "military_access",
        "event_id": f"{namespace}.1", "event_file_name": f"eyes_agreement_{digest}.txt",
        "localisation_file_name": f"eyes_agreement_{digest}_l_english.yml",
        "execution_request_id": execution_request_id,
        "open_event_command": f"event {namespace}.1 {player}",
        "result_marker": applied_marker,
        "failure_marker": f"EYES_HOI4_ACTION_FAILED_V2|{player}|{other}|{execution_request_id}|military_access|precondition_changed",
        "game_effect": {"action_id": "military_access", "grantor": actor, "recipient": recipient,
                        "duration_days": None, "enforced_term_ids": ["military_access"],
                        "unenforced_term_ids": []},
        "manual_confirmation_required": True, "active_mod_install_required": True,
        "live_verified": False, "execution_command": None,
        "event_file": event_file, "localisation_file": localisation,
        "news_localisation_ru": news_ru,
    }

