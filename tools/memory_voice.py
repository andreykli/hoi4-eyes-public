"""Memory voice: turn a bounded pair summary into one diplomatic line.

The summary comes from ``diplomatic_memory.pair_summary`` and is already
campaign-scoped and prompt-sized. This module never invents history: with no
validated position and no confirmed outcome it returns None.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

import diplomatic_templates as catalog

READINESS_TONE = {
    "recent_refusal_recorded": "distrustful",
    "open_with_conditions": "conciliatory",
    "continuing_negotiation": "neutral",
    "position_deferred": "cautious",
    "information_requested": "cautious",
    "constructive_position_nonbinding": "neutral",
}

def tone_for(summary: Mapping[str, Any]) -> str | None:
    """Pick a tone only when the summary carries real evidence."""
    if not isinstance(summary, Mapping):
        return None
    positions = summary.get("prior_negotiation_positions") or []
    outcomes = summary.get("confirmed_game_outcomes") or []
    if not positions and not outcomes:
        return None
    readiness = summary.get("readiness") or ""
    trust = (summary.get("promise_trust") or {}).get("value")
    tone = READINESS_TONE.get(readiness)
    try:
        number = float(trust)
    except (TypeError, ValueError):
        number = None
    if number is not None and number < 0.4:
        tone = "cautious" if tone in (None, "neutral") else tone
    if number is not None and number >= 0.75:
        tone = "conciliatory"
    return tone or "neutral"


def voice(*, summary: Mapping[str, Any], country_a: str, country_b: str,
          action_id: str, language: str, key: str,
          templates: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any] | None:
    tone = tone_for(summary)
    if tone is None:
        return None
    status = "defer" if (summary.get("readiness") == "position_deferred") else "clarify"
    language = language if language in {"ru", "en", "fr"} else "en"
    pool = list(templates) if templates is not None else catalog.load()
    candidates = catalog.filter_templates(
        [row for row in pool if row['id'].startswith('memory.observed.')],
        category="memory", status=status)
    if not candidates:
        return None
    template = catalog.choose(candidates, key=f"memory|{key}|{tone}|{status}")
    values = {}
    try:
        text = catalog.render(template, values, language=language)
    except catalog.TemplateError:
        return None
    return {"tone": tone, "status": "not_factual", "template_id": template["id"],
            "text": text, "readiness": summary.get("readiness"),
            "game_effect": None}
