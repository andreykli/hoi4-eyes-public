"""Windows WebView2 client for the same local HOI4-EYES services."""
import argparse
from html import escape
import json
import logging
import os
from pathlib import Path
import re
import threading
import time

from bridge_instance import bridge_instance_lock
from hub_backend import HubBackend
from hub_control import atomic_json
from ui_locale import tr
from user_storage import APP_ROOT

URL = 'http://127.0.0.1:5016/desktop'
TABS = ('diplomacy','atlas','recognition','meetings','fleet','maker')
DESKTOP_EVENTS = {'shell_loaded', 'workspace_loaded', 'workspace_changed', 'chat_redrawn',
                  'status_changed', 'workspace_error', 'request_error', 'script_error'}


class Controls:
    def __init__(self,backend):
        self._backend=backend
        self._lock=threading.Lock()
        self._hotkey=None

    def atlas_shortcut(self, key=None):
        if self._hotkey is None:
            return {'error':tr('Not ready')}
        if key is not None:
            try:
                self._hotkey.set(key)
                atomic_json(self._backend.runtime/'atlas-ui.json',{'key':key})
            except (OSError,ValueError) as error:
                logging.warning('atlas_shortcut registration_failed key=%s error=%s',key,error)
                return {'key':self._hotkey.key,'error':str(error)}
        return {'key':self._hotkey.key}

    def status(self):
        result=self._backend.status()
        if result.get('error'):
            return {'error':result['error']}
        return {name:bool(result[key].get('ready')) for name,key in
                (('Flask','flask'),('Bridge','bridge'),('Ollama','ollama'))}

    def trace(self, event, page, details=None):
        if event in DESKTOP_EVENTS and page in TABS:
            logging.info('desktop_render event=%s page=%s', event, page)
            if event in {'script_error','workspace_error'} and isinstance(details,dict):
                # No chat text, URL queries, stack dumps or model output in diagnostics.
                safe={key:details[key] for key in ('kind','source','line','column')
                      if key in details and isinstance(details[key],(str,int))}
                logging.warning('desktop_script %s',json.dumps(safe,ensure_ascii=True)[:600])

    def open_chat(self, identifier):
        if not isinstance(identifier,str) or not re.fullmatch(r'[a-f0-9]{32}',identifier):
            return {'error':tr('No diplomatic chats yet. Choose a country in HOI4.')}
        try:
            self._backend.open_chat(identifier)
            return {'opened':True}
        except (OSError,RuntimeError,ValueError) as error:
            return {'error':str(error)}

    def stop(self):
        with self._lock:
            return self._stop()

    def _stop(self):
        try:
            return {'detail':self._backend.stop_owned()}
        except (OSError,RuntimeError,ValueError) as error:
            return {'error':str(error)}


def window_size(runtime):
    try:
        value=json.loads((runtime/'desktop-window.json').read_text(encoding='utf8'))
        return min(2500,max(760,int(value['width']))),min(1600,max(480,int(value['height'])))
    except (OSError,ValueError,KeyError,TypeError):
        return 1170,780


def controls_for_backend(backend):
    from developer_access import enabled
    if enabled(backend.root):
        from developer_desktop import DeveloperControls
        return DeveloperControls(backend)
    return Controls(backend)


