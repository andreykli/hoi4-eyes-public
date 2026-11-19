"""Read-only readiness of the actual Tk event loop, not merely a Python PID."""
import json,time
from pathlib import Path
from user_storage import runtime_dir

def ready_record(root, rows, now=None):
    root=Path(root)
    try:
        status=json.loads((runtime_dir(root)/'bridge_status.json').read_text(encoding='utf8'))
        pulse=json.loads((runtime_dir(root)/'bridge_ready.json').read_text(encoding='utf8'))
    except (OSError,ValueError):return None
    pid=status.get('pid')
    if (rows.get(pid,{}).get('kind')!='bridge' or pulse.get('pid')!=pid
            or pulse.get('source_revision')!=status.get('source_revision')
            or pulse.get('state')!='ui_ready' or not status.get('overlay_enabled')):return None
    stamp=pulse.get('heartbeat_unix')
    if not isinstance(stamp,(int,float)) or not 0 <= (time.time() if now is None else now)-stamp <= 6:return None
    return status

def write_ui_pulse(path, pid, revision, game_session=None):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+f'.{pid}.tmp')
    tmp.write_text(json.dumps({'pid':pid,'source_revision':revision,'state':'ui_ready',
        'heartbeat_unix':time.time(),**({'game_session':game_session} if game_session is not None else {})}),encoding='utf8')
    tmp.replace(path)
