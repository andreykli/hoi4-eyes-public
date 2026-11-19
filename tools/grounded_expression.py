"""Finite grounded expression. Canonical decisions remain byte-for-byte intact."""
from pathlib import Path
import json
from functools import lru_cache
@lru_cache(maxsize=1)
def profiles():
 result=json.loads((Path(__file__).resolve().parents[1]/'data/history/behavior_profiles.json').read_text(encoding='utf-8-sig'))['profiles']
 from historical_behavior import evidence
 for f in evidence():
  p=result.setdefault(f['person'],{'risk_preference':None,'speech_style':None,'sources':[]})
  p.setdefault('features',[]).append(f)
  p['sources']=list(dict.fromkeys([*p.get('sources',[]),f['source']]))
 return result

STYLES={
 'official':('Наше правительство судит об этом предложении по его существу.','Our government judges this proposal on its merits.'),
 'cautious':('Мы предпочитаем взвесить последствия, а не полагаться на обнадёживающие слова.','We prefer to weigh the consequences rather than rely on reassuring words.'),
 'friendly':('Мы хотим, чтобы этот разговор помог нам лучше понять друг друга.','We want this conversation to help us understand one another better.'),
 'reserved':('Красивые обещания сами по себе нас не убеждают.','Fine promises alone do not persuade us.'),
 'tense':('Сейчас особенно важно говорить спокойно и без двусмысленности.','It is especially important now to speak calmly and without ambiguity.'),
 'firm':('Не хотим оставлять у вас сомнений в нашей позиции.','We do not want to leave you in doubt about our position.'),
 'conciliatory':('Ищем выход, который позволит продолжить диалог без отказа от существенных интересов.','We seek a way forward that preserves dialogue without sacrificing essential interests.'),
 'protocol':('От имени нашего правительства изложим ответ в тех пределах, на которые он распространяется.','On behalf of our government, we state our answer within its intended scope.')}
FACTS={
 'low_relations':('При нынешнем недоверии одних заверений мало.','With relations this strained, assurances alone carry little weight.'),
 'good_relations':('Нынешние добрые отношения дают нам основание вести этот разговор открыто.','Our present good relations give us reason to speak openly.'),
 'at_war':('Пока наши страны воюют, цена неверного шага особенно высока.','While our countries are at war, the cost of a mistaken step is especially high.'),
 'same_faction':('Наше общее участие в союзе придаёт этому вопросу дополнительное значение.','Our membership in the same alliance gives this question added importance.'),
 'previous_position':('Мы уже излагали свою позицию и не хотим создавать впечатление, будто отказались от неё.','We have stated our position before and do not wish to imply that we have abandoned it.'),
 'hull_trade':('Связывая торговлю ограничениями, мы должны помнить и о возможностях сотрудничества.','In restricting trade, we should also keep the possibilities of cooperation in mind.')}
FRENCH_STYLES={
 'official': 'Voici la position de notre gouvernement.',
 'cautious': 'Mesurons d’abord la portée de nos engagements.',
 'friendly': 'Nous apprécions de pouvoir parler ouvertement de cette question.',
 'reserved': 'Nous nous en tiendrons aux engagements formulés devant nous.',
 'tense': 'Dans cet échange, chaque engagement doit être formulé clairement.',
 'firm': 'Sur ce point, notre réponse est sans équivoque.',
 'conciliatory': 'Cherchons ce qui nous permettrait de poursuivre utilement les discussions.',
 'protocol': 'Notre délégation est chargée de vous transmettre la réponse suivante.',
}
FRENCH_FACTS={
 'low_relations': 'Vu la défiance entre nos gouvernements, de simples assurances pèseront peu.',
 'good_relations': 'Les bonnes relations entre nos pays permettent d’aborder la question avec franchise.',
 'at_war': 'Tant que nos pays sont en guerre, cet engagement aurait des conséquences particulièrement graves.',
 'same_faction': 'Notre appartenance à la même alliance donne à cette question une portée particulière.',
 'previous_position': 'La position que nous vous avons déjà communiquée demeure d’actualité.',
}
POSITION_NOTES={
 'accept': ('Это согласие касается изложенных вами условий.', 'Our acceptance covers the terms you have presented.', 'Notre accord porte sur les conditions que vous avez présentées.'),
 'reject': ('На предложенных условиях мы обязательств не принимаем.', 'We cannot undertake the obligations on those terms.', 'Nous ne prendrons pas cet engagement aux conditions proposées.'),
 'counter_offer': ('Готовы продолжить разговор на предложенных нами условиях.', 'We are ready to continue on the terms we have proposed.', 'Nous sommes prêts à poursuivre sur la base de notre contre-proposition.'),
 'clarify': ('Ваш ответ поможет нам оценить предложение.', 'Your answer will help us assess the proposal.', 'Votre réponse nous permettra d’apprécier la portée de la proposition.'),
 'discuss': ('Мы готовы продолжить обсуждение и пока оставляем вопрос открытым.', 'We are prepared to continue the discussion and leave the question open for now.', 'Nous pouvons poursuivre cet échange, sans arrêter notre position à ce stade.'),
}

