"""Conversation progress over existing point policies; never consent or effects."""
import re
from copy import deepcopy
from diplomatic_language import canonical_period


def state_ids(text):
    """Only explicitly numbered states; never guess names or vanilla IDs."""
    return list(dict.fromkeys(int(m) for m in re.findall(
        r'(?:\bstates?\s*(?:id\s*)?|\bобласт\w*\s*(?:id\s*)?|\bрегион\w*\s*(?:id\s*)?)#?\s*(\d{1,7})\b',
        text, re.I)))[:32]


def prepare_point(point):
    p=deepcopy(point)
    if p.get('action_id')=='territory_partition' and not p.get('term_id'):
        p['state_ids']=state_ids(str(p.get('source_excerpt') or ''))
        # Source IDs identify the requested subject, not its existence/ownership.
        # Aggregate state counts in native snapshots cannot certify an allocation.
        p['unresolved_fields']=list(dict.fromkeys([
            *(f for f in p.get('unresolved_fields',[]) if f!='unsupported_structured_action'),
            *(['state_ids'] if not p['state_ids'] else ['verified_state_allocation'])]))
    return p


def signature(p):
    return {k:canonical_period(p.get(k)) if k=='duration' else p.get(k)
            for k in ('action_id','term_id','term_clause','actors','target','recipient',
                      'duration','conditions','exceptions','additional_terms','state_ids')}


def mark_progress(row, history):
    """Attach conversational state after the planner has selected its stance."""
    prior=next((p for p in reversed(history)
                if p.get('action_id')==row.get('action_id')
                and p.get('term_id')==row.get('term_id')
                and p.get('actors')==row.get('actors')
                and p.get('target')==row.get('target')
                and p.get('recipient')==row.get('recipient')),None)
    stance=row.get('stance');stage={'accept':'accepted_position','reject':'refusal',
        'clarify':'clarification','counter_offer':'counteroffer','discuss':'evaluation'}.get(stance,'evaluation')
    same=bool(prior and signature(prior)==signature(row) and not row.get('new_proposal'))
    if same and prior.get('stance')==stance:
        if stance=='accept':stage='confirmation_needed'
        elif stance=='reject':stage='refusal_maintained'
        elif stance=='clarify':stage='awaiting_missing_information'
        elif stance=='counter_offer':stage='disagreement'
    if (prior and prior.get('proposed_duration_years') and stance=='accept' and not same
            and canonical_period(row.get('duration'))==canonical_period(f"{prior['proposed_duration_years']} years")
            and all(prior.get(k)==row.get(k) for k in ('conditions','exceptions','additional_terms'))):
        stage='compromise'
    row['conversation_stage']=stage
    row['open_questions']=list(row.get('clarification_reasons',[])) if stance=='clarify' else []
    row['next_step']=({'accept':'review_exact_version_and_confirm_separately',
        'reject':'revise_substantive_terms_or_close','clarify':'answer_open_questions',
        'counter_offer':'respond_to_saved_counterterms','discuss':'supply_reason_or_concrete_terms'}[stance])
    row['previous_position_reused']=same
    row['creates_consent']=False
    return row


def progress_text(row,country,other,language):
    if language not in {'ru','en'} or row.get('term_id'):return None
    ru=language=='ru';stage=row.get('conversation_stage')
    if row.get('action_id')=='guarantee' and row.get('speaking_role')=='observer':
        return (f'{country} понимает, что гарантию должна предоставить другая сторона. Мы не можем принять это обязательство от её имени: требуется отдельное подтверждение уполномоченного гаранта.' if ru else
            f'{country} understands that the guarantee would be provided by the other party. We cannot undertake that obligation on its behalf: the authorised guarantor must confirm it separately.')
    if row.get('action_id')=='territory_partition':
        ids=row.get('state_ids',[])
        if not ids:
            return (f'{country} просит назвать области, о которых идёт речь: укажите их игровые ID и кому вы предлагаете их передать. Без этого мы не можем оценить территориальное предложение.' if ru else
                f'{country} asks which states you mean: give their in-game IDs and the proposed recipient. Without that, we cannot assess the territorial proposal.')
        subject=', '.join(map(str,ids))
        return (f'{country} понимает, что речь идёт об областях с ID {subject}. Теперь нужны подтверждённые сведения об их существовании, владельцах, контролёрах и точном распределении. Согласия на передачу пока нет.' if ru else
            f'{country} understands that the proposal concerns state IDs {subject}. We now need verified existence, owners, controllers and the exact allocation. No transfer has been accepted.')
    if stage=='confirmation_needed':
        return (f'{country} уже выразила согласие с этими условиями; повторять предложение не требуется. Проверьте точную редакцию и подтвердите её отдельно. Это ещё не исполнение договора в игре.' if ru else
            f'{country} has already accepted these terms; there is no need to repeat the offer. Review the exact version and confirm it separately. This does not mean the treaty has been applied in the game.')
    if stage=='refusal_maintained':
        return (f'{country} сохраняет отказ: условия не изменились. Если вы хотите продолжить переговоры, укажите, какое обязательство предлагаете пересмотреть.' if ru else
            f'{country} maintains its refusal because the terms have not changed. To continue negotiations, specify which obligation you propose to revise.')
    return None


