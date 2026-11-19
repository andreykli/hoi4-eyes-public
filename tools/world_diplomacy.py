"""Recognition records and fail-closed country drafts; no engine execution."""
import json, sqlite3, uuid, re, struct, os, time
from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED
from contextlib import closing
from pathlib import Path
from functools import lru_cache
from flask import jsonify, request, send_file, render_template
from validate_hoi4_gui import discover_hoi4_install
GAME=Path(os.environ.get('EYES_WORLD_MAP_ROOT') or discover_hoi4_install() or 'D:/SteamLibrary/steamapps/common/Hearts of Iron IV')
def atlas_sources():
 from atlas_map_sources import active_map_files
 documents=Path.home()/'Documents/Paradox Interactive/Hearts of Iron IV'
 return active_map_files(GAME,documents)

def native_geometry_supported():
 try:
  paths=atlas_sources()
  vanilla_states={path.relative_to(GAME).as_posix() for path in (GAME/'history/states').glob('*.txt')}
  return (not os.environ.get('EYES_WORLD_MAP_ROOT')
          and paths.keys()==vanilla_states|{'map/provinces.bmp','map/definition.csv'}
          and all(path.is_relative_to(GAME) for path in paths.values()))
 except (OSError,ValueError,TypeError):
  return False

def geography(sources=None):
 sources=atlas_sources() if sources is None else sources
 return read_geography(tuple((key,path,path.stat().st_mtime_ns,path.stat().st_size)
                             for key,path in sorted(sources.items())))

class DuplicateAtlasState(ValueError):
 def __init__(self,state,files):
  super().__init__('duplicate_atlas_state')
  self.details={'state':state,'files':[Path(name).name for name in files]}

@lru_cache(maxsize=2)
def read_geography(revision):
 sources={key:path for key,path,_,_ in revision}
 colors={}; states={}
 for line in sources['map/definition.csv'].read_text(encoding='utf-8-sig').splitlines():
  v=line.split(';')
  if len(v)>4 and v[0].isdigit() and int(v[0])>0:
   color=tuple(map(int,v[1:4]))
   if color in colors:raise ValueError('duplicate_atlas_colour')
   colors[color]=int(v[0])
 if len(set(colors.values()))!=len(colors):raise ValueError('duplicate_atlas_province')
 state_files={}
 for key,p in sources.items():
  if not key.startswith('history/states/'):continue
  text=re.sub(r'#.*','',p.read_text(encoding='utf-8-sig',errors='replace'))
  sid=re.search(r'\bid\s*=\s*(\d+)',text); block=re.search(r'\bprovinces\s*=\s*\{([^}]+)',text)
  if not sid or not block:raise ValueError('invalid_atlas_state')
  state=int(sid[1])
  if state in state_files:raise DuplicateAtlasState(state,(state_files[state],key))
  state_files[state]=key
  for pid in map(int,re.findall(r'\d+',block[1])):
   if pid in states:raise ValueError('duplicate_atlas_state_province')
   states[pid]=state
 if not states or not states.keys()<=set(colors.values()):raise ValueError('invalid_atlas_province_reference')
 return colors,states

