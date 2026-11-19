"""Typed initiator continuation; consumes saved engine policies, never model prose."""
from copy import deepcopy
import re
from diplomatic_language import canonical_period, period
from diplomatic_planner import build_point_decisions, render_point_policy


def _parties_match(value, expected):
    return (isinstance(value, list) and len(value) == len(expected)
            and all(isinstance(tag, str) for tag in value)
            and {tag.upper() for tag in value} == expected)


def continuation_points(chat, agreement):
    version = agreement['versions'][-1]
    parties = set(agreement['participants'])
    terms = version['terms']
    if not 1 <= len(terms) <= 2:
        return None
    points = []
    for term in terms:
        spec = term.get('proposal_spec')
        if term.get('term_id') == 'non_aggression_pact':
            action = spec.get('action', {}) if isinstance(spec, dict) else {}
            if not isinstance(action, dict):
                return None
            if spec is not None and (not isinstance(spec, dict) or spec.get('term_clause')
                    or set(spec) - {'action', 'term_clause'}
                    or set(action) - {'action_id', 'parties', 'duration', 'conditions', 'exceptions',
                                      'actor_country_tag', 'actor_country_tags', 'recipient_country_tag', 'target_country_tag'}
                    or action.get('action_id') != 'non_aggression_pact'
                    or not _parties_match(action.get('parties'), parties)
                    or any(action.get(k) for k in ('conditions', 'exceptions', 'recipient_country_tag', 'target_country_tag'))):
                return None
            duration = action.get('duration')
            if duration is not None and (not period(duration) or period(duration)[1] != 'year' or not 1 <= period(duration)[0] <= 10):
                return None
            points.append({'action_id': 'non_aggression_pact', 'duration': canonical_period(duration),
                           'conditions': [], 'exceptions': [], 'speaking_role': 'party'})
        elif term.get('term_id') == 'advance_notice' and isinstance(spec, dict):
            clause = spec.get('term_clause')
            # Narrow recognized commitment. Other notification wording must
            # be clarified, not assumed to mean this exact bilateral promise.
            match = re.fullmatch(r'(?:mutual\s+)?(?:written\s+)?(?:advance\s+)?notice\s+of\s+(\d+)\s+days?\s+'
                r'before\s+military\s+deployments\s+across\s+the\s+shared\s+border[.]?', str(clause), re.I)
            if (not match or not 1 <= int(match[1]) <= 365 or set(spec) - {'action_id', 'parties', 'term_clause'}
                    or not _parties_match(spec.get('parties'), parties)):
                return None
            points.append({'action_id': 'non_aggression_pact', 'term_id': 'advance_notice',
                'term_clause': clause, 'conditions': [], 'exceptions': [], 'speaking_role': 'party'})
        else:
            return None
    if sum(not p.get('term_id') for p in points) != 1:
        return None
    latest = next((m for m in reversed(chat['messages']) if m.get('kind') == 'country_response'), None)
    if latest:
        policy = chat.get('engine_point_policies', {}).get(latest['request_id'], [])
        if not isinstance(policy, list) or (latest.get('decision') == 'counter_offer' and not policy):
            return None
        # Counters come exclusively from the durable Python policy, not from
        # parsing a three-year claim in the expression or a diagnostic reply.
        for row in policy:
            if row.get('authority') != 'engine_point_policy' or row.get('policy_request_id') != latest['request_id']:
                return None
            if row.get('term_id') or row.get('stance') != 'counter_offer':
                continue
            current = next(p for p in points if not p.get('term_id'))
            years = row.get('proposed_duration_years')
            if (row.get('action_id') != current['action_id']
                    or canonical_period(row.get('duration')) != current.get('duration')
                    or not _parties_match(row.get('party_tags'), parties)
                    or row.get('conditions') or row.get('exceptions') or row.get('unresolved_fields')
                    or type(years) is not int or not period(current.get('duration'))
                    or not 1 <= years < period(current['duration'])[0]):
                return None
            current['duration'] = canonical_period(f'{years} years')
            current['counter_source_request_id'] = latest['request_id']
    return points


def render_continuation(points, strategy, actor, target, previous_positions=()):
    positions = build_point_decisions(deepcopy(points), strategy, previous_positions)
    if not positions or any(p['stance'] in {'clarify', 'discuss'} for p in positions):
        return {'status': 'initiator_clarification_required', 'positions': positions, 'message': None}
    if all(p['stance'] == 'accept' for p in positions):
        core = next(p for p in positions if not p.get('term_id'))
        duration = str(core.get('duration') or '').replace(' years', '-year').replace(' year', '-year')
        subjects = [f'a {duration + " " if duration else ""}non-aggression pact with {target}']
        subjects.extend('mutual ' + p['term_clause'] if not p['term_clause'].lower().startswith('mutual ') else p['term_clause']
                        for p in positions if p.get('term_id'))
        message = 'I explicitly accept ' + ' and '.join(subjects) + '. '
        message += f'Does {target} explicitly accept this exact pact without further changes? '
        message += 'This remains pending human authorization and has not been executed.'
        return {'status': 'initiator_acceptance_delivered', 'positions': positions, 'message': message}
    return {'status': 'initiator_positions_delivered', 'positions': positions,
            'message': '\n'.join(render_point_policy(p, actor, target) for p in positions)}
