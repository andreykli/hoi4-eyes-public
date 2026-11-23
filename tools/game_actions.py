"""Present exact typed proposals and report the existing treaty lifecycle."""

from guarded_treaty_effects import EFFECTS, NEW_ACTIONS, proposal_text
from hoi4_country_tags import is_country_tag

_FRENCH = {
    'cancel_nap': 'Mettre fin au pacte de non-agression',
    'cancel_guarantee': 'Retirer la garantie',
    'cancel_military_access': 'Retirer l’accès militaire',
    'faction_dismantle': 'Dissoudre la faction',
    'faction_create': 'Créer une faction sans membres',
}
_TEXT = {
    'button': ('Treaty actions', 'Действия по договорам', 'Actions des traités'),
    'preview': ('Review the proposal before sending it to the other country.',
                'Проверьте предложение перед отправкой другой стране.',
                'Vérifiez la proposition avant de l’envoyer à l’autre pays.'),
    'send': ('Send proposal', 'Отправить предложение', 'Envoyer la proposition'),
    'sent': ('Proposal saved. Continue negotiations in the diplomatic chat.',
             'Предложение сохранено. Продолжите переговоры в дипломатическом чате.',
             'Proposition enregistrée. Poursuivez les négociations dans le dialogue diplomatique.'),
    'close': ('Close', 'Закрыть', 'Fermer'),
    'prepared': ('Prepared', 'Подготовлено', 'Préparé'),
    'confirmed': ('Terms confirmed', 'Условия подтверждены', 'Conditions confirmées'),
    'sent_to_game': ('Sent to HOI4', 'Передано в HOI4', 'Transmis à HOI4'),
    'verified': ('Verified by HOI4', 'Подтверждено HOI4', 'Vérifié par HOI4'),
    'failed': ('Failed', 'Ошибка исполнения', 'Échec'),
    'needs_review': ('Needs review', 'Требует проверки', 'À vérifier'),
}


def presentation(language):
    index = {'ru': 1, 'fr': 2}.get(language, 0)
    names = {action: _FRENCH[action] if index == 2 else EFFECTS[action][2 + index] for action in NEW_ACTIONS}
    return {'names': names, 'text': {key: values[index] for key, values in _TEXT.items()}}


def catalog(language):
    from action_registry import ActionRegistry
    from state_operations import ACTIONS
    index = {'ru': 1, 'fr': 2}.get(language, 0)
    names = {
        'non_aggression_pact':('Non-aggression pact','Пакт о ненападении','Pacte de non-agression'),
        'guarantee':('Guarantee','Гарантия независимости','Garantie d’indépendance'),
        'embargo':('Impose embargo','Ввести эмбарго','Imposer un embargo'),
        'lift_embargo':('Lift embargo','Снять эмбарго','Lever l’embargo'),
        'military_access':('Military access','Военный доступ','Accès militaire'),
        'docking_rights':('Docking rights','Право захода в порт','Droits d’amarrage'),
        'air_base_access':('Air base access','Доступ на авиабазы','Accès aux bases aériennes'),
        'faction_join':('Join faction','Вступить во фракцию','Rejoindre la faction'),
        'faction_leave':('Leave faction','Выйти из фракции','Quitter la faction'),
        'market_access':('Market access','Доступ к рынку','Accès au marché'),
        'truce':('Truce','Перемирие','Trêve'),
        'white_peace':('White peace','Белый мир','Paix blanche'),
        'peace_conference':('Peace conference','Мирная конференция','Conférence de paix'),
        'territory_partition':('Transfer states','Передать области','Transférer des régions'),
        'political_annexation':('Political accession','Политическое присоединение','Rattachement politique'),
    }
    localized = presentation(language)['names']
    result = [{'id':row['action_id'], 'category':'diplomatic',
               'name':localized.get(row['action_id']) or names[row['action_id']][index],
               'supported':row['implemented_in_eyes'], 'live_verified':row['live_verified'],
               'dlc':'By Blood Alone' if row['action_id'] in {'embargo','lift_embargo'} else None}
              for row in ActionRegistry().all()]
    result.extend({'id':action,'category':meta[0],'name':meta[1+index],
                   'supported':True,'live_verified':False,'dlc':None}
                  for action,meta in ACTIONS.items())
    result.extend((
        {'id':'recognition','category':'diplomatic',
         'name':('Recognition and withdrawal','Признание и отзыв','Reconnaissance et retrait')[index],
         'supported':True,'live_verified':False,'dlc':None},
        {'id':'country_maker','category':'administrative',
         'name':('Country Maker','Конструктор государств','Création d’États')[index],
         'supported':True,'live_verified':True,'dlc':None},
    ))
    return result


