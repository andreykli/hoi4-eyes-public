"""Preloaded, read-only monitor for an existing prepared NAP event.

No expiry executor, console sender or hot-loader is implemented here.
"""
from copy import deepcopy
import json
from pathlib import Path
import re
import time
from treaty_lifecycle import validate_observation, view

MARKER = 'EYES_TREATY_OBSERVATION_V1|'


def compile_monitor(agreement):
    life = view(agreement)
    package = agreement.get('execution_package')
    if not life['registered'] or agreement.get('action_id') != 'non_aggression_pact' or not isinstance(package, dict):
        raise ValueError('registered prepared native NAP required')
    if agreement.get('execution_result') is not None:
        raise ValueError('monitor must be prepared before game application; no retroactive hot-loading')
    if len(agreement['participants']) != 2: raise ValueError('bilateral monitor required')
    from hoi4_windows_adapter import validate_package
    # Reuse the current adapter's command/body allowlist before deriving files.
    validate_package(agreement['campaign_id'], agreement['execution_request_id'], package, player_approved=True)
    actor, target = agreement['participants']
    if package['result_marker'].split('|')[1:3] != [actor, target]:
        raise ValueError('monitor event scope differs from treaty participants')
    monitor = life['monitor_id']
    for value in (actor, target):
        if not re.fullmatch(r'[A-Z][A-Z0-9]{2}', value): raise ValueError('invalid country tag')
    start = f'eyes_treaty_start_{monitor}'
    sequence = f'eyes_treaty_sequence_{monitor}'
    flag = f'eyes_treaty_monitor_{monitor}'
    seed = '[?global.eyes_campaign_seed_1|0]-[?global.eyes_campaign_seed_2|0]-[?global.eyes_campaign_seed_3|0]-[?global.eyes_campaign_seed_4|0]'
    def log(pact, war):
        return (f'log = "{MARKER}{seed}|{agreement["chat_id"]}|{agreement["agreement_id"]}|{life["version"]}|{monitor}|'
                f'[?{sequence}|0]|[?global.num_days|0]|[?{start}|0]|{pact}|{war}|[GetDateText]"')
    observation = f'''add_to_variable = {{ {sequence} = 1 }}
set_temp_variable = {{ eyes_treaty_pact = 0 }}
set_temp_variable = {{ eyes_treaty_war = 0 }}
if = {{ limit = {{ has_war_with = {target} }} set_temp_variable = {{ eyes_treaty_war = 1 }} }}
if = {{ limit = {{ has_non_aggression_pact_with = {target} }} set_temp_variable = {{ eyes_treaty_pact = 1 }} }}
{log('[?eyes_treaty_pact|0]', '[?eyes_treaty_war|0]')}
if = {{ limit = {{ OR = {{ has_war_with = {target} NOT = {{ has_non_aggression_pact_with = {target} }} }} }} clr_country_flag = {flag} }}'''
    needle = f'log = "{package["result_marker"]}"'
    body = package['event_file']
    if body.count(needle) != 1: raise ValueError('unique verified success branch required')
    # Application day is captured in the successful original action branch,
    # never guessed from the first observation after Bridge reconnects.
    appended = (needle + f'\nset_variable = {{ {start} = global.num_days }}\n'
        + f'set_variable = {{ {sequence} = 0 }}\nset_country_flag = {flag}\n' + observation)
    derived = deepcopy(package)
    derived['event_file'] = body.replace(needle, appended)
    validate_package(agreement['campaign_id'], agreement['execution_request_id'], derived, player_approved=True)
    return {'status': 'PREPARED_NOT_INSTALLED', 'game_effect_applied': False,
        'monitor_id': monitor, 'event_package': derived,
        'on_action_file_name': f'eyes_treaty_monitor_{monitor}.txt',
        'on_action_file': f'''# Original EYES read-only observation hook. Not an expiry executor.
on_actions = {{ on_daily = {{ effect = {{
    if = {{ limit = {{ tag = {actor} has_country_flag = {flag} }}
        {observation}
    }}
}} }} }}
'''}


def parse_marker(line):
    if MARKER not in line: return None
    fields = line.split(MARKER, 1)[1].rstrip('\r\n').split('|')
    if len(fields) != 11: raise ValueError('invalid treaty observation marker')
    token, chat, agreement, version, monitor, sequence, day, application, pact, war, raw_date = fields
    if pact not in {'0', '1'} or war not in {'0', '1'}: raise ValueError('invalid treaty game flags')
    from hoi4_campaign_identity import campaign_id_from_save_token
    data = {'campaign_id': campaign_id_from_save_token(token), 'chat_id': chat,
        'agreement_id': agreement, 'version': int(version), 'monitor_id': monitor,
        'sequence': int(sequence), 'day': int(day), 'application_day': int(application),
        'pact_present': pact == '1', 'at_war': war == '1', 'date': raw_date}
    return {'campaign_token': token, **validate_observation(data)}


class ObservationQueue:
    """A small durable outgoing observation journal, never an effect queue."""
    def __init__(self, path):
        self.path = Path(path)
        self.rows = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else []
        if not isinstance(self.rows, list) or len(self.rows) > 64: raise ValueError('invalid treaty observation journal')
        for row in self.rows: validate_observation(row)
        self.retry_after = 0.0

    def write(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + '.tmp')
        temporary.write_text(json.dumps(self.rows, ensure_ascii=False), encoding='utf-8')
        temporary.replace(self.path)

    def enqueue(self, row):
        row = validate_observation(row)
        identity = ('campaign_id', 'chat_id', 'agreement_id', 'version', 'monitor_id', 'sequence')
        old = next((r for r in self.rows if all(r[k] == row[k] for k in identity)), None)
        if old:
            if old != row: raise ValueError('conflicting queued treaty observation')
            return
        if len(self.rows) >= 64: raise ValueError('treaty observation queue full')
        self.rows.append(row);self.write()

    def retry(self, provider):
        if not self.rows or time.monotonic() < self.retry_after: return []
        self.retry_after = time.monotonic() + 2
        row = self.rows[0]
        result = provider.record_treaty_observation(row)
        if result.get('error'):
            if result['error'] not in {'network', 'server_busy', 'state_persistence', 'bridge_auth_required', 'game_application_pending'}:
                rejected_path = self.path.with_suffix('.rejected.json')
                rejected = json.loads(rejected_path.read_text(encoding='utf-8')) if rejected_path.exists() else []
                if not isinstance(rejected, list) or len(rejected) >= 64:
                    return [{'event': 'treaty_observation_pending', 'error': 'rejection_journal_full'}]
                temporary = rejected_path.with_suffix('.tmp')
                temporary.write_text(json.dumps([*rejected, {'observation': row, 'result': result}]), encoding='utf-8')
                temporary.replace(rejected_path)
                self.rows.pop(0);self.write()
                return [{'event': 'treaty_observation_rejected', **result}]
            return [{'event': 'treaty_observation_pending', **result}]
        if result.get('recorded') is not True:
            return [{'event': 'treaty_observation_pending', 'error': 'invalid_acknowledgement'}]
        self.rows.pop(0);self.write()
        return [{'event': 'treaty_observation_recorded', **result}]
