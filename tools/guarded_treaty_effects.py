"""Narrow bilateral effects compiled for manual confirmation in a verified save."""
import hashlib
import re

EFFECTS = {
    'white_peace': ('white_peace', 'wp', 'White peace', 'Белый мир'),
    'faction_join': ('add_to_faction', 'fj', 'Admission to the faction', 'Приём во фракцию'),
    'faction_leave': ('remove_from_faction', 'fl', 'Departure from the faction', 'Выход из фракции'),
    'faction_create': ('create_faction', 'fc', 'Create an empty faction', 'Создать пустую фракцию'),
    'faction_dismantle': ('dismantle_faction', 'fd', 'Dissolve the faction', 'Распустить фракцию'),
    'cancel_nap': ('diplomatic_relation', 'cn', 'End the non-aggression pact', 'Расторгнуть пакт о ненападении'),
    'cancel_guarantee': ('diplomatic_relation', 'cg', 'Withdraw the guarantee', 'Отозвать гарантию'),
    'cancel_military_access': ('diplomatic_relation', 'ca', 'Withdraw military access', 'Отозвать военный доступ'),
}
RELATIONS = {
    'cancel_nap': ('non_aggression_pact', ('has_non_aggression_pact_with', 'has_non_aggression_pact_with')),
    'cancel_guarantee': ('guarantee', (None, 'is_guaranteed_by')),
    'cancel_military_access': ('military_access', (None, 'has_military_access_to')),
}
NEW_ACTIONS = (*RELATIONS, 'faction_create', 'faction_dismantle')


def relation_scope(action, grantor, recipient):
    # In HOI4 military access is stored from the visitor's perspective.
    return (recipient, grantor) if action == 'cancel_military_access' else (grantor, recipient)


_RECIPROCAL = r'(?:mutual\w*|reciprocal\w*|взаимн\w*|обоюдн\w*|mutuel\w*|réciproque\w*)'
_RECIPROCAL_RELATIONS = {
    action: re.compile(rf'\b{_RECIPROCAL}\s+(?:{noun})\b|\b(?:{noun})\s+{_RECIPROCAL}\b', re.I)
    for action, noun in {'guarantee':r'guarant\w*|гарант\w*|garant\w*',
                         'military_access':r'military access|военн\w+ доступ|accès militaire'}.items()
}


def reciprocal_direction_unresolved(term, chat):
    pattern = _RECIPROCAL_RELATIONS.get(term.get('action_id'))
    if not pattern:
        return False
    spec = (term.get('proposal_spec') or {}).get('action') or {}
    source_ids = {term.get('proposal_source_request_id'), spec.get('source_request_id')} - {None}
    excerpts = [term.get('text') or '', spec.get('source_excerpt') or '']
    excerpts.extend(row.get('text') or '' for row in chat.get('messages', []) if row.get('request_id') in source_ids)
    return any(pattern.search(excerpt) for excerpt in excerpts)


_COMMANDS = {
    'white_peace': (r' proposes white peace with ', r' предлагает белый мир стране ', r' propose une paix blanche à '),
    'faction_join': (r' proposes admitting ', r' предлагает принять ', r' propose d’admettre '),
    'faction_leave': (r' proposes removing ', r' предлагает исключить ', r' propose d’exclure '),
    'cancel_nap': (' proposes ending the non-aggression pact with ', ' предлагает расторгнуть пакт о ненападении со страной ', ' propose de mettre fin au pacte de non-agression avec '),
    'cancel_guarantee': (' proposes withdrawing its guarantee of ', ' предлагает отозвать гарантию стране ', ' propose de retirer sa garantie à '),
    'cancel_military_access': (' proposes withdrawing military access granted to ', ' предлагает отозвать военный доступ, предоставленный стране ', ' propose de retirer l’accès militaire accordé à '),
    'faction_dismantle': (' proposes dissolving its faction with ', ' предлагает распустить свою фракцию вместе со страной ', ' propose de dissoudre sa faction avec '),
    'faction_create': (' proposes creating an empty faction with the consent of ', ' предлагает создать пустую фракцию с согласия страны ', ' propose de créer une faction sans membres avec l’accord de '),
}
_SUFFIXES = {
    'white_peace': ('', '', ''),
    'faction_join': (' into its faction', ' в свою фракцию', ' dans sa faction'),
    'faction_leave': (' from its faction', ' из своей фракции', ' de sa faction'),
}
_TAG = r'[A-Z][A-Z0-9]{2}'
PROPOSAL_CUES = {action: '|'.join(re.escape(phrase.strip()) for phrase in _COMMANDS[action])
                 for action in NEW_ACTIONS}
