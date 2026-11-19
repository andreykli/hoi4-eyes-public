"""Validation helpers for vanilla and modded Hearts of Iron IV country tags.

HOI4 tags are three ASCII alphanumeric characters. Dynamic and mod-added tags
commonly contain digits (for example ``D01``), so a letters-only validator
silently rejects valid countries and breaks country selection and agreements.
"""

from __future__ import annotations

import re
from typing import Any


COUNTRY_TAG_PATTERN = r"[A-Z0-9]{3}"
COUNTRY_TAG_RE = re.compile(rf"^{COUNTRY_TAG_PATTERN}$")


def is_country_tag(value: Any) -> bool:
    """Return whether a value is a three-character ASCII HOI4 tag."""
    return isinstance(value, str) and COUNTRY_TAG_RE.fullmatch(value.strip().upper()) is not None


def normalize_country_tag(value: Any, field_name: str = "country tag") -> str:
    """Normalize a valid tag to uppercase, raising for malformed values."""
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a three-character HOI4 country tag")
    tag = value.strip().upper()
    if not COUNTRY_TAG_RE.fullmatch(tag):
        raise ValueError(f"{field_name} must be a three-character HOI4 country tag")
    return tag
