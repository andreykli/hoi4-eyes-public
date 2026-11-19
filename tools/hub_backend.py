"""One desktop controller over existing Experimental services, never a new engine."""
from ui_locale import tr
import os,secrets,shutil,subprocess,sys,time,json
from pathlib import Path
from experimental_stack import stack_environment,listener_pids
from restart_chat_only import processes,ROOT
from stack_readiness import ready_record
from bridge_instance import bridge_instance_lock
from hub_control import command
import requests
from user_storage import runtime_dir
from model_profile import selected_model

URL='http://127.0.0.1:5016'

class HubBackend:
 def __init__(self,root=ROOT):
  self.root=Path(root).resolve()
  if not (self.root/'HOI4_EYES_Experimental.mod').is_file():raise ValueError('HOI4-EYES application files are missing')
  self.runtime=runtime_dir(self.root);self.owned={};self.owner=secrets.token_hex(32)
  self.desired=False;self.recoveries=0

 def _http(self,method,path,**kwargs):
  response=requests.request(method,URL+path,timeout=kwargs.pop('timeout',3),**kwargs)
  if response.status_code>=400:
   try:data=response.json()
   except ValueError:data={}
   raise RuntimeError(data.get('detail') or (tr('Component busy. Wait for the request to finish.', 'Компонент занят; дождитесь завершения запроса.') if data.get('error')=='busy' else
                      f"{tr('Request failed (', 'Запрос не выполнен (')}{response.status_code}): {data.get('error') or tr('check the logs','смотрите журналы')}"))
  return response.json()

 def _token(self):return (self.runtime/'experimental_auth.token').read_text(encoding='ascii').strip()

 def _verify_server(self,rows=None):
  rows=processes() if rows is None else rows;owners=listener_pids()
  if not owners or any(rows.get(pid,{}).get('kind')!='server' for pid in owners):
   raise RuntimeError(tr('Port 5016 is not owned by the expected HOI4-EYES Flask process.', 'Порт 5016 не принадлежит подтверждённому Flask HOI4-EYES. Ничего не запущено и не остановлено.'))
  return owners

 def _remember(self,kind,child):
  # Retain the actual process handle; never reconstruct ownership from a PID file.
  self.owned[kind]={'process':child,'created':None}
  if kind!='ollama':
   row=processes().get(child.pid)
   if row:self.owned[kind]['created']=row.get('created')

 def _spawn_server(self,env):
  with (self.runtime/'flask.log').open('a',encoding='utf8') as log:
   child=subprocess.Popen([sys.executable,str(self.root/'tools/experimental_stack.py'),'--root',str(self.root),'--serve'],
      cwd=self.root,env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
  self._remember('server',child)
  return child

 def ollama_status(self):
  try:
   r=requests.get('http://127.0.0.1:11434/api/tags',timeout=2);r.raise_for_status()
   installed=any(m.get('name')==selected_model() for m in r.json().get('models',[]))
   return {'ready':installed,'detail':tr('Qwen is installed.', 'Qwen установлена; генерация не проверялась') if installed else tr('Ollama is available but Qwen is missing.', 'Служба доступна, Qwen не найдена. Модели не скачиваются.')}
  except (requests.RequestException,ValueError,AttributeError,TypeError) as e:return {'ready':False,'detail':tr('Ollama unavailable: ', 'Ollama недоступна: ')+str(e)[:140]}

 def start_ollama(self):
  if self.ollama_status()['ready']:return tr('Using the running Ollama service.', 'Используется уже работающая Ollama; владение не присваивается.')
  if listener_pids(11434):return tr('Ollama port is occupied. Fallback replies remain available.', 'Порт Ollama уже занят. Существующая служба не изменена; доступен резервный ответ.')
  previous=self.owned.get('ollama')
  if previous and previous['process'].poll() is None:return tr('Ollama is starting.', 'Ollama запускается; дождитесь готовности.')
  portable=Path(os.environ.get('LOCALAPPDATA',''))/'Programs/Ollama/ollama.exe'
  executable=shutil.which('ollama') or (str(portable) if portable.is_file() else None)
  if not executable:return tr('Ollama was not found in PATH. Fallback replies remain available.', 'Ollama не найдена в PATH. Доступен шаблонный резерв; установка не выполнялась.')
  self.runtime.mkdir(exist_ok=True)
  with (self.runtime/'ollama.log').open('a',encoding='utf8') as log:
   child=subprocess.Popen([executable,'serve'],cwd=self.root,env=dict(os.environ,OLLAMA_HOST='127.0.0.1:11434'),
      stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
  self._remember('ollama',child)
  for _ in range(20):
   if child.poll() is not None:return tr('Ollama exited. See runtime/ollama.log.', 'Ollama завершилась: смотрите runtime/ollama.log. Другие процессы не изменены.')
   if self.ollama_status()['ready']:return tr('Local Ollama started.', 'Локальная Ollama запущена этим меню.')
   time.sleep(.5)
  return tr('Ollama is still starting. Fallback replies remain available.', 'Ollama ещё не готова; Flask может работать с резервным генератором.')

 def start(self,recovery=False):
  if not recovery:self.recoveries=0
  self.runtime.mkdir(exist_ok=True)
  with bridge_instance_lock(self.runtime/'stack_start.lock') as locked:
   if not locked:raise RuntimeError(tr('Another launch is in progress.', 'Другой запуск ещё выполняется. Дождитесь готовности; второй экземпляр не создаётся.'))
   rows=processes();owners=listener_pids()
   if owners:self._verify_server(rows)
   note=self.start_ollama()
   if owners:self._verify_server(rows)
   else:
    child=self._spawn_server(stack_environment(self.root,self.owner))
    for _ in range(45):
     if child.poll() is not None:raise RuntimeError(tr('Flask exited. See runtime/flask.log.', 'Flask завершился: runtime/flask.log. Автоматического завершения процессов не было.'))
     try:
      health=self._http('GET','/health')
      if health.get('status')=='alive':break
     except requests.RequestException:pass
     time.sleep(.5)
    else:raise RuntimeError(tr('Flask did not become ready. See runtime/flask.log.', 'Flask не подтвердил готовность. Смотрите runtime/flask.log.'))
    self._verify_server()
   health=self._http('GET','/health')
   if not health.get('bridge_auth_configured'):raise RuntimeError(tr('Flask requires Bridge authentication.', 'Flask не настроил авторизацию Bridge; запуск остановлен.'))
   from runtime_revision import compute_runtime_source_revision
   if health.get('runtime_source_revision')!=compute_runtime_source_revision():
    raise RuntimeError(tr('Close the previous Flask instance and launch again.', 'Flask использует старую ревизию. Меню не перезапускает прежний процесс. Завершите старую HOI4-EYES штатно и повторите запуск.'))
   rows=processes()
   if any(r['kind']=='unverified_bridge' for r in rows.values()):raise RuntimeError(tr('Multiple Bridge candidates found.', 'Найден неоднозначный Bridge. Второй не запускается.'))
   bridge_pids=[pid for pid,r in rows.items() if r['kind']=='bridge']
   if bridge_pids:
    ready=ready_record(self.root,rows)
    if not ready:raise RuntimeError(tr('Bridge is running but the window is not ready.', 'Bridge найден, но окно не подтвердило готовность. Второй экземпляр не запускается.'))
    from hoi4_bridge import compute_bridge_source_revision
    if ready.get('source_revision')!=compute_bridge_source_revision():
     raise RuntimeError(tr('Close the previous Bridge instance and launch again.', 'Bridge использует старую ревизию. Завершите старую HOI4-EYES штатно; второй Bridge не запускается.'))
   else:
    from start_missing_bridge import spawn_bridge
    child=spawn_bridge(self.root,stack_environment(self.root,self.owner));self._remember('bridge',child)
    for _ in range(90):
     if child.poll() is not None:raise RuntimeError(tr('Bridge exited before readiness. See runtime/bridge.log.', 'Bridge завершился до готовности: runtime/bridge.log.'))
     if ready_record(self.root,processes()):break
     time.sleep(.5)
    else:raise RuntimeError(tr('Bridge window did not become ready. See runtime/bridge.log.', 'Bridge не подтвердил готовность Tk: runtime/bridge.log. Процесс не убивался.'))
   self.desired=True
   if not recovery:
    try:self.maintain_game_reviews()
    except (OSError,ValueError,RuntimeError) as error:
     import logging
     logging.getLogger(__name__).warning('Treaty maintenance deferred: %s',error)
   return note+tr(' Flask and Bridge are ready.', ' Flask и Bridge готовы.')

 def maintain_game_reviews(self):
  from review_retirement import retire
  from treaty_exit_queue import active_unified_mod,install_all_after_exit
  from treaty_package_install import hoI4_running
  from user_storage import DATABASE
  if hoI4_running() is not False:
   return {'installed':0,'blocked':[],'retired':[]}
  mod=active_unified_mod(self.root)
  archived=retire(mod,self.runtime/DATABASE,confirm=True)
  result=install_all_after_exit(self.runtime,mod,
    lambda chat,agreement:self._http('GET',f'/chats/{chat}/agreements/{agreement}'))
  return {**result,'retired':archived}

 def status(self):
  result={'flask':{'ready':False},'bridge':{'ready':False},'ollama':self.ollama_status(),'hoi4':{'running':None},'owned':[]}
  try:
   rows=processes();pids=listener_pids();result['flask']['pids']=pids
   if pids:
    self._verify_server(rows);health=self._http('GET','/health?features=1');result['flask'].update(ready=True,health=health)
    from runtime_revision import compute_runtime_source_revision
    result['flask']['current_revision']=health.get('runtime_source_revision')==compute_runtime_source_revision()
   ready=ready_record(self.root,rows)
   if ready:
    from hoi4_bridge import compute_bridge_source_revision
    result['bridge']={'ready':True,'pid':ready['pid'],'current_revision':ready.get('source_revision')==compute_bridge_source_revision()}
   else:result['bridge']['pids']=[p for p,r in rows.items() if r['kind']=='bridge']
  except Exception as e:result['error']=str(e)
  try:
   from hoi4_bridge import get_hoi4_window_state
   result['hoi4']=get_hoi4_window_state() or {'running':None}
   if (self.runtime/'bridge_ready.json').exists():
    pulse=json.loads((self.runtime/'bridge_ready.json').read_text(encoding='utf8'))
    if result['bridge']['ready']:result['bridge']['game_session']=pulse.get('game_session',{})
  except Exception as e:result['hoi4']['detail']=str(e)
  result['owned']=[kind for kind,v in self.owned.items() if v['process'].poll() is None]
  return result

 def read(self,path):
  self._verify_server()
  from bridge_auth import BRIDGE_TOKEN_HEADER
  return self._http('GET',path,headers={BRIDGE_TOKEN_HEADER:self._token()})

 def bridge_command(self,operation,**values):
  rows=processes();ready=ready_record(self.root,rows)
  if not ready:raise RuntimeError(tr('Bridge did not become ready.', 'Bridge не подтвердил готовность.'))
  from hoi4_bridge import compute_bridge_source_revision
  if ready['source_revision']!=compute_bridge_source_revision():raise RuntimeError(tr('Bridge uses an older revision. Restart it manually.', 'Bridge использует старую ревизию; требуется ручной перезапуск.'))
  secret=self.owner if operation=='shutdown' else self._token()
  request_id=command(self.runtime/'bridge_control.json',ready['pid'],operation,secret,**values)
  for _ in range(60):
   try:
    reply=json.loads((self.runtime/'bridge_control_reply.json').read_text(encoding='utf8'))
    if reply.get('request_id')==request_id:
     if reply.get('error'):raise RuntimeError(reply['error'])
     return reply
   except (FileNotFoundError,ValueError):pass
   time.sleep(.1)
  raise RuntimeError(tr('Bridge did not answer the menu request.', 'Bridge не ответил на запрос меню. Перезапуск автоматически не выполнялся.'))

 def open_chat(self,chat_id,panel='chat'):
  chat=self.read('/chats/'+chat_id)
  return self.bridge_command('open_chat',chat_id=chat_id,campaign_id=chat['campaign_id'],player_country=chat['player_country'],
      participants=chat['participants'],panel=panel)

 def stop_owned(self,include_ollama=False):
  self.desired=False
  # No PID-file adoption, no taskkill, no process trees. Busy components stay up.
  for kind in ('bridge','server'):
   owned=self.owned.get(kind)
   if not owned or owned['process'].poll() is not None:continue
   child=owned['process'];row=processes().get(child.pid)
   if row and row.get('kind')==kind and owned['created'] is None:
    owned['created']=row.get('created') # Still the original retained live OS handle.
   if not row or row.get('kind')!=kind or not owned['created'] or row.get('created')!=owned['created']:
    raise RuntimeError(tr('Process ownership changed. Stop cancelled.', 'Принадлежность процесса изменилась; остановка отменена.'))
   if kind=='bridge':self.bridge_command('shutdown')
   else:
    self._verify_server();self._http('POST','/experimental-control/shutdown',headers={'X-EYES-Owner':self.owner})
   try:child.wait(timeout=8)
   except subprocess.TimeoutExpired:raise RuntimeError(tr('Component is still shutting down.', 'Компонент ещё завершает работу; принудительная остановка не выполнялась.'))
  own=self.owned.get('ollama')
  if include_ollama and own and own['process'].poll() is None:
   if listener_pids():raise RuntimeError(tr('Flask is still running. Ollama remains available.', 'Flask ещё работает; Ollama оставлена доступной.'))
   # The retained OS process handle is the one created here, never a PID lookup.
   own['process'].terminate();own['process'].wait(timeout=8)
  return tr('Stopped the selected components owned by this window.', 'Остановлены только собственные выбранные компоненты. Ранее запущенные службы не изменены.')

 def recover_bridge(self):
  if not self.desired:return None
  rows=processes()
  if any(r.get('kind') in {'bridge','unverified_bridge'} for r in rows.values()):return None
  if self.recoveries<2:
   self.recoveries+=1;return self.start(recovery=True)
  return None
