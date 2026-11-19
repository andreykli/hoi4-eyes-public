"""Cabinet discussion and private plans on the active Atlas timeline."""
from contextlib import closing
import json
from hashlib import sha256
import re
import sqlite3
from pathlib import Path
from uuid import uuid4

from flask import jsonify, render_template, request
from ui_locale import language, tr
from cabinet_roster import choices


ROLES = ('head', 'minister', 'general', 'marshal', 'admiral', 'diplomat')
TOPICS = ('economy', 'laws', 'industry', 'army', 'fleet', 'foreign', 'custom')
MODES = ('realistic', 'plausible', 'sandbox')
VIEWS = ('government', 'independent', 'opposition')
KINDS = ('proposal', 'objection', 'alternative', 'response')
FORMATS = ('private', 'roundtable', 'discussion')
STANCES = ('support', 'concern', 'conditional', 'undecided')
DISCUSSION_LIMIT = 6
KEY = re.compile(r'[a-f0-9]{32}')


class MeetingConflict(ValueError):
    pass


def required_text(value, limit):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit or len(value) > limit:
        raise ValueError('invalid_meeting_input')
    return value.strip()


def initial_state(body, roster: list):
    roles, topic, mode = [body.get(key) for key in ('roles', 'topic', 'mode')]
    if (not isinstance(roles, list) or not 1 <= len(roles) <= len(ROLES)
            or any(x not in ROLES for x in roles) or len(roles) != len(set(roles))
            or topic not in TOPICS or mode not in MODES):
        raise ValueError('invalid_meeting_input')
    meeting_format = body.get('format', 'private' if len(roles)==1 else 'roundtable')
    if meeting_format not in FORMATS or (meeting_format == 'private') != (len(roles)==1):
        raise ValueError('invalid_meeting_input')
    selected = body.get('people', {})
    if not isinstance(selected, dict) or any(role not in roles for role in selected):
        raise ValueError('invalid_meeting_input')
    by_id = {p['id']:p for p in roster}
    participants = []
    names = set()
    for role in roles:
        identifier = selected.get(role)
        if identifier is None:
            participants.append({'role':role,'name':None})
            continue
        person = by_id.get(identifier) if isinstance(identifier,str) else None
        if not person or person['role'] != role or person['name'].casefold() in names:
            raise MeetingConflict('meeting_participant_unavailable')
        participants.append(person)
        names.add(person['name'].casefold())
    return {'topic':topic,'mode':mode,'format':meeting_format,
            'title':required_text(body.get('title'),100),'participants':participants}


def reviewed_replies(raw, roles):
    if not isinstance(raw, list) or len(raw) != len(roles):
        return []
    answers = {}
    for item in raw:
        if (not isinstance(item, dict) or set(item)-{'stance'} != {'role','kind','text'}
                or item['role'] not in roles or item['role'] in answers or item['kind'] not in KINDS):
            return []
        try:
            answers[item['role']] = dict(item, text=required_text(item['text'], 700),
                stance=item.get('stance') if item.get('stance') in STANCES else 'undecided')
        except ValueError:
            return []
    return [answers[role] for role in roles]


