"""Per-user state paths and a one-time, read-only import of the previous installation."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime
import uuid

APP_ROOT = Path(__file__).resolve().parents[1]
DATABASE = 'eyes_experimental.sqlite3'


def profile_dir():
    override = os.getenv('EYES_PROFILE_DIR')
    if override:
        return Path(override).expanduser().resolve()
    base = os.getenv('LOCALAPPDATA') or os.getenv('XDG_DATA_HOME')
    return (Path(base) if base else Path.home()/'.local/share')/'HOI4-EYES'


def runtime_dir(root=APP_ROOT):
    root = Path(root).resolve()
    return profile_dir()/'runtime' if root == APP_ROOT else root/'runtime'


def copy_database(source, destination):
    with closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro', uri=True)) as reader:
        with closing(sqlite3.connect(destination)) as writer:
            reader.backup(writer)
            if writer.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                raise RuntimeError('Legacy database integrity check failed')


def legacy_import_allowed(root=APP_ROOT):
    marker=profile_dir()/'legacy-import.json'
    try:
        selected=Path(json.loads(marker.read_text(encoding='utf-8'))['source']).resolve()
    except (OSError,ValueError,TypeError,KeyError):
        return False
    return selected==(Path(root).resolve()/'runtime'/DATABASE).resolve()


def migrate_legacy(root=APP_ROOT):
    """Call after acquiring the Flask listening socket, while the old service is stopped."""
    root = Path(root).resolve()
    runtime = runtime_dir(root)
    runtime.mkdir(parents=True, exist_ok=True)
    source = root/'runtime'/DATABASE
    destination = runtime/DATABASE
    if destination.exists() or destination == source or not source.is_file() or not legacy_import_allowed(root):
        return None
    backup = runtime.parent/'backups'/('before_profile_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8])
    backup.mkdir(parents=True)
    copy_database(source, backup/DATABASE)
    for path in source.parent.glob('response.json.*'):
        if path.is_file() and path.suffix=='.json':
            shutil.copy2(path, backup/path.name)
            if not (runtime/path.name).exists():
                shutil.copy2(path, runtime/path.name)
    for name in ('response.json','prepared_player_pact.json'):
        path=source.parent/name
        if path.is_file():
            shutil.copy2(path,backup/name)
            if not (runtime/name).exists():
                shutil.copy2(path,runtime/name)
    legacy_config = root.parent/'hoi4_eyes.ini'
    new_config = runtime.parent/'hoi4_eyes.ini'
    if legacy_config.is_file():
        shutil.copy2(legacy_config,backup/'hoi4_eyes.ini')
        if not new_config.exists():
            shutil.copy2(legacy_config,new_config)
    staged = runtime/('.'+DATABASE+'.'+uuid.uuid4().hex)
    try:
        shutil.copy2(backup/DATABASE,staged)
        os.replace(staged,destination)
    finally:
        staged.unlink(missing_ok=True)
    (backup/'migration.json').write_text(json.dumps({'source':str(source),'destination':str(destination)}),encoding='utf-8')
    return backup
