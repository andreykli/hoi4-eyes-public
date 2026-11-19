"""AI-origin chats using the existing agreement engine, never human approval.

The chat's legacy player slot is an explicitly labelled computer initiator.
No approval or effect-preparation method is called here.
"""
from copy import deepcopy
from v6_autonomy import campaign_state, pair_fingerprint


def _evidence(engine, campaign, proposal):
    state = campaign_state(engine.diplomatic_memory, campaign)
    snapshot = state['snapshot']
    if not engine.autonomous_initiatives_enabled or not snapshot or snapshot['historical_focuses'] is None:
        raise ValueError('autonomous_negotiation_disabled')
    native = engine._latest_campaign_integration_snapshot(campaign, snapshot['player_country'])
    rules = native.get('game_rules', {}) if isinstance(native, dict) else {}
    if (rules.get('source') == 'authenticated_bridge_hoi4_historical_focuses_v1'
            and type(rules.get('historical_focuses_enabled')) is bool
            and rules['historical_focuses_enabled'] != snapshot['historical_focuses']):
        raise ValueError('historical_setting_contradicts_game_marker')
    identity = {k: proposal[k] for k in ('actor', 'target', 'action_id')}
    if snapshot['historical_focuses'] and identity not in state['historical_allowlist']:
        raise ValueError('historical_policy_not_authorized')
    actor, target = proposal['actor'], proposal['target']
    for tag in (actor, target):
        country = snapshot['countries'].get(tag, {})
        if country.get('ai_controlled') is not True or country.get('observed_day') != snapshot['day']:
            raise ValueError('ai_participant_not_fresh')
    rows = {}
    for a, b in ((actor, target), (target, actor)):
        row = next((p for p in snapshot['pairs'] if p['actor'] == a and p['target'] == b
                    and p['observed_day'] == snapshot['day']), None)
        if not row:
            raise ValueError('reciprocal_context_missing')
        rows[a] = row['context']
    if pair_fingerprint(snapshot, actor, target, rows[actor]) != proposal['context_digest']:
        raise ValueError('initiative_context_changed')
    return snapshot, rows


def _opening_message(proposal):
    from diplomatic_language import canonical_period, period
    opening = proposal.get('terms')
    if (not isinstance(opening, list) or not 1 <= len(opening) <= 2
            or not isinstance(opening[0], dict) or opening[0].get('term_id') != 'non_aggression_pact'
            or set(opening[0]) - {'term_id', 'duration', 'target_country'}
            or opening[0].get('target_country') not in (None, proposal['target'])):
        raise ValueError('unsupported_initiative_term')
    duration = opening[0].get('duration')
    if duration is not None and (not period(duration) or period(duration)[1] != 'year' or not 1 <= period(duration)[0] <= 10):
        raise ValueError('unsupported_initiative_duration')
    adjective = str(canonical_period(duration) or '').replace(' years', '-year').replace(' year', '-year')
    text = f'{proposal["actor"]} proposes a {adjective + " " if adjective else ""}non-aggression pact with {proposal["target"]}.'
    for term in opening[1:]:
        if (not isinstance(term, dict) or set(term) != {'term_id', 'notice_days'} or term['term_id'] != 'advance_notice'
                or type(term['notice_days']) is not int or not 1 <= term['notice_days'] <= 365):
            raise ValueError('unsupported_initiative_term')
        text += f' A separate condition requires mutual written advance notice of {term["notice_days"]} days before military deployments across the shared border.'
    return text + ' Please state your position or specific counterconditions.'


def start_negotiation(engine, campaign, proposal_id):
    state = campaign_state(engine.diplomatic_memory, campaign)
    proposal = state['inbox'].get(proposal_id)
    if not proposal or proposal['status'] not in {'queued', 'opened'}:
        raise ValueError('initiative_unavailable')
    snapshot, rows = _evidence(engine, campaign, proposal)
    if proposal.get('chat_id'):
        return {'chat_id': proposal['chat_id'], 'status': 'already_started'}
    # Only the symmetric, supported pact is currently allowed in this adapter.
    # Directed guarantees/access need explicit direction-aware proposal text.
    if proposal['action_id'] != 'non_aggression_pact':
        return {'status': 'ai_negotiation_action_not_connected', 'game_effect_applied': False}
    text = _opening_message(proposal)
    actor, target = proposal['actor'], proposal['target']
    chat = engine.create_chat(campaign, actor, [target])
    stored = engine.chats[chat['chat_id']]
    stored['chat_mode'] = 'ai_to_ai'
    stored['autonomous_origin'] = {'proposal_id': proposal_id, 'human_observer': snapshot['player_country'],
                                  'initiator_is_ai': True, 'action_id': proposal['action_id'],
                                  'last_day': snapshot['day'], 'steps': 0}
    ctx = rows[target]
    observation = {'date': str(snapshot['day']),
        **{k: 'yes' if ctx[k] else 'no' for k in ('war_status', 'same_faction', 'existing_non_aggression_pact')},
        'relations': str(ctx.get('relations', 'unknown')), 'threat': str(ctx.get('threat', 'unknown'))}
    engine.record_game_context_observation(chat['chat_id'], target, campaign,
        f'{campaign}:{snapshot["sequence"]:010d}', observation)
    engine.send_chat_message(chat['chat_id'],
        text,
        engine.get_chat(chat['chat_id'])['next_request_id'])
    proposal.update(status='opened', chat_id=chat['chat_id'])
    return {'chat_id': chat['chat_id'], 'status': 'ai_offer_delivered', 'game_effect_applied': False}


