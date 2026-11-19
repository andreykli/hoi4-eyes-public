"""Durable Atlas session routing. A native seed identifies a source, not a playthrough."""
import json
import time
import uuid
from contextlib import closing
import re
import sqlite3

from world_snapshot import digest


def equivalent(first,second):
    if (first.get('digest') and first.get('digest')==second.get('digest')) or digest(first)==digest(second):
        return True
    # An immediate recognition export can have a different displayed hour on
    # the same day. Native ownership and country scope still must agree.
    return ('recognition' in second and
            all(first[k]==second[k] for k in ('day','player','states')) and
            set(first['countries'])==set(second['countries']))


def restore_recovered_exports(runtime, database):
    """Import a local, validated recovery batch into the unassigned inbox once."""
    recovery=runtime/'recovered_world_exports.json'
    if not recovery.is_file() or not database.is_file():
        return
    data=json.loads(recovery.read_text(encoding='utf-8'))
    session,source=data['id'],data['source']
    from saved_campaign_binding import matches_bridge_observation
    from world_snapshot import validate
    if (not re.fullmatch(r'[a-f0-9]{32}',session) or not re.fullmatch(r'[a-f0-9]{32}',source)
            or not matches_bridge_observation(source,data['token'],runtime/'response.json.campaign-identities.json')
            or not 1<=len(data['snapshots'])<=32):
        raise ValueError('invalid_recovered_world_exports')
    snapshots=[validate(snapshot) for snapshot in data['snapshots']]
    with closing(sqlite3.connect(database)) as connection:
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            ensure_tables(connection)
            if connection.execute('SELECT 1 FROM eyes_world_sessions WHERE id=?',(session,)).fetchone():
                return
            connection.execute('INSERT INTO eyes_world_sessions VALUES(?,?,NULL,?)',(session,source,time.time()))
            for snapshot in snapshots:
                snapshot['digest']=digest(snapshot)
                connection.execute('INSERT INTO eyes_world_pending VALUES(?,?,?)',
                                   (session,snapshot['day'],json.dumps(snapshot)))


def ensure_tables(connection):
    connection.execute('''CREATE TABLE IF NOT EXISTS eyes_world_sessions(
        id TEXT PRIMARY KEY, source TEXT NOT NULL, campaign TEXT, updated REAL NOT NULL)''')
    connection.execute('''CREATE TABLE IF NOT EXISTS eyes_world_pending(
        session TEXT NOT NULL, day INTEGER NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(session, day))''')


def queue(connection, session, source, snapshot):
    ensure_tables(connection)
    row = connection.execute('SELECT source,campaign FROM eyes_world_sessions WHERE id=?',(session,)).fetchone()
    if row and row[0]!=source:
        raise ValueError('world_session_source_mismatch')
    if row:
        campaign=row[1]
    else:
        existing=connection.execute("""SELECT 1 FROM eyes_world_records WHERE campaign=? AND kind='world_snapshot'
            UNION ALL SELECT 1 FROM eyes_world_sessions WHERE source=? LIMIT 1""",(source,source)).fetchone()
        campaign=None if existing else source
        connection.execute('INSERT INTO eyes_world_sessions VALUES(?,?,?,?)',(session,source,campaign,time.time()))
    connection.execute('UPDATE eyes_world_sessions SET updated=? WHERE id=?',(time.time(),session))
    if campaign:
        return campaign
    previous=connection.execute('SELECT payload FROM eyes_world_pending WHERE session=? AND day=?',
                                (session,snapshot['day'])).fetchone()
    if previous and not equivalent(json.loads(previous[0]),snapshot):
        raise ValueError('conflicting_same_day_snapshot')
    if not previous:
        connection.execute('INSERT INTO eyes_world_pending VALUES(?,?,?)',
                           (session,snapshot['day'],json.dumps(snapshot)))
    elif 'recognition' in snapshot:
        current=json.loads(previous[0])
        trail=current.get('recognition_trail',[])
        if current.get('recognition') and current['recognition']!=snapshot['recognition']:
            trail.append({'date':current['date'],'recognition':current['recognition']})
        updated={**current,'date':snapshot['date'],'recognition':snapshot['recognition'],'recognition_trail':trail[-64:]}
        connection.execute('UPDATE eyes_world_pending SET payload=? WHERE session=? AND day=?',
                           (json.dumps(updated),session,snapshot['day']))
    return None