def pick(x,y):
 with atlas_sources()['map/provinces.bmp'].open('rb') as f:
  h=f.read(54); offset=struct.unpack_from('<I',h,10)[0]; w,height=struct.unpack_from('<ii',h,18); bits=struct.unpack_from('<H',h,28)[0]
  if bits!=24 or not (0<=x<w and 0<=y<abs(height)): raise ValueError('map_coordinate')
  f.seek(offset+((abs(height)-1-y) if height>0 else y)*((w*3+3)//4*4)+x*3); b,g,r=f.read(3)
 colors,states=geography(); province=colors.get((r,g,b))
 return {'province':province,'state':states.get(province),'source':'configured_playset_map_not_live_ownership'}

def register(app,db_path,authorize):
 witness_path=Path(db_path).with_name('world_live_observation.json') if db_path else None
 live_player=None
 candidate=None

 def restore_observation():
  if not witness_path or not witness_path.is_file() or not Path(db_path).is_file():return None
  try:
   observed=json.loads(witness_path.read_text(encoding='utf-8'))
   uri=Path(db_path).resolve().as_uri()+'?mode=ro'
   with closing(sqlite3.connect(uri,uri=True)) as c:
    binding=c.execute('SELECT source,campaign FROM eyes_world_sessions WHERE id=?',(observed['session'],)).fetchone()
    if binding!=(observed['source'],observed['campaign']):return None
    row=c.execute("SELECT payload FROM eyes_world_records WHERE campaign=? AND kind='world_snapshot' ORDER BY rowid DESC LIMIT 1",(observed['campaign'],)).fetchone()
   snapshot=json.loads(row[0]) if row else {}
   if snapshot.get('day')!=observed['day'] or snapshot.get('digest')!=observed['digest']:return None
   snapshot.pop('naval',None)
   if 'naval' in observed:
    from fleet_dashboard import validate_totals
    snapshot['naval']=validate_totals(observed['naval'])
   snapshot.pop('cabinet',None)
   if 'cabinet' in observed:
    from cabinet_roster import validate_roster
    snapshot['cabinet']=validate_roster(observed['cabinet'])
   return {**observed,'snapshot':snapshot,'received':time.monotonic()}
  except (OSError,ValueError,TypeError,KeyError,sqlite3.Error):return None

 def remember_observation(observation):
  if not witness_path or not observation.get('proof') or not observation.get('save_token'):return
  data={k:observation[k] for k in ('pid','campaign','player','day','date','source','session','save_token','proof')}
  data['digest']=observation['snapshot']['digest']
  if 'naval' in observation['snapshot']:data['naval']=observation['snapshot']['naval']
  if 'cabinet' in observation['snapshot']:data['cabinet']=observation['snapshot']['cabinet']
  temporary=witness_path.with_name(f'.{witness_path.name}.{uuid.uuid4().hex}')
  try:
   temporary.write_text(json.dumps(data),encoding='utf-8')
   os.replace(temporary,witness_path)
  except OSError:
   app.logger.warning('Atlas could not persist its live observation')
  finally:temporary.unlink(missing_ok=True)

 live_player=restore_observation()

 def observation_status(*,for_confirmation=False):
  observation=live_player
  if observation is None:return None,'fresh_native_observation_required'
  from hoi4_bridge import get_hoi4_window_state
  game=get_hoi4_window_state()
  if game and len(game.get('pids', ()))>1:
   return None,'game_process_ambiguous'
  if not game or not game.get('running') or not game.get('pid'):
   return None,'game_session_unavailable'
  if game['pid']!=observation['pid']:
   return None,'game_process_changed'
  if observation.get('proof'):
   from world_freshness import still_current
   limits={'max_idle_seconds':None} if for_confirmation else {}
   if not still_current(observation['proof'],observation['pid'],observation['save_token'],observation['player'],observation['day'],**limits):
    return None,'native_log_changed_or_stale'
  elif time.monotonic()-observation['received']>60:
   return None,'fresh_native_observation_required'
  return observation,None

 def treaty_observation(*,for_confirmation=False,expected_campaign=None,expected_player=None):
  # A witness for another branch cannot diagnose a process restart in this one.
  if expected_campaign is not None:
   if (candidate and candidate.get('pending') and candidate['source']==expected_campaign
       and candidate['snapshot']['player']==expected_player):
    return None,'treaty_branch_pending'
   if live_player is not None and (live_player['campaign']!=expected_campaign
                                    or live_player['player']!=expected_player):
    return None,'treaty_branch_does_not_match_native_source'
  observation,reason=observation_status(for_confirmation=for_confirmation)
  if observation is None and expected_campaign is None and candidate and candidate.get('pending'):
   return None,'treaty_branch_pending'
  if observation is None or not observation.get('save_token'):
   return observation,reason
  if for_confirmation and not observation.get('proof'):
   return None,'treaty_branch_has_no_verified_native_export'
  from hoi4_campaign_identity import campaign_id_from_save_token
  from saved_campaign_binding import VerifiedAtlasBinding, matches_bridge_observation
  campaign,token=observation['campaign'],observation['save_token']
  try:
   if not db_path or observation['source']!=campaign:
    return None,'treaty_branch_does_not_match_native_source'
   if campaign_id_from_save_token(token)==campaign:
    return observation,None
   # A shared seed is not a branch selector. Only the active, log-proven
   # Bridge export whose exact source and durable Atlas binding agree can
   # authorize the alias. Legacy token-only callers keep their strict check.
   if not observation.get('proof') or not matches_bridge_observation(campaign,token):
    return None,'treaty_branch_has_no_verified_native_export'
   uri=Path(db_path).resolve().as_uri()+'?mode=ro'
   with closing(sqlite3.connect(uri,uri=True)) as c:
    row=c.execute('SELECT source,campaign FROM eyes_world_sessions WHERE id=?',
                  (observation['session'],)).fetchone()
    if row!=(campaign,campaign):
     return None,'treaty_branch_has_no_current_session_binding'
    snapshot=c.execute("SELECT payload FROM eyes_world_records WHERE campaign=? AND kind='world_snapshot' ORDER BY rowid DESC LIMIT 1",
                       (campaign,)).fetchone()
   head=json.loads(snapshot[0]) if snapshot else {}
   if (head.get('digest')!=observation['snapshot']['digest']
       or head.get('day')!=observation['day'] or head.get('player')!=observation['player']):
    return None,'treaty_branch_has_no_matching_snapshot'
  except (KeyError,OSError,ValueError,TypeError,sqlite3.Error):
   return None,'treaty_branch_verification_failed'
  return {**observation,'atlas_binding':VerifiedAtlasBinding(campaign,token)},None

 from fleet_dashboard import register as register_fleet
 register_fleet(app,db_path,treaty_observation,GAME,Path.home()/'Documents/Paradox Interactive/Hearts of Iron IV')

 @app.get('/world/player')
 def current_player():
  observation,reason=observation_status()
  if observation is None:return jsonify(player=None,reason=reason,
    branch_pending=bool(candidate and candidate.get('pending')))
  return jsonify(player=observation['player'],campaign=observation['campaign'],
                 day=observation['day'],date=observation['date'],source='recent_native_export',
                 can_prepare=bool(db_path and observation.get('save_token')))

 @app.post('/world/snapshots')
 def ingest_world():
  nonlocal live_player,candidate
  failure=authorize()
  if failure:return failure
  from world_snapshot import validate,digest
  body=request.get_json(silent=True) or {}
  try:
   campaign=body['campaign']; b=validate(body['snapshot'])
   if not re.fullmatch(r'[a-f0-9]{32}',campaign):raise ValueError('invalid campaign')
  except (KeyError,TypeError,ValueError):return jsonify(error='invalid_world_snapshot'),400
  b.pop('recognition_trail',None)
  from world_sessions import queue, store_snapshot
  source=campaign
  pid=body.get('live_game_pid')
  token=body.get('save_token')
  session=body.get('session_id') or (f'legacy-{source}-{pid}' if type(pid) is int else None)
  from saved_campaign_binding import matches_bridge_observation
  try:
   bound=(type(pid) is int and pid>0 and isinstance(token,str)
          and matches_bridge_observation(source,token))
  except (ValueError,OSError):
   bound=False
  if token is not None and not bound:return jsonify(error='fresh_native_observation_required'),409
  if bound and (not isinstance(session,str) or not re.fullmatch(r'[a-z0-9-]{1,80}',session)):
   return jsonify(error='invalid_world_session'),400
  if 'recognition' in b:
   from world_freshness import still_current
   if not bound or not still_current(body.get('live_proof'),pid,token,b['player'],b['day']):
    b.pop('recognition')
  b['digest']=digest(b)
  try:
   with closing(db()) as c:
    with c:
     c.execute('BEGIN IMMEDIATE')
     if bound:
      campaign=queue(c,session,source,b)
     status=store_snapshot(c,campaign,b) if campaign else 'pending'
  except ValueError as error:
   return jsonify(error=str(error)),409
  if bound:
   candidate={'session':session,'source':source,'pid':pid,'token':token,'snapshot':b,
              'received':time.monotonic(),'pending':status=='pending','proof':body.get('live_proof')}
  if status=='pending':
   live_player=None
   return jsonify(error='world_branch_binding_required',session_id=session,retained=True),409
  if type(pid) is int and pid>0 and (status=='stored' or bound):
   live_player={'pid':pid,'received':time.monotonic(),'player':b['player'],
                'campaign':campaign,'day':b['day'],'date':b['date'],
                'snapshot':b,'save_token':token if bound else None,'source':source,
                'session':session,'proof':body.get('live_proof') if bound else None}
   remember_observation(live_player)
  return jsonify(status=status,complete_batch=True)

 @app.get('/world/pending-sessions')
 def list_pending():
  if not db_path or not Path(db_path).is_file():return jsonify(sessions=[])
  uri=Path(db_path).resolve().as_uri()+'?mode=ro'
  with closing(sqlite3.connect(uri,uri=True)) as c:
   if not c.execute("SELECT 1 FROM sqlite_master WHERE name='eyes_world_pending'").fetchone():
    return jsonify(sessions=[])
   from world_sessions import pending_sessions
   return jsonify(sessions=pending_sessions(c))

 @app.post('/world/bind-branch')
 def bind_branch():
  nonlocal live_player
  if request.headers.get('Sec-Fetch-Site') not in (None,'same-origin'):
   return jsonify(error='cross_origin_request'),403
  data=request.get_json(silent=True)
  if not isinstance(data,dict) or data.get('confirmed') is not True:
   return jsonify(error='explicit_confirmation_required'),400
  session=data.get('session_id') or (candidate or {}).get('session')
  campaign=data.get('campaign')
  if (not db_path or not isinstance(session,str) or not re.fullmatch(r'[a-z0-9-]{1,80}',session)
      or (campaign is not None and (not isinstance(campaign,str) or not re.fullmatch(r'[a-f0-9]{32}',campaign)))):
   return jsonify(error='select_world_session'),409
  from world_sessions import bind,ensure_tables
  try:
   with closing(db()) as c:
    with c:
     c.execute('BEGIN IMMEDIATE')
     ensure_tables(c)
     campaign,b=bind(c,session,campaign)
  except ValueError as error:
   return jsonify(error=str(error)),409
  recent=candidate
  if recent and recent['session']==session and b:
   recent['pending']=False
   live_player={'pid':recent['pid'],'received':recent['received'],'player':b['player'],'campaign':campaign,
                'day':b['day'],'date':b['date'],'snapshot':b,'save_token':recent['token'],'source':recent['source'],
                'session':session,'proof':recent['proof']}
   remember_observation(live_player)
  return jsonify(campaign=campaign,status='bound')

 @app.post('/world/country-package')
 def prepare_country():
  if request.args.get('preview') or not native_geometry_supported():
   return jsonify(error='unverified_atlas_geometry'),409
  # The browser can only download a package. A native confirmation event gates execution.
  if request.headers.get('Sec-Fetch-Site') not in (None,'same-origin'):
   return jsonify(error='cross_origin_request'),403
  if request.content_length is None or request.content_length>1000000:
   return jsonify(error='country_request_size'),413
  body=request.get_json(silent=True)
  observation,reason=observation_status()
  if not isinstance(body,dict) or body.get('confirmed') is not True:
   return jsonify(error='explicit_confirmation_required'),400
  if (not db_path or not observation or not observation.get('save_token')
      or body.get('campaign')!=observation['campaign'] or body.get('day')!=observation['day']):
   return jsonify(error='fresh_native_observation_required',reason=reason or 'selected_snapshot_differs'),409
  try:
   _,state_by_province=geography()
   expected_states={str(state) for state in state_by_province.values()}
   if not expected_states or expected_states!=observation['snapshot']['states'].keys():
    return jsonify(error='snapshot_state_roster_mismatch'),409
  except (OSError,ValueError,TypeError):
   return jsonify(error='unverified_atlas_geometry'),409
  from country_maker_execution import compile_country
  output=BytesIO()
  with closing(db()) as c:
   with c:
    c.execute('BEGIN IMMEDIATE')
    reserved={json.loads(row[0]).get('country_tag') for row in c.execute(
     "SELECT payload FROM eyes_world_records WHERE kind='country_creation'")}
    try:package=compile_country(body,observation['snapshot'],observation['save_token'],reserved_tags=reserved)
    except (ValueError,OSError) as error:return jsonify(error=str(error)),400
    files=package.pop('files');package.pop('save_token')
    package.update(status='prepared',prepared_day=observation['day'],source_campaign=observation['source'])
    with ZipFile(output,'w',ZIP_DEFLATED) as archive:
     for path,content in files.items():archive.writestr(path,content)
     overlay=Path(__file__).resolve().parents[1]/'prepared/world_snapshot_overlay'
     for path in overlay.rglob('*'):
      if path.is_file():archive.write(path,path.relative_to(overlay).as_posix())
    c.execute('INSERT INTO eyes_world_records VALUES(?,?,?,?,?)',(
     package['id'],observation['campaign'],'country_creation',observation['date'],json.dumps(package,ensure_ascii=False)))
  output.seek(0)
  return send_file(output,mimetype='application/zip',as_attachment=True,
                   download_name='eyes_country_'+package['id']+'.zip')

 @app.post('/world/country-result')
 def country_result():
  failure=authorize()
  if failure:return failure
  body=request.get_json(silent=True) or {}
  if not db_path or not Path(db_path).exists():return jsonify(error='unknown_country_request'),404
  if not isinstance(body,dict):return jsonify(error='invalid_country_result'),400
  status=body.get('status');tag=body.get('new_tag')
  day=body.get('day');date=body.get('date')
  if type(day) is not int or day<0 or not isinstance(date,str) or not date.strip() or len(date)>100:
   return jsonify(error='invalid_country_result_date'),400
  if status not in ('blocked','partial','applied','rolled_back','rollback_failed') or not isinstance(tag,str) or (
     not re.fullmatch(r'[A-Z][A-Z0-9]{2}',tag) if status=='applied' else tag!='-'):
   return jsonify(error='invalid_country_result'),400
  identifier=body.get('id')
  if not isinstance(identifier,str) or not re.fullmatch(r'[a-f0-9]{32}',identifier):
   return jsonify(error='invalid_country_request'),400
  with closing(db()) as c:
   with c:
    c.execute('BEGIN IMMEDIATE')
    row=c.execute("SELECT campaign,payload FROM eyes_world_records WHERE id=? AND kind='country_creation'",(body.get('id'),)).fetchone()
    new_retry=row is None
    if new_retry:
     retry_path=Path(db_path).parent/'pending_country_retries'/f'{identifier}.json'
     try:
      retry=json.loads(retry_path.read_text(encoding='utf-8'))
      campaign,record=retry['campaign'],retry['record']
      original=c.execute("SELECT payload FROM eyes_world_records WHERE id=? AND campaign=? AND kind='country_creation'",
                         (record['retry_of'],campaign)).fetchone()
      if not original:return jsonify(error='unknown_country_request'),404
      original=json.loads(original[0])
      shared=('name','states','capital','country_status','actor','source_campaign','prepared_day')
      if (original['status'] not in ('partial','rollback_failed','rolled_back') or record['id']!=identifier
          or record['status']!='prepared' or any(record.get(key)!=original.get(key) for key in shared)):
       return jsonify(error='invalid_retry_binding'),409
     except (OSError,ValueError,KeyError,TypeError):return jsonify(error='unknown_country_request'),404
    else:
     campaign,record=row[0],json.loads(row[1])
    if record.get('source_campaign',campaign)!=body.get('campaign'):
     return jsonify(error='country_result_campaign_mismatch'),409
    if record['actor']!=body.get('actor') or tag==record['actor']:
     return jsonify(error='country_result_actor_mismatch'),409
    if status=='applied' and (tag!=record['country_tag'] if 'country_tag' in record else not re.fullmatch(r'D[0-9]{2}',tag)):
     return jsonify(error='country_result_tag_mismatch'),409
    result_window=8 if record.get('executor_version',0)>=3 else 7
    if status!='blocked' and not record['prepared_day']<=day<=record['prepared_day']+result_window:
     return jsonify(error='country_result_day_mismatch'),409
    if record['status'] in ('applied','partial','rolled_back','rollback_failed'):
     return jsonify(status='duplicate') if record['status']==status and record.get('new_tag')==tag else (jsonify(error='conflicting_country_result'),409)
    record.update(status=status,new_tag=tag,result_day=day,result_date=date)
    if new_retry:
     c.execute('INSERT INTO eyes_world_records VALUES(?,?,?,?,?)',(
      identifier,campaign,'country_creation',date,json.dumps(record,ensure_ascii=False)))
    else:c.execute('UPDATE eyes_world_records SET payload=? WHERE id=?',(json.dumps(record,ensure_ascii=False),identifier))
  return jsonify(status='recorded')
 def db():
  if not db_path: raise ValueError('persistent_storage_unavailable')
  c=sqlite3.connect(db_path);c.row_factory=sqlite3.Row
  c.execute('CREATE TABLE IF NOT EXISTS eyes_world_records(id TEXT PRIMARY KEY,campaign TEXT NOT NULL,kind TEXT NOT NULL,game_date TEXT NOT NULL,payload TEXT NOT NULL)')
  return c

 @app.route('/world/state-operations', methods=['GET','POST'])
 def state_operations():
  if request.headers.get('Sec-Fetch-Site') not in (None,'same-origin'):
   return jsonify(error='cross_origin_request'),403
  observed,reason=treaty_observation(for_confirmation=True)
  if not observed:
   return jsonify(error=reason),409
  if not native_geometry_supported():
   return jsonify(error='unverified_atlas_geometry'),409
  try:
   _, state_by_province=geography()
   states=observed['snapshot']['states']
   if set(states)!={str(state) for state in state_by_province.values()}:
    return jsonify(error='snapshot_state_roster_mismatch'),409
  except (OSError,ValueError,KeyError,TypeError):
   return jsonify(error='unverified_atlas_geometry'),409
  actor=observed['player']
  if request.method=='GET':
   with closing(db()) as connection:
    history=[json.loads(row[0]) for row in connection.execute(
     "SELECT payload FROM eyes_world_records WHERE campaign=? AND kind='state_operation' ORDER BY rowid DESC", (observed['campaign'],))]
   return jsonify(player=actor,campaign=observed['campaign'],day=observed['day'],
    states=sorted(int(state) for state,owners in states.items() if owners=={'owner':actor,'controller':actor}),
    history=[{k:row[k] for k in ('id','action','state','day','status')} for row in history])
  body=request.get_json(silent=True)
  if (not isinstance(body,dict) or body.get('confirmed') is not True
      or type(body.get('version')) is not int or body['version']!=1
      or body.get('campaign')!=observed['campaign'] or body.get('day')!=observed['day'] or body.get('player')!=actor):
   return jsonify(error='review_or_live_binding_changed'),409
  from state_operations import prepare
  try:
   record,packet=prepare(body.get('action'),body.get('state'),observed,body.get('previous_capital'),
                         body.get('current_level'),body.get('level'))
  except (ValueError,KeyError,TypeError) as error:
   return jsonify(error=str(error)),400
  with closing(db()) as connection, connection:
   connection.execute('BEGIN IMMEDIATE')
   existing=connection.execute("SELECT payload FROM eyes_world_records WHERE id=?", (record['id'],)).fetchone()
   if existing and json.loads(existing[0]).get('status')!='prepared':
    return jsonify(error='operation_already_resolved'),409
   if not existing:
    connection.execute('INSERT INTO eyes_world_records VALUES(?,?,?,?,?)',(
     record['id'],observed['campaign'],'state_operation',observed['date'],json.dumps(record)))
  return jsonify(packet=packet,status='prepared')

 @app.post('/world/state-operation-result')
 def state_operation_result():
  failure=authorize()
  if failure:return failure
  from state_operations import decode_result
  body=request.get_json(silent=True)
  try:
   parsed=decode_result(body['marker'])
   if parsed['day']>4_000_000:raise ValueError('invalid_game_day')
  except (KeyError,ValueError,TypeError,AttributeError):
   return jsonify(error='invalid_state_operation_result'),400
  if not db_path or not Path(db_path).exists():
   return jsonify(error='unknown_operation'),404
  with closing(db()) as connection, connection:
   connection.execute('BEGIN IMMEDIATE')
   stored=connection.execute("SELECT payload FROM eyes_world_records WHERE id=? AND kind='state_operation'",
                             (parsed['id'],)).fetchone()
   if not stored:return jsonify(error='unknown_operation'),404
   record=json.loads(stored[0])
   if (body.get('campaign')!=record['campaign'] or parsed['campaign_token']!=record['save_token']
       or any(parsed[key]!=record[key] for key in ('actor','action','state'))
       or not record['day']<=parsed['day']<=record['through_day']):
    return jsonify(error='state_operation_result_mismatch'),409
   if record.get('result')==parsed:
    return jsonify(recorded=True,duplicate=True)
   if record['status']!='prepared':
    return jsonify(error='conflicting_state_operation_result'),409
   record.update(status=parsed['status'],result=parsed)
   connection.execute("UPDATE eyes_world_records SET payload=? WHERE id=?", (json.dumps(record),record['id']))
  return jsonify(recorded=True,duplicate=False)

 @app.get('/world')
 def page():
  from ui_locale import language,translations
  return render_template('world.html',ui_language=language(),ui_translations=translations('fr'))
 @app.post('/world/locale')
 def set_locale():
  if request.headers.get('Sec-Fetch-Site') not in (None,'same-origin'):
   return jsonify(error='cross_origin_request'),403
  from ui_locale import save_language
  body=request.get_json(silent=True)
  if not isinstance(body,dict):return jsonify(error='invalid_ui_language'),400
  try:save_language(body.get('language'))
  except ValueError:return jsonify(error='invalid_ui_language'),400
  return jsonify(language=body['language'])
 def requested_map_sources():
  if request.args.get('preview') == 'more-provinces':
   from atlas_map_sources import more_provinces_preview_files
   return more_provinces_preview_files(GAME)
  return atlas_sources()

 @app.get('/world/map')
 def image():
  try:return send_file(requested_map_sources()['map/provinces.bmp'],mimetype='image/bmp',max_age=0)
  except (OSError,ValueError,TypeError,IndexError):return jsonify(error='atlas_map_unavailable'),409
 @app.get('/world/geography')
 def map_metadata():
  from hoi4_country_names import load_country_names
  preview=request.args.get('preview') == 'more-provinces'
  try:colors,states=geography(requested_map_sources()) if preview else geography()
  except DuplicateAtlasState as error:
   return jsonify(error=str(error),**error.details),409
  except (OSError,ValueError,TypeError,IndexError):return jsonify(error='atlas_map_unavailable'),409
  return jsonify(colors={str((r<<16)|(g<<8)|b):states.get(pid,0) for (r,g,b),pid in colors.items()},
                 state_ids=sorted(set(states.values())),
                 country_names={lang:load_country_names(lang) for lang in ('en','ru','fr')},
                 source='more_provinces_preview' if preview else 'configured_playset_map_not_live_ownership',
                 political_available=False,geometry_verified=not preview and native_geometry_supported())
 @app.get('/world/pick')
 def coordinate():
  try:return jsonify(pick(int(request.args['x']),int(request.args['y'])))
  except (OSError,ValueError,KeyError,TypeError):return jsonify(error='invalid_coordinate'),400
 @app.get('/world/records')
 def records():
  # Read-only browsing never migrates or opens a second diplomatic database.
  if not db_path or not Path(db_path).exists():return jsonify(records=[])
  c=sqlite3.connect(f'file:{Path(db_path).as_posix()}?mode=ro',uri=True)
  try:
   if not c.execute("SELECT 1 FROM sqlite_master WHERE name='eyes_world_records'").fetchone():return jsonify(records=[])
   rows=c.execute('SELECT id,kind,game_date,payload FROM eyes_world_records WHERE campaign=? ORDER BY rowid',(request.args.get('campaign',''),)).fetchall()
   return jsonify(records=[dict(id=r[0],kind=r[1],date=r[2],data=json.loads(r[3])) for r in rows])
  finally:c.close()
 @app.get('/world/campaigns')
 def campaigns():
  if not db_path or not Path(db_path).exists():return jsonify(campaigns=[])
  uri=Path(db_path).resolve().as_uri()+'?mode=ro'
  with closing(sqlite3.connect(uri,uri=True)) as c:
   if not c.execute("SELECT 1 FROM sqlite_master WHERE name='eyes_world_records'").fetchone():
    return jsonify(campaigns=[])
   rows=c.execute('''SELECT campaign,game_date,payload FROM eyes_world_records
     WHERE rowid IN (SELECT MAX(rowid) FROM eyes_world_records
      WHERE kind='world_snapshot' GROUP BY campaign) ORDER BY rowid DESC''').fetchall()
   versions=dict(c.execute("SELECT campaign,MAX(rowid) FROM eyes_world_records WHERE kind='recognition' GROUP BY campaign"))
  return jsonify(campaigns=[{'id':campaign,'date':date,'player':json.loads(payload).get('player'),
                            **({'recognition_revision':versions[campaign]} if campaign in versions else {})}
                            for campaign,date,payload in rows])
 @app.post('/world/recognition')
 def recognition():
  failure=authorize()
  if failure:return failure
  b=request.get_json(silent=True) or {}
  if not all(b.get(k) for k in ('campaign','game_date','observer','subject','source')):return jsonify(error='missing_evidence'),400
  allowed={'unknown','recognized','not_recognized'}
  if b.get('legal','unknown') not in allowed or b.get('de_facto') not in (None,True,False) or b.get('protest') not in (None,True,False):return jsonify(error='invalid_position'),400
  if 'day' in b and (type(b['day']) is not int or b['day'] < 0):return jsonify(error='invalid_game_day'),400
  ident=uuid.uuid4().hex
  with closing(db()) as c:
   with c:c.execute('INSERT INTO eyes_world_records VALUES(?,?,?,?,?)',(ident,b['campaign'],'recognition',b['game_date'],json.dumps(b,ensure_ascii=False)))
  return jsonify(id=ident,status='recorded_not_a_game_effect')
 @app.post('/world/country-preview')
 def preview():
  failure=authorize()
  if failure:return failure
  b=request.get_json(silent=True) or {}; ids=b.get('states',[])
  if not isinstance(ids,list) or not ids or any(type(i)!=int or i<=0 for i in ids) or b.get('capital') not in ids:return jsonify(error='invalid_states_or_capital'),400
  name=b.get('name','')
  if not isinstance(name,str) or not name.strip() or len(name)>120 or any(ord(ch)<32 for ch in name):return jsonify(error='invalid_name'),400
  status=b.get('status','independent')
  if status not in ('independent','dependent'):return jsonify(error='invalid_country_status'),400
  # A stored snapshot can qualify a draft. Execution needs a separate in-game check.
  snapshot_id=b.get('snapshot_id')
  if snapshot_id is not None:
   if not isinstance(snapshot_id,str) or not re.fullmatch(r'world:[a-f0-9]{32}:\d+',snapshot_id) or not db_path or not Path(db_path).exists():
    return jsonify(error='unknown_world_snapshot'),404
   with closing(sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=ro',uri=True)) as c:
    exists=c.execute("SELECT 1 FROM sqlite_master WHERE name='eyes_world_records'").fetchone()
    stored=c.execute("SELECT payload FROM eyes_world_records WHERE id=? AND kind='world_snapshot'",(snapshot_id,)).fetchone() if exists else None
   if stored is None:return jsonify(error='unknown_world_snapshot'),404
   snapshot=json.loads(stored[0]);player=snapshot['player']
   if any(snapshot['states'].get(str(i))!={'owner':player,'controller':player} for i in ids):
    return jsonify(error='territory_not_owned_and_controlled'),409
  return jsonify(name=name,states=list(dict.fromkeys(ids)),capital=b['capital'],country_status=status,status='blocked',execution_ready=False,cores_applied=False,
    reason='verified_live_owner_controller_and_country_executor_required',requires_separate_confirmation=True)

 return treaty_observation
