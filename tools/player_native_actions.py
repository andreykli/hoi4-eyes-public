"""Preload existing typed native effects with an exact human authorization."""
from copy import deepcopy
from agreement_manager import build_guarantee_event_package, build_military_access_event_package, build_embargo_event_package
from hoi4_windows_adapter import validate_package
from guarded_treaty_effects import EFFECTS as GUARDED_EFFECTS, NEW_ACTIONS, build_package as build_guarded_treaty
from saved_campaign_binding import matches_for_native_package
from hoi4_campaign_identity import SAVE_CAMPAIGN_TOKEN_RE
from validate_hoi4_gui import parse_pdx, Block

BUILDERS = {'guarantee': build_guarantee_event_package, 'military_access': build_military_access_event_package,
            'embargo': build_embargo_event_package, 'lift_embargo': build_embargo_event_package,
            **dict.fromkeys(GUARDED_EFFECTS, build_guarded_treaty)}
DELIVERY_ACTIONS = {'non_aggression_pact', *BUILDERS}
SENT_MARKER = 'EYES_HOI4_ACTION_SENT_V2|'


def preview_zip(agreement, chat, reason):
    """Export an agreed version without any files that HOI4 can execute."""
    import json
    from treaty_lifecycle import register
    player = chat.get('player_country')
    parties = agreement['participants']
    version = agreement['current_version']
    if (chat.get('chat_mode') == 'ai_to_ai' or agreement.get('approved_by') != player
            or agreement.get('approved_version') != version or player not in parties
            or len(set(parties)) != 2 or set(parties) != set(chat.get('participants', []))
            or agreement.get('chat_id') != chat.get('chat_id')
            or agreement.get('campaign_id') != chat.get('campaign_id')):
        raise ValueError('Exact pending human authorization required')
    package = agreement.get('execution_package') or {}
    validate_package(agreement['campaign_id'], agreement['execution_request_id'], package, player_approved=True)
    register(deepcopy(agreement), version=version, request_id=f'preview:v{version}')
    manifest = {'status': 'PREVIEW_ONLY', 'agreement_id': agreement['agreement_id'],
                'campaign_id': agreement['campaign_id'], 'version': version,
                'player': player, 'participants': parties, 'event_id': package['event_id'],
                'reason': reason, 'terms': agreement['versions'][version - 1]['terms']}
    return {'status': 'PREVIEW_ONLY', 'agreement_id': agreement['agreement_id'], 'version': version,
            'detail': reason, 'game_effect_applied': False,
            'files': {'agreement-preview.json': json.dumps(manifest, ensure_ascii=False, indent=2)}}


def sent_marker(package):
    return package['result_marker'].replace('EYES_HOI4_ACTION_APPLIED_V2|', SENT_MARKER, 1)


def log_on_open(tree, package):
    event = tree.first('country_event')
    gate = event.first('trigger')
    event.entries.append(('immediate', Block([('if', Block([
        ('limit', deepcopy(gate)), ('log', '"' + sent_marker(package) + '"'),
    ]))])))


def saved_packet(agreement):
    delivery = agreement.get('native_delivery') or {}
    packet = delivery.get('packet')
    if (agreement.get('status') == 'manual_required' and not agreement.get('execution_result')
            and delivery.get('version') == agreement.get('current_version')
            and isinstance(packet, dict) and packet.get('files')
            and packet.get('agreement_id') == agreement.get('agreement_id')
            and packet.get('result_marker') == (agreement.get('execution_package') or {}).get('result_marker')):
        return deepcopy(packet)
    return None


def installed_for_review(packet, mod_root):
    """Only the byte-for-byte approved game files identify this decision."""
    from pathlib import Path
    from treaty_package_install import validate_package
    root = Path(mod_root).resolve()
    files = validate_package(packet)['files']
    for name, text in files.items():
        file = (root/name).resolve()
        if not file.is_relative_to(root):
            return False
        try:
            if file.read_bytes() != text.encode('utf-8'):
                return False
        except OSError:
            return False
    return True


def confirmation_command(agreement, chat, observation, mod_root):
    """Return a manually pasted command for the exact frozen event on disk."""
    from hoi4_campaign_identity import campaign_id_from_save_token
    from pathlib import Path
    delivery = agreement.get('native_delivery') or {}
    packet = saved_packet(agreement)
    if (not packet or agreement.get('approved_version') != agreement.get('current_version')
            or agreement.get('approved_by') != chat.get('player_country')
            or agreement.get('campaign_id') != chat.get('campaign_id')
            or agreement.get('chat_id') != chat.get('chat_id')):
        raise ValueError('Prepare this exact game package first.')
    if (not observation or observation.get('campaign') != chat.get('campaign_id')
            or observation.get('player') != chat.get('player_country')
            or not observation.get('save_token')
            or campaign_id_from_save_token(observation['save_token']) != delivery.get('native_seed_id')):
        raise ValueError('The running save is not verified for this prepared package.')
    day = observation.get('day')
    if type(day) is not int or not delivery['first_day'] <= day <= delivery['last_day']:
        raise ValueError('The prepared package is outside its game-date window.')
    package = agreement['execution_package']
    if package.get('bundle_term_ids'):
        raise ValueError('Confirm this treaty through its installed HOI4 decision.')
    validate_package(agreement['campaign_id'], agreement['execution_request_id'], package, player_approved=True)
    relative = 'events/' + package['event_file_name']
    expected = packet['files'].get(relative)
    try:
        installed = (Path(mod_root) / relative).read_text(encoding='utf-8-sig')
    except OSError:
        installed = None
    if not expected or installed != expected.lstrip('\ufeff').replace('\r\n', '\n'):
        raise ValueError('Install the approved treaty after exiting HOI4, then reload the matching save.')
    return {'command': package['open_event_command'], 'status': 'SENT' if delivery.get('sent') else 'PREPARED'}


