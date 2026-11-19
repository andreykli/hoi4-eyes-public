"""Complete native territory batches, separate from sparse diplomatic pairs."""
import re,time,hashlib,json
from datetime import datetime
from copy import deepcopy
MARKER='EYES_WORLD_V1|'
TAG=re.compile(r'[A-Z][A-Z0-9]{2}')

def recent_native_line(line, now=None):
    """Use the game's wall-clock prefix, rather than a snapshot read from SQLite."""
    match = re.match(r'^\[(\d{2}):(\d{2}):(\d{2})\]\[', line)
    if not match:
        return False
    hour, minute, second = map(int, match.groups())
    if hour > 23 or minute > 59 or second > 59:
        return False
    now = now or datetime.now()
    age = (now.hour*3600 + now.minute*60 + now.second - hour*3600 - minute*60 - second) % 86400
    return age <= 10
def validate(b):
 if not isinstance(b,dict) or type(b.get('day'))!=int or b['day']<0:raise ValueError('invalid world day')
 countries=b.get('countries');states=b.get('states')
 if not isinstance(countries,list) or not countries or len(countries)>4096 or any(not isinstance(t,str) or not TAG.fullmatch(t) for t in countries) or len(set(countries))!=len(countries):raise ValueError('invalid world roster')
 roster=set(countries)
 if not isinstance(b.get('player'),str) or b['player'] not in roster:raise ValueError('missing player in world roster')
 if not isinstance(states,dict) or not states or len(states)>20000:raise ValueError('invalid world states')
 for sid,row in states.items():
  if (not isinstance(sid,str) or not re.fullmatch(r'[1-9][0-9]{0,8}',sid) or not isinstance(row,dict)
      or any(not isinstance(row.get(k),str) or row[k] not in roster for k in ('owner','controller'))):
   raise ValueError('invalid state allocation')
 if not isinstance(b.get('date'),str) or not b['date'].strip() or len(b['date'])>100 or any(ord(c)<32 for c in b['date']):raise ValueError('invalid world date')
 if 'naval' in b:
  from fleet_dashboard import validate_totals
  validate_totals(b['naval'])
 if 'cabinet' in b:
  from cabinet_roster import validate_roster
  b={**b,'cabinet':validate_roster(b['cabinet'])}
 if 'recognition' in b:
  from native_recognition import validate as validate_recognition
  validate_recognition(b['recognition'],b['player'],roster)
 return deepcopy(b)
class Collector:
 def __init__(self):self.pending={}
 def feed(self,line):
  if MARKER not in line:return None
  parts=line.split(MARKER,1)[1].strip().split('|')
  if len(parts)<4 or len(line)>2048:raise ValueError('invalid world marker')
  kind,token,seq,*data=parts
  from hoi4_campaign_identity import campaign_id_from_save_token
  campaign_id_from_save_token(token)
  if not seq.isdigit():raise ValueError('invalid world sequence')
  key=(token,seq);now=time.monotonic();self.pending={k:v for k,v in self.pending.items() if now-v['started']<120}
  try:
   if kind=='BEGIN':
    if len(data)!=3 or self.pending:raise ValueError('world export already pending or header invalid')
    self.pending[key]={'started':now,'data':{'day':int(data[0]),'date':data[1],'player':data[2],'countries':[],'states':{}}};return None
   if key not in self.pending:raise ValueError('world header missing')
   b=self.pending[key]['data']
   if kind=='COUNTRY':
    if len(data)!=1 or data[0] in b['countries'] or len(b['countries'])>=4096:raise ValueError('duplicate country')
    b['countries'].append(data[0])
   elif kind=='STATE':
    if len(data)!=3 or data[0] in b['states'] or len(b['states'])>=20000:raise ValueError('duplicate state')
    b['states'][data[0]]={'owner':data[1],'controller':data[2]}
   elif kind=='RECOGNITION':
    from native_recognition import STATUSES
    values=b.setdefault('recognition',{})
    if (len(data)!=2 or not TAG.fullmatch(data[0]) or data[1] not in STATUSES
        or data[0] in values or len(values)>=4096):
     self.pending[key]['recognition_invalid']=True
    else:values[data[0]]=data[1]
   elif kind=='NAVY':
    # Optional observation. A missing or unsupported counter cannot destroy Atlas data.
    from fleet_dashboard import parse_totals
    transfer=self.pending[key]
    try:
     if transfer.get('naval_seen'):raise ValueError('duplicate naval observation')
     b['naval']=parse_totals(data)
    except ValueError:b.pop('naval',None)
    transfer['naval_seen']=True
   elif kind in ('CABINET','CABINET_END'):
    from cabinet_roster import ROSTER_LIMIT, validate_roster
    transfer=self.pending[key]
    rows=transfer.setdefault('cabinet_rows',[])
    if transfer.get('cabinet_closed'):
     b.pop('cabinet',None)
    elif kind=='CABINET':
     if len(rows)<ROSTER_LIMIT:rows.append(data)
     else:transfer['cabinet_overflow']=True
    else:
     transfer['cabinet_closed']=True
     if (not transfer.get('cabinet_overflow') and len(data)==1 and data[0]==str(len(rows))):
      valid=[]
      for row in rows:
       if len(row)!=2:continue
       try:valid.extend(validate_roster([{'role':row[0],'name':row[1]}]))
       except ValueError:continue
      b['cabinet']=valid
   elif kind=='END':
    if len(data)!=2 or [len(b['countries']),len(b['states'])]!=list(map(int,data)):raise ValueError('incomplete world batch')
    if self.pending[key].get('recognition_invalid'):b.pop('recognition',None)
    result={'campaign_token':token,'sequence':int(seq),'snapshot':validate(b)};del self.pending[key];return result
   else:raise ValueError('unknown world marker')
  except (ValueError,TypeError):self.pending.pop(key,None);raise
  return None

def digest(snapshot):
    # Roster order carries no game state. Accept legacy stored digests on reads.
    payload={key:value for key,value in snapshot.items() if key not in ('digest','naval','cabinet','recognition')}
    payload['countries']=sorted(payload['countries'])
    return hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
