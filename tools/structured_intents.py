"""Schema and fail-closed validation for Ollama's advisory message analysis.

This module extracts what a message appears to discuss. It never decides
whether an offer is accepted or whether a HOI4 action is allowed.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from hoi4_country_tags import COUNTRY_TAG_PATTERN, COUNTRY_TAG_RE
from guarded_treaty_effects import NEW_ACTIONS, PROPOSAL_CUES


ACTION_IDS = [
    "non_aggression_pact", "guarantee", "embargo", "lift_embargo",
    "military_access", "docking_rights", "air_base_access", "faction_join", "faction_leave",
    "white_peace", "peace_conference", "territory_partition", "political_annexation", "truce",
    "market_access", *NEW_ACTIONS, "other",
]
COMMUNICATION_ACTS = [
    "proposal", "question", "condition", "exception", "time_limit",
    "acceptance_statement", "rejection_statement", "counter_offer", "explanation", "other",
]

# These are conversation-level speech acts, not treaty terms.  Keeping them
# separate from ACTION_IDS prevents a sentence about acknowledging a refusal
# from being parsed as another vote on the rejected treaty.
DIPLOMATIC_COMMUNICATION_ACTS = {
    "accept_offer", "acknowledge_refusal", "request_explanation",
    "request_alternative", "reject_offer", "counteroffer",
}

_ACKNOWLEDGE_REFUSAL = re.compile(
    r"\b(?:accept|accepts|accepted|acknowledge|acknowledges|acknowledged|"
    r"respect|respects|respected)\s+(?:that\s+)?(?:(?:the|your|their|france'?s|germany'?s)\s+)?"
    r"(?:refusal|rejection|decision\s+to\s+(?:refuse|reject|decline))\b|"
    r"\b(?:accept|accepts|accepted)\s+(?:the\s+)?(?:fact\s+)?that\s+"
    r"(?:you|they|france|germany)\s+(?:refused|rejected|declined)\b",
    re.IGNORECASE,
)
_REQUEST_EXPLANATION = re.compile(
    r"\b(?:could|would|can|please|kindly|explain|clarify|tell\s+us|state)\b"
    r".{0,120}\b(?:why|reason\w*|refus\w*|reject\w*|declin\w*|decision|position)\b|"
    r"\bwhy\s+(?:did|does|has)\s+(?:you|your\s+country|france|germany)\b.{0,100}"
    r"(?:refus\w*|reject\w*|declin\w*|decid\w*)\b|"
    r"\b(?:reasons?\s+for|why)\s+(?:your|the|france'?s|germany'?s)\s+"
    r"(?:refus\w*|reject\w*|declin\w*)\b|"
    r"(?:поясніть|поясните|объясни(?:те)?|поясни(?:те)?|назовите|укажите)\b.{0,100}"
    r"(?:причин\w*|відмов\w*|отказ\w*|відхил\w*|отклон\w*)|"
    r"(?:почему|чому)\s+(?:ви|вы|франція|франция|франції|франции)\b.{0,100}"
    r"(?:відмов\w*|отказ\w*|відхил\w*|отклони\w*)|"
    r"(?:pourriez-vous|veuillez|expliquez|précisez)\b.{0,100}"
    r"(?:raison\w*|refus\w*|rejet\w*)",
    re.IGNORECASE,
)
_REQUEST_ALTERNATIVE = re.compile(
    r"\b(?:could|would|can|please|kindly|suggest|propose|offer|recommend|give)\b"
    r".{0,120}\b(?:alternative|another|different|replacement|counterproposal|counter-offer)\b|"
    r"\b(?:alternative|another|different|replacement|counterproposal|counter-offer)\b"
    r".{0,100}\b(?:could|would|can|please|kindly|suggest|propose|offer|recommend)\b|"
    r"(?:предложите|предложи|предложить|можете предложить|может предложить|запропонуйте|"
    r"запропонуй|можете запропонувати)\b.{0,120}"
    r"(?:альтернатив\w*|інш\w* варіант\w*|друг\w* вариант\w*)|"
    r"(?:pourriez-vous|veuillez|proposez|suggérez)\b.{0,100}(?:alternative|autre accord)",
    re.IGNORECASE,
)
_EXPLICIT_ACCEPT_OFFER = re.compile(
    r"\b(?:accept|accepts|accepted|agree|agrees|agreed|approve|approves|approved|"
    r"ratify|ratifies|ratified)\b.{0,100}\b(?:offer|proposal|pact|agreement|treaty|terms|conditions)\b|"
    r"\b(?:offer|proposal|pact|agreement|treaty|terms|conditions)\b.{0,100}\b"
    r"(?:is|are)\s+(?:acceptable|accepted)\b|"
    r"\b(?:accepte\w*|approuve\w*)\b.{0,100}\b(?:offre|accord|traité|pacte|conditions?)\b|"
    r"(?:соглас\w*|принима\w*|подтвержда\w*).{0,100}(?:предложен\w*|пакт\w*|договор\w*|услов\w*)",
    re.IGNORECASE,
)
_EXPLICIT_REJECT_OFFER = re.compile(
    r"\b(?:reject|rejects|rejected|refuse|refuses|refused|decline|declines|declined|"
    r"do\s+not\s+accept|does\s+not\s+accept|cannot\s+accept|can't\s+accept|won't\s+sign)\b"
    r".{0,100}\b(?:offer|proposal|pact|agreement|treaty|terms|conditions)\b|"
    r"\b(?:offer|proposal|pact|agreement|treaty|terms|conditions)\b.{0,100}\b"
    r"(?:is|are)\s+(?:rejected|refused|unacceptable)\b|"
    r"(?:відхил\w*|відмов\w*|не\s+прийма\w*|не\s+соглас\w*).{0,100}"
    r"(?:пропозиц\w*|пакт\w*|договор\w*|умов\w*|угод\w*)|"
    r"(?:refus\w*|rejet\w*|n'accepte pas).{0,100}(?:offre|accord|traité|pacte|conditions?)",
    re.IGNORECASE,
)
_COUNTEROFFER_MARKER = re.compile(
    r"\b(?:counteroffer|counter-offer|counterproposal|instead|in exchange|only if|"
    r"provided that|on condition that|we would accept if|we can agree if)\b|"
    r"(?:взамен|встречн\w* предложен\w*|лишь при услов\w*|за умови|натомість|contre-proposition|"
    r"en échange|à condition que)",
    re.IGNORECASE,
)


def analyze_communication_acts(source_text: Any) -> dict:
    """Classify a message's conversational function without granting consent.

    A refusal acknowledgement can coexist with requests for an explanation
    and an alternative.  Such a message is never treated as a fresh treaty
    acceptance, rejection, or counteroffer.
    """
    text = str(source_text or "").strip()
    if not text or len(text) > 4000:
        return {"acts": [], "primary_act": "other", "contract_actionable": False}

    acts: list[str] = []
    acknowledges_refusal = bool(_ACKNOWLEDGE_REFUSAL.search(text))
    if acknowledges_refusal:
        acts.append("acknowledge_refusal")
    asks_explanation = bool(_REQUEST_EXPLANATION.search(text))
    asks_alternative = bool(_REQUEST_ALTERNATIVE.search(text))
    if asks_explanation:
        acts.append("request_explanation")
    if asks_alternative:
        acts.append("request_alternative")

    # Remove the acknowledgement clause before checking for a fresh treaty
    # vote.  In "we accept France's refusal of the pact", the verb accept is
    # about the refusal, not the pact itself.  A separate explicit vote on the
    # treaty remains actionable (for example, "we accept the pact and ...").
    vote_text = _ACKNOWLEDGE_REFUSAL.sub(" ", text) if acknowledges_refusal else text
    has_explicit_vote = bool(
        _EXPLICIT_REJECT_OFFER.search(vote_text)
        or _EXPLICIT_ACCEPT_OFFER.search(vote_text)
    )
    # Meta-requests have priority over quoted prior decisions, but do not erase
    # a separate explicit decision about the current offer.
    if acts and not has_explicit_vote:
        return {
            "acts": list(dict.fromkeys(acts)),
            "primary_act": acts[0],
            "contract_actionable": False,
        }

    if _COUNTEROFFER_MARKER.search(vote_text):
        contract_act = "counteroffer"
    elif _EXPLICIT_ACCEPT_OFFER.search(vote_text):
        contract_act = "accept_offer"
    elif _EXPLICIT_REJECT_OFFER.search(vote_text):
        contract_act = "reject_offer"
    else:
        contract_act = None
    if contract_act:
        acts.append(contract_act)
    elif not acts:
        acts.append("other")
    primary = contract_act or acts[0]
    return {
        "acts": acts,
        "primary_act": primary,
        "contract_actionable": primary in {
            "accept_offer", "reject_offer", "counteroffer",
        },
    }

MESSAGE_ANALYSIS_SCHEMA = {
    # Compact keys reduce both constrained-output prompt cost and generation
    # length. The provider expands these into the stable stored representation.
    "type": "object",
    "properties": {
        "analysis": {
            "type": "object",
            "properties": {
                "actions": {"type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "enum": ACTION_IDS},
                        "act": {"type": "string", "enum": COMMUNICATION_ACTS},
                        "src": {"type": "string"},
                        "target": {"type": ["string", "null"]},
                        "tag": {"type": ["string", "null"]},
                        "actors": {"type": "array", "items": {"type": "string"}},
                        "recipient": {"type": ["string", "null"]},
                        "duration": {"type": ["string", "null"]},
                        "if": {"type": "array", "items": {"type": "string"}},
                        "except": {"type": "array", "items": {"type": "string"}},
                        "unclear": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["id", "act", "src", "target", "tag", "actors",
                                 "recipient", "duration", "if", "except", "unclear"],
                    "additionalProperties": False,
                }},
                "links": {"type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "from": {"type": "integer"},
                        "rel": {"type": "string", "enum": [
                            "condition_for", "exception_to", "depends_on", "modifies",
                        ]},
                        "to": {"type": "integer"},
                        "src": {"type": "string"},
                    },
                    "required": ["from", "rel", "to", "src"],
                    "additionalProperties": False,
                }},
            },
            "required": ["actions", "links"],
            "additionalProperties": False,
        },
        "reply": {"type": "string"},
    },
    "required": ["reply", "analysis"],
    "additionalProperties": False,
}

INTENT_COVERAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate": {"type": "integer", "minimum": 1},
                    "id": {"type": "string", "enum": ACTION_IDS},
                    "act": {"type": "string", "enum": COMMUNICATION_ACTS},
                },
                "required": ["candidate", "id", "act"],
                "additionalProperties": False,
            },
        },
        # Allows the model to surface an action phrased outside the current
        # lexical cue inventory. These are still only candidates: downstream
        # validation requires an exact source excerpt and known action ID.
        "discoveries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "enum": [item for item in ACTION_IDS if item != "other"]},
                    "act": {"type": "string", "enum": COMMUNICATION_ACTS},
                    "src": {"type": "string"},
                    "target": {"type": ["string", "null"]},
                    "tag": {"type": ["string", "null"]},
                    "actors": {"type": "array", "items": {"type": "string"}},
                    "recipient": {"type": ["string", "null"]},
                    "duration": {"type": ["string", "null"]},
                    "if": {"type": "array", "items": {"type": "string"}},
                    "except": {"type": "array", "items": {"type": "string"}},
                    "unclear": {"type": "array", "items": {"type": "string"}},
                },
                # Keep semantic discovery lightweight: the exact source quote
                # and canonical action ID are mandatory. Role/term fields are
                # optional, then accepted only if independently source-grounded.
                "required": ["id", "src"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["actions", "discoveries"],
    "additionalProperties": False,
}

SEMANTIC_DISCOVERY_SCHEMA = {
    "type": "object",
    "properties": {
        "discoveries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "enum": [item for item in ACTION_IDS if item != "other"]},
                    "src": {"type": "string"},
                    "tag": {"type": ["string", "null"]},
                    "target": {"type": ["string", "null"]},
                    "actors": {"type": "array", "items": {"type": "string"}},
                    "recipient": {"type": ["string", "null"]},
                    "duration": {"type": ["string", "null"]},
                    "if": {"type": "array", "items": {"type": "string"}},
                    "except": {"type": "array", "items": {"type": "string"}},
                    "unclear": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["id", "src"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["discoveries"],
    "additionalProperties": False,
}

_TAG = re.compile(rf"(?<![A-Z0-9])({COUNTRY_TAG_PATTERN})(?![A-Z0-9])")
_ACTION_CUES = {
    "non_aggression_pact": re.compile(r"non.?aggression|ненапад|ненападени|не\s+напад|неагрес|pacte de non-agression", re.I),
    "guarantee": re.compile(r"guarantee|guarant(?:ee|y)|гаранти|гаранті|garant\w*", re.I),
    "embargo": re.compile(r"embargo|эмбарго|ембарго|sanction", re.I),
    "military_access": re.compile(
        r"military access|военн\w+ доступ|військов\w+ доступ|accès militaire|"
        r"passage\s+(?:militaire|(?:pour|de)\s+(?:(?:ses|les|nos|vos|des)\s+)?troupes)", re.I),
    "docking_rights": re.compile(r"docking rights|naval basing|права стоянки|port access", re.I),
    "air_base_access": re.compile(r"air.?base access|доступ к авиабаз|доступ до авіабаз", re.I),
    "faction_join": re.compile(r"join.{0,25}faction|вступить.{0,20}фракц|вступити.{0,20}фракц|admitting.{0,25}faction|принять.{0,25}фракц|admettre.{0,25}faction", re.I),
    "territory_partition": re.compile(r"territorial (?:agreement|proposal|question)|transfer\w*.{0,90}(?:state|city|region|area|territor)|\bcede\b|give.{0,60}(?:territor|surrounding|land|region)|территориальн\w* (?:соглашен|вопрос|предложен)|переда.{0,80}(?:штат|област|территор)|отда.{0,80}(?:област|территор)|\bcéder\b|розділ територ|раздел территор", re.I),
    "political_annexation": re.compile(
        r"peaceful.{0,25}(?:annexation|incorporation|union)|voluntary.{0,25}(?:annexation|incorporation|union)|"
        r"political.{0,25}(?:annexation|incorporation|union)|annexation|annex\w*|incorporat\w*|"
        r"peaceful.{0,25}unification|voluntary.{0,25}unification|"
        r"мирн\w*.{0,25}(?:аннекс|присоедин|объедин)|добровольн\w*.{0,25}(?:присоедин|объедин)|"
        r"политическ\w*.{0,25}(?:аннекс|присоедин|объедин)|аннекс\w*|присоедин\w*|"
        r"мирн\w*.{0,25}об[’']?єднан|добровільн\w*.{0,25}(?:приєднан|об[’']?єднан)|"
        r"політичн\w*.{0,25}(?:анекс|приєднан|об[’']?єднан)|анекс\w*|приєднан\w*|"
        r"annexion|annex\w*|rattachement|rattacher|unification pacifique|union volontaire",
        re.I,
    ),
    "white_peace": re.compile(r"white peace|белый мир|білий мир|paix blanche", re.I),
    "faction_leave": re.compile(r"removing.{0,25}faction|исключить.{0,25}фракц|exclure.{0,25}faction", re.I),
    "truce": re.compile(r"\btruce\b|armistice|перемир|перемир'я", re.I),
    **{action: re.compile(cue, re.I) for action,cue in PROPOSAL_CUES.items()},
}
_LIFT_VERB = re.compile(
    r"\b(?:lift|remove|cancel|end|terminate)\b|снять|сним\w*|отмен\w*|зняти|скасув\w*|"
    r"lever|annuler|supprimer", re.I,
)
_COUNTER_CUE = re.compile(
    r"\b(?:counter\w*(?:\s+(?:with|offer|proposal))?|instead,?\s+(?:propos\w*|offer\w*)|"
    r"rather propose|in return propose)\b|"
    r"\b(?:reject\w*|refus\w*)[^.!?]{0,120}\b(?:counter\w*|propos\w*|offer\w*)\b|"
    r"отклон\w*[^.!?]{0,120}предлага\w*|"
    r"не\s+(?:принима\w*|соглас\w*)[^.!?]{0,120}предлага\w*|"
    r"refus\w*[^.!?]{0,120}(?:propos\w*|offr\w*)|"
    r"rejett?\w*[^.!?]{0,120}(?:propos\w*|offr\w*)|"
    r"встречн\w*\s+предлож\w*|контрпредлож\w*|вместо этого предлож\w*|"
    r"вместо этого предлага\w*|взамен предлож\w*|взамен предлага\w*|"
    r"\b(?:proposer plutôt|propose plutôt|en revanche proposer|à la place proposer)\b", re.I,
)
_CURRENT_EFFECT_CUE = re.compile(
    r"\b(?:already\s+)?(?:in force|in effect|currently active|ongoing|has been applied|"
    r"was granted|is forbidden|are forbidden|under\s+(?:a\s+)?(?:joint\s+)?embargo)\b|"
    r"\b(?:is|are|was|were|has been|have been)\s+(?:already\s+)?"
    r"(?:granted|guaranteed|imposed|implemented|activated|signed|concluded)\b|"
    r"\b(?:granted|imposed|implemented|activated)\s+(?:the\s+)?(?:military access|embargo|guarantee)\b|"
    r"\b(?:en cours|(?:est|sont) (?:déjà )?en vigueur|déjà appliqu\w*|est interdit\w*|sont interdit\w*)\b|"
    r"\b(?:уже действует|вступил в силу|действует сейчас|запрещён\w*|запрещен\w*|не получил\w*)\b",
    re.I,
)
_ACTION_VERB_CUE = re.compile(
    r"\b(?:propos\w*|offer\w*|suggest\w*|counter\w*|grant\w*|provid\w*|giv\w*|"
    r"guarante\w*|impos\w*|introduc\w*|establish\w*|sign\w*|conclud\w*|"
    r"reject\w*|refus\w*|accept\w*|lift\w*|cancel\w*|not\s+to\s+attack)\b|"
    r"предлага\w*|ввод\w*|ввести|предостав\w*|гарантир\w*|заключ\w*|подпис\w*|"
    r"отклон\w*|отказыва\w*|принима\w*|сним\w*|отмен\w*|не\s+напада\w*|"
    r"аннекс\w*|присоедин\w*|объедин\w*|анекс\w*|приєднан\w*|об[’']?єднан\w*|"
    r"propos\w*|offr\w*|suggér\w*|accord\w*|fourn\w*|garant\w*|impos\w*|"
    r"instaur\w*|sign\w*|conclu\w*|refus\w*|rejett\w*|accept\w*|lev\w*|annul\w*|"
    r"annex\w*|rattach\w*|unifi\w*|incorpor\w*",
    re.I,
)


def validate_message_analysis(raw: Any, *, source_text: str, source_request_id: str | None,
                              allowed_country_tags: set[str] | None = None,
                              country_tag_aliases: dict[str, str] | None = None,
                              include_missing_action_placeholders: bool = True) -> dict | None:
    """Keep only bounded clauses grounded in the exact source message.

    Extracted positions remain advisory and explicitly unconfirmed. Country tags
    are retained only when written in the source or present in a trusted tag set.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("actions"), list):
        return None
    if not isinstance(raw.get("relationships", raw.get("links", [])), list):
        return None
    allowed = {tag for tag in (allowed_country_tags or set())
               if isinstance(tag, str) and COUNTRY_TAG_RE.fullmatch(tag)}
    # HOI4 tags are explicitly uppercase tokens. Uppercasing prose first would
    # incorrectly turn ordinary words such as "the" or "for" into tags.
    literal_tags = {tag for tag in _TAG.findall(source_text)
                    if tag in allowed or f"[{tag}]" in source_text}
    tag_allowlist = allowed | literal_tags
    actions = []
    clarification_targets = []
    for item in raw["actions"][:12]:
        if not isinstance(item, dict):
            continue
        action_id = item.get("id", item.get("action_id"))
        act = item.get("act", item.get("communication_act"))
        excerpt = item.get("src", item.get("source_excerpt"))
        if action_id not in ACTION_IDS or act not in COMMUNICATION_ACTS:
            continue
        if (not isinstance(excerpt, str) or not excerpt.strip() or len(excerpt) > 500
                or excerpt.casefold() not in source_text.casefold()):
            excerpt = _source_span_for_action(source_text, action_id)
            if not excerpt:
                continue
            excerpt_was_inferred = True
        else:
            excerpt_was_inferred = False
        target_reference = item.get("target", item.get("target_reference"))
        if target_reference is not None:
            if (not isinstance(target_reference, str) or len(target_reference) > 100
                    or target_reference.casefold() not in source_text.casefold()):
                target_reference = None
        target_tag = item.get("tag", item.get("target_country_tag"))
        if not isinstance(target_tag, str) or not COUNTRY_TAG_RE.fullmatch(target_tag):
            target_tag = None
        elif target_tag not in tag_allowlist:
            target_tag = None
        actors_raw = item.get("actors")
        if actors_raw is None:
            legacy_actor = item.get("actor_country_tag")
            actors_raw = [legacy_actor] if legacy_actor else []
        if not isinstance(actors_raw, list):
            actors_raw = []
        actors = []
        for actor_candidate in actors_raw[:6]:
            if not isinstance(actor_candidate, str) or not COUNTRY_TAG_RE.fullmatch(actor_candidate):
                continue
            alias = (country_tag_aliases or {}).get(actor_candidate)
            if actor_candidate in literal_tags or _country_alias_occurs(alias, source_text):
                actors.append(actor_candidate)
        actors = list(dict.fromkeys(actors))
        role_tags = {}
        recipient_candidate = item.get("recipient", item.get("recipient_country_tag"))
        for field, candidate in (("recipient_country_tag", recipient_candidate),):
            if not isinstance(candidate, str) or not COUNTRY_TAG_RE.fullmatch(candidate):
                role_tags[field] = None
                continue
            alias = (country_tag_aliases or {}).get(candidate)
            named_in_source = candidate in literal_tags or _country_alias_occurs(alias, source_text)
            role_tags[field] = candidate if named_in_source else None
        if action_id == "non_aggression_pact" and role_tags.get("recipient_country_tag"):
            actors = list(dict.fromkeys([*actors, role_tags["recipient_country_tag"]]))
        duration = item.get("duration")
        if duration is not None and (not isinstance(duration, str) or len(duration) > 100
                                     or duration.casefold() not in source_text.casefold()):
            duration = None
        conditions = _grounded_list(item.get("if", item.get("conditions")), source_text)
        exceptions = _grounded_list(item.get("except", item.get("exceptions")), source_text)
        unclear = item.get("unclear", [])
        if not isinstance(unclear, list):
            unclear = []
        unresolved_fields = [field for field in unclear
                             if field in {"action", "target", "actor", "recipient", "duration",
                                          "conditions", "exceptions", "relationship"}]
        if excerpt_was_inferred:
            unresolved_fields.append("action")
        if action_id in {"embargo", "lift_embargo", "guarantee"}:
            if not (target_tag or target_reference):
                unresolved_fields.append("target")
            elif not target_tag:
                unresolved_fields.append("target_country_tag")
        if action_id in {"non_aggression_pact", "guarantee", "embargo", "lift_embargo",
                         "military_access"} and not actors:
            unresolved_fields.append("actor")
        if action_id == "non_aggression_pact" and len(actors) < 2:
            unresolved_fields.append("parties")
        if action_id == "military_access" and not role_tags.get("recipient_country_tag"):
            unresolved_fields.append("recipient")
        if re.search(r"joint|mutual|shared|совмест|взаимн|commun|conjoint", excerpt, re.I) and len(actors) < 2:
            unresolved_fields.append("actor")
        unresolved_fields = list(dict.fromkeys(unresolved_fields))
        normalized_action = {
            "action_id": action_id,
            "communication_act": act,
            "source_excerpt": excerpt.strip(),
            "source_request_id": source_request_id,
            "target_reference": target_reference,
            "target_country_tag": target_tag,
            "actor_country_tags": actors,
            "actor_country_tag": actors[0] if actors else None,
            "recipient_country_tag": role_tags.get("recipient_country_tag"),
            "duration": duration,
            "conditions": conditions,
            "exceptions": exceptions,
            "consent_status": "unconfirmed",
            "parse_status": "needs_clarification" if unresolved_fields else "parsed",
            "unresolved_fields": unresolved_fields,
        }
        for evidence_field in ("role_basis", "recipient_basis", "target_basis"):
            evidence = item.get(evidence_field)
            if isinstance(evidence, str) and evidence and len(evidence) <= 100:
                normalized_action[evidence_field] = evidence
        actions.append(normalized_action)
        for field in unresolved_fields:
            clarification_targets.append({
                "action_id": action_id, "field": field,
                "source_excerpt": excerpt.strip(), "source_request_id": source_request_id,
            })

    # Retain model-understood items when another explicitly named item was
    # omitted. A narrow lexical inventory only creates an unresolved placeholder;
    # it can never establish direction, terms, consent, or executability.
    expected_actions = _expected_action_ids(source_text) if include_missing_action_placeholders else set()
    parsed_actions = {item["action_id"] for item in actions}
    for missing_action in sorted(expected_actions - parsed_actions):
        excerpt = _source_span_for_action(source_text, missing_action)
        if not excerpt:
            continue
        placeholder = {
            "action_id": missing_action,
            "communication_act": "proposal",
            "source_excerpt": excerpt,
            "source_request_id": source_request_id,
            "target_reference": None, "target_country_tag": None,
            "actor_country_tags": [], "actor_country_tag": None,
            "recipient_country_tag": None, "duration": None,
            "conditions": [], "exceptions": [],
            "consent_status": "unconfirmed", "parse_status": "needs_clarification",
            "unresolved_fields": ["action"],
        }
        actions.append(placeholder)
        clarification_targets.append({
            "action_id": missing_action, "field": "action",
            "source_excerpt": excerpt, "source_request_id": source_request_id,
        })

    relationships = []
    relationships_raw = raw.get("links", raw.get("relationships", []))
    for item in relationships_raw[:24]:
        if not isinstance(item, dict):
            continue
        left = item.get("from", item.get("action_index"))
        right = item.get("to", item.get("related_action_index"))
        relation = item.get("rel", item.get("relation"))
        excerpt = item.get("src", item.get("source_excerpt"))
        if (isinstance(left, bool) or not isinstance(left, int) or not 0 <= left < len(actions)
                or isinstance(right, bool) or not isinstance(right, int) or not 0 <= right < len(actions)
                or relation not in {"condition_for", "exception_to", "depends_on", "modifies"}
                or not isinstance(excerpt, str) or len(excerpt) > 500
                or excerpt.casefold() not in source_text.casefold()):
            continue
        relationships.append({
            "action_index": left,
            "relation": relation,
            "related_action_index": right,
            "source_excerpt": excerpt.strip(),
            "source_request_id": source_request_id,
        })
    # Preserve condition/exception fragments even when the model omitted the
    # edge. Link only if the fragment itself names exactly one extracted action;
    # otherwise retain it as a targeted clarification instead of guessing.
    linked_spans = {item["source_excerpt"].casefold() for item in relationships}
    unresolved_clauses = []
    for clause_kind, pattern in _RELATION_CUES.items():
        for clause in _find_relation_clauses(source_text, pattern):
            if any(clause.casefold() in span or span in clause.casefold()
                   for span in linked_spans):
                continue
            action_key = "exceptions" if clause_kind == "exception" else "conditions"
            mentioned = [index for index, action in enumerate(actions)
                         if any(clause.casefold() in value.casefold()
                                or value.casefold() in clause.casefold()
                                for value in action[action_key])]
            if not mentioned:
                mentioned = [index for index, action in enumerate(actions)
                             if _ACTION_CUES.get(action["action_id"])
                             and _ACTION_CUES[action["action_id"]].search(clause)]
            if not mentioned and len(actions) == 1:
                mentioned = [0]
            linked_index = mentioned[0] if len(mentioned) == 1 else None
            if linked_index is not None:
                relation = "exception_to" if clause_kind == "exception" else "condition_for"
                relationships.append({
                    "action_index": linked_index, "relation": relation,
                    "related_action_index": linked_index, "source_excerpt": clause,
                    "source_request_id": source_request_id,
                    "link_basis": "clause_explicitly_names_action",
                })
                key = "exceptions" if clause_kind == "exception" else "conditions"
                if not any(clause.casefold() in value.casefold()
                           or value.casefold() in clause.casefold()
                           for value in actions[linked_index][key]):
                    actions[linked_index][key].append(clause)
            else:
                unresolved_clauses.append({
                    "kind": clause_kind, "source_excerpt": clause,
                    "related_action_index": None, "status": "needs_clarification",
                    "source_request_id": source_request_id,
                })
                clarification_targets.append({
                    "action_id": None, "field": "relationship",
                    "source_excerpt": clause, "source_request_id": source_request_id,
                })
    complete = not clarification_targets and not unresolved_clauses
    return {
        "source_request_id": source_request_id,
        "source_text_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        "actions": actions,
        "relationships": relationships,
        "unresolved_clauses": unresolved_clauses,
        "clarification_targets": clarification_targets,
        "completeness": "complete" if complete else "partial",
        "authority": "advisory_only",
    }


