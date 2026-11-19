"""Bilingual diplomatic template catalog: validation, deterministic choice, rendering.

The catalog lives in ``tools/templates/*.json`` so that no thousands of strings
are embedded in Python. Selection is deterministic: the same event key always
picks the same template, which keeps negotiations reproducible and testable.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

DEFAULT_DIR = Path(__file__).resolve().parent / "template_catalog"

CATEGORIES = ("acceptance", "rejection", "counteroffer", "world_news",
              "third_country", "memory")
TONES = ("friendly", "neutral", "cautious", "distrustful", "cold", "solemn",
         "compromising", "firm", "disappointed", "conciliatory")
STATUSES = ("proposal", "accept", "refuse", "counter_offer", "partial_accept",
            "defer", "clarify", "execution_confirmed", "execution_verified",
            "evidence_pending", "not_factual")
PLACEHOLDERS = ("{COUNTRY_A}", "{COUNTRY_B}", "{COUNTRY_C}", "{TREATY_TYPE}",
                "{DATE}", "{CONDITIONS}", "{TERMS}", "{TOPIC}", "{ACTION}")

_ID = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\.\d{3}$")
_MIN_LEN, _MAX_LEN = 12, 260


class TemplateError(ValueError):
    """Invalid catalog data or an impossible render request; message is a code."""


def _normalized(text: str) -> str:
    return " ".join(re.sub(r"\{[A-Z_]+\}", " @ ", text).casefold().split())


def validate_template(template: Any) -> dict[str, Any]:
    if not isinstance(template, Mapping):
        raise TemplateError("template_not_object")
    required = ("id", "category", "subcategory", "tone", "statuses", "placeholders", "ru", "en")
    if any(key not in template for key in required):
        raise TemplateError("template_missing_field")
    identifier = template["id"]
    if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
        raise TemplateError("invalid_template_id")
    if template["category"] not in CATEGORIES:
        raise TemplateError("invalid_category")
    if template["tone"] not in TONES:
        raise TemplateError("invalid_tone")
    if not isinstance(template["subcategory"], str) or not re.fullmatch(r"[a-z][a-z0-9_]{1,30}", template["subcategory"]):
        raise TemplateError("invalid_subcategory")
    statuses = template["statuses"]
    if not isinstance(statuses, list) or not statuses or any(item not in STATUSES for item in statuses):
        raise TemplateError("invalid_statuses")
    placeholders = template["placeholders"]
    if not isinstance(placeholders, list) or any(item not in PLACEHOLDERS for item in placeholders):
        raise TemplateError("invalid_placeholders")
    used_all: set[str] = set()
    for language in (("ru", "en", "fr") if "fr" in template else ("ru", "en")):
        text = template[language]
        if not isinstance(text, str):
            raise TemplateError("invalid_text")
        if not _MIN_LEN <= len(text) <= _MAX_LEN:
            raise TemplateError("text_length_out_of_range")
        if "\n" in text or "  " in text or text != text.strip():
            raise TemplateError("invalid_text_whitespace")
        used = {item for item in PLACEHOLDERS if item in text}
        leftover = re.findall(r"\{[^}]*\}", text)
        if set(leftover) != used:
            raise TemplateError("unknown_placeholder")
        used_all |= used
    # A language may use a subset: the declared list is the union both sides need.
    if used_all != set(placeholders):
        raise TemplateError("placeholder_list_mismatch")
    return {**template, "statuses": list(statuses), "placeholders": list(placeholders)}


def load(directory: Any = None, *, pattern: str = "*.json") -> list[dict[str, Any]]:
    """Load and strictly validate every catalog file; duplicates are an error."""
    folder = Path(directory) if directory is not None else DEFAULT_DIR
    if not folder.is_dir():
        raise TemplateError("catalog_directory_missing")
    templates: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_text: set[tuple[str, str]] = set()
    for path in sorted(folder.glob(pattern)):
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as error:
            raise TemplateError(f"catalog_unreadable:{path.name}") from error
        if not isinstance(payload, list):
            raise TemplateError(f"catalog_not_list:{path.name}")
        for raw in payload:
            template = validate_template(raw)
            if template["id"] in seen_ids:
                raise TemplateError(f"duplicate_id:{template['id']}")
            seen_ids.add(template["id"])
            pair = (_normalized(template["ru"]), _normalized(template["en"]))
            if pair in seen_text:
                raise TemplateError(f"duplicate_text:{template['id']}")
            seen_text.add(pair)
            templates.append(template)
    return templates


def filter_templates(templates: Iterable[dict[str, Any]], *, category: str,
                     status: str | None = None, tone: str | None = None,
                     subcategory: str | None = None,
                     placeholders: Sequence[str] | None = None) -> list[dict[str, Any]]:
    required = set(placeholders or ())
    selected = []
    for template in templates:
        if template["category"] != category:
            continue
        if status is not None and status not in template["statuses"]:
            continue
        if tone is not None and template["tone"] != tone:
            continue
        if subcategory is not None and template["subcategory"] != subcategory:
            continue
        if not required <= set(template["placeholders"]):
            continue
        selected.append(template)
    return selected


def choose(templates: Sequence[dict[str, Any]], *, key: str,
           avoid_ids: Sequence[str] = ()) -> dict[str, Any]:
    """Deterministically pick one candidate from an already filtered list."""
    if not templates:
        raise TemplateError("no_templates_match")
    ordered = sorted(templates, key=lambda item: item["id"])
    avoided = set(avoid_ids)
    fresh = [item for item in ordered if item["id"] not in avoided] or ordered
    index = int(hashlib.sha256(key.encode("utf-8")).hexdigest(), 16) % len(fresh)
    return fresh[index]


def statistics(templates: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Counts used by tests and the stage report; never guesses at quality."""
    per_category: dict[str, int] = {}
    per_tone: dict[str, int] = {}
    for template in templates:
        per_category[template["category"]] = per_category.get(template["category"], 0) + 1
        per_tone[template["tone"]] = per_tone.get(template["tone"], 0) + 1
    return {"total": len(templates), "by_category": dict(sorted(per_category.items())),
            "by_tone": dict(sorted(per_tone.items())),
            "unique_ids": len({item["id"] for item in templates}),
            "with_placeholders": sum(1 for item in templates if item["placeholders"])}


def render(template: Mapping[str, Any], values: Mapping[str, str], *, language: str) -> str:
    """Substitute declared placeholders; never leak a raw ``{...}`` to the player."""
    if language not in {"ru", "en", "fr"} or language not in template:
        raise TemplateError("unsupported_language")
    declared = set(template["placeholders"])
    missing = declared - set(values)
    if missing:
        raise TemplateError("missing_placeholder_values:" + ",".join(sorted(missing)))
    text = template[language]
    for name in declared:
        replacement = values[name]
        if not isinstance(replacement, str) or not replacement.strip():
            raise TemplateError("empty_placeholder_value:" + name)
        text = text.replace(name, replacement.strip())
    if re.search(r"\{[^}]*\}", text):
        raise TemplateError("unresolved_placeholder")
    return text
