"""Typed domestic and administrative state actions using the native EYES installer."""
from hashlib import sha256
import json
import re

from hoi4_campaign_identity import SAVE_CAMPAIGN_TOKEN_RE
from hoi4_country_tags import is_country_tag
from treaty_decision_package import REVIEW_CATEGORY

ACTIONS = {
    'move_capital': ('domestic', 'Move capital', 'Перенести столицу', 'Déplacer la capitale'),
    'add_core': ('administrative', 'Add national core', 'Добавить национальную область', 'Ajouter un territoire national'),
    'remove_core': ('administrative', 'Remove national core', 'Удалить национальную область', 'Retirer un territoire national'),
    'adjust_infrastructure': ('administrative', 'Set infrastructure level', 'Изменить уровень инфраструктуры', 'Modifier le niveau d’infrastructure'),
}
MARKER = 'EYES_STATE_OPERATION_V1|'
HEX_ID = re.compile(r'[0-9a-f]{32}')
OUTCOMES = {'verified', 'precondition_failed', 'postcondition_failed'}
WINDOW_DAYS = 7


def prepare(action, state, observation, previous_capital=None, current_level=None, level=None):
    if not isinstance(action, str) or action not in ACTIONS:
        raise ValueError('unsupported_state_action')
    if type(state) is not int or not 1 <= state <= 100_000:
        raise ValueError('invalid_state_id')
    if action == 'move_capital':
        if type(previous_capital) is not int or not 1 <= previous_capital <= 100_000 or previous_capital == state:
            raise ValueError('invalid_previous_capital')
    elif previous_capital is not None:
        raise ValueError('invalid_previous_capital')
    if action == 'adjust_infrastructure':
        if (type(current_level) is not int or type(level) is not int
                or not 0<=current_level<=5 or not 0<=level<=5 or abs(level-current_level)!=1):
            raise ValueError('invalid_infrastructure_levels')
    elif current_level is not None or level is not None:
        raise ValueError('unexpected_infrastructure_levels')
    campaign, actor, day, token = (observation[key] for key in ('campaign', 'player', 'day', 'save_token'))
    if (not HEX_ID.fullmatch(campaign) or not is_country_tag(actor)
            or type(day) is not int or not 0 <= day <= 4_000_000-WINDOW_DAYS):
        raise ValueError('invalid_atlas_observation')
    seed = SAVE_CAMPAIGN_TOKEN_RE.fullmatch(token)
    if not seed or observation.get('source') != campaign or not observation.get('proof'):
        raise ValueError('fresh_native_observation_required')
    states = observation['snapshot']['states']
    for selected in (state,) if previous_capital is None else (state, previous_capital):
        if states.get(str(selected)) != {'owner':actor, 'controller':actor}:
            raise ValueError('territory_not_owned_and_controlled')
    record = {'action':action, 'campaign':campaign, 'actor':actor, 'state':state,
              'previous_capital':previous_capital, 'current_level':current_level, 'level':level,
              'day':day, 'through_day':day+WINDOW_DAYS,
              'save_token':token, 'version':1}
    identity = sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()[:32]
    record.update(id=identity, status='prepared')
    ns = 'eyes_state_'+identity[:20]
    seed_guard = ' '.join(f'check_variable = {{ var = global.eyes_campaign_seed_{i} value = {number} compare = equals }}'
                          for i,number in enumerate(seed.groups(), 1))
    scope = (f'tag = {actor} is_ai = no {seed_guard} '
             f'check_variable = {{ var = global.num_days value = {day} compare = greater_than_or_equals }} '
             f'check_variable = {{ var = global.num_days value = {day+WINDOW_DAYS} compare = less_than_or_equals }} '
             f'NOT = {{ has_country_flag = {ns}_consumed }}')
    control = 'is_owned_by = ROOT is_fully_controlled_by = ROOT'
    state_scope = f'{state} = {{ {control} }}'
    if action == 'move_capital':
        effect = f'set_capital = {{ state = {state} remember_old_capital = no }}'
        before = f'{previous_capital} = {{ {control} is_capital = yes }} {state} = {{ is_capital = no is_core_of = ROOT }}'
        after = f'{state} = {{ is_capital = yes is_core_of = ROOT }} {previous_capital} = {{ is_capital = no }}'
    elif action == 'adjust_infrastructure':
        effect = f'{state} = {{ set_building_level = {{ type = infrastructure level = {level} }} }}'
        def level_check(value):
            return (f'{state} = {{ is_core_of = ROOT '
                    f'check_variable = {{ var = building_level@infrastructure value = {value} compare = equals }} '
                    'check_variable = { var = damaged_building_level@infrastructure value = 0 compare = equals } }')
        before,after = level_check(current_level),level_check(level)
    else:
        have_core = 'is_core_of = ROOT'
        no_core = f'NOT = {{ {have_core} }}'
        adding = action == 'add_core'
        effect = f'{"add_state_core" if adding else "remove_state_core"} = {state}'
        before = f'{state} = {{ is_capital = no {no_core if adding else have_core} }}'
        after = f'{state} = {{ {have_core if adding else no_core} }}'
    prefix = f'{MARKER}{identity}|{token}|{actor}|{action}|{state}|'
    def receipt(status):
        return f'log = "{prefix}{status}|[?global.num_days]"'
    script = f'''add_namespace = {ns}
country_event = {{
 id = {ns}.1 title = {ns}.1.t desc = {ns}.1.d is_triggered_only = yes
 trigger = {{ {scope} }}
 option = {{
  name = {ns}.1.a
  if = {{
   limit = {{ {scope} }}
   set_country_flag = {ns}_consumed
   if = {{
    limit = {{ has_war = no {state_scope} {before} }}
    {effect}
    if = {{ limit = {{ {state_scope} {after} }} {receipt('verified')} }}
    else = {{ {receipt('postcondition_failed')} }}
   }}
   else = {{ {receipt('precondition_failed')} }}
  }}
 }}
 option = {{ name = {ns}.1.b }}
}}
'''
    decisions = (f'{REVIEW_CATEGORY} = {{ {ns}_review = {{ cost = 0 visible = {{ {scope} }} '
                 f'available = {{ {scope} }} ai_will_do = {{ factor = 0 }} '
                 f'complete_effect = {{ if = {{ limit = {{ {scope} }} country_event = {{ id = {ns}.1 }} }} }} }} }}')
    files = {f'events/{ns}.txt':script, f'common/decisions/{ns}.txt':decisions}
    for index,folder,message,apply,cancel in (
        (1,'english', 'The game will recheck ownership, control and the starting conditions.', 'Apply action', 'Cancel'),
        (2,'russian', 'Игра повторно проверит владельца, контролёра и исходные условия.', 'Применить', 'Отмена'),
        (3,'french', 'Le jeu vérifiera à nouveau le propriétaire, le contrôle et les conditions initiales.', 'Appliquer', 'Annuler'),
    ):
        title = ACTIONS[action][index]
        subject = f'{actor} · #{state}' if previous_capital is None else f'{actor} · #{previous_capital} → #{state}'
        if action == 'adjust_infrastructure':
            subject += f' · {current_level} → {level}'
        files[f'localisation/{folder}/{ns}_l_{folder}.yml'] = (f'\ufeffl_{folder}:\n {ns}_review:0 "{title}"\n'
            f' {ns}.1.t:0 "{title}"\n {ns}.1.d:0 "{subject}. {message}"\n {ns}.1.a:0 "{apply}"\n {ns}.1.b:0 "{cancel}"\n')
    return record, {'status':'PREPARED_NOT_INSTALLED', 'game_effect_applied':False, 'campaign_id':campaign,
                    'agreement_id':identity, 'execution_request_id':f'state:{identity}:v1',
                    'version':1, 'action_id':action, 'files':files, 'result_marker':prefix}


def decode_result(raw):
    identifier, token, actor, action, state, outcome, day = raw.split('|')
    if (not HEX_ID.fullmatch(identifier) or not SAVE_CAMPAIGN_TOKEN_RE.fullmatch(token)
            or not is_country_tag(actor) or action not in ACTIONS or outcome not in OUTCOMES
            or not re.fullmatch(r'[1-9][0-9]{0,5}',state) or not re.fullmatch(r'[0-9]{1,7}',day)):
        raise ValueError('invalid_state_operation_result')
    return {'id':identifier,'campaign_token':token,'actor':actor,'action':action,
            'state':int(state),'status':outcome,'day':int(day)}