_RELATION_CUES = {
    "condition": re.compile(
        r"(?:only if|only after|not until|until|unless|provided(?:\s+that)?|on condition that|subject to|"
        r"\bif\b|только если|только после|лишь после|до тех пор пока|пока не|при условии|если только не|"
        r"\bесли\b|лишь при условии|seulement si|uniquement après|qu['’]?après|"
        r"à condition que|\bsi\b|sauf si|à moins que)", re.I),
    "exception": re.compile(
        r"(?:except(?: that| for)?|excluding|excluded|excludes|with the exception of|not affected|not affect|"
        r"\bbut\s+not\b|\bwithout\b|"
        r"\b(?:do\s+not|don't|must\s+not|shall\s+not)\s+embargo\b|"
        r"не\s+(?:(?:ввод\w*|распространя\w*)\s+)?эмбарго\s+(?:на|в отношении)\s+"
        r"(?:лекарств\w*|медикамент\w*|продовольств\w*|поставк\w*|товар\w*|гуманитарн\w*)|"
        r"за исключением|исключ\w*|кроме(?!\s+того\b)|исключая|за винятком|не подпада\w*|"
        r"не.{0,20}затрагива\w*|"
        r"\bne\b.{0,35}\b(?:pas|plus|jamais)\b.{0,40}(?:affect\w*|touch\w*|concern\w*|inclu\w*|frapp\w*)|"
        r"окрім|sauf|"
        r"но\s+не|а\s+не|без|mais\s+pas|sans|"
        r"à l['’]exception de|exclu\w*|hormis)", re.I),
}

