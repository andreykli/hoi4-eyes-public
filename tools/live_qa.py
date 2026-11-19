"""Prepare and read an isolated HOI4 QA playset; never starts HOI4."""
from collections import Counter
import json
from pathlib import Path
import re
from uuid import uuid4

from hub_control import atomic_json
from live_qa_scripts import EXTRA, cases, scripts
from user_storage import runtime_dir

MARKER = 'EYES_LIVE_QA_V1|'
STAMP = re.compile(r'^\[\d{2}:\d{2}:\d{2}\]\[[0-9.]+\]:\s+')
NONCE = re.compile(r'[0-9a-f]{16}')
TAG = re.compile(r'[A-Z][A-Z0-9]{2}')
CASES = {**{row.key:row.title for row in cases()},**EXTRA}
STATES = {'5','8','65'}
STATUS = {'PASS','FAIL','BLOCKED'}
TAGS = ('QZX','QZY')
EXPECTED = {**{case.key:case.expected for case in cases()}, 'precondition':'blocked','replay':'blocked',
            'country_maker':'new_country','news_event':'delivered','news_display':'human_ack'}
RU = {
    'absent':'Отсутствует', 'active':'Действует', 'ger_leads':'Германия возглавляет фракцию',
    'qa_leads':'Тестовая страна возглавляет пустую фракцию',
    'state_64':'Столица в области 64', 'state_65':'Столица в области 65',
    'core_ger':'Область является core Германии', 'no_core':'У Германии нет core',
    'level_3':'Инфраструктура уровня 3', 'level_4':'Инфраструктура уровня 4',
    'ger_controls_5':'Область 5 принадлежит Германии и контролируется ею',
    'fra_controls_5':'Область 5 принадлежит Франции и контролируется ею',
    'unknown':'Позиция не установлена', 'recognized':'Признано', 'not_recognized':'Признание отозвано',
    'wrong_owner':'Неверный владелец', 'blocked':'Действие заблокировано',
    'finished':'Прогон уже запускался', 'tag_absent':'Тег свободен',
    'new_country':'Государство создано, столица и cores проверены',
    'queued':'Новость отправлена', 'delivered':'Игровое событие получено', 'shown':'Получено подтверждение игрока',
    'human_ack':'Выбрана кнопка подтверждения новости', 'postcondition_false':'Проверка состояния не прошла',
    'precondition_false':'Исходное условие ложно', 'unchanged':'Эффект пропущен',
    'guard_bypassed':'Ошибочно выполнено заблокированное действие',
    'guard_rejected':'Проверка исходных условий остановила действие',
    'wrong_postcondition':'Фактическое состояние расходится с ожидаемым',
    'country_missing':'Тестовая страна не появилась',
    'wrong_controller':'Неверный контролёр области',
    'missing_core':'У новой страны отсутствует назначенный core',
    'extra_cores':'Обнаружены лишние cores',
    'extra_territory':'Новая страна владеет областью вне сценария',
    'country_is_subject':'Новая страна оказалась зависимой',
    'wrong_capital':'Столица новой страны находится вне области 5',
}


def decode(line, nonce, *, legacy=False):
    if not STAMP.match(line):
        return None
    fields = STAMP.sub('',line,count=1).strip().split('|')
    if fields[:3] != [MARKER[:-1],nonce,'GER']:
        return None
    parts = fields[3:]
    if parts in (['BEGIN'],['END']):
        return tuple(parts)
    if (len(parts)==5 and parts[0]=='ATLAS' and parts[1] in {'before','after'} and parts[2] in STATES
            and all(TAG.fullmatch(tag) for tag in parts[3:])):
        return tuple(parts)
    if (len(parts)==7 and parts[0]=='CASE' and parts[1] in CASES and parts[2] in STATUS
            and all(re.fullmatch(r'[a-z0-9_-]{1,40}',field) for field in parts[3:])):
        expected = 'ger_leads' if legacy and parts[1] == 'create_faction' else EXPECTED[parts[1]]
        if parts[4]!=expected or (parts[2]=='PASS' and (parts[4]!=parts[5] or parts[6]!='-')):
            return None
        return tuple(parts)
    return None