_CANCELLATION = r'(?:cancel\w*|withdra\w*|revok\w*|terminat\w*|end(?:ing)?|отозва\w*|отмен\w*|расторг\w*|retir\w*|annul\w*|mettre fin)'
for _action, _target in {
    'cancel_nap': r'non.aggression|ненапад|non.agression',
    'cancel_guarantee': r'guarantee|гаранти|garantie',
    'cancel_military_access': r'military access|военн\w+ доступ|accès militaire',
}.items():
    PROPOSAL_CUES[_action] += rf'|\b{_CANCELLATION}\b[^.!?;,]{{0,34}}(?:{_target})'


def proposal_tags(text, action):
    for command, suffix in zip(_COMMANDS[action], _SUFFIXES.get(action, ('', '', ''))):
        match = re.fullmatch(f'({_TAG}){re.escape(command)}({_TAG}){re.escape(suffix)}[.]?', text.strip(), re.I)
        if match and match[1].upper() != match[2].upper():
            return match[1].upper(), match[2].upper()
    return None


def proposal_text(action, actor, target, language):
    index = {'ru': 1, 'fr': 2}.get(language, 0)
    return actor + _COMMANDS[action][index] + target + _SUFFIXES.get(action, ('', '', ''))[index] + '.'


def exact_scope(chat, term):
    action = term['action_id']
    source = next((row for row in chat.get('messages', [])
                   if row.get('request_id') == term.get('proposal_source_request_id')
                   and row.get('kind') == 'player_message' and row.get('speaker') == chat['player_country']), None)
    tags = proposal_tags(source.get('text', ''), action) if source else None
    if not tags or tags[0] != chat['player_country'] or set(tags) != set(term.get('required_participants', ())):
        return None
    return tags


