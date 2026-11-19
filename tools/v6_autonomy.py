"""Bounded, deterministic initiative inbox, not a second agreement engine.

Only authenticated snapshot routes may feed this planner. Proposals carry no
consent or execution authority and are handed to the existing chat machinery.
"""
from copy import deepcopy
import hashlib
import json
import math
import re

from action_registry import ActionRegistry
from diplomatic_planner import build_strategic_plan
from diplomatic_doctrine import assess_doctrine
from strategic_interests import refresh_interests
from voices_adapter import schedule_reviews, finish_review
from voices_history.context import world_snapshot_block

TAG = re.compile(r'[A-Z][A-Z0-9]{2}')
CAMPAIGN = re.compile(r'[a-f0-9]{32}')
ACTIONS = {'non_aggression_pact', 'guarantee', 'military_access', 'embargo'}
FIELDS = {'war_status', 'same_faction', 'existing_non_aggression_pact', 'existing_guarantee',
          'existing_military_access', 'existing_embargo', 'relations', 'threat', 'military_balance',
          'economic_dependence', 'ideology_group', 'player_ideology_group', 'claims',
          'joint_embargo_target', 'embargo_allowed'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()[:32]


def pair_fingerprint(snapshot, actor, target, context):
    return digest({'context': context, 'actor': {k: snapshot['countries'][actor][k]
                   for k in ('ai_controlled', 'major_power', 'ideology_group')},
                   'target': {k: snapshot['countries'][target][k]
                   for k in ('ai_controlled', 'major_power', 'ideology_group')}})


def boolean(value):
    if isinstance(value, bool): return value
    return {'yes': True, 'no': False, 'true': True, 'false': False, 'unknown': None}.get(str(value).lower())


def empty():
    return {'schema_version': 1, 'campaigns': {}}


def normalize(value):
    if value is None: return empty()
    if not isinstance(value, dict) or value.get('schema_version') != 1 or not isinstance(value.get('campaigns'), dict):
        raise ValueError('invalid V6 autonomy state')
    result = empty()
    for campaign, state in value['campaigns'].items():
        if not isinstance(campaign, str) or not CAMPAIGN.fullmatch(campaign) or not isinstance(state, dict):
            continue
        snapshot = validate_snapshot(state['snapshot']) if state.get('snapshot') else None
        inbox = state.get('inbox', {})
        if not isinstance(inbox, dict): raise ValueError('invalid initiative inbox')
        valid = {}
        for key, proposal in inbox.items():
            if (isinstance(proposal, dict) and proposal.get('campaign_id') == campaign
                    and proposal.get('proposal_id') == key and isinstance(proposal.get('action_id'), str) and proposal.get('action_id') in ACTIONS
                    and isinstance(proposal.get('status'), str) and proposal.get('status') in {'queued', 'opened', 'stale', 'dismissed'}):
                item = deepcopy(proposal)
                item.update(binding=False, consent_status='unconfirmed', execution_state='not_prepared')
                valid[key] = item
        allow = state.get('historical_allowlist', [])
        result['campaigns'][campaign] = {'snapshot': snapshot, 'inbox': valid,
            'historical_allowlist': validate_allowlist(allow),
            'last_delivery': deepcopy(state.get('last_delivery'))}
        if snapshot and type(state.get('native_sequence')) is int and state['native_sequence'] == snapshot['sequence']:
            result['campaigns'][campaign]['native_sequence'] = snapshot['sequence']
    return result


def validate_allowlist(rows):
    if not isinstance(rows, list) or len(rows) > 32: raise ValueError('invalid historical policy')
    result = []
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {'actor', 'target', 'action_id'}
                or not isinstance(row['actor'], str) or not TAG.fullmatch(row['actor'])
                or not isinstance(row['target'], str) or not TAG.fullmatch(row['target'])
                or row['actor'] == row['target'] or not isinstance(row['action_id'], str) or row['action_id'] not in ACTIONS):
            raise ValueError('invalid historical policy rule')
        result.append(dict(row))
    return result


