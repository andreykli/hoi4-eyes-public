"""Authored bilingual speech with explicit semantic eligibility, not treaty logic."""
from pathlib import Path
from functools import lru_cache
ROOT=Path(__file__).resolve().parents[1]
ALL={'non_aggression_pact','guarantee','military_access','embargo','lift_embargo','alliance','faction_join','territory_partition','political_annexation','white_peace','truce','peace_conference','market_access','docking_rights','air_base_access'}
DEBATE={'discuss','clarify','counter_offer'}
RULES={
 'nap':('security',{'non_aggression_pact'},DEBATE),
 'security':('security',{'non_aggression_pact','guarantee','alliance','faction_join','truce'},DEBATE),
 'guarantee':('guarantee',{'guarantee'},DEBATE),
 'access':('access',{'military_access','docking_rights','air_base_access'},DEBATE),
 'alliance':('alliance',{'alliance','faction_join'},DEBATE),
 'territory':('territory',{'territory_partition'},DEBATE),
 'trade':('trade',{'embargo','lift_embargo','market_access'},DEBATE),
 'peace':('peace',{'white_peace','truce','peace_conference'},DEBATE),
 'crisis':('ultimatum',ALL,{'reject','counter_offer','discuss'}),
 'counter':('counter_offer',ALL,{'counter_offer'}),
 'refusal':('reject',ALL,{'reject'}),
 'acceptance':('accept',ALL,{'accept'}),
 'conference':('conference_open',ALL,DEBATE),
 'adviser':('internal',ALL,{'accept','reject','counter_offer','clarify','discuss'}),
 'invitation':('invitation',ALL,{'discuss'}),
 'closing':('conclusion',ALL,{'reject','discuss'}),
 'risk':('security',ALL,{'counter_offer','discuss'}),
 'questions':('clarify',ALL,{'clarify'}),
 'economic':('trade',{'embargo','lift_embargo','market_access','military_access'},DEBATE),
 'sovereignty':('territory',{'territory_partition','political_annexation','guarantee','military_access'},DEBATE),
 'response':('compromise',ALL,{'counter_offer','discuss'}),
 'confidential':('confidentiality',ALL,{'discuss'}),
 'mediation':('mediation',ALL,DEBATE),
 'reasoning':('security',ALL,{'reject','counter_offer','discuss'}),
}
CONFERENCE={'conference','invitation','confidential','mediation'}
QUESTION_FIELDS=('duration','actor','recipient','state_ids','protected_country','conditions','exceptions','signing_authority','parties','verified_state_allocation','verified_state_allocation','agreement_version','recipient','actor','conditions','no_supported_amendment','critical_context_stale_conflicting_or_unverified','conditions','duration','recipient')

@lru_cache(maxsize=1)
def rows():
 result=[];group=None;counts={}
 for line in (ROOT/'data/diplomatic_fragments/natural_bilingual.tsv').read_text(encoding='utf8').splitlines():
  if not line.strip():continue
  if line.startswith('# group='):
   group=line.split('=',1)[1];assert group in RULES;continue
  if line.startswith('#'):continue
  if group is None:raise ValueError('Speech group missing')
  en,ru=line.split('|');counts[group]=counts.get(group,0)+1
  category,actions,stances=RULES[group]
  if group=='access' and counts[group]==7:actions={'docking_rights'}
  if group=='access' and counts[group]==8:actions={'air_base_access'}
  result.append({'id':f'voice.{group}.{counts[group]:03d}','category':category,
    'translations':{'en':en,'ru':ru},'source':'original_authored_bilingual_speech','historical_quote':False,
    'editorial_destination':'adviser_discussion' if group=='adviser' else 'politician_speech',
    'usage':'guarded_natural_speech','speech_group':group,'allowed_actions':sorted(actions),
    'allowed_stances':sorted(stances),'eligible_stages':['evaluation','clarification','counteroffer','disagreement','compromise','accepted_position','refusal'],
    'requires_context':'active_conference' if group in CONFERENCE else 'unresolved_fields' if group=='questions' else 'none',
    'requires_missing_field':QUESTION_FIELDS[counts[group]-1] if group=='questions' else None,
    'requires_source_topic':{'invitation':'invitation','confidential':'confidential','mediation':'mediation'}.get(group),
    'eligibility':['matching_action','matching_saved_stance','matching_conversation_stage','exact_core_preserved','not_a_side_clause','no_unverified_game_claims']})
 return tuple(result)

@lru_cache(maxsize=1)
def index():
 out={}
 for r in rows():
  if r['editorial_destination']!='politician_speech':continue
  for action in r['allowed_actions']:
   for stance in r['allowed_stances']:out.setdefault((action,stance),[]).append(r)
 return {key:tuple(value) for key,value in out.items()}

def eligible(point):
 if point.get('term_id') or point.get('conversation_stage') in {'confirmation_needed','refusal_maintained','awaiting_missing_information'}:return ()
 out=[]
 for r in index().get((point.get('action_id'),point.get('stance')),()):
  required=r['requires_context']
  if required=='active_conference' and not point.get('conference_active'):continue
  if required=='unresolved_fields' and not point.get('unresolved_fields'):continue
  if r['requires_missing_field'] and r['requires_missing_field'] not in point.get('unresolved_fields',[]):continue
  if r['requires_source_topic'] and r['requires_source_topic'] not in point.get('source_discussion_topics',[]):continue
  if point.get('conversation_stage') and point['conversation_stage'] not in r['eligible_stages']:continue
  # Pressure language is never introduced into an ordinary peaceful proposal.
  if r['speech_group']=='crisis' and not point.get('pressure_in_source'):continue
  out.append(r)
 return tuple(out)
