"""Build an unpublished variant-C Workshop folder; never install or upload it.

Wheel downloads/builds are an explicit prior developer operation. This builder
only copies reviewed wheels and verifies their official PyPI hashes online.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from email.parser import BytesParser
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import io
import tarfile
import urllib.request
import zipfile
from prepare_publication import source_files,scan
from install_manager import OWNER

ROOT=Path(__file__).resolve().parents[1]
EXTRA=('tools/install_manager.py','tools/installation_wizard.py','tools/installer_text.json',
       'tools/installer_mod_migration.py','tools/installer_lifecycle.py','tools/workshop_update.py')

def wheel_info(path):
    with zipfile.ZipFile(path) as z:
        metadata=next(n for n in z.namelist() if n.endswith('.dist-info/METADATA'))
        fields=BytesParser().parsebytes(z.read(metadata))
    return {'name':fields['Name'],'version':fields['Version'],'file':path.name,
            'sha256':sha256(path.read_bytes()).hexdigest()}

def verify_wheel(row,wheelhouse):
    url='https://pypi.org/pypi/'+row['name']+'/'+row['version']+'/json'
    with urllib.request.urlopen(url,timeout=20) as stream:metadata=json.load(stream)
    match=next((r for r in metadata['urls'] if r['filename']==row['file']),None)
    if match and match['digests']['sha256']==row['sha256']:
        return {**row,'origin':match['url'],'upstream_sha256':row['sha256']}
    if row['name'].lower().replace('_','-')=='proxy-tools' and row['version']=='0.1.0':
        source=next(r for r in metadata['urls'] if r['packagetype']=='sdist')
        with urllib.request.urlopen(source['url'],timeout=20) as stream:raw=stream.read()
        if sha256(raw).hexdigest()!=source['digests']['sha256']:raise ValueError('source_hash_mismatch')
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            expected={n.split('/',1)[1]:archive.extractfile(n).read() for n in archive.getnames()
                      if n.startswith('proxy_tools-0.1.0/proxy_tools/') and n.endswith('.py')}
        with zipfile.ZipFile(wheelhouse/row['file']) as wheel:
            actual={n:wheel.read(n) for n in wheel.namelist() if not '.dist-info/' in n}
            allowed={'METADATA','WHEEL','RECORD','top_level.txt'}
            if (actual!=expected or any(n.split('.dist-info/',1)[1] not in allowed
                    for n in wheel.namelist() if '.dist-info/' in n)):
                raise ValueError('local_wheel_not_equivalent_to_reviewed_source')
        return {**row,'origin':source['url'],'upstream_source_sha256':source['digests']['sha256'],
                'local_wheel_build':True,'license_note':'Source BSD notice retained; package metadata says MIT.'}
    raise ValueError('unverified_wheel:'+row['file'])

def build(output,wheelhouse):
    output=Path(output).resolve();wheelhouse=Path(wheelhouse).resolve()
    if not output.is_relative_to(ROOT/'runtime/installers') or output.exists():
        raise ValueError('new_isolated_installer_output_required')
    app,mod=source_files(ROOT)
    for name in EXTRA:app[name]=(ROOT/name).read_bytes()
    protocol=json.dumps({'protocol':1,'version':'0.9.0-beta.4','installer_revision':2},indent=2).encode()
    app['release/installation_protocol.json']=protocol
    app['prepared/unified_game_mod/ai_assistant/release/installation_protocol.json']=protocol
    mod['release/installation_protocol.json']=protocol
    app['SOURCE-MANIFEST.json']=(json.dumps({'version':'0.9.0-beta.4','files':{
        n:{'sha256':sha256(d).hexdigest(),'bytes':len(d)} for n,d in sorted(app.items())}},indent=2)+'\n').encode()
    scan(app);scan(mod)
    if b'PUBLIC_BUILD = True' not in app['tools/developer_access.py']:raise ValueError('public_capability_stub_required')
    rows=[wheel_info(p) for p in sorted(wheelhouse.glob('*.whl'))]
    if not rows or not any(r['name'].lower()=='pywebview' for r in rows):raise ValueError('complete_wheelhouse_required')
    approved=json.loads((ROOT/'release/installer_wheels.json').read_text())
    if {r['file']:r['sha256'] for r in rows}!={r['file']:r['sha256'] for r in approved}:
        raise ValueError('wheelhouse_differs_from_reviewed_lock')
    with ThreadPoolExecutor(max_workers=6) as pool:rows=list(pool.map(lambda row:verify_wheel(row,wheelhouse),rows))
    # Source files and dependency code are supplied directly, not a giant ZIP.
    files=dict(mod)
    files['HOW_TO_INSTALL.txt']=(ROOT/'installer/HOW_TO_INSTALL.txt').read_bytes()
    files.update({'_eyes_setup/app/'+name:data for name,data in app.items()})
    for row in rows:files['_eyes_setup/wheels/'+row['file']]=(wheelhouse/row['file']).read_bytes()
    lock=''.join(f'{r["name"]}=={r["version"]} --hash=sha256:{r["sha256"]}\n' for r in rows)
    if lock!=(ROOT/'release/requirements-installer.lock').read_text():raise ValueError('requirements_lock_mismatch')
    files['_eyes_setup/requirements.lock']=lock.encode()
    files['_eyes_setup/wheel-provenance.json']=json.dumps(rows,indent=2).encode()
    files['_eyes_setup/licenses/proxy_tools-BSD.txt']=(ROOT/'installer/licenses/proxy_tools-BSD.txt').read_bytes()
    files['_eyes_setup/THIRD-PARTY.txt']=(ROOT/'installer/THIRD-PARTY.txt').read_bytes()
    plan={'schema':1,'owner':OWNER,'version':'0.9.0-beta.4-installer.c2',
          'sources':json.loads((ROOT/'release/installer_sources.json').read_text())['sources'],
          'model':json.loads((ROOT/'release/model_profile.json').read_text()),
          'files':{n:{'sha256':sha256(d).hexdigest(),'bytes':len(d)}
                   for n,d in sorted(files.items()) if n.startswith('_eyes_setup/')}}
    output.mkdir(parents=True)
    for name,data in files.items():
        path=output/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    plan_path=output/'_eyes_setup/plan.json';plan_path.write_bytes(json.dumps(plan,indent=2).encode())
    compiler=Path(os.environ.get('WINDIR','C:/Windows'))/'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    command=[str(compiler),'/nologo','/target:winexe','/platform:x64','/optimize+','/debug-','/codepage:65001',
        '/r:System.Windows.Forms.dll','/r:System.Drawing.dll','/r:System.Web.Extensions.dll',
        '/win32manifest:'+str(ROOT/'installer/app.manifest'),
        '/resource:'+str(plan_path)+',eyes.plan','/out:'+str(output/'Install HOI4-EYES.exe'),
        str(ROOT/'installer/Bootstrap.cs')]
    subprocess.run(command,check=True,capture_output=True)
    report={'variant':'C preview','published':False,'vendor_installers_executed':False,
        'native_bootstrap_signed':False,'gameplay_verified':False,'files':len(files)+2,
        'bytes':sum(p.stat().st_size for p in output.rglob('*') if p.is_file()),
        'wheel_count':len(rows),'public_qa_excluded':True,
        'remaining':['Clean Windows/VM installation and upgrade acceptance','Authenticode signing/reputation',
                     'HOI4 Workshop policy confirmation before publication']}
    # Outside the folder delivered as a mod.
    output.with_suffix('.report.json').write_text(json.dumps(report,indent=2))
    return report

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--wheelhouse',type=Path,required=True);args=parser.parse_args()
    print(json.dumps(build(args.output,args.wheelhouse),indent=2))