class MeetingStore:
    def __init__(self, path):
        self.path = Path(path)

    def records(self, scope):
        if not self.path.is_file():
            return {}
        uri = self.path.resolve().as_uri() + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='eyes_meeting_events'").fetchone():
                return {}
            result = {}
            for meeting, day, session, payload in db.execute(
                'SELECT meeting,day,session,payload FROM eyes_meeting_events WHERE campaign=? AND player=? ORDER BY seq',
                (scope['campaign'], scope['player'])):
                entry = result.setdefault(meeting, {'id': meeting, 'events': [], 'has_future': False})
                if day > scope['day'] or (day == scope['day'] and scope.get('session') not in (None,session)):
                    entry['has_future'] = True
                    continue
                entry['events'].append(dict(json.loads(payload), day=day))
            return {key: value for key,value in result.items() if value['events']}

    def archives(self):
        if not self.path.is_file():
            return []
        with closing(sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro', uri=True)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='eyes_meeting_events'").fetchone():
                return []
            return [{'campaign':campaign,'player':player,'day':day} for campaign,player,day in db.execute(
                'SELECT campaign,player,MAX(day) FROM eyes_meeting_events GROUP BY campaign,player ORDER BY MAX(seq) DESC')]

    def append(self, scope, meeting, command, revision, event):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS eyes_meeting_events(
                seq INTEGER PRIMARY KEY, campaign TEXT, player TEXT, meeting TEXT, day INTEGER, session TEXT,
                command TEXT, payload TEXT, UNIQUE(campaign,player,command))''')
            db.execute('BEGIN IMMEDIATE')
            keys = (scope['campaign'], scope['player'])
            cursor = db.execute('SELECT meeting,payload FROM eyes_meeting_events WHERE campaign=? AND player=? AND command=?',
                                (*keys, command)).fetchone()
            if cursor:
                if ((meeting is not None and cursor[0] != meeting)
                        or json.loads(cursor[1]).get('input_hash') != event.get('input_hash')):
                    raise MeetingConflict('meeting_changed')
                return cursor[0]
            if meeting is not None:
                count, blocked = db.execute(
                    '''SELECT COUNT(*), MAX(day>? OR (day=? AND session!=?))
                       FROM eyes_meeting_events WHERE campaign=? AND player=? AND meeting=?''',
                    (scope['day'],scope['day'],scope['session'],*keys,meeting)).fetchone()
                if not count or count != revision or blocked:
                    raise MeetingConflict('meeting_changed')
            else:
                meeting = uuid4().hex
            db.execute('INSERT INTO eyes_meeting_events(campaign,player,meeting,day,session,command,payload) VALUES(?,?,?,?,?,?,?)',
                       (*keys, meeting, scope['day'], scope['session'], command, json.dumps(event)))
        return meeting


def prepare_project(body, day):
    stages = body.get('stages')
    if not isinstance(stages, list) or not 1 <= len(stages) <= 6:
        raise ValueError('invalid_meeting_input')
    duration = body.get('duration')
    if type(duration) is not int or not len(stages) <= duration <= 3650:
        raise ValueError('invalid_meeting_input')
    return {'id': uuid4().hex, 'title': required_text(body.get('title'), 100),
            'stages': [{'text': required_text(stage, 240), 'due': day + duration * (i + 1) // len(stages)}
                       for i,stage in enumerate(stages)]}


def dialogue_context(history, operation, snapshot):
    from language_detection import detect_language
    last_player = next((row['text'] for row in reversed(history)
                        if row['kind']=='turn' and row.get('text')), '')
    autonomous = []
    for row in reversed(history):
        if row['kind'] == 'turn':
            break
        if row['kind']=='exchange' and row.get('task')=='continue' and row.get('replies'):
            autonomous.append(row)
    original = history[0]
    if operation=='continue' and (original.get('format')!='discussion' or len(autonomous)>=DISCUSSION_LIMIT):
        raise MeetingConflict('meeting_discussion_limit')
    return ({**original,'task':operation,'language':detect_language(last_player, fallback=language()),
             'naval_context':snapshot.get('naval') if original['topic']=='fleet' else None}, len(autonomous))


def register(app, db_path, observation, provider, background, news):
    store = MeetingStore(db_path) if db_path is not None else None

    def capture():
        row, _ = observation()
        if not row or not row.get('proof') or not row.get('save_token'):
            return None, {}
        proof = row['proof'] if isinstance(row['proof'],dict) else {}
        snapshot = {**row.get('snapshot',{}),'native_calendar':proof.get('game_date')}
        return {key: row[key] for key in ('campaign','player','day','date','session')}, snapshot

    @app.get('/meetings')
    def meetings_page():
        return render_template('meetings.html', roles=ROLES, topics=TOPICS, modes=MODES, views=VIEWS,
                               formats=FORMATS, limit=DISCUSSION_LIMIT, locale=language(), t=tr)

    @app.get('/meetings/state')
    def meetings_state():
        scope, snapshot = capture()
        meetings = store.records(scope) if store is not None and scope else {}
        return jsonify(scope=scope, meetings=list(meetings.values()),
                       roster=choices(scope['player'],snapshot.get('cabinet',[])) if scope else [],
                       archives=store.archives() if store is not None else [])

    @app.get('/meetings/archive')
    def meeting_archive():
        selector = {key:request.args.get(key) for key in ('campaign','player')}
        entry = next((row for row in store.archives() if all(row[k]==v for k,v in selector.items())), None) if store else None
        if entry is None:
            return jsonify(error='meeting_context_required'), 404
        return jsonify(meetings=list(store.records(entry).values()))

    @app.get('/meetings/press')
    def meeting_press():
        scope, _ = capture()
        view = request.args.get('view')
        if view not in VIEWS or not scope:
            return jsonify(error='meeting_context_required'), 409
        return jsonify(view=view, news=news(scope['campaign']).get('news', [])[:20])

    @app.post('/meetings/change')
    def change_meeting():
        if request.headers.get('Sec-Fetch-Site') not in (None, 'same-origin'):
            return jsonify(error='cross_origin_request'), 403
        scope, snapshot = capture()
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or store is None or scope is None or body.get('scope') != scope:
            return jsonify(error='meeting_context_required'), 409
        command = body.get('command')
        if not isinstance(command, str) or not KEY.fullmatch(command):
            return jsonify(error='invalid_meeting_input'), 400
        meeting, action = body.get('meeting'), body.get('action')
        input_hash = sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        try:
            if action == 'schedule' and meeting is None:
                delay = body.get('delay', 0)
                if type(delay) is not int or not 0 <= delay <= 365:
                    raise ValueError('invalid_meeting_input')
                roster = choices(scope['player'], snapshot.get('cabinet', []))
                event = {'kind':'scheduled','scheduled_day':scope['day']+delay,**initial_state(body,roster)}
                revision = 0
            else:
                if not isinstance(meeting, str) or not KEY.fullmatch(meeting):
                    raise ValueError('invalid_meeting_input')
                entry = store.records(scope).get(meeting)
                if not entry or entry['has_future']:
                    raise MeetingConflict('meeting_changed')
                history = entry['events']
                replay = next((row for row in history if row.get('command') == command), None)
                if replay:
                    if replay.get('input_hash') != input_hash:
                        raise MeetingConflict('meeting_changed')
                    return jsonify(meeting=entry, replay=True)
                revision = body.get('revision')
                if type(revision) is not int or revision != len(history) or history[-1]['kind'] == 'closed':
                    raise MeetingConflict('meeting_changed')
                if action != 'close' and history[0]['scheduled_day'] > scope['day']:
                    raise MeetingConflict('meeting_not_due')
                if action in ('turn','continue','compromise'):
                    if action=='continue' and history[-1]['kind']=='exchange' and not history[-1]['replies']:
                        raise MeetingConflict('meeting_discussion_unavailable')
                    text = required_text(body.get('text'),2000) if action=='turn' else ''
                    context, position = dialogue_context(history,action,snapshot)
                    current_people = {p['id']:p for p in choices(scope['player'],snapshot.get('cabinet',[]))}
                    participants = []
                    for row in context['participants']:
                        if row.get('id') and row['id'] not in current_people:
                            raise MeetingConflict('meeting_participant_unavailable')
                        person = current_people.get(row.get('id'), {'role':row['role'],'name':None})
                        fact = background({**scope,'native_calendar':snapshot['native_calendar']},person) if person['name'] else None
                        participants.append(dict(person, historical_background=fact) if fact else person)
                    context['seats'] = [row['role'] for row in participants]
                    context['voices'] = {row['role']:row['name'] for row in participants if row['name']}
                    context['decisions'] = [row['text'][:140] for row in history if row['kind']=='decision'][-2:]
                    positions = {}
                    for previous in reversed(history):
                        for reply in previous.get('replies',[]):
                            if reply.get('name') == context['voices'].get(reply['role']):
                                positions.setdefault(reply['role'],reply.get('stance','undecided'))
                    context['positions'] = positions
                    if action=='continue':
                        participants = [participants[position%len(participants)]]
                    elif action=='compromise':
                        participants = [next((row for row in participants if row['role']=='head'), participants[0])]
                    elif context.get('format')=='discussion':
                        participants = [participants[position%len(participants)]]
                    model = getattr(provider(), 'cabinet_turn', None)
                    replies = reviewed_replies(
                        model(scope, context, participants, history[-8:], text) if model else None,
                        [row['role'] for row in participants])
                    names = {row['role']:row['name'] for row in participants}
                    replies = [dict(reply, name=names[reply['role']]) for reply in replies]
                    event = {'kind':'turn' if action=='turn' else 'exchange',
                             'task':action,'text':text,'replies':replies}
                elif action == 'stop' and history[0].get('format') == 'discussion':
                    event = {'kind':'paused'}
                elif action == 'decision' and body.get('confirmed') is True:
                    event = {'kind':'decision','text':required_text(body.get('text'), 1200)}
                elif action == 'motion':
                    options = body.get('options')
                    if not isinstance(options,list) or not 2 <= len(options) <= 4:
                        raise ValueError('invalid_meeting_input')
                    options = [required_text(option,240) for option in options]
                    if len(set(options)) != len(options):
                        raise ValueError('invalid_meeting_input')
                    event = {'kind':'motion','id':uuid4().hex,
                             'text':required_text(body.get('text'),240),'options':options}
                elif action == 'choose' and body.get('confirmed') is True:
                    motion = next((row for row in history if row['kind']=='motion' and row['id']==body.get('motion')), None)
                    choice = body.get('choice')
                    if motion is None or type(choice) is not int or not 0 <= choice < len(motion['options']):
                        raise ValueError('invalid_meeting_input')
                    if any(row.get('motion')==motion['id'] for row in history):
                        raise MeetingConflict('meeting_changed')
                    event = {'kind':'decision','motion':motion['id'],'text':motion['options'][choice]}
                elif action == 'project' and body.get('confirmed') is True:
                    event = {'kind':'project','project':prepare_project(body, scope['day'])}
                elif action == 'milestone' and body.get('confirmed') is True:
                    project = next((row['project'] for row in history if row['kind']=='project'
                                    and row['project']['id']==body.get('project')), None)
                    if project is None:
                        raise ValueError('invalid_meeting_input')
                    index = sum(row['kind']=='milestone' and row['project']==project['id'] for row in history)
                    if index >= len(project['stages']):
                        raise MeetingConflict('meeting_changed')
                    event = {'kind':'milestone','project':project['id'],'index':index,
                             'text':required_text(body.get('text'), 600),'evidence':'player_record'}
                elif action == 'close' and body.get('confirmed') is True:
                    event = {'kind':'closed'}
                else:
                    raise ValueError('invalid_meeting_input')
            updated, updated_snapshot = capture()
            if updated != scope or (action in ('schedule','turn','continue','compromise')
                    and updated_snapshot.get('cabinet') != snapshot.get('cabinet')):
                raise MeetingConflict('meeting_context_required')
            meeting = store.append(scope, meeting, command, revision, dict(event, command=command, input_hash=input_hash))
            return jsonify(meeting=store.records(scope)[meeting])
        except MeetingConflict as error:
            return jsonify(error=str(error)), 409
        except ValueError as error:
            return jsonify(error=str(error)), 400
