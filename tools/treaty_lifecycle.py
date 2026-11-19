"""Version-local obligations on existing agreements, never a second treaty store.

No negotiation text can activate this state. Exact prepared execution evidence
and campaign-correlated game observations are separate requirements.
"""
from copy import deepcopy
from datetime import date, timedelta
import calendar
import hashlib
import json
import re
from diplomatic_language import period

SCHEMA = 1
TYPES = {
    'non_aggression_pact': {'family': 'non_aggression', 'native_effect': 'diplomatic_relation', 'monitor': 'pact_presence'},
    'guarantee': {'family': 'guarantee', 'native_effect': 'give_guarantee', 'monitor': 'directed_guarantee'},
    'faction_join': {'family': 'alliance', 'native_effect': 'add_to_faction', 'monitor': 'faction_membership'},
    'military_access': {'family': 'military_access', 'native_effect': 'give_military_access', 'monitor': 'directed_access'},
    'territory_partition': {'family': 'territorial', 'native_effect': 'transfer_state_to', 'monitor': 'owner_and_controller'},
    'truce': {'family': 'ceasefire', 'native_effect': 'set_truce', 'monitor': 'war_and_truce'},
    'white_peace': {'family': 'peace_settlement', 'native_effect': 'white_peace', 'monitor': 'war_relation'},
}


def mechanisms():
    from action_registry import ActionRegistry
    registry = ActionRegistry()
    return {action: {**{k: v for k, v in spec.items() if k != 'negotiation'},
                     'action_registry': registry.get(action),
                     'lifecycle_monitor_implemented': action == 'non_aggression_pact',
                     'preloaded_human_decision_implemented': action == 'non_aggression_pact',
                     'preloaded_authorized_ai_execution_implemented': action == 'non_aggression_pact',
                     'runtime_delivery_supported': False,
                     'live_verified_by_this_stage': False} for action, spec in TYPES.items()}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def calendar_date(raw):
    if raw is None: return None
    if not isinstance(raw, str) or len(raw) > 100: raise ValueError('invalid calendar date')
    try: return date.fromisoformat(raw)
    except ValueError: pass
    months = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
    match = re.fullmatch(r'([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})', raw.strip())
    if match and match[1].lower() in months:
        try: return date(int(match[3]), months[match[1].lower()], int(match[2]))
        except ValueError: pass
    return None  # Unknown/localized date never means an invented January 1.


def due_day(start_day, start_date, duration):
    if not duration: return None
    amount, unit = duration['amount'], duration['unit']
    if type(amount) is not int or amount < 1 or unit not in {'day', 'month', 'year'}:
        raise ValueError('unsupported duration unit/amount')
    if unit == 'day': return start_day + amount
    anchor = calendar_date(start_date)
    if anchor is None: return None
    months = amount * 12 if unit == 'year' else amount
    month_index = anchor.year * 12 + anchor.month - 1 + months
    year, month = divmod(month_index, 12);month += 1
    try: target = date(year, month, min(anchor.day, calendar.monthrange(year, month)[1]))
    except (ValueError, OverflowError): return None
    return start_day + (target - anchor).days


def terms_projection(agreement):
    version = agreement['versions'][agreement['current_version'] - 1]
    projected = []
    for term in version['terms']:
        spec = term.get('proposal_spec') or {}
        action = spec.get('action') or {}
        duration = period(action.get('duration'))
        family = TYPES.get(term.get('action_id'), {}).get('family', 'unsupported')
        item = {'term_id': term['term_id'], 'family': family, 'content': term.get('text'),
            'participants': deepcopy(term.get('required_participants', agreement['participants'])),
            'duration': {'amount': duration[0], 'unit': duration[1]} if duration else None,
            'conditions': deepcopy(action.get('conditions', [])), 'exceptions': deepcopy(action.get('exceptions', [])),
            'implementation': 'not_verified', 'obligation': 'pending_game_evidence'}
        if term['term_id'] == 'advance_notice':
            clause = str(spec.get('term_clause') or '')
            days = re.search(r'\b(\d+)\s*[- ]?\s*days?\b', clause, re.I)
            scope = ('termination' if re.search(r'terminat|withdraw|cancel', clause, re.I)
                     else 'military_deployment' if re.search(r'deploy', clause, re.I) else 'unknown')
            item.update(family='advance_notice', notice_days=int(days[1]) if days else None,
                        notice_scope=scope, duration=None)
            if item['notice_days'] is not None and not 1 <= item['notice_days'] <= 3650:
                item['notice_days'] = None
        projected.append(item)
    return projected


