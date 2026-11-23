"""Player review and explicit authorization over the existing V5 agreement API."""
from ui_locale import tr


def agreement_title(agreement, locale=None):
    """Readable projection only. Never rename signed records or their files."""
    import re
    from ui_locale import language
    from game_actions import catalog
    from hoi4_country_names import load_country_names
    locale=locale or language()
    name=agreement.get('name')
    generated=(str(agreement.get('action_id') or '').replace('_',' ').strip().title()
               + ' — ' + '–'.join(agreement.get('participants',[])))
    technical=(not isinstance(name,str) or not name.strip() or name==agreement.get('action_id')
        or name==generated
        or re.search(r'(?:\.(?:txt|json|zip|mod|sqlite3?)$|^[A-Z]{3}[-_].*txt$|^eyes_|^agreement:|^[a-f0-9]{16,}$)',name or '',re.I))
    if not technical:
        return name
    names={row['id']:row['name'] for row in catalog(locale)}
    versions=agreement.get('versions') or []
    current=agreement.get('current_version',1)
    terms=versions[current-1].get('terms',[]) if 0<current<=len(versions) else []
    title=names.get(agreement.get('action_id')) if len(terms)<=1 else None
    if not title:
        title={'en':'Diplomatic agreement','ru':'Дипломатическое соглашение','fr':'Accord diplomatique'}.get(locale,'Diplomatic agreement')
    countries=load_country_names(locale)
    parties=' – '.join(countries.get(tag,tag) for tag in agreement.get('participants',[]))
    return title+(' · '+parties if parties else '')


def agreement_stage(agreement):
    status = agreement.get('status')
    if status == 'execution_confirmed':
        return 'EXECUTION CONFIRMED'
    terms = agreement.get('term_executions') or {}
    if any(term.get('status') == 'execution_confirmed' for term in terms.values()):
        return 'PARTIAL'
    if status == 'failed' and (agreement.get('execution_result') or {}).get('reason') == 'bundle_partial':
        return 'PARTIAL'
    if status in {'failed', 'blocked'}:
        return 'FAILED' if status == 'failed' else 'BLOCKED'
    if agreement.get('native_delivery'):
        return 'PENDING GAME EXECUTION'
    if agreement.get('execution_package'):
        from player_native_actions import DELIVERY_ACTIONS
        if agreement.get('action_id') in DELIVERY_ACTIONS and not agreement.get('native_delivery'):
            return 'SIGNED' if agreement.get('approved_version') == agreement.get('current_version') else 'AGREED'
        return 'PREPARED'
    if status == 'approved':
        return 'SIGNED'
    if status in {'awaiting_approval', 'manual_required'}:
        return 'AGREED'
    return 'NEGOTIATING'


def preparation_issue(code):
    if not code:
        return tr('No game package is available for this version.')
    if code.startswith('independent_policy_requires_review:defer:'):
        return tr('Game data is incomplete or ambiguous. Refresh the counterpart observation before signing; no refusal or consent has been recorded.')
    if code.startswith('independent_policy_requires_review:reject:'):
        return tr('The counterpart rejects these terms on strategic grounds. Change the terms or wait for a real change in circumstances.')
    if code.startswith('independent_policy_requires_review:counteroffer:'):
        return tr('The counterpart requests a compromise. Review the disputed terms and obtain fresh confirmations.')
    if code.startswith('independent_policy_requires_review:'):
        return tr('The policy record does not match this treaty version. Review the treaty again before signing.')
    if 'reciprocal_direction_not_enforceable' in code:
        return tr('A reciprocal guarantee or access needs two separately agreed directions. It cannot be reduced to a one-way clause.')
    if code.startswith(('approved_effect_contains_unenforced_details:',
                        'agreement_contains_unenforceable_terms:')):
        return tr('HOI4 cannot enforce every agreed term. Review the duration, conditions, and side commitments in this version.')
    if code.startswith('required_context_unknown:'):
        return tr('The current game state is incomplete. Select the counterpart again in HOI4 to refresh the observation.')
    if code == 'per_term_execution_required':
        return tr('A combined review is available for a bilateral pact, guarantee and military access with enforceable terms.')
    return tr(code)


