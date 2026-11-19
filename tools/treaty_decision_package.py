"""Compile a frozen, preloaded human-click NAP decision; never install or execute it.

This is not a Python-to-running-game transport. The game must load these files
at startup. Subsequent Python revocations cannot reach this frozen package.
"""
from copy import deepcopy
import argparse
import json
from pathlib import Path

from agreement_manager import build_non_aggression_event_package, nap_territory_exclusion
from hoi4_campaign_identity import SAVE_CAMPAIGN_TOKEN_RE, campaign_id_from_save_token
from treaty_lifecycle import register, view
from treaty_monitor import compile_monitor
from validate_hoi4_gui import parse_pdx
REVIEW_CATEGORY = 'eyes_autonomy_observer_category'


def prepare_native_nap(agreement, *, actor, save_token, not_after_day, atlas_binding=None):
    """Shared validated source for human and preauthorized AI execution.

    not_after_day is an absolute global.num_days deadline supplied from a known
    game observation, not a wall-clock date or assumed campaign starting day.
    """
    a = deepcopy(agreement)
    from saved_campaign_binding import matches_for_native_package
    if not matches_for_native_package(a.get('campaign_id'), save_token, atlas_binding):
        raise ValueError('save token does not identify this campaign')
    if type(not_after_day) is not int or not 0 <= not_after_day <= 1000000:
        raise ValueError('bounded absolute game-day deadline required')
    if a.get('execution_result') is not None:
        raise ValueError('already attempted application cannot be recompiled')
    if a.get('status') not in {'approved', 'manual_required'}:
        raise ValueError('approved pending agreement required')
    if not isinstance(a.get('execution_package'), dict) or not view(a).get('registered'):
        raise ValueError('prepare execution and persist lifecycle registration before compilation')
    if actor not in a.get('participants', []):
        raise ValueError('execution actor must be a signatory')
    version = a['versions'][a['current_version'] - 1]
    if sum(not nap_territory_exclusion(term) for term in version.get('terms', [])) != 1:
        raise ValueError('every term must be supported by the native NAP executor')
    # Regenerate from the authoritative terms: never wrap an arbitrary event
    # supplied by a client or by Ollama. All old builder consent checks apply.
    a['status'] = 'approved'
    request_id = a.get('execution_request_id') or f"agreement:{a['agreement_id']}:v{a['current_version']}"
    package = build_non_aggression_event_package({'player_country': actor}, a, version, request_id)
    a['execution_request_id'] = request_id
    a['execution_package'] = package
    # Existing prepared acknowledgements must still address the same request,
    # scope and version. Do not silently replace a different pending action.
    old = agreement.get('execution_package')
    if old and (old.get('event_id') != package['event_id'] or old.get('result_marker') != package['result_marker']):
        raise ValueError('existing prepared package binding differs')
    # Revalidate the registered terms against their saved fingerprint on a
    # private copy; compilation must not accept same-version in-place edits.
    register(a, version=a['current_version'], request_id='decision-revalidate:' + request_id)
    monitor = compile_monitor(a)
    seeds = SAVE_CAMPAIGN_TOKEN_RE.fullmatch(save_token).groups()
    checks = '\n'.join(f'check_variable = {{ var = global.eyes_campaign_seed_{i} value = {seed} compare = equals }}'
                       for i, seed in enumerate(seeds, 1))
    return a, monitor, checks


