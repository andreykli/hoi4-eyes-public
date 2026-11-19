"""Diplomatic reply text for one stance, chosen deterministically from the catalog.

This layer only produces wording. It never decides a stance, never changes an
agreement and never claims that a game effect happened: the reply describes the
position, and the three real events (chat consent, in-game acceptance, verified
execution) stay separate and carry their own statuses.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

import os

import diplomatic_templates as catalog

STANCE_CATEGORY = {
    "accept": ("acceptance", "accept"),
    "refuse": ("rejection", "refuse"),
    "counter_offer": ("counteroffer", "counter_offer"),
    "partial_accept": ("counteroffer", "partial_accept"),
    "defer": ("memory", "defer"),
    "clarify": ("memory", "clarify"),
}

ACTION_TOPIC = {
    "non_aggression_pact": "security",
    "guarantee": "security",
    "military_access": "alliance",
    "embargo": "trade",
    "lift_embargo": "trade",
    "territory_partition": "territory",
    "political_annexation": "sovereignty",
}

ACTION_LABEL = {
    "non_aggression_pact": {"ru": "пакт о ненападении", "en": "non-aggression pact"},
    "guarantee": {"ru": "гарантии", "en": "guarantees"},
    "military_access": {"ru": "военный доступ", "en": "military access"},
    "embargo": {"ru": "эмбарго", "en": "embargo"},
    "territory_partition": {"ru": "территориальное соглашение", "en": "territorial settlement"},
    "political_annexation": {"ru": "политическое присоединение", "en": "political annexation"},
}


def reply(*, stance: str, language: str, country_a: str, country_b: str,
          action_id: str, key: str, tone: str | None = None,
          avoid_ids: Sequence[str] = (), templates: Sequence[Mapping[str, Any]] | None = None,
          extra: Mapping[str, str] | None = None,
          memory_summary: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """Return one rendered reply, or None when no safe template exists."""
    mapping = STANCE_CATEGORY.get(stance)
    if mapping is None:
        return None
    category, status = mapping
    language = language if language in {"ru", "en"} else "en"
    pool = list(templates) if templates is not None else catalog.load()
    subcategory = ACTION_TOPIC.get(action_id, "general")
    candidates = catalog.filter_templates(
        pool, category=category, status=status, tone=tone, subcategory=subcategory)
    if not candidates:
        candidates = catalog.filter_templates(pool, category=category, status=status, tone=tone)
    if not candidates:
        return None
    template = catalog.choose(candidates, key=f"reply|{key}|{stance}|{tone}", avoid_ids=avoid_ids)
    label = ACTION_LABEL.get(action_id, {"ru": "договор", "en": "treaty"})
    values = {"{COUNTRY_A}": country_a, "{COUNTRY_B}": country_b,
              "{TREATY_TYPE}": label[language], "{TOPIC}": label[language],
              "{TERMS}": label[language], "{CONDITIONS}": label[language]}
    for name, value in (extra or {}).items():
        if name in catalog.PLACEHOLDERS and isinstance(value, str) and value.strip():
            values[name] = value.strip()
    try:
        text = catalog.render(template, values, language=language)
    except catalog.TemplateError:
        return None
    memory = None
    if memory_summary is not None and os.getenv("EYES_DIPLOMATIC_MEMORY", "0").strip().casefold() in {"1", "true", "yes"}:
        try:
            import memory_voice
            memory = memory_voice.voice(summary=memory_summary, country_a=country_a,
                                        country_b=country_b, action_id=action_id,
                                        language=language, key=key, templates=pool)
        except (ImportError, ValueError, KeyError, TypeError, OSError):
            memory = None
    if memory:
        # Memory colours the wording; it never replaces the legal core chosen here.
        text = f"{memory['text']} {text}"
    return {"stance": stance, "status": "not_factual", "tone": template["tone"],
            "template_id": template["id"], "language": language, "text": text,
            "memory_template_id": memory["template_id"] if memory else None,
            "game_effect": None}