def recheck_on_confirm(tree, *, player, guard, namespace, package):
    """Re-evaluate the native apply option's triggers when its effects run."""
    event = tree.first('country_event')
    if not isinstance(event, Block):
        raise ValueError('generated confirmation event required')
    option = next(iter(event.values('option')), None)
    if not isinstance(option, Block) or not isinstance(option.first('trigger'), Block):
        raise ValueError('generated apply option requires native checks')
    attributes = {'name', 'trigger', 'ai_chance'}
    effects = [(key, value) for key, value in option.entries if key not in attributes]
    gate = option.first('trigger')
    option.entries[:] = [(key, value) for key, value in option.entries if key in attributes]
    option.entries.append(('if', Block([('limit', deepcopy(gate)), *effects])))
    handled = namespace + '_handled'
    scope = parse_pdx(f'tag = {player} {guard} NOT = {{ has_country_flag = {handled} }}')
    option.entries.append(('else_if', Block([('limit', scope), ('set_country_flag', handled),
                                            ('log', '"' + package['failure_marker'] + '"')])))


def compile_delivery(agreement, chat, observation):
    """Use only the current authenticated observation, never a browser-supplied seed."""
    from treaty_lifecycle import register
    if (not observation or observation.get('campaign') != chat.get('campaign_id')
            or observation.get('player') != chat.get('player_country')
            or not observation.get('save_token') or type(observation.get('day')) is not int):
        raise ValueError('Export a fresh Atlas snapshot from this campaign and player first.')
    if (chat.get('chat_mode') == 'ai_to_ai' or agreement.get('status') != 'manual_required'
            or agreement.get('campaign_id') != chat.get('campaign_id')
            or agreement.get('chat_id') != chat.get('chat_id')
            or agreement.get('approved_by') != chat.get('player_country')
            or not agreement.get('execution_package') or agreement.get('execution_result') is not None):
        raise ValueError('This version has no pending native action.')
    if not set(agreement['participants']) <= set(observation['snapshot']['countries']):
        raise ValueError('The current Atlas snapshot does not contain both signatories.')
    a = deepcopy(agreement)
    version = a['current_version']
    register(a, version=version, request_id=f'player-delivery:v{version}')
    day = observation['day']
    previous = a.get('native_delivery') or {}
    from hoi4_campaign_identity import campaign_id_from_save_token
    native_id = campaign_id_from_save_token(observation['save_token'])
    if previous.get('native_seed_id', native_id) != native_id:
        raise ValueError('The existing package belongs to another native save identity.')
    if previous.get('sent'):
        raise ValueError('HOI4 has already opened this package. Review the decision in the game.')
    if previous and (previous['version'] != version or not previous['first_day'] <= day <= previous['last_day']):
        raise ValueError('The existing package is expired or belongs to another version.')
    first_day = previous.get('first_day', day)
    last_day = previous.get('last_day', day + 7)
    kwargs = dict(save_token=observation['save_token'], not_after_day=last_day, not_before_day=first_day,
                  atlas_binding=observation.get('atlas_binding'))
    if a['action_id'] == 'non_aggression_pact':
        from treaty_decision_package import compile_decision
        packet = compile_decision(a, **kwargs)
    else:
        packet = compile_action(a, chat, **kwargs)
    a['native_delivery'] = previous or {
        'version': version, 'first_day': first_day, 'last_day': last_day,
        'native_seed_id': native_id,
        'sent_marker': sent_marker(a['execution_package']),
    }
    a['native_delivery']['packet'] = deepcopy(packet)
    return packet, a['lifecycle'], a['native_delivery']