_ADVISORY_IF_NEEDED = re.compile(
    r"\b(?:if\s+(?:needed|necessary|appropriate|required|desired|applicable)|"
    r"as\s+needed|if\s+need\s+be|"
    r"si\s+(?:nécessaire|besoin)|au\s+besoin|"
    r"если\s+(?:нужно|необходимо|потребуется|понадобится)|при\s+необходимости|"
    r"якщо\s+(?:потрібно|необхідно|знадобиться)|за\s+потреби)\b",
    re.I,
)


def _source_span_for_action(source_text: str, action_id: str) -> str | None:
    spans = _source_spans_for_action(source_text, action_id)
    return spans[0] if spans else None


def is_guarantee_followup_reference(source_text: str, span: str) -> bool:
    """Only a bounded willingness question referring to the same named issuer.

    No additional terms, beneficiaries, refusals or conditions are discarded.
    """
    question = re.fullmatch(
        r"(?:готов[аы]?\s+ли\s+(?P<ru>\w+)\s+предоставить\s+такую\s+гарантию|"
        r"(?:would|will|can)\s+(?P<en>\w+)\s+(?:provide|give)\s+(?:such\s+a|that|this)\s+guarantee)[?\s]*",
        span.strip(), re.I)
    if not question:
        return False
    before = source_text[:source_text.casefold().find(span.casefold())]
    issuers = list(re.finditer(
        r"(?:предлага\w*\s+(?P<ru>\w+)\s+гарантировать|"
        r"requests?\s+(?P<en>\w+)\s+to\s+guarantee)", before, re.I))
    if len(issuers) != 1:
        return False
    issuer = issuers[0].group('ru') or issuers[0].group('en')
    referred = question.group('ru') or question.group('en')
    return (_alias_token_matches(issuer.casefold(), referred.casefold())
            or _alias_token_matches(referred.casefold(), issuer.casefold()))


def _source_spans_for_action(source_text: str, action_id: str) -> list[str]:
    if action_id == "lift_embargo":
        lift_pattern = re.compile(
            r"(?:\b(?:lift|remove|cancel|end|terminate)\b|снять|сним\w*|отмен\w*|"
            r"зняти|скасув\w*|lever|annuler|supprimer)[^.;!?\n]{0,80}?(?:embargo|эмбарго|ембарго)|"
            r"(?:embargo|эмбарго|ембарго)[^.;!?\n]{0,40}?(?:lifted|removed|cancelled|снят\w*|"
            r"отмен\w*|скасован\w*|levé)", re.I,
        )
        cue_spans = [match.span() for match in lift_pattern.finditer(source_text)]
    else:
        cue = _ACTION_CUES.get(action_id)
        if not cue:
            return []
        cue_spans = [match.span() for match in cue.finditer(source_text)]
    spans = []
    for cue_span in cue_spans:
        span = _source_span_for_cue(source_text, cue_span, action_id)
        reference_probe = span or ""
        if action_id in {"embargo", "lift_embargo"} and span:
            # A modifier can begin just after the source span when a later
            # action cue made _source_span_for_cue stop at the relation word.
            # Include only the remainder of this punctuation-bounded clause
            # when deciding whether this noun is an existing-action reference.
            tail_endings = [position for mark in ".;!?\n"
                            if (position := source_text.find(mark, cue_span[1])) >= 0]
            tail_end = min(tail_endings) if tail_endings else len(source_text)
            relation_tail = source_text[cue_span[1]:tail_end]
            if (_RELATION_CUES["condition"].search(relation_tail)
                    or _RELATION_CUES["exception"].search(relation_tail)):
                reference_probe = f"{span} {relation_tail}"
        if span and not (action_id in {"embargo", "lift_embargo"}
                         and _is_embargo_reference(reference_probe)):
            spans.append(span)
    if action_id == 'guarantee' and len(spans) > 1:
        spans = [span for span in spans if not is_guarantee_followup_reference(source_text, span)]
    return list(dict.fromkeys(spans))


def _append_counteroffer_clause(source_text: str, span: str) -> str:
    """Attach an explicit next-sentence counterterm to its referenced pact.

    This is limited to a clause beginning after sentence punctuation and
    containing a duration, so unrelated later actions stay separate.
    """
    if not span or _COUNTER_CUE.search(span):
        return span
    start = source_text.casefold().find(span.casefold())
    if start < 0:
        return span
    end = start + len(span)
    for match in _COUNTER_CUE.finditer(source_text, end):
        if not re.fullmatch(r"[\s.;!?]*", source_text[end:match.start()]):
            continue
        clause_endings = [position for mark in ".;!?\n"
                          if (position := source_text.find(mark, match.end())) >= 0]
        clause_end = min(clause_endings) if clause_endings else len(source_text)
        counter_clause = source_text[match.start():clause_end].strip()
        if _DURATION.search(counter_clause):
            return f"{span}; {counter_clause}"[:500]
    return span


def _is_embargo_reference(span: str) -> bool:
    explicit_action = re.search(
        r"\b(?:impose|introduce|establish|propos\w*|offer\w*|suggest\w*|lift|remove|cancel|end|terminate)\b|"
        r"\b(?:ввод\w*|ввести|предлага\w*|установ\w*|снять|отмен\w*)\b|"
        r"\b(?:impos\w*|propos\w*|offr\w*|instaur\w*|lever|annuler|supprimer|mettre en place)\b|"
        r"\b(?:may|might|will|shall|must|can|could|would)\s+(?:joint\s+)?(?:an?\s+)?embargo\b",
        span, re.I,
    )
    reference_cue = (
        _RELATION_CUES["exception"].search(span)
        or _RELATION_CUES["condition"].search(span)
        or re.search(r"(?:takes? effect|becomes? effective|enters? into force|"
                     r"вступ\w*\s+в\s+силу|начинает действовать|prend\w*\s+effet|entre en vigueur|"
                     r"\bapplies\b|применя\w*|s'applique)", span, re.I)
    )
    exception_subject = re.search(
        r"\b(?:food|medicine|medicines|medical supplies|goods|shipments|humanitarian aid)\b|"
        r"продовольств\w*|лекарств\w*|медикамент\w*|товар\w*|поставк\w*|гуманитарн\w*|"
        r"médicament\w*|médicaments|livraison\w*|marchandise\w*|aide humanitaire",
        span, re.I,
    )
    excluded_embargo_subject = bool(exception_subject and re.search(
        r"\b(?:do not|don't|must not|shall not)\s+embargo\b|"
        r"не\s+(?:ввод\w*|распространя\w*|затрагива\w*)|"
        r"\bne\b.{0,35}\b(?:pas|plus|jamais)\b.{0,40}(?:affect\w*|touch\w*|concern\w*|inclu\w*|frapp\w*)|"
        r"\b(?:ne\s+pas|n['’]\w+\s+pas)\b",
        span, re.I,
    ))
    return bool((reference_cue and not explicit_action)
                or excluded_embargo_subject)


def _source_span_for_cue(source_text: str, cue_span: tuple[int, int], action_id: str) -> str | None:
    start = max(0, source_text.rfind(".", 0, cue_span[0]) + 1,
                source_text.rfind(";", 0, cue_span[0]) + 1,
                source_text.rfind(",", 0, cue_span[0]) + 1,
                source_text.rfind("!", 0, cue_span[0]) + 1,
                source_text.rfind("?", 0, cue_span[0]) + 1)
    stops = [index for mark in ".,;!?\n" if (index := source_text.find(mark, cue_span[1])) >= 0]
    end = min(stops) if stops else len(source_text)
    # Keep a comma-separated counterterm attached to the rejected term so the
    # latter is not mistaken for a standalone refusal.
    if end < len(source_text) and source_text[end] == ",":
        hard_stops = [index for mark in ".;!?\n"
                      if (index := source_text.find(mark, end + 1)) >= 0]
        counter_end = min(hard_stops) if hard_stops else len(source_text)
        if _COUNTER_CUE.search(source_text[end + 1:counter_end]):
            end = counter_end
    other_matches = []
    for other_id, cue in _ACTION_CUES.items():
        other_matches.extend((match.start(), match.end(), other_id)
                             for match in cue.finditer(source_text)
                             if match.span() != cue_span)
    previous = [entry for entry in other_matches if entry[1] <= cue_span[0]]
    if previous:
        _, previous_end, _ = max(previous)
        bridge = source_text[previous_end:cue_span[0]]
        relation = next((match for pattern in _RELATION_CUES.values()
                         for match in pattern.finditer(bridge)), None)
        coordinator = re.search(r"(?:,\s*|\b(?:and|и|й|et|ni|nor|but|но|mais|however)\b\s+|"
                                r"\s*,\s*(?:and|и|et)\b\s*)", bridge, re.I)
        if relation:
            start = max(start, previous_end + relation.start())
        elif coordinator:
            # Preserve negative coordinators ("ni", "nor") because they
            # change the polarity of the following action.
            connector = coordinator.group(0).strip().casefold()
            keep_connector = connector in {"ni", "nor"}
            start = max(start, previous_end + (coordinator.start() if keep_connector
                                                 else coordinator.end()))
    following = [entry for entry in other_matches if entry[0] >= cue_span[1]]
    if following:
        next_start, _, _ = min(following)
        bridge = source_text[cue_span[1]:next_start]
        relation = next((match for pattern in _RELATION_CUES.values()
                         for match in pattern.finditer(bridge)), None)
        coordinator = re.search(r"(?:,\s*|\b(?:and|и|й|et|ni|nor|but|но|mais|however)\b\s+|"
                                r"\s*,\s*(?:and|и|et)\b\s*)", bridge, re.I)
        if relation and not _COUNTER_CUE.search(bridge):
            end = min(end, cue_span[1] + relation.start())
        elif coordinator and not _COUNTER_CUE.search(bridge):
            end = min(end, cue_span[1] + coordinator.start())
    return source_text[start:end].strip()[:500] or None


