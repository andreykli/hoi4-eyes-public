"""Consent-based installation primitives. No actions occur on import.

Only managed application files are removable. Profiles, saves, shared runtimes
and models are deliberately outside this module's removal authority.
"""
from hashlib import sha256
from contextlib import contextmanager
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from uuid import uuid4

OWNER='HOI4-EYES installer C v1'
HOSTS={'www.python.org','go.microsoft.com','github.com','release-assets.githubusercontent.com',
       'objects.githubusercontent.com','msedge.sf.dl.delivery.mp.microsoft.com'}

def digest(path):
    h=sha256()
    with Path(path).open('rb') as stream:
        for part in iter(lambda:stream.read(1024*1024),b''):h.update(part)
    return h.hexdigest()

def launcher_bytes(relative):
    # Keep the file ASCII. cmd expands its own directory as Unicode; do not
    # embed a user's name in a batch file interpreted using an OEM codepage.
    if (not re.fullmatch(r'versions/[A-Za-z0-9_.-]+',relative) or '..' in relative):
        raise ValueError('unsafe_launcher_target')
    base='%~dp0'+relative.replace('/','\\')
    return ('@echo off\r\nsetlocal DisableDelayedExpansion\r\nstart "" "'+base+
        '\\.venv\\Scripts\\pythonw.exe" "'+base+'\\tools\\windows_desktop.py" --root "'+base+'"\r\n').encode('ascii')

def child(root,name):
    path=PurePosixPath(name)
    if (not name or path.is_absolute() or any(p in {'..','.'} for p in path.parts)
            or any(c in name for c in ('\\',':','\n','\r','\0'))):
        raise ValueError('unsafe_install_path')
    root=Path(root).resolve();result=root.joinpath(*path.parts)
    if not result.resolve().is_relative_to(root):raise ValueError('install_path_escape')
    for item in (result,*result.parents):
        if item==root:break
        if item.exists() and (item.is_symlink() or getattr(item,'is_junction',lambda:False)()):
            raise ValueError('install_reparse_path')
    return result

def atomic(path,value):
    path=Path(path);temp=path.with_name(path.name+'.pending')
    if temp.is_symlink() or getattr(temp,'is_junction',lambda:False)():raise ValueError('unsafe_temporary_path')
    temp.write_text(json.dumps(value,indent=2),encoding='utf-8');os.replace(temp,path)

def verify_files(root,files):
    for name,expected in files.items():
        path=child(root,name)
        if (not path.is_file() or path.stat().st_size!=expected['bytes']
                or digest(path)!=expected['sha256']):raise ValueError('file_integrity_failed:'+name)

def load_plan(bundle):
    bundle=Path(bundle).resolve()
    raw=(bundle/'_eyes_setup/plan.json').read_bytes()
    plan=json.loads(raw)
    if plan.get('schema')!=1 or plan.get('owner')!=OWNER:raise ValueError('unknown_install_plan')
    for name,row in plan['files'].items():
        child(bundle,name)
        if not re.fullmatch('[0-9a-f]{64}',row['sha256']) or type(row['bytes']) is not int or row['bytes']<0:
            raise ValueError('invalid_install_manifest')
    verify_files(bundle,plan['files'])
    actual={p.relative_to(bundle).as_posix() for p in (bundle/'_eyes_setup').rglob('*') if p.is_file()}
    if actual!=set(plan['files'])|{'_eyes_setup/plan.json'}:raise ValueError('unexpected_setup_file')
    return plan,sha256(raw).hexdigest()

def owned_root(root,create=False):
    root=Path(root).resolve();marker=root/'installer-owner.json'
    if root==Path(root.anchor) or root==Path.home().resolve():raise ValueError('unsafe_install_root')
    if marker.exists():
        if json.loads(marker.read_text(encoding='utf-8')).get('owner')!=OWNER:raise ValueError('foreign_installation')
    elif create:
        if root.exists() and any(root.iterdir()):raise ValueError('nonempty_unmanaged_installation')
        root.mkdir(parents=True,exist_ok=True);atomic(marker,{'owner':OWNER})
    else:raise ValueError('managed_installation_required')
    return root

def stage(bundle,root,*,consent=False):
    if not consent:raise PermissionError('installation_consent_required')
    plan,identity=load_plan(bundle);root=owned_root(root,create=True)
    name=plan['version']+'-'+identity[:12]
    if not re.fullmatch('[A-Za-z0-9_.-]+',name):raise ValueError('invalid_install_version')
    target=child(root,'versions/'+name);target.mkdir(parents=True,exist_ok=True)
    prefix='_eyes_setup/app/'
    for path,row in plan['files'].items():
        if not path.startswith(prefix):continue
        relative=path[len(prefix):];dest=child(target,relative);source=child(bundle,path)
        dest.parent.mkdir(parents=True,exist_ok=True)
        # Identical retries are harmless. Modified managed source is not replaced.
        if dest.exists():
            if digest(dest)!=row['sha256']:raise ValueError('existing_version_modified:'+relative)
        else:
            temp=dest.with_name(dest.name+'.pending');shutil.copyfile(source,temp);os.replace(temp,dest)
    atomic(target/'installer-stage.json',{'owner':OWNER,'plan':identity})
    return target