def compile_action(a, chat, *, save_token, not_after_day, not_before_day=0, atlas_binding=None):
    a=deepcopy(a)
    if a.get('action_id') not in BUILDERS: raise ValueError('No native decision adapter for this action')
    if (a.get('campaign_id')!=chat.get('campaign_id') or a.get('chat_id')!=chat.get('chat_id')
        or chat.get('chat_mode')=='ai_to_ai' or a.get('approved_by')!=chat.get('player_country')
        or a.get('approved_version')!=a.get('current_version') or a.get('status')!='manual_required'
        or a.get('execution_result') is not None): raise ValueError('Exact pending human authorization required')
    if not matches_for_native_package(a['campaign_id'],save_token,atlas_binding): raise ValueError('wrong save identity')
    if type(not_after_day) is not int or type(not_before_day) is not int or not 0 <= not_before_day <= not_after_day <= 4_000_000:
        raise ValueError('absolute deadline required')
    version=a['versions'][a['current_version']-1]
    if len(version['terms'])!=1: raise ValueError('Use existing per-term pipeline for multi-effect agreements; no terms may be dropped')
    for t in version['terms']:
        spec = (t.get('proposal_spec') or {}).get('action') or {}
        if any(t.get(k) or spec.get(k) for k in ('conditions','exceptions','duration')) or t.get('unparsed_execution_details'):
            raise ValueError('Native relation cannot enforce custom conditions or duration')
        required=set(t.get('required_participants',[]))
        if len(required)!=2 or required!=set(a['participants']) or set(t.get('confirmed_by',[]))!=required:
            raise ValueError('Every required participant must explicitly accept')
        if any(not t.get('confirmed_by_request_ids',{}).get(tag) for tag in required): raise ValueError('Consent provenance missing')
    old=a.get('execution_package') or {};request=a.get('execution_request_id');a['status']='approved'
    package=BUILDERS[a['action_id']](chat,a,version,request)
    if (old.get('result_marker')!=package['result_marker'] or old.get('event_id')!=package['event_id']
            or old.get('game_effect')!=package['game_effect']):
        raise ValueError('Prepared action binding changed')
    validate_package(a['campaign_id'],request,package,player_approved=True)
    actor=chat['player_country'];namespace=package['event_id'].rsplit('.',1)[0]
    guard = delivery_guard(save_token, not_before_day, not_after_day)
    event=package['event_file'].replace(f'tag = {actor}',f'tag = {actor}\n{guard}')
    from treaty_ai_execution import render
    tree=parse_pdx(event)
    country_event=tree.first('country_event')
    if not isinstance(country_event,Block): raise ValueError('generated confirmation event required')
    recheck_on_confirm(tree, player=actor, guard=guard, namespace=namespace, package=package)
    log_on_open(tree, package)
    country_event.entries.append(('option',Block([('name',namespace+'.cancel')])))
    event=render(tree)
    condition=f'tag = {actor} {guard} NOT = {{ has_country_flag = {namespace}_handled }}'
    from treaty_decision_package import REVIEW_CATEGORY, native_news_locale_files
    category=REVIEW_CATEGORY
    files={f'events/{package["event_file_name"]}':event,
        f'localisation/english/{package["localisation_file_name"]}':'\ufeff'+package['localisation_file'].lstrip('\ufeff')+f'\n {namespace}_review:0 "Review {a["action_id"]}"\n {namespace}.cancel:0 "Not now; apply nothing"\n',
        f'common/decisions/{namespace}.txt':f'{category} = {{ {namespace}_review = {{ cost = 0 visible = {{ {condition} }} available = {{ {condition} }} ai_will_do = {{ factor = 0 }} complete_effect = {{ if = {{ limit = {{ {condition} }} country_event = {{ id = {package["event_id"]} }} }} }} }} }}'}
    files.update(native_news_locale_files(package))
    if a['action_id'] in NEW_ACTIONS:
        from game_actions import native_review_strings
        for code, folder in (('ru','russian'), ('fr','french')):
            name = package['localisation_file_name'].replace('_l_english.yml', f'_l_{folder}.yml')
            path = f'localisation/{folder}/{name}'
            contents = files.get(path, f'\ufeffl_{folder}:\n')
            files[path] = contents.rstrip() + '\n' + native_review_strings(package, code) + '\n'
    for name,text in files.items():
        if name.endswith('.txt'):parse_pdx(text)
    return {'status':'PREPARED_NOT_INSTALLED','game_effect_applied':False,'files':files,'action_id':a['action_id'],
        'chat_id':chat['chat_id'],'campaign_id':a['campaign_id'],'agreement_id':a['agreement_id'],
        'version':a['current_version'],'execution_request_id':request,
        'result_marker':package['result_marker'],'limitations':['requires startup with this packet','not newly verified in HOI4','no hot revocation']}


def delivery_guard(save_token, first_day, last_day):
    seed_checks = '\n'.join(f'check_variable = {{ var = global.eyes_campaign_seed_{i} value = {v} compare = equals }}'
                            for i, v in enumerate(SAVE_CAMPAIGN_TOKEN_RE.fullmatch(save_token).groups(), 1))
    return (f'{seed_checks}\nis_ai = no\n'
            f'check_variable = {{ var = global.num_days value = {first_day} compare = greater_than_or_equals }}\n'
            f'check_variable = {{ var = global.num_days value = {last_day} compare = less_than_or_equals }}')
