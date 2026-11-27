"""One EYES registration; Steam owns delivery, the app uses the enabled game mod."""
import json
from pathlib import Path
from uuid import uuid4
import shutil
import sqlite3
from contextlib import closing

ITEM='3842411221'
CORE=('interface/ai_assistant_diplomacy_gui.gui','common/scripted_guis/ai_assistant_diplomacy_gui.txt')

def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def cache_candidates(registered):
    from validate_hoi4_gui import discover_hoi4_install
    result=[Path(registered)]
    game=discover_hoi4_install()
    if game is not None:
        game=Path(game)
        if game.parent.name.casefold()=='common':
            result.append(game.parent.parent/'workshop/content/394360'/ITEM)
    return list(dict.fromkeys(result))

def workshop_registration(documents):
    from validate_hoi4_gui import parse_mod_descriptor
    from workshop_update import registered_workshop_id,version_key
    for path in sorted((Path(documents)/'mod').glob('*.mod')):
        data=parse_mod_descriptor(path.read_text(encoding='utf-8-sig'))
        registered=Path(str(data.first('path') or '').strip('"'))
        try:item=registered_workshop_id(path,data)
        except FileNotFoundError:
            item=ITEM if path.name=='ugc_'+ITEM+'.mod' and registered.parts[-2:]==('394360',ITEM) else ''
        if item!=ITEM:continue
        verified=[]
        for folder in cache_candidates(registered):
            folder=folder.resolve()
            try:
                meta=read(folder/'PUBLICATION.json')
                protocol=read(folder/'release/installation_protocol.json')
            except FileNotFoundError:continue
            if (folder.parts[-2:]!=('394360',ITEM) or meta.get('project')!='HOI4-EYES'
                    or meta.get('author')!='NightPulse' or meta.get('github_url')!='https://github.com/andreykli/hoi4-eyes-public'):
                raise ValueError('unknown_workshop_identity')
            verified.append((version_key(meta['version']),int(protocol.get('installer_revision',0)),
                (folder/'release/installation_protocol.json').stat().st_mtime_ns,folder))
        if not verified:raise ValueError('official_workshop_files_unavailable')
        return path,max(verified,key=lambda row:row[:3])[3]
    return None

def registration_paths(documents):
    from validate_hoi4_gui import parse_mod_descriptor
    paths=[]
    for p in (Path(documents)/'mod').glob('*.mod'):
        data=parse_mod_descriptor(p.read_text(encoding='utf-8-sig'))
        title=str(data.first('name') or '').strip('"')
        if title=='HOI4-EYES' or title.startswith('HOI4-EYES 0.') or p.name=='ugc_'+ITEM+'.mod':paths.append(p)
    return paths

def verify_workshop(app,folder):
    expected=read(Path(app)/'release/installation_protocol.json')
    if read(folder/'release/installation_protocol.json')!=expected:
        raise ValueError('update_workshop_before_setup')
    for name in CORE:
        own=Path(app)/'prepared/unified_game_mod/ai_assistant'/name
        if own.is_file() and own.read_bytes()!=(folder/name).read_bytes():
            raise ValueError('workshop_game_files_differ_from_application')

def active_source(app,documents):
    from validate_hoi4_gui import parse_mod_descriptor
    app,documents=Path(app),Path(documents)
    managed=(app/'prepared/unified_game_mod/ai_assistant').resolve()
    selected=[]
    for entry in read(documents/'dlc_load.json').get('enabled_mods',[]):
        relative=Path(entry)
        if relative.is_absolute() or '..' in relative.parts:raise ValueError('unsafe_playset_entry')
        p=documents/relative
        if not p.is_file():continue
        data=parse_mod_descriptor(p.read_text(encoding='utf-8-sig'))
        title=str(data.first('name') or '').strip('"')
        folder=Path(str(data.first('path') or '').strip('"'))
        if not folder.is_absolute():folder=documents/folder
        folder=folder.resolve()
        if folder==managed or title.startswith('HOI4-EYES'):selected.append((p,folder))
    if len(selected)!=1:raise ValueError('Enable only one HOI4-EYES mod in the launcher.')
    descriptor,folder=selected[0]
    if folder==managed:return folder
    official=workshop_registration(documents)
    if official and official[1]==folder:
        verify_workshop(app,folder);return folder
    raise ValueError('Unrecognized EYES game mod. Run Update / repair.')

