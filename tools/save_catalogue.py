"""Bounded read-only save discovery. File metadata never proves a live campaign."""
from pathlib import Path
from hashlib import sha256
import json
import os
import re
import zipfile
import codecs
import threading
import time
from functools import lru_cache

LIMIT = 512 * 1024
_SETTINGS_LOCK = threading.RLock()


def settings():
    try:
        value = json.loads(settings_file().read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def update_settings(changes):
    from install_manager import atomic
    import shutil
    from uuid import uuid4
    with _SETTINGS_LOCK:
        file = settings_file()
        if file.exists():
            try:
                previous = json.loads(file.read_text(encoding='utf-8'))
                if not isinstance(previous, dict):raise ValueError('Invalid settings')
            except (ValueError, UnicodeError):
                backup = file.parent/'backups'/('save-settings-'+uuid4().hex+'.json')
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(file, backup)
        data = settings()
        data.update(changes)
        atomic(file, data)


def save_filter(value):
    if not isinstance(value, str) or (value not in {'', 'unlinked'} and not re.fullmatch('[a-f0-9]{64}', value)):
        raise ValueError('Invalid history filter')
    update_settings({'last_filter': value})


def settings_file():
    from user_storage import profile_dir
    return profile_dir() / 'save-folders.json'


def configured_folders():
    try:
        data = json.loads(settings_file().read_text(encoding='utf-8'))
        if not isinstance(data,dict) or not isinstance(data.get('folders',[]),list):return []
        return [Path(p) for p in data.get('folders', []) if isinstance(p, str)]
    except (OSError, ValueError, TypeError):
        return []


def add_folder(path):
    from install_manager import atomic
    if not isinstance(path,str) or not path.strip():raise ValueError('Save folder is unavailable')
    folder = Path(path).resolve()
    if not folder.is_dir():
        raise ValueError('Save folder is unavailable')
    with _SETTINGS_LOCK:
        roots = list(dict.fromkeys([str(p) for p in configured_folders()] + [str(folder)]))
        update_settings({'folders': roots})


def roots():
    from unified_mod_setup import documents_directory
    from hoi4_country_names import _game_roots
    result = [documents_directory()]
    if os.name == 'nt':
        import ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.shell32.SHGetFolderPathW(None,5,None,0,buffer) == 0:
            result.append(Path(buffer.value)/'Paradox Interactive/Hearts of Iron IV')
    for home in (Path.home()/'Documents', Path.home()/'OneDrive/Documents'):
        result.append(home/'Paradox Interactive/Hearts of Iron IV')
    steam = [Path(os.getenv('ProgramFiles(x86)', 'C:/Program Files (x86)'))/'Steam', Path('D:/Steam')]
    for game in _game_roots():
        if game.is_dir():
            steam.append(game.parents[2])
            userdir = game/'userdir.txt'
            if userdir.is_file():
                try:value = userdir.read_text(encoding='utf-8-sig').strip()
                except (OSError,UnicodeError):value = ''
                if value:
                    result.append((game/value).resolve())
    for folder in steam:
        result.extend((folder/'userdata').glob('*/394360/remote'))
    result.extend(configured_folders())
    result.extend(Path(p) for p in os.getenv('EYES_SAVE_DIRS', '').split(os.pathsep) if p)
    # Directory mods occasionally ship separate test save folders.
    try:
        from validate_hoi4_gui import parse_mod_descriptor
        docs = documents_directory()
        enabled = json.loads((docs/'dlc_load.json').read_text(encoding='utf-8-sig')).get('enabled_mods', [])
        for name in enabled:
            d = parse_mod_descriptor((docs/name).read_text(encoding='utf-8-sig'))
            source = d.first('path')
            if isinstance(source, str):
                mod = docs/source.strip('"')
                result.extend([mod/'save games', mod/'savegames'])
    except (OSError, ValueError, TypeError):
        pass
    return list(dict.fromkeys(p.resolve() for p in result if p.is_dir()))


@lru_cache(maxsize=4096)
def _metadata(path, size, modified):
    file = Path(path)
    row = {'file_id': sha256(os.path.normcase(path).encode()).hexdigest(),
           'path': path, 'filename': file.name, 'name': file.stem,
           'size': size, 'date': None, 'player': None, 'format': 'unknown',
           'reason': 'unknown_format', 'authority': 'file_metadata_only'}
    try:
        with file.open('rb') as stream:
            prefix = stream.read(LIMIT)
        row['snapshot_id'] = sha256((row['file_id']+str(size)+str(modified)).encode()+prefix).hexdigest()
        texts = [prefix]
        if prefix.startswith(b'PK'):
            row['format'] = 'zip'
            texts = []
            with zipfile.ZipFile(file) as archive:
                for member in archive.infolist():
                    if member.filename.lower() in {'meta', 'metadata', 'gamestate'}:
                        with archive.open(member) as stream:
                            texts.append(stream.read(LIMIT))
        for raw in texts:
            if raw.startswith(b'HOI4bin') or b'\0' in raw[:100]:
                row['format'] = 'binary'
                if row['date'] is None and row['player'] is None:row['reason'] = 'binary_format'
                continue
            try:text = codecs.getincrementaldecoder('utf-8-sig')('strict').decode(raw,final=len(raw)<LIMIT)
            except UnicodeError:
                if row['date'] is None and row['player'] is None:row['reason'] = 'unsupported_encoding_or_binary'
                continue
            date = re.search(r'^(?:date|game_date)\s*=\s*"?(\d{1,4})\.(\d{1,2})\.(\d{1,2})(?:\.\d+)?"?\s*$', text, re.M)
            player = re.search(r'^(?:player|player_tag)\s*=\s*"?([A-Z][A-Z0-9]{2})"?\s*$', text, re.M)
            if date and 1 <= int(date[2]) <= 12 and 1 <= int(date[3]) <= 31:
                row['date'] = f'{int(date[3]):02d}.{int(date[2]):02d}.{int(date[1]):04d}'
            if player:
                row['player'] = player[1]
            if date or player or text.startswith('HOI4txt'):
                row['reason'] = None
                if row['format'] == 'unknown':
                    row['format'] = 'text'
        if row['reason'] == 'unknown_format' and size <= 4096 and len(texts) == 1:
            try:
                from validate_hoi4_gui import parse_mod_descriptor
                auxiliary = parse_mod_descriptor(texts[0].decode('utf-8-sig'))
                if len(auxiliary.entries) == 1 and auxiliary.entries[0][0] == 'achievement':
                    row['reason'] = 'not_a_campaign_save'
            except (ValueError, UnicodeError):
                pass
        after = file.stat()
        if after.st_size != size or after.st_mtime_ns != modified:
            row['reason'] = 'file_changed_retry'
    except PermissionError:
        row['reason'] = 'access_denied'
    except (zipfile.BadZipFile, RuntimeError):
        row['reason'] = 'damaged_archive'
    except UnicodeError:
        row['reason'] = 'unsupported_encoding_or_binary'
    except OSError:
        row['reason'] = 'file_unavailable'
    row['save_id'] = row['file_id']
    row.setdefault('snapshot_id', sha256((path+str(size)+str(modified)).encode()).hexdigest())
    return row


def scan(folders=None):
    folders = roots() if folders is None else [Path(p) for p in folders]
    found = {}; warnings = []; ignored = 0; deadline = time.monotonic()+8; visited = 0
    for folder in folders:
        def error(_):
            warnings.append('Some folders could not be read')
        for parent, directories, files in os.walk(folder, followlinks=False, onerror=error):
            visited += len(files)+len(directories)
            if visited > 100000 or time.monotonic() > deadline:
                return {'saves':list(found.values()),'warnings':warnings+['Scan incomplete; select a narrower folder'],'truncated':True}
            directories[:] = [n for n in directories if not (Path(parent)/n).is_symlink()
                               and not getattr(Path(parent)/n, 'is_junction', lambda: False)()]
            for name in files:
                path = Path(parent)/name
                if path.suffix.lower() != '.hoi4' or path.is_symlink():
                    continue
                try:
                    stat = path.stat(); canonical = str(path.resolve())
                    row = dict(_metadata(canonical, stat.st_size, stat.st_mtime_ns))
                    if row['reason'] == 'not_a_campaign_save':
                        ignored += 1
                        continue
                    found[os.path.normcase(canonical)] = row
                except OSError:
                    warnings.append('A save disappeared or could not be read')
                if len(found) >= 5000:
                    return {'saves': list(found.values()), 'warnings': warnings+['Scan limit reached'], 'truncated': True}
    return {'saves': sorted(found.values(), key=lambda r: (r['name'].casefold(), r['path'])),
            'warnings': warnings, 'truncated': False, 'ignored_auxiliary_files': ignored}


def resolve(save_id):
    matches = [row for row in scan()['saves'] if row['save_id'] == save_id]
    if len(matches) != 1:
        raise ValueError('Save changed or is unavailable; refresh the save list')
    if matches[0]['reason'] == 'file_changed_retry':
        raise ValueError('Save is being written; refresh after saving finishes')
    return matches[0]