def validate_snapshot(data):
    required = {'sequence', 'day', 'player_country', 'historical_focuses', 'countries', 'pairs'}
    if not isinstance(data, dict) or set(data) != required: raise ValueError('invalid autonomy snapshot fields')
    if any(type(data[k]) is not int or data[k] < 0 for k in ('sequence', 'day')):
        raise ValueError('invalid snapshot clock')
    if data['historical_focuses'] is not None and type(data['historical_focuses']) is not bool:
        raise ValueError('historical setting must be Boolean or unknown')
    countries = data['countries']
    if not isinstance(countries, dict) or not 2 <= len(countries) <= 512: raise ValueError('invalid country roster')
    for tag, row in countries.items():
        if (not isinstance(tag, str) or not TAG.fullmatch(tag) or not isinstance(row, dict)
                or set(row) != {'ai_controlled', 'observed_day', 'major_power', 'ideology_group'}
                or type(row['ai_controlled']) is not bool or type(row['observed_day']) is not int
                or row['observed_day'] < 0 or row['observed_day'] > data['day']
                or (row['major_power'] is not None and type(row['major_power']) is not bool)
                or (row['ideology_group'] is not None and (not isinstance(row['ideology_group'], str) or len(row['ideology_group']) > 80))):
            raise ValueError('invalid country observation')
    player = data['player_country']
    if not isinstance(player, str) or player not in countries or countries[player]['ai_controlled']:
        raise ValueError('human observer must be explicit')
    if not isinstance(data['pairs'], list) or len(data['pairs']) > 2048: raise ValueError('invalid pair observations')
    seen = set()
    for pair in data['pairs']:
        if not isinstance(pair, dict) or set(pair) != {'actor', 'target', 'observed_day', 'context'}:
            raise ValueError('pair identity required')
        a, b = pair['actor'], pair['target']
        if not isinstance(a, str) or not isinstance(b, str) or a not in countries or b not in countries or a == b or (a, b) in seen:
            raise ValueError('invalid or duplicate directed pair')
        seen.add((a, b))
        if type(pair['observed_day']) is not int or not 0 <= pair['observed_day'] <= data['day']:
            raise ValueError('invalid pair date')
        context = pair['context']
        if not isinstance(context, dict) or not set(context) <= FIELDS: raise ValueError('unsupported pair field')
        for field in ('relations', 'threat', 'military_balance', 'economic_dependence'):
            if field in context and context[field] is not None:
                value = context[field]
                low, high = (-100, 100) if field == 'relations' else (-1, 1) if field == 'military_balance' else (0, 1)
                if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                    raise ValueError('invalid pair numeric fact')
        for field in ('war_status', 'same_faction', 'existing_non_aggression_pact', 'existing_guarantee',
                      'existing_military_access', 'existing_embargo', 'embargo_allowed'):
            if field in context and context[field] is not None and type(context[field]) is not bool:
                raise ValueError('pair flags must be explicit booleans')
        if 'joint_embargo_target' in context and (not isinstance(context['joint_embargo_target'], str)
                or context['joint_embargo_target'] not in set(countries) - {a, b}):
            raise ValueError('embargo needs a verified third country')
        for field, country in [('ideology_group', a), ('player_ideology_group', b)]:
            if field in context and context[field] != countries[country]['ideology_group']:
                raise ValueError('contradictory ideology observations')
        if 'claims' in context and (not isinstance(context['claims'], list) or len(context['claims']) > 64
                or any(type(v) is not int or v <= 0 for v in context['claims'])):
            raise ValueError('invalid dynamic state claims')
    directed = {(p['actor'], p['target']): p for p in data['pairs']}
    for (a, b), row in directed.items():
        reverse = directed.get((b, a))
        if not reverse or reverse['observed_day'] != row['observed_day']:
            continue
        for field in ('war_status', 'same_faction', 'existing_non_aggression_pact'):
            left, right = row['context'].get(field), reverse['context'].get(field)
            if left is not None and right is not None and left != right:
                raise ValueError('contradictory symmetric pair observations')
    return deepcopy(data)


def campaign_state(memory, campaign):
    if not isinstance(campaign, str) or not CAMPAIGN.fullmatch(campaign): raise ValueError('invalid campaign ID')
    return memory.setdefault('v6_autonomy', empty())['campaigns'].setdefault(campaign,
        {'snapshot': None, 'inbox': {}, 'historical_allowlist': []})


def status(memory, campaign):
    if not isinstance(campaign, str) or not CAMPAIGN.fullmatch(campaign): raise ValueError('invalid campaign ID')
    return deepcopy(memory.get('v6_autonomy', empty())['campaigns'].get(campaign,
        {'snapshot': None, 'inbox': {}, 'historical_allowlist': []}))


