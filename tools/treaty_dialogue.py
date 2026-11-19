"""Validate and render one model decision for an already grounded treaty."""
from collections import defaultdict
import hashlib
import json


KINDS = {'non_aggression_pact': 'non_aggression_pact', 'guarantee': 'independence_guarantee',
         'military_access': 'military_access'}
DECISIONS = {'accept', 'reject', 'counteroffer', 'clarify', 'defer'}
REASONS = {
    'accept': {'mutual_security', 'reduced_tensions', 'defensive_assurance'},
    'reject': {'security_risk', 'limits_on_commitments', 'insufficient_trust'},
    'counteroffer': {'security_risk', 'limits_on_commitments', 'insufficient_trust'},
    'defer': {'needs_deliberation', 'insufficient_context'},
    'clarify': {'missing_scope'},
}
QUESTIONS = {'actor', 'recipient', 'target_country', 'protected_country', 'duration'}


def term_specs(points):
    return [{
        'term_id': KINDS[point['action_id']], 'type': point['action_id'],
        'provider': point['actors'][0], 'recipient': point['recipient'],
        'open_questions': sorted(QUESTIONS.intersection(point['unresolved_fields'])),
    } for point in points]


WIRE_IDS = {'non_aggression_pact':'pact','independence_guarantee':'guarantee','military_access':'access'}


