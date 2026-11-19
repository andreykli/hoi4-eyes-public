"""Compile one native review for an approved bilateral package of relations."""
from copy import deepcopy
from hashlib import sha256
from agreement_manager import _append_effect_news
from guarded_treaty_effects import reciprocal_direction_unresolved
from hoi4_campaign_identity import campaign_id_from_save_token
from hoi4_windows_adapter import validate_package
from native_treaty_news import ACK_TEXT
from saved_campaign_binding import matches_for_native_package
from treaty_ai_execution import render
from treaty_decision_package import REVIEW_CATEGORY
from treaty_lifecycle import register
from validate_hoi4_gui import Block, parse_pdx


RELATIONS = {'non_aggression_pact', 'guarantee', 'military_access'}


def is_pending(agreement):
    return (agreement.get('status') == 'manual_required'
            and agreement.get('block_reason') == 'per_term_execution_required'
            and not agreement.get('term_executions'))


def _relation_step(engine, chat, agreement, term):
    # Each step goes through the same consent, source, native guards and
    # allowlist used by the existing per-term executor. Its event stays private.
    from diplomacy_engine import DiplomacyEngine
    term_id = term['term_id']
    temporary = object.__new__(DiplomacyEngine)
    temporary.action_registry = engine.action_registry
    temporary.chats = {chat['chat_id']: {**chat, 'agreements': {
        agreement['agreement_id']: deepcopy(agreement)}}}
    prepared = temporary.prepare_agreement_term_execution(
        chat['chat_id'], agreement['agreement_id'], term_id=term_id, request_id=f'bundle-review:{term_id}')
    record = prepared['prepared_term']
    package = record.get('execution_package')
    if not package:
        raise ValueError(f'{term_id}: {record.get("reason", "native action could not be prepared")}')
    validate_package(agreement['campaign_id'], record['execution_request_id'], package, player_approved=True)
    option = parse_pdx(package['event_file']).first('country_event').first('option')
    effects = [(key, value) for key, value in option.entries
               if key not in {'name', 'trigger', 'ai_chance', 'set_country_flag', 'if', 'else'}]
    pre = option.first('trigger')
    post = option.first('if').first('limit')
    if len(effects) != 1 or not pre.entries or not post.entries:
        raise ValueError(f'{term_id}: unexpected native action shape')
    return render(pre), render(Block(effects)), render(post), package['game_effect']


