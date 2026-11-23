"""Typed acceptance policy. Player prose and model votes are never inputs."""
from math import isfinite
import re


def decision(point, facts, speaker, strategy=None):
    action=point.get('action_id')
    actor=(point.get('actors') or [''])[0]
    recipient=point.get('recipient')
    def result(vote,reason):
        return {'decision':vote,'reason':reason,'counter':'omit_term' if vote=='counteroffer' else 'none','questions':[]}
    supported = {'non_aggression_pact','guarantee','military_access','white_peace',
                 'faction_join','faction_leave','faction_create','faction_dismantle',
                 'cancel_nap','cancel_guarantee','cancel_military_access','embargo','lift_embargo'}
    if action not in supported:
        return result('defer','insufficient_context')
    if point.get('unresolved_fields') or point.get('conditions') or point.get('exceptions'):
        return result('defer','insufficient_context')
    war=facts.get('war_status')
    if action=='white_peace':
        if war in (True,'yes','at_war'):
            return result('accept','reduced_tensions')
        return result('defer','insufficient_context')
    if war in (True,'yes','at_war'):
        return result('reject','security_risk')
    if war not in (False,'no','not_at_war'):
        return result('defer','insufficient_context')
    relations=facts.get('relations')
    if isinstance(relations,str) and len(relations)<=12 and re.fullmatch(r'-?\d{1,3}(?:\.\d+)?',relations):
        relations=float(relations)
    if isinstance(relations,bool) or not isinstance(relations,(int,float)) or not isfinite(relations):
        return result('defer','insufficient_context')
    if not -200<=relations<=200:
        return result('defer','insufficient_context')
    if relations < -60:
        return result('reject','insufficient_trust')
    if action in {'faction_join','faction_leave','faction_create','faction_dismantle',
                  'cancel_nap','cancel_guarantee','cancel_military_access','embargo','lift_embargo'}:
        required={'faction_join':'same_faction','faction_leave':'same_faction',
            'faction_create':'same_faction','faction_dismantle':'same_faction',
            'cancel_nap':'existing_non_aggression_pact','cancel_guarantee':'existing_guarantee',
            'cancel_military_access':'existing_military_access','embargo':'existing_embargo','lift_embargo':'existing_embargo'}[action]
        if facts.get(required) not in (True,False,'yes','no'):
            return result('defer','insufficient_context')
        if not isinstance(strategy,dict) or strategy.get('authority')!='advisory_engine_strategic_plan':
            return result('defer','insufficient_context')
        vote={'counter_offer':'counteroffer','defer':'defer','accept':'accept','reject':'reject'}.get(strategy.get('selected_action'),'defer')
        return result(vote,{'accept':'mutual_security','reject':'security_risk',
                           'counteroffer':'limits_on_commitments','defer':'insufficient_context'}[vote])
    if isinstance(strategy,dict) and strategy.get('selected_action')=='reject':
        return result('reject','security_risk')
    if action=='non_aggression_pact':
        return result('accept','mutual_security') if relations>=-30 else result('counteroffer','insufficient_trust')
    if action in {'guarantee','military_access'}:
        if speaker==recipient and speaker!=actor:
            return result('accept','defensive_assurance')
        if speaker==actor and actor!=recipient:
            threshold=25 if action=='guarantee' else 10
            return result('accept','mutual_security') if relations>=threshold else result('counteroffer','limits_on_commitments')
    return result('defer','insufficient_context')