def view(agreement):
    version = str(agreement['current_version'])
    stored = (agreement.get('lifecycle') or {}).get('versions', {}).get(version)
    return deepcopy(stored) if stored else {'schema_version': SCHEMA, 'registered': False,
        'agreement_id': agreement['agreement_id'], 'version': int(version),
        'participants': deepcopy(agreement['participants']), 'terms': terms_projection(agreement),
        'phase': 'not_registered', 'fully_verified': False}


def register(agreement, *, version, request_id):
    if type(version) is not int or version != agreement['current_version']:
        raise ValueError('stale treaty version')
    if not isinstance(request_id, str) or not request_id or len(request_id) > 250:
        raise ValueError('registration request_id required')
    if agreement.get('approved_version') != version or not agreement.get('approved_by'):
        raise ValueError('explicit human approval required')
    current = agreement['versions'][version - 1]
    if not current.get('terms') or any(not t.get('required_participants', agreement['participants'])
            or not set(t.get('required_participants', agreement['participants'])) <= set(agreement['participants'])
            or set(t.get('confirmed_by', [])) != set(t.get('required_participants', agreement['participants'])) for t in current['terms']):
        raise ValueError('all current terms require exact participant consent')
    root = agreement.setdefault('lifecycle', {'schema_version': SCHEMA, 'versions': {}, 'registrations': {}})
    binding = {'campaign_id': agreement['campaign_id'], 'chat_id': agreement['chat_id'],
        'agreement_id': agreement['agreement_id'], 'version': version,
        'participants': agreement['participants'], 'terms': terms_projection(agreement)}
    fingerprint = digest({**binding, 'terms': [{k: v for k, v in t.items() if k != 'content'} for t in binding['terms']]})
    prior_request = root['registrations'].get(request_id)
    if prior_request and prior_request != fingerprint: raise ValueError('conflicting registration replay')
    stored = root['versions'].get(str(version))
    if stored:
        if stored['fingerprint'] != fingerprint: raise ValueError('registered terms changed without version')
        root['registrations'][request_id] = fingerprint
        return deepcopy(stored)
    if any(v.get('start_day') is not None and v['phase'] not in {'termination_observed', 'breach_observed_unattributed'} for v in root['versions'].values()):
        raise ValueError('previous active treaty version must be resolved')
    stored = {**binding, 'schema_version': SCHEMA, 'registered': True, 'fingerprint': fingerprint,
        'monitor_id': fingerprint[:16], 'phase': 'awaiting_game_confirmation', 'fully_verified': False,
        'start_day': None, 'start_date': None, 'due_day': None, 'last_day': None, 'last_sequence': -1,
        'history': [], 'observations': {}, 'notices': [], 'termination_not_before': None}
    root['versions'][str(version)] = stored;root['registrations'][request_id] = fingerprint
    return deepcopy(stored)


def validate_observation(data):
    fields = {'campaign_id', 'chat_id', 'agreement_id', 'version', 'monitor_id', 'sequence',
              'day', 'application_day', 'pact_present', 'at_war', 'date'}
    if not isinstance(data, dict) or set(data) != fields: raise ValueError('invalid treaty observation fields')
    if any(not isinstance(data[k], str) or not re.fullmatch(r'[a-f0-9]{32}', data[k]) for k in ('campaign_id', 'chat_id', 'agreement_id')):
        raise ValueError('invalid treaty observation identity')
    if not isinstance(data['monitor_id'], str) or not re.fullmatch(r'[a-f0-9]{16}', data['monitor_id']): raise ValueError('invalid monitor id')
    if any(type(data[k]) is not int or data[k] < 0 for k in ('sequence', 'day', 'application_day')) or type(data['version']) is not int or data['version'] < 1:
        raise ValueError('invalid treaty clock/version')
    if data['application_day'] > data['day']: raise ValueError('application day is in the future')
    if any(type(data[k]) is not bool for k in ('pact_present', 'at_war')): raise ValueError('explicit game facts required')
    calendar_date(data['date'])
    return deepcopy(data)


