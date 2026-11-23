"""Owned installer lifecycle. Imports are read-only; all mutations need consent.

The uninstall authority deliberately excludes profiles, game saves, shared
runtimes, models, old source folders and the cached recovery installer.
"""
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

from install_manager import OWNER, child, atomic, digest, load_plan, owned_root, active

UNINSTALL_KEY = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\HOI4-EYES'


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def identify_source(root):
    """Require the exported product identity AND intact distributed source files."""
    root = Path(root).resolve()
    publication = read_json(root/'PUBLICATION.json')
    if (publication.get('project') != 'HOI4-EYES' or publication.get('author') != 'NightPulse'
            or publication.get('github_url') != 'https://github.com/andreykli/hoi4-eyes-public'):
        raise ValueError('unrecognized_previous_source')
    manifest = read_json(root/'SOURCE-MANIFEST.json')
    for name, row in manifest['files'].items():
        file = child(root, name)
        if not file.is_file() or digest(file) != row['sha256']:
            raise ValueError('previous_source_modified_requires_review')
    return {'root': str(root), 'version': publication['version'], 'verified': True}


def discover_previous(documents, extra=(), managed_root=None):
    """Bounded discovery: registered mods, the managed root and user-selected folders."""
    from validate_hoi4_gui import parse_pdx
    candidates = list(extra)
    registrations = []
    for descriptor in sorted((Path(documents)/'mod').glob('*.mod')):
        try:
            data = parse_pdx(descriptor.read_text(encoding='utf-8-sig'))
            name = str(data.first('name') or '').strip('"')
            if not (name == 'HOI4-EYES' or name.startswith('HOI4-EYES 0.')):
                continue  # Acceptance and other people's mods are never adopted.
            path = data.first('path')
            if not isinstance(path, str):
                continue
            mod = Path(path.strip('"'))
            if not mod.is_absolute():
                mod = Path(documents)/mod
            if mod.parts[-3:] == ('prepared', 'unified_game_mod', 'ai_assistant'):
                candidates.append(mod.parents[2])
            registrations.append(str(descriptor))
        except (OSError, ValueError, TypeError):
            continue
    if managed_root:
        try:
            candidates.append(active(managed_root))
        except (OSError, ValueError, KeyError):
            pass
    rows = []
    for root in dict.fromkeys(str(Path(p).resolve()) for p in candidates):
        try:
            rows.append(identify_source(root))
        except (OSError, ValueError, KeyError, TypeError):
            rows.append({'root': root, 'verified': False, 'needs_review': True})
    return {'installations': rows, 'registrations': registrations}


def import_legacy_profile(old, profile, *, consent=False):
    """Explicit one-time import only into an empty profile; retain the source."""
    if not consent:
        raise PermissionError('legacy_import_consent_required')
    identify_source(old)
    old = Path(old); profile = Path(profile)
    source = old/'runtime'
    if source.is_symlink() or getattr(source,'is_junction',lambda:False)():
        raise ValueError('legacy_runtime_reparse_path')
    database = source/'eyes_experimental.sqlite3'
    if not database.is_file():
        return {'imported': False, 'reason': 'no_legacy_database'}
    destination = profile/'runtime'
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('existing_profile_preserved_choose_manually')
    # Preflight every path before the first copy. Never traverse junctions.
    files = []
    for folder, dirs, names in os.walk(source, followlinks=False):
        for name in dirs + names:
            path = Path(folder)/name
            child(source, path.relative_to(source).as_posix())
        for name in names:
            path = Path(folder)/name
            if path.name.endswith(('-wal', '-shm', '.lock')):
                continue
            files.append(path)
    profile.mkdir(parents=True, exist_ok=True)
    backup = child(profile, 'backups/legacy-import-'+uuid4().hex)
    staging = backup/'runtime'; staging.mkdir(parents=True)
    from user_storage import copy_database
    for path in files:
        target = child(staging, path.relative_to(source).as_posix())
        target.parent.mkdir(parents=True, exist_ok=True)
        if path == database:
            copy_database(path, target)  # SQLite online backup, including WAL contents.
        else:
            shutil.copyfile(path, target)
    # A disk/copy failure must not expose a half-imported live profile.
    pending=child(profile,'.legacy-import-'+uuid4().hex)
    shutil.copytree(staging,pending)
    if destination.exists():
        destination.rmdir()  # Only an empty directory, never a recursive delete.
    os.replace(pending,destination)
    config = old.parent/'hoi4_eyes.ini'
    if config.is_file() and not (profile/'hoi4_eyes.ini').exists():
        shutil.copyfile(config, profile/'hoi4_eyes.ini')
    atomic(backup/'migration.json', {'source': str(source.resolve()), 'source_preserved': True})
    return {'imported': True, 'backup': str(backup)}


