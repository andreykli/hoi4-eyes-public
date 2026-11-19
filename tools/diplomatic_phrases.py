"""Curated expression fragments; no decisions, consent or game facts live here.

128 authored fragments, 192 compositions/language for a fixed operative core.
The core comes exclusively from render_point_policy and is never paraphrased
by this module. Tone changes register, not the diplomatic decision.
"""
import hashlib,itertools,json

OPENINGS={
 'ru':{
 'formal':('Мы рассмотрели ваше обращение.','Наша делегация ознакомилась с предложением.','Изложим нашу позицию по существу.','Отвечаем на предложенные условия.','Благодарим за изложенную позицию.','Мы готовы обозначить нашу позицию.'),
 'conciliatory':('Благодарим за готовность к диалогу.','Прямой обмен мнениями полезен для переговоров.','Мы ценим возможность обсудить предложение.','Постараемся ясно обозначить нашу позицию.','Благодарим за обращение по дипломатическим каналам.','Дадим содержательный ответ на ваше предложение.'),
 'cautious':('Подобные обязательства требуют взвешенного подхода.','Наш ответ должен быть недвусмысленным.','Считаем важным точно определить пределы обязательств.','Рассмотрим предложение без поспешных обещаний.','Разграничим обсуждение и принятие обязательств.','Наша позиция требует точной формулировки.'),
 'direct':('Перейдём к существу предложения.','Наш ответ следующий.','Обозначим позицию прямо.','По этому вопросу ответим определённо.','Сформулируем наш ответ кратко.','Остановимся на главном.')},
 'en':{
 'formal':('We have considered your communication.','Our delegation has reviewed the proposal.','We shall state our position on its substance.','We respond to the terms you have presented.','Thank you for setting out your position.','We are prepared to state our position.'),
 'conciliatory':('Thank you for opening this discussion.','A direct exchange is useful for these negotiations.','We appreciate the opportunity to discuss the proposal.','We shall make our position clear.','Thank you for approaching us through diplomatic channels.','We shall address the substance of your proposal.'),
 'cautious':('Such commitments require careful consideration.','Our answer must be unambiguous.','The limits of any obligation must be clear.','We shall consider the proposal without hasty promises.','Discussion and commitment must remain distinct.','Our position calls for precise wording.'),
 'direct':('Let us address the substance.','Our answer is as follows.','We shall state our position directly.','We can give a definite answer on this point.','We shall keep our response brief.','Let us focus on the essential issue.')}}
CLOSINGS={
 'ru':{
 'accept':('Это согласие относится только к изложенным условиям.','Отдельные дополнительные условия требуют отдельного обсуждения.','Наш ответ не распространяется на неоговорённые обязательства.','Просим сверить точную редакцию перед окончательным подтверждением.','Последующее подтверждение должно относиться к той же редакции.','Не будем добавлять к этому согласию подразумеваемых условий.','Важно сохранить точный объём принятого предложения.','Подтверждение и исполнение остаются отдельными этапами.'),
 'reject':('Этот отказ относится к рассматриваемому предложению.','Не следует считать обсуждение согласием.','На изложенные условия нашего согласия нет.','Обязательства по этому предложению не приняты.','Представленная редакция не получила нашего согласия.','Считаем необходимым ясно зафиксировать эту позицию.','Наш ответ не создаёт новых обязательств.','Не будем оставлять сомнений относительно этой позиции.'),
 'counter_offer':('Это встречное предложение, а не принятие исходной редакции.','Просим ответить именно на предложенное изменение.','Остальные пункты сохраняют свои отдельные позиции.','Встречные условия требуют отдельного согласования.','Согласованной редакции пока нет.','Не будем считать встречное предложение подписанием договора.','Просим различать исходные и встречные условия.','Дальнейшее согласование должно учитывать эту редакцию.'),
 'clarify':('До уточнения согласие не подтверждено.','Просим ответить по существу этого вопроса.','Не будем подменять неизвестные условия предположениями.','Неясность не должна превращаться в обязательство.','После вашего ответа можно будет оценить этот пункт.','Уточнение само по себе не означает принятия.','Пока оставим этот пункт неподтверждённым.','Важно получить однозначную формулировку.')},
 'en':{
 'accept':('This acceptance concerns only the stated terms.','Any additional terms require separate discussion.','Our answer does not extend to unstated obligations.','Please check the exact wording before final confirmation.','Any subsequent confirmation must concern the same version.','No implied conditions should be added to this acceptance.','The precise scope of the accepted proposal matters.','Confirmation and execution remain separate steps.'),
 'reject':('This refusal concerns the proposal under consideration.','Discussion must not be mistaken for acceptance.','We have not accepted the terms as presented.','No obligations under this proposal have been accepted.','The submitted version has not received our acceptance.','We consider it necessary to record this position clearly.','Our answer creates no new obligations.','There should be no ambiguity about this position.'),
 'counter_offer':('This is a counteroffer, not acceptance of the original wording.','Please respond to the specific amendment we propose.','Other clauses retain their separate positions.','The counterterms require separate agreement.','There is no agreed version yet.','A counteroffer must not be treated as a signed treaty.','The original terms and counterterms must remain distinct.','Further agreement must take this version into account.'),
 'clarify':('Acceptance remains unconfirmed pending clarification.','Please address this specific question.','We should not replace unknown terms with assumptions.','Ambiguity must not become an obligation.','Your answer will allow this clause to be assessed.','Clarification does not itself constitute acceptance.','This clause remains unconfirmed for now.','An unambiguous formulation is important.')}}

