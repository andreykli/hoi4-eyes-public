"""Connect existing ActionRegistry actions to wording; never prepare or execute one."""
from pathlib import Path
from functools import lru_cache
import json
ROOT=Path(__file__).resolve().parents[1]
@lru_cache(maxsize=1)
def bindings():
 data=json.loads((ROOT/'data/diplomatic_fragments/mechanic_bindings.json').read_text(encoding='utf-8-sig'))
 from action_registry import ActionRegistry
 registry=ActionRegistry()
 if data.get('schema_version')!=1 or any(not registry.get(k) for k in data['actions']):raise ValueError('Unknown mechanic binding')
 return data

def allowed_categories():
 b=bindings();return {v['root']+'_'+phase for v in b['actions'].values() for phase in b['phases']}
def suffixes(row,language):
 if language not in {'en','ru'} or row.get('term_id'):return []
 b=bindings();spec=b['actions'].get(row.get('action_id'));phase=row.get('stance')
 if not spec or phase not in b['automatic_suffix_phases']:return []
 from diplomatic_catalog import phrase_index
 category=spec['root']+'_'+phase
 return [r['translations'][language] for r in phrase_index()[0].get((category,language),())]

def status(action_id):
 from action_registry import ActionRegistry
 a=ActionRegistry().get(action_id)
 if not a:return {'action_id':action_id,'known':False,'may_execute':False}
 return {'action_id':action_id,'known':True,'native_effect':a['vanilla_effect'],
         'required_context':a['required_context'],'safety_guards':a['safety_guards'],
         'manual_confirmation_required':a['manual_confirmation_required'],
         'legacy_registry_live_claim':a.get('live_verified',False),
         'legacy_live_scope':a.get('live_verification_scope'),
         'wording_binding':bindings()['actions'].get(action_id),
         'may_execute':False,'notice':'This lookup is not approval, preparation, or in-game execution.'}
