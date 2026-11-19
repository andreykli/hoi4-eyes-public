"""World-news headlines from the bilingual catalog, opt-in per process.

The existing engine keeps its built-in headline so that nothing changes for
current installations. With ``EYES_NEWS_TEMPLATES=1`` the catalog supplies a
varied headline for the two supported confirmation levels only. Categories that
the engine cannot yet prove (alliances, crises, expiries) stay unconnected and
are listed in ``PRERARED_NOT_CONNECTED``.
"""
from __future__ import annotations

import os
from typing import Any, Mapping, Sequence

import diplomatic_templates as catalog

ENV_FLAG = "EYES_NEWS_TEMPLATES"

# Engine status -> catalog status. Everything else stays with the built-in text:
# publishing a "treaty expired" headline before an expiry check exists would be
# a false event.
STATUS_MAP = {"execution_confirmed": "execution_confirmed",
              "execution_verified": "execution_verified"}

ACTIONS = {
    "non_aggression_pact": ("security", {"ru": "пакт о ненападении", "en": "non-aggression pact"}),
    "guarantee": ("security", {"ru": "гарантии", "en": "guarantees"}),
    "military_access": ("alliance", {"ru": "военный доступ", "en": "military access"}),
    "embargo": ("trade", {"ru": "торговое эмбарго", "en": "trade embargo"}),
    "lift_embargo": ("trade", {"ru": "снятие эмбарго", "en": "lifting of the embargo"}),
    "territory_partition": ("territory", {"ru": "территориальное соглашение", "en": "territorial settlement"}),
    "political_annexation": ("sovereignty", {"ru": "политическое присоединение", "en": "political annexation"}),
    "white_peace": ("peace", {"ru": "белый мир", "en": "white peace"}),
    "faction_join": ("alliance", {"ru": "вступление во фракцию", "en": "faction admission"}),
    "faction_leave": ("alliance", {"ru": "выход из фракции", "en": "departure from the faction"}),
}

PREPARED_NOT_CONNECTED = ("alliance_created", "alliance_dissolved", "diplomatic_crisis",
                          "relations_shift", "territory_change", "treaty_expired",
                          "treaty_breached")

_cache: dict[str, Any] = {"templates": None}


def enabled() -> bool:
    return os.getenv(ENV_FLAG, "0").strip().casefold() in {"1", "true", "yes"}


def templates() -> list[dict[str, Any]]:
    if _cache["templates"] is None:
        _cache["templates"] = catalog.load()
    return _cache["templates"]


def reset_cache() -> None:
    _cache["templates"] = None


def headline(*, status: str, participants: Sequence[str], action_id: str,
             language: str, key: str, date: str | None = None) -> str | None:
    """Return a catalog headline, or None when the event has no safe template."""
    catalog_status = STATUS_MAP.get(status)
    if catalog_status is None or not isinstance(participants, (list, tuple)) or len(participants) < 2:
        return None
    language = language if language in {"ru", "en"} else "en"
    _, label = ACTIONS.get(action_id, ("general", {"ru": "договор", "en": "treaty"}))
    candidates = catalog.filter_templates(
        [row for row in templates() if row['id'].startswith('news.verified.')],
        category="world_news", status=catalog_status,
        placeholders=("{COUNTRY_A}", "{COUNTRY_B}"),
    )
    if not candidates:
        return None
    template = catalog.choose(candidates, key=f"news|{key}|{catalog_status}")
    values = {"{COUNTRY_A}": str(participants[0]), "{COUNTRY_B}": str(participants[1]),
              "{TREATY_TYPE}": label[language], "{TOPIC}": label[language]}
    if date:
        values["{DATE}"] = str(date)
    try:
        return catalog.render(template, values, language=language)
    except catalog.TemplateError:
        return None
