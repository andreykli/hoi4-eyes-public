"""Read a small cached alias index from installed HOI4 country localisation."""

from __future__ import annotations

import os
from pathlib import Path
import re
from functools import lru_cache
from hoi4_country_tags import COUNTRY_TAG_PATTERN
from language_detection import HOI_LANGUAGE_LOCALES


_BASE_NAME = re.compile(rf'^\s*({COUNTRY_TAG_PATTERN}):\s*"((?:\\.|[^"\\])*)"')
_ADJECTIVE_NAME = re.compile(
    rf'^\s*({COUNTRY_TAG_PATTERN})(?:_[A-Za-z0-9]+)*_ADJ:\s*"((?:\\.|[^"\\])*)"'
)
_LANG_FILES = {
    language: f"{locale[2:]}/countries_{locale}.yml"
    for locale, language in HOI_LANGUAGE_LOCALES.items()
}
# Ukrainian is an EYES chat extension, but HOI4 1.19.3 does not ship a
# Ukrainian localization directory; use English unless a mod-specific label
# was supplied by the caller.
_LANG_FILES["uk"] = _LANG_FILES["en"]

# Common country-name forms are not always present in HOI4's base country
# localisation (for example the Russian USA key is simply "Америка"). Keep
# these as complete names only; short/ambiguous fragments would cause false
# country matches in model-authored text.
_EXTRA_COUNTRY_ALIASES = {
    "USA": (
        "США", "Соединённые Штаты", "Соединенные Штаты",
        "Соединённых Штатов", "Соединенных Штатов",
        "Соединённым Штатам", "Соединенным Штатам",
        "Соединёнными Штатами", "Соединенными Штатами",
        "Соединённые Штаты Америки", "Соединенные Штаты Америки",
        "Объединённые Штаты", "Объединенные Штаты",
        "Объединённые Штаты Америки", "Объединенные Штаты Америки",
        "United States", "United States of America",
        "États-Unis", "États Unis d'Amérique",
        "Сполучені Штати", "Сполучені Штати Америки",
    ),
}


def _game_roots() -> list[Path]:
    from validate_hoi4_gui import discover_hoi4_install
    discovered=discover_hoi4_install()
    roots = [discovered] if discovered else []
    configured = os.getenv("HOI4_GAME_PATH")
    if configured:
        roots.append(Path(configured))
    roots.extend([
        Path(r"D:\SteamLibrary\steamapps\common\Hearts of Iron IV"),
        Path(r"C:\Program Files (x86)\Steam\steamapps\common\Hearts of Iron IV"),
    ])
    return list(dict.fromkeys(roots))


@lru_cache(maxsize=10)
def load_country_names(language: str = "en") -> dict[str, str]:
    """Load only verified country names, excluding adjectives and variants."""
    relative = _LANG_FILES.get(language, _LANG_FILES["en"])
    for root in _game_roots():
        path = root / "localisation" / relative
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError:
            continue
        names = {}
        for line in lines:
            match = _BASE_NAME.match(line)
            if not match:
                continue
            tag, name = match.group(1), match.group(2).replace(r'\"', '"').strip()
            if name and not name.startswith("$"):
                names.setdefault(tag, name)
        if names:
            return names
    return {}


@lru_cache(maxsize=4)
def load_country_name_aliases(language: str = "en") -> dict[str, tuple[str, ...]]:
    """Return tag -> verified localized names without loading the game process.

    Only base three-character country localization keys are read. Ideology variants,
    adjectives, and dynamic runtime names are deliberately omitted.
    """
    relative = _LANG_FILES.get(language, _LANG_FILES["en"])
    aliases: dict[str, list[str]] = {}
    for root in _game_roots():
        path = root / "localisation" / relative
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            match = _BASE_NAME.match(line)
            if not match:
                match = _ADJECTIVE_NAME.match(line)
            if not match:
                continue
            tag, name = match.group(1), match.group(2).replace(r"\"", '"').strip()
            if name and not name.startswith("$"):
                aliases.setdefault(tag, []).append(name)
        if aliases:
            break
    # Add the base names from the other supported language files so text that
    # switches language mid-conversation can still be resolved to the same tag.
    if language in _LANG_FILES:
        for other_relative in set(_LANG_FILES.values()):
            for root in _game_roots():
                path = root / "localisation" / other_relative
                if not path.is_file():
                    continue
                try:
                    lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
                except OSError:
                    continue
                for line in lines:
                    match = _BASE_NAME.match(line)
                    if not match:
                        match = _ADJECTIVE_NAME.match(line)
                    if match:
                        tag, name = match.group(1), match.group(2).strip()
                        if name and not name.startswith("$"):
                            aliases.setdefault(tag, []).append(name)
                break
    for tag, names in _EXTRA_COUNTRY_ALIASES.items():
        if tag in aliases:
            aliases[tag].extend(names)
    return {tag: tuple(dict.fromkeys(names)) for tag, names in aliases.items()}


__all__ = ["load_country_names", "load_country_name_aliases"]