def wire_contract(binding, terms):
    reference = hashlib.sha256(json.dumps([binding, terms, 'term-votes-v2'], sort_keys=True).encode()).hexdigest()[:16]
    options = [{
        **{f'{stance}:{reason}':(stance, reason, []) for stance, reasons in REASONS.items()
           for reason in reasons if stance != 'clarify' and (not term['open_questions'] or stance != 'accept')},
        **{f'clarify:{field}':('clarify','missing_scope',[field]) for field in term['open_questions']},
    } for term in terms]

    def obj(properties):
        return {'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}

    fields = [WIRE_IDS[term['term_id']] for term in terms]
    schema = obj({
        'ref':{'type':'string','enum':[reference]},
        'positions':obj({key:{'type':'string','enum':sorted(allowed)} for key, allowed in zip(fields, options)}),
        'reply':{'type':'string','minLength':1,'maxLength':240},
    })
    return reference, fields, options, schema


def expand_wire(raw, binding, terms, reference, fields, options):
    data = json.loads(raw, object_pairs_hook=_unique_dict)
    if (not isinstance(data, dict) or set(data) != {'ref','positions','reply'} or data['ref'] != reference
            or not isinstance(data['positions'], dict) or set(data['positions']) != set(fields)):
        raise ValueError('invalid_treaty_wire_binding')
    rows = []
    for field, term, allowed in zip(fields, terms, options):
        vote = data['positions'][field]
        if not isinstance(vote, str) or vote not in allowed:
            raise ValueError('invalid_treaty_wire_position')
        stance, reason, questions = allowed[vote]
        rows.append({**{key:term[key] for key in ('term_id','type','provider','recipient')},
                     'decision':stance,'reason':reason,'questions':questions,
                     'counter':'omit_term' if stance == 'counteroffer' else 'none'})
    if not isinstance(data['reply'], str) or len(data['reply']) > 240:
        raise ValueError('invalid_treaty_prose')
    votes={row['decision'] for row in rows}
    status=next(iter(votes)) if len(votes)==1 else 'partial'
    return validate_decision(json.dumps({**binding,'terms':rows,'status':status,'reply':data['reply']}),
                             binding, terms)


def validate_decision(raw, binding, terms):
    data = json.loads(raw, object_pairs_hook=_unique_dict)
    if (not isinstance(data, dict) or set(data) != {*binding, 'status', 'terms', 'reply'}
            or any(type(data[key]) is not type(value) or data[key] != value for key, value in binding.items())
            or not isinstance(data['reply'], str) or not 1 <= len(data['reply'].strip()) <= 600
            or not isinstance(data['terms'], list) or len(data['terms']) != len(terms)):
        raise ValueError('invalid_treaty_envelope')
    expected = {row['term_id']:row for row in terms}
    parsed = {}
    for row in data['terms']:
        if (not isinstance(row,dict) or set(row) != {'term_id','type','provider','recipient','decision','reason','counter','questions'}
                or not all(isinstance(row[key],str) for key in set(row) - {'questions'})
                or row['term_id'] in parsed or row['term_id'] not in expected):
            raise ValueError('invalid_treaty_term')
        spec = expected[row['term_id']]
        if any(row[key] != spec[key] for key in ('type','provider','recipient')):
            raise ValueError('wrong_treaty_direction')
        stance, questions = row['decision'], row['questions']
        if (row['reason'] not in REASONS.get(stance, ())
                or row['counter'] != ('omit_term' if stance == 'counteroffer' else 'none')
                or not isinstance(questions,list) or len(questions) > 2
                or any(not isinstance(q,str) or q not in spec['open_questions'] for q in questions)
                or len(set(questions)) != len(questions)
                or bool(questions) != (stance == 'clarify')
                or (spec['open_questions'] and stance == 'accept')):
            raise ValueError('invalid_treaty_position')
        parsed[row['term_id']] = row
    stances = {row['decision'] for row in parsed.values()}
    if data['status'] != (next(iter(stances)) if len(stances) == 1 else 'partial'):
        raise ValueError('contradictory_treaty_status')
    return [parsed[row['term_id']] for row in terms], data['reply']


def _unique_dict(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_json_key')
        result[key] = value
    return result


_RENDER = {
    'ru': (('пакт о ненападении', 'гарантию независимости', 'военный доступ'),
           {'accept':'Мы согласны включить в договор', 'reject':'Мы отклоняем',
            'counteroffer':'Мы предлагаем исключить из договора', 'defer':'Нам нужно ещё время, чтобы рассмотреть',
            'clarify':'Нам нужны подробности по пункту'}, ' и '),
    'en': (('the non-aggression pact', 'the independence guarantee', 'military access'),
           {'accept':'We agree to include', 'reject':'We decline', 'counteroffer':'We propose leaving out',
            'defer':'We need more time to consider', 'clarify':'We need further details on'}, ' and '),
    'fr': (('le pacte de non-agression', 'la garantie d’indépendance', 'l’accès militaire'),
           {'accept':'Nous acceptons d’inclure', 'reject':'Nous refusons', 'counteroffer':'Nous proposons de retirer',
            'defer':'Il nous faut plus de temps pour examiner', 'clarify':'Nous souhaitons préciser'}, ' et '),
}
_REASON_TEXT = {
    'mutual_security': ('Это укрепит общую безопасность.', 'That would strengthen mutual security.', 'Cela renforcerait notre sécurité commune.'),
    'reduced_tensions': ('Это поможет снизить напряжённость.', 'That would help ease tensions.', 'Cela contribuerait à apaiser les tensions.'),
    'defensive_assurance': ('Это даст нам дополнительные гарантии безопасности.', 'That would give us greater confidence in our security.', 'Cela renforcerait nos assurances de sécurité.'),
    'security_risk': ('Для нас это создаёт риск в сфере безопасности.', 'We see a security risk in that commitment.', 'Cet engagement présente un risque pour notre sécurité.'),
    'limits_on_commitments': ('Мы хотим ограничить объём обязательств.', 'We want to limit the scope of our commitments.', 'Nous souhaitons limiter la portée de nos engagements.'),
    'insufficient_trust': ('Для такого шага пока недостаточно доверия.', 'There is too little trust for that step at present.', 'La confiance nécessaire à cette démarche fait encore défaut.'),
}


def render_decision(rows, language):
    language = language if language in _RENDER else 'en'
    labels, openings, joiner = _RENDER[language]
    names = dict(zip(KINDS, labels))
    grouped = defaultdict(list)
    for row in rows:
        grouped[row['decision']].append(row)
    sentences = []
    used_reasons = set()
    for stance, clauses in grouped.items():
        subjects = [names[row['type']] for row in clauses]
        phrase = joiner.join(subjects) if len(subjects) < 3 else ', '.join(subjects[:-1]) + joiner + subjects[-1]
        sentences.append(f'{openings[stance]} {phrase}.')
        reasons = {row['reason'] for row in clauses}
        if len(reasons) == 1:
            reason, = reasons
            if reason in _REASON_TEXT and reason not in used_reasons:
                sentences.append(_REASON_TEXT[reason][('ru','en','fr').index(language)])
                used_reasons.add(reason)
    return ' '.join(sentences)
