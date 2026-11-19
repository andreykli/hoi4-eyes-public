"""Preloaded execution of one explicitly authorized AI--AI native pact.

No live command transport, process access, installation, or new treaty store.
The immutable package carries Python's existing approved plan into game startup.
"""
from validate_hoi4_gui import Block, parse_pdx
from treaty_decision_package import native_news_locale_files, prepare_native_nap
from v6_autonomy import validate_snapshot


def render(block, indent=0):
    """Serialize the parsed generated subset, not arbitrary model output."""
    lines = []
    for key, value in block.entries:
        if isinstance(value, Block):
            lines.extend([' ' * indent + key + ' = {', render(value, indent + 2), ' ' * indent + '}'])
        else:
            lines.append(' ' * indent + key + ' = ' + value)
    return '\n'.join(lines)


def compile_ai_execution(agreement, chat, snapshot, *, save_token, not_after_day,
                         confirm_preapproved_execution=False):
    """Prepare a frozen AI--AI action, never grant approval or apply it.

    snapshot comes from the existing validated campaign observer store. An
    explicit compile-mode acknowledgement is required in addition to approval.
    """
    if confirm_preapproved_execution is not True:
        raise ValueError('explicit acknowledgement of preauthorized automatic execution required')
    snapshot = validate_snapshot(snapshot)
    if (chat.get('chat_id') != agreement.get('chat_id')
            or chat.get('campaign_id') != agreement.get('campaign_id')
            or chat.get('chat_mode') != 'ai_to_ai'):
        raise ValueError('matching AI--AI chat required')
    participants = agreement.get('participants')
    origin = chat.get('autonomous_origin') or {}
    observer = origin.get('human_observer')
    actor = chat.get('player_country')  # legacy initiator slot is AI in this chat mode
    if (not isinstance(participants, list) or len(participants) != 2
            or set(chat.get('participants', [])) != set(participants)
            or actor not in participants or observer in participants
            or origin.get('initiator_is_ai') is not True
            or agreement.get('approved_by') != observer
            or agreement.get('approval_authority') != 'human_observer'):
        raise ValueError('existing explicit human observer approval of this AI treaty required')
    if not isinstance(snapshot, dict) or snapshot.get('player_country') != observer:
        raise ValueError('current observer snapshot required')
    countries = snapshot.get('countries') or {}
    if (countries.get(observer, {}).get('ai_controlled') is not False
            or any(countries.get(tag, {}).get('ai_controlled') is not True for tag in participants)):
        raise ValueError('both signatories must be observed AI countries and observer must be human')
    day = snapshot.get('day')
    if (type(day) is not int or day < 0 or type(not_after_day) is not int
            or not day <= not_after_day <= day + 31
            or any(countries.get(tag, {}).get('observed_day') != day for tag in [observer, *participants])):
        raise ValueError('same-day country evidence and a maximum 31-day dispatch window required')
    if not isinstance(observer, str) or len(observer) != 3 or not observer.isascii() or not observer.isalnum() or observer != observer.upper():
        raise ValueError('invalid observer tag')
    a, monitor, campaign_guard = prepare_native_nap(agreement, actor=actor,
        save_token=save_token, not_after_day=not_after_day)
    package = monitor['event_package']
    namespace = package['event_id'].rsplit('.', 1)[0]
    target = next(tag for tag in participants if tag != actor)
    parsed = parse_pdx(package['event_file'])
    events = parsed.values('country_event')
    if len(events) != 1 or not isinstance(events[0], Block) or events[0].first('id') != package['event_id']:
        raise ValueError('unexpected generated event layout')
    option = events[0].first('option')
    if not isinstance(option, Block) or not isinstance(option.first('trigger'), Block):
        raise ValueError('generated effect lacks checked execution option')
    effects = Block([(k, v) for k, v in option.entries if k not in {'name', 'trigger'}])
    if sum(k == 'diplomatic_relation' for k, _ in effects.entries) != 1:
        raise ValueError('exactly one generated native relation required')
    once = f'{namespace}_handled'
    effect_name = f'{namespace}_apply_preapproved'
    identity = f'''tag = {actor}
{campaign_guard}
is_ai = yes
{observer} = {{ exists = yes is_ai = no }}
NOT = {{ has_country_flag = {once} }}'''
    deadline = f'check_variable = {{ var = global.num_days value = {not_after_day} compare = less_than_or_equals }}'
    after_start = f'check_variable = {{ var = global.num_days value = {day} compare = greater_than_or_equals }}'
    changed = package['failure_marker']
    # Expiry is a failed precondition, using the already saved canonical
    # marker so no unsigned alternative result is introduced by compilation.
    expired = changed
    # The original option includes native preconditions and success-triggered
    # application-day observation. Never convert a failed check into success.
    scripted = f'''{effect_name} = {{
 if = {{ limit = {{ {identity} {after_start} }}
  if = {{ limit = {{ {deadline} }}
   if = {{ limit = {{ {render(option.first('trigger'))} {target} = {{ is_ai = yes }} }}
    {render(effects)}
   }}
   else = {{ set_country_flag = {once} log = "{changed}" }}
  }}
  else = {{ set_country_flag = {once} log = "{expired}" }}
 }}
}}
'''
    dispatch = f'''# Preloaded, explicitly authorized action; not an inbound queue.
on_actions = {{ on_daily = {{ effect = {{
 if = {{ limit = {{ tag = {observer} is_ai = no {campaign_guard} }}
  {actor} = {{ {effect_name} = yes }}
 }}
}} }} }}
'''
    files = {
        f'common/scripted_effects/{namespace}_preapproved.txt': scripted,
        f'common/on_actions/{namespace}_dispatch.txt': dispatch,
        f'common/on_actions/{monitor["on_action_file_name"]}': monitor['on_action_file'],
    }
    news = parsed.values('news_event')
    if news:
        files[f'events/{package["event_file_name"]}'] = render(
            Block([('add_namespace', namespace), ('news_event', news[0])]),
        )
        files[f'localisation/english/{package["localisation_file_name"]}'] = (
            '\ufeff' + package['localisation_file'].lstrip('\ufeff')
        )
        files.update(native_news_locale_files(package))
    for name, content in files.items():
        if name.endswith('.txt'):
            parse_pdx(content)
    return {'status': 'PREPARED_NOT_INSTALLED', 'game_effect_applied': False,
            'delivery': 'preloaded_preapproved_ai_to_ai_on_daily',
            'campaign_id': a['campaign_id'], 'agreement_id': a['agreement_id'],
            'version': a['current_version'], 'execution_request_id': a['execution_request_id'],
            'participants': participants[:], 'human_observer': observer,
            'not_before_day': day, 'not_after_day': not_after_day,
            'monitor_id': monitor['monitor_id'], 'result_marker': package['result_marker'],
            'failure_marker': changed, 'files': files,
            'limitations': ['requires preload at game startup; no hot delivery',
                            'Python approval revocation after compilation cannot reach the frozen script',
                            'only plain native NAP; no custom duration, notice, war, or territorial changes',
                            'HOI4 engine execution and news display not yet verified']}