def compile_bundle(engine, chat, agreement, observation):
    from player_native_actions import delivery_guard, sent_marker

    version = agreement['current_version']
    actor = chat['player_country']
    parties = set(agreement['participants'])
    if (not is_pending(agreement) or agreement.get('approved_version') != version
            or agreement.get('approved_by') != actor or chat.get('chat_mode') == 'ai_to_ai'
            or agreement['campaign_id'] != chat['campaign_id'] or agreement['chat_id'] != chat['chat_id']
            or len(parties) != 2 or parties != set(chat['participants']) or actor not in parties):
        raise ValueError('An unchanged bilateral treaty with exact approval is required.')
    other, = parties - {actor}
    terms = agreement['versions'][version - 1]['terms']
    actions = [term.get('action_id') for term in terms]
    if not 2 <= len(terms) <= 3 or set(actions) - RELATIONS or len(set(actions)) != len(actions):
        raise ValueError('The combined review supports one each of pact, independence guarantee and military access. Other terms require separate review.')
    if agreement['action_id'] not in actions:
        raise ValueError('The lead action is missing from the treaty.')
    for term in terms:
        if set(term.get('required_participants', [])) != parties:
            raise ValueError('Every clause must have the same two signatories.')
        if reciprocal_direction_unresolved(term, chat):
            raise ValueError('A reciprocal guarantee or access needs two separately agreed directions. It cannot be reduced to a one-way clause.')
    if not observation or observation.get('campaign') != chat['campaign_id'] or observation.get('player') != actor:
        raise ValueError('Export a fresh Atlas snapshot from this campaign and player first.')
    token, first = observation.get('save_token'), observation.get('day')
    if (not matches_for_native_package(agreement['campaign_id'], token, observation.get('atlas_binding'))
            or type(first) is not int or not 0 <= first <= 4_000_000 - 7
            or not parties <= set(observation['snapshot']['countries'])):
        raise ValueError('The Atlas snapshot does not identify the approved treaty and both signatories.')
    steps = [_relation_step(engine, chat, agreement, term) for term in terms]
    registered = deepcopy(agreement)
    register(registered, version=version, request_id=f'player-delivery:v{version}')
    request = f'agreement:{agreement["agreement_id"]}:v{version}'
    ns = 'eyes_bundle_' + sha256(f'{agreement["campaign_id"]}|{request}'.encode()).hexdigest()[:12]
    action = agreement['action_id']
    marker = f'EYES_HOI4_ACTION_APPLIED_V2|{actor}|{other}|{request}|{action}'
    failed = marker.replace('ACTION_APPLIED_V2', 'ACTION_FAILED_V2')
    failed_pre = failed + '|precondition_changed'
    failed_post = failed + '|postcondition_failed'
    partial = failed + '|bundle_partial'
    final = (f'if = {{ limit = {{ {" ".join(post for _, _, post, _ in steps)} }} '
             f'set_variable = {{ eyes_world_next_day = 0 }} log = "{marker}" }} '
             f'else = {{ log = "{partial}" }}')
    for index, (_, effect, post, _) in reversed(list(enumerate(steps))):
        final = f'{effect} if = {{ limit = {{ {post} }} {final} }} else = {{ log = "{partial if index else failed_post}" }}'
    guard = (f'tag = {actor} {delivery_guard(token, first, first + 7)} '
             f'NOT = {{ has_country_flag = {ns}_handled }}')
    all_pre = ' '.join(pre for pre, _, _, _ in steps)
    source = f'''add_namespace = {ns}
country_event = {{
 id = {ns}.1 title = {ns}_title desc = {ns}_description is_triggered_only = yes
 trigger = {{ {guard} }}
 immediate = {{ if = {{ limit = {{ {guard} }} log = "{sent_marker({'result_marker':marker})}" }} }}
 option = {{ name = {ns}_confirm ai_chance = {{ factor = 0 }}
  if = {{ limit = {{ {guard} }}
   set_country_flag = {ns}_handled
   if = {{ limit = {{ {all_pre} }} {final} }}
   else = {{ log = "{failed_pre}" }}
  }}
 }}
 option = {{ name = {ns}_cancel }}
}}
'''
    # Reuse the world-news dispatcher, with text for the complete revision.
    source, _, _ = _append_effect_news(source, 'l_english:', ns, marker, actor, other)
    texts = {
        'english': ('Review joint treaty', 'Confirm all clauses', 'Not now', 'Treaty takes effect'),
        'russian': ('Проверить совместный договор', 'Подтвердить все пункты', 'Не сейчас', 'Договор вступил в силу'),
        'french': ('Examiner le traité conjoint', 'Confirmer toutes les clauses', 'Plus tard', 'Le traité entre en vigueur'),
    }
    files = {
        f'events/{ns}.txt': source,
        f'common/decisions/{ns}.txt': f'''{REVIEW_CATEGORY} = {{ {ns}_review = {{
 cost = 0 visible = {{ {guard} }} available = {{ {guard} }} ai_will_do = {{ factor = 0 }}
 complete_effect = {{ if = {{ limit = {{ {guard} }} country_event = {{ id = {ns}.1 }} }} }}
}} }}
''',
    }
    for folder, (review, confirm, cancel, headline) in texts.items():
        terms_label = ' / '.join(_clause_label(effect, folder) for _, _, _, effect in steps)
        files[f'localisation/{folder}/{ns}_l_{folder}.yml'] = (
            f'\ufeffl_{folder}:\n {ns}_title:0 "{review}: {actor} / {other}"\n'
            f' {ns}_review:0 "{review}: {actor} / {other} v{version}"\n'
            f' {ns}_description:0 "{terms_label}"\n {ns}_confirm:0 "{confirm}"\n {ns}_cancel:0 "{cancel}"\n'
            f' {ns}.2.t:0 "{actor} / {other}"\n'
            f' {ns}.2.d:0 "{headline}\\n\\n{terms_label}"\n {ns}.2.a:0 "{ACK_TEXT[folder]}"\n')
    for name, text in files.items():
        if name.endswith('.txt'):
            parse_pdx(text)
    term_ids = [term['term_id'] for term in terms]
    package = {'result_marker':marker, 'failure_marker':failed_pre, 'event_file':source,
               'event_id':f'{ns}.1', 'bundle_term_ids':term_ids}
    seed = campaign_id_from_save_token(token)
    packet = {'status':'PREPARED_NOT_INSTALLED', 'game_effect_applied':False, 'files':files,
              'action_id':action, 'chat_id':chat['chat_id'], 'campaign_id':agreement['campaign_id'],
              'agreement_id':agreement['agreement_id'], 'version':version, 'execution_request_id':request,
              'result_marker':marker, 'review_slot':{'seed':seed, 'pair':sorted(parties),
                                                    'from_day':first, 'through_day':first + 7}}
    delivery = {'packet':packet, 'version':version, 'first_day':first, 'last_day':first + 7,
                'native_seed_id':seed, 'sent_marker':sent_marker(package)}
    return package, registered['lifecycle'], delivery


def _clause_label(effect, folder):
    names = {
        'non_aggression_pact': ('Non-aggression pact', 'Пакт о ненападении', 'Pacte de non-agression'),
        'guarantee': ('Independence guarantee', 'Гарантия независимости', 'Garantie d’indépendance'),
        'military_access': ('Military access', 'Военный доступ', 'Accès militaire'),
    }
    action = effect['action_id']
    label = names[action][('english', 'russian', 'french').index(folder)]
    if action == 'non_aggression_pact':
        return label
    if action == 'guarantee':
        grantor, recipient = effect['guarantor'], effect['beneficiary']
    else:
        grantor, recipient = effect['grantor'], effect['recipient']
    return f'{label}: {grantor} → {recipient}'
