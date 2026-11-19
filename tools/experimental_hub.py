"""Desktop entry point for local HOI4-EYES services."""
from ui_locale import language, save_language, tr
import argparse,json,logging,os,queue,threading,time,hashlib
from pathlib import Path
from bridge_instance import bridge_instance_lock
from hub_control import atomic_json
from hub_backend import HubBackend,ROOT
from atlas_window import AtlasHotkey, DEFAULT_KEY, open_atlas

def compute_hub_revision():
 return hashlib.sha256(b''.join((ROOT/'tools'/name).read_bytes() for name in (
  'experimental_hub.py','hub_backend.py','hub_control.py','overlay_control.py','atlas_window.py','treaty_exit_queue.py',
  'experimental_stack.py','start_missing_bridge.py','restart_chat_only.py','unified_mod_setup.py'))).hexdigest()[:16]

HUB_SOURCE_REVISION=compute_hub_revision()

HELP=tr('The menu starts local components. Open HOI4 manually and select a country through EYES. The history tabs can be used without a running game. Only services started by this window can be stopped here.', 'Это внешнее главное меню HOI4-EYES.\n\n1. Меню запускает локальные службы. Уже работающие службы используются без перезапуска.\n2. Запустите HOI4 вручную и загрузите сохранение. Меню никогда не запускает игру.\n3. В игре нажмите EYES и выберите страну. После этого можно открыть тот же чат отсюда.\n4. Историю и состояние конференций можно читать здесь без запуска игры.\n\nНажатие «Остановить мои компоненты» относится только к процессам, запущенным этим\nэкземпляром меню. Занятый запрос не обрывается. Чужие и прежние процессы защищены.\nOllama по умолчанию остаётся работающей; для собственного экземпляра есть отдельный флажок.\nЗакрытие меню само по себе не останавливает службы. Чтобы завершить их, сначала нажмите остановку.\n\nКампания определяется по сохранённым данным и игровым наблюдениям. Наличие процесса HOI4\nне доказывает, что выбранная здесь кампания сейчас загружена. Для чата нужен свежий выбор в игре.\nПри неизвестном состоянии главного меню игры используется прежнее безопасное поведение Bridge.\n\nСогласованный текст, подготовленный пакет и подтверждённое применение в HOI4 — разные состояния.\nВ этом меню нет кнопки обхода подтверждений или выполнения произвольной команды.\n')
LANGUAGE_OPTIONS={'English':'en','Русский':'ru','Français':'fr'}
STATE_LABELS={'awaiting_player':tr('Waiting for player', 'Ожидает игрока'),'awaiting_ai':tr('Country reply', 'Ответ страны'),'awaiting_country':tr('Country reply', 'Ответ страны'),'closed':tr('Closed', 'Закрыт'),'generating':tr('Preparing reply', 'Подготовка ответа')}

def transcript(chat):
 return '\n\n'.join(f"{m.get('speaker','?')}: {m.get('text','')}" for m in chat.get('messages',[]) if m.get('text')) or tr('No messages in this chat.', 'В этом чате ещё нет сообщений.')

def campaign_text(chat):
 lines=[f"{tr('Campaign: ', 'Кампания: ')}{chat.get('campaign_id', '—')}",f"{tr('Player: ', 'Игрок: ')}{chat.get('player_country', '—')}",
        tr('Participants: ', 'Участники: ')+', '.join(chat.get('participants',[])),f"{tr('Negotiation status: ', 'Состояние переговоров: ')}{STATE_LABELS.get(chat.get('state'), chat.get('state', '—'))}",
        f"{tr('Agreements in chat: ', 'Договоров в чате: ')}{len(chat.get('agreements', {}))}",'',
        tr('These details come from the selected chat. Verify the loaded campaign in the game.', 'Это сохранённые сведения выбранного чата, а не подтверждение текущей загрузки кампании.')]
 observations=chat.get('game_context_observations',{}).get('by_country',{})
 for country,rows in observations.items():
  if not rows:continue
  last=rows[-1];c=last.get('context',{})
  lines.extend(['',f"{tr('Observation ', 'Наблюдение ')}{country}: {c.get('date') or tr('unknown date','дата неизвестна')}{tr(' · game day ', ' · игровой день ')}{c.get('game_day', '—')}",
     f"{tr('Received: ', 'Получено: ')}{last.get('observed_at', '—')}",f"{tr('Source: ', 'Источник: ')}{last.get('source') or tr('unknown','неизвестен')}"])
 return '\n'.join(lines)

