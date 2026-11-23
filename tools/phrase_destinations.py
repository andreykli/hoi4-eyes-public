"""Conservative destination labels, separate from diplomatic semantic categories."""
SPEECH_IDS=frozenset({'greeting.1','greeting.3','greeting.5','greeting.6','invitation.1','invitation.5','reject.1','reject.4','exp.invitation.01','exp.invitation.04','exp.conclusion.02'})
# Editorially reviewed IDs. An empty action set means any root action;
# applicability is checked against the saved stance, not inferred from prose.
CHAT_RULES={
 'accept.1':({'accept'},set()),'accept.4':({'accept'},set()),'accept.6':({'accept'},set()),
 'reject.1':({'reject'},set()),'reject.2':({'reject'},set()),'reject.4':({'reject'},set()),
 'reject.5':({'reject'},set()),'reject.6':({'reject'},set()),
 'exp.reject.02':({'reject'},set()),'exp.reject.03':({'reject'},set()),
 'exp.reject.04':({'reject'},set()),'exp.reject.05':({'reject'},set()),
 'counter_offer.2':({'counter_offer'},set()),'counter_offer.6':({'counter_offer'},set()),
 'exp.counter.03':({'counter_offer'},set()),'exp.counter.05':({'counter_offer'},set()),
 'exp.security.01':({'reject','counter_offer','discuss'},{'non_aggression_pact','truce'}),
 'exp.security.05':({'clarify'},{'non_aggression_pact','truce'}),
 'exp.guarantee.02':({'accept','reject','discuss'},{'guarantee'}),
 'exp.guarantee.06':({'reject','discuss'},{'guarantee'}),
 'exp.wavec_guarantee.02':({'clarify','discuss'},{'guarantee'}),
 'exp.wavec_guarantee.05':({'accept','reject','discuss'},{'guarantee'}),
 'exp.access.01':({'accept','discuss'},{'military_access'}),
 'exp.access.06':({'accept','discuss'},{'military_access'}),
 'exp.wavec_access.05':({'accept','discuss'},{'military_access'}),
 'exp.trade.03':({'discuss','clarify'},{'embargo','lift_embargo'}),
 'exp.trade.06':({'accept','discuss'},{'embargo','lift_embargo'}),
 'exp.wavec_trade.05':({'clarify','discuss'},{'embargo','lift_embargo'}),
 'exp.alliance.03':({'clarify','discuss'},{'alliance','faction_join'}),
 'exp.alliance.05':({'accept','discuss'},{'alliance','faction_join'}),
 'exp.wavec_alliance.01':({'discuss'},{'alliance','faction_join'}),
 'exp.peace.01':({'clarify','discuss'},{'white_peace','truce','peace_conference'}),
 'exp.peace.04':({'clarify','discuss'},{'white_peace','truce','peace_conference'}),
 'exp.wavec_peace.03':({'clarify','discuss'},{'white_peace','truce','peace_conference'}),
 'exp.territory.01':({'clarify'},{'territory_partition'}),
 'exp.wavec_territory.01':({'clarify'},{'territory_partition'}),
}

def chat_eligible(fragment,point):
 if point.get('term_id') or fragment['id'] not in CHAT_RULES:return False
 if fragment['id']=='exp.territory.01' and point.get('state_ids'):return False
 stances,actions=CHAT_RULES[fragment['id']]
 return point.get('stance') in stances and (not actions or point.get('action_id') in actions)
def destination(row):
 if row.get('editorial_destination') in {'politician_speech','adviser_discussion'}:return row['editorial_destination']
 if row['id'] in SPEECH_IDS or row['id'] in CHAT_RULES:return 'politician_speech'
 cat=row['category'];text=row['translations'].get('en','')
 if cat in {'internal','delegation'}:return 'adviser_discussion'
 if cat=='clarify' or cat.endswith('_clarify'):return 'clarifying_question'
 if cat in {'drafting','confirmation','mandate','procedure'} or cat.endswith('_review'):return 'legal_wording'
 if cat in {'implementation','evidence','intelligence','leak','confidentiality'}:return 'system_check'
 if cat in {'conference_open','conclusion','reschedule','address'}:return 'interface_notice'
 if text.endswith('?'):return 'clarifying_question'
 # Unreviewed instructional statements are never granted a political voice.
 return 'system_check'
def speech_allowed(row):return destination(row)=='politician_speech'
