"""Standalone native fixtures for a fresh 1936 GER campaign."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Case:
    key: str
    title: str
    before: str
    expected: str
    guard: str
    effect: str
    check: str


def state(number, body):
    return f'{number} = {{ {body} }}'


def owns(number, tag):
    return state(number, f'is_owned_by = {tag} is_fully_controlled_by = {tag}')


def neg(condition):
    return f'NOT = {{ {condition} }}'


def core(number, tag, present=True):
    condition = f'is_core_of = {tag}'
    return state(number, condition if present else neg(condition))


def cases(anchor_tag='QZX', faction_created='eyesqa_faction_created'):
    nap = 'has_non_aggression_pact_with = FRA FRA = { has_non_aggression_pact_with = GER }'
    no_nap = 'NOT = { has_non_aggression_pact_with = FRA } FRA = { NOT = { has_non_aggression_pact_with = GER } }'
    guarantee = 'FRA = { is_guaranteed_by = GER }'
    access = 'FRA = { has_military_access_to = GER }'
    reverse_access = 'has_military_access_to = FRA'
    factions = f'{anchor_tag} = {{ exists = yes is_in_faction = no is_subject = no has_war = no }}'
    leader = f'{anchor_tag} = {{ exists = yes is_in_faction = yes is_faction_leader = yes }}'
    empty_faction = f'{anchor_tag} = {{ NOT = {{ any_other_country = {{ is_in_faction_with = {anchor_tag} }} }} }}'
    capital = lambda number: f'capital_scope = {{ state = {number} }}'
    here = owns(5, 'GER')
    infra = lambda level: state(5, f'check_variable = {{ var = building_level@infrastructure value = {level} compare = equals }}')
    no_damage = state(5, 'check_variable = { var = damaged_building_level@infrastructure value = 0 compare = equals }')
    recognized = 'is_in_array = { array = eyes_recognized value = FRA }'
    withdrawn = 'is_in_array = { array = eyes_not_recognized value = FRA }'
    relation = lambda label,on: f'diplomatic_relation = {{ country = FRA relation = {label} active = {on} }}'
    return (
        Case('nap','Пакт о ненападении','absent','active',no_nap,relation('non_aggression_pact','yes'),nap),
        Case('cancel_nap','Расторжение пакта','active','absent',nap,relation('non_aggression_pact','no'),no_nap),
        Case('guarantee','Гарантия независимости','absent','active',neg(guarantee),'give_guarantee = FRA',guarantee),
        Case('cancel_guarantee','Отзыв гарантии','active','absent',guarantee,relation('guarantee','no'),neg(guarantee)),
        Case('access','Военный доступ','absent','active',f'{neg(access)} {neg(reverse_access)}',
             'give_military_access = FRA',f'{access} {neg(reverse_access)}'),
        Case('cancel_access','Отзыв военного доступа','active','absent',f'{access} {neg(reverse_access)}',
             'FRA = { diplomatic_relation = { country = GER relation = military_access active = no } }',
             f'{neg(access)} {neg(reverse_access)}'),
        Case('create_faction','Создание фракции','absent','qa_leads',factions,
             f'{anchor_tag} = {{ create_faction = EYES_QA_FACTION }}',f'{leader} {empty_faction}'),
        Case('dismantle_faction','Роспуск фракции','qa_leads','absent',
             f'has_country_flag = {faction_created} {leader} {empty_faction}',
             f'{anchor_tag} = {{ dismantle_faction = yes }}',factions),
        Case('capital','Перенос столицы','state_64','state_65',f'{capital(64)} {owns(65,"GER")} {core(65,"GER")}',
             'set_capital = { state = 65 remember_old_capital = no }',f'{capital(65)} {owns(65,"GER")}'),
        Case('remove_core','Удаление core','core_ger','no_core',f'{here} {core(5,"GER")} {neg(capital(5))}',
             'remove_state_core = 5',f'{here} {core(5,"GER",False)}'),
        Case('add_core','Добавление core','no_core','core_ger',f'{here} {core(5,"GER",False)}',
             'add_state_core = 5',f'{here} {core(5,"GER")}'),
        Case('infrastructure','Изменение инфраструктуры','level_3','level_4',f'{here} {infra(3)} {no_damage}',
             state(5,'set_building_level = { type = infrastructure level = 4 }'),f'{here} {infra(4)}'),
        Case('transfer','Передача территории','ger_controls_5','fra_controls_5',f'{here} {neg(capital(5))}',
             state(5,'transfer_state_to = FRA'),owns(5,'FRA')),
        Case('return_state','Возврат тестовой территории','fra_controls_5','ger_controls_5',owns(5,'FRA'),
             state(5,'transfer_state_to = GER'),here),
        Case('recognition','Дипломатическое признание','unknown','recognized',f'{neg(recognized)} {neg(withdrawn)}',
             'add_to_array = { array = eyes_recognized value = FRA }',recognized),
        Case('withdraw_recognition','Отзыв признания','recognized','not_recognized',recognized,
             'remove_from_array = { array = eyes_recognized value = FRA } add_to_array = { array = eyes_not_recognized value = FRA }',
             f'{withdrawn} {neg(recognized)}'),
    )


EXTRA = {
    'precondition':'Отказ при неверном владельце', 'replay':'Защита от повтора',
    'country_maker':'Нативное создание страны', 'news_event':'Доставка мировой новости',
    'news_display':'Подтверждение видимой новости',
}


def scripts(nonce, anchor_tag, maker_tag):
    ns = f'eyesqa_{nonce}'
    done = f'{ns}_done'
    finished = f'{ns}_finished'
    anchor = f'{anchor_tag} = {{ exists = yes has_country_flag = {ns}_fresh }} {state(8,f"is_owned_by = {anchor_tag}")}'
    guard = f'tag = GER is_ai = no date > 1935.12.31 date < 1936.1.3 has_war = no FRA = {{ exists = yes is_ai = yes has_war = no }} {anchor}'
    ready = f'{guard} {neg(f"has_country_flag = {done}")}'
    pending = f'{ns}_maker_pending'
    faction_created = f'{ns}_faction_created'
    log = lambda payload: f'log = "EYES_LIVE_QA_V1|{nonce}|GER|{payload}"'
    emit = lambda key,status,before,expected,actual,reason: log(f'CASE|{key}|{status}|{before}|{expected}|{actual}|{reason}')

    def branch(condition, good, bad=''):
        text = f'if = {{ limit = {{ {condition} }} {good} }}'
        return text + (f'\nelse = {{ {bad} }}' if bad else '')

    def run(case):
        attempt = f'{ns}_{case.key}'
        success = emit(case.key,'PASS',case.before,case.expected,case.expected,'-')
        if case.key == 'create_faction':
            success = f'set_country_flag = {faction_created} {success}'
        verified = branch(case.check, success,
                          emit(case.key,'FAIL',case.before,case.expected,'postcondition_false','wrong_postcondition'))
        return branch(f'{case.guard} NOT = {{ has_country_flag = {attempt} }}',
                      f'set_country_flag = {attempt} {case.effect}\n{verified}',
                      emit(case.key,'BLOCKED','precondition_false',case.expected,'unchanged','guard_rejected'))

    maker_guard = f'{owns(5,"GER")} {maker_tag} = {{ exists = no }} NOT = {{ any_state = {{ is_core_of = {maker_tag} }} }}'
    maker_effect = f'5 = {{ add_core_of = {maker_tag} }} release = {maker_tag} set_country_flag = {pending}'
    maker_checks = (
        (f'{maker_tag} = {{ exists = yes }}', 'country_missing'),
        (state(5, f'is_owned_by = {maker_tag}'), 'wrong_owner'),
        (state(5, f'is_fully_controlled_by = {maker_tag}'), 'wrong_controller'),
        (core(5,maker_tag), 'missing_core'),
        (f'NOT = {{ any_state = {{ is_core_of = {maker_tag} NOT = {{ state = 5 }} }} }}', 'extra_cores'),
        (f'{maker_tag} = {{ NOT = {{ any_owned_state = {{ NOT = {{ state = 5 }} }} }} }}', 'extra_territory'),
        (f'{maker_tag} = {{ is_subject = no }}', 'country_is_subject'),
    )
    maker_capital = (f'{maker_tag} = {{ capital_scope = {{ state = 5 }} }}', 'wrong_capital')
    maker_result = emit('country_maker','PASS','tag_absent','new_country','new_country','-')
    for condition, reason in reversed((*maker_checks, maker_capital)):
        fail = emit('country_maker','FAIL','tag_absent','new_country','postcondition_false',reason)
        maker_result = branch(condition,maker_result,fail)
    rejected = branch(owns(8,'GER'), f'set_country_flag = {ns}_negative')
    rejected += branch(neg(f'has_country_flag = {ns}_negative'),
                       emit('precondition','PASS','wrong_owner','blocked','blocked','-'),
                       emit('precondition','FAIL','wrong_owner','blocked','effect_ran','guard_bypassed'))
    atlas = lambda phase: '\n'.join(state(number,log(f'ATLAS|{phase}|{number}|[THIS.Owner.GetTag]|[THIS.Controller.GetTag]'))
                                   for number in (5,8,65))
    main = '\n'.join([f'set_country_flag = {done}',log('BEGIN'),atlas('before'),
                     *(run(case) for case in cases(anchor_tag, faction_created)),rejected,
                     branch(maker_guard,maker_effect,emit('country_maker','BLOCKED','precondition_false','new_country','unchanged','guard_rejected')),
                     f'country_event = {{ id = {ns}.4 hours = 1 }}'])
    after = '\n'.join([f'set_country_flag = {finished}',
        branch(f'has_country_flag = {pending}',maker_result),
        atlas('after'), f'{ns}_suite = yes',
        f'news_event = {{ id = {ns}.3 }}',log('END')])
    suite = f'{ns}_suite = {{\n' + branch(ready,main,branch(
        f'has_country_flag = {done} has_country_flag = {finished}',
        emit('replay','PASS','finished','blocked','blocked','-'))) + '\n}\n'
    finish_guard = f'{guard} has_country_flag = {done} NOT = {{ has_country_flag = {finished} }}'
    set_capital = branch(' '.join([f'has_country_flag = {pending}', *(cond for cond, _ in maker_checks)]),
                         f'{maker_tag} = {{ set_capital = {{ state = 5 remember_old_capital = no }} }}')
    news_guard = f'{anchor} tag = GER is_ai = no has_country_flag = {finished}'
    event = f'''add_namespace = {ns}
country_event = {{
 id = {ns}.1 title = {ns}_run desc = {ns}_description is_triggered_only = yes
 trigger = {{ {ready} }}
 option = {{ name = {ns}_confirm ai_chance = {{ factor = 0 }} {ns}_suite = yes }}
 option = {{ name = {ns}_cancel }}
}}
country_event = {{
 id = {ns}.2 hidden = yes is_triggered_only = yes trigger = {{ {finish_guard} }}
 immediate = {{ {branch(finish_guard, after)} }}
}}
country_event = {{
 id = {ns}.4 hidden = yes is_triggered_only = yes trigger = {{ {finish_guard} }}
 immediate = {{ {branch(finish_guard, set_capital + f' country_event = {{ id = {ns}.2 hours = 1 }}')} }}
}}
news_event = {{
 id = {ns}.3 title = {ns}_news desc = {ns}_description major = yes is_triggered_only = yes
 immediate = {{ {branch(news_guard,emit('news_event','PASS','queued','delivered','delivered','-'))} }}
 option = {{ name = {ns}_close ai_chance = {{ factor = 0 }}
  {branch(f'{news_guard} NOT = {{ has_country_flag = {ns}_news_ack }}',
          f'set_country_flag = {ns}_news_ack '+emit('news_display','PASS','shown','human_ack','human_ack','-'))}
 }}
}}
'''
    decision = (f'{ns}_category = {{ {ns}_run = {{ cost = 0 fire_only_once = yes '
                f'visible = {{ {ready} }} available = {{ {ready} }} ai_will_do = {{ factor = 0 }} '
                f'complete_effect = {{ country_event = {{ id = {ns}.1 }} }} }} }}\n')
    return ns, event, decision, f'{ns}_category = {{ visible = {{ {guard} }} }}\n', suite