def _expected_action_ids(source_text: str) -> set[str]:
    expected = set()
    for action_id, cue in _ACTION_CUES.items():
        if cue.search(source_text):
            expected.add(action_id)
    for cancellation, original in (('cancel_nap','non_aggression_pact'),
                                   ('cancel_guarantee','guarantee'),
                                   ('cancel_military_access','military_access')):
        if cancellation in expected:
            expected.discard(original)
    embargo_matches = list(_ACTION_CUES["embargo"].finditer(source_text))
    if embargo_matches:
        has_lift = any(_LIFT_VERB.search(source_text[max(0, match.start() - 45):match.start()])
                       for match in embargo_matches)
        has_new_embargo = any(not _LIFT_VERB.search(
            source_text[max(0, match.start() - 45):match.start()]
        ) for match in embargo_matches)
        expected.discard("embargo")
        expected.discard("lift_embargo")
        if has_new_embargo:
            expected.add("embargo")
        if has_lift:
            expected.add("lift_embargo")
    return expected


def infer_context_action_references(source_text: str, recent_messages: Any, *,
                                   current_request_id: str | None,
                                   player_country_tag: str | None) -> dict[str, dict]:
    """Resolve only explicit elliptical references to a uniquely recent player offer.

    This never imports consent or state from country/AI replies. A reference is
    returned only when the preceding player message has a request_id and exactly
    one recent action of that type can anchor the current wording.
    """
    if not isinstance(recent_messages, list):
        return {}
    player_tag = str(player_country_tag or "").upper()
    current_id = str(current_request_id or "")
    referenced_types = set()
    has_nap_reference = (
        "non_aggression_pact" not in _expected_action_ids(source_text)
        and bool(re.search(r"\b(?:term|duration|years?|year)\b|срок\w*|длительност\w*|"
                           r"\b(?:durée|terme|ans?)\b", source_text, re.I))
        and (_COUNTER_CUE.search(source_text)
             or _source_polarity(source_text) in {"negated", "mixed"}
             or "?" in source_text)
    )
    has_embargo_reference = (
        "embargo" in _expected_action_ids(source_text)
        and not _source_spans_for_action(source_text, "embargo")
        and not _source_spans_for_action(source_text, "lift_embargo")
        and bool(_RELATION_CUES["condition"].search(source_text)
                 or _RELATION_CUES["exception"].search(source_text))
    )
    if has_nap_reference:
        referenced_types.add("non_aggression_pact")
    if has_embargo_reference:
        referenced_types.add("embargo")
    if (not _expected_action_ids(source_text) and re.search(
            r'\bstates?\s*(?:id\s*)?#?\s*\d+|област\w*\s*(?:id\s*)?#?\s*\d+', source_text, re.I)):
        latest = next((m for m in reversed(recent_messages[-8:])
                       if str(m.get('speaker') or m.get('sender') or '').upper()==player_tag
                       and str(m.get('request_id') or '')!=current_id
                       and _expected_action_ids(str(m.get('text') or ''))), None)
        if latest and _expected_action_ids(str(latest.get('text') or ''))=={'territory_partition'}:
            referenced_types.add('territory_partition')
    if not referenced_types:
        return {}

    found: dict[str, list[dict]] = {action_id: [] for action_id in referenced_types}
    for item in reversed(recent_messages[-8:]):
        if not isinstance(item, dict):
            continue
        speaker = str(item.get("speaker") or item.get("sender") or "").upper()
        request_id = item.get("request_id")
        text = item.get("text")
        if (speaker != player_tag or not isinstance(request_id, str) or not request_id.strip()
                or request_id == current_id or not isinstance(text, str) or not text.strip()):
            continue
        for action_id in referenced_types:
            if action_id=='territory_partition' and found[action_id]:
                continue
            spans = _source_spans_for_action(text, action_id)
            for span in spans:
                if _source_polarity(span) == "negated":
                    continue
                found[action_id].append({
                    "source_request_id": request_id,
                    "source_excerpt": span[:500],
                })
    # Do not guess between multiple potentially relevant prior offers.
    return {action_id: entries[0] for action_id, entries in found.items()
            if len(entries) == 1}


def _is_factuality_guard_instruction(clause: str) -> bool:
    """Recognize meta-instructions about reporting facts, not treaty terms."""
    fact_reference = re.search(
        r"\bas facts?\b|\bas established facts?\b|\bcomme (?:un fait|des faits|établi)\b|"
        r"\bкак факт\w*\b|\bкак подтвержд[её]нн\w+ факт\w*\b|"
        r"\bяк факт\w*\b|\bяк підтверджен\w+ факт\w*\b",
        clause, re.I,
    )
    reporting_prohibition = re.search(
        r"\b(?:do not|don't|don’t|must not|never)\s+(?:state|present|claim|report|treat|assume|describe)\b|"
        r"\bne(?:\s+\w+){0,2}\s+(?:présentez|déclarez|considérez|présumez)\w*"
        r"(?:\s+\w+){0,2}\s+pas\b|"
        r"\bне\s+(?:представляйте|утверждайте|выдавайте|считайте|подавайте)\w*\b|"
        r"\bне\s+(?:подавайте|представляйте|стверджуйте|вважайте)\w*\b",
        clause, re.I,
    )
    return bool(fact_reference and reporting_prohibition)


def _is_observation_freshness_caveat(clause: str) -> bool:
    """Recognize caveats about snapshot age, not treaty conditions/exceptions."""
    recorded_observation = re.search(
        r"\b(?:last|latest|most recent)\b[^.!?;]{0,60}\b(?:recorded|stored|logged|available)?\s*"
        r"(?:observation|snapshot|record)\b|"
        r"\b(?:derni[eè]re|plus r[eé]cente)\b[^.!?;]{0,60}\b(?:observation|capture|donn[eé]e)\b|"
        r"\b(?:последн\w*|недавн\w*)\b[^.!?;]{0,60}\b(?:записанн\w*\s+)?"
        r"(?:наблюдени\w*|снимк\w*|запис\w*)\b|"
        r"\b(?:останн\w*|найсвіжіш\w*)\b[^.!?;]{0,60}\b(?:записан\w*\s+)?"
        r"(?:спостереженн\w*|знімк\w*|запис\w*)\b",
        clause, re.I,
    )
    not_current_confirmation = re.search(
        r"\b(?:not|isn't|is not|doesn't|does not|cannot|can't)\b[^.!?;]{0,65}"
        r"\b(?:confirm\w*|evidence|proof)\b[^.!?;]{0,55}\b(?:later|current|present|updated|newer)\b|"
        r"\bne\b[^.!?;]{0,55}\b(?:confirme\w*|prouve\w*|preuve)\b[^.!?;]{0,55}"
        r"\b(?:ult[eé]rieur\w*|actuel\w*|pr[eé]sent\w*)\b|"
        r"\bне\b[^.!?;]{0,55}\b(?:подтвержд\w*|доказательств\w*|свидетельств\w*)\b"
        r"[^.!?;]{0,55}\b(?:более поздн\w*|текущ\w*|нынешн\w*|актуальн\w*)\b|"
        r"\bне\b[^.!?;]{0,55}\b(?:підтвердж\w*|доказ\w*|свідч\w*)\b"
        r"[^.!?;]{0,55}\b(?:пізніш\w*|поточн\w*|нинішн\w*|актуальн\w*)\b",
        clause, re.I,
    )
    return bool(recorded_observation and not_current_confirmation)


def is_negotiation_invitation(text: str) -> bool:
    return bool(re.fullmatch(
        r"\s*(?:если\s+[^.;!?]+?\s+не\s+устраива\w*\s+(?:срок|условия),?\s*"
        r"[^.;!?]+?\s+готов\w*\s+рассмотреть\s+встречное\s+предложение|"
        r"if\s+[^.;!?]+?\s+(?:duration|term)\s+is\s+unacceptable,?\s*"
        r"[^.;!?]+?\s+(?:is willing|are willing)\s+to\s+consider\s+a\s+counteroffer)\s*[.!]?\s*",
        str(text), re.I))


def is_nap_definition(text: str) -> bool:
    """Recognize only a restatement of reciprocal nonaggression, no extra effects."""
    return bool(re.fullmatch(
        r"\s*(?:обе\s+стороны\s+обязуются\s+воздерживаться\s+от\s+военных\s+действий\s+друг\s+против\s+друга|"
        r"договор\s+запрещает\s+нападение\s+одной\s+стороны\s+на\s+другую|"
        r"both\s+(?:parties|sides)\s+(?:agree\s+to|will)\s+refrain\s+from\s+"
        r"(?:military\s+action|attacking)\s+(?:against\s+)?(?:each\s+other|one\s+another))\s*[.!]?\s*",
        str(text), re.I))


def is_new_proposal_statement(text: str) -> bool:
    return bool(re.fullmatch(
        r"(?:это\s+новое\s+предложение,?\s+а\s+не\s+изменение\s+(?:предыдущего\s+)?договора|"
        r"this\s+is\s+a\s+new\s+proposal,?\s+not\s+an?\s+amendment\s+to\s+the\s+(?:previous\s+)?treaty)[.!\s]*",
        text.strip(), re.I))


def _find_relation_clauses(source_text: str, pattern: re.Pattern) -> list[str]:
    results = []
    relation_matches = list(pattern.finditer(source_text))
    other_pattern = (_RELATION_CUES["exception"]
                     if pattern is _RELATION_CUES["condition"]
                     else _RELATION_CUES["condition"]
                     if pattern is _RELATION_CUES["exception"] else None)
    other_matches = list(other_pattern.finditer(source_text)) if other_pattern else []
    is_exception = pattern is _RELATION_CUES["exception"]
    for match in relation_matches:
        start = max(0, source_text.rfind(".", 0, match.start()) + 1,
                    source_text.rfind(";", 0, match.start()) + 1,
                    source_text.rfind("!", 0, match.start()) + 1,
                    source_text.rfind("?", 0, match.start()) + 1,
                    source_text.rfind("\n", 0, match.start()) + 1)
        stop_positions = [position for mark in ".;!?\n" if (position := source_text.find(mark, match.start())) >= 0]
        end = min(stop_positions) if stop_positions else len(source_text)
        if is_new_proposal_statement(source_text[start:end]):
            continue
        if is_negotiation_invitation(source_text[start:end]):
            continue  # Willingness to hear a counteroffer is not a treaty condition.
        # Split a sentence that contains both a condition and a distinct
        # exception. Without this boundary, each relationship receives the
        # entire sentence and duplicates unrelated terms across both fields.
        if is_exception:
            prior_conditions = [item for item in other_matches if item.end() <= match.start()
                                and item.start() >= start]
            if prior_conditions:
                prior = max(prior_conditions, key=lambda item: item.end())
                separators = list(re.finditer(
                    r",\s*(?:(?:and|et|и)\b\s*)?|\b(?:and|et|и)\b\s+",
                    source_text[prior.end():match.start()], re.I,
                ))
                if separators:
                    start = prior.end() + separators[-1].end()
        else:
            next_exceptions = [item for item in other_matches if item.start() >= match.end()
                               and item.start() < end]
            if next_exceptions:
                following = min(next_exceptions, key=lambda item: item.start())
                separators = list(re.finditer(
                    r",\s*(?:(?:and|et|и)\b\s*)?|\b(?:and|et|и)\b\s+",
                    source_text[match.end():following.start()], re.I,
                ))
                if separators:
                    end = min(end, match.end() + separators[-1].start())
        clause = source_text[start:end].strip()
        if (is_exception and re.match(
                r'(?:without\s+(?:any\s+)?(?:further|additional|new)\s+(?:conditions?|changes?|amendments?)\b|'
                r'без\s+(?:новых|дополнительных)\s+(?:условий|изменений)\b|'
                r'sans\s+(?:nouvelles?\s+conditions?|modifications?\s+supplémentaires?)\b)(?=\s*(?:[.,;!?]|$))',
                source_text[match.start():], re.I)):
            # An explicit absence of new negotiating conditions is not a
            # substantive exception. Other matches (e.g. except medicine)
            # remain independent and must still be extracted.
            continue
        if (is_exception and _DIRECT_COUNTRY_STANCE_QUESTION.search(clause)):
            # “...accept this exact pact without further changes?” asks the
            # other signatory to state its position; the wording after
            # “without” is not an exception to the treaty itself.
            continue
        # “Suggest terms if needed” is a request to the negotiating partner,
        # not a condition on the proposed treaty.
        if pattern is _RELATION_CUES["condition"] and _ADVISORY_IF_NEEDED.search(clause):
            continue
        has_diplomatic_action = any(
            action_pattern.search(clause) for action_pattern in _ACTION_CUES.values()
        )
        if clause and not (
            (_is_factuality_guard_instruction(clause)
             or _is_observation_freshness_caveat(source_text))
            and not has_diplomatic_action
        ):
            results.append(clause[:500])
    return list(dict.fromkeys(results))