def activate(root,target,*,consent=False,game_closed=False,preserved=()):
    if not consent or not game_closed:raise PermissionError('close_game_and_confirm')
    root=owned_root(root);target=Path(target).resolve()
    if not target.is_relative_to(root/'versions'):raise ValueError('foreign_install_target')
    marker=json.loads((target/'installer-stage.json').read_text())
    if marker.get('owner')!=OWNER:raise ValueError('foreign_install_target')
    # Snapshot only files owned at activation. Future treaty files are not ours.
    files={}
    for p in target.rglob('*'):
        if not p.is_file():continue
        name=p.relative_to(target).as_posix();child(target,name)
        if name not in preserved:files[name]={'bytes':p.stat().st_size,'sha256':digest(p)}
    previous=root/'installation.json'
    if previous.exists():
        backup=root/('installation-before-update-'+uuid4().hex+'.json')
        shutil.copyfile(previous,backup)
    atomic(previous,{'owner':OWNER,'active':target.relative_to(root).as_posix(),'files':files})

def active(root):
    root=owned_root(root);state=json.loads((root/'installation.json').read_text())
    if state.get('status')=='removed':raise ValueError('application_removed')
    if state.get('owner')!=OWNER or not state['active'].startswith('versions/'):
        raise ValueError('invalid_installation_state')
    return child(root,state['active'])

def remove(root,*,consent=False,game_closed=False,preserve_prefixes=()):
    if not consent or not game_closed:raise PermissionError('removal_consent_and_closed_game_required')
    root=owned_root(root);target=active(root)
    state=json.loads((root/'installation.json').read_text());kept=[];removed=[]
    for name,row in state['files'].items():
        path=child(target,name)
        if not path.exists():continue
        if any(name.startswith(prefix) for prefix in preserve_prefixes):kept.append(name);continue
        if not path.is_file() or digest(path)!=row['sha256']:kept.append(name);continue
        try:path.unlink();removed.append(name)
        except OSError:kept.append(name)
    # Never recursive-delete a directory. Preserve unknown/generated files.
    for directory in sorted((p for p in target.rglob('*') if p.is_dir()),key=lambda p:len(p.parts),reverse=True):
        try:child(target,directory.relative_to(target).as_posix());directory.rmdir()
        except (OSError,ValueError):pass
    launcher=child(root,'Start HOI4-EYES.bat')
    if launcher.is_file() and state.get('launcher_sha256')==digest(launcher):launcher.unlink()
    atomic(root/'last-removal.json',{'removed':removed,'preserved':kept,'profile_preserved':True})
    atomic(root/'installation.json',{'owner':OWNER,'status':'removed'})
    return {'removed':removed,'preserved':kept,'profile_preserved':True}

@contextmanager
def installation_lock(root,*,consent=False):
    if not consent:raise PermissionError('installation_consent_required')
    root=owned_root(root,create=True)
    with child(root,'installer.lock').open('a+b') as stream:
        if stream.tell()==0:stream.write(b'0');stream.flush()
        stream.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError as error:raise RuntimeError('another_installation_is_running') from error
        try:yield
        finally:
            stream.seek(0)
            if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)

class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        validate_url(newurl)
        return super().redirect_request(req,fp,code,msg,headers,newurl)

def validate_url(url):
    parsed=urllib.parse.urlsplit(url)
    if (parsed.scheme!='https' or parsed.hostname not in HOSTS or parsed.username or parsed.password
            or parsed.port not in (None,443)):raise ValueError('unapproved_download_origin')

