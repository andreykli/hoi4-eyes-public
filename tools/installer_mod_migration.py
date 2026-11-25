"""Copy existing EYES-owned package files, without changing IDs or receipts."""
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
from install_manager import child,atomic
from treaty_package_install import read_manifest,hoI4_running
from unified_mod_setup import documents_directory,DESCRIPTOR
from validate_hoi4_gui import parse_mod_descriptor as parse_pdx

def copy_owned(old,new):
    old,new=Path(old).resolve(),Path(new).resolve()
    if not old.is_dir() or not (old/'descriptor.mod').is_file():raise ValueError('old_mod_missing')
    manifest=read_manifest(old);owned={};retired=set()
    for entry in manifest['installed']:
        owned.update(entry['files']);retired.update(entry.get('retired_reviews',[]))
    # Detect generated material which has no trustworthy installation record.
    for path in old.rglob('*'):
        if (path.is_file() and '.eyes_install' not in path.relative_to(old).parts
                and re.match(r'eyes_(?:agreement|cm|treaty_monitor)_',path.name)
                and path.relative_to(old).as_posix() not in owned):
            raise ValueError('unmanaged_personal_package_requires_review')
    copies={}
    for name,expected in owned.items():
        source=child(old,name)
        if name in retired and not source.exists():continue
        if not source.is_file() or sha256(source.read_bytes()).hexdigest()!=expected:
            raise ValueError('old_package_modified_or_missing')
        if Path(name).suffix.lower() not in {'.txt','.yml','.tga','.dds','.gui','.mod'}:
            raise ValueError('unexpected_package_file_type')
        copies[name]=source
    journal=old/'.eyes_install'
    if journal.exists():
        for path in journal.rglob('*'):
            if path.is_file() and path.suffix not in {'.lock','.tmp'}:
                name=path.relative_to(old).as_posix();copies[name]=child(old,name)
    # Preflight the entire migration before writing a single destination.
    for name,source in copies.items():
        dest=child(new,name)
        if dest.exists() and dest.read_bytes()!=source.read_bytes():raise ValueError('new_mod_package_collision')
    for name,source in copies.items():
        dest=child(new,name);dest.parent.mkdir(parents=True,exist_ok=True)
        if not dest.exists():shutil.copyfile(source,dest)
    return sorted(copies)

def migrate(root,documents=None):
    if hoI4_running() is not False:raise ValueError('close_game_before_migration')
    root=Path(root).resolve();docs=Path(documents or documents_directory())
    descriptor=docs/'mod'/DESCRIPTOR
    preserved=[]
    if descriptor.exists():
        data=parse_pdx(descriptor.read_text(encoding='utf-8-sig'))
        name=str(data.first('name') or '').strip('"');source=data.first('path')
        if not re.fullmatch(r'HOI4-EYES(?: \d+\.\d+\.\d+(?:-[a-z0-9.]+)?)?',name) or not isinstance(source,str):
            raise ValueError('foreign_mod_registration')
        old=Path(source.strip('"'))
        if not old.is_absolute():old=docs/old
        new=root/'prepared/unified_game_mod/ai_assistant'
        if old.resolve()!=new.resolve():preserved=copy_owned(old,new)
    atomic(root/'installer-migrated.json',{'preserved':['prepared/unified_game_mod/ai_assistant/'+n for n in preserved]})
    return preserved

if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True)
    args=parser.parse_args();print(json.dumps({'preserved_files':len(migrate(args.root))}))
