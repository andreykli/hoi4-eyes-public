"""Bounded, native leader identity evidence. Identity never grants treaty consent."""
import re
from copy import deepcopy
from historical_people import load_people
MARKER='EYES_LEADER_V1|'
def parse(line):
 if MARKER not in line:return None
 fields=line.split(MARKER,1)[1].strip().split('|')
 if len(fields)!=5:raise ValueError('Invalid leader marker')
 token,country,seq,day,leader=fields
 if not re.fullmatch(r'\d+-\d+-\d+-\d+',token) or not re.fullmatch(r'[A-Z0-9]{3}',country):raise ValueError('Invalid leader identity')
 if not seq.isdecimal() or not day.isdecimal() or len(seq)>12 or len(day)>12:raise ValueError('Invalid leader clock')
 if not re.fullmatch(r'[A-Za-z0-9_]{1,120}',leader):raise ValueError('Invalid leader key')
 return {'campaign_token':token,'country':country,'sequence':int(seq),'day':int(day),'leader_id':None if leader=='unknown' else leader}
def record(engine,campaign,row):
 if not re.fullmatch(r'[0-9a-f]{32}',campaign):raise ValueError('Invalid campaign')
 country=row.get('country');leader=row.get('leader_id')
 if not isinstance(country,str) or not re.fullmatch(r'[A-Z0-9]{3}',country):raise ValueError('Invalid country')
 if type(row.get('day')) is not int or row['day']<0 or type(row.get('sequence')) is not int or row['sequence']<1:raise ValueError('Invalid observed clock')
 if leader is not None and not any(leader in p['game_ids'] for p in load_people().values()):raise ValueError('Leader key not in reviewed export catalog')
 chats=[c for c in engine.chats.values() if c['campaign_id']==campaign and country in c['participants']]
 if not chats:raise ValueError('No matching campaign chat')
 from historical_conferences import Conferences
 clocks=[]
 for c in chats:
  try:clocks.append(Conferences(engine).clock(c)['day'])
  except ValueError:pass
 if not clocks or row['day']!=max(clocks):raise ValueError('Leader evidence must match current observed game day')
 state=Conferences(engine).state;index=state.setdefault('observed_leaders',{}).setdefault(campaign,{})
 old=index.get(country);fingerprint={k:row[k] for k in ('country','sequence','day','leader_id')}
 if old:
  if old['day']>row['day']:raise ValueError('Stale leader evidence')
  if old['day']==row['day'] and old['sequence']==row['sequence']:
   if any(old[k]!=v for k,v in fingerprint.items()):raise ValueError('Conflicting leader replay')
   return deepcopy(old)
  if old['day']==row['day'] and old['sequence']>row['sequence']:raise ValueError('Stale leader sequence')
 current={**fingerprint,'source':'authenticated_bridge_native_leader_match','authority_to_sign':False}
 index[country]=current
 if not old or old['leader_id']!=leader:
  state.setdefault('leader_history',{}).setdefault(campaign,[]).append(deepcopy(current))
 if old and old['leader_id'] and leader and old['leader_id']!=leader:
  for c in chats:
   conf=Conferences(engine).active(c['chat_id'])
   if conf:conf.update(status='interrupted',outcome='leadership_changed')
   if c['player_country']==country:
    c['state']='closed';c['closure_reason']='observed_leadership_change'
 return deepcopy(current)
def generate():
 # Current country scope; ROOT remains player when called from the pair observer.
 rows=['eyes_observe_known_leader = {']
 ids=sorted({gid for p in load_people().values() for gid in p['game_ids']})
 for i,gid in enumerate(ids):
  if not re.fullmatch(r'[A-Za-z0-9_]+',gid):raise ValueError('Unsafe game ID')
  rows.append((' if' if i==0 else ' else_if')+' = { limit = { has_country_leader = { character = '+gid+' ruling_only = yes } } log = "EYES_LEADER_V1|[?global.eyes_campaign_seed_1|0]-[?global.eyes_campaign_seed_2|0]-[?global.eyes_campaign_seed_3|0]-[?global.eyes_campaign_seed_4|0]|[THIS.GetTag]|[?ROOT.eyes_pair_context_sequence|0]|[?global.num_days|0]|'+gid+'" }')
 rows+=[' else = { log = "EYES_LEADER_V1|[?global.eyes_campaign_seed_1|0]-[?global.eyes_campaign_seed_2|0]-[?global.eyes_campaign_seed_3|0]-[?global.eyes_campaign_seed_4|0]|[THIS.GetTag]|[?ROOT.eyes_pair_context_sequence|0]|[?global.num_days|0]|unknown" }','}']
 return '\n'.join(rows)+'\n'

def attribution(engine,chat,country=None):
 country=country or chat['player_country']
 from historical_conferences import Conferences
 try:clock=Conferences(engine).clock(chat)
 except ValueError:return {'basis':'current_leader_unverified','person_id':None}
 row=Conferences(engine).state.get('observed_leaders',{}).get(chat['campaign_id'],{}).get(country)
 if not row or row['day']!=clock['day'] or not row['leader_id']:return {'basis':'current_leader_unverified','person_id':None}
 from historical_people import representative
 result=representative(country,clock['date'],row['leader_id'])
 return {**result,'observation_day':row['day'],'text_unchanged':True,'authority_to_sign':False}
