"""Interpret short player replies against one exact, saved bilateral draft."""
from copy import deepcopy
import re


ASSENT = (
    r"(?:(?:я|мы) )?(?:тоже |также )?соглас(?:ен|на|ны)|(?:(?:я|мы) )?подтвержда(?:ю|ем)(?: условия)?|"
    r"принима(?:ю|ем)(?: (?:эти |все )?условия)?|"
    r"(?:i|we) (?:agree|accept|confirm)(?: (?:the |these )?terms)?|confirmed|"
    r"d'accord|(?:je|nous) (?:suis|sommes) d'accord|"
    r"j'accepte(?: les conditions)?|nous acceptons(?: les conditions)?|"
    r"je confirme(?: les conditions)?|nous confirmons(?: les conditions)?"
)
REJECTION = (r"нет|отказыва(?:юсь|емся)|(?:(?:я|мы) )?не соглас(?:ен|на|ны)|"
             r"no|(?:i|we) (?:decline|don't agree)|non|je refuse|nous refusons|je ne suis pas d'accord")

# An explicit reference to unchanged terms is assent, even if it repeats the
# name of the pact. Keep the grammar closed so new conditions reach the parser.
NAP_CONFIRMATION = re.compile(
    r"(?:подтвержда(?:ю|ем) (?:условия )?пакт(?:а)? о ненападении,? "
    r"(?:редакци[яию] ?(?:№ ?)?|v)(?P<short>[1-9][0-9]*),? без изменений|"
    r"я имею в виду пакт о ненападении между германией и францией, редакция № ?(?P<pair>[1-9][0-9]*)\. "
    r"поскольку франция уже согласилась с предложенными условиями, германия (?:также )?"
    r"(?:официально )?подтверждает своё согласие на заключение этого пакта без изменений)"
)


def short_reply(text):
    normalized = " ".join(str(text or "").casefold().replace("’", "'").split())
    if normalized.endswith("?"):
        return None
    text = normalized.rstrip(" .!…")
    if re.fullmatch(ASSENT, text):
        return "accept"
    if re.fullmatch(REJECTION, text):
        return "reject"
    if re.fullmatch(rf"(?:{ASSENT})[, ]+(?:но|but|mais)", text):
        return "qualified"
    return None


MESSAGES = {
    "ru": {
        "accepted": "Ваше подтверждение получено. Обе стороны согласовали редакцию № {version} «{name}». Подготовку игрового действия можно отдельно разрешить в окне условий.",
        "rejected": "Ваш отказ по редакции № {version} «{name}» зафиксирован.",
        "qualified": "Какое условие вы хотите изменить или добавить? Укажите его целиком.",
        "ambiguous": "Укажите, о каком предложении и редакции идёт речь. Я не могу привязать короткий ответ к конкретным условиям.",
        "pending": "Пока нельзя подтвердить редакцию № {version} целиком. Нужно согласовать следующие пункты: {terms}.",
    },
    "en": {
        "accepted": "We have received your confirmation. Both parties agree on version {version} of “{name}”. You can separately authorize game preparation in the conditions window.",
        "rejected": "Your refusal of version {version} of “{name}” has been recorded.",
        "qualified": "Which term would you change or add? Please state it in full.",
        "ambiguous": "Which proposal and version do you mean? Please identify the terms your reply refers to.",
        "pending": "Version {version} still has terms to settle: {terms}. Please address those points first.",
    },
    "fr": {
        "accepted": "Nous avons reçu votre confirmation. Les deux parties approuvent la version {version} de « {name} ». Vous pouvez autoriser séparément la préparation dans la fenêtre des conditions.",
        "rejected": "Votre refus de la version {version} de « {name} » a été enregistré.",
        "qualified": "Quelle clause souhaitez-vous modifier ou ajouter ? Veuillez en donner le texte complet.",
        "ambiguous": "De quelle proposition et de quelle version s’agit-il ? Indiquez les clauses visées par votre réponse.",
        "pending": "La version {version} comporte encore des points à régler : {terms}. Veuillez préciser votre position sur ces clauses.",
    },
}


