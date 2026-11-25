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
    try:current = CONFIG.read_text(encoding='utf-8-sig')
    except FileNotFoundError:current = ''
    if re.search(r'^\s*language\s*=.*$',current,re.M):
        current = re.sub(r'^\s*language\s*=.*$',lambda _:f'language = {value}',current,flags=re.M)
    else:current += ('\n' if current and not current.endswith('\n') else '')+f'language = {value}\n'
    temporary.write_text(current, encoding='utf-8')
    os.replace(temporary, CONFIG)


def tr(english, russian=None):
    if not isinstance(english,str):
        return tr('Information unavailable')
    selected = language()
    entry = CATALOGUE.get(english,{})
    fallback = english
    if re.fullmatch(r'[a-z][a-z0-9]*(?:[-_][a-z0-9]+)+',english):
        fallback = CATALOGUE.get('Information unavailable',{}).get(selected,'Information unavailable')
    if selected == 'ru':
        return russian or entry.get('ru', entry.get('en',fallback))
    return entry.get(selected, entry.get('en',fallback))


def translations(selected):
    return {english: values[selected] for english, values in CATALOGUE.items() if selected in values}