def observe(agreement, data):
    data = validate_observation(data)
    life = view(agreement)
    if agreement.get('action_id') != 'non_aggression_pact':
        raise ValueError('this treaty family has no implemented lifecycle monitor')
    if (not life['registered'] or any(data[k] != agreement[k] for k in ('campaign_id', 'chat_id', 'agreement_id'))
            or data['version'] != agreement['current_version'] or data['monitor_id'] != life['monitor_id']):
        raise ValueError('observation belongs to another treaty/version/campaign')
    old = life['observations'].get(str(data['sequence']))
    if old:
        if old != digest(data): raise ValueError('conflicting observation replay')
        return {'recorded': True, 'duplicate': True, 'lifecycle': life}
    if data['sequence'] <= life['last_sequence'] or (life['last_day'] is not None and data['day'] < life['last_day']):
        raise ValueError('stale or regressed treaty timeline')
    if life['start_day'] is not None and data['application_day'] != life['start_day']:
        raise ValueError('application day changed')
    known_date = calendar_date(data['date'])
    if life['start_day'] is not None and known_date:
        inferred_start = (known_date - timedelta(days=data['day'] - life['start_day'])).isoformat()
        if life['start_date'] is not None and life['start_date'] != inferred_start:
            raise ValueError('calendar and game-day observations contradict')
        if life['start_date'] is None:
            life['start_date'] = inferred_start
            main = next((t for t in life['terms'] if t['family'] == 'non_aggression'), None)
            life['due_day'] = due_day(life['start_day'], inferred_start, main.get('duration')) if main else None
    if life['last_day'] is not None and data['day'] - life['last_day'] > 1:
        life['history'].append({'kind': 'monitoring_gap', 'from_day': life['last_day'], 'to_day': data['day']})
    proof = agreement.get('execution_result') or {}
    package = agreement.get('execution_package') or {}
    applied = (agreement.get('status') == 'execution_confirmed'
               and proof.get('source') == 'hoi4_game_log' and proof.get('status') == 'execution_confirmed'
               and proof.get('request_id') == agreement.get('execution_request_id')
               and proof.get('marker') == package.get('result_marker') and bool(package.get('result_marker'))
               and agreement.get('approved_version') == agreement['current_version'])
    if life['start_day'] is None and not applied:
        return {'recorded': False, 'pending': True, 'error': 'game_application_pending', 'lifecycle': view(agreement)}
    if applied and life['start_day'] is None:
        anchor = calendar_date(data['date'])
        life['start_day'] = data['application_day']
        life['start_date'] = (anchor - timedelta(days=data['day'] - data['application_day'])).isoformat() if anchor else None
        main = next((t for t in life['terms'] if t['family'] == 'non_aggression'), None)
        life['due_day'] = due_day(life['start_day'], life['start_date'], main.get('duration')) if main else None
        life['phase'] = 'active_monitored' if main and len(life['terms']) == 1 and not main.get('duration') else 'partially_verified'
        for term in life['terms']:
            if term['family'] == 'non_aggression':
                term.update(implementation='native_effect_marker_confirmed',
                    obligation='observed_active' if data['pact_present'] and not data['at_war'] else 'current_state_missing')
            elif ('no_territorial_changes' == term['term_id']
                  and term['term_id'] in package.get('game_effect', {}).get('enforced_term_ids', [])):
                term.update(implementation='native_effect_scope_confirmed', obligation='satisfied_on_application')
        if (main and not main.get('duration')
                and all(t is main or t['implementation'] == 'native_effect_scope_confirmed' for t in life['terms'])):
            life['phase'] = 'active_monitored'
        life['history'].append({'kind': 'game_application_observed', 'day': life['start_day'], 'marker': proof['marker']})
    terminal = {'termination_observed', 'breach_observed_unattributed', 'expiration_observed',
                'war_after_notice_requires_review', 'war_after_deadline_requires_review', 'possible_breach_requires_review'}
    if life['start_day'] is not None and life['phase'] not in terminal:
        if data['at_war']:
            if life['termination_not_before'] is not None and data['day'] >= life['termination_not_before']:
                life['phase'] = 'war_after_notice_requires_review'
            elif life['due_day'] is not None and data['day'] >= life['due_day']:
                life['phase'] = 'war_after_deadline_requires_review'
            elif any(t['conditions'] or t['exceptions'] for t in life['terms']):
                life['phase'] = 'possible_breach_requires_review'
            else:
                life['phase'] = 'breach_observed_unattributed'
            life['history'].append({'kind': 'war_observed', 'day': data['day'], 'responsible_country': None})
        elif not data['pact_present']:
            if life['termination_not_before'] is not None and data['day'] >= life['termination_not_before']:
                life['phase'] = 'termination_observed'
            elif life['due_day'] is not None and data['day'] >= life['due_day']:
                life['phase'] = 'expiration_observed'
            else:
                life['phase'] = 'relation_missing_unexplained'
        elif life['due_day'] is not None and data['day'] >= life['due_day']:
            life['phase'] = 'expiration_due_not_verified'
    # Never certify a custom expiry, notification or whole treaty from one
    # relation-presence bit. No game effects, trust awards or news here.
    life.update(last_day=data['day'], last_sequence=data['sequence'])
    life['observations'][str(data['sequence'])] = digest(data)
    life['history'].append({'kind': 'game_observation', 'day': data['day'], 'sequence': data['sequence'],
                            'pact_present': data['pact_present'], 'at_war': data['at_war']})
    agreement['lifecycle']['versions'][str(data['version'])] = life
    return {'recorded': True, 'duplicate': False, 'lifecycle': deepcopy(life)}


