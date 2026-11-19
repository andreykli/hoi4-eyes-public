"""Bounded expressive history in the existing chat snapshot, not treaty memory."""
from copy import deepcopy
from difflib import SequenceMatcher
import hashlib,re,unicodedata
MAX_ROWS=96
MAX_RECENT=32

def normal(text):return ' '.join(re.findall(r'\w+',unicodedata.normalize('NFKC',str(text)).casefold()))
def envelope(text,core):
 if not isinstance(text,str) or not isinstance(core,str) or text.count(core)!=1:return None
 return normal(text.replace(core,' ',1))[:1200]
def history(chat,country,person_id=None):
 memory=chat.get('phrase_memory') or {}
 rows=memory.get('rows',[]) if isinstance(memory,dict) else []
 return deepcopy([r for r in rows if isinstance(r,dict) and r.get('country')==country and r.get('person_id')==person_id][-MAX_RECENT:])
def commit(chat,entry,choices,person_id=None):
 # Called only for delivered text. Model drafts and failed generations are not usage.
 if not isinstance(choices,list):return
 memory=chat.setdefault('phrase_memory',{'schema_version':1,'rows':[]})
 if memory.get('schema_version')!=1 or not isinstance(memory.get('rows'),list):raise ValueError('Invalid phrase memory')
 rows=memory['rows']
 for choice in choices[:12]:
  text=choice.get('text');core=choice.get('core')
  if not isinstance(text,str) or text not in str(entry.get('text','')) or len(text)>600:continue
  signature=envelope(text,core)
  if not signature:continue # Mandatory legal wording is never penalised.
  row={'request_id':entry['request_id'],'point':choice.get('point'),'country':entry['speaker'],
       'person_id':person_id,'language':choice.get('language'),'action_id':choice.get('action_id'),
       'stance':choice.get('stance'),'envelope':signature,'style':choice.get('style'),
       'fingerprint':hashlib.sha256(signature.encode()).hexdigest()[:20],
       'opening':normal(text.split(core,1)[0]),'ending':normal(text.split(core,1)[1])}
  prior=next((r for r in rows if all(r.get(k)==row[k] for k in ('request_id','point','country'))),None)
  if prior is not None:continue # Retry does not count as a fresh diplomatic turn.
  rows.append(row)
 memory['rows']=rows[-MAX_ROWS:]
def recently_used(text,core,rows,language,request_id):
 candidate=envelope(text,core)
 if not candidate:return False
 opening,ending=(normal(t) for t in text.split(core,1))
 recent=[r for r in rows[-MAX_RECENT:] if r.get('language')==language and r.get('request_id')!=request_id]
 for row in recent[-2:]:
  if opening and row.get('opening')==opening:return True
  if ending and row.get('ending')==ending:return True
 for row in rows[-MAX_RECENT:]:
  if row.get('request_id')==request_id or row.get('language')!=language:continue
  old=row.get('envelope','')
  if candidate==old:return True
  # Lexical closeness only; never presented as full semantic understanding.
  matcher=SequenceMatcher(None,candidate,old,autojunk=False)
  if matcher.real_quick_ratio()>.86 and matcher.quick_ratio()>.86 and matcher.ratio()>.86:return True
 return False

def expression_style(context,row):
 # Known traits can affect expression, never the saved position.
 trusted=context.get('leader_expression_style') or []
 aliases={'formal':'official','concise':'reserved','direct':'firm','cautious':'cautious','conciliatory':'conciliatory'}
 for style in trusted if isinstance(trusted,list) else []:
  if style in aliases:return aliases[style]
 if row.get('stance')=='clarify':return 'cautious'
 if row.get('stance')=='reject':return 'firm'
 facts=context.get('known_game_context') or {}
 if facts.get('war_status') in (True,'yes'):return 'tense'
 try:
  relation=float(facts['relations'])
  if relation>=40:return 'friendly'
  if relation<=-40:return 'reserved'
 except (KeyError,TypeError,ValueError):pass
 if row.get('stance')=='counter_offer':return 'conciliatory'
 if context.get('negotiation_stage')=='confirmation':return 'protocol'
 return 'official'
