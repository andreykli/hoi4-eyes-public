"""Sourced expressive priors; no authority to select or amend diplomatic policy."""
import json
from pathlib import Path
from functools import lru_cache
from datetime import date,datetime
from historical_people import load_people,eligibility
ROOT=Path(__file__).resolve().parents[1]

@lru_cache(maxsize=1)
def _people():return load_people()

@lru_cache(maxsize=1)
def evidence():
 data=json.loads((ROOT/'data/history/behavior_evidence.json').read_text(encoding='utf8'))
 people=load_people();seen=set()
 for f in data['features']:
  key=(f['person'],f['id'])
  if f['person'] not in people or key in seen or not f.get('claim') or not f['source'].startswith('https://'):
   raise ValueError('Invalid historical behavioral provenance')
  date.fromisoformat(f['from']);seen.add(key)
  if set(f['argument'])!={'ru','en'}:raise ValueError('Missing authored translation')
 return tuple(data['features'])

def calendar(value):
 for fmt in ('%Y-%m-%d','%B %d, %Y'):
  try:return datetime.strptime(str(value),fmt).date().isoformat()
  except ValueError:pass
 return None

def active_features(person_id,action_id,calendar_date,identity_verified=False):
 day=calendar(calendar_date)
 if not identity_verified or not day:return ()
 person=_people().get(person_id)
 if not person or eligibility(person,day)!='eligible':return ()
 return tuple(f for f in evidence() if f['person']==person_id and action_id in f['actions'] and day>=f['from'])

def feature_text(person_id,feature_id,language):
 return next((f['argument'].get(language) for f in evidence()
              if f['person']==person_id and f['id']==feature_id),None)

def audit():
 features=evidence();out=[]
 for p in load_people().values():
  fs=[f for f in features if f['person']==p['id']]
  out.append({'person_id':p['id'],'name':p['name'],'country':p['country'],
   'birth':p.get('birth'),'birth_year':p.get('birth_year'),'death':p.get('death'),
   'life_dates_precision':'day' if p.get('birth') and p.get('death') else 'bounded_birth_year' if p.get('birth_year') and p.get('death') else 'incomplete',
   'behavior_status':'sourced_limited_profile' if fs else 'insufficient_reviewed_behavioral_evidence',
   'features':[dict(f) for f in fs],'existing_biography_sources':p['sources'],
   'game_ids':p['game_ids'],'risk_preference':None,'temperament':None,
   'speech_style_evidence':[f['speech_evidence'] for f in fs if f.get('speech_evidence')],
   'unknown_fields':['risk_preference','private_temperament','complete_ideology','language_fluency'],
   'historical_quote':False,'changes_state_decision':False})
 return {'people_reviewed':len(out),'limited_profiles':sum(bool(r['features']) for r in out),
         'policy':'Audit of stored fields plus targeted behavioral evidence; existing biography URLs are not automatically re-certified as behavioral sources.',
         'people':out}
