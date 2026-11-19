"""Register the bundled unified mod with a launcher without copying user data."""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
from uuid import uuid4

from bridge_instance import bridge_instance_lock
from hub_control import atomic_json
from treaty_package_install import hoI4_running
from user_storage import APP_ROOT, profile_dir
from validate_hoi4_gui import discover_hoi4_install, parse_pdx

DESCRIPTOR = 'HOI4_EYES_Experimental.mod'
VERSION = '1.19.5.0'
ASSETS = ('descriptor.mod', 'common/on_actions/eyes_world_snapshot.txt',
          'common/scripted_effects/eyes_world_snapshot.txt',
          'common/on_actions/ai_assistant_gui_on_actions.txt')


def documents_directory():
    for candidate in (Path.home()/'Documents', Path.home()/'OneDrive/Documents'):
        path = candidate/'Paradox Interactive/Hearts of Iron IV'
        if path.is_dir():
            return path
    return Path.home()/'Documents/Paradox Interactive/Hearts of Iron IV'


def digest(data):
    return sha256(data).hexdigest()


def setup_manifest(game, documents, source):
    version = json.loads((game/'launcher-settings.json').read_text(encoding='utf-8-sig')).get('rawVersion')
    if not (game/'hoi4.exe').is_file() or version != VERSION:
        raise ValueError('game_version_unsupported')
    hashes = {}
    for name in ASSETS:
        file = (source/name).resolve()
        if not file.is_relative_to(source):
            raise ValueError('invalid_mod_asset')
        data = file.read_bytes()
        parse_pdx(data.decode('utf-8-sig'))
        hashes[name] = digest(data)
    try:
        from atlas_map_sources import active_map_files
        atlas = active_map_files(game, documents)
        playset = 'modified' if any(not path.is_relative_to(game) for path in atlas.values()) else 'vanilla_map'
    except (OSError, ValueError, TypeError):
        playset = 'unverified'
    return {'version':version,'assets':hashes,'playset':playset}


def change_setup(action, game, documents, *, root=APP_ROOT, profile=None, confirm=False):
    game, documents = game.resolve(), documents.resolve()
    source = (root/'prepared/unified_game_mod/ai_assistant').resolve()
    target_dir = documents/'mod'
    if (not documents.is_dir() or target_dir.resolve().parent != documents
            or target_dir.is_relative_to(game) or source.is_relative_to(target_dir)
            or any(c in source.as_posix() for c in ('"', '\n', '\r'))):
        raise ValueError('unsafe_mod_directory')
    target = target_dir/DESCRIPTOR
    journal = (profile or profile_dir())/'mod-setup'/digest(os.path.normcase(str(target)).encode())
    metadata_path = journal/'installation.json'
    def read_target():
        if target.is_symlink() or target_dir.is_symlink():
            raise ValueError('unsafe_mod_directory')
        return target.read_bytes() if target.is_file() else None
    report = setup_manifest(game, documents, source)
    report.update(target=str(target), source=str(source))
    if action == 'check':
        current = read_target()
        recorded = json.loads(metadata_path.read_text(encoding='utf-8')) if metadata_path.is_file() else None
        report['integrity'] = ('valid' if current and recorded and recorded.get('descriptor')==digest(current)
                               and recorded.get('assets')==report['assets']
                               else 'unmanaged_or_changed' if current else 'not_registered')
        return report
    if action not in ('install','remove') or not confirm or hoI4_running() is not False:
        raise ValueError('close_hoi4_and_confirm')
    with bridge_instance_lock(journal/'lock') as acquired:
        if not acquired or hoI4_running() is not False:
            raise ValueError('setup_busy_or_game_running')
        current = read_target()
        recorded = json.loads(metadata_path.read_text(encoding='utf-8')) if metadata_path.is_file() else {}
        expected = recorded.get('descriptor')
        if current and expected != digest(current):
            descriptor = parse_pdx(current.decode('utf-8-sig'))
            path = descriptor.first('path')
            # Existing official registrations of this exact project are safe to adopt on explicit install.
            if (action != 'install' or expected or descriptor.first('name') != '"HOI4-EYES"'
                    or not isinstance(path, str) or Path(path.strip('"')).resolve() != source):
                raise ValueError('descriptor_owned_by_someone_else')
        if action == 'remove' and (not current or not expected):
            raise ValueError('managed_descriptor_required')
        backup = journal/f'{uuid4().hex}.mod'
        if current:
            backup.write_bytes(current)
        if action == 'remove':
            atomic_json(journal/'last_removal.json', {'backup':str(backup),'removed_hash':expected})
            if read_target() != current or hoI4_running() is not False:
                raise ValueError('descriptor_changed_during_setup')
            target.unlink()
            return dict(report, integrity='removed', backup=str(backup))
        target_dir.mkdir(exist_ok=True)
        data = f'name="HOI4-EYES"\nsupported_version="1.19.5.*"\npath="{source.as_posix()}"\n'.encode('utf-8')
        temp = target_dir / f'.eyes-setup-{uuid4().hex}.tmp'
        try:
            temp.write_bytes(data)
            if read_target() != current or hoI4_running() is not False:
                raise ValueError('descriptor_changed_during_setup')
            os.replace(temp,target)
            try:
                if read_target() != data:
                    raise ValueError('descriptor_verification_failed')
                atomic_json(metadata_path, {'descriptor':digest(data),'assets':report['assets']})
            except (OSError, ValueError):
                if read_target() == data:
                    if current is None:
                        target.unlink()
                    else:
                        temp.write_bytes(current)
                        os.replace(temp,target)
                raise
        finally:
            temp.unlink(missing_ok=True)
    return dict(report, integrity='valid', backup=str(backup) if current else None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check','install','remove'))
    parser.add_argument('--game', type=Path, default=discover_hoi4_install())
    parser.add_argument('--documents', type=Path, default=documents_directory())
    parser.add_argument('--confirm', action='store_true')
    args = parser.parse_args()
    if args.game is None:
        parser.error('HOI4 was not found. Pass --game.')
    try:
        print(json.dumps(change_setup(args.action,args.game,args.documents,confirm=args.confirm),indent=2))
    except (ValueError,OSError) as error:
        parser.exit(1,f'{error}\n')


if __name__ == '__main__':
    main()