def workspace(language):
    index = {'ru':1,'fr':2}.get(language,0)
    translations = {
        'title':('Game actions','Игровые действия','Actions de jeu'),
        'search':('Search','Поиск','Rechercher'),
        'all':('All categories','Все категории','Toutes les catégories'),
        'diplomatic':('Diplomatic','Дипломатические','Diplomatiques'),
        'domestic':('Domestic','Внутригосударственные','Intérieures'),
        'administrative':('Administrative','Административные','Administratives'),
        'target':('Owned and controlled state','Область под владением и контролем','Région possédée et contrôlée'),
        'capital':('Current capital','Текущая столица','Capitale actuelle'),
        'current_level':('Current infrastructure level','Текущий уровень инфраструктуры','Niveau d’infrastructure actuel'),
        'level':('New infrastructure level','Новый уровень инфраструктуры','Nouveau niveau d’infrastructure'),
        'choose':('Select…','Выберите…','Sélectionner…'),
        'history':('Action history','История действий','Historique'),
        'output':('Prepare package','Подготовить пакет','Préparer le paquet'),
        'prepared':('Prepared; confirm in HOI4 after installing the package.','Пакет подготовлен. После установки подтвердите действие в HOI4.','Paquet préparé. Confirmez dans HOI4 après l’installation.'),
        'confirm_prepare':('Prepare this exact action for manual confirmation in HOI4?','Подготовить именно это действие для ручного подтверждения в HOI4?','Préparer cette action précise pour confirmation manuelle dans HOI4 ?'),
        'missing_snapshot':('Open the matching campaign and export a fresh Atlas snapshot.','Откройте нужную кампанию и экспортируйте свежий снимок Atlas.','Ouvrez la campagne correspondante et exportez un nouvel instantané Atlas.'),
        'unverified_atlas_geometry':('These operations require verified vanilla geometry.','Для этих действий нужна подтверждённая ванильная геометрия.','Ces actions exigent une géométrie vanilla vérifiée.'),
        'open_diplomacy':('Continue in Diplomacy','Открыть дипломатию','Ouvrir la diplomatie'),
        'open_recognition':('Open Recognition Atlas','Открыть атлас признания','Ouvrir l’atlas de reconnaissance'),
        'open_maker':('Open Country Maker','Открыть конструктор','Ouvrir la création d’États'),
        'unsupported':('Executor unavailable','Исполнитель недоступен','Exécution indisponible'),
        'verified':('Verified in HOI4','Подтверждено в HOI4','Vérifié dans HOI4'),
        'precondition_failed':('Starting conditions changed','Исходные условия изменились','Conditions initiales modifiées'),
        'postcondition_failed':('Outcome check failed','Итоговая проверка не прошла','Vérification du résultat échouée'),
        'qa':('Automatic check','Автоматическая проверка','Contrôle automatique'),
        'qa_running':('Running offline checks…','Идут офлайн-проверки…','Contrôles hors jeu en cours…'),
        'qa_passed':('Offline checks passed','Офлайн-проверки пройдены','Contrôles hors jeu réussis'),
        'qa_failed':('Offline checks failed','Офлайн-проверки прерваны или не прошли','Contrôles hors jeu échoués'),
        'qa_manual':('In-game evidence requires a separate game session.','Для игрового подтверждения нужна отдельная партия.','Les preuves en jeu exigent une session distincte.'),
        'qa_live':('HOI4 live test','Автотест в HOI4','Test en jeu HOI4'),
        'qa_live_prepare':('Prepare separate QA mod','Подготовить отдельный QA-мод','Préparer le mod QA séparé'),
        'qa_live_confirm':('Create a new QA mod entry for a disposable campaign?', 'Создать запись отдельного QA-мода для одноразовой кампании?', 'Créer un mod QA distinct pour une campagne de test ?'),
        'qa_live_instructions':('Enable only this mod in a new playset. Start a new 1936 GER campaign, run its QA decision and advance three hours.',
          'Включите только этот мод в новом наборе. Начните новую кампанию 1936 за Германию, подтвердите решение QA и пропустите три игровых часа.',
          'Activez uniquement ce mod dans un nouveau playset. Lancez une nouvelle partie GER en 1936, confirmez la décision QA et avancez de trois heures.'),
        'qa_live_idle':('No QA run prepared','Прогон ещё не подготовлен','Aucun test préparé'),
        'qa_live_waiting':('Waiting for the QA decision in HOI4','Ожидание решения QA в HOI4','En attente de la décision QA dans HOI4'),
        'qa_live_running':('Game test in progress','Игровая проверка выполняется','Test en cours'),
        'qa_live_review':('Game run finished; check blocked and unverified rows','Прогон завершён. Проверьте заблокированные и неподтверждённые строки','Test terminé. Contrôlez les résultats bloqués et non vérifiés'),
        'qa_live_passed':('All live checks passed','Все игровые проверки пройдены','Tous les tests en jeu ont réussi'),
        'qa_live_failed':('Game test failed','Игровая проверка выявила ошибки','Le test a détecté des erreurs'),
        'qa_live_close_game':('Close HOI4 before preparing the mod.','Закройте HOI4 перед подготовкой мода.','Fermez HOI4 avant de préparer le mod.'),
        'qa_live_version':('The QA overlay requires a reviewed HOI4 1.19.5.0 or 1.20.0.0 installation.','Для QA-мода нужна проверенная установка HOI4 1.19.5.0 или 1.20.0.0.','Le mod QA exige une installation vérifiée de HOI4 1.19.5.0 ou 1.20.0.0.'),
        'qa_live_static_failed':('The generated QA scripts did not pass validation.','Игровые скрипты QA не прошли проверку.','Les scripts QA n’ont pas passé la validation.'),
        'qa_live_playset':('Select the standalone QA playset in the launcher.','Выберите отдельный набор QA в лаунчере.','Sélectionnez le playset QA distinct dans le lanceur.'),
    }
    return {key:values[index] for key,values in translations.items()}