def record_snapshot(memory, campaign, data):
    snapshot = validate_snapshot(data)
    state = campaign_state(memory, campaign)
    prior = state['snapshot']
    if prior and snapshot['sequence'] <= prior['sequence']:
        if snapshot == prior: return {'recorded': True, 'idempotent': True}
        raise ValueError('stale_or_conflicting_snapshot_sequence')
    if prior and (snapshot['day'] < prior['day'] or snapshot['player_country'] != prior['player_country']):
        for item in state['inbox'].values():
            if item['status'] == 'queued': item['status'] = 'stale'
    for item in state['inbox'].values():
        if item['status'] != 'queued': continue
        pair = next((p for p in snapshot['pairs'] if p['actor'] == item['actor'] and p['target'] == item['target']), None)
        if not pair and item['actor'] in snapshot['countries'] and item['target'] in snapshot['countries'] and snapshot['day'] - item['day'] <= 14:
            continue  # A sparse observation omitted the pair; unknown is not a changed fact.
        if (not pair or snapshot['day'] - pair['observed_day'] > 7
                or pair_fingerprint(snapshot, item['actor'], item['target'], pair['context']) != item['context_digest']):
            item['status'] = 'stale'
    state['snapshot'] = snapshot
    return {'recorded': True, 'idempotent': False, 'day': snapshot['day']}


def policy(memory, campaign, allowlist):
    value = validate_allowlist(allowlist)
    campaign_state(memory, campaign)['historical_allowlist'] = value
    return {'historical_allowlist': value, 'automatic_game_execution': False}