def agreement_summary(chat):
    from agreement_manager import directional_term_label, recoverable_draft_terms
    from ui_locale import language
    locale = language()
    lines = []
    messages = {item.get('request_id'): item.get('text') for item in chat.get('messages', [])}
    for a in chat.get('agreements', {}).values():
        v = a['versions'][a['current_version'] - 1]
        stage = agreement_stage(a)
        lines.append(f"{agreement_title(a,locale)} | v{a['current_version']} | {tr(stage)}")
        for term in v.get('terms', []):
            labels = {'agreed':'AGREED', 'rejected':'Rejected term', 'unconfirmed':'Unconfirmed term'}
            text = (directional_term_label(term, locale) if term.get('term_id') in {'military_access', 'independence_guarantee'}
                    else term.get('text', term.get('term_id')))
            lines.append(f"• {text} [{tr(labels.get(term.get('status'),'Unconfirmed term'))}]")
            for key, title in (('proposed_by', 'Proposed by'), ('required_by', 'Required by'),
                               ('confirmed_by', 'Accepted by'), ('rejected_by', 'Rejected by'),
                               ('conditional_by', 'Conditional position')):
                parties = term.get(key)
                if parties:
                    lines.append(f"  {tr(title)} · {', '.join(parties)}")
            missing = [tag for tag in term.get('required_participants', []) if tag not in term.get('confirmed_by', [])]
            if missing and term.get('status') == 'unconfirmed':
                lines.append(f"  {tr('Awaiting confirmation from')} · {', '.join(missing)}")
            spec = (term.get('proposal_spec') or {}).get('action') or {}
            for key in ('duration', 'conditions', 'exceptions'):
                value = term.get(key) or spec.get(key)
                if value:
                    lines.append('  ' + ('; '.join(map(str, value)) if isinstance(value, list) else str(value)))
            source = messages.get(term.get('proposal_source_request_id'))
            if source and source != term.get('text'):
                lines.append(f"  {tr('Proposal text')} · {source[:800]}")
        if stage in {'AGREED', 'SIGNED', 'PREPARED', 'PENDING GAME EXECUTION', 'EXECUTION CONFIRMED', 'PARTIAL'}:
            lines.append(tr('Final agreed terms') + ' · ' + ' / '.join(
                directional_term_label(term, locale) if term.get('term_id') in {'military_access', 'independence_guarantee'}
                else term.get('text') or term['term_id'] for term in v.get('terms', [])))
        if a.get('block_reason'):
            lines.append(tr('Game execution blocked') + ' · ' + preparation_issue(a['block_reason']))
        if (a.get('execution_result') or {}).get('reason'):
            lines.append(tr('Game result') + ' · ' + tr(a['execution_result']['reason']))
        recovered = recoverable_draft_terms(chat, a, v)
        if recovered:
            lines.append(tr('The original messages identify three terms. Resend the full wording to review and confirm the corrected version.'))
            labels = {'non_aggression_pact':tr('Non-aggression pact'), 'guarantee':tr('Independence guarantee'),
                      'military_access':tr('Military access')}
            lines.extend(f"• {labels[kind]} · {actor} {'↔' if kind == 'non_aggression_pact' else '→'} {recipient}"
                         for kind, actor, recipient in recovered)
    recent = [item for item in chat.get('messages', []) if item.get('decision') == 'counter_offer']
    for item in recent[-3:]:
        lines.append(f"{tr('Counteroffer')} · {item.get('speaker', '')} · {item.get('text', '')[:800]}")
    return '\n'.join(lines) or tr('No agreement draft yet. Send a proposal to begin negotiations.')


def authorize_and_prepare(provider, chat_id, agreement_id, version, *, for_signing=False):
    fresh = provider._chat_api('GET', f'/chats/{chat_id}/agreements/{agreement_id}')
    if fresh.get('error'): return fresh
    if fresh.get('current_version') != version:
        return {'error': 'stale_review', 'detail': 'Treaty changed. Review the current conditions again.'}
    base = f'/chats/{chat_id}/agreements/{agreement_id}'
    if fresh.get('status') == 'awaiting_approval':
        fresh = provider._chat_api('POST', base + '/approve',
            {'version': version, 'request_id': f'player-ui-approval:{agreement_id}:v{version}', 'confirm': True})
        if fresh.get('error'):
            return fresh
    if fresh.get('status') == 'approved':
        fresh = provider._chat_api('POST', base + '/prepare',
            {'request_id': f'player-ui-prepare:{agreement_id}:v{version}'})
    from treaty_bundle import is_pending
    if (fresh.get('error') or fresh.get('status') != 'manual_required'
            or (not fresh.get('execution_package') and not is_pending(fresh))):
        return {**fresh, 'chat': provider.get_chat(chat_id)}
    from player_native_actions import DELIVERY_ACTIONS
    if fresh.get('action_id') not in DELIVERY_ACTIONS:
        return fresh
    packet = provider._chat_api('POST', base + '/native-package',
                                {'version':version, 'for_signing':for_signing}, timeout=10)
    return {**packet, 'chat': provider.get_chat(chat_id)}


def sign_and_queue(provider, chat_id, agreement_id, version, root, runtime):
    """Authorize the exact version and use the existing closed-game installer."""
    from player_native_actions import installed_for_review
    from treaty_exit_queue import active_unified_mod, enqueue_signed
    result = authorize_and_prepare(provider,chat_id,agreement_id,version,for_signing=True)
    if result.get('status') != 'PREPARED_NOT_INSTALLED':
        return result
    package = {key:value for key,value in result.items() if key!='chat'}
    try:
        mod = active_unified_mod(root)
        if installed_for_review(package,mod):
            status = 'INSTALLED_FOR_REVIEW'
        else:
            status = enqueue_signed(runtime,package)
    except (OSError,ValueError,KeyError,TypeError) as exc:
        return {**result,'error':'treaty_installation','detail':str(exc)}
    return {**result,'queue_status':status}