def consolidate(app,documents,profile,*,consent=False):
    from treaty_package_install import hoI4_running
    from install_manager import require_launcher_closed
    from installer_lifecycle import identify_source
    from installer_mod_migration import copy_owned
    from validate_hoi4_gui import parse_mod_descriptor
    if not consent or hoI4_running() is not False:raise PermissionError('close_game_and_confirm')
    require_launcher_closed()
    app,documents,profile=Path(app),Path(documents),Path(profile)
    official=workshop_registration(documents)
    if not official:raise ValueError('official_workshop_registration_required')
    primary,folder=official;verify_workshop(app,folder)
    duplicates=[]
    for p in registration_paths(documents):
        if p==primary:continue
        data=parse_mod_descriptor(p.read_text(encoding='utf-8-sig'))
        old=Path(str(data.first('path') or '').strip('"'))
        if not old.is_absolute():old=documents/old
        if old.resolve()!=folder:
            if old.parts[-3:]!=('prepared','unified_game_mod','ai_assistant'):raise ValueError('unknown_eyes_copy_preserved')
            identify_source(old.parents[2])
        duplicates.append((p,old))
    backup=profile/'backups'/('single-mod-'+uuid4().hex);backup.mkdir(parents=True)
    load=documents/'dlc_load.json';paths=[primary,load,*[p for p,_ in duplicates]]
    before={p:p.read_bytes() if p.exists() else None for p in paths}
    for i,(p,data) in enumerate(before.items()):
        if data is not None:(backup/(str(i)+'-'+p.name)).write_bytes(data)
    database=documents/'launcher-v2.sqlite'
    if database.exists():
        from user_storage import copy_database
        copy_database(database,backup/'launcher-v2.sqlite')
    try:
        primary_data=parse_mod_descriptor(primary.read_text(encoding='utf-8-sig'))
        old_primary=Path(str(primary_data.first('path') or '').strip('"'))
        if old_primary.is_dir() and old_primary.resolve()!=folder:
            copy_owned(old_primary,folder)
        for _,old in duplicates:
            if old.resolve()!=folder:copy_owned(old,folder)
        text=primary.read_text(encoding='utf-8-sig')
        import re
        text=re.sub(r'(?m)^\s*name\s*=\s*"[^"\r\n]*"','name="HOI4-EYES"',text,count=1)
        text=re.sub(r'(?m)^\s*path\s*=\s*"[^"\r\n]*"',lambda _: 'path="'+folder.as_posix()+'"',text,count=1)
        primary.write_bytes(text.encode('utf-8'))
        config=read(load) if load.exists() else {'enabled_mods':[]}
        removed={'mod/'+p.name for p,_ in duplicates};chosen='mod/'+primary.name
        config['enabled_mods']=[n for n in config.get('enabled_mods',[]) if n.replace('\\','/') not in removed|{chosen}]+[chosen]
        from install_manager import atomic
        atomic(load,config)
        for p,_ in duplicates:p.unlink()
        if database.exists():
            with closing(sqlite3.connect(database)) as db:
                db.execute('PRAGMA foreign_keys=ON')
                rows=db.execute('SELECT id,name,steamId,gameRegistryId FROM mods').fetchall()
                selected=[row for row in rows if str(row[2] or '')==ITEM]
                if len(selected)!=1:raise ValueError('launcher_workshop_record_requires_review')
                keep=selected[0][0]
                for identifier,title,steam,registry in rows:
                    if identifier==keep:continue
                    if (not steam and (str(title or '')=='HOI4-EYES' or str(title or '').startswith('HOI4-EYES '))):
                        db.execute('DELETE FROM playsets_mods WHERE modId=?',(identifier,))
                        db.execute('DELETE FROM mods WHERE id=?',(identifier,))
                db.execute('UPDATE mods SET name=?,displayName=?,dirPath=? WHERE id=?',('HOI4-EYES','HOI4-EYES',str(folder),keep))
                active=db.execute('SELECT id FROM playsets WHERE isActive=1').fetchall()
                for (playset,) in active:
                    if db.execute('SELECT 1 FROM playsets_mods WHERE playsetId=? AND modId=?',(playset,keep)).fetchone():
                        db.execute('UPDATE playsets_mods SET enabled=1 WHERE playsetId=? AND modId=?',(playset,keep))
                    else:db.execute('INSERT INTO playsets_mods(playsetId,modId,enabled,position) VALUES(?,?,1,0)',(playset,keep))
                db.commit()
    except:
        for p,data in before.items():
            if data is None:p.unlink(missing_ok=True)
            else:p.write_bytes(data)
        raise
    result={'primary':str(primary),'source':str(folder),'removed_registrations':[p.name for p,_ in duplicates],
            'backup':str(backup),'old_source_folders_preserved':True}
    (backup/'consolidation.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    return result
