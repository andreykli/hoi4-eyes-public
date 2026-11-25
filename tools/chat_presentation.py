"""Localized chat labels only; country names never grant game authority."""
from pathlib import Path
from functools import lru_cache
import json
import re
import time
import zipfile
import io


def _localisation_texts(root, header):
    """Read directory and ZIP mods without extracting or executing their files."""
    languages = ('english','russian','french','german','polish','spanish','braz_por','japanese','korean','simp_chinese')
    def relevant(name):
        return not any(name.casefold().endswith('_l_'+lang+'.yml') for lang in languages if lang != header)
    def text(stream):
        with io.TextIOWrapper(stream,encoding='utf-8-sig') as reader:
            first = reader.read(4096)
            if re.search(r'^\s*l_'+header+r'\s*:',first,re.M):
                return first+reader.read(8*1024*1024)
    if root.is_dir():
        for file in sorted((root/'localisation').rglob('*.yml')):
            if relevant(file.name):
                try:
                    with file.open('rb') as stream:
                        value = text(stream)
                    if value is not None:yield value
                except (OSError,UnicodeError):
                    continue
    elif root.is_file():
        try:
            with zipfile.ZipFile(root) as archive:
                for member in sorted(archive.infolist(),key=lambda m:m.filename):
                    if (member.filename.startswith('localisation/') and member.filename.endswith('.yml')
                            and relevant(member.filename) and member.file_size <= 8*1024*1024):
                        try:
                            with archive.open(member) as stream:
                                value = text(stream)
                            if value is not None:yield value
                        except (OSError,UnicodeError,RuntimeError,zipfile.BadZipFile):
                            continue
        except (OSError,zipfile.BadZipFile):
            return


@lru_cache(maxsize=12)
def _names(locale, game_path, documents, revision, period):
    from hoi4_country_names import load_country_names, _game_roots
    from validate_hoi4_gui import parse_mod_descriptor
    result = dict(load_country_names(locale))
    roots = [p for p in _game_roots() if p.is_dir()][:1]
    docs = Path(documents)
    try:
        entries = json.loads((docs/'dlc_load.json').read_text(encoding='utf-8-sig')).get('enabled_mods', [])
        for entry in entries:
            d = parse_mod_descriptor((docs/entry).read_text(encoding='utf-8-sig'))
            source = d.first('path') or d.first('archive')
            if isinstance(source, str):
                roots.append(docs/source.strip('"'))
    except (OSError, ValueError, TypeError):
        pass
    header = {'en': 'english', 'ru': 'russian', 'fr': 'french'}[locale]
    pattern = re.compile(r'^\s*([A-Z][A-Z0-9]{2}(?:_[a-z_]+)?):\s*(?:\d+\s*)?"((?:\\.|[^"\\])*)"')
    for root in roots:
        for text in _localisation_texts(root, header):
            for line in text.splitlines():
                match = pattern.match(line)
                if match:
                    value = match[2].replace(r'\"', '"')
                    if value and not any(c in value for c in ('[', ']')):
                        result[match[1]] = re.sub(r'§.', '', value)
    for _ in range(6):
        for key,value in list(result.items()):
            result[key]=re.sub(r'\$([A-Za-z0-9_]+)\$',lambda m:result.get(m[1],m[0]),value)
    return {k:v for k,v in result.items() if '$' not in v}


def names(locale):
    from unified_mod_setup import documents_directory
    import os
    docs = documents_directory(); playset = docs/'dlc_load.json'
    return _names(locale, os.getenv('HOI4_GAME_PATH', ''), str(docs),
                  playset.stat().st_mtime_ns if playset.exists() else 0, int(time.monotonic()//60))


def decorate(chat, locale=None):
    locale = locale or chat.get('interface_language', 'en')
    locale = locale if locale in {'en', 'ru', 'fr'} else 'en'
    if chat.get('group_negotiation') or len(chat.get('participants',[]))>2:
        from group_negotiation import english_names
        labels = english_names()
    else:
        labels = names(locale)
    countries = {}
    for tag in dict.fromkeys([*chat.get('participants', []), *[m.get('speaker') for m in chat.get('messages',[]) if m.get('speaker')]]):
        observations=chat.get('game_context_observations',{}).get('by_country',{}).get(tag,[])
        context=observations[-1].get('context',{}) if observations else {}
        ideology=context.get('ideology') or context.get('ideology_group') or context.get('ruling_ideology')
        key=tag+'_'+str(ideology) if ideology in {'democratic','fascism','communism','neutrality'} else tag
        if chat.get('group_negotiation') or len(chat.get('participants',[]))>2:key=tag
        countries[tag]=labels.get(key,labels.get(tag,tag))
    joining = {'en': ' and ', 'ru': ' и ', 'fr': ' et '}[locale]
    values=[countries[tag] for tag in chat.get('participants',[])]
    participants = ', '.join(values[:-1])+joining+values[-1] if len(values)>2 else joining.join(values)
    title = {'en': participants+' Conversation', 'ru': 'Переговоры: '+participants,
             'fr': 'Conversation : '+participants}[locale]
    ref = chat.get('save_reference') or {}
    prefix = {'en': 'Save', 'ru': 'Сохранение', 'fr': 'Sauvegarde'}[locale]
    unknown = {'en': 'Not linked', 'ru': 'Не привязано', 'fr': 'Non liée'}[locale]
    return {**chat, 'display_title': title, 'country_labels': countries,
            'save_label': prefix+': '+(ref.get('name') or unknown)+((' — '+ref['date']) if ref.get('date') else '')}