def tick(engine, campaign):
    memory = engine.diplomatic_memory
    state = campaign_state(memory, campaign);snapshot = state['snapshot']
    blocked = lambda reason: {'status': 'blocked', 'reason': reason, 'game_effect_applied': False}
    if not engine.autonomous_initiatives_enabled: return blocked('autonomy_disabled')
    if not snapshot or snapshot['historical_focuses'] is None: return blocked('historical_or_snapshot_unknown')
    native = engine._latest_campaign_integration_snapshot(campaign, snapshot['player_country'])
    rules = native.get('game_rules', {}) if isinstance(native, dict) else {}
    if (rules.get('source') == 'authenticated_bridge_hoi4_historical_focuses_v1'
            and type(rules.get('historical_focuses_enabled')) is bool
            and rules['historical_focuses_enabled'] != snapshot['historical_focuses']):
        return blocked('historical_setting_contradicts_game_marker')
    if snapshot['historical_focuses'] and not state['historical_allowlist']: return blocked('historical_policy_not_configured')
    day, countries = snapshot['day'], snapshot['countries']
    for item in state['inbox'].values():
        if item['status'] == 'queued' and day - item['day'] > 14: item['status'] = 'stale'
    pending = [r for r in state['inbox'].values() if r['status'] == 'queued']
    if len(pending) >= 6: return blocked('initiative_inbox_full')
    world = {}
    for pair in snapshot['pairs']:
        actor = pair['actor'];ctx = pair['context']
        if (countries[actor]['ai_controlled'] and countries[actor]['observed_day'] == day
                and pair['observed_day'] == day and boolean(ctx.get('war_status')) is not None
                and boolean(ctx.get('same_faction')) is not None):
            world[actor] = {'day': day, 'verified': True, 'ai_controlled': True, 'context': ctx}
    if not world: return blocked('no_fresh_directed_pair_context')
    # A fixed alphabetical prefix permanently excluded mod-added countries.
    # Rotate the bounded work set by actual review history, not random tags.
    reviews = memory.get('strategic_reviews', {}).get(campaign, {}).get('reviews', [])
    last_review = {actor: max((r['day'] for r in reviews if r.get('actor') == actor), default=-1)
                   for actor in world}
    selected = sorted(world, key=lambda actor: (last_review[actor], actor))[:6]
    review = schedule_reviews(memory, campaign_id=campaign, player_tag=snapshot['player_country'],
        campaign_day=day, world=world, historical=snapshot['historical_focuses'], enabled=True,
        verified=True, countries=selected, historical_review_authorized=bool(state['historical_allowlist']))
    if review['status'] != 'prepared_review': return review
    reservation = review['review'];actor = reservation['actor'];candidates = []
    interest_context = {'campaign_day': day}
    if countries[actor]['major_power'] is not None: interest_context['major_power'] = countries[actor]['major_power']
    interests = refresh_interests(memory, campaign, actor, interest_context)
    for pair in snapshot['pairs']:
        if pair['actor'] != actor or pair['observed_day'] != day: continue
        target, context = pair['target'], pair['context']
        if countries[target]['observed_day'] != day: continue
        from diplomatic_memory import pair_summary
        memory_summary = pair_summary(memory, campaign_id=campaign, observer_tag=actor, counterpart_tag=target)
        partner_summary = pair_summary(memory, campaign_id=campaign, observer_tag=target, counterpart_tag=actor)
        refusal_unresolved = False
        for summary, directed in [(memory_summary, context), (partner_summary,
                next((p['context'] for p in snapshot['pairs'] if p['actor'] == target and p['target'] == actor and p['observed_day'] == day), {}))]:
            if summary.get('discussion_readiness', {}).get('state') == 'recent_refusal_recorded':
                old_relation = (summary.get('relationship_observation') or {}).get('value')
                new_relation = directed.get('relations')
                try: improved = float(new_relation) >= float(old_relation) + 20
                except (TypeError, ValueError): improved = False
                if not improved: refusal_unresolved = True
        if refusal_unresolved: continue
        possible = []
        if context.get('war_status') is False and context.get('existing_non_aggression_pact') is False and context.get('same_faction') is False:
            possible.append('non_aggression_pact')
        if context.get('war_status') is False and context.get('same_faction') is True:
            if context.get('existing_guarantee') is False: possible.append('guarantee')
            if context.get('existing_military_access') is False: possible.append('military_access')
        if context.get('joint_embargo_target') and context.get('embargo_allowed') is True and context.get('existing_embargo') is False:
            possible.append('embargo')
        for action in possible:
            identity = {'actor': actor, 'target': target, 'action_id': action}
            if snapshot['historical_focuses'] and identity not in state['historical_allowlist']: continue
            if any(chat.get('campaign_id') == campaign and set(chat.get('participants', [])) == {actor, target}
                    and chat.get('autonomous_origin', {}).get('action_id') == action and chat.get('state') != 'closed'
                    for chat in engine.chats.values()): continue
            if any(all(p.get(k) == v for k, v in identity.items()) and (
                    p['status'] == 'queued' or day - p['day'] < 30
                    or (p['status'] == 'opened' and engine.chats.get(p.get('chat_id'), {}).get('state') != 'closed'
                        and p['context_digest'] == pair_fingerprint(snapshot, actor, target, context)))
                    for p in state['inbox'].values()): continue
            if any(chat.get('campaign_id') == campaign and {actor, target}.issubset(chat.get('participants', []))
                    and any(agreement.get('status') in {'draft', 'awaiting_approval', 'approved'}
                            and agreement.get('action_id') == action for agreement in chat.get('agreements', {}).values())
                    for chat in engine.chats.values()): continue
            spec = ActionRegistry().get(action)
            strategy_context = {**context, '_bridge_verified': True,
                'ideology_group': countries[actor]['ideology_group'],
                'player_ideology_group': countries[target]['ideology_group'],
                'major_power': countries[actor]['major_power'], 'player_major_power': countries[target]['major_power']}
            doctrine = assess_doctrine(actor, strategy_context, target, '')
            relationship = {**context, 'promise_trust': memory_summary['promise_trust']['value']}
            plan = build_strategic_plan(intent=spec['intent'], engine_decision='defer', relationship=relationship,
                game_context=strategy_context, doctrine=doctrine, interests=interests, memory=memory_summary, campaign_id=campaign,
                conversation_id=f'{campaign}:initiative:{actor}:{target}', allow_override=True)
            # An initiative is proposed only when the actor's own policy supports it.
            from initiative_compromise import opening_terms
            compromise = opening_terms(action, plan, context)
            if compromise is False: continue
            if compromise:
                plan = {**plan, 'opening_compromise_terms': compromise, 'opening_compromise_authority': 'engine_nonbinding_proposal'}
            score = next(r['score'] for r in plan['candidate_scores'] if r['action'] == plan['selected_action'])
            candidates.append((score, action, target, plan, context))
    if not candidates:
        finish_review(memory, reservation, 'No supported initiative justified by current evidence.')
        return {'status': 'reviewed_no_initiative', 'actor': actor, 'game_effect_applied': False}
    score, action, target, plan, context = sorted(candidates, key=lambda c: (-c[0], c[1], c[2]))[0]
    proposal_id = digest([campaign, actor, target, action, day, reservation['signature']])
    proposal = {'proposal_id': proposal_id, 'campaign_id': campaign, 'actor': actor, 'target': target,
        'action_id': action, 'day': day, 'status': 'queued', 'plan': plan,
        'context_digest': pair_fingerprint(snapshot, actor, target, context), 'binding': False, 'consent_status': 'unconfirmed',
        'execution_state': 'not_prepared', 'game_effect_applied': False,
        'terms': [{'term_id': action, 'duration': None, 'target_country': context.get('joint_embargo_target') if action == 'embargo' else target}],
        'proposal_text': f'{actor} proposes {action.replace("_", " ")} with {target}' +
                         (f' against {context["joint_embargo_target"]}' if action == 'embargo' else '') + '.',
        'world_context': world_snapshot_block({t: {'tag': t, 'name': t, 'maj': '1' if c['major_power'] is True else 'unknown',
            'ideology_name': c['ideology_group'] or 'Unknown'} for t, c in countries.items() if c['observed_day'] == day})}
    state['inbox'][proposal_id] = proposal
    if plan.get('opening_compromise_terms'):
        proposal['terms'] = deepcopy(plan['opening_compromise_terms'])
        proposal['proposal_text'] = f"{actor} proposes a limited {proposal['terms'][0]['duration']} non-aggression pact with {target}."
    finish_review(memory, reservation, json.dumps({'proposal_id': proposal_id, 'action_id': action, 'score': score}))
    return {'status': 'initiative_queued', 'proposal': deepcopy(proposal), 'game_effect_applied': False}