def report(config, lines, bridge_rows=()):
    bridge_rows = set(bridge_rows)
    nonce = config['id']
    state_rows = {'before':{},'after':{}}
    rows = {key:{'case':key,'title':title,'status':'NOT VERIFIED'} for key,title in CASES.items()}
    started = finished = False
    errors = []
    for line in lines:
        row = decode(line,nonce,legacy=config.get('fixture_version') == 1)
        if row is None:
            continue
        if row==('BEGIN',):
            if started:
                errors.append('Дублирующее начало прогона')
            started = True
            continue
        if not started:
            continue
        if row==('END',):
            if finished:
                errors.append('Дублирующее завершение прогона')
            finished = True
            continue
        if row[0]=='ATLAS':
            _, stage, state, owner, controller = row
            if finished or state in state_rows[stage]:
                errors.append(f'Повторная строка карты для области {state}')
            else:
                state_rows[stage][state] = {'owner':owner,'controller':controller}
            continue
        _, key, status, before, expected, actual, reason = row
        if finished and key not in {'news_event','news_display'}:
            errors.append(f'Квитанция после завершения: {key}')
            continue
        item = rows[key]
        if item['status']!='NOT VERIFIED':
            item.update(status='FAIL',reason='Повторная игровая квитанция')
            item['ru']['reason'] = item['reason']
            continue
        item.update(status=status,before=before,expected=expected,actual=actual,reason=reason,
                    bridge=row in bridge_rows)
        item['ru'] = {key:RU.get(value,value) for key,value in
                      (('before',before),('expected',expected),('actual',actual),('reason',reason))}
    if finished and any(set(batch)!=STATES for batch in state_rows.values()):
        errors.append('Неполное наблюдение Atlas')
    if finished and any(batch.get('8') != {'owner':TAGS[0],'controller':TAGS[0]} for batch in state_rows.values()):
        errors.append('Тестовая область не совпадает с нативной меткой')
    if rows['country_maker']['status']=='PASS' and state_rows['after'].get('5') not in (
            None,{'owner':TAGS[1],'controller':TAGS[1]}):
        errors.append('Результат создания страны расходится с наблюдением Atlas')
    counts = Counter(row['status'] for row in rows.values())
    if errors or counts['FAIL']:
        phase = 'failed'
    elif finished and counts['PASS']==len(rows):
        phase = 'passed'
    elif finished:
        phase = 'review'
    else:
        phase = 'running' if started else 'waiting'
    return {'id':nonce, 'phase':phase, 'rows':list(rows.values()), 'counts':dict(counts), 'errors':errors,
            'atlas':{'branch':nonce,'scope':'qa_state_subset',**state_rows},
            'bridge':{'seen':sum(row.get('bridge') is True for row in rows.values()),
                      'expected':sum(row['status']!='NOT VERIFIED' for row in rows.values()),
                      'start':('BEGIN',) in bridge_rows,'end':('END',) in bridge_rows},
            'evidence':'native_game_log', 'begin':started, 'end':finished,
            'mod':config['descriptor']}


def files_for(game, nonce):
    if not NONCE.fullmatch(nonce):
        raise ValueError('invalid_qa_nonce')
    anchor, created = TAGS
    registered = set()
    for path in (game/'common/country_tags').glob('*.txt'):
        registered.update(re.findall(r'^\s*([A-Z][A-Z0-9]{2})\s*=',path.read_text('utf-8-sig',errors='replace'),re.M))
    if set(TAGS)&registered:
        raise ValueError('qa_tag_collision')
    ns, event, decisions, category, suite = scripts(nonce,anchor,created)
    original = list((game/'history/states').glob('8-*.txt'))
    if len(original)!=1:
        raise ValueError('qa_state_not_found')
    text = original[0].read_text('utf-8-sig')
    text, number = re.subn(r'^(\s*)owner\s*=\s*LUX\b',rf'\g<1>owner = {anchor}\n\g<1>add_core_of = {anchor}',text,flags=re.M)
    if number!=1:
        raise ValueError('qa_fixture_geometry_changed')
    files = {
        f'history/states/{original[0].name}':text,
        f'events/{ns}.txt':event,
        f'common/decisions/{ns}.txt':decisions,
        f'common/decisions/categories/{ns}.txt':category,
        f'common/scripted_effects/{ns}.txt':suite,
        f'common/country_tags/{ns}.txt':f'{anchor} = "countries/{ns}.txt"\n{created} = "countries/{ns}.txt"\n',
        f'common/countries/{ns}.txt':'graphical_culture = western_european_gfx\ngraphical_culture_2d = western_european_2d\ncolor = { 90 130 120 }\n',
        f'history/countries/{anchor} - {ns}.txt':f'capital = 8\nset_country_flag = {ns}_fresh\nset_rule = {{ can_create_factions = yes }}\n',
        f'history/countries/{created} - {ns}.txt':'capital = 5\n',
    }
    for locale, run, warning, go, cancel, news, close in (
        ('english','Run HOI4-EYES QA','Disposable 1936 QA campaign. This changes test regions and diplomatic relations.','Run all native tests','Cancel','HOI4-EYES QA news','Acknowledge'),
        ('russian','Запустить автотест HOI4-EYES','Одноразовая кампания 1936 года. Проверка изменит тестовые области и дипломатические отношения.','Запустить все игровые тесты','Отмена','Новость HOI4-EYES QA','Подтвердить показ'),
        ('french','Lancer le test HOI4-EYES','Campagne de test de 1936. Le test modifie des régions et relations diplomatiques.','Lancer les essais','Annuler','Actualité HOI4-EYES QA','Confirmer l’affichage'),
    ):
        values = {f'{ns}_category':'HOI4-EYES QA',f'{ns}_run':run,f'{ns}_description':warning,
                  f'{ns}_confirm':go,f'{ns}_cancel':cancel,f'{ns}_news':news,f'{ns}_close':close,
                  'EYES_QA_FACTION':'EYES QA',anchor:'EYES QA Anchor',created:'EYES QA State'}
        files[f'localisation/{locale}/{ns}_l_{locale}.yml'] = '\ufeffl_'+locale+':\n'+''.join(
            f' {key}:0 "{value}"\n' for key,value in values.items())
    return files