def _country_alias_occurs(alias: str, source_text: str) -> bool:
    """Match a localized country name including common inflected forms.

    This only validates an LLM-proposed tag against a known participant alias;
    it does not choose a tag or infer who performs an action.
    """
    if isinstance(alias, (list, tuple, set)):
        return any(_country_alias_occurs(str(item), source_text) for item in alias)
    if not isinstance(alias, str) or not alias.strip():
        return False
    alias_words = re.findall(r"[\wÀ-ž]+", alias.casefold(), flags=re.UNICODE)
    source_words = re.findall(r"[\wÀ-ž]+", source_text.casefold(), flags=re.UNICODE)
    if not alias_words:
        return False
    # Require the complete name, avoiding false tags such as AND from
    # "Andorra" matching the English conjunction "and". Only Cyrillic
    # country names receive conservative inflection matching.
    for start in range(len(source_words)):
        if start + len(alias_words) > len(source_words):
            continue
        matched = True
        for offset, word in enumerate(alias_words):
            candidate = source_words[start + offset]
            if _alias_token_matches(word, candidate):
                continue
            matched = False
            break
        if matched:
            return True
    return False


def _alias_token_matches(alias_word: str, candidate: str) -> bool:
    if candidate == alias_word or (alias_word.isascii() and candidate == alias_word + "s"):
        return True
    if not re.search(r"[А-Яа-яІіЇїЄєҐґ]", alias_word):
        return False
    # Country names commonly inflect in Russian/Ukrainian. Expand only known
    # noun endings; the former broad prefix rule made adjectives such as
    # "бельгийск-" falsely match the noun "Бельгии" and ambiguous tags.
    if alias_word.endswith(("ия", "ія")):
        stem = alias_word[:-1]
        return candidate in {stem + ending for ending in ("и", "ю", "ей", "ям", "ями", "ях")}
    if alias_word.endswith(("я", "а")):
        stem = alias_word[:-1]
        return candidate in {stem + ending for ending in ("и", "ы", "ю", "у", "е", "і", "єю", "ой", "ою")}
    if alias_word.endswith(("ск", "цьк", "ський", "ская", "ская")):
        return candidate.startswith(alias_word)
    return False


_CLEAR_BILATERAL = (
    ("non_aggression_pact", r"(.+?) предлагает (.+?) заключить (?:взаимный )?пакт о ненападении"),
    ("non_aggression_pact", r"(.+?) и (.+?) заключают (?:взаимный )?пакт о ненападении"),
    ("guarantee", r"(.+?) гарантирует независимость (.+)"),
    ("military_access", r"(.+?) предоставляет (.+?) военный доступ(?: на территорию (.+))?"),
    ("non_aggression_pact", r"(.+?) proposes (?:a mutual|a) non-aggression pact with (.+)"),
    ("non_aggression_pact", r"(.+?) and (.+?) agree to (?:a mutual|a) non-aggression pact"),
    ("guarantee", r"(.+?) guarantees the independence of (.+)"),
    ("military_access", r"(.+?) grants military access to (.+)"),
    ("non_aggression_pact", r"(.+?) propose un pacte de non-agression (?:à|au|aux) (.+)"),
    ("non_aggression_pact", r"(.+?) et (.+?) acceptent un pacte de non-agression"),
    ("guarantee", r"(.+?) garantit l['’]indépendance (?:de|du|des) (.+)"),
    ("military_access", r"(.+?) accorde (?:à|au|aux) (.+?) un accès militaire"),
)
_NO_RECIPROCITY_RU = (
    ("guarantee", r"(.+?) не обязан[ао]? гарантировать независимость (.+)"),
    ("military_access", r"(.+?) не обязан[ао]? предоставлять (.+?) военный доступ"),
)


def explicit_bilateral_offer(source, request_id, player, counterpart, aliases):
    """Resolve a fully stated bilateral pact/guarantee/access offer from its clauses."""
    parties = {player, counterpart}
    if len(parties) != 2 or not all(_valid_tag(tag) for tag in parties):
        return None

    def party(name):
        # Compare complete operands, never a substring of a country name.
        names = re.findall(r"\w+", re.sub(r"^(?:l['’]|la |le |les )", "", name.casefold()))
        matches = []
        for tag in parties:
            labels = aliases.get(tag, ())
            labels = (labels,) if isinstance(labels, str) else labels
            for label in (*labels, tag):
                tokens = re.findall(r"\w+", label.casefold())
                if names and len(names) == len(tokens) and all(
                    _alias_token_matches(token, word) for token, word in zip(tokens, names)
                ):
                    matches.append(tag)
                    break
        return matches[0] if len(matches) == 1 else None

    actions = {}
    disclaimers = []
    accepts_revision = False
    for chunk in re.split(r"[.\n]+", source):
        clause = chunk.strip()
        if (not clause or re.fullmatch(r"[1-3]", clause) or
                re.fullmatch(r".+? уточняет три условия договора:|"
                             r"Все три условия являются частью одного (?:договора|соглашения)", clause, re.I)):
            continue
        introduction = re.fullmatch(r"(.+?) предлагает (.+?) заключить единый договор из тр[её]х условий:", clause, re.I)
        if introduction and party(introduction[1]) == player and party(introduction[2]) == counterpart:
            continue
        question = re.fullmatch(r"Просим (.+?) подтвердить согласие с каждым условием или предложить конкретные изменения", clause, re.I)
        if question and party(question[1]) == counterpart:
            continue
        assent = re.fullmatch(r"(.+?) согласн[ао]? с этой редакцией|"
                              r"(.+?) принимает эту редакцию договора и готов[ао]? подписать его, "
                              r"если (.+?) согласится со всеми тремя пунктами", clause, re.I)
        direct_assent = re.fullmatch(r"(.+?) accepts this version|(.+?) accepte cette version", clause, re.I)
        if ((assent and (party(assent[1]) == player if assent[1] else
                         party(assent[2]) == player and party(assent[3]) == counterpart))
                or (direct_assent and party(direct_assent[1] or direct_assent[2]) == player)):
            accepts_revision = True
            continue
        parts = re.split(r"\s+и\s+(?=предоставляет\s)", clause, maxsplit=1, flags=re.I)
        previous_actor = None
        for excerpt in parts:
            clean = re.sub(r"^также\s+", "", excerpt, flags=re.I)
            for action_id, pattern in (*_CLEAR_BILATERAL, *_NO_RECIPROCITY_RU):
                shared = previous_actor and re.match(r"^предоставляет\s", clean, re.I)
                match = re.fullmatch(pattern, f"{previous_actor} {clean}" if shared else clean, re.I)
                if not match:
                    continue
                actor, recipient = party(match[1]), party(match[2])
                if {actor, recipient} != parties or (match.lastindex == 3 and party(match[3]) != actor):
                    return None
                if "не обязан" in match[0].casefold():
                    disclaimers.append((action_id, actor, recipient))
                    break
                prior = actions.get(action_id)
                if prior and (prior["actor_country_tag"], prior["recipient_country_tag"]) != (actor, recipient):
                    return None
                previous_actor = actor
                actions.setdefault(action_id, {
                    "action_id": action_id, "communication_act": "proposal", "polarity": "affirmed",
                    "source_excerpt": excerpt, "source_request_id": request_id, "parse_status": "parsed",
                    "actor_country_tag": actor, "actor_country_tags": [actor, recipient]
                    if action_id == "non_aggression_pact" else [actor],
                    "recipient_country_tag": recipient, "target_country_tag": recipient
                    if action_id == "guarantee" else None,
                    "negotiation_party_country_tags": sorted(parties), "role_basis": "explicit_bilateral_clauses",
                    "duration": None, "conditions": [], "exceptions": [],
                    "unresolved_fields": [], "consent_status": "unconfirmed",
                })
                break
            else:
                return None
    if set(actions) != {"non_aggression_pact", "guarantee", "military_access"}:
        return None
    if any(actions[k]["actor_country_tag"] != recipient or actions[k]["recipient_country_tag"] != actor
           for k, actor, recipient in disclaimers):
        return None
    return {"source_request_id": request_id, "source_text_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "actions": list(actions.values()), "relationships": [], "clarification_targets": [],
            "unresolved_clauses": [], "completeness": "complete", "authority": "advisory_only",
            "interpretation_mode": "explicit_bilateral_clauses", "explicit_revision_acceptance": accepts_revision}


