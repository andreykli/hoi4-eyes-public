"""User-run isolated stack. Never starts HOI4 or replaces production services."""
from pathlib import Path
import argparse
import os
import secrets
import socket
import subprocess
import sys
import time
import requests
from user_storage import runtime_dir, migrate_legacy
from model_profile import selected_model

PORT = 5016

def stack_environment(root, owner_token=None):
    """Common configuration for the existing console launcher and desktop hub."""
    root=Path(root).resolve();runtime=runtime_dir(root);runtime.mkdir(parents=True,exist_ok=True)
    if not (root/'HOI4_EYES_Experimental.mod').is_file():raise RuntimeError('HOI4-EYES application files are missing')
    token_file=runtime/'experimental_auth.token'
    if not token_file.exists():
        try:
            with token_file.open('x',encoding='ascii') as stream:stream.write(secrets.token_hex(32))
        except FileExistsError:pass
    env=dict(os.environ,EYES_BRIDGE_AUTH_FILE=str(token_file),EYES_STATE_DB=str(runtime/'eyes_experimental.sqlite3'),
        EYES_RESPONSE_PROVIDER='ollama',OLLAMA_MODEL=selected_model(),AI_BRIDGE_URL=f'http://127.0.0.1:{PORT}',
        EYES_BRIDGE_STATUS_FILE=str(runtime/'bridge_status.json'))
    env.setdefault('EYES_EXPRESSION_MODE','auto');env.setdefault('EYES_EXPRESSION_TIMEOUT','12');env.setdefault('EYES_OLLAMA_THREADS','2')
    env.pop('EYES_BRIDGE_AUTH_TOKEN',None)
    if owner_token:env['EYES_MENU_OWNER_TOKEN']=owner_token
    else:env.pop('EYES_MENU_OWNER_TOKEN',None)
    return env


def listener_pids(port=PORT):
    """Read socket ownership without stopping or attaching to any process."""
    netstat=str(Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/netstat.exe') if os.name=='nt' else 'netstat'
    result = subprocess.run([netstat, '-ano', '-p', 'tcp'], capture_output=True, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    found = set()
    for line in result.stdout.splitlines():
        fields = line.split()
        if (len(fields) >= 5 and fields[0] == 'TCP'
                and fields[1].rsplit(':', 1)[-1] == str(port)
                and fields[3] == 'LISTENING' and fields[-1].isdigit()):
            found.add(int(fields[-1]))
    return sorted(found)


def existing_listener():
    pids = listener_pids()
    if not pids:
        return False
    print(f'Port {PORT} already occupied. PID: {", ".join(map(str, pids))}. No second instance started.', flush=True)
    print('Existing processes were not touched. This does not reload updated source code.', flush=True)
    return True


def main():
    p = argparse.ArgumentParser();p.add_argument('--root', required=True);p.add_argument('--serve', action='store_true')
    args = p.parse_args();root = Path(args.root).resolve();runtime = runtime_dir(root)
    if not (root / 'HOI4_EYES_Experimental.mod').exists(): raise SystemExit('HOI4-EYES application files are missing')
    if not args.serve and existing_listener():
        from start_missing_bridge import start_missing_bridge
        raise SystemExit(start_missing_bridge(root))
    env = stack_environment(root)
    # The child inherits a menu owner capability, but ordinary console launches
    # do not create one or adopt existing processes.
    if args.serve and os.getenv('EYES_MENU_OWNER_TOKEN'):env['EYES_MENU_OWNER_TOKEN']=os.environ['EYES_MENU_OWNER_TOKEN']
    os.environ.pop('EYES_BRIDGE_AUTH_TOKEN',None)
    os.environ.update({k: env[k] for k in env if k.startswith(('EYES_', 'OLLAMA_')) or k == 'AI_BRIDGE_URL'})
    if args.serve:
        from werkzeug.serving import make_server
        # Own the socket before opening SQLite. A losing concurrent start must
        # not initialize the database while the real server is already running.
        server=make_server('127.0.0.1',PORT,None,threaded=True)
        try:
            migrate_legacy(root)
            from world_sessions import restore_recovered_exports
            restore_recovered_exports(runtime,Path(env['EYES_STATE_DB']))
            from diplomacy_engine import DiplomacyEngine
            from ollama_provider import OllamaTextProvider
            from mock_ai_server import create_app
            engine=DiplomacyEngine(response_text_provider=OllamaTextProvider.from_env())
            engine.autonomous_initiatives_enabled=True
            if not Path(env['EYES_STATE_DB']).exists():
                from chat_state_store import ChatStateStore
                ChatStateStore(env['EYES_STATE_DB']).save(engine.export_chat_state())
            app=create_app(engine=engine,state_db_path=env['EYES_STATE_DB'])
            from hub_control import attach_server_control
            attach_server_control(app,env.get('EYES_MENU_OWNER_TOKEN'),server.shutdown)
            server.app=app
            server.serve_forever()
        finally:server.server_close()
        return
    try:
        tags = requests.get('http://127.0.0.1:11434/api/tags', timeout=3).json()
        if not any(m.get('name') == env['OLLAMA_MODEL'] for m in tags.get('models', [])):
            raise RuntimeError(env['OLLAMA_MODEL']+' is not installed; nothing downloaded')
        with socket.socket() as sock: sock.bind(('127.0.0.1', PORT))
    except Exception as error:
        if existing_listener():
            from start_missing_bridge import start_missing_bridge
            raise SystemExit(start_missing_bridge(root))
        raise SystemExit(f'Readiness check failed: {error}. Existing processes were not touched.')
    hidden = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    server = bridge = None
    with (runtime / 'flask.log').open('a', encoding='utf-8') as log:
        try:
            server = subprocess.Popen([sys.executable, __file__, '--root', str(root), '--serve'],
                env=env, cwd=root, stdout=log, stderr=log, creationflags=hidden)
            for _ in range(30):
                if server.poll() is not None: raise RuntimeError('HOI4-EYES Flask stopped; inspect runtime/flask.log')
                try:
                    if requests.get(f'http://127.0.0.1:{PORT}/health', timeout=1).ok: break
                except requests.RequestException: pass
                time.sleep(1)
            else: raise RuntimeError('HOI4-EYES Flask readiness timeout')
            from start_missing_bridge import start_missing_bridge
            return start_missing_bridge(root)

        finally:
            # No automatic process termination. The launcher reports failures;
            # the user retains ownership of the running Experimental server.
            pass



if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('Launcher closed. Existing processes were not terminated.', flush=True)
        raise SystemExit(130)
    except Exception as error:
        print(f'HOI4-EYES startup failed: {error}', file=sys.stderr, flush=True)
        raise SystemExit(1)