def registration_controls(documents, profile):
    from unified_mod_setup import DESCRIPTOR, digest as hash_bytes
    descriptor = Path(documents)/'mod'/DESCRIPTOR
    key = hash_bytes(os.path.normcase(str(descriptor.resolve())).encode())
    return [descriptor, Path(profile)/'mod-setup'/key/'installation.json', Path(documents)/'dlc_load.json']


def adopt_registration(documents, profile, *, consent=False):
    """Adopt only an intact older public source, never an arbitrary same-name mod."""
    if not consent:
        raise PermissionError('registration_consent_required')
    from validate_hoi4_gui import parse_pdx
    descriptor, journal, _ = registration_controls(documents, profile)
    if not descriptor.exists():
        return False
    raw = descriptor.read_bytes(); expected = digest(descriptor)
    if journal.exists():
        if read_json(journal).get('descriptor') != expected:
            raise ValueError('registration_changed_requires_review')
        return False
    data = parse_pdx(raw.decode('utf-8-sig'))
    name = str(data.first('name') or '').strip('"')
    mod = Path(str(data.first('path') or '').strip('"'))
    if (not mod.is_absolute() or mod.parts[-3:] != ('prepared','unified_game_mod','ai_assistant')
            or not (name == 'HOI4-EYES' or name.startswith('HOI4-EYES 0.'))):
        raise ValueError('unmanaged_registration_requires_review')
    identify_source(mod.parents[2])
    journal.parent.mkdir(parents=True, exist_ok=True)
    atomic(journal, {'descriptor': expected, 'assets': {}, 'adopted_public_source': True})
    return True


def select_single_mod(documents, *, consent=False):
    """Change the enabled playset only; retain every .mod file and foreign mod."""
    if not consent:
        raise PermissionError('playset_consent_required')
    from unified_mod_setup import DESCRIPTOR
    from validate_hoi4_gui import parse_pdx
    documents = Path(documents); load = documents/'dlc_load.json'
    data = read_json(load) if load.exists() else {'enabled_mods': []}
    previous = load.read_bytes() if load.exists() else None
    enabled = data.get('enabled_mods')
    if not isinstance(enabled, list) or not all(isinstance(n, str) for n in enabled):
        raise ValueError('unknown_playset_format')
    result = []; selected = 'mod/'+DESCRIPTOR
    for name in enabled:
        path = child(documents, name.replace('\\','/'))
        if name.replace('\\','/') == selected:
            continue
        if not path.is_file():
            result.append(name); continue
        descriptor = parse_pdx(path.read_text(encoding='utf-8-sig'))
        title = str(descriptor.first('name') or '').strip('"')
        item = str(descriptor.first('remote_file_id') or '').strip('"')
        if item == '3842411221':
            continue
        if title == 'HOI4-EYES' or title.startswith('HOI4-EYES 0.'):
            mod = Path(str(descriptor.first('path') or '').strip('"'))
            if mod.parts[-3:] != ('prepared','unified_game_mod','ai_assistant'):
                raise ValueError('duplicate_mod_requires_review')
            identify_source(mod.parents[2]); continue
        if title.startswith('HOI4-EYES - '):
            raise ValueError('disable_acceptance_in_launcher_before_install')
        result.append(name)
    result.append(selected); data['enabled_mods'] = result
    canonical = child(documents, selected)
    if not canonical.is_file():raise ValueError('install_local_mod_before_selecting_playset')
    selected_data=parse_pdx(canonical.read_text(encoding='utf-8-sig'))
    title=str(selected_data.first('name') or '').strip('"')
    if not (title=='HOI4-EYES' or title.startswith('HOI4-EYES 0.')):
        raise ValueError('foreign_canonical_registration')
    if (load.read_bytes() if load.exists() else None) != previous:
        raise ValueError('playset_changed_during_setup')
    atomic(load, data)
    return result