def build_analysis_from_summary(source_text: str, semantic_summary: str, *,
                                source_request_id: str | None,
                                proposer_country_tag: str | None,
                                addressed_country_tag: str | list[str] | tuple[str, ...] | None,
                                participant_country_tags: list[str] | tuple[str, ...] | set[str] | None = None,
                                country_tag_aliases: dict[str, Any] | None = None,
                                semantic_action_counts: dict[str, int] | None = None,
                                contextual_action_references: dict[str, dict] | None = None) -> dict:
    """Ground an LLM's natural-language analysis against the original message.

    The local model supplies semantic coverage; source spans, participant tags,
    and action fields are retained only when supported by the message/context.
    Ambiguity is represented per item, never promoted to consent or execution.
    """
    summary = str(semantic_summary or "")[:6000]
    aliases = country_tag_aliases or {}
    proposer = _valid_tag(proposer_country_tag)
    if isinstance(addressed_country_tag, (list, tuple, set)):
        addressed_tags = list(dict.fromkeys(
            tag for tag in (_valid_tag(value) for value in addressed_country_tag) if tag
        ))
    else:
        addressed_tag = _valid_tag(addressed_country_tag)
        addressed_tags = [addressed_tag] if addressed_tag else []
    addressed = addressed_tags[0] if len(addressed_tags) == 1 else None
    context_references = contextual_action_references or {}
    expected = _expected_action_ids(source_text) | {
        action_id for action_id in context_references if action_id in ACTION_IDS
    }
    summary_counts = semantic_action_counts if semantic_action_counts is not None \
        else _semantic_action_counts(summary)
    participants = [tag for tag in (proposer, *addressed_tags) if tag]
    participants = list(dict.fromkeys(participants))
    source_participant_tags = list(dict.fromkeys(
        tag for tag in [proposer, *addressed_tags, *(
            _valid_tag(value) for value in (participant_country_tags or [])
        )] if tag
    ))
    actions = []
    action_source_positions: list[int] = []
    clarification_targets = []
    occurrences = []
    from guarded_treaty_effects import EFFECTS, proposal_tags
    for action_id in expected:
        spans = ([source_text.strip()] if action_id in EFFECTS and proposal_tags(source_text, action_id)
                 else _source_spans_for_action(source_text, action_id))
        if not spans and action_id in context_references:
            reference_span = source_text.strip()[:500]
            spans = [reference_span] if reference_span else []
        for span in spans:
            occurrences.append((source_text.casefold().find(span.casefold()), action_id, span))
    for source_position, action_id, span in sorted(occurrences):
        if action_id in {"non_aggression_pact", "truce"}:
            # The lexical action span can end after the first country name
            # (for example, "pact between Germany"). Preserve an explicitly
            # stated bilateral party clause so source grounding can resolve
            # all named parties without substituting the current chat speaker.
            relation_patterns = (
                r"\bbetween\s+([^,;.!?\n]+?)\s+and\s+([^,;.!?\n]+)",
                r"\bentre\s+([^,;.!?\n]+?)\s+et\s+([^,;.!?\n]+)",
                r"\bzwischen\s+([^,;.!?\n]+?)\s+und\s+([^,;.!?\n]+)",
                r"между\s+([^,;.!?\n]+?)\s+и\s+([^,;.!?\n]+)",
                r"між\s+([^,;.!?\n]+?)\s+(?:і|та)\s+([^,;.!?\n]+)",
            )
            action_start = max(0, source_position)
            for relation_pattern in relation_patterns:
                relation = next((match for match in re.finditer(
                    relation_pattern, source_text, re.IGNORECASE,
                ) if match.start() >= action_start
                    and not re.search(r"[.!?;\n]", source_text[action_start:match.start()])), None)
                if relation:
                    span = source_text[action_start:relation.end()].strip()
                    source_position = action_start
                    break
            span = _append_counteroffer_clause(source_text, span)
            source_position = source_text.casefold().find(span.casefold())
        action_source_positions.append(source_position)
        action_ordinal = sum(1 for item in actions if item["action_id"] == action_id)
        unresolved = []
        if (int(summary_counts.get(action_id, 0) or 0) <= action_ordinal
                and not (action_id in EFFECTS and proposal_tags(source_text, action_id) == (proposer, addressed))):
            unresolved.append("summary_confirmation")
        context_reference = context_references.get(action_id)
        grounding_text = " ".join((span, str(context_reference.get("source_excerpt") or ""))) \
            if isinstance(context_reference, dict) else span
        mentioned_tags = _tags_in_text(grounding_text, aliases)
        mentioned_participant_tags = _tags_in_text(
            grounding_text, aliases, allowed_tags=source_participant_tags,
        )
        source_action_participants = list(dict.fromkeys(
            [*participants, *mentioned_participant_tags]
        ))
        third_party_tags = [tag for tag in mentioned_tags if tag not in participants]
        target = None
        recipient = None
        actor_tags: list[str] = []
        role_basis = ""
        if action_id == "non_aggression_pact":
            # The source span itself names the pact parties. Requiring those
            # names to also appear in the opening salutation wrongly dropped
            # FRA from a sentence such as "Germany proposes a pact between
            # Germany and France" when the message did not begin "France, ...".
            explicit_parties = list(dict.fromkeys(mentioned_participant_tags))
            actor_tags = (explicit_parties if len(explicit_parties) >= 2 else
                          list(dict.fromkeys([*explicit_parties, *participants])))
            role_basis = "explicit_bilateral_parties_in_source" if len(explicit_parties) >= 2 \
                else "bilateral_participants_from_chat"
            if len(actor_tags) < 2:
                unresolved.append("parties")
        elif action_id in {"embargo", "lift_embargo"}:
            target = third_party_tags[0] if len(third_party_tags) == 1 else None
            joint = bool(re.search(r"joint|mutual|shared|совмест\w*|взаимн\w*|conjoint\w*|commun\w*",
                                   grounding_text + " " + summary, re.I))
            actor_tags = participants if joint else ([addressed] if addressed else [])
            role_basis = "explicit_joint_action" if joint else "proposal_addressed_country"
            if target is None:
                unresolved.append("target_country")
            if not actor_tags:
                unresolved.append("actor")
        elif action_id == "military_access":
            explicit_actor = _explicit_action_actor(span, action_id, source_action_participants, aliases)
            actor_tags = [explicit_actor or addressed] if (explicit_actor or addressed) else []
            role_basis = "explicit_subject_before_grant_verb" if explicit_actor \
                else "proposal_addressed_country_grants_access"
            recipient_candidates = [tag for tag in mentioned_tags if tag not in actor_tags]
            participant_recipients = [tag for tag in recipient_candidates if tag in participants]
            if participant_recipients:
                recipient_candidates = participant_recipients
            recipient = recipient_candidates[0] if len(recipient_candidates) == 1 else None
            if not actor_tags:
                unresolved.append("actor")
            if recipient is None:
                unresolved.append("recipient")
        elif action_id == "docking_rights":
            explicit_actor = _explicit_action_actor(span, action_id, source_action_participants, aliases)
            actor_tags = [explicit_actor or addressed] if (explicit_actor or addressed) else []
            role_basis = "explicit_subject_before_rights_clause" if explicit_actor \
                else "proposal_addressed_country_rights_clause"
            recipient_candidates = [tag for tag in mentioned_tags if tag not in actor_tags]
            participant_recipients = [tag for tag in recipient_candidates if tag in participants]
            if participant_recipients:
                recipient_candidates = participant_recipients
            recipient = recipient_candidates[0] if len(recipient_candidates) == 1 else None
            if not actor_tags:
                unresolved.append("actor")
            if recipient is None:
                unresolved.append("recipient")
        elif action_id == "guarantee":
            explicit_actor = _explicit_action_actor(span, action_id, source_action_participants, aliases)
            actor_tags = [explicit_actor or addressed] if (explicit_actor or addressed) else []
            role_basis = "explicit_subject_before_guarantee_verb" if explicit_actor \
                else "proposal_addressed_country_guarantees"
            target_candidates = [tag for tag in mentioned_tags if tag not in actor_tags]
            # Several tags may share a short name (e.g. alternative regimes).
            # Resolve only aliases wholly shared with a known chat participant;
            # an explicitly distinct third-country name must remain ambiguous.
            known_targets = [tag for tag in target_candidates if tag in source_action_participants]
            if len(known_targets) == 1:
                def matching_names(tag):
                    names = aliases.get(tag, ())
                    names = (names,) if isinstance(names, str) else names
                    return {str(n).casefold() for n in names if _country_alias_occurs((n,), grounding_text)}
                known_names = matching_names(known_targets[0])
                target_candidates = [tag for tag in target_candidates if tag in known_targets
                    or not matching_names(tag) or not matching_names(tag).issubset(known_names)]
            target = target_candidates[0] if len(target_candidates) == 1 else None
            if not actor_tags:
                unresolved.append("actor")
            if target is None:
                unresolved.append("protected_country")
        elif action_id == "political_annexation":
            target_candidates = list(dict.fromkeys(
                tag for tag in mentioned_participant_tags if tag != proposer
            ))
            if len(target_candidates) == 1:
                target = target_candidates[0]
            elif not target_candidates and addressed and addressed != proposer:
                target = addressed
            actor_tags = [proposer] if proposer else []
            recipient = target
            role_basis = "proposal_author_and_explicit_target_country"
            if not actor_tags:
                unresolved.append("initiator")
            if target is None:
                unresolved.append("target_country")
        elif action_id in EFFECTS:
            tags = proposal_tags(source_text, action_id)
            if tags and tags[0] == proposer and tags[1] in addressed_tags:
                actor_tags, target = [tags[0]], tags[1]
                recipient = target
                role_basis = "explicit_bilateral_effect"
            else:
                actor_tags = []
                unresolved.append("exact_bilateral_proposal")
        elif action_id == 'territory_partition':
            # Tags identify discussion participants, never owners/allocations.
            actor_tags = participants[:]
            target = None
            recipient = None
            role_basis = 'discussion_participants_allocation_unverified'
            unresolved.append('verified_state_allocation')
        else:
            actor_tags = []
            unresolved.append("unsupported_structured_action")

        duration = extract_duration(span, action_id=action_id)
        if action_id in {"non_aggression_pact", "truce"} and _has_duration_cue(span) and not duration:
            unresolved.append("duration")
        contextual_only = bool(context_reference and not _ACTION_VERB_CUE.search(span))
        if contextual_only and _RELATION_CUES["exception"].search(span):
            communication_act = "exception"
            action_polarity = "conditional"
        elif contextual_only and _RELATION_CUES["condition"].search(span):
            communication_act = "condition"
            action_polarity = "conditional"
        else:
            communication_act = _source_communication_act(span)
            action_polarity = _source_polarity(span)
        action = {
            "action_id": action_id,
            "communication_act": communication_act,
            "source_excerpt": span,
            "source_request_id": source_request_id,
            "target_reference": _country_name_for_tag(target, aliases) if target else None,
            "target_country_tag": target,
            "actor_country_tags": actor_tags,
            # The party set of a bilateral pact is unordered, while its
            # primary actor is the author of the source message. `_tags_in_text`
            # sorts tags alphabetically, which could otherwise mislabel FRA as
            # proposer when GER authored the offer.
            "actor_country_tag": (
                proposer if action_id in {"non_aggression_pact", "truce"}
                and proposer in actor_tags else actor_tags[0] if actor_tags else None
            ),
            "recipient_country_tag": recipient,
            "duration": duration,
            "conditions": [],
            "exceptions": [],
            "consent_status": "unconfirmed",
            "polarity": action_polarity,
            "parse_status": "needs_clarification" if unresolved else "parsed",
            "unresolved_fields": list(dict.fromkeys(unresolved)),
            "role_basis": role_basis,
        }
        if role_basis == "explicit_bilateral_effect":
            action["negotiation_party_country_tags"] = list(tags)
        if isinstance(context_reference, dict):
            action["context_reference"] = {
                "source_request_id": context_reference.get("source_request_id"),
                "source_excerpt": context_reference.get("source_excerpt"),
            }
        actions.append(action)
        for field in action["unresolved_fields"]:
            clarification_targets.append({
                "action_id": action_id, "field": field,
                "source_excerpt": span, "source_request_id": source_request_id,
            })

    relationships = []
    unresolved_clauses = []
    extracted_action_ids = {action_id for _, action_id, _ in occurrences}
    for action_id in sorted(expected - extracted_action_ids):
        unresolved_clauses.append({
            "kind": "action_reference", "source_excerpt": source_text[:500],
            "related_action_index": None, "status": "needs_clarification",
            "source_request_id": source_request_id,
        })
        clarification_targets.append({
            "action_id": action_id, "field": "referenced_action_context",
            "source_excerpt": source_text[:500], "source_request_id": source_request_id,
        })
    for clause_kind, pattern in _RELATION_CUES.items():
        for clause in _find_relation_clauses(source_text, pattern):
            matched_indices = [index for index, action in enumerate(actions)
                              if _ACTION_CUES.get(action["action_id"])
                              and _ACTION_CUES[action["action_id"]].search(clause)]
            if clause_kind == "condition":
                clause_offset = source_text.casefold().find(clause.casefold())
                relation_match = pattern.search(clause)
                relation_offset = (clause_offset + relation_match.start()
                                   if clause_offset >= 0 and relation_match else -1)
                contextual_before = [index for index, action in enumerate(actions)
                                     if action.get("context_reference")
                                     and action_source_positions[index] < relation_offset]
                matched_indices = list(dict.fromkeys(matched_indices + contextual_before))
                # "If France cedes territory, Germany will accept the treaty"
                # constrains the preceding offer as well as naming a new action.
                # Recognising the territorial condition must not detach it from
                # the pact and make that pact appear unconditional.
                if re.search(r'прим\w*\s+договор|accept\w*.{0,20}(?:treaty|pact)|accepter\w*.{0,20}(?:traité|pacte)',clause,re.I):
                    prior=[i for i in range(len(actions)) if action_source_positions[i]<relation_offset]
                    if prior:
                        matched_indices=list(dict.fromkeys(matched_indices+[max(prior,key=lambda i:action_source_positions[i])]))
            if not matched_indices and clause_kind == "exception" and re.search(
                    r"shipment|food|medicine|medical supplies|goods|trade|livraison|marchandise|aliment|"
                    r"médicament\w*|aide humanitaire|поставк\w*|лекарств\w*|медикамент\w*|"
                    r"продовольств\w*|товар\w*|гуманитар", clause, re.I):
                embargo_indices = [index for index, action in enumerate(actions)
                                   if action["action_id"] in {"embargo", "lift_embargo"}]
                if len(embargo_indices) == 1:
                    matched_indices = embargo_indices
            if not matched_indices and len(actions) == 1:
                matched_indices = [0]
            # In a clause such as "France guarantees Belgium only if Germany
            # guarantees France", the condition belongs to the preceding
            # proposal. The conditional promise remains a separate action.
            if len(matched_indices) > 1 and clause_kind in {"condition", "exception"}:
                relation_match = pattern.search(clause)
                clause_offset = source_text.casefold().find(clause.casefold())
                relation_offset = (clause_offset + relation_match.start()
                                   if clause_offset >= 0 and relation_match else -1)
                preceding = [index for index in matched_indices
                             if action_source_positions[index] < relation_offset]
                if preceding:
                    matched_indices = [max(preceding,
                                          key=lambda index: action_source_positions[index])]
            if len(matched_indices) == 1:
                index = matched_indices[0]
                relation = "exception_to" if clause_kind == "exception" else "condition_for"
                relationships.append({
                    "action_index": index, "relation": relation,
                    "related_action_index": index, "source_excerpt": clause,
                    "source_request_id": source_request_id,
                    "link_basis": (
                        "contextual_player_offer_request"
                        if actions[index].get("context_reference")
                        else ("source_names_unique_action"
                              if _ACTION_CUES[actions[index]["action_id"]].search(clause)
                              else ("only_compatible_embargo_action" if clause_kind == "exception"
                                    and actions[index]["action_id"] in {"embargo", "lift_embargo"}
                                    else "only_action_in_message"))
                    ),
                    **({"related_source_request_id": actions[index]["context_reference"].get("source_request_id")}
                       if actions[index].get("context_reference") else {}),
                })
                key = "exceptions" if clause_kind == "exception" else "conditions"
                if not any(clause.casefold() in value.casefold()
                           or value.casefold() in clause.casefold()
                           for value in actions[index][key]):
                    actions[index][key].append(clause)
                if clause_kind == "exception":
                    # A negated sub-action in an exception ("access but not
                    # naval basing") inherits the parties from the primary
                    # action it qualifies; do not invent a separate actor.
                    for excluded in actions:
                        if (excluded is not actions[index]
                                and excluded["polarity"] == "negated"
                                and excluded["source_excerpt"].casefold() in clause.casefold()
                                and excluded["action_id"] == "docking_rights"):
                            excluded["actor_country_tags"] = list(actions[index]["actor_country_tags"])
                            excluded["actor_country_tag"] = actions[index]["actor_country_tag"]
                            excluded["recipient_country_tag"] = actions[index]["recipient_country_tag"]
                            excluded["role_basis"] = "inherited_from_exception_parent"
            else:
                unresolved_clauses.append({
                    "kind": clause_kind, "source_excerpt": clause,
                    "related_action_index": None, "status": "needs_clarification",
                    "source_request_id": source_request_id,
                })
                clarification_targets.append({
                    "action_id": None, "field": "relationship", "source_excerpt": clause,
                    "source_request_id": source_request_id,
                })
    # The model's summary is a coverage signal, never a source of executable
    # fields. A summary mentioning an unsupported action adds no action here.
    complete = not clarification_targets and not unresolved_clauses
    return {
        "source_request_id": source_request_id,
        "source_text_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        "actions": actions,
        "relationships": relationships,
        "unresolved_clauses": unresolved_clauses,
        "clarification_targets": clarification_targets,
        "completeness": "complete" if complete else "partial",
        "authority": "advisory_only",
    }


def _semantic_candidates(source_text: str, contextual_action_references: dict[str, dict] | None = None):
    references = contextual_action_references or {}
    candidates = []
    action_ids = _expected_action_ids(source_text) | set(references)
    for action_id in action_ids:
        spans = _source_spans_for_action(source_text, action_id)
        if not spans and action_id in references:
            spans = [source_text.strip()[:500]] if source_text.strip() else []
        for span in spans:
            candidates.append((source_text.casefold().find(span.casefold()), action_id, span))
    candidates.sort()
    return candidates