def basis(facts,verified=False,history=(),person_id=None,action_id=None):
 result={'facts':[],'verified':verified,'profile':None}
 if verified:
  try:
   relation=float(facts['relations'])
   if relation<=-40:result['facts'].append('low_relations')
   elif relation>=40:result['facts'].append('good_relations')
  except (ValueError,KeyError,TypeError):pass
  if facts.get('war_status') in ('yes',True):result['facts'].append('at_war')
  if facts.get('same_faction') in ('yes',True):result['facts'].append('same_faction')
 from historical_behavior import active_features
 fs=active_features(person_id,action_id,facts.get('date'),identity_verified=verified)
 result['profile_arguments']=[f['id'] for f in fs]
 result['profile_sources']=[f['source'] for f in fs]
 if fs:result['profile']=person_id
 if fs and profiles().get(person_id,{}).get('priority')=='trade_liberalization' and action_id in {'embargo','lift_embargo'}:
  result['facts'].append('hull_trade');result['profile']='usa_hull'
 return result

def options(core,row,language,style=None):
 if language not in {'ru','en','fr'} or row.get('term_id'):return [core]
 if language=='fr' and row.get('stance') not in POSITION_NOTES:return [core]
 i={'ru':0,'en':1,'fr':2}[language];stored=row.get('expression_basis',{})
 if row.get('conversation_stage') in {'confirmation_needed','refusal_maintained'}:
  return [core] # A next step is useful here; another ornamental opening is not.
 keys=[k for k in stored.get('facts',[]) if k in FACTS]
 if not stored.get('verified'):keys=[k for k in keys if k=='hull_trade' and stored.get('profile')=='usa_hull']
 if row.get('negotiation_progress')=='awaiting_response_to_existing_counteroffer':keys.append('previous_position')
 out=[]
 for name in ([style] if style in STYLES else list(STYLES)):
  lead=FRENCH_STYLES[name] if language=='fr' else STYLES[name][i]
  out.append(f'{lead} {core}')
  for k in dict.fromkeys(keys):
   fact=FRENCH_FACTS.get(k) if language=='fr' else FACTS[k][i]
   if fact:out.append(f'{core} {fact} {lead}')
 note=POSITION_NOTES.get(row.get('stance'))
 if note:
  out.append(f'{core} {note[i]}')
 from diplomatic_catalog import records
 from phrase_destinations import chat_eligible
 for fragment in records():
  if chat_eligible(fragment,row) and language in fragment['translations']:
   out.append(core+' '+fragment['translations'][language])
 from natural_bilingual import eligible
 for fragment in eligible(row):
  if language in fragment['translations']:
   out.append(core+' '+fragment['translations'][language])
 if stored.get('verified') and stored.get('profile'):
  from historical_behavior import feature_text
  for key in stored.get('profile_arguments',[]):
   text=feature_text(stored['profile'],key,language)
   if text:out.insert(0,core+' '+text)
 return [t for t in out if len(t)<=600] or [core]