def proposal(action, chat):
    actor = chat.get('player_country')
    parties = chat.get('participants', [])
    if (action not in NEW_ACTIONS or chat.get('chat_mode') == 'ai_to_ai'
            or len(parties) != 2 or len(set(parties)) != 2 or actor not in parties
            or not all(is_country_tag(tag) for tag in parties)):
        raise ValueError('Select a bilateral player diplomatic chat and a supported action.')
    target = next(tag for tag in parties if tag != actor)
    return proposal_text(action, actor, target, chat.get('response_language', 'en'))


def native_review_strings(package, language):
    action = package['action_id']
    actor, target = (package['game_effect'][name] for name in ('actor', 'target'))
    ns = package['event_id'].split('.')[0]
    title = presentation(language)['names'][action]
    if language == 'ru':
        description = f'{actor} и {target}. Перед применением игра перепроверит согласованные условия.'
        confirm, blocked, cancel = ('Подтвердить действие', 'Игровые условия изменились', 'Закрыть без изменений')
    else:
        description = f'{actor} et {target}. Le jeu vérifiera à nouveau les conditions avant d’appliquer l’action.'
        confirm, blocked, cancel = ('Confirmer l’action', 'Les conditions ont changé', 'Fermer sans appliquer')
    strings = [('_review',title),('.1.t',title),('.1.d',description),('.1.a',confirm),('.1.b',blocked),('.cancel',cancel)]
    if action == 'faction_create':
        strings.append(('_faction', f'Дипломатический союз {actor}' if language == 'ru' else f'Alliance diplomatique {actor}'))
    if language == 'fr' and f'id = {ns}.2' in package['event_file']:
        a, b = f'[{actor}.GetName]', f'[{target}.GetName]'
        outcome = {
            'cancel_nap': f'{a} et {b} ont mis fin à leur pacte de non-agression.',
            'cancel_guarantee': f'{a} a retiré sa garantie d’indépendance à {b}.',
            'cancel_military_access': f'{a} a retiré l’accès militaire accordé à {b}.',
            'faction_dismantle': f'La faction dirigée par {a}, dont {b} était membre, a été dissoute.',
            'faction_create': f'{a} a créé une faction avec le consentement de {b}.',
        }[action]
        strings.extend([('.2.t',f'{actor} / {target}'),('.2.d',outcome)])
    return '\n'.join(f' {ns}{key}:0 "{value}"' for key,value in strings)


def reported_status(agreement):
    status = agreement.get('status')
    if status in {'execution_failed', 'failed'}:
        return 'failed'
    result = agreement.get('execution_result') or {}
    if status == 'execution_confirmed' and result.get('status') == status and result.get('source') == 'hoi4_game_log':
        return 'verified'  # New native markers follow the script's postcondition.
    delivery = agreement.get('native_delivery') or {}
    if delivery.get('sent'):
        return 'sent_to_game'
    if delivery.get('packet'):
        return 'prepared'
    if status in {'approved', 'agreed', 'awaiting_approval'}:
        return 'confirmed'
    return 'needs_review'