def _semantic_conflict_action_ids(source_text: str, semantic_summary: str, *,
                                  contextual_action_references: dict[str, dict] | None = None) -> set[str]:
    """Flag model lines that contradict source polarity or invent an applied effect."""
    decoded_objects = _decode_json_objects(semantic_summary)
    structured = [obj for obj in decoded_objects if isinstance(obj.get("actions"), list)]
    if structured:
        candidates = _semantic_candidates(source_text, contextual_action_references)
        conflicts = set()
        for decoded in structured:
            for row in decoded["actions"]:
                if not isinstance(row, dict):
                    continue
                candidate_number = row.get("candidate")
                if len(candidates) == 1 and len(decoded["actions"]) == 1:
                    candidate_number = 1
                if not isinstance(candidate_number, int) or not 1 <= candidate_number <= len(candidates):
                    continue
                _, expected_id, span = candidates[candidate_number - 1]
                model_id = row.get("id")
                model_act = row.get("act")
                if model_id != expected_id and model_id in ACTION_IDS:
                    conflicts.update({expected_id, model_id})
        if conflicts:
            return conflicts
        # Compact JSON contains labels only, not free-form factual claims.
        # Speech-act disagreements are reported separately and source cues win.
        return set()
    lines = [line.strip(" |\t") for line in str(semantic_summary or "").splitlines()
             if line.strip(" |\t-")]
    conflicts = set()
    for action_id in _expected_action_ids(source_text):
        cue = _ACTION_CUES.get(action_id)
        if not cue:
            continue
        source_spans = _source_spans_for_action(source_text, action_id)
        recovery_lines = [line for line in lines
                          if line.casefold().startswith(f"recovery action_id={action_id}:")]
        model_lines = recovery_lines[-1:] if recovery_lines else [line for line in lines if cue.search(line)]
        if not source_spans or not model_lines:
            continue
        for line in model_lines:
            model_polarity = _source_polarity(line)
            for span in source_spans:
                source_polarity = _source_polarity(span)
                if ((source_polarity == "affirmed" and model_polarity == "negated")
                        or (source_polarity == "negated" and model_polarity == "affirmed")):
                    conflicts.add(action_id)
                    break
                if _CURRENT_EFFECT_CUE.search(line) and not _CURRENT_EFFECT_CUE.search(span):
                    conflicts.add(action_id)
                    break
            if action_id in conflicts:
                break
    return conflicts


def _semantic_act_disagreement_ids(source_text: str, semantic_summary: str, *,
                                   contextual_action_references: dict[str, dict] | None = None) -> set[str]:
    """Identify compact-model speech-act errors overridden by explicit source cues."""
    structured = [obj for obj in _decode_json_objects(semantic_summary)
                  if isinstance(obj.get("actions"), list)]
    if not structured:
        return set()
    candidates = _semantic_candidates(source_text, contextual_action_references)
    disagreements = set()
    explicit_act_cue = re.compile(
        r"\b(?:reject\w*|refus\w*|do not|don't|accept\w*|agree\w*|counter\w*|"
        r"propos\w*|offer\w*|question|whether|only if|only after|provided(?:\s+that)?)\b|"
        r"отклон\w*|не\s+(?:соглас\w*|предостав\w*|гарантир\w*|заключ\w*)|"
        r"принима\w*|соглас\w*|встречн\w*|контрпредлож\w*|предлага\w*|"
        r"только\s+(?:если|после)|при\s+условии|\b(?:refus\w*|rejett?\w*|"
        r"accepte\w*|accord\w*|propose\w*|contre-proposition|à condition que|"
        r"n['’]\w+\s+pas|ne\b.{0,35}\b(?:pas|plus|jamais)\b|"
        r"seulement si|uniquement après|est-ce que)\b", re.I,
    )
    for decoded in structured:
        for row in decoded["actions"]:
            if not isinstance(row, dict):
                continue
            number = row.get("candidate")
            if len(candidates) == 1 and len(decoded["actions"]) == 1:
                number = 1
            if not isinstance(number, int) or not 1 <= number <= len(candidates):
                continue
            _, action_id, span = candidates[number - 1]
            if (explicit_act_cue.search(span)
                    and row.get("act") != _source_communication_act(span)):
                disagreements.add(action_id)
    return disagreements


def _semantic_action_counts(semantic_summary: str) -> dict[str, int]:
    """Count action mentions, excluding rows that only describe conditions/effects."""
    counts: dict[str, int] = {}
    structured = [obj for obj in _decode_json_objects(semantic_summary)
                  if isinstance(obj.get("actions"), list)]
    if structured:
        for decoded in structured:
            for row in decoded["actions"]:
                if isinstance(row, dict) and row.get("id") in ACTION_IDS:
                    action_id = row["id"]
                    counts[action_id] = counts.get(action_id, 0) + 1
        return counts
    for original_line in str(semantic_summary or "").splitlines():
        line = original_line.strip(" |\t")
        if not line or re.match(r"^(?:R\d+|\[R\d+\]|condition\b|exception\b|"
                                r"условие\b|исключение\b|condition\s*:|exception\s*:)", line, re.I):
            continue
        line_has_action_verb = bool(_ACTION_VERB_CUE.search(line))
        numbered_candidate = bool(re.match(r"^\[\d+\]", line))
        row_label = bool(re.match(r"^(?:ACTION|ДЕЙСТВИЕ|ДІЯ|PROPOSE|PROPOSITION)\b", line, re.I))
        normalized_line = line.replace("_", " ")
        for action_id, cue in _ACTION_CUES.items():
            for match in cue.finditer(normalized_line):
                if _CURRENT_EFFECT_CUE.search(normalized_line) and not line_has_action_verb:
                    continue
                local_span = _source_span_for_cue(normalized_line, match.span(), action_id)
                relation_only = bool(local_span and (
                    (_RELATION_CUES["condition"].search(local_span)
                     or _RELATION_CUES["exception"].search(local_span))
                    and not _ACTION_VERB_CUE.search(local_span)
                ))
                if relation_only:
                    continue
                if line_has_action_verb or numbered_candidate or row_label:
                    counts[action_id] = counts.get(action_id, 0) + 1
    return counts


def _decode_json_objects(value: str) -> list[dict]:
    """Decode consecutive JSON objects returned by one initial plus recovery calls."""
    text = str(value or "")
    decoder = json.JSONDecoder()
    results = []
    cursor = 0
    while cursor < len(text):
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor >= len(text):
            break
        try:
            decoded, end = decoder.raw_decode(text, cursor)
        except ValueError:
            return []
        if not isinstance(decoded, dict):
            return []
        results.append(decoded)
        cursor = end
    return results


def _valid_tag(value: Any) -> str | None:
    return value.upper() if isinstance(value, str) and COUNTRY_TAG_RE.fullmatch(value.upper()) else None


def _source_position(source_text: str, action_id: str) -> int:
    span = _source_span_for_action(source_text, action_id)
    return source_text.casefold().find(span.casefold()) if span else len(source_text)


def _tags_in_text(source_text: str, aliases: dict[str, Any],
                  allowed_tags: list[str] | set[str] | tuple[str, ...] | None = None) -> list[str]:
    allowed = ({str(tag).upper() for tag in allowed_tags if _valid_tag(str(tag))}
               if allowed_tags is not None else None)
    valid_alias_tags = {tag.upper() for tag in aliases if _valid_tag(tag)
                        and (allowed is None or tag.upper() in allowed)}
    literal = {tag for tag in _TAG.findall(source_text)
               if tag in valid_alias_tags or f"[{tag}]" in source_text}
    found = set(literal)
    for tag, names in aliases.items():
        if (_valid_tag(tag) and (allowed is None or tag.upper() in allowed)
                and _country_alias_occurs(names, source_text)):
            found.add(tag.upper())
    return sorted(found)


def _country_alias_positions(aliases: dict[str, Any], tag: str, source_text: str) -> list[int]:
    names = aliases.get(tag) or aliases.get(tag.upper())
    if isinstance(names, str):
        names = (names,)
    if not isinstance(names, (list, tuple, set)):
        names = ()
    names = (*names, tag) # Actual participant tags are valid explicit subjects too.
    source_tokens = list(re.finditer(r"[\wÀ-ž]+", source_text.casefold(), flags=re.UNICODE))
    positions = []
    for name in names:
        alias_tokens = re.findall(r"[\wÀ-ž]+", str(name).casefold(), flags=re.UNICODE)
        if not alias_tokens:
            continue
        for start in range(len(source_tokens) - len(alias_tokens) + 1):
            matched = True
            for offset, alias_word in enumerate(alias_tokens):
                candidate = source_tokens[start + offset].group(0)
                if _alias_token_matches(alias_word, candidate):
                    continue
                matched = False
                break
            if matched:
                positions.append(source_tokens[start].start())
    return positions


def _explicit_action_actor(source_excerpt: str, action_id: str, participants: list[str],
                           aliases: dict[str, Any]) -> str | None:
    if action_id == "guarantee":
        issuer = re.search(r"(?:\bguarantee\s+(?:offered|provided|issued|given)\s+by|"
                           r"\bgarantie\s+(?:offerte|accordée|fournie|donnée)\s+par)\s+([^.;!?]+)",
                           source_excerpt, re.I)
        if issuer:
            subject = re.split(r"\s+(?:to|à|au|aux)\s+", issuer.group(1), maxsplit=1, flags=re.I)[0]
            candidates = [tag for tag in participants
                          if _country_alias_positions(aliases, tag, subject)]
            return candidates[0] if len(candidates) == 1 else None
    effect_verbs = {
        "military_access": re.compile(
            r"grant\w*|provid\w*|giv(?:e|es|ing)|open\w*|accord\w*|предостав\w*|открыва\w*|accord\w*|donne\w*",
            re.I,
        ),
        "guarantee": re.compile(
            r"(?:предостав\w*|provid\w*|grant\w*|giv\w*)"
            r"(?=[^.;!?]{0,80}(?:guarantee|гаранти))|guarante\w*|garant\w*|гарантир\w*", re.I),
    }
    verb = effect_verbs.get(action_id)
    match = verb.search(source_excerpt) if verb else None
    if not match:
        return None
    preceding = []
    for tag in participants:
        positions = _country_alias_positions(aliases, tag, source_excerpt)
        before = [position for position in positions if position < match.start()]
        if before:
            preceding.append((max(before), tag))
    return max(preceding)[1] if preceding else None


def _country_name_for_tag(tag: str | None, aliases: dict[str, Any]) -> str | None:
    if tag is None:
        return None
    names = aliases.get(tag) or aliases.get(tag.upper())
    if isinstance(names, str):
        return names
    if isinstance(names, (list, tuple, set)):
        return next(iter(names), None)
    return None


_DURATION = re.compile(
    r"\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|un|une|deux|trois|quatre|"
    r"cinq|six|sept|huit|neuf|dix)\s*[- ]?\s*(?:days?|jours?|years?|months?|ans?|années?|mois)\b|"
    r"\b(?:один|одна|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять|\d+)\s*"
    r"(?:дн\w*|лет|года?|месяц\w*)\b|"
    r"\b(?:одно|двух|тр[её]х|четыр[её]х|пяти|шести|семи|восьми|девяти|десяти)летн\w*\b|"
    r"\b\d+[- ]летн\w*\b", re.I,
)
_DURATION_TRANSITION = re.compile(
    r"\b(?:from|de|du|с|со|з)\s+(?P<old>" + _DURATION.pattern + r")\s*"
    r"(?:to|à|au|до)\s+(?P<new>" + _DURATION.pattern + r")",
    re.I,
)
_DURATION_REPLACEMENT = re.compile(
    r"(?P<new>" + _DURATION.pattern + r")\s+(?:instead of|rather than|in place of|au lieu de|вместо|замість)\s+"
    r"(?:the\s+)?(?P<old>" + _DURATION.pattern + r")",
    re.I,
)
_DURATION_CHANGE_CUE = re.compile(
    r"\b(?:revise\w*|change\w*|shorten\w*|extend\w*|reduce\w*|increase\w*|"
    r"replace\w*|amend\w*|now|instead)\b|"
    r"измен\w*|пересмотр\w*|сократ\w*|продл\w*|увелич\w*|уменьш\w*|теперь|вместо|"
    r"змін\w*|перегляд\w*|скороч\w*|подовж\w*|збільш\w*|зменш\w*|тепер|замість|"
    r"révis\w*|modifi\w*|raccourc\w*|prolong\w*|augment\w*|rédui\w*|désormais|à la place",
    re.I,
)


_DURATION_ACTION_ANCHOR = re.compile(
    r"\b(?:non[ -]?aggression\s+pact|pact|truce|ceasefire|armistice|"
    r"пакт|перемири\w*|ненапад\w*|неагресс\w*|"
    r"pacte|non-agression|trêve)\b", re.I,
)
_DURATION_ACTION_PERIOD_ANCHOR = re.compile(
    r"\b(?:duration|validity|period|term)\b|срок\w*|длительност\w*|"
    r"(?:durée|période|terme)\b|строк\w*|тривалост\w*", re.I,
)
_DURATION_OTHER_TERM_ANCHOR = re.compile(
    r"\b(?:advance[ -]notice|prior notice|written notice|notification|"
    r"guarantee|written commitment|equal obligations|embargo|military access|"
    r"предварительн\w+ уведом|уведомлени\w*|гаранти\w*|"
    r"письмов\w+ повідом|повідомленн\w*|гаранті\w*)\b", re.I,
)