CLOSINGS['ru']['discuss']=(
 'Обсуждение пока не означает принятия обязательств.','Наша позиция по этому предложению остаётся открытой.',
 'Согласие на договор пока не дано.','Не будем считать продолжение разговора подписанием.',
 'Прежде чем брать обязательства, следует взвесить их последствия.','Дальнейший обмен мнениями не заменяет подтверждения условий.',
 'Готовность обсуждать следует отличать от согласия.','На данном этапе обязательства не подтверждены.')
CLOSINGS['en']['discuss']=(
 'Discussion does not yet mean undertaking obligations.','Our position on this proposal remains open.',
 'Acceptance of a treaty has not been given.','Continuing this exchange must not be treated as signing.',
 'The consequences should be weighed before commitments are made.','Further discussion does not replace confirmation of terms.',
 'Willingness to discuss must be distinguished from acceptance.','No commitments are confirmed at this stage.')

def statistics():
    return {'base_fragments':sum(len(v) for rows in OPENINGS.values() for v in rows.values())+
            sum(len(v) for rows in CLOSINGS.values() for v in rows.values()),
            'compositions_per_core_per_language':192,'compositions_per_core_ru_and_en':384,
            'languages':['ru','en'],'tones':list(OPENINGS['en'])}

def tone_for(context,row):
    styles=context.get('leader_expression_style') or []
    if isinstance(styles,list):
        for style in styles:
            if style in OPENINGS['en']:return style
    if row.get('stance') in {'reject','clarify'}:return 'cautious'
    doctrine=context.get('diplomatic_doctrine') or {}
    if isinstance(doctrine,dict) and doctrine.get('ideology_observed') is True:
        own=doctrine.get('ideology_group'); other=doctrine.get('sender_ideology_group')
        if own not in {None,'','unknown'} and other not in {None,'','unknown'} and own!=other:return 'cautious'
    goals=row.get('decision_reasons') or []
    if 'preserve_independence' in goals:return 'cautious'
    if len(context.get('recent_messages') or []) >= 6:return 'direct'
    # Register may reflect a known relationship; it never asserts that fact.
    facts=context.get('known_game_context') or context.get('game_context') or {}
    if isinstance(facts,dict):
        try:
            value=float(facts['relations'])
            if value>=40:return 'conciliatory'
            if value<=-40:return 'cautious'
        except (KeyError,TypeError,ValueError):pass
    return 'formal'