def conference_text(data):
 lines=[]
 for conf in data.get('conferences',[]):
  lines += [f"{tr('Conference: ', 'Конференция: ')}{conf.get('conference_id', '—')}",f"{tr('Goal: ', 'Цель: ')}{conf.get('goal', '—')}",
     f"{tr('Status: ', 'Состояние: ')}{conf.get('status', '—')}{tr(' · outcome: ', ' · итог: ')}{conf.get('outcome') or tr('pending','пока нет')}",
     f"{tr('Game days: ', 'Игровые дни: ')}{conf.get('start_day', '—')} — {conf.get('end_day', '—')}",'']
 return '\n'.join(lines) or tr('No visible conferences in this chat. Open Bridge to manage conferences.', 'Доступных конференций в этом чате пока нет. Создание и важные решения — в существующем окне Bridge.')

class ExperimentalHub:
 def __init__(self,root,backend):
  import tkinter as tk
  from tkinter import ttk
  self.root=root;self.backend=backend;self.tk=tk;self.ttk=ttk;self.queue=queue.Queue();self.busy=False
  self.chats={};self.last_status={};self.closed=False;self.next_recovery=0
  root.title(tr('HOI4-EYES', 'HOI4-EYES — центр дипломатии'));root.geometry('1120x760');root.minsize(940,650)
  root.configure(bg='#111923')
  style=ttk.Style(root);style.theme_use('clam')
  style.configure('.',font=('Segoe UI',10),background='#17232e',foreground='#e2e7ec')
  style.configure('TFrame',background='#111923');style.configure('TLabel',background='#111923',foreground='#d7e0e6')
  style.configure('TButton',padding=(12,7),background='#263947',foreground='#f0ede3')
  style.map('TButton',background=[('active','#395267'),('disabled','#202932')])
  style.configure('Treeview',background='#17232e',fieldbackground='#17232e',foreground='#e2e7ec',rowheight=29)
  style.configure('Treeview.Heading',background='#263947',foreground='#d4b679')
  style.configure('TNotebook.Tab',padding=(16,8));style.map('TNotebook.Tab',background=[('selected','#365164')])
  outer=ttk.Frame(root,padding=20);outer.pack(fill='both',expand=True)
  ttk.Label(outer,text='HOI4–EYES',font=('Segoe UI',20,'bold'),foreground='#d4b679').pack(anchor='w')
  ttk.Label(outer,text=tr('Diplomacy · history · services', 'Дипломатия · история · управление компонентами'),font=('Segoe UI',10)).pack(anchor='w',pady=(2,16))
  status_row=ttk.Frame(outer);status_row.pack(fill='x');self.indicators={};self.status_labels={}
  for name,label in [('flask','Flask · 5016'),('bridge',tr('Bridge · chat window', 'Bridge · окно чата')),('ollama',tr('Ollama · local Qwen', 'Ollama · локальная Qwen')),('hoi4',tr('HOI4 connection', 'Связь с HOI4'))]:
   card=ttk.Frame(status_row,padding=9);card.pack(side='left',fill='both',expand=True)
   ttk.Label(card,text=label,font=('Segoe UI',10,'bold')).pack(anchor='w')
   value=tk.StringVar(value=tr('Checking…', 'Проверяем…'));self.indicators[name]=value
   label_widget=ttk.Label(card,textvariable=value,wraplength=240);label_widget.pack(anchor='w',pady=(6,0));self.status_labels[name]=label_widget
  buttons=ttk.Frame(outer);buttons.pack(fill='x',pady=15)
  self.start_button=ttk.Button(buttons,text=tr('Start components', '▶ Запустить компоненты'),command=lambda:self.submit('start',backend.start))
  self.start_button.pack(side='left')
  self.stop_button=ttk.Button(buttons,text=tr('Stop my components', 'Остановить мои компоненты'),command=self.stop);self.stop_button.pack(side='left',padx=8)
  ttk.Button(buttons,text=tr('Refresh', 'Обновить'),command=self.refresh).pack(side='left')
  self.atlas_hotkey=AtlasHotkey()
  ttk.Button(buttons,text=tr('Recognition Atlas', 'Атлас признания'),command=self.show_atlas).pack(side='left')
  ttk.Button(buttons,text=tr('Country Maker', 'Создать государство'),command=lambda:self.show_atlas(True)).pack(side='left')
  self.stop_ollama=tk.BooleanVar(value=False)
  self.feature_status=tk.StringVar()
  ttk.Label(outer,textvariable=self.feature_status).pack(anchor='w')
  ttk.Checkbutton(outer,text=tr('Also stop Ollama if started by this window', 'При остановке также завершить Ollama, если её запустило это меню'),variable=self.stop_ollama).pack(anchor='w')
  self.message=tk.StringVar(value=tr('The services start automatically. Open HOI4 manually when ready.', 'Службы запускаются автоматически. HOI4 откройте вручную, когда будете готовы.'))
  ttk.Label(outer,textvariable=self.message,wraplength=1040,foreground='#d4b679').pack(fill='x',pady=(10,12))
  notebook=ttk.Notebook(outer);notebook.pack(fill='both',expand=True)
  page=ttk.Frame(notebook,padding=10);notebook.add(page,text=tr('Negotiations and history', 'Переговоры и история'))
  row=ttk.Frame(page);row.pack(fill='x')
  ttk.Label(row,text=tr('Campaign:', 'Кампания:')).pack(side='left')
  self.filter=tk.StringVar(value=tr('All campaigns', 'Все кампании'));self.campaign=ttk.Combobox(row,textvariable=self.filter,state='readonly',values=[tr('All campaigns', 'Все кампании')],width=36)
  self.campaign.pack(side='left',padx=8);self.campaign.bind('<<ComboboxSelected>>',lambda e:self.render_chats())
  ttk.Button(row,text=tr('Load history', 'Загрузить историю'),command=self.load_chats).pack(side='right')
  split=ttk.Panedwindow(page,orient='horizontal');split.pack(fill='both',expand=True,pady=8)
  left=ttk.Frame(split);right=ttk.Frame(split);split.add(left,weight=2);split.add(right,weight=3)
  list_frame=ttk.Frame(left);list_frame.pack(fill='both',expand=True,pady=(0,8))
  self.tree=ttk.Treeview(list_frame,columns=('player','partners','state','count'),show='headings',height=5,selectmode='browse')
  for name,title,width in [('player',tr('Player', 'Игрок'),55),('partners',tr('Participants', 'Участники'),100),('state',tr('Negotiations', 'Переговоры'),135),('count',tr('Messages', 'Реплик'),60)]:
   self.tree.heading(name,text=title);self.tree.column(name,width=width)
  self.tree.pack(side='left',fill='both',expand=True);self.tree.bind('<<TreeviewSelect>>',self.select_chat)
  scroll=ttk.Scrollbar(list_frame,orient='vertical',command=self.tree.yview);scroll.pack(side='right',fill='y');self.tree.configure(yscrollcommand=scroll.set)
  self.open_button=ttk.Button(left,text=tr('Open selected chat in Bridge', 'Открыть выбранный чат в Bridge'),command=self.open_chat);self.open_button.pack(fill='x',pady=(0,6))
  self.history=self.make_text(right);self.history.pack(fill='both',expand=True)
  conf=ttk.Frame(notebook,padding=10);notebook.add(conf,text=tr('Conferences', 'Конференции'))
  ttk.Label(conf,text=tr('Review conferences for the selected chat. Open Bridge to manage them.', 'Конференции выбранного чата. Полномочия и важные решения остаются в существующем Bridge.')).pack(anchor='w')
  ttk.Button(conf,text=tr('Show conferences', 'Показать конференции'),command=self.load_conferences).pack(anchor='w',pady=8)
  ttk.Button(conf,text=tr('Open conference controls in Bridge', 'Открыть управление конференцией в Bridge'),command=lambda:self.open_chat('conference')).pack(anchor='w',pady=(0,8))
  self.conferences=self.make_text(conf);self.conferences.pack(fill='both',expand=True)
  campaign=ttk.Frame(notebook,padding=10);notebook.add(campaign,text=tr('Campaign', 'Кампания'))
  self.campaign_details=self.make_text(campaign);self.campaign_details.pack(fill='both',expand=True)
  fleet=ttk.Frame(notebook,padding=10);notebook.add(fleet,text=tr('Fleet'))
  ttk.Label(fleet,text=tr('Review naval observations and prepare orders for the in-game fleet.')).pack(anchor='w',pady=(0,8))
  ttk.Button(fleet,text=tr('Open fleet dashboard'),command=lambda:self.show_atlas(fleet=True)).pack(anchor='w')
  meetings=ttk.Frame(notebook,padding=10);notebook.add(meetings,text=tr('Government meetings'))
  ttk.Button(meetings,text=tr('Open government meetings'),command=lambda:self.show_atlas(meetings=True)).pack(anchor='w')
  setup=ttk.Frame(notebook,padding=10);notebook.add(setup,text=tr('Mod setup'))
  for action,caption in (('check','Check registration'),('install','Register or repair mod'),('remove','Remove registration')):
   ttk.Button(setup,text=tr(caption),command=lambda value=action:self.mod_setup(value)).pack(anchor='w',pady=4)
  settings=ttk.Frame(notebook,padding=10);notebook.add(settings,text=tr('Interface settings','Настройки интерфейса'))
  ttk.Label(settings,text=tr('Atlas shortcut · Ctrl+Alt+key','Открытие Atlas · Ctrl+Alt+клавиша')).pack(anchor='w')
  self.atlas_key=ttk.Combobox(settings,state='readonly',values=('F8','F9','F10','F11','Off'),width=12)
  self.atlas_key.pack(anchor='w',pady=6)
  try:key=json.loads((backend.runtime/'atlas-ui.json').read_text(encoding='utf8'))['key']
  except (OSError,ValueError,KeyError,TypeError):key=DEFAULT_KEY
  self.atlas_key.set(key)
  self.atlas_key.bind('<<ComboboxSelected>>',self.change_atlas_key)
  self.change_atlas_key()
  root.after(100,self.poll_atlas_key)
  ttk.Label(settings,text=tr('Interface language','Язык интерфейса')).pack(anchor='w',pady=(12,0))
  self.language_choice=ttk.Combobox(settings,state='readonly',values=tuple(LANGUAGE_OPTIONS),width=14)
  self.language_choice.pack(anchor='w',pady=6)
  self.language_choice.set(next(label for label,code in LANGUAGE_OPTIONS.items() if code==language()))
  self.language_notice=tk.StringVar()
  ttk.Label(settings,textvariable=self.language_notice,wraplength=820).pack(anchor='w')
  self.language_choice.bind('<<ComboboxSelected>>',self.change_language)
  help_page=ttk.Frame(notebook,padding=10);notebook.add(help_page,text=tr('Help', 'Помощь'))
  info=self.make_text(help_page);info.pack(fill='both',expand=True);self.set_text(info,HELP)
  root.protocol('WM_DELETE_WINDOW',self.close)
  root.after(100,self.drain);root.after(150,self.refresh);root.after(500,self.focus_requests);root.after(5000,self.periodic)

 def make_text(self,parent):
  frame=self.ttk.Frame(parent);frame.pack(fill='both',expand=True)
  text=self.tk.Text(frame,wrap='word',state='disabled',bg='#17232e',fg='#e2e7ec',font=('Segoe UI',10),relief='flat',padx=12,pady=10)
  scroll=self.ttk.Scrollbar(frame,orient='vertical',command=text.yview);scroll.pack(side='right',fill='y')
  text.configure(yscrollcommand=scroll.set);text.pack(side='left',fill='both',expand=True)
  return text

 def change_language(self,_event=None):
  code=LANGUAGE_OPTIONS.get(self.language_choice.get())
  if code is None or code==language():return
  saved=tr('Language saved. Reopen the EYES windows to apply it.','Язык сохранён. Откройте окна EYES заново.')
  try:
   save_language(code)
  except OSError:
   self.language_choice.set(next(label for label,value in LANGUAGE_OPTIONS.items() if value==language()))
   self.language_notice.set(tr('Could not save the language setting.','Не удалось сохранить язык интерфейса.'))
  else:self.language_notice.set(saved)

 def change_atlas_key(self,_event=None):
  try:
   self.atlas_hotkey.set(self.atlas_key.get())
   if _event is not None:atomic_json(self.backend.runtime/'atlas-ui.json',{'key':self.atlas_hotkey.key})
  except (OSError,ValueError,queue.Empty) as error:
   self.atlas_key.set(self.atlas_hotkey.key or 'Off')
   self.message.set(str(error))

 def show_atlas(self,maker=False,*,fleet=False,meetings=False):
  try:open_atlas(self.backend.runtime,maker,fleet=fleet,meetings=meetings)
  except OSError as error:self.message.set(str(error))

 def mod_setup(self,action):
  from tkinter import filedialog,messagebox
  from unified_mod_setup import change_setup,documents_directory
  from validate_hoi4_gui import discover_hoi4_install
  game=discover_hoi4_install()
  if game is None:
   chosen=filedialog.askdirectory(title=tr('Choose the HOI4 game folder'),parent=self.root)
   if not chosen:return
   game=Path(chosen)
  chosen=filedialog.askdirectory(title=tr('Choose the HOI4 user data folder'),initialdir=documents_directory(),parent=self.root)
  if not chosen:return
  if action!='check' and not messagebox.askyesno(tr('Mod setup'),tr('Change this launcher registration?')+'\n'+chosen,parent=self.root):
   return
  def apply():
   report=change_setup(action,game,Path(chosen),root=self.backend.root,confirm=action!='check')
   return tr(report['integrity'])+' · '+report['version']+' · '+tr(report['playset'])
  self.submit('setup',apply)

 def poll_atlas_key(self):
  if self.closed:return
  if self.atlas_hotkey.pressed():self.show_atlas()
  self.root.after(100,self.poll_atlas_key)
 def set_text(self,widget,text):
  widget.configure(state='normal');widget.delete('1.0','end');widget.insert('end',text);widget.configure(state='disabled')
 def submit(self,name,callback):
  if self.busy:return False
  self.busy=True;self.message.set({'start':tr('Starting components…', 'Запускаем компоненты и проверяем готовность…'),'stop':tr('Waiting for owned components to stop…', 'Проверяем принадлежность и ждём безопасного завершения…')}.get(name,tr('Loading…', 'Загружаем…')))
  self.start_button.configure(state='disabled');self.stop_button.configure(state='disabled')
  def run():
   try:self.queue.put((name,callback(),None))
   except Exception as error:
    logging.exception('Hub operation failed: %s',name);self.queue.put((name,None,str(error)))
  threading.Thread(target=run,daemon=True).start();return True
 def selected(self):
  chosen=self.tree.selection();return self.chats.get(chosen[0]) if chosen else None
 def stop(self):
  from tkinter import messagebox
  if self.busy:return
  if not any(v['process'].poll() is None for v in self.backend.owned.values()):
   self.message.set(tr('This window did not start the running components.', 'Это меню не запускало работающие компоненты. Ранее запущенные процессы защищены.'));return
  include=self.stop_ollama.get()
  if messagebox.askyesno(tr('Stop components', 'Остановка HOI4-EYES'),tr('Stop the components started by this window?', 'Остановить только компоненты, запущенные этим окном?\nРанее работающие службы не будут завершены.')+
      (tr('\nThis includes the Ollama instance started here.', '\nТакже будет остановлена собственная Ollama. Другие приложения могут использовать её локальный порт.') if include else ''),parent=self.root):
   self.submit('stop',lambda:self.backend.stop_owned(include))
 def refresh(self):
  if self.closed:return
  if not self.busy:self.submit('status',self.backend.status)
 def load_chats(self):self.submit('chats',lambda:self.backend.read('/chats'))
 def render_chats(self):
  for item in self.tree.get_children():self.tree.delete(item)
  for chat in self.chats.values():
   if self.filter.get() not in {tr('All campaigns', 'Все кампании'),chat['campaign_id']}:continue
   self.tree.insert('', 'end',iid=chat['chat_id'],values=(chat['player_country'],', '.join(chat['participants']),STATE_LABELS.get(chat.get('state'),chat.get('state')),len(chat.get('messages',[]))))
 def select_chat(self,_event=None):
  chat=self.selected()
  if chat:
   self.set_text(self.history,transcript(chat));self.set_text(self.campaign_details,campaign_text(chat))
   self.set_text(self.conferences,tr('Click Show conferences to view the selected chat.', 'Нажмите «Показать конференции», чтобы получить разрешённый обзор выбранного чата.'))
 def open_chat(self,panel='chat'):
  chat=self.selected()
  if not chat:self.message.set(tr('Select a chat from history or choose a country in HOI4.', 'Сначала выберите переговоры из истории. Новую страну выберите через EYES в игре.'));return
  self.submit('open',lambda:self.backend.open_chat(chat['chat_id'],panel=panel))
 def load_conferences(self):
  chat=self.selected()
  if not chat:self.message.set(tr('Select a chat in the history tab.', 'Сначала выберите чат на вкладке истории.'));return
  self.submit('conferences',lambda:self.backend.read('/chats/'+chat['chat_id']+'/conferences'))
 def render_status(self,data):
  self.last_status=data
  features=data['flask'].get('health',{}).get('features',{})
  self.feature_status.set(' · '.join(f"{tr(caption)} {('✓' if features.get(key) else '—')}" for key,caption in
    (('atlas','Recognition Atlas'),('diplomacy','Diplomacy'),('cabinet','Government meetings'),('fleet','Fleet'))))
  for key in ('flask','bridge','ollama'):
   value=data[key];line=tr('Ready', 'Готов') if value.get('ready') else tr('Not ready', 'Не готов')
   pids=value.get('pids') or ([value['pid']] if value.get('pid') else [])
   if pids:line+=' · PID '+', '.join(map(str,pids))
   if value.get('current_revision') is False:line+=tr(' · older revision', ' · старая ревизия')
   self.indicators[key].set(line)
   self.status_labels[key].configure(foreground='#8dc9a3' if value.get('ready') and value.get('current_revision') is not False else '#d5ad72')
  game=data.get('hoi4',{});session=data['bridge'].get('game_session',{});selection=session.get('selection') or {}
  line=tr('Game running', 'Игра запущена') if game.get('running') is True else tr('Game closed', 'Игра закрыта') if game.get('running') is False else tr('Game status unknown', 'Состояние неизвестно')
  if selection:line+=tr('\nLast selection: ', '\nПоследний выбор: ')+str(selection.get('player_country'))+' → '+str(selection.get('selected_country'))
  else:line+=tr('\nNo recent country selection', '\nСвежего выбора страны нет')
  self.indicators['hoi4'].set(line)
  self.message.set(data.get('error') or (tr('Owned components: ', 'Собственные компоненты: ')+', '.join(data['owned']) if data['owned'] else tr('Connected to existing services.', 'Подключение к существующим службам. Остановка прежних процессов недоступна.')))
  if not data.get('error') and not data['ollama']['ready']:self.message.set(data['ollama']['detail']+tr(' A fallback reply is available.', ' Возможен резервный ответ.'))
 def periodic(self):
  if self.closed:return
  from treaty_exit_queue import queued_paths
  if not self.busy and queued_paths(self.backend.runtime):
   from treaty_package_install import hoI4_running
   if hoI4_running() is False:
    self.submit('treaty_exit',self.install_queued_treaty)
   else:self.refresh()
  else:self.refresh()
  self.root.after(5000,self.periodic)

 def install_queued_treaty(self):
  result=self.backend.maintain_game_reviews()
  if result['blocked']:
   return tr('Treaties installed: ', 'Установлено договоров: ')+str(result['installed'])+' · '+result['blocked'][0]['reason']
  if result['installed']:
   return tr('Treaties installed after HOI4 exit. Load the matching save.',
             'Договоры установлены после выхода из HOI4. Загрузи соответствующее сохранение.')
 def drain(self):
  try:
   while True:
    name,data,error=self.queue.get_nowait();self.busy=False
    self.start_button.configure(state='normal');self.stop_button.configure(state='normal' if any(v['process'].poll() is None for v in self.backend.owned.values()) else 'disabled')
    if error:self.message.set(error)
    elif name=='status':
     self.render_status(data)
     if self.backend.desired and time.monotonic()>self.next_recovery:
      self.next_recovery=time.monotonic()+10
      if not data['bridge']['ready'] and data['flask']['ready'] and self.backend.recoveries<2:
       self.root.after(10,lambda:self.submit('recovery',self.backend.recover_bridge))
    elif name=='chats':
     self.chats={c['chat_id']:c for c in data['chats']};self.campaign.configure(values=[tr('All campaigns', 'Все кампании'),*sorted({c['campaign_id'] for c in self.chats.values()})]);self.render_chats()
     self.message.set(f"{tr('Negotiations loaded: ', 'Загружено переговоров: ')}{len(self.chats)}{tr('.', '. История открывается без отправки сообщений.')}")
    elif name=='conferences':self.set_text(self.conferences,conference_text(data));self.message.set(tr('Conference overview loaded.', 'Показан разрешённый обзор конференций.'))
    elif name=='open':self.message.set(tr('Bridge accepted the request and will open the window.', 'Запрос принят Bridge; окно открывается асинхронно.'))
    else:
     self.message.set(str(data or tr('Done', 'Готово')))
     if name in {'start','recovery'}:self.root.after(100,self.load_chats)
  except queue.Empty:pass
  if not self.closed:self.root.after(100,self.drain)
 def focus_requests(self):
  try:
   data=json.loads((self.backend.runtime/'hub_focus.json').read_text(encoding='utf8'))
   if 0<=time.time()-data['issued']<3:
    self.root.deiconify();self.root.lift()
    if data.get('expected_revision',HUB_SOURCE_REVISION)!=HUB_SOURCE_REVISION:
     self.message.set(tr('Reopen the menu to load the updated code.', 'Это меню работает на старой ревизии. Закройте только меню и запустите BAT снова; службы автоматически не завершаются.'))
  except (OSError,ValueError,KeyError):pass
  if not self.closed:self.root.after(1000,self.focus_requests)
 def close(self):
  from tkinter import messagebox
  if self.busy:
   messagebox.showinfo(tr('Operation in progress', 'Дождитесь операции'),tr('Wait for the current operation to finish.', 'Сначала дождитесь окончания операции меню. Службы не останавливаются.'),parent=self.root);return
  if any(v['process'].poll() is None for v in self.backend.owned.values()):
   if not messagebox.askyesno(tr('Close menu?', 'Закрыть только меню?'),tr('Close the menu and leave components running?', 'Компоненты останутся работать. Для их остановки сначала используйте «Остановить мои компоненты». Закрыть только меню?'),parent=self.root):return
  self.closed=True;self.atlas_hotkey.close();self.root.destroy()

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--root',default=str(ROOT))
 parser.add_argument('--manual',action='store_true');args=parser.parse_args()
 backend=HubBackend(args.root);backend.runtime.mkdir(parents=True,exist_ok=True)
 logging.basicConfig(filename=backend.runtime/'hub.log',encoding='utf8',level=logging.INFO)
 with bridge_instance_lock(backend.runtime/'hub_instance.lock') as acquired:
  if not acquired:
   atomic_json(backend.runtime/'hub_focus.json',{'issued':time.time(),'expected_revision':HUB_SOURCE_REVISION});return 0
  atomic_json(backend.runtime/'hub_status.json',{'pid':os.getpid(),'source_revision':HUB_SOURCE_REVISION})
  import tkinter as tk
  root=tk.Tk();hub=ExperimentalHub(root,backend)
  if not args.manual:root.after(50,lambda:hub.submit('start',backend.start))
  root.mainloop()
 return 0

if __name__=='__main__':
 try:raise SystemExit(main())
 except Exception as error:
  logging.exception('HOI4-EYES hub failed')
  if os.name=='nt':
   import ctypes
   ctypes.windll.user32.MessageBoxW(None,tr('Unable to open the HOI4-EYES menu.\n', 'Не удалось открыть меню HOI4-EYES.\n')+str(error)+tr('\nSee runtime/hub.log.', '\nСмотрите runtime/hub.log.'),'HOI4-EYES',16)
  raise
