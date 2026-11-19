"""User-run recovery of a missing HOI4-EYES Bridge; never stops processes."""
import os
from pathlib import Path
import subprocess
import sys
import time
import requests
from user_storage import runtime_dir
from restart_chat_only import processes, ROOT, ENDPOINT

def spawn_bridge(root,env):
    root=Path(root);runtime=runtime_dir(root)
    flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
    with (runtime/'bridge.log').open('a',encoding='utf8') as log:
        return subprocess.Popen([sys.executable,'-u',str(root/'tools/hoi4_bridge.py'),
            '--endpoint',ENDPOINT,'--response-file',str(runtime/'response.json'),
            '--status-file',str(runtime/'bridge_status.json')],cwd=root,env=env,
            stdout=log,stderr=log,creationflags=flags)


def launch_bridge(root, env):
    """Same logged, unbuffered startup for both fresh and reused Flask."""
    from stack_readiness import ready_record
    runtime=runtime_dir(root)
    flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
    with (runtime/'bridge.log').open('a',encoding='utf8') as log:
        for attempt in range(3):
            child=spawn_bridge(root,env)
            deadline=time.monotonic()+45
            while time.monotonic()<deadline:
                if child.poll() is not None:
                    break
                ready=ready_record(root,processes())
                if ready:
                    print(f"Flask ready on 5016. Bridge UI ready. PID: {ready['pid']}. Keep this launcher open.",flush=True)
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError('Bridge process started but Tk readiness timed out. Inspect runtime/bridge.log. No process stopped.')
            if child.poll() is None:
                code=child.wait()
            else:
                code=child.returncode
            if code==0:
                return 0
            if any(row['kind']=='bridge' for row in processes().values()):
                raise RuntimeError('Bridge exited but another reader remains. No duplicate started; inspect runtime/bridge.log')
            print(f'Bridge exited with code {code}; recovery {attempt+1}/2. See runtime/bridge.log',flush=True)
        raise RuntimeError('Bridge recovery exhausted; inspect runtime/bridge.log. Flask not stopped.')


def start_missing_bridge(root=ROOT):
    root=Path(root).resolve()
    if root != ROOT or not (root/'HOI4_EYES_Experimental.mod').exists():
        raise RuntimeError('HOI4-EYES identity mismatch; nothing started')
    from experimental_stack import listener_pids
    rows=processes(); owners=listener_pids()
    if not owners or any(rows.get(pid,{}).get('kind')!='server' for pid in owners):
        raise RuntimeError('Port 5016 is not owned by this HOI4-EYES server; nothing started')
    if any(row['kind']=='unverified_bridge' for row in rows.values()):
        raise RuntimeError('Ambiguous Bridge found; nothing started')
    bridges=[pid for pid,row in rows.items() if row['kind']=='bridge']
    token=runtime_dir(root)/'experimental_auth.token'
    if not token.exists():raise RuntimeError('Existing HOI4-EYES auth token missing; nothing started')
    health=requests.get('http://127.0.0.1:5016/health',timeout=3)
    health.raise_for_status(); data=health.json()
    if data.get('status')!='alive' or not data.get('bridge_auth_configured'):
        raise RuntimeError('HOI4-EYES Flask is not ready/authenticated; nothing started')
    from runtime_revision import compute_runtime_source_revision
    if data.get('runtime_source_revision')!=compute_runtime_source_revision():
        print('Warning: Flask uses an older source revision; it will NOT be restarted.',flush=True)
    if bridges:
        from stack_readiness import ready_record
        ready=ready_record(root,rows)
        if not ready:
            raise RuntimeError('Bridge PID exists but its UI readiness is missing/stale. No second Bridge started. Close the old HOI4-EYES session manually, then run start.bat.')
        print(f"HOI4-EYES Bridge UI ready. PID: {ready['pid']}. No duplicate started.",flush=True)
        return 0
    env=dict(os.environ,EYES_BRIDGE_AUTH_FILE=str(token),AI_BRIDGE_URL='http://127.0.0.1:5016')
    print('Starting missing HOI4-EYES Bridge only. Keep this window open. Flask/HOI4 are untouched.',flush=True)
    return launch_bridge(root,env)


if __name__=='__main__':
    try:sys.exit(start_missing_bridge())
    except Exception as error:print('Bridge recovery refused:',error,file=sys.stderr);sys.exit(1)
