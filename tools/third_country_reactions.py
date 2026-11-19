"""Third-country reactions: text only, driven by available context.

No reaction may change game relations, join a faction or declare anything. When
the context does not carry enough facts, the module returns a neutral line or
None — it never invents an attitude such as "Britain is always against Germany".
"""
from __future__ import annotations

import os
from typing import Any, Mapping, Sequence

import diplomatic_templates as catalog

AUTO_FLAG = "EYES_AUTO_THIRD_COUNTRY_REACTIONS"
AUTO_LIMIT = 3
AUTO_LOG_LIMIT = 20

def _relation_tone(value: Any) -> str | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number >= 30:
        return "friendly"
    if number <= -50:
        return "distrustful"
    if number <= -15:
        return "cautious"
    return "neutral"


def assess(context: Mapping[str, Any]) -> dict[str, Any]:
    """Derive a tone and a justification from explicit facts only."""
    if not isinstance(context, Mapping):
        return {"tone": None, "reason": "no_context", "known": False}
    at_war = context.get("at_war")
    if at_war is True:
        return {"tone": "distrustful", "reason": "at_war_with_party", "known": True}
    if context.get("same_faction") is True:
        return {"tone": "friendly", "reason": "shares_faction_with_party", "known": True}
    tone = _relation_tone(context.get("relations"))
    if tone is not None:
        return {"tone": tone, "reason": "observed_relations", "known": True}
    if isinstance(context.get("ideology"), str) and context["ideology"].strip():
        return {"tone": "cautious", "reason": "ideology_only", "known": True}
    return {"tone": None, "reason": "insufficient_context", "known": False}


def native_reaction_contexts(memory: Mapping[str, Any], campaign: str, player: str,
                             participants: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Use only directed, same-day AI evidence from this campaign's native export."""
    state = memory.get('v6_autonomy', {}).get('campaigns', {}).get(campaign, {})
    snapshot = state.get('snapshot') or {}
    if not snapshot or state.get('native_sequence') != snapshot['sequence'] or snapshot['player_country'] != player:
        return {}
    day, countries = snapshot['day'], snapshot['countries']
    parties = set(participants)
    if not parties <= countries.keys() or any(countries[tag]['observed_day'] != day for tag in parties):
        return {}
    by_observer: dict[str, list[dict[str, Any]]] = {}
    for pair in snapshot['pairs']:
        actor, target = pair['actor'], pair['target']
        country = countries[actor]
        if (actor not in parties and target in parties and country['ai_controlled']
                and country['observed_day'] == pair['observed_day'] == day):
            by_observer.setdefault(actor, []).append(pair['context'])
    contexts = {}
    for tag, rows in by_observer.items():
        relations = [row['relations'] for row in rows if row.get('relations') is not None]
        # Evidence may cover only one signatory. Prioritize conflict, and use
        # the most cautious observed opinion instead of inventing an average.
        context = {
            'at_war': any(row.get('war_status') is True for row in rows),
            'same_faction': any(row.get('same_faction') is True for row in rows),
            'relations': min(relations) if relations else None,
            'evidence_day': day, 'snapshot_sequence': snapshot['sequence'],
        }
        if context['at_war'] or context['same_faction'] or relations:
            contexts[tag] = context
    return contexts


def auto_react_for_chat(chat: Mapping[str, Any], participants: Sequence[str],
                        action_id: str, event_key: str, *,
                        observed_contexts: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Append text-only reactions for one confirmed event; flag-off means no-op.

    Only countries with an explicit context entry react, at most AUTO_LIMIT per
    event, never a signatory, and never twice for the same (event, speaker).
    Nothing here writes relations, agreements or game state.
    """
    if os.getenv(AUTO_FLAG, "0").strip().casefold() not in {"1", "true", "yes"}:
        return []
    if not isinstance(chat, dict) or len(participants) != 2:
        return []
    contexts = observed_contexts if observed_contexts is not None else chat.get("country_contexts")
    if not isinstance(contexts, Mapping):
        return []
    language = chat.get("response_language") or "en"
    store = chat.setdefault("third_country_reactions", [])
    if not isinstance(store, list):
        store = chat["third_country_reactions"] = []
    seen = {(item.get("event_key"), item.get("speaker")) for item in store if isinstance(item, Mapping)}
    added = []
    for tag, context in sorted(contexts.items()):
        if len(added) >= AUTO_LIMIT:
            break
        if not isinstance(tag, str) or tag in participants or not isinstance(context, Mapping):
            continue
        if (event_key, tag) in seen:
            continue
        result = react(speaker=tag, parties=list(participants), action_id=action_id,
                       context=context, language=language, key=f"{event_key}:{tag}",
                       allow_neutral_fallback=False)
        if result is None:
            continue
        result.update(event_key=event_key, created_by="auto_third_country")
        if observed_contexts is not None:
            result.update(evidence_source='native_snapshot', evidence_day=context['evidence_day'],
                          snapshot_sequence=context['snapshot_sequence'])
        store.append(result)
        seen.add((event_key, tag))
        added.append(result)
    if len(store) > AUTO_LOG_LIMIT:
        del store[:-AUTO_LOG_LIMIT]
    return added


def react(*, speaker: str, parties: Sequence[str], action_id: str, context: Mapping[str, Any],
          language: str, key: str, templates: Sequence[Mapping[str, Any]] | None = None,
          allow_neutral_fallback: bool = True) -> dict[str, Any] | None:
    """Return a text-only reaction dict, or None when nothing responsible can be said."""
    if not isinstance(speaker, str) or not speaker or len(parties) != 2:
        return None
    judged = assess(context)
    tone = judged["tone"]
    if tone is None and not allow_neutral_fallback:
        return None
    if tone is None:
        tone = "neutral"
    language = language if language in {"ru", "en", "fr"} else "en"
    pool = list(templates) if templates is not None else catalog.load()
    candidates = catalog.filter_templates(
        [row for row in pool if row['id'].startswith('third.observed.')],
        category="third_country", tone=tone,
        placeholders=("{COUNTRY_A}", "{COUNTRY_B}", "{COUNTRY_C}"),
    )
    if not candidates:
        return None
    template = catalog.choose(candidates, key=f"third|{key}|{speaker}|{tone}")
    values = {"{COUNTRY_A}": speaker, "{COUNTRY_B}": str(parties[0]),
              "{COUNTRY_C}": str(parties[1])}
    try:
        text = catalog.render(template, values, language=language)
    except catalog.TemplateError:
        return None
    return {"speaker": speaker, "tone": tone, "reason": judged["reason"],
            "template_id": template["id"], "text": text, "status": "not_factual",
            "game_effect": None}