def cache_setup(bundle, root, *, consent=False):
    if not consent:
        raise PermissionError('installation_consent_required')
    plan, identity = load_plan(bundle)
    root = owned_root(root)
    # Keep paths short on old Windows tooling. The full identity remains in the
    # embedded plan; any shortened-name collision is rejected, never overwritten.
    target = child(root, 'setup-cache/'+identity[:16])
    names = [*plan['files'], '_eyes_setup/plan.json', 'Install HOI4-EYES.exe']
    for name in names:
        source = child(bundle, name); dest = child(target, name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and digest(dest) != digest(source):
            raise ValueError('cached_setup_modified')
        if not dest.exists():
            shutil.copyfile(source, dest)
    load_plan(target)
    return target


def installed_apps(root, cache, version, *, consent=False, registry=None, remove=False):
    """Per-user registration only. The native bootstrap opens a confirmation UI."""
    if not consent:
        raise PermissionError('installed_apps_consent_required')
    root = owned_root(root); cache = Path(cache).resolve()
    if not cache.is_relative_to(root/'setup-cache'):
        raise ValueError('foreign_recovery_installer')
    if registry is None:
        import winreg as registry
    try:
        existing=registry.OpenKey(registry.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except FileNotFoundError:
        existing=None
    if existing is None:
        if remove:return
    else:
        with existing as key:
            try:
                owner = registry.QueryValueEx(key, 'EYESOwner')[0]
                location = registry.QueryValueEx(key, 'InstallLocation')[0]
            except FileNotFoundError as error:
                raise ValueError('unmanaged_installed_apps_entry_requires_review') from error
            if owner != OWNER or Path(location).resolve() != root:
                raise ValueError('foreign_installed_apps_entry')
    if remove:
        registry.DeleteKey(registry.HKEY_CURRENT_USER, UNINSTALL_KEY)
        return
    values = {'DisplayName': 'HOI4-EYES', 'DisplayVersion': version, 'Publisher': 'NightPulse',
              'InstallLocation': str(root), 'EYESOwner': OWNER,
              'URLInfoAbout': 'https://github.com/andreykli/hoi4-eyes-public',
              'UninstallString': '"'+str(cache/'Install HOI4-EYES.exe')+'" --uninstall',
              'ModifyPath': '"'+str(cache/'Install HOI4-EYES.exe')+'"'}
    with registry.CreateKeyEx(registry.HKEY_CURRENT_USER, UNINSTALL_KEY, 0, registry.KEY_WRITE) as key:
        for name, value in values.items():
            registry.SetValueEx(key, name, 0, registry.REG_SZ, value)
        registry.SetValueEx(key, 'NoRepair', 0, registry.REG_DWORD, 0)


class UpdateTransaction:
    """Persisted compare-and-swap rollback of a small explicit control-file set.

    Profiles and SQLite must NEVER be passed here. New versions are staged
    separately, so rollback restores pointers without downgrading a database.
    """
    def __init__(self, root, paths):
        self.root = owned_root(root)
        self.folder = child(self.root, 'transactions/'+uuid4().hex)
        self.folder.mkdir(parents=True)
        self.rows = []
        for index, path in enumerate(paths):
            path = Path(path).absolute()
            if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
                raise ValueError('unsafe_transaction_path')
            if (path.name not in {'installation.json','dlc_load.json','Start HOI4-EYES.bat'}
                    and path.suffix.lower() != '.mod'):
                raise ValueError('not_an_installer_control_file')
            before = path.read_bytes() if path.exists() else None
            if before is not None:
                (self.folder/str(index)).write_bytes(before)
            self.rows.append({'path': str(path), 'before': before is not None,
                              'before_sha256': digest(path) if before is not None else 'absent','after': None})
        self.save('started')

    def save(self, status):
        atomic(self.folder/'transaction.json', {'owner': OWNER, 'status': status, 'files': self.rows})

    def checkpoint(self, paths=None):
        selected=None if paths is None else {str(Path(p).absolute()) for p in paths}
        for row in self.rows:
            if selected is not None and row['path'] not in selected:continue
            path = Path(row['path'])
            row['after'] = digest(path) if path.is_file() else 'absent'
        self.save('pending')

    def rollback(self):
        conflicts = []
        for index, row in enumerate(self.rows):
            path = Path(row['path'])
            now = digest(path) if path.is_file() else 'absent'
            if row['after'] is None:
                if now != row['before_sha256']:
                    conflicts.append(str(path))
                continue
            if now != row['after']:
                conflicts.append(str(path)); continue
            if row['before']:
                if digest(self.folder/str(index)) != row['before_sha256']:
                    conflicts.append(str(path)); continue
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((self.folder/str(index)).read_bytes())
            elif path.exists():
                path.unlink()
        self.save('needs_review' if conflicts else 'rolled_back')
        if conflicts:
            raise ValueError('rollback_conflict_preserved')

    def commit(self):
        self.save('committed')


def recover_pending(root, allowed_paths, *, consent=False, game_closed=False):
    if not consent or not game_closed:
        raise PermissionError('recovery_consent_and_closed_game_required')
    root=owned_root(root);allowed={str(Path(p).absolute()) for p in allowed_paths};recovered=[]
    for record in sorted((root/'transactions').glob('*/transaction.json')):
        child(root,record.relative_to(root).as_posix());data=read_json(record)
        if data.get('owner')!=OWNER:raise ValueError('unknown_recovery_record')
        if data.get('status') not in {'started','pending','needs_review'}:continue
        rows=data['files']
        if not all(row.get('path') in allowed for row in rows):
            raise ValueError('foreign_recovery_control_file')
        transaction=object.__new__(UpdateTransaction)
        transaction.root=root;transaction.folder=record.parent;transaction.rows=rows
        transaction.rollback();recovered.append(str(record))
    return recovered