def record_notice(agreement, *, version, sender, day, request_id, confirm, scope):
    life = view(agreement)
    if not life['registered'] or version != life['version'] or type(version) is not int: raise ValueError('stale/unregistered notice')
    if confirm is not True or sender not in life['participants']: raise ValueError('explicit participant notice required')
    if type(day) is not int or scope not in {'termination', 'military_deployment'}: raise ValueError('typed notice scope/day required')
    if not isinstance(request_id, str) or not request_id or len(request_id) > 250: raise ValueError('notice request_id required')
    term = next((t for t in life['terms'] if t['family'] == 'advance_notice' and t['notice_scope'] == scope), None)
    if not term or not term.get('notice_days'): raise ValueError('notice scope/duration is not agreed')
    notice = {'request_id': request_id, 'sender': sender, 'recipients': [p for p in life['participants'] if p != sender],
              'day': day, 'scope': scope, 'earliest_action_day': day + term['notice_days'],
              'source': 'explicit_human_confirmation', 'game_action_applied': False}
    existing = next((n for n in life['notices'] if n['request_id'] == request_id), None)
    if existing:
        if existing != notice: raise ValueError('conflicting notice replay')
        return deepcopy(existing)
    if life['start_day'] is None or life['phase'] not in {'active_monitored', 'partially_verified', 'expiration_due_not_verified'}:
        raise ValueError('notice requires an active monitored obligation')
    if life['last_day'] is None or day != life['last_day']: raise ValueError('notice needs latest game day')
    life['notices'].append(notice)
    if scope == 'termination': life['termination_not_before'] = notice['earliest_action_day']
    life['history'].append({'kind': 'formal_notice', **notice})
    agreement['lifecycle']['versions'][str(version)] = life
    return deepcopy(notice)