class LiveQA:
    def __init__(self, runtime=None, documents=None, game_log=None):
        self.root = Path(runtime or runtime_dir())/'qa-live'
        self.documents = Path(documents or Path.home()/'Documents/Paradox Interactive/Hearts of Iron IV')
        self.game_log = Path(game_log or self.documents/'logs/game.log')
        self.cache = None

    def selected(self):
        try:
            info = json.loads((self.root/'current.json').read_text('utf8'))
            identifier = info['id']
            if NONCE.fullmatch(identifier) and info['descriptor']==f'HOI4_EYES_QA_{identifier}.mod':
                return {'fixture_version':1, **info}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def prepare(self, game=None):
        from hoi4_static_audit import audit
        from treaty_package_install import hoI4_running
        from validate_hoi4_gui import discover_hoi4_install, parse_pdx
        if hoI4_running() is not False:
            raise ValueError('qa_close_game')
        game = game or discover_hoi4_install()
        if game is None or json.loads((game/'launcher-settings.json').read_text('utf-8-sig')).get('rawVersion')!='1.19.5.0':
            raise ValueError('qa_unsupported_version')
        selected = self.selected()
        if selected and selected['fixture_version'] == 2:
            current = self.status()
            if current.get('phase')=='waiting' and (self.documents/'mod'/current['mod']).is_file():
                return current
        nonce = uuid4().hex[:16]
        folder = self.root/nonce
        mod = self.documents/'mod'
        mod.mkdir(parents=True,exist_ok=True)
        descriptor = mod/f'HOI4_EYES_QA_{nonce}.mod'
        project = folder/'mod'
        data = files_for(game,nonce)
        for name, content in data.items():
            if name.startswith(('events/','common/decisions/','common/scripted_effects/')):
                parse_pdx(content)
        text = f'name="HOI4-EYES QA {nonce}"\nsupported_version="1.19.5.*"\n'
        for name, content in {**data,'descriptor.mod':text}.items():
            destination = project/name
            destination.parent.mkdir(parents=True,exist_ok=True)
            with destination.open('x',encoding='utf8') as output:
                output.write(content)
        checks = audit(project,game)
        if checks['errors']:
            atomic_json(folder/'static-errors.json',checks)
            raise ValueError('qa_static_validation_failed')
        with descriptor.open('x',encoding='utf8') as output:
            output.write(text+f'path="{project.as_posix()}"\n')
        info = {'id':nonce,'descriptor':descriptor.name, 'name':f'HOI4-EYES QA {nonce}', 'fixture_version':2}
        atomic_json(self.root/'current.json',info)
        return self.status()

    def playset_selected(self, info):
        try:
            settings = json.loads((self.documents/'dlc_load.json').read_text('utf-8-sig'))
            return settings.get('enabled_mods') == ['mod/'+info['descriptor']]
        except (OSError,ValueError,AttributeError):
            return False

    def record_bridge(self, line):
        info = self.selected()
        row = decode(line,info['id'],legacy=info['fixture_version'] == 1) if info else None
        if row is None:
            return False
        target = self.root/info['id']/'bridge.jsonl'
        with target.open('a',encoding='utf8') as output:
            output.write(json.dumps(row)+'\n')
        return True

    def status(self):
        info = self.selected()
        if not info:
            return {'phase':'idle'}
        folder = self.root/info['id']
        target = folder/'report.json'
        witness = folder/'bridge.jsonl'
        def signature(path):
            return path.stat().st_mtime_ns if path.is_file() else None
        selected = self.playset_selected(info)
        key = (info['id'],signature(self.game_log),signature(witness),selected)
        if self.cache and self.cache[0]==key:
            return self.cache[1]
        lines = []
        if self.game_log.is_file():
            with self.game_log.open('rb') as stream:
                stream.seek(max(0,self.game_log.stat().st_size-4_000_000))
                lines = stream.read(4_000_000).decode('utf8',errors='replace').splitlines()
        bridge = set()
        if witness.is_file():
            for line in witness.read_text('utf8').splitlines():
                try:
                    bridge.add(tuple(json.loads(line)))
                except (ValueError,TypeError):
                    continue
        result = report(info,lines,bridge)
        try:
            previous = json.loads(target.read_text('utf8'))
        except (OSError, ValueError):
            previous = {}
        if not isinstance(previous, dict):
            previous = {}
        if not result['begin'] and previous.get('begin'):
            result = dict(previous)
            if not previous.get('end'):
                result.update(phase='review',errors=list(dict.fromkeys([
                    *previous['errors'],'Журнал сменился до завершения прогона'])))
        result.update(report=str(target),name=info['name'],playset_selected=selected)
        if result['begin'] and result != {**previous, 'playset_selected':selected}:
            atomic_json(target,result)
        self.cache = (key,result)
        return result
