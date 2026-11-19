"""Bounded native log snapshots. No commands, effects, or consent cross here."""
from copy import deepcopy
import time
from v6_autonomy import validate_snapshot

MARKER = 'EYES_AUTONOMY_V1|'
WORLD_MARKER = 'EYES_AUTONOMY_V2|'


class SnapshotCollector:
    def __init__(self):
        self.pending = {}

    def feed(self, line):
        marker = WORLD_MARKER if WORLD_MARKER in line else MARKER
        if marker not in line:
            return None
        fields = line.split(marker, 1)[1].strip().split('|')
        if len(fields) < 3 or len(line) > 2048:
            raise ValueError('invalid autonomy marker')
        kind, token, sequence, *data = fields
        from hoi4_campaign_identity import campaign_id_from_save_token
        campaign_id_from_save_token(token)  # Validate signed native save seed.
        seq = int(sequence)
        if seq < 0:
            raise ValueError('invalid snapshot sequence')
        key = (token, seq, marker)
        now = time.monotonic()
        self.pending = {k: v for k, v in self.pending.items() if now - v['started'] < 120}
        if kind == 'BEGIN':
            if len(data) != 3 or data[2] not in {'0', '1'}:
                raise ValueError('invalid snapshot header')
            if len(self.pending) >= 4:
                raise ValueError('too many unfinished snapshots')
            self.pending[key] = {'started': now, 'rows': 0, 'snapshot': {
                'sequence': seq, 'day': int(data[0]), 'player_country': data[1],
                'historical_focuses': data[2] == '1', 'countries': {}, 'pairs': []}}
            return None
        batch = self.pending.get(key)
        if not batch:
            raise ValueError('autonomy header missing')
        snapshot = batch['snapshot']
        try:
            if kind == 'COUNTRY':
                if len(data) != 3 or data[1] not in {'0', '1'} or data[2] not in {'0', '1'}:
                    raise ValueError('invalid country row')
                if data[0] in snapshot['countries'] or len(snapshot['countries']) >= (512 if marker == WORLD_MARKER else 7):
                    raise ValueError('duplicate or excessive country row')
                snapshot['countries'][data[0]] = {'ai_controlled': data[1] == '1',
                    'observed_day': snapshot['day'], 'major_power': data[2] == '1', 'ideology_group': None}
            elif kind == 'PAIR':
                if len(data) != 7 or any(v not in {'0', '1'} for v in data[3:6]):
                    raise ValueError('invalid directed pair row')
                actor, target, relation, war, faction, nap, threat = data
                previous = next((p for p in snapshot['pairs'] if p['actor'] == actor and p['target'] == target), None)
                candidate = {'actor': actor, 'target': target, 'observed_day': snapshot['day'],
                    'context': {'relations': float(relation), 'war_status': war == '1',
                        'same_faction': faction == '1', 'existing_non_aggression_pact': nap == '1', 'threat': float(threat)}}
                if previous:
                    if marker == WORLD_MARKER and previous == candidate:
                        batch['rows'] += 1
                        return None
                    raise ValueError('duplicate or conflicting directed pair')
                if len(snapshot['pairs']) >= (64 if marker == WORLD_MARKER else 42):
                    raise ValueError('excessive directed pairs')
                snapshot['pairs'].append({'actor': actor, 'target': target, 'observed_day': snapshot['day'],
                    'context': {'relations': float(relation), 'war_status': war == '1',
                        'same_faction': faction == '1', 'existing_non_aggression_pact': nap == '1',
                        'threat': float(threat)}})
            elif kind == 'END':
                if len(data) != 1 or int(data[0]) != batch['rows']:
                    raise ValueError('incomplete autonomy snapshot')
                # Require every directed pair: a dropped tail must not silently
                # bias partner selection toward the first countries logged.
                n = len(snapshot['countries'])
                if marker == MARKER and len(snapshot['pairs']) != n * (n - 1):
                    raise ValueError('incomplete autonomy pair matrix')
                result = {'campaign_token': token, 'snapshot': validate_snapshot(snapshot)}
                del self.pending[key]
                return deepcopy(result)
            else:
                raise ValueError('unknown autonomy marker')
            batch['rows'] += 1
        except (ValueError, TypeError):
            self.pending.pop(key, None)
            raise
        return None


def accept_native_snapshot(engine, campaign, data):
    """Serialized API operation: persist evidence, review, and hand off one chat.

    No model call for a human inbox notice. AI replies are advanced separately
    so the initial proposal itself does not require model availability. Later
    snapshots advance at most one reply; a hard interruption is held for
    recovery rather than reinterpreted as a different diplomatic decision.
    """
    from v6_autonomy import record_snapshot, tick, open_chat, campaign_state
    recorded = record_snapshot(engine.diplomatic_memory, campaign, data)
    state = campaign_state(engine.diplomatic_memory, campaign)
    if recorded.get('idempotent'):
        return {'recorded': True, 'idempotent': True, 'campaign_id': campaign,
                'sequence': data['sequence'], 'delivery': deepcopy(state.get('last_delivery'))}
    state['native_sequence'] = data['sequence']
    from historical_conferences import Conferences
    Conferences(engine).native_snapshot(campaign, state['snapshot'])
    reviewed = tick(engine, campaign)
    delivery = {'review': reviewed, 'game_effect_applied': False}
    # Bound inference to one existing AI conversation per fresh snapshot.
    # Starting a new chat only writes a proposal; it makes no model call.
    from autonomous_negotiation import advance_negotiation
    for proposal in sorted(state['inbox'].values(), key=lambda p: (p['day'], p['proposal_id'])):
        chat = engine.chats.get(proposal.get('chat_id'), {})
        if proposal['status'] == 'opened' and chat.get('autonomous_origin'):
            try:
                outcome = advance_negotiation(engine, campaign, proposal['proposal_id'])
                delivery['negotiation'] = outcome
                if outcome['status'] in {'initiator_replan_required', 'awaiting_human_authorization', 'round_limit', 'reply_recovery_required'}:
                    continue
            except ValueError as error:
                delivery['negotiation'] = {'status': 'blocked', 'reason': str(error)}
                continue
            break
    if reviewed.get('status') == 'initiative_queued':
        proposal = reviewed['proposal']
        if proposal['target'] == data['player_country']:
            chat = open_chat(engine, campaign, proposal['proposal_id'])
            delivery['chat_id'] = chat['chat_id']
        else:
            from autonomous_negotiation import start_negotiation
            delivery.update(start_negotiation(engine, campaign, proposal['proposal_id']))
    state['last_delivery'] = deepcopy(delivery)
    return {'recorded': True, 'idempotent': False, 'campaign_id': campaign,
            'sequence': data['sequence'], 'delivery': delivery}
