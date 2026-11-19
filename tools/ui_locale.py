"""Shared UI language preference and translation catalogue."""
import json
import os
from pathlib import Path
import re
import uuid
from user_storage import profile_dir, legacy_import_allowed

ROOT = Path(__file__).resolve().parents[2]
CONFIG = profile_dir() / 'hoi4_eyes.ini'
LANGUAGES = ('en', 'ru', 'fr')
CATALOGUE = json.loads((Path(__file__).parent / 'ui_text.json').read_text(encoding='utf-8-sig'))


def language():
    try:
        path=CONFIG
        if not path.exists() and legacy_import_allowed():
            path=ROOT/'hoi4_eyes.ini'
        text = path.read_text(encoding='utf-8-sig')
    except OSError:
        return 'en'
    match = re.search(r'^\s*language\s*=\s*(en|ru|fr)\s*$', text, re.MULTILINE)
    return match[1] if match else 'en'


def save_language(value):
    if value not in LANGUAGES:
        raise ValueError('invalid_ui_language')
    CONFIG.parent.mkdir(parents=True,exist_ok=True)
    temporary = CONFIG.with_name(f'.{CONFIG.name}.{uuid.uuid4().hex}.tmp')
    temporary.write_text(f'language = {value}\n', encoding='utf-8')
    os.replace(temporary, CONFIG)


def tr(english, russian=None):
    selected = language()
    if selected == 'ru':
        return russian or CATALOGUE.get(english, {}).get('ru', english)
    return CATALOGUE.get(english, {}).get(selected, english)


def translations(selected):
    return {english: values[selected] for english, values in CATALOGUE.items() if selected in values}