def pending_sessions(connection):
    rows=connection.execute('''SELECT s.id,s.source,s.updated,p.payload,
        (SELECT COUNT(*) FROM eyes_world_pending WHERE session=s.id)
        FROM eyes_world_sessions s JOIN eyes_world_pending p ON p.session=s.id
        WHERE s.campaign IS NULL AND p.day=(SELECT MAX(day) FROM eyes_world_pending WHERE session=s.id)
        ORDER BY s.updated DESC''').fetchall()
    return [{'id':session,'source':source,'received_at':updated,'count':count,
             'date':(snapshot:=json.loads(payload))['date'],'player':snapshot['player']}
            for session,source,updated,payload,count in rows]


def store_snapshot(connection, campaign, snapshot):
    from native_recognition import record
    key=f'world:{campaign}:{snapshot["day"]}'
    old=connection.execute('SELECT payload FROM eyes_world_records WHERE id=?',(key,)).fetchone()
    if old:
        stored=json.loads(old[0])
        if not equivalent(stored,snapshot):
            raise ValueError('conflicting_same_day_snapshot')
        # Leave existing records and their persisted live witness IDs intact.
        snapshot['digest']=stored['digest']
        record(connection,campaign,snapshot)
        return 'duplicate'
    latest=connection.execute("SELECT payload FROM eyes_world_records WHERE campaign=? AND kind='world_snapshot' ORDER BY rowid DESC LIMIT 1",(campaign,)).fetchone()
    if latest and snapshot['day']<json.loads(latest[0])['day']:
        raise ValueError('snapshot_rewind_requires_separate_campaign')
    connection.execute('INSERT INTO eyes_world_records VALUES(?,?,?,?,?)',
                       (key,campaign,'world_snapshot',snapshot['date'],json.dumps(snapshot)))
    record(connection,campaign,snapshot)
    return 'stored'


def bind(connection, session, campaign):
    row=connection.execute('SELECT source,campaign FROM eyes_world_sessions WHERE id=?',(session,)).fetchone()
    if not row:
        raise ValueError('unknown_world_session')
    source, already=row
    if already:
        if campaign is None or campaign==already:
            return already, None
        raise ValueError('world_session_already_bound')
    snapshots=[json.loads(row[0]) for row in connection.execute(
        'SELECT payload FROM eyes_world_pending WHERE session=? ORDER BY day',(session,))]
    if not snapshots:
        raise ValueError('world_session_has_no_snapshots')
    if campaign is None:
        campaign=uuid.uuid4().hex
        connection.execute('INSERT INTO eyes_world_records VALUES(?,?,?,?,?)',(
            'binding:'+campaign,campaign,'world_branch',snapshots[-1]['date'],json.dumps({'source':source})))
    elif campaign!=source:
        from saved_campaign_binding import share_observed_seed
        alias=connection.execute("SELECT payload FROM eyes_world_records WHERE id=? AND kind='world_branch'",('binding:'+campaign,)).fetchone()
        target_source=json.loads(alias[0]).get('source') if alias else campaign
        exists=connection.execute("SELECT 1 FROM eyes_world_records WHERE campaign=? AND kind='world_snapshot' LIMIT 1",(campaign,)).fetchone()
        if not exists or (target_source!=source and not share_observed_seed(target_source,source)):
            raise ValueError('wrong_world_branch')
    for snapshot in snapshots:
        from native_recognition import record
        for previous in snapshot.pop('recognition_trail',[]):
            record(connection,campaign,{**snapshot,**previous})
        store_snapshot(connection,campaign,snapshot)
    connection.execute('UPDATE eyes_world_sessions SET campaign=? WHERE id=?',(campaign,session))
    connection.execute('DELETE FROM eyes_world_pending WHERE session=?',(session,))
    return campaign,snapshots[-1]