def extract_duration(source_text: str, *, action_id: str | None = None) -> str | None:
    transition = _DURATION_TRANSITION.search(source_text)
    if transition:
        value = re.sub(r"\s+", " ", transition.group("new").replace("-", " ")).strip()
        return _normalize_duration(value)
    replacement = _DURATION_REPLACEMENT.search(source_text)
    if replacement:
        value = re.sub(r"\s+", " ", replacement.group("new").replace("-", " ")).strip()
        return _normalize_duration(value)
    matches = list(_DURATION.finditer(source_text))
    if not matches:
        return None
    if action_id in {"non_aggression_pact", "truce"}:
        # A single contextual action span can contain a separate period for a
        # notice/guarantee clause. Do not let (for example) "30 days' notice"
        # replace the pact's "three years" duration. Discard only a duration
        # nearer to a distinct paper-term cue than to the pact cue.
        retained = []
        action_cues = list(_DURATION_ACTION_ANCHOR.finditer(source_text))
        # Elliptical follow-ups often say "the proposed five-year duration"
        # without repeating "pact". Treat that phrase as the action-duration
        # anchor so a nearby 30-day notice clause cannot overwrite the pact.
        action_cues.extend(_DURATION_ACTION_PERIOD_ANCHOR.finditer(source_text))
        term_cues = list(_DURATION_OTHER_TERM_ANCHOR.finditer(source_text))
        if action_cues and term_cues:
            for match in matches:
                action_distance = min(abs(match.start() - cue.end()) for cue in action_cues)
                term_distance = min(abs(match.start() - cue.start()) for cue in term_cues)
                retained.append(match) if action_distance <= term_distance else None
            # If every numeric period belongs more closely to a separate
            # paper clause, leave the pact duration unset. Falling back to the
            # first unrelated period silently turns (for example) a 30-day
            # notice window into a 30-day pact.
            if not retained:
                return None
            matches = retained
    match = matches[-1] if (_COUNTER_CUE.search(source_text)
                            or _DURATION_CHANGE_CUE.search(source_text)) else matches[0]
    value = re.sub(r"\s+", " ", match.group(0).replace("-", " ")).strip()
    return _normalize_duration(value)


def _normalize_duration(value: str) -> str:
    from diplomatic_language import period
    if re.search(r"летн", value, re.I) and period(value):
        return f"{period(value)[0]} лет"
    parts = value.split()
    if len(parts) == 2 and parts[1].casefold() in {"year", "month", "day"}:
        if parts[0].casefold() not in {"one", "un", "une", "один", "одна", "1"}:
            parts[1] += "s"
        value = " ".join(parts)
    elif len(parts) == 2 and parts[1].casefold() == "an" and parts[0].casefold() not in {"un", "une", "1"}:
        value = f"{parts[0]} ans"
    return value


def _has_duration_cue(source_text: str) -> bool:
    # Require token boundaries for English/French duration units. The old
    # unbounded ans? alternative matched "an" inside "France", so a normal
    # bilateral pact was incorrectly marked as missing duration.
    return bool(re.search(
        r"\b\d+\b|\byears?\b|\bmonths?\b|\bdays?\b|\bans?\b|\bmois\b|\bannées?\b|"
        r"\bлет\b|\bгод\w*|\bмесяц\w*|\bдн\w*",
        source_text, re.I,
    ))


_NON_ACTION_DISCLAIMER = re.compile(
    r"\bno\s+(?:acceptance|consent|agreement)\s+(?:is\s+)?"
    r"(?:presumed|assumed|implied|inferred)\b[,;:]?|"
    r"\bno\s+(?:game\s+)?action\s+is\s+"
    r"(?:approved|prepared|authorized|executed|applied)"
    r"(?:\s+or\s+(?:approved|prepared|authorized|executed|applied))*\b[.!;,:]?",
    re.I,
)
_NO_ASSENT_STATUS = re.compile(
    r"\b(?:[A-Z][\w-]*|I|we|they|it)\s+(?:has|have|had)\s+not\s+(?:yet\s+)?"
    r"(?:accepted|agreed(?:\s+to)?|signed|approved)\b[^.!?\n]*[.!?]?",
    re.I,
)
_QUESTIONED_STANCE_ALTERNATIVES = re.compile(
    r"\b(?:whether|if)\b[^.!?]{0,180}\b"
    r"(?:accepts?|agrees?|requires?|rejects?|refuses?|declines?)\b[^.!?]*|"
    r"\b(?:explain|state|give|provide)\b[^.!?]{0,60}\b(?:any\s+)?"
    r"(?:refusal|rejection|decline)\b[^.!?]*",
    re.I,
)
_DIRECT_COUNTRY_STANCE_QUESTION = re.compile(
    r"\b(?:does|do|did|can|could|will|would|has|have)\s+"
    r"(?:[A-Z][\wÀ-ÿ.'-]*(?:\s+[A-Z][\wÀ-ÿ.'-]*){0,3}|you|we|they)\s+"
    r"(?:explicitly\s+)?(?:accept|agree|sign|ratify|confirm|reject|refuse|decline)\b",
    re.I,
)
_NEGOTIATION_STATUS_INSTRUCTION = re.compile(
    r"\b(?:do not|don't|don’t|never|must not)\s+(?:treat|count|consider)\s+"
    r"(?:the\s+)?(?:discussion|conversation|negotiation|proposal|chat|exchange)\b"
    r"[^.!?]{0,100}\bas\s+(?:an?\s+)?(?:agreement|acceptance|consent|deal)\b[^.!?]*[.!?]?",
    re.I,
)
_COUNTEROFFER_REQUEST = re.compile(
    r"\b(?:make|state|give|provide|submit)\s+(?:(?:a|your|the)\s+)?"
    r"(?:(?:specific|concrete|formal|shorter|different|exact)\s+){0,2}"
    r"(?:counterproposal|counter-offer|counteroffer)\b|"
    r"\bwhat\b[^.!?]{0,100}\b(?:would|could|will)\b[^.!?]{0,60}"
    r"\b(?:propose|offer|suggest)\b[^.!?]{0,50}\b(?:shorter|different|alternative|counter)\b|"
    r"\b(?:what|which)\b[^.!?]{0,80}\b(?:shorter|different|alternative)\b[^.!?]{0,60}"
    r"\b(?:would|could)\b[^.!?]{0,50}\b(?:propose|offer|suggest)\b",
    re.I,
)


def _without_non_action_disclaimers(source_text: str) -> str:
    """Remove process disclaimers that describe the test, not the proposal's polarity."""
    # A report that a party has not accepted/signed yet records the absence of
    # consent. It is not an explicit rejection and must not negate a different
    # proposal or question in the same message.
    without_status = _NO_ASSENT_STATUS.sub(" ", str(source_text or ""))
    without_non_action = _NON_ACTION_DISCLAIMER.sub(" ", without_status)
    return _NEGOTIATION_STATUS_INSTRUCTION.sub(" ", without_non_action)


def _source_communication_act(source_text: str) -> str:
    source_text = _without_non_action_disclaimers(source_text)
    # A direct question asking another signatory to accept/sign is not the
    # sender's own rejection merely because its package contains negative
    # safeguards (for example, “no territorial changes”). Keep that speech act
    # distinct from the proposal's conditional polarity below.
    if "?" in source_text and _DIRECT_COUNTRY_STANCE_QUESTION.search(source_text):
        return "question"
    # A rejection followed by a request that the counterpart devise terms is
    # not itself a counteroffer. Otherwise its conditional example (e.g. “three
    # years would be acceptable if France independently proposes it”) can be
    # promoted into a new treaty version before France has proposed anything.
    if _COUNTEROFFER_REQUEST.search(source_text):
        return "question"
    if _source_polarity(source_text) == "mixed":
        return "counter_offer"
    if _source_polarity(source_text) == "negated":
        return "rejection_statement"
    if _source_polarity(source_text) == "conditional":
        return "condition"
    speech_act_text = _QUESTIONED_STANCE_ALTERNATIVES.sub(" ", source_text)
    if re.search(r"\b(?:we accept|accepts? the offer|agrees? to|approved|ratify)\b|"
                 r"принима\w*|соглас\w*|accepte\w*|approuve\w*", speech_act_text, re.I):
        return "acceptance_statement"
    if re.search(r"\b(?:do not|don't|never|reject\w*|refus\w*|not accept)\b|"
        r"отклон\w*|не\s+(?:соглас\w*|ввод|предостав|гарантир|заключ|принимать|принимаем)|"
                 r"\b(?:ne\s+pas|refus\w*|rejett?\w*|n'accepte pas)\b", speech_act_text, re.I):
        return "rejection_statement"
    if "?" in source_text or re.search(r"\b(?:whether|can|could|will|would|est-ce que|pouvez-vous|может ли|готова ли)\b",
                                       source_text, re.I):
        return "question"
    if re.search(r"\b(?:propose|offer|suggest|proposer|offrir)\b|предлага\w*|пропон\w*",
                 source_text, re.I):
        return "proposal"
    return "proposal"


def _source_polarity(source_text: str) -> str:
    """Detect explicit polarity cues; ambiguity never becomes executable consent."""
    source_text = _without_non_action_disclaimers(source_text)
    # Asking the counterpart whether it accepts, requires, or rejects a term
    # names possible positions; it is not the sender's rejection/counteroffer.
    stance_question = _QUESTIONED_STANCE_ALTERNATIVES.search(source_text)
    polarity_text = (_QUESTIONED_STANCE_ALTERNATIVES.sub(" ", source_text)
                     if stance_question else source_text)
    # Negated clauses inside an explicitly enumerated condition list describe
    # safeguards, not the sender's rejection of the diplomatic instrument.
    # Remove only the negation tokens in that bounded list; explicit verbs such
    # as “reject” or “cannot accept” remain visible to the polarity check.
    condition_list = re.search(
        r"\b(?:under|subject\s+to|with|on)\s+"
        r"(?:(?:all|each|these|those|the|stated|listed|following)\s+)*"
        r"(?:(?:three|3|\d+)\s+)?conditions?\s*:\s*",
        polarity_text, re.I,
    )
    if condition_list:
        end_candidates = [position for mark in ".!?\n"
                          if (position := polarity_text.find(mark, condition_list.end())) >= 0]
        end = min(end_candidates) if end_candidates else len(polarity_text)
        condition_text = polarity_text[condition_list.end():end]
        explicit_refusal = re.search(
            r"\b(?:reject\w*|refus\w*|declin\w*|cannot\s+accept|can't\s+accept|"
            r"will\s+not\s+sign|won't\s+sign)\b|"
            r"отклон\w*|отказыва\w*|не\s+(?:принима\w*|соглас\w*)",
            condition_text, re.I,
        )
        if not explicit_refusal:
            neutralized = re.sub(
                r"\b(?:no|not|without|doesn't|don't|does\s+not|do\s+not|"
                r"will\s+not|shall\s+not)\b",
                " ", condition_text, flags=re.I,
            )
            polarity_text = (polarity_text[:condition_list.end()] + neutralized
                             + polarity_text[end:])
    negative = re.search(
        r"\b(?:do not|don't|never|reject\w*|refus\w*|not|no)\b|"
        r"отклон\w*|\b(?:не|ни|без)\s+(?:соглас\w*|ввод\w*|предостав\w*|гарантир\w*|заключ\w*|приним\w*|будем|станем)|"
        r"\b(?:ne\s+pas|ne\b.{0,35}\b(?:pas|plus|jamais)\b|n['’]est pas|"
        r"n['’]accepte pas|n['’]\w+\s+pas|ne\s+sera pas|ni\s+ne\b|refus\w*|rejett?\w*|interdit\w*)\b",
        polarity_text, re.I,
    )
    if negative:
        if _COUNTER_CUE.search(polarity_text):
            return "mixed"
        if re.search(r"\b(?:not|no)\b.{0,30}\buntil\b|\bне\b.{0,35}\bдо\b|"
                     r"только\s+после|лишь\s+после|до\s+тех\s+пор\s+пока|пока\s+не|"
                     r"ne\s+.*\s+que\b", polarity_text, re.I):
            return "conditional"
        return "negated"
    if re.search(r"\b(?:only if|only after|unless|provided(?:\s+that)?|subject to|"
                 r"under\s+(?:(?:all|each|these|those|the|stated|listed|following)\s+)*"
                 r"(?:(?:three|3|\d+)\s+)?conditions?)\b|"
                 r"\bесли\b|только\s+если|только\s+после|при\s+условии|лишь\s+после|"
                 r"\b(?:if|si|à condition que|seulement si|uniquement après)\b|"
                 r"\bne\b.{0,80}\bqu['’]", polarity_text, re.I):
        return "conditional"
    return "affirmed"


def _grounded_list(value: Any, source_text: str) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value[:8]:
        if (isinstance(item, str) and 0 < len(item) <= 240
                and item.casefold() in source_text.casefold()):
            result.append(item.strip())
    return list(dict.fromkeys(result))


__all__ = ["ACTION_IDS", "MESSAGE_ANALYSIS_SCHEMA", "INTENT_COVERAGE_SCHEMA", "validate_message_analysis",
           "build_analysis_from_summary"]
