"""Standard-library setup GUI, launched only after the native bootstrap consent."""
import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk,messagebox,filedialog
from install_manager import (load_plan,stage,activate,active,remove,run_visible,digest,
                             download,ollama_status,model_state,pull_model,owned_root,atomic,installation_lock,launcher_bytes)
from installer_lifecycle import (cache_setup,installed_apps,discover_previous,UpdateTransaction,
    registration_controls,adopt_registration,select_single_mod,import_legacy_profile,recover_pending)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--bundle',type=Path,required=True)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--language',choices=('en','ru','fr'),default='en')
    parser.add_argument('--uninstall',action='store_true')
    args=parser.parse_args()
    plan,_=load_plan(args.bundle)
    texts=json.loads((Path(__file__).with_name('installer_text.json')).read_text(encoding='utf-8'))
    def tr(key):return texts[key][args.language]
    window=tk.Tk();window.title('HOI4-EYES — '+tr('setup'));window.geometry('820x690');window.minsize(680,580)
    frame=ttk.Frame(window,padding=18);frame.pack(fill='both',expand=True)
    ttk.Label(frame,text=tr('intro'),wraplength=760).pack(anchor='w',pady=8)
    ttk.Label(frame,text=str(args.root),wraplength=760).pack(anchor='w')
    ttk.Label(frame,text=tr('preserve'),wraplength=760).pack(anchor='w',pady=8)
    consent=tk.BooleanVar(value=False);ollama=tk.BooleanVar(value=False);webview=tk.BooleanVar(value=False)
    model=tk.BooleanVar(value=False);register=tk.BooleanVar(value=False)
    option_labels=[]
    for variable,key in ((consent,'app'),(ollama,'ollama'),(webview,'webview'),(model,'model'),(register,'register')):
        option=ttk.Frame(frame);option.pack(fill='x',pady=3)
        ttk.Checkbutton(option,variable=variable).pack(side='left')
        label=ttk.Label(option,text=tr(key),wraplength=720);label.pack(side='left',fill='x',expand=True)
        label.bind('<Button-1>',lambda event,var=variable:var.set(not var.get()))
        option_labels.append(label)
    window.bind('<Configure>',lambda event:[label.configure(wraplength=max(450,event.width-90)) for label in option_labels]
                if event.widget is window else None)
    game=tk.StringVar();row=ttk.Frame(frame);row.pack(fill='x',pady=8)
    ttk.Label(row,text=tr('game')).pack(side='left');ttk.Entry(row,textvariable=game).pack(side='left',fill='x',expand=True)
    def browse():
        selected=filedialog.askdirectory(parent=window)
        if selected:game.set(selected)
    ttk.Button(row,text=tr('browse'),command=browse).pack(side='right')
    previous=tk.StringVar();legacy=tk.BooleanVar(value=False)
    row=ttk.Frame(frame);row.pack(fill='x',pady=4)
    ttk.Label(row,text=tr('previous_folder')).pack(side='left')
    ttk.Entry(row,textvariable=previous).pack(side='left',fill='x',expand=True)
    def browse_previous():
        selected=filedialog.askdirectory(parent=window)
        if selected:previous.set(selected)
    ttk.Button(row,text=tr('browse'),command=browse_previous).pack(side='right')
    ttk.Checkbutton(frame,text=tr('legacy_import'),variable=legacy).pack(anchor='w')
    out=tk.Text(frame,height=8,wrap='word',state='disabled');out.pack(fill='both',expand=True,pady=8)
    progress=ttk.Progressbar(frame,maximum=100);progress.pack(fill='x',pady=4)
    events=queue.Queue();busy=False;installed=None
    try:installed=active(args.root)
    except (OSError,ValueError,KeyError):pass
    from unified_mod_setup import documents_directory
    try:
        previous_installations=discover_previous(documents_directory(),managed_root=args.root)['installations']
    except (OSError,ValueError,TypeError,KeyError):
        previous_installations=[]
    verified_previous=[row for row in previous_installations if row.get('verified')]
    if len(verified_previous)==1:previous.set(verified_previous[0]['root'])
    if previous_installations:events.put(('text',tr('previous_found')+str(len(previous_installations))))
    def emit(key):events.put(('text',tr(key)))
    def status():
        try:
            rows=ollama_status();state=model_state(rows,plan['model'])
            emit('model_ready' if state=='ready' else 'model_changed' if state=='different_digest' else 'model_missing')
        except Exception:emit('ollama_missing')
    def run_python(python,arguments,cwd):run_visible([python,*arguments],consent=True,cwd=cwd)
    idle_check=('from treaty_package_install import hoI4_running; '
                'from install_manager import require_idle,local_app_pids,require_launcher_closed; '
                'require_idle(hoI4_running(),local_app_pids()); require_launcher_closed()')
    def worker(action,chosen,game_path):
        nonlocal installed
        lock=None;transaction=None
        try:
            cache=args.root/'downloads'
            if action=='check':status();events.put(('done',None));return
            lock=installation_lock(args.root,consent=chosen['app']);lock.__enter__()
            if action=='remove':
                target=active(args.root);python=target/'.venv/Scripts/python.exe'
                run_python(python,['-c',idle_check],target/'tools')
                state=json.loads((args.root/'installation.json').read_text(encoding='utf-8'))
                # Existing registration removal is ownership-checked and backed up.
                if chosen['register']:
                    command=[str(target/'tools/unified_mod_setup.py'),'remove','--confirm']
                    if game_path:command+=['--game',game_path]
                    run_python(python,command,target)
                remove(args.root,consent=True,game_closed=True,
                       preserve_prefixes=() if chosen['register'] else ('prepared/unified_game_mod/',))
                if state.get('setup_cache'):
                    installed_apps(args.root,args.root/state['setup_cache'],plan['version'],consent=True,remove=True)
                installed=None;emit('removed');events.put(('done',None));return
            emit('validating');target=stage(args.bundle,args.root,consent=chosen['app'])
            emit('python_env');python=target/'.venv/Scripts/python.exe'
            base=Path(sys.executable).with_name('python.exe')
            if not python.exists():run_python(base,['-m','venv',str(target/'.venv')],target)
            emit('dependencies')
            run_python(python,['-m','pip','install','--no-index','--find-links',str(args.bundle/'_eyes_setup/wheels'),
                '--require-hashes','-r',str(args.bundle/'_eyes_setup/requirements.lock')],target)
            for enabled,key in ((chosen['webview'],'webview2'),(chosen['ollama'],'ollama')):
                if enabled:
                    if key=='ollama':
                        try:
                            state=model_state(ollama_status(),plan['model'])
                            emit({'ready':'model_ready','different_digest':'model_changed','missing':'model_missing'}[state]);continue
                        except Exception:pass
                        if (Path(os.environ.get('LOCALAPPDATA',''))/'Programs/Ollama/ollama.exe').is_file():continue
                    if key=='webview2':
                        try:
                            run_python(python,['-c','from webview.platforms.winforms import _is_chromium; import sys; sys.exit(0 if _is_chromium() else 4)'],target)
                            continue
                        except RuntimeError:pass
                    emit('downloading_'+key)
                    executable=download(plan['sources'][key],cache,consent=True,
                        progress=lambda n,total:events.put(('progress',100*n/total)))
                    # Official vendor GUI remains visible. No /quiet or elevation.
                    run_visible([executable],consent=True)
            if chosen['model']:
                try:ollama_status()
                except Exception:
                    executable=Path(os.environ.get('LOCALAPPDATA',''))/'Programs/Ollama/ollama.exe'
                    if not executable.is_file():raise RuntimeError('ollama_not_installed')
                    emit('start_ollama')
                    subprocess.Popen([str(executable),'serve'],env=dict(os.environ,OLLAMA_HOST='127.0.0.1:11434'))
                    import time
                    for _ in range(30):
                        try:ollama_status();break
                        except Exception:time.sleep(1)
                    else:raise RuntimeError('ollama_not_ready')
                emit('downloading_model');pull_model(plan['model'],consent=True,
                    progress=lambda row:events.put(('progress',100*row.get('completed',0)/max(1,row.get('total',1)))))
            emit('checking_app');run_python(python,[str(target/'tools/release_preflight.py')],target)
            run_python(python,['-c','import webview,clr; from webview.platforms.winforms import _is_chromium; import sys; sys.exit(0 if _is_chromium() else 4)'],target)
            # A running/unknown game prevents activation and registration.
            run_python(python,['-c',idle_check],target/'tools')
            from unified_mod_setup import documents_directory
            from user_storage import profile_dir
            docs=documents_directory();profile=profile_dir()
            discovered=discover_previous(docs,extra=[chosen['previous']] if chosen['previous'] else [],managed_root=args.root)
            events.put(('text',tr('previous_found')+str(len(discovered['installations']))))
            if chosen['legacy']:
                if not chosen['previous']:raise ValueError('choose_previous_folder_for_import')
                import_legacy_profile(Path(chosen['previous']),profile,consent=True)
            controls=[args.root/'installation.json',args.root/'Start HOI4-EYES.bat']
            if chosen['register']:controls+=registration_controls(docs,profile)
            recover_pending(args.root,controls,consent=True,game_closed=True)
            transaction=UpdateTransaction(args.root,controls)
            if chosen['register']:
                emit('registering')
                run_python(python,[str(target/'tools/installer_mod_migration.py'),'--root',str(target)],target)
                if adopt_registration(docs,profile,consent=True):
                    transaction.checkpoint([registration_controls(docs,profile)[1]])
                command=[str(target/'tools/unified_mod_setup.py'),'install','--confirm']
                if game_path:command+=['--game',game_path]
                run_python(python,command,target)
                transaction.checkpoint(registration_controls(docs,profile)[:2])
                select_single_mod(docs,consent=True);transaction.checkpoint([docs/'dlc_load.json'])
            migrated=target/'installer-migrated.json'
            preserved=json.loads(migrated.read_text(encoding='utf-8'))['preserved'] if migrated.exists() else []
            activate(args.root,target,consent=True,game_closed=True,preserved=preserved);installed=target
            transaction.checkpoint([args.root/'installation.json'])
            # Stable user-run launcher. No global environment variables changed.
            launchpath=args.root/'Start HOI4-EYES.bat'
            launchpath.write_bytes(launcher_bytes(target.relative_to(args.root.resolve()).as_posix()))
            transaction.checkpoint([launchpath])
            cache_setup_path=cache_setup(args.bundle,args.root,consent=True)
            state=json.loads((args.root/'installation.json').read_text(encoding='utf-8'));state['launcher_sha256']=digest(launchpath)
            state['setup_cache']=cache_setup_path.relative_to(args.root.resolve()).as_posix()
            state['version']=plan['version']
            atomic(args.root/'installation.json',state)
            transaction.checkpoint([args.root/'installation.json'])
            installed_apps(args.root,cache_setup_path,plan['version'],consent=True)
            transaction.commit()
            status();emit('complete');events.put(('done',None))
        except Exception as error:
            if transaction is not None:
                try:transaction.rollback()
                except (OSError,ValueError):pass  # Conflicting control files remain untouched for review.
            events.put(('failed',None))
            # Record only an error category, never tokens, chat or installer output.
            try:
                root=owned_root(args.root);atomic(root/'last-setup-error.json',{'kind':type(error).__name__,'reason':str(error)[:160]})
            except Exception:pass
        finally:
            if lock is not None:
                try:lock.__exit__(None,None,None)
                except (OSError,RuntimeError):pass
    buttons=ttk.Frame(frame);buttons.pack(fill='x')
    controls=[]
    def begin(action):
        nonlocal busy
        if busy:return
        chosen={'app':consent.get(),'ollama':ollama.get(),'webview':webview.get(),'model':model.get(),'register':register.get()}
        chosen.update(previous=previous.get().strip(),legacy=legacy.get())
        if action!='check' and not chosen['app']:messagebox.showinfo('HOI4-EYES',tr('need_consent'));return
        if action=='remove' and not messagebox.askyesno('HOI4-EYES',tr('remove_confirm')):return
        if action=='install' and not messagebox.askyesno('HOI4-EYES',tr('install_confirm')):return
        busy=True
        for button in controls:button.configure(state='disabled')
        threading.Thread(target=worker,args=(action,chosen,game.get().strip()),daemon=True).start()
    for key,action in (('check','check'),('install','install'),('remove','remove')):
        label='update' if key=='install' and (installed is not None or previous_installations) else key
        button=ttk.Button(buttons,text=tr(label),command=lambda action=action:begin(action));button.pack(side='left',padx=4);controls.append(button)
    def launch():
        if installed is not None:os.startfile(str(args.root/'Start HOI4-EYES.bat'))
    button=ttk.Button(buttons,text=tr('launch'),command=launch);button.pack(side='right');controls.append(button)
    def poll():
        nonlocal busy
        while True:
            try:kind,value=events.get_nowait()
            except queue.Empty:break
            if kind=='text':out.configure(state='normal');out.insert('end',value+'\n');out.see('end');out.configure(state='disabled')
            elif kind=='progress':progress.configure(value=value)
            else:
                busy=False
                for button in controls:button.configure(state='normal')
                if kind=='failed':messagebox.showerror('HOI4-EYES',tr('failed'))
        window.after(150,poll)
    def close():
        if busy:messagebox.showinfo('HOI4-EYES',tr('busy'));return
        window.destroy()
    window.protocol('WM_DELETE_WINDOW',close)
    if args.uninstall:
        window.after(200,lambda:messagebox.showinfo('HOI4-EYES',tr('uninstall_help')))
    poll();window.mainloop()

if __name__=='__main__':main()