def compile_decision(agreement, *, save_token, not_after_day, not_before_day=0, atlas_binding=None):
    """Pure compilation of the existing human-signatory review route."""
    actor = agreement.get('approved_by')
    if actor not in agreement.get('participants', []):
        raise ValueError('first decision milestone requires a human signatory; observer-only AI treaties are not supported')
    a, monitor, checks = prepare_native_nap(agreement, actor=actor,
        save_token=save_token, not_after_day=not_after_day, atlas_binding=atlas_binding)
    package = monitor['event_package']
    request_id = a['execution_request_id']
    namespace = package['event_id'].rsplit('.', 1)[0]
    if type(not_before_day) is not int or not 0 <= not_before_day <= not_after_day:
        raise ValueError('absolute start day required')
    from player_native_actions import log_on_open, recheck_on_confirm
    guard = (f'is_ai = no\n{checks}\n'
             f'check_variable = {{ var = global.num_days value = {not_before_day} compare = greater_than_or_equals }}\n'
             f'check_variable = {{ var = global.num_days value = {not_after_day} compare = less_than_or_equals }}')
    # Check identity/deadline again at the actual option click, not only when
    # the decision or event first appeared. Existing once/war/faction guards stay.
    event = package['event_file'].replace(f'tag = {actor}', f'tag = {actor}\n{guard}')
    decline_marker = (f'EYES_HOI4_TREATY_DECLINED_V1|{a["agreement_id"]}|'
                      f'v{a["current_version"]}|{request_id}')
    from treaty_ai_execution import render
    from validate_hoi4_gui import Block
    tree = parse_pdx(event)
    tree.first('country_event').entries.append(
        ('option', Block([('name', f'{namespace}.cancel'), ('log', f'"{decline_marker}"')])),
    )
    recheck_on_confirm(tree, player=actor, guard=guard, namespace=namespace, package=package)
    log_on_open(tree, package)
    event = render(tree)
    category = REVIEW_CATEGORY
    decision = f'{namespace}_review'
    counterpart = next(tag for tag in a['participants'] if tag != actor)
    review_key = f'{actor} / {counterpart} · v{a["current_version"]} · {a["agreement_id"][:8]}'
    available = f'''tag = {actor}
{guard}
country_exists = {counterpart}
NOT = {{ has_non_aggression_pact_with = {counterpart} }}
NOT = {{ has_country_flag = {namespace}_handled }}'''
    files = {
        f'events/{package["event_file_name"]}': event,
        f'common/on_actions/{monitor["on_action_file_name"]}': monitor['on_action_file'],
        f'common/decisions/{namespace}.txt': f'''{category} = {{
 {decision} = {{
  cost = 0
  fire_only_once = yes
  visible = {{ {available} }}
  available = {{ {available} }}
  ai_will_do = {{ factor = 0 }}
  complete_effect = {{ if = {{ limit = {{ {available} }} country_event = {{ id = {package['event_id']} }} }} }}
 }}
}}
''',
        f'localisation/english/{package["localisation_file_name"]}': '\ufeff' + package['localisation_file'].lstrip('\ufeff') + f'''
 {decision}:0 "Review pact: {review_key}"
 {decision}_desc:0 "Frozen approved version {a['current_version']}. Review the terms in EYES before confirming. Valid through game day {not_after_day}. Later Python changes cannot revoke this preloaded decision."
 {namespace}.cancel:0 "Not now; apply nothing (recorded in EYES as a refusal)"
''',
    }
    files.update(native_news_locale_files(package))
    for name, content in files.items():
        if name.endswith('.txt'): parse_pdx(content)
    return {'status': 'PREPARED_NOT_INSTALLED', 'game_effect_applied': False,
            'delivery': 'preloaded_decision_with_explicit_human_click',
            'campaign_id': a['campaign_id'], 'agreement_id': a['agreement_id'],
            'chat_id': a['chat_id'],
            'version': a['current_version'], 'execution_request_id': request_id,
            'review_slot': {'seed':campaign_id_from_save_token(save_token),
                            'pair':sorted((actor,counterpart)),
                            'from_day':not_before_day,'through_day':not_after_day},
            'not_after_day': not_after_day, 'result_marker': package['result_marker'],
            'monitor_id': monitor['monitor_id'], 'files': files,
            'limitations': ['requires game startup with this package loaded',
                            'no live delivery or revocation from Python',
                            'custom duration and notification clauses not executable',
                            'not verified in HOI4; no signing or state mutation by compiler']}


def native_news_locale_files(package):
    from native_treaty_news import ACK_TEXT
    russian = package.get('news_localisation_ru')
    if not russian and '\nl_russian:\n' in package.get('localisation_file', ''):
        section = package['localisation_file'].split('\nl_russian:\n', 1)[1]
        russian = 'l_russian:\n' + section.split('\nl_', 1)[0].rstrip() + '\n'
    if not russian:
        return {}
    name = package['localisation_file_name'].replace('_l_english.yml', '_l_russian.yml')
    files = {f'localisation/russian/{name}': '\ufeff' + russian}
    event_id = package.get('event_id', '')
    namespace, _, _ = event_id.rpartition('.')
    if f'EYES_HOI4_NEWS_ACK_V1|{namespace}.2' in package.get('event_file', ''):
        french_name = package['localisation_file_name'].replace('_l_english.yml', '_l_french.yml')
        files[f'localisation/french/{french_name}'] = f'\ufeffl_french:\n {namespace}.2.a:0 "{ACK_TEXT["french"]}"\n'
    return files


def write_preview(result, destination):
    """Only a new directory below runtime/tmp; never installed mod paths."""
    target = Path(destination).resolve()
    root = Path(__file__).resolve().parents[1] / 'runtime' / 'tmp'
    if not target.is_relative_to(root.resolve()) or target == root.resolve():
        raise ValueError('preview must be a new isolated runtime/tmp directory')
    if target.exists(): raise ValueError('refusing to overwrite an existing preview')
    target.mkdir(parents=True)
    for relative, content in result['files'].items():
        path = target / relative
        if not path.resolve().is_relative_to(target): raise ValueError('unsafe output path')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
    manifest = {k: v for k, v in result.items() if k != 'files'}
    (target / 'PREPARED_NOT_INSTALLED.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('agreement_json', type=Path)
    p.add_argument('--save-token', required=True)
    p.add_argument('--not-after-day', required=True, type=int)
    p.add_argument('--output', required=True, type=Path)
    args = p.parse_args()
    result = compile_decision(json.loads(args.agreement_json.read_text(encoding='utf-8-sig')),
                              save_token=args.save_token, not_after_day=args.not_after_day)
    write_preview(result, args.output)
    print('Prepared only. No installed files or game state changed.')
