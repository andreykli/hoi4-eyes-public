"""Typed nonbinding opening compromise; never converts counter_offer to accept."""
def opening_terms(action, plan, context):
    if plan.get('selected_action') == 'accept': return None
    if action != 'non_aggression_pact' or plan.get('selected_action') != 'counter_offer': return False
    if any(context.get(k) is not False for k in ('war_status', 'same_faction', 'existing_non_aggression_pact')): return False
    if not any(x.get('kind') == 'limited_duration' and x.get('eligible') is True for x in plan.get('alternatives', [])): return False
    risk = plan.get('risk_assessment', {}).get('score')
    if type(risk) not in (int, float): return False
    years = 1 if risk >= .25 else 3
    return [{'term_id': 'non_aggression_pact', 'duration': f'{years} years'}]