def plan_reply(chat, source, country, response_id):
    """Build a reply and optional consent update from stored evidence only."""
    text = (source or {}).get("text", "")
    stance = short_reply(text)
    explicit = NAP_CONFIRMATION.fullmatch(" ".join(text.casefold().split()).rstrip(" .!"))
    if explicit and (not explicit["pair"] or (chat.get("player_country"), country) == ("GER", "FRA")):
        stance = "accept"
    else:
        explicit = None
    language = chat.get("response_language")
    if not stance or language not in MESSAGES or chat.get("chat_mode") == "ai_to_ai":
        return None
    player = chat.get("player_country")
    source_id = source.get("request_id")
    if (source.get("kind") != "player_message" or source.get("speaker") != player
            or not source_id or country == player or set(chat.get("participants", [])) != {player, country}):
        return None
    history = chat.get("messages", [])
    index = next((i for i, item in enumerate(history) if item.get("request_id") == source_id), None)
    if index is None:
        return None
    preceding_turns = [item for item in history[:index] if item.get("kind") != "initiative_notice"]
    while stance == "accept" and len(preceding_turns) >= 2:
        attempted, failed = preceding_turns[-2:]
        if not (failed.get("kind") == "country_response" and failed.get("speaker") == country
                and failed.get("provider_status") in {"fallback", "timeout", "error"}
                and not failed.get("agreement_id") and attempted.get("kind") == "player_message"
                and attempted.get("speaker") == player and short_reply(attempted.get("text")) == "accept"
                and failed.get("player_source_request_id") == attempted.get("request_id")):
            break
        del preceding_turns[-2:]
    preceding = preceding_turns[-1] if preceding_turns else {}
    agreements = chat.get("agreements") or {}
    agreement = agreements.get(preceding.get("agreement_id"))
    if stance == "accept" and (explicit or agreement is None):
        candidates = [item for item in agreements.values()
                      if item.get("status") in {"draft", "awaiting_approval"}
                      and set(item.get("participants", [])) == {player, country}
                      and (not explicit or item.get("action_id") == "non_aggression_pact"
                           and str(item.get("current_version")) == (explicit["short"] or explicit["pair"]))]
        agreement = candidates[0] if len(candidates) == 1 else None
        if agreement is not None:
            for item in reversed(preceding_turns):
                if (item.get("speaker") == country and item.get("agreement_id") == agreement["agreement_id"]
                        and item.get("agreement_version") == agreement["current_version"]):
                    preceding = item
                    break
                if item.get("kind") == "player_message" and short_reply(item.get("text")) == "accept":
                    continue
                if (item.get("kind") == "country_response" and not item.get("agreement_id")
                        and not (item.get("message_analysis") or {}).get("actions")
                        and (item.get("provider_status") in {"fallback", "timeout", "error"}
                             or item.get("provider_reason") == "contextual_player_reply")):
                    continue
                agreement = None
                break
            else:
                agreement = None
    strings = MESSAGES[language]

    def reply(key, *, record=None, **fields):
        return {"text": strings[key].format(**fields), "stance": stance,
                "agreement": agreement if key in {"accepted", "rejected", "pending"} else None,
                "record": record, "source_request_id": source_id}

    if stance == "qualified":
        return reply("qualified")
    if (not agreement or agreement.get("status") not in {"draft", "awaiting_approval"}
            or agreement.get("campaign_id") != chat.get("campaign_id")
            or agreement.get("chat_id") != chat.get("chat_id")
            or preceding.get("kind") != "country_response" or preceding.get("speaker") != country
            or preceding.get("agreement_id") != agreement.get("agreement_id")
            or preceding.get("agreement_version") != agreement.get("current_version")
            or set(agreement.get("participants", [])) != {player, country}
            or agreement.get("approval_requests") or agreement.get("execution_package")):
        return reply("ambiguous")
    version = agreement["current_version"]
    original = agreement["versions"][-1]
    name = agreement.get("name") or f"{player}–{country}"
    terms = original.get("terms") or []
    if (original.get("version") != version or original.get("participant_scope_status") != "source_grounded"
            or not terms or any(set(term.get("required_participants", [])) != {player, country}
                                for term in terms)):
        return reply("ambiguous")
    if stance == "accept" and agreement["status"] == "awaiting_approval":
        if all(term.get("status") == "agreed" and set(term.get("confirmed_by", [])) == {player, country}
               for term in terms) and not original.get("unresolved_items"):
            return reply("accepted", version=version, name=name)
        return reply("ambiguous")

    unresolved = set(original.get("unresolved_items", []))
    allowed = {"player_explicit_acceptance", *(term["term_id"] for term in terms)}
    counterpart_id = preceding.get("request_id")
    source_ids = {item["request_id"] for item in preceding_turns
                  if item.get("kind") == "player_message" and item.get("request_id")}
    from agreement_manager import nap_territory_exclusion
    open_terms = [term for term in terms if (
        (not (term.get("proposal_spec") or {}).get("action") and not nap_territory_exclusion(term))
        or term.get("proposal_source_request_id") not in source_ids or term.get("unparsed_execution_details")
        or term.get("conditional_by") or term.get("rejected_by")
        or country not in term.get("confirmed_by", [])
        or (term.get("confirmed_by_request_ids") or {}).get(country) != counterpart_id
        or (term.get("confirmation_source_request_ids") or {}).get(country) != term["proposal_source_request_id"]
    )]
    if stance == "accept" and (open_terms or unresolved - allowed
            or original.get("interpreted_actions") or original.get("conditional_acceptance_by")
            or not counterpart_id or preceding.get("retryable") or preceding.get("decision") != "accept"):
        names = "; ".join(term.get("text") or term["term_id"] for term in open_terms or terms)
        return reply("pending", version=version, terms=names)
    if stance == "reject" and agreement["status"] != "draft":
        return reply("ambiguous")

    record = deepcopy(original)
    for term in record["terms"]:
        term_id = term["term_id"]
        confirmed = term.setdefault("confirmed_by_request_ids", {})
        position = {"stance": "accepted" if stance == "accept" else "rejected",
                    "request_id": source_id, "source_request_id": term.get("proposal_source_request_id"),
                    "response_request_id": response_id, "participant_role": "party",
                    "required_for_consent": True, "origin": "player_contextual_confirmation",
                    "consent_status": "confirmed" if stance == "accept" else "unconfirmed",
                    "authority": "player"}
        term.setdefault("participant_positions", {})[player] = position
        term.setdefault("position_request_ids", {})[player] = source_id
        if stance == "accept":
            confirmed[player] = source_id
            term["confirmed_by"] = list(term["required_participants"])
            term.setdefault("confirmation_source_request_ids", {})[player] = term["proposal_source_request_id"]
            term["status"] = "agreed"
        else:
            confirmed.pop(player, None)
            term.setdefault("confirmation_source_request_ids", {}).pop(player, None)
            term["confirmed_by"] = [tag for tag in term.get("confirmed_by", []) if tag != player]
            term["conditional_by"] = [tag for tag in term.get("conditional_by", []) if tag != player]
            term["rejected_by"] = list(dict.fromkeys([*term.get("rejected_by", []), player]))
            term["status"] = "rejected"
        record.setdefault("participant_term_confirmation_request_ids", {})[term_id] = deepcopy(confirmed)
    if stance == "accept":
        record.setdefault("participant_acceptance_request_ids", {})[player] = source_id
        record["status"] = "awaiting_approval"
        record["unresolved_items"] = []
        heading = {"ru": "Нужно уточнить", "en": "Clarification required", "fr": "À clarifier"}[language]
        record["final_text"] = record.get("final_text", "").partition(f"\n{heading}:")[0]
    else:
        record.setdefault("participant_acceptance_request_ids", {}).pop(player, None)
        record["unresolved_items"] = list(dict.fromkeys([*record.get("unresolved_items", []),
                                                        *(term["term_id"] for term in terms)]))
    record["source_request_ids"] = list(dict.fromkeys([*record.get("source_request_ids", []), source_id]))
    return reply("accepted" if stance == "accept" else "rejected", record=record, version=version, name=name)


def commit_reply(plan, entry):
    agreement = plan["agreement"]
    if agreement is None:
        return
    if plan["record"] is not None:
        agreement["versions"][-1] = plan["record"]
        agreement["status"] = plan["record"]["status"]
        agreement["updated_at"] = entry["timestamp"]
    entry.update(agreement_id=agreement["agreement_id"],
                 agreement_version=agreement["current_version"], agreement_status=agreement["status"])


def engine_result(plan, timestamp, request_id):
    record = plan.get("record") or {}
    accepted = (record.get("status") == "awaiting_approval"
                or (plan["stance"] == "accept" and (plan["agreement"] or {}).get("status") == "awaiting_approval"))
    return {"response": plan["text"], "timestamp": timestamp,
            "intent": {"accept": "accept", "reject": "refuse"}.get(plan["stance"], "information"),
            "decision": "accept" if accepted else "informational_reply",
            "reasoning_factors": ["contextual_player_reply"], "action": None,
            "provider_diagnostic": {"provider": "python", "status": "generated",
                                    "reason": "contextual_player_reply", "request_id": request_id,
                                    "elapsed_seconds": 0.0}}