def followup(text, positions, language):
    """Answer narrow process/reason questions without casting another vote."""
    if language not in {'ru','en'} or len(positions)!=1:return None
    from structured_intents import _expected_action_ids
    if _expected_action_ids(text):return None
    row=positions[0];ru=language=='ru'
    why=bool(re.search(r'\bwhy\b|\breasons?\b|почему|объясн\w*|причин\w*',text,re.I))
    next_step=bool(re.search(r'what (?:happens |comes )?next|how (?:do|can) (?:I|we) confirm|what remains|что дальше|как подтверд|что оста[её]тся',text,re.I))
    if not (why or next_step):return None
    if why:
        if row.get('stance')=='clarify':
            reply=progress_text(row,'Наша делегация' if ru else 'Our delegation','',language) or (
                'Причина в том, что существенное условие ещё не определено; мы не можем оценить неизвестное обязательство.' if ru else
                'A material term is still unresolved; we cannot assess an obligation whose scope is unknown.')
        elif row.get('stance')=='counter_offer' and row.get('proposed_duration_years'):
            years=row['proposed_duration_years']
            reply=(f'Мы предлагаем срок {years} года, чтобы ограничить длительность обязательства. Это встречное условие, а не принятие первоначального срока.' if ru else
                f'We proposed {years} years to limit the length of the commitment. That is a counterterm, not acceptance of the original duration.')
        else:
            from grounded_expression import FACTS
            b=row.get('expression_basis',{})
            from historical_behavior import feature_text
            personal=[feature_text(b.get('profile'),k,language) for k in b.get('profile_arguments',[])] if b.get('verified') else []
            keys=[k for k in b.get('facts',[]) if k in FACTS] if b.get('verified') else []
            reply=(next((t for t in personal if t),None) or (FACTS[keys[0]][0 if ru else 1] if keys else
                ('В сохранённой позиции отдельная причина не указана. Не станем приписывать ей мотивы, которых не обсуждали.' if ru else
                 'No separate reason was recorded with our position. We will not attribute motives that were never discussed.')))
        stage='argumentation'
    else:
        stance=row.get('stance')
        if stance=='accept':
            reply=('Следующий шаг — проверить точную редакцию и подтвердить её в «Условиях». Согласование текста ещё не означает применения договора в игре.' if ru else
                'The next step is to review the exact version and confirm it in Conditions. Agreement on wording does not mean the treaty has been applied in the game.')
        elif stance=='counter_offer':
            reply=('Остаётся ваш ответ на встречные условия: принять их, отклонить или предложить конкретное изменение. Наша позиция пока прежняя.' if ru else
                'We await your response to the counterterms: accept, reject, or propose a specific change. Our position remains unchanged.')
        elif stance=='clarify':
            reply=progress_text(row,'Наша делегация' if ru else 'Our delegation','',language) or (
                'Остаётся ответить на уже заданный вопрос об условиях. Пока он не решён, согласия нет.' if ru else
                'The outstanding question about the terms still needs an answer. Until it is resolved, there is no acceptance.')
        else:
            reply=('Можно изменить существенное условие или завершить обсуждение; новых обязательств мы не принимали.' if ru else
                'You may revise a substantive term or close the discussion; we have undertaken no new obligations.')
        stage='next_step'
    return {'text':reply,'stage':stage,'source_policy_request_id':row.get('policy_request_id'),
            'creates_consent':False,'conditions_changed':False}
