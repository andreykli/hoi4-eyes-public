"""User-run Bridge replacement. WMI via cscript; no PowerShell or DB access."""
import ctypes
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
from user_storage import runtime_dir

ROOT = Path(__file__).resolve().parents[1]
ENDPOINT = 'http://127.0.0.1:5016/respond'


def argv(command):
    count = ctypes.c_int()
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    result = shell.CommandLineToArgvW(command, ctypes.byref(count))
    if not result: raise RuntimeError('Cannot verify process command line')
    try: return [result[i] for i in range(count.value)]
    finally:
        kernel = ctypes.WinDLL('kernel32')
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(ctypes.cast(result, ctypes.c_void_p))


def same_path(left, right):
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def classify(arguments):
    arguments=list(arguments)
    while len(arguments)>1 and arguments[1] in {'-u','-B','-I','-E','-s'}:
        arguments.pop(1)
    if len(arguments) < 2: return 'other'
    # WMI does not provide the process working directory. A relative script
    # path must never be resolved against this launcher's Experimental cwd.
    if not os.path.isabs(arguments[1]):return 'other'
    if same_path(arguments[1], ROOT / 'tools/experimental_stack.py'):
        try:
            if not same_path(arguments[arguments.index('--root')+1],ROOT):return 'other'
        except (ValueError,IndexError):return 'other'
        return 'server' if '--serve' in arguments[2:] else 'supervisor'
    if same_path(arguments[1], ROOT / 'tools/hoi4_bridge.py'):
        try:
            endpoint = arguments[arguments.index('--endpoint') + 1]
            response = arguments[arguments.index('--response-file') + 1]
        except (ValueError, IndexError): return 'unverified_bridge'
        if endpoint == ENDPOINT and same_path(response, runtime_dir(ROOT) / 'response.json'):
            return 'bridge'
        return 'unverified_bridge'
    return 'other'


def processes():
    system = Path(os.environ['SystemRoot']) / 'System32'
    result = subprocess.run([str(system / 'cscript.exe'), '//nologo',
        str(ROOT / 'tools/bridge_processes.vbs')], capture_output=True, text=True, timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    if result.returncode: raise RuntimeError('WMI/cscript is unavailable. No processes stopped.')
    rows = {}
    for line in result.stdout.splitlines():
        fields = line.split('\t', 4)
        if len(fields) != 5: raise RuntimeError('Unrecognized WMI response. No processes stopped.')
        pid, parent, created, executable, command = fields
        if not command: raise RuntimeError('Process identity unavailable. No processes stopped.')
        rows[int(pid)] = {'parent': int(parent), 'created': created, 'executable': executable,
                          'kind': classify(argv(command))}
    return rows


def main():
    if os.name != 'nt': raise RuntimeError('Windows required')
    if not (ROOT / 'HOI4_EYES_Experimental.mod').exists(): raise RuntimeError('HOI4-EYES application files are missing')
    token = runtime_dir(ROOT) / 'experimental_auth.token'
    if not token.exists(): raise RuntimeError('HOI4-EYES token missing. No processes stopped.')
    with urllib.request.urlopen('http://127.0.0.1:5016/health', timeout=3) as response:
        if response.status != 200: raise RuntimeError('HOI4-EYES Flask is not ready')
    rows = processes()
    if any(r['kind'] in {'supervisor', 'unverified_bridge'} for r in rows.values()):
        raise RuntimeError('Supervisor or ambiguous HOI4-EYES Bridge found. No processes stopped; Flask protected.')
    targets = [(pid, r) for pid, r in rows.items() if r['kind'] == 'bridge']
    # Recheck PID creation times before stopping anything. Never kill a tree.
    fresh = processes()
    for pid, row in targets:
        if pid in fresh and (fresh[pid]['created'] != row['created'] or fresh[pid]['kind'] != 'bridge'):
            raise RuntimeError('Process identity changed. No processes stopped.')
    taskkill = str(Path(os.environ['SystemRoot']) / 'System32/taskkill.exe')
    for pid, _ in sorted(targets, key=lambda x: x[1]['parent'], reverse=True):
        current = processes().get(pid)
        if current is None: continue
        if current['created'] != rows[pid]['created'] or current['kind'] != 'bridge':
            raise RuntimeError('PID changed; stopping restart safely')
        subprocess.run([taskkill, '/PID', str(pid), '/F'], check=True, capture_output=True)
    time.sleep(0.5)
    if any(r['kind'] == 'bridge' for r in processes().values()): raise RuntimeError('Old Bridge remains; no duplicate started')
    env = dict(os.environ, EYES_BRIDGE_AUTH_FILE=str(token), AI_BRIDGE_URL='http://127.0.0.1:5016')
    print('Starting HOI4-EYES Bridge only. Flask and HOI4 stay running.', flush=True)
    return subprocess.call([sys.executable, str(ROOT / 'tools/hoi4_bridge.py'), '--endpoint', ENDPOINT,
        '--response-file', str(runtime_dir(ROOT) / 'response.json'),
        '--status-file', str(runtime_dir(ROOT) / 'bridge_status.json')], cwd=ROOT, env=env)


if __name__ == '__main__':
    try: sys.exit(main())
    except Exception as error:
        print('Restart refused:', error, file=sys.stderr);sys.exit(1)