def main():
    import webview
    webview.settings['ALLOW_DOWNLOADS'] = True
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--page',choices=TABS,default='diplomacy')
    parser.add_argument('--root',type=Path,default=APP_ROOT)
    args=parser.parse_args()
    # Variant-C builds opt in at export. Run BEFORE HubBackend or SQLite opens.
    if (args.root/'release/installation_protocol.json').is_file():
        from workshop_update import startup_check
        from unified_mod_setup import documents_directory
        from ui_locale import language
        import tkinter as tk
        from tkinter import messagebox
        update_text=json.loads((Path(__file__).with_name('installer_text.json')).read_text(encoding='utf-8'))
        selected_language=language()
        parent=tk.Tk();parent.withdraw()
        try:
            try:
                update=startup_check(args.root,documents_directory())
            except (OSError,ValueError,KeyError,TypeError):
                update={'blocked':True,'status':'compatibility_check_failed','updates':[]}
            if update['blocked']:
                logging.warning('Installer compatibility gate: %s',update['status'])
                messagebox.showerror('HOI4-EYES',update_text['startup_blocked'][selected_language],parent=parent)
                return
            for row in update['updates']:
                if row['newer'] and messagebox.askyesno('HOI4-EYES',
                        update_text['startup_update'][selected_language]+'\n'+row['version'],parent=parent):
                    os.startfile(row['folder'])
        finally:
            parent.destroy()
    backend=HubBackend(args.root)
    runtime=backend.runtime
    runtime.mkdir(parents=True,exist_ok=True)
    from logging.handlers import RotatingFileHandler
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                        handlers=[RotatingFileHandler(runtime/'desktop.log', maxBytes=512*1024,
                                                      backupCount=2, encoding='utf8')])
    with bridge_instance_lock(runtime/'hub_instance.lock') as acquired:
        if not acquired:
            atomic_json(runtime/'hub_focus.json',{'issued':time.time(),'page':args.page})
            return
        controls=controls_for_backend(backend)
        width,height=window_size(runtime)
        done=threading.Event()
        size={'width':width,'height':height}
        window=webview.create_window('HOI4-EYES',html=f'<body style="background:#13212b;color:#eee;padding:30px">{escape(tr("Starting components…"))}</body>',
                                     js_api=controls,width=width,height=height,min_size=(760,480))
        def resize(w,h):
            if w>=760 and h>=480:size.update(width=w,height=h)

        window.events.resized += resize
        window.events.closed += lambda:done.set()
        window.events.loaded += lambda:logging.info('desktop top-level document loaded')
        logging.info('desktop window created')

        def focus(page):
            if page in TABS:
                window.run_js('window.eyesShowWorkspace?.('+json.dumps(page)+')')
            window.restore()
            window.show()

        def operate():
            from atlas_window import AtlasHotkey, DEFAULT_KEY
            hotkey=AtlasHotkey()
            controls._hotkey=hotkey
            try:
                with controls._lock:
                    backend.start()
                from ollama_warmup import schedule
                schedule()
                if done.is_set():
                    return
                window.load_url(URL+'?page='+args.page)
                try:
                    key=json.loads((runtime/'atlas-ui.json').read_text(encoding='utf8'))['key']
                except (OSError,ValueError,KeyError):
                    key=DEFAULT_KEY
                try:
                    hotkey.set(key)
                except (OSError,ValueError) as error:
                    logging.warning('Atlas shortcut unavailable key=%s error=%s; retrying while unavailable',key,error)
            except Exception as error:
                logging.exception('Desktop service startup failed')
                if not done.is_set():
                    window.load_html('<body style="background:#13212b;color:#eee;padding:30px">'
                                     f'<h1>HOI4-EYES</h1><p>{escape(str(error))}</p>'
                                     '<p>start.bat --manual · runtime/desktop.log</p></body>')
                hotkey.close()
                return
            previous=0
            was_running=False
            next_process_check=0
            while not done.wait(.25):
                if hotkey.pressed():
                    focus('atlas')
                try:
                    request=json.loads((runtime/'hub_focus.json').read_text(encoding='utf8'))
                    if request['issued']!=previous and 0<=time.time()-request['issued']<4:
                        previous=request['issued']
                        focus(request.get('page'))
                except (OSError,ValueError,KeyError,TypeError):
                    pass
                if time.monotonic() < next_process_check:
                    continue
                next_process_check=time.monotonic()+5
                if hotkey.key is None:
                    try:
                        hotkey.set(key)
                        logging.info('atlas_shortcut recovered key=%s',key)
                    except (OSError,ValueError) as error:
                        logging.debug('atlas_shortcut still unavailable: %s',error)
                from treaty_package_install import hoI4_running
                from treaty_exit_queue import queued_paths
                running=hoI4_running()
                if running is False and (was_running or queued_paths(runtime)):
                    try:
                        backend.maintain_game_reviews()
                    except (OSError,RuntimeError,ValueError):
                        logging.exception('Deferred treaty maintenance')
                if running is not None:was_running=running
            hotkey.close()

        try:
            webview.start(operate,gui='edgechromium',debug=False,private_mode=True)
        finally:
            done.set()
            atomic_json(runtime/'desktop-window.json',size)
            # Only owned handles can be stopped. Let interactive work finish if busy.
            with controls._lock:
                result=controls._stop()
                if result.get('error'):logging.warning('Desktop services left running: %s',result['error'])


if __name__=='__main__':
    main()
