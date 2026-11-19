"""Preloaded HOI4 country creation decision. Installation and the game option are separate."""
import re
import uuid
import base64
import binascii
import struct
import json
from pathlib import Path
from zipfile import ZipFile

from hoi4_campaign_identity import SAVE_CAMPAIGN_TOKEN_RE
from validate_hoi4_gui import parse_pdx
from country_maker_design import compile_design

MARKER = 'EYES_COUNTRY_MAKER_V1|'
FLAG_SIZES = {'large': (82, 52), 'medium': (41, 26), 'small': (10, 7)}


def flag_files(namespace, pixels):
    """Encode bounded canvas pixels as the uncompressed 32-bit TGA used by HOI4."""
    if pixels is None:
        return {}
    if not isinstance(pixels, dict) or pixels.keys() != FLAG_SIZES.keys():
        raise ValueError('invalid_flag')
    files = {}
    for size, (width, height) in FLAG_SIZES.items():
        encoded = pixels[size]
        length = width * height * 4
        if not isinstance(encoded, str) or len(encoded) != 4 * ((length + 2) // 3):
            raise ValueError('invalid_flag')
        try:
            rgba = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError('invalid_flag') from error
        if len(rgba) != length:
            raise ValueError('invalid_flag')
        bgra = bytearray()
        for row in range(height - 1, -1, -1):
            start = row * width * 4
            for index in range(start, start + width * 4, 4):
                red, green, blue, _ = rgba[index:index+4]
                bgra.extend((blue, green, red, 255))
        header = struct.pack('<BBBHHBHHHHBB', 0, 0, 2, 0, 0, 0, 0, 0, width, height, 32, 0)
        folder = '' if size == 'large' else size + '/'
        files[f'gfx/flags/{folder}{namespace}.tga'] = header + bgra
    return files


TAG_DEFINITION = re.compile(r'^\s*([A-Z0-9]{3})\s*=\s*"', re.M)
TAG_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'


def installed_tags(game_root=None, documents=None):
    """Enumerate the base game and every enabled mod before allocating a static tag."""
    if game_root is None:
        from validate_hoi4_gui import discover_hoi4_install
        game_root = discover_hoi4_install()
    if not game_root or not (Path(game_root)/'common/country_tags').is_dir():
        raise ValueError('game_country_tags_unavailable')
    documents = Path(documents or Path.home()/'Documents/Paradox Interactive/Hearts of Iron IV')
    tags = set()

    def collect(folder):
        for path in (Path(folder)/'common/country_tags').rglob('*.txt'):
            tags.update(TAG_DEFINITION.findall(path.read_text(encoding='utf-8-sig',errors='replace')))

    collect(game_root)
    load_file = documents/'dlc_load.json'
    if not load_file.is_file():
        return tags
    for entry in json.loads(load_file.read_text(encoding='utf-8-sig')).get('enabled_mods', []):
        descriptor = (documents/entry).read_text(encoding='utf-8-sig')
        match = re.search(r'^\s*(path|archive)\s*=\s*"([^"]+)"', descriptor, re.M)
        if not match:
            raise ValueError('enabled_mod_path_unavailable')
        location = documents/match[2]
        if match[1]=='archive':
            with ZipFile(location) as archive:
                for name in archive.namelist():
                    if name.startswith('common/country_tags/') and name.endswith('.txt'):
                        tags.update(TAG_DEFINITION.findall(archive.read(name).decode('utf-8-sig',errors='replace')))
        elif location.is_dir():
            collect(location)
        else:
            raise ValueError('enabled_mod_path_unavailable')
    return tags


def allocate_tag(used, identifier):
    pool = [f'X{first}{second}' for first in TAG_ALPHABET for second in TAG_ALPHABET]
    start = int(identifier,16)%len(pool)
    for index in range(len(pool)):
        tag = pool[(start+index)%len(pool)]
        if tag not in used:
            return tag
    raise ValueError('country_tag_pool_exhausted')


def compile_country(plan, observation, save_token, request_id=None, *, reserved_tags=(), game_root=None, documents=None):
    actor = observation['player']
    states = plan.get('states')
    capital = plan.get('capital')
    name = plan.get('name')
    status = plan.get('status')
    seed = SAVE_CAMPAIGN_TOKEN_RE.fullmatch(save_token)
    if not seed or not re.fullmatch(r'[A-Z][A-Z0-9]{2}', actor):
        raise ValueError('invalid_save_identity')
    if not isinstance(name, str) or not name.strip() or len(name) > 120 or re.search(r'["\\\[\]\$§\x00-\x1f]', name):
        raise ValueError('invalid_name')
    if (not isinstance(states, list) or not states or len(states) > 20000
            or any(type(state) is not int or state <= 0 for state in states)
            or len(set(states)) != len(states) or type(capital) is not int or capital not in states):
        raise ValueError('invalid_states_or_capital')
    if status not in ('independent', 'dependent'):
        raise ValueError('invalid_country_status')
    if any(observation['states'].get(str(state)) != {'owner': actor, 'controller': actor} for state in states):
        raise ValueError('territory_not_owned_and_controlled')
    identifier = request_id or uuid.uuid4().hex
    if not re.fullmatch(r'[a-f0-9]{32}', identifier):
        raise ValueError('invalid_request')
    namespace = 'eyes_cm_' + identifier
    used = installed_tags(game_root,documents) | set(observation.get('countries', ())) | set(reserved_tags) | {actor}
    requested = plan.get('country_tag')
    if requested not in (None, ''):
        if not isinstance(requested, str) or not re.fullmatch(r'X[A-Z]{2}', requested):
            raise ValueError('invalid_requested_country_tag')
        if requested in used:
            raise ValueError('country_tag_already_reserved')
    tag = requested or allocate_tag(used,identifier)
    design_files, design_loc, design_effects, design_checks, design = compile_design(plan,tag,namespace,name.strip())
    color = ' '.join(str(int(design['color'][i:i+2],16)) for i in (1,3,5))
    handled = namespace + '_handled'
    no_old_cores = f'NOT = {{ any_state = {{ is_core_of = {tag} }} }}'
    checks = [
        f'tag = {actor}', 'is_ai = no', 'is_subject = no', 'has_war = no',
        f'NOT = {{ has_country_flag = {handled} }}',
        f'{tag} = {{ exists = no is_dynamic_country = no NOT = {{ any_owned_state = {{ always = yes }} }} }}',
        no_old_cores,
        f'check_variable = {{ var = global.num_days value = {observation["day"]} compare = greater_than_or_equals }}',
        f'check_variable = {{ var = global.num_days value = {observation["day"]+7} compare = less_than_or_equals }}',
        *(f'check_variable = {{ var = global.eyes_campaign_seed_{i} value = {value} compare = equals }}'
          for i, value in enumerate(seed.groups(), 1)),
        *(f'{state} = {{ is_owned_by = ROOT is_controlled_by = ROOT is_capital = no }}' for state in states),
    ]
    guard = '\n'.join(checks)
    selected = ' '.join(f'state = {state}' for state in states)
    log_prefix = f'{MARKER}{identifier}|{save_token}|{actor}|'
    stamp = '|[?global.num_days|0]|[GetDateText]'

    def log(status, result_tag='-'):
        return f'log = "{log_prefix}{status}|{result_tag}{stamp}"'

    added_cores = '\n'.join(f'{state} = {{ add_core_of = {tag} }}' for state in states)
    exact_cores = (
        ' '.join(f'{state} = {{ is_core_of = {tag} }}' for state in states)
        + f' NOT = {{ any_state = {{ is_core_of = {tag} NOT = {{ OR = {{ {selected} }} }} }} }}'
    )
    all_states = ' '.join(f'{state} = {{ is_owned_by = {tag} is_controlled_by = {tag} }}' for state in states)
    clean_territory = f'{tag} = {{ NOT = {{ any_owned_state = {{ NOT = {{ OR = {{ {selected} }} }} }} }} }}'
    dependent = status == 'dependent'
    release_effect = (f'release_autonomy = {{ target = {tag} autonomy_state = autonomy_puppet release_non_owned_controlled = no }}'
                      if dependent else f'release = {tag}')
    autonomy_check = f'{tag} = {{ '+('is_subject_of = ROOT has_autonomy_state = autonomy_puppet' if dependent else 'is_subject = no')+' }'
    restored = ' '.join(f'{state} = {{ is_owned_by = ROOT is_controlled_by = ROOT NOT = {{ is_core_of = {tag} }} }}'
                        for state in states)
    rollback = '\n'.join(f"""
    {state} = {{
     if = {{ limit = {{ is_owned_by = {tag} }} transfer_state_to = ROOT }}
     if = {{ limit = {{ is_owned_by = ROOT is_controlled_by = {tag} }} set_state_controller_to = ROOT }}
     if = {{ limit = {{ is_core_of = {tag} }} remove_core_of = {tag} }}
    }}""" for state in states)
    pending = f'{namespace}_awaiting_capital'
    valid_country = f'{tag} = {{ exists = yes {design_checks} }} {all_states} {exact_cores} {clean_territory} {autonomy_check}'
    correct_capital = f'{tag} = {{ capital_scope = {{ state = {capital} }} }}'
    later_guard = ' '.join(check for check in checks if 'eyes_campaign_seed_' in check)
    followup_guard = (
        f'tag = {actor} is_ai = no has_country_flag = {pending} {later_guard} {valid_country} '
        f'check_variable = {{ var = global.num_days value = {observation["day"]} compare = greater_than_or_equals }} '
        f'check_variable = {{ var = global.num_days value = {observation["day"]+8} compare = less_than_or_equals }}'
    )
    event = f"""
add_namespace = {namespace}
country_event = {{
 id = {namespace}.1
 title = {namespace}_title
 desc = {namespace}_desc
 is_triggered_only = yes
 option = {{
  name = {namespace}_confirm
  ai_chance = {{ factor = 0 }}
  if = {{
   limit = {{ {guard} }}
   set_country_flag = {handled}
   {added_cores}
   if = {{
    limit = {{ {exact_cores} }}
    {release_effect}
    if = {{
     limit = {{ {tag} = {{ exists = yes }} {all_states} {exact_cores} {clean_territory} {autonomy_check} }}
     {tag} = {{
      {design_effects}
      set_capital = {{ state = {capital} remember_old_capital = no }}
     }}
     set_country_flag = {pending}
     country_event = {{ id = {namespace}.2 hours = 1 }}
    }}
   }}
   if = {{
    limit = {{ NOT = {{ has_country_flag = {pending} }} }}
    {rollback}
    if = {{
     limit = {{ {restored} {no_old_cores} {tag} = {{ NOT = {{ any_owned_state = {{ always = yes }} }} }} }}
     {log('rolled_back')}
    }}
    else = {{ {log('rollback_failed')} }}
   }}
  }}
  else = {{ {log('blocked')} }}
 }}
 option = {{ name = {namespace}_cancel ai_chance = {{ factor = 100 }} }}
}}

country_event = {{
 id = {namespace}.2
 hidden = yes
 is_triggered_only = yes
 immediate = {{
  if = {{
   limit = {{ has_country_flag = {pending} }}
   if = {{
    limit = {{ {followup_guard} {valid_country} }}
    {tag} = {{ set_capital = {{ state = {capital} remember_old_capital = no }} }}
    country_event = {{ id = {namespace}.3 hours = 1 }}
   }}
   else = {{ clr_country_flag = {pending} {log('partial')} }}
  }}
 }}
}}
country_event = {{
 id = {namespace}.3
 hidden = yes
 is_triggered_only = yes
 immediate = {{
  if = {{
   limit = {{ has_country_flag = {pending} }}
   clr_country_flag = {pending}
   if = {{
    limit = {{ {followup_guard} {valid_country} {correct_capital} }}
    {log('applied',tag)}
    log = "EYES_COUNTRY_CAPITAL_V1|{identifier}|{tag}|{capital}|verified"
    set_variable = {{ eyes_world_next_day = 0 }}
    if = {{
     limit = {{ NOT = {{ has_global_flag = eyes_news_disabled }} }}
     news_event = {{ id = {namespace}.4 }}
    }}
   }}
   else = {{ {log('partial')} }}
  }}
 }}
}}
news_event = {{
 id = {namespace}.4
 title = {namespace}_news_title
 desc = {namespace}_news_desc
 picture = GFX_news_event_generic_sign_treaty1
 major = yes
 is_triggered_only = yes
 option = {{ name = {namespace}_news_ack if = {{ limit = {{ is_ai = no }} log = "EYES_HOI4_NEWS_ACK_V1|{namespace}.4" }} }}
}}
"""
    from treaty_decision_package import REVIEW_CATEGORY
    category = REVIEW_CATEGORY
    files = {
        f'events/{namespace}.txt': event,
        f'history/countries/{tag} - {namespace}.txt': f'capital = {capital}\n',
        **design_files,
        f'common/country_tags/{namespace}.txt': f'{tag} = "countries/{namespace}.txt"\n',
        f'common/countries/{namespace}.txt': (
            'graphical_culture = western_european_gfx\n'
            'graphical_culture_2d = western_european_2d\n'
            f'color = {{ {color} }}\n'
        ),
        f'common/decisions/{namespace}.txt': (
            f'{category} = {{ {namespace}_review = {{ cost = 0 visible = {{ {guard} }} '
            f'available = {{ {guard} }} ai_will_do = {{ factor = 0 }} '
            f'complete_effect = {{ country_event = {{ id = {namespace}.1 }} }} }} }}'
        ),
    }
    state_list = ', '.join(map(str, states))
    news = {
        'english': ('A new state is established', f'[{actor}.GetName] has established [{tag}.GetName].',
                    'Established as a subject.', 'Established as an independent state.', 'Noted'),
        'russian': ('Образовано новое государство', f'Страна [{actor}.GetName] учредила государство [{tag}.GetName].',
                    'Оно получило зависимый статус.', 'Оно получило независимость.', 'Принято к сведению'),
        'french': ('Un nouvel État voit le jour', f'[{actor}.GetName] a établi un nouvel État, [{tag}.GetName].',
                   'Le nouvel État est un État sujet.', 'Le nouvel État est indépendant.', 'Bien reçu'),
    }
    for language, labels in {
        'english': ('Country Maker', 'Review state creation', 'Create in HOI4', 'Cancel',
                    'Regions and cores', 'Capital', 'Status', 'Puppet', 'Independent'),
        'russian': ('Конструктор государств', 'Проверить создание страны', 'Создать в HOI4', 'Отмена',
                    'Области и cores', 'Столица', 'Статус', 'Марионетка', 'Независимое'),
        'french': ('Créateur de pays', 'Vérifier la création du pays', 'Créer dans HOI4', 'Annuler',
                   'Régions et territoires nationaux', 'Capitale', 'Statut', 'État fantoche', 'Indépendant'),
    }.items():
        _, review, confirm, cancel, regions, capital_label, status_label, puppet, independent = labels
        description = f'{regions}: {state_list}. {capital_label}: {capital}. {status_label}: {puppet if dependent else independent}.'
        headline, founded, subject, sovereign, ack = news[language]
        entries = {
            namespace + '_review': review, namespace + '_title': name.strip(),
            namespace + '_desc': description, namespace + '_confirm': confirm, namespace + '_cancel': cancel,
            tag: name.strip(), tag + '_DEF': name.strip(), tag + '_ADJ': name.strip(),
            namespace + '_news_title': headline,
            namespace + '_news_desc': f'{founded} {subject if dependent else sovereign}',
            namespace + '_news_ack': ack,
            **design_loc,
        }
        files[f'localisation/{language}/{namespace}_l_{language}.yml'] = (
            f'\ufeffl_{language}:\n' + ''.join(f' {key}:0 "{value}"\n' for key, value in entries.items())
        )
    for path, content in files.items():
        if path.endswith('.txt') and not path.startswith('common/countries/'):
            parse_pdx(content)
    flags=flag_files(tag, plan.get('flag_pixels'))
    files.update(flags)
    for path, data in flags.items():
        for ideology in ('democratic','fascism','communism','neutrality'):
            files[path.removesuffix('.tga')+'_'+ideology+'.tga']=data
    return {'id': identifier, 'files': files, 'name': name.strip(), 'states': states,
            'capital': capital, 'country_status': status, 'actor': actor, 'save_token': save_token, 'country_tag': tag, 'executor_version': 5, **design}