def build_package(chat, agreement, version, request):
    from agreement_manager import _append_effect_news, _guard_native_effect_result

    action = agreement['action_id']
    effect, prefix, title, title_ru = EFFECTS[action]
    terms = version.get('terms', [])
    if (agreement.get('status') != 'approved' or agreement.get('approved_version') != version.get('version')
            or len(terms) != 1 or terms[0].get('action_id') != action):
        raise ValueError('One explicitly approved, unconditional bilateral term is required')
    term = terms[0]
    tags = exact_scope(chat, term)
    spec = (term.get('proposal_spec') or {}).get('action') or {}
    if (not tags or set(tags) != set(agreement['participants']) or term.get('status') != 'agreed'
            or set(tags) != set(term.get('confirmed_by', []))
            or any(not term.get('confirmed_by_request_ids', {}).get(tag) for tag in tags)
            or any(term.get(k) or spec.get(k) for k in ('duration', 'conditions', 'exceptions', 'unparsed_execution_details'))):
        raise ValueError('Exact plain terms and separate confirmation from both signatories are required')
    player, other = tags
    digest = hashlib.sha256(f'{agreement["campaign_id"]}|{request}|{action}'.encode()).hexdigest()[:12]
    ns = f'eyes_{prefix}_{digest}'
    common = f'is_ai = no is_subject = no num_subjects = 0 {other} = {{ exists = yes is_ai = yes is_subject = no num_subjects = 0 }}'
    if action in RELATIONS:
        relation, (outbound, inbound) = RELATIONS[action]
        receiving = f'{other} = {{ {inbound} = ROOT }}'
        active = f'{outbound} = {other} {receiving}' if outbound else receiving
        guard = f'{common} has_war = no {other} = {{ has_war = no }} {active}'
        scope, target = relation_scope(action, player, other)
        effect_line = f'{scope} = {{ diplomatic_relation = {{ country = {target} relation = {relation} active = no }} }}'
        post = f'{other} = {{ exists = yes NOT = {{ {inbound} = ROOT }} }}'
        if outbound:
            post += f' NOT = {{ {outbound} = {other} }}'
        warning = f'{title}: {player} → {other}. The existing relation is checked again when you confirm.'
    elif action == 'white_peace':
        guard = (f'{common} has_war_with = {other} is_in_faction = no '
                 f'{other} = {{ has_war_with = ROOT is_in_faction = no }} '
                 f'NOT = {{ any_other_country = {{ NOT = {{ tag = {other} }} '
                 f'OR = {{ has_war_with = ROOT has_war_with = {other} }} }} }}')
        effect_line = f'{player} = {{ white_peace = {{ tag = {other} }} }}'
        post = f'has_war = no {other} = {{ exists = yes has_war = no }}'
        warning = f'Ends the isolated war between {player} and {other}. HOI4 assigns {player} the winner scope.'
    elif action == 'faction_create':
        guard = f'{common} has_war = no is_in_faction = no has_rule = can_create_factions {other} = {{ has_war = no }}'
        effect_line = f'{player} = {{ create_faction = {ns}_faction }}'
        post = (f'is_in_faction = yes is_faction_leader = yes '
                f'NOT = {{ any_other_country = {{ is_in_faction_with = {player} }} }}')
        warning = f'{player} will lead a new faction. {other} will not join under this treaty.'
    else:
        # No wartime alliances or third faction members. The human signatory
        # must still lead the faction when the approved option is selected.
        guard = (f'{common} is_faction_leader = yes has_war = no '
                 f'{other} = {{ has_war = no {"is_in_faction = no" if action == "faction_join" else "is_in_faction_with = ROOT"} }} '
                 f'NOT = {{ any_other_country = {{ NOT = {{ tag = {other} }} is_in_faction_with = ROOT }} }}')
        effect_line = f'{player} = {{ {effect} = {"yes" if action == "faction_dismantle" else other} }}'
        member = f'{other} = {{ exists = yes is_in_faction_with = ROOT }}' if action == 'faction_join' else f'{other} = {{ exists = yes is_in_faction = no }}'
        post = (f'is_in_faction = no {other} = {{ exists = yes is_in_faction = no }}'
                if action == 'faction_dismantle' else f'is_faction_leader = yes {member}')
        warning = f'Applies the agreed faction change involving {player} and {other} while both are at peace.'
    marker = f'EYES_HOI4_ACTION_APPLIED_V2|{player}|{other}|{request}|{action}'
    failure = f'EYES_HOI4_ACTION_FAILED_V2|{player}|{other}|{request}|{action}|precondition_changed'
    script = f"""add_namespace = {ns}
country_event = {{
 id = {ns}.1
 title = {ns}.1.t
 desc = {ns}.1.d
 is_triggered_only = yes
 trigger = {{ tag = {player} is_ai = no }}
 option = {{
  name = {ns}.1.a
  trigger = {{ tag = {player} {guard} }}
  {effect_line}
  log = "{marker}"
 }}
 option = {{
  name = {ns}.1.b
  trigger = {{ tag = {player} NOT = {{ {guard} }} }}
  log = "{failure}"
 }}
}}
"""
    loc = f"""l_english:
 {ns}.1.t:0 "{title}"
 {ns}.1.d:0 "{warning}"
 {ns}.1.a:0 "Confirm the agreement"
 {ns}.1.b:0 "The in-game conditions have changed"
"""
    if action == 'faction_create':
        loc += f' {ns}_faction:0 "{player} Diplomatic Alliance"\n'
    script = _guard_native_effect_result(script, namespace=ns, player=player, effect=effect_line,
                                         marker=marker, postcondition=post)
    script, loc, ru = _append_effect_news(
        script, loc, ns, marker, player, other, None if action in NEW_ACTIONS else agreement.get('name'),
        publish_news=agreement.get('_publish_effect_news', True) is True,
    )
    return {'status': 'manual_required', 'action_id': action, 'event_id': ns+'.1',
            'event_file_name': f'eyes_agreement_{digest}.txt',
            'localisation_file_name': f'eyes_agreement_{digest}_l_english.yml',
            'execution_request_id': request, 'open_event_command': f'event {ns}.1 {player}',
            'result_marker': marker, 'failure_marker': failure, 'event_file': script,
            'localisation_file': loc, 'news_localisation_ru': ru,
            'game_effect': {'action_id': action, 'actor': player, 'target': other,
                            'enforced_term_ids': [term['term_id']], 'unenforced_term_ids': []},
            'manual_confirmation_required': True, 'active_mod_install_required': True,
            'live_verified': False, 'execution_command': None}