def candidates(core,stance,language,tone=None):
    if language not in OPENINGS:return [core]
    closing=CLOSINGS[language].get(stance)
    if closing is None:return [core] # discussion is not acceptance or clarification
    tones=[tone] if tone in OPENINGS[language] else list(OPENINGS[language])
    return [f'{opening} {core} {ending}' for t in tones
            for opening,ending in itertools.product(OPENINGS[language][t],closing)
            if len(f'{opening} {core} {ending}')<=600]

def _catalog_opening(row,context,language):
    """Opt-in variety from the bilingual catalog; the legal core is always appended."""
    import os
    if os.getenv('EYES_TEMPLATE_CATALOG','0').strip().casefold() not in {'1','true','yes'}:return None
    if language not in {'ru','en'}:return None
    stance={'reject':'refuse'}.get(row.get('stance'),row.get('stance'))
    safe_openings={
        'accept': {'accept.cautious.115'},
        'refuse': {'reject.disappointed.008'},
    }
    if stance not in safe_openings:return None
    try:
        from diplomatic_response import reply
        from news_templates import templates
        result=reply(stance=stance,language=language,
                     country_a=str(context.get('speaking_country_tag') or ''),
                     country_b=str(context.get('other_country_tag') or ''),
                     action_id=str(row.get('action_id') or 'non_aggression_pact'),
                     key=str(context.get('request_id') or ''),
                     templates=[item for item in templates() if item['id'] in safe_openings[stance]])
    except (ImportError,ValueError,KeyError,TypeError,OSError):return None
    return result['text'] if result else None

def choose(core,row,context,language):
    from phrase_usage_memory import expression_style
    style=expression_style(context,row)
    tone={'official':'formal','cautious':'cautious','friendly':'conciliatory','reserved':'formal',
          'tense':'cautious','firm':'direct','conciliatory':'conciliatory','protocol':'formal'}[style]
    from grounded_expression import options as natural_options
    options=natural_options(core,row,language,style)
    if not options:return core
    seed=json.dumps([context.get('request_id'),row.get('point'),core,tone],ensure_ascii=False)
    start=int(hashlib.sha256(seed.encode('utf8')).hexdigest()[:12],16)%len(options)
    recent={m.get('text') for m in context.get('recent_messages',[]) if isinstance(m,dict)
            and m.get('request_id')!=context.get('request_id')}
    # Compare individual point paragraphs as well as complete multi-point turns.
    for m in context.get('recent_messages',[]):
        if isinstance(m,dict) and m.get('request_id')!=context.get('request_id'):
            recent.update(str(m.get('text','')).splitlines())
    from phrase_usage_memory import recently_used
    retained=context.get('phrase_recent') or []
    opening=_catalog_opening(row,context,language)
    if opening:
        combined=f'{opening} {core}'
        if len(combined)<=600 and combined not in recent and not recently_used(combined,core,retained,language,context.get('request_id')):
            return combined
    ordered=options[start:]+options[:start]
    from grounded_expression import FACTS, FRENCH_FACTS
    evidence=row.get('expression_basis',{})
    if evidence.get('verified') and language in {'ru','en','fr'}:
        index=0 if language=='ru' else 1
        from historical_behavior import feature_text
        personal=[feature_text(evidence.get('profile'),key,language) for key in evidence.get('profile_arguments',[])]
        known = FRENCH_FACTS if language == 'fr' else {key: values[index] for key, values in FACTS.items()}
        ordered.sort(key=lambda text:0 if any(p and p in text for p in personal) else 1 if any(known[k] in text for k in evidence.get('facts',[]) if k in known) else 2)
    for option in ordered:
        if option not in recent and not recently_used(option,core,retained,language,context.get('request_id')):return option
    return core # Never suppress exact legal wording when expressive options are exhausted.

def matches(text,core,row,language):
    from diplomatic_catalog import position_candidates
    from grounded_expression import options as natural_options
    return text==core or text in natural_options(core,row,language)

def fast_path(policy):
    if len(policy)!=1:return False
    row=policy[0]
    return (row.get('stance') in {'accept','reject'} and not row.get('term_id')
            and not any(row.get(k) for k in ('conditions','exceptions','additional_terms','unresolved_fields'))
            and row.get('action_id') in {'guarantee','non_aggression_pact','military_access','embargo','lift_embargo'})
