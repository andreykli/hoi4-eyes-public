"""Authored conference UI messages, not historical quotations or treaty terms."""
import hashlib
PHRASES={
 'en':{
 'opening':('The delegations may now address the stated diplomatic objective.','The conference opens for discussion of its agreed agenda.','The meeting begins; no treaty is signed by opening it.'),
 'invitation':('An invitation is being considered; attendance is not presumed.','The proposed participant must be available before joining.','An invitation alone grants no negotiating mandate.'),
 'advisory':('The guest may advise the delegation but cannot sign on its behalf.','Participation is advisory and creates no state commitment.','The invited participant has no authority to bind the country.'),
 'decline':('The requested participant is unavailable for this meeting.','This invitation cannot be accepted under the recorded availability rules.','The meeting must proceed without the requested participant.'),
 'reschedule':('The participant is already engaged in another meeting.','A later meeting may be considered after the existing engagement.','The invitation requires a different date because of a scheduling conflict.'),
 'review':('The delegation reviews the engine-selected position before its public wording.','Personal advice is recorded separately from the official state position.','A dissenting view does not replace the authorised state decision.'),
 'conclusion':('The conference has concluded; any treaty still follows its own confirmation procedure.','The scheduled meeting is over; no game effect is inferred from this notice.','The closing of the conference does not itself sign an agreement.'),
 'intelligence':('No verified game intelligence is available through this channel.','The system has no reliable observation of foreign intelligence capabilities.','Unknown intelligence access must not be treated as surveillance.')},
 'ru':{
 'opening':('Делегации могут приступить к обсуждению заявленной дипломатической цели.','Конференция открывается для обсуждения её повестки.','Встреча начинается; её открытие не означает подписания договора.'),
 'invitation':('Приглашение рассматривается; участие пока не предполагается.','Перед участием необходимо проверить доступность приглашённого.','Само приглашение не предоставляет дипломатических полномочий.'),
 'advisory':('Гость может консультировать делегацию, но не подписывать договор от её имени.','Участие носит совещательный характер и не создаёт обязательств государства.','Приглашённый участник не уполномочен связывать государство обязательствами.'),
 'decline':('Запрошенный участник недоступен для этой встречи.','Правила доступности не позволяют принять это приглашение.','Встречу придётся проводить без запрошенного участника.'),
 'reschedule':('Участник уже занят на другой встрече.','Следующую встречу можно рассмотреть после завершения текущей.','Из-за пересечения встреч потребуется другая дата.'),
 'review':('Делегация рассматривает выбранную движком позицию до её публичной формулировки.','Личный совет сохраняется отдельно от официальной позиции государства.','Особое мнение не заменяет уполномоченное государственное решение.'),
 'conclusion':('Конференция завершилась; договор по-прежнему требует своей процедуры подтверждения.','Запланированная встреча окончена; это сообщение не подтверждает игровой эффект.','Закрытие конференции само по себе не означает подписания соглашения.'),
 'intelligence':('Этот канал не предоставляет подтверждённых игровых разведданных.','Достоверных наблюдений о возможностях иностранной разведки нет.','Неизвестный доступ разведки нельзя считать установленной слежкой.')}}

def render(kind,key):
    from diplomatic_catalog import texts, LOCALE_DIRS
    category={'opening':'conference_open','advisory':'delegation','decline':'participant_unavailable','review':'internal'}.get(kind,kind)
    result={}
    for lang in LOCALE_DIRS:
        options=list(PHRASES.get(lang,{}).get(kind,()))+texts(category,lang)
        if options:result[lang]=options[int(hashlib.sha256(str(key).encode()).hexdigest()[:8],16)%len(options)]
    return result

def counts():return {lang:sum(map(len,rows.values())) for lang,rows in PHRASES.items()}