def advance_negotiation(engine, campaign, proposal_id):
    proposal = campaign_state(engine.diplomatic_memory, campaign)['inbox'].get(proposal_id)
    if not proposal or not proposal.get('chat_id'):
        raise ValueError('autonomous_chat_missing')
    snapshot, rows = _evidence(engine, campaign, proposal)
    chat = engine.chats[proposal['chat_id']]
    if set(chat['participants']) != {proposal['actor'], proposal['target']}:
        raise ValueError('autonomous_participants_changed')
    origin = chat.get('autonomous_origin')
    if not origin:
        raise ValueError('not_an_autonomous_chat')
    inflight = origin.get('reply_inflight')
    if inflight:
        if inflight not in chat.get('processed_request_ids', {}):
            return {'status': 'reply_recovery_required', 'chat_id': chat['chat_id'],
                    'response_request_id': inflight, 'game_effect_applied': False}
        origin.pop('reply_inflight', None)
        origin.update(last_day=snapshot['day'], steps=origin['steps'] + 1)
        return {'status': 'reply_recovered', 'chat_id': chat['chat_id'], 'game_effect_applied': False}
    if origin['steps'] >= 8:
        return {'status': 'round_limit', 'chat_id': chat['chat_id']}
    if origin['steps'] and origin['last_day'] >= snapshot['day']:
        return {'status': 'not_due', 'chat_id': chat['chat_id']}
    if chat['response_queue']:
        # One existing engine response; its plan is persisted before wording.
        ctx = rows[proposal['target']]
        observation = {'date': str(snapshot['day']),
            **{k: 'yes' if ctx[k] else 'no' for k in ('war_status', 'same_faction', 'existing_non_aggression_pact')},
            'relations': str(ctx.get('relations', 'unknown')), 'threat': str(ctx.get('threat', 'unknown'))}
        engine.record_game_context_observation(chat['chat_id'], proposal['target'], campaign,
            f'{campaign}:{snapshot["sequence"]:010d}', observation)
        origin['reply_inflight'] = engine.get_chat(chat['chat_id'])['next_request_id']
        reply = engine.advance_chat(chat['chat_id'], origin['reply_inflight'])
        origin.pop('reply_inflight', None)
        origin.update(last_day=snapshot['day'], steps=origin['steps'] + 1)
        return {'status': 'ai_reply_delivered', 'chat_id': chat['chat_id'], 'reply': reply,
                'game_effect_applied': False}
    agreements = list(chat['agreements'].values())
    if len(agreements) == 1:
        agreement = agreements[0]
        version = agreement['versions'][-1]
        if version['status'] == 'awaiting_approval' or agreement.get('status') == 'awaiting_approval':
            return {'status': 'awaiting_human_authorization', 'chat_id': chat['chat_id'],
                    'game_effect_applied': False}
        from autonomous_term_plan import continuation_points, render_continuation
        points = continuation_points(chat, agreement)
        if points and not any(t.get('rejected_by') for t in version['terms']):
            from diplomatic_planner import build_strategic_plan
            from diplomatic_doctrine import assess_doctrine
            from strategic_interests import refresh_interests
            from diplomatic_memory import pair_summary
            actor, target = proposal['actor'], proposal['target']
            context = {**rows[actor], '_bridge_verified': True}
            for key in ('ideology_group', 'major_power'):
                context[key] = snapshot['countries'][actor][key]
                context['player_' + key] = snapshot['countries'][target][key]
            summary = pair_summary(engine.diplomatic_memory, campaign_id=campaign,
                                   observer_tag=actor, counterpart_tag=target)
            request_id = engine.get_chat(chat['chat_id'])['next_request_id']
            plan = build_strategic_plan(intent='non_aggression', engine_decision='defer',
                relationship={**context, 'promise_trust': summary['promise_trust']['value']},
                game_context=context, doctrine=assess_doctrine(actor, context, target, ''),
                interests=refresh_interests(engine.diplomatic_memory, campaign, actor, {'campaign_day': snapshot['day']}),
                memory=summary, campaign_id=campaign, conversation_id=chat['conversation_id'],
                request_id=request_id, allow_override=True)
            plans = origin.setdefault('round_plans', {})
            plan_key = f'{agreement["agreement_id"]}:{version["version"]}'
            plan = plans.setdefault(plan_key, deepcopy(plan))
            from v6_autonomy import digest
            continuation_key = digest([plan_key, points])
            continuations = origin.setdefault('term_round_plans', {})
            previous = [p for record in continuations.values() for p in record['positions']]
            continuation = continuations.setdefault(continuation_key,
                render_continuation(points, plan, actor, target, previous))
            if continuation['message']:
                engine.send_chat_message(chat['chat_id'], continuation['message'], request_id)
                chat['messages'][-1].update(decision_plan_key=continuation_key,
                    decision_authority='python', text_origin='deterministic')
                origin.update(last_day=snapshot['day'], steps=origin['steps'] + 1)
                return {'status': continuation['status'], 'chat_id': chat['chat_id'],
                        'version': version['version'], 'game_effect_applied': False}
            return {'status': continuation['status'], 'chat_id': chat['chat_id'],
                    'game_effect_applied': False}
    # Do not transform another country's unparsed counterconditions into consent.
    return {'status': 'initiator_replan_required', 'chat_id': chat['chat_id'],
            'game_effect_applied': False}