def open_chat(engine, campaign, proposal_id):
    if not isinstance(proposal_id, str) or not re.fullmatch(r'[a-f0-9]{32}', proposal_id):
        raise ValueError('invalid initiative identity')
    state = campaign_state(engine.diplomatic_memory, campaign);proposal = state['inbox'].get(proposal_id)
    if proposal and proposal['status'] == 'stale': raise ValueError('initiative_context_changed_or_expired')
    if not proposal or proposal['status'] not in {'queued', 'opened'}: raise ValueError('initiative_unavailable')
    if proposal.get('chat_id'): return engine.get_chat(proposal['chat_id'])
    snapshot = state['snapshot'];player = snapshot['player_country']
    if proposal['target'] != player: raise ValueError('ai_to_ai_delivery_not_connected')
    if snapshot['day'] < proposal['day'] or snapshot['day'] - proposal['day'] > 14: raise ValueError('initiative_expired')
    pair = next((p for p in snapshot['pairs'] if p['actor'] == proposal['actor'] and p['target'] == player), None)
    if (pair and (pair['observed_day'] != snapshot['day']
            or snapshot['countries'][proposal['actor']]['observed_day'] != snapshot['day']
            or snapshot['countries'][player]['observed_day'] != snapshot['day'])):
        raise ValueError('initiative_context_not_fresh')
    if not pair or pair_fingerprint(snapshot, proposal['actor'], player, pair['context']) != proposal['context_digest']:
        raise ValueError('initiative_context_changed')
    chat = engine.create_chat(campaign, player, [proposal['actor']])
    stored = engine.chats[chat['chat_id']]
    context = pair['context']
    observation = {'date': str(snapshot['day']),
        **{k: ('yes' if context.get(k) is True else 'no' if context.get(k) is False else 'unknown')
           for k in ('war_status', 'same_faction', 'existing_non_aggression_pact')},
        'relations': str(context.get('relations', 'unknown')), 'threat': str(context.get('threat', 'unknown'))}
    engine.record_game_context_observation(chat['chat_id'], proposal['actor'], campaign,
        f'{campaign}:{snapshot["sequence"]:010d}', observation)
    if chat.get('response_language') == 'ru':
        actor = proposal['actor'];action = proposal['action_id']
        descriptions = {'non_aggression_pact': f'{actor} предлагает пакт о ненападении с {player}.',
            'guarantee': f'{actor} предлагает гарантировать независимость {player}.',
            'military_access': f'{actor} предлагает предоставить военный доступ {player}.',
            'embargo': f'{actor} предлагает {player} совместное эмбарго против {proposal["terms"][0]["target_country"]}.'}
        notice = descriptions[action] + ' Это неподтверждённое предложение. Обсудите точные условия; согласие и исполнение ещё не зафиксированы.'
    else:
        notice = proposal['proposal_text'] + ' This is an unconfirmed proposal. Discuss exact terms before any agreement.'
    stored['messages'].append({'speaker': proposal['actor'], 'kind': 'initiative_notice',
        'text': notice,
        'request_id': f'{campaign}:initiative-notice:{proposal_id}', 'binding': False})
    proposal.update(status='opened', chat_id=chat['chat_id'])
    return engine.get_chat(chat['chat_id'])
