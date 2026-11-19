"""Two-round constrained delegation review. No invented biographies or treaty authority."""
from copy import deepcopy
from historical_people import eligibility

def deliberate(delegates,people,positions,goal,calendar,personal_trust=None):
 turns=[];proposals=[];opinions=[]
 for d in delegates:
  p=people.get(d.get('person_id'))
  if not p or not calendar or eligibility(p,calendar)!='eligible':continue
  roles=[r['role'] for r in p['offices'] if r['start']<=calendar<r['end']]
  sourced_trade=any(v.get('topic')=='trade_liberalization' and v.get('source') for v in p.get('views',[]))
  for position in positions:
   point=position.get('point');stance=position['stance']
   issue='trade_liberalization' if goal=='embargo' and sourced_trade else 'missing_terms' if position.get('unresolved_fields') else 'mandate_limits' if not d.get('authority_to_sign') else 'state_interests'
   personal='discuss' if issue in {'trade_liberalization','missing_terms'} else stance
   key=f"{p['id']}:{point}"
   turns.append({'turn_id':key+':1','round':1,'person_id':p['id'],'point':point,'speech_act':'object' if personal!=stance else 'support_with_limits','issue':issue,'basis':'sourced_view' if issue=='trade_liberalization' else 'procedural_role','historical_roles':roles,'historical_quote':False,'binding':False})
   proposal={'proposal_id':key,'point':point,'kind':'request_economic_review' if issue=='trade_liberalization' else 'request_clarification' if issue=='missing_terms' else 'preserve_engine_conditions','proposer':p['id'],'changes_treaty':False}
   proposals.append(proposal)
   turns.append({'turn_id':key+':2','round':2,'person_id':p['id'],'point':point,'speech_act':'maintain_reservation' if personal!=stance else 'acknowledge_limits','responds_to':[t['turn_id'] for t in turns if t['round']==1 and t['person_id']!=p['id'] and t['point']==point], 'resolution':'refer_to_engine_new_plan_required' if personal!=stance else 'retain_exact_conditions','binding':False,'historical_quote':False})
   opinions.append({'person_id':p['id'],'display_name':p['name'],'point':point,'personal_stance':personal,'official_stance':stance,'basis':'sourced_prior_is_advisory_not_state_policy' if issue=='trade_liberalization' else 'state_position_review','binding':False,'historical_quote':False})
 first=[t for t in turns if t['round']==1];second=[t for t in turns if t['round']==2]
 for t in second:t['responds_to']=[v['turn_id'] for v in first if v['person_id']!=t['person_id'] and v['point']==t['point']]
 turns=first+second
 for t in turns:
  if t['speech_act']=='object':
   en,ru=('I fear that trade restrictions could close off opportunities for cooperation.','Боюсь, что ограничения торговли могут закрыть возможности для сотрудничества.') if t['issue']=='trade_liberalization' else ('We should clarify the unresolved terms before giving consent.','До согласия следует уточнить нерешённые условия.')
  elif t['speech_act']=='maintain_reservation':en,ru='My doubts remain. I would like us to examine the substance before going further.','Мои сомнения остаются. Хотелось бы разобраться в существе вопроса, прежде чем двигаться дальше.'
  elif t['speech_act']=='support_with_limits':en,ru='On balance I support this position, while recognising the limits of my own role.','В целом я поддерживаю эту позицию, понимая пределы собственной роли.'
  else:en,ru='I have heard my colleagues, but still regard this position as the more reasonable course.','Я выслушал коллег, но пока считаю эту позицию более разумной.'
  texts={'en':en,'ru':ru}
  if t['round']==1 and t['speech_act']=='support_with_limits':
   from natural_bilingual import rows as speech_rows
   import hashlib
   choices=[r for r in speech_rows() if r['speech_group']=='adviser' and r['id']!='voice.adviser.004']
   selected=choices[int(hashlib.sha256((t['turn_id']+goal+str(t['issue'])).encode()).hexdigest()[:8],16)%len(choices)]
   texts=dict(selected['translations']);t['fragment_id']=selected['id']
  if t['round']==2 and t['responds_to'] and t['speech_act']=='acknowledge_limits':
   from natural_bilingual import rows as speech_rows
   selected=next(r for r in speech_rows() if r['id']=='voice.adviser.004')
   texts=dict(selected['translations']);t['fragment_id']=selected['id']
  from historical_behavior import active_features
  personal=active_features(t['person_id'],goal,calendar,identity_verified=True)
  if personal and t['round']==1:
   texts=dict(personal[0]['argument'])
   t.pop('fragment_id',None)
   t['historical_argument_basis']={'feature':personal[0]['id'],'source':personal[0]['source'],'interpretation_not_quotation':True}
  t['display_text']=texts

 # Personal friendship is deliberately excluded from approval of state terms.
 return {'rounds':2,'turns':turns,'proposals':proposals,'opinions':opinions,'consensus':deepcopy(positions),'consensus_basis':'existing_engine_plan_not_majority_vote','dissent':[o for o in opinions if o['personal_stance']!=o['official_stance']],'new_plan_required':any(p['kind']!='preserve_engine_conditions' for p in proposals),'authority_to_sign':False}
