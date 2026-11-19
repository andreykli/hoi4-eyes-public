"""A meeting roster derived from a complete native Atlas batch."""
from hashlib import sha256
import re
import unicodedata

ROSTER_ROLES = ('head', 'minister', 'general', 'marshal', 'admiral')
ROSTER_LIMIT = 768


def clean_name(value):
    if not isinstance(value, str):
        raise ValueError('invalid_cabinet_name')
    name = ' '.join(unicodedata.normalize('NFC', value).split())
    if (not 1 <= len(name) <= 150 or not any(ch.isalpha() for ch in name)
            or any(ch in '|[]$§' or unicodedata.category(ch).startswith('C') for ch in value)
            or name.casefold() in {'unknown','unknown leader','no leader','none','null','inconnu',
                                   'dirigeant inconnu','aucun dirigeant','неизвестно','нет лидера','неизвестный лидер'}
            or re.fullmatch(r'[A-Z0-9]{3}_[A-Za-z0-9_]+', name)):
        raise ValueError('invalid_cabinet_name')
    return name


def validate_roster(rows):
    if not isinstance(rows, list) or len(rows) > ROSTER_LIMIT:
        raise ValueError('invalid_cabinet_roster')
    people = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'role','name'} or row['role'] not in ROSTER_ROLES:
            raise ValueError('invalid_cabinet_member')
        people.append({'role':row['role'],'name':clean_name(row['name'])})
    return people


def choices(player, rows):
    from collections import Counter
    keys = [(r['role'], r['name'].casefold()) for r in rows]
    counts = Counter(keys)
    head_count = sum(key[0]=='head' for key in keys)
    return [
        {**row, 'id':sha256(f'{player}|{key[0]}|{key[1]}'.encode()).hexdigest()[:32],
         'country':player, 'basis':'native_roster'}
        for row,key in zip(rows,keys) if counts[key]==1 and (key[0]!='head' or head_count==1)
    ]


def script_rows():
    prefix = 'EYES_WORLD_V1|{kind}|' + '-'.join(
        f'[?global.eyes_campaign_seed_{index}|0]' for index in range(1,5)) + '|[?ROOT.eyes_world_sequence|0]'

    def emit(role, name):
        return (f'log = "{prefix.format(kind="CABINET")}|{role}|{name}" '
                'ROOT = { add_to_variable = { eyes_cabinet_rows = 1 } }')

    return f''' # Cabinet evidence belongs only to this authenticated Atlas batch.
 set_variable = {{ eyes_cabinet_rows = 0 }}
 {emit('head', '[ROOT.GetLeader]')}
 every_character = {{
  limit = {{ is_political_advisor = yes }}
  {emit('minister', '[THIS.GetName]')}
 }}
 every_army_leader = {{
  if = {{ limit = {{ is_field_marshal = yes }} {emit('marshal', '[THIS.GetName]')} }}
  else = {{ {emit('general', '[THIS.GetName]')} }}
 }}
 every_navy_leader = {{ {emit('admiral', '[THIS.GetName]')} }}
 log = "{prefix.format(kind='CABINET_END')}|[?ROOT.eyes_cabinet_rows|0]"
'''