def download(component,cache,*,consent=False,progress=lambda n,total:None,opener=None):
    if not consent:raise PermissionError('download_consent_required')
    validate_url(component['url'])
    expected=component['sha256']
    if not re.fullmatch('[0-9a-f]{64}',expected):raise ValueError('invalid_vendor_digest')
    cache=Path(cache);cache.mkdir(parents=True,exist_ok=True)
    target=child(cache,expected+'.exe')
    if target.exists() and digest(target)==expected:return target
    partial=child(cache,expected+'.part')
    op=opener or urllib.request.build_opener(SafeRedirect())
    offset=partial.stat().st_size if partial.exists() else 0
    if offset>=component['bytes']:
        if offset==component['bytes'] and digest(partial)==expected:os.replace(partial,target);return target
        partial.rename(partial.with_name(partial.name+'.rejected-'+uuid4().hex));offset=0
    request=(urllib.request.Request(component['url'],headers={'Range':'bytes='+str(offset)+'-'})
             if offset else component['url'])
    with op.open(request,timeout=30) as source:
        status=getattr(source,'status',200)
        if offset and status==206:
            header=source.headers.get('Content-Range','')
            match=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',header)
            if (not match or int(match[1])!=offset or int(match[3])!=component['bytes']
                    or not offset<=int(match[2])<component['bytes']):raise ValueError('invalid_download_range')
        elif status==200:offset=0
        else:raise ValueError('unexpected_download_status')
        with partial.open('ab' if offset else 'wb') as out:
            size=offset
            while True:
                part=source.read(1024*1024)
                if not part:break
                size+=len(part)
                if size>component['bytes']:raise ValueError('vendor_download_too_large')
                out.write(part);progress(size,component['bytes'])
    if size!=component['bytes'] or digest(partial)!=expected:
        if size==component['bytes']:partial.rename(partial.with_name(partial.name+'.rejected-'+uuid4().hex))
        raise ValueError('vendor_download_integrity_failed')
    os.replace(partial,target);return target

def run_visible(argv,*,consent=False,cwd=None,env=None):
    if not consent:raise PermissionError('process_consent_required')
    # No shell, elevation, hidden windows, policy changes or downloaded scripts.
    result=subprocess.run([str(v) for v in argv],cwd=cwd,env=env,check=False)
    if result.returncode not in (0,3010):raise RuntimeError('component_exit_'+str(result.returncode))

def require_idle(game_running,pids):
    if game_running is not False or not isinstance(pids,(list,tuple,set)) or pids:
        raise PermissionError('close_eyes_and_game_before_setup')

def require_launcher_closed():
    import csv
    import io
    executable=Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/tasklist.exe'
    result=subprocess.run([str(executable),'/FO','CSV','/NH'],capture_output=True,text=True,timeout=20)
    if result.returncode or not result.stdout.strip():raise RuntimeError('launcher_state_unknown')
    rows=list(csv.reader(io.StringIO(result.stdout)))
    if any(len(row)<2 or not row[1].isdigit() for row in rows):raise RuntimeError('launcher_state_unknown')
    if any(row[0].casefold() in {'dowser.exe','launcher.exe','paradox launcher.exe','paradoxlauncher.exe','bootstrapper-v2.exe'} for row in rows):
        raise RuntimeError('close_paradox_launcher_before_setup')

def local_app_pids():
    """Unlike a readiness hint, setup must reject failed socket observation."""
    executable=Path(os.environ.get('SystemRoot','C:/Windows'))/'System32/netstat.exe'
    result=subprocess.run([str(executable),'-ano','-p','tcp'],capture_output=True,text=True,timeout=20)
    if result.returncode or not result.stdout.strip():raise RuntimeError('app_state_unknown')
    pids=set()
    for line in result.stdout.splitlines():
        fields=line.split()
        if not fields or fields[0]!='TCP':continue
        if len(fields)<5 or not fields[-1].isdigit():raise RuntimeError('app_state_unknown')
        if fields[1].rsplit(':',1)[-1]=='5016' and int(fields[-1])>0:pids.add(int(fields[-1]))
    return sorted(pids)

def ollama_status(url='http://127.0.0.1:11434',opener=urllib.request.urlopen):
    if url!='http://127.0.0.1:11434':raise ValueError('local_ollama_endpoint_required')
    with opener(url+'/api/tags',timeout=3) as response:return json.load(response).get('models',[])

def model_state(models,model):
    row=next((m for m in models if m.get('name')==model['model']),None)
    return 'missing' if row is None else 'ready' if row.get('digest')==model['ollama_digest'] else 'different_digest'

def pull_model(model,*,consent=False,opener=urllib.request.urlopen,progress=lambda row:None):
    if not consent:raise PermissionError('model_download_consent_required')
    current=model_state(ollama_status(opener=opener),model)
    if current=='different_digest':raise ValueError('existing_model_preserved_different_digest')
    if current=='ready':return 'reused'
    # Check the registry's exact manifest before allowing the mutable tag pull.
    registry='https://registry.ollama.ai/v2/library/qwen3/manifests/4b-instruct'
    with opener(registry,timeout=15) as response:raw=response.read()
    if sha256(raw).hexdigest()!=model['ollama_digest']:raise ValueError('upstream_model_changed')
    request=urllib.request.Request('http://127.0.0.1:11434/api/pull',
        data=json.dumps({'model':model['model'],'stream':True}).encode(),headers={'Content-Type':'application/json'})
    with opener(request,timeout=600) as response:
        for line in response:
            row=json.loads(line)
            if row.get('error'):raise RuntimeError('ollama_model_download_failed')
            progress({key:row[key] for key in ('status','total','completed') if key in row})
    if model_state(ollama_status(opener=opener),model)!='ready':raise ValueError('model_result_not_verified')
    return 'verified'
