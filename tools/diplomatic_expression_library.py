"""Indexed original phrasing; it cannot decide, add, or execute treaty terms."""
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path

@lru_cache(maxsize=1)
def index():
    rows=json.loads((Path(__file__).resolve().parents[1]/'data/diplomatic_expression_library.json').read_text(encoding='utf-8'))['entries']
    result={}
    for row in rows:result.setdefault((row['action'],row['stance']),[]).append(row)
    return result

def search(core, row, context, language, limit=3):
    stance={'defer':'discuss','counteroffer':'counter_offer'}.get(row.get('stance'),row.get('stance'))
    choices=index().get((row.get('action_id'),stance),index().get(('general',stance),[]))
    if not choices:return [core]
    from diplomatic_phrases import tone_for
    tone=tone_for(context,row)
    seed=str(context.get('request_id',''))+str(row.get('point',''))
    choices=sorted(choices,key=lambda r:(r['tone']!=tone,sha256((seed+r['id']).encode()).hexdigest()))
    recent={m.get('text') for m in context.get('recent_messages',[]) if isinstance(m,dict)}
    texts=[r['texts'].get(language,r['texts']['en']).replace('{position}',core) for r in choices]
    texts=[t for t in texts if len(t)<=600]
    return (sorted(texts,key=lambda t:t in recent) or [core])[:limit]

def unavailable(language, action='general'):
    core={'en':'Please specify the exact obligations and the countries concerned. No agreement has been confirmed.',
          'ru':'Просим указать точные обязательства и участвующие страны. Согласие по договору не подтверждено.',
          'fr':'Veuillez préciser les engagements exacts et les pays concernés. Aucun accord n’a été confirmé.'}.get(language)
    if core is None:core='Please clarify the exact obligations. No agreement has been confirmed.'
    return search(core,{'action_id':action,'stance':'clarify'},{},language,1)[0]
