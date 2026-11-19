"""Resolve the map files visible in a launcher-configured directory playset."""

from pathlib import Path, PurePosixPath
import json
import re


def active_map_files(game, documents, *, strategic_regions=False):
    game, documents = Path(game), Path(documents)
    paths = {}

    def overlay(root):
        for key in ('map/provinces.bmp', 'map/definition.csv'):
            path = root / key
            if path.is_file():
                paths[key] = path
        folders = ('history/states', 'map/strategicregions') if strategic_regions else ('history/states',)
        for folder in folders:
            for path in (root / folder).glob('*.txt'):
                paths[path.relative_to(root).as_posix()] = path

    overlay(game)
    config = documents / 'dlc_load.json'
    playset = json.loads(config.read_text(encoding='utf-8-sig')) if config.is_file() else {}
    enabled = playset.get('enabled_mods', []) if isinstance(playset, dict) else None
    if not isinstance(enabled, list) or any(not isinstance(entry, str) or not entry.strip() for entry in enabled):
        raise ValueError('invalid_atlas_playset')
    for entry in enabled:
        descriptor = (documents / entry).read_text(encoding='utf-8-sig')
        source = re.findall(r'^\s*(path|archive)\s*=\s*"([^"]+)"', descriptor, re.M)
        if len(source) != 1 or source[0][0] != 'path':
            raise ValueError('unsupported_atlas_mod_source')
        root = documents / source[0][1]
        if not root.is_dir():
            raise ValueError('missing_atlas_mod_source')
        internal = root / 'descriptor.mod'
        manifest = descriptor + ('\n' + internal.read_text(encoding='utf-8-sig') if internal.is_file() else '')
        for scope in re.findall(r'^\s*replace_path\s*=\s*"([^"]+)"', manifest, re.M):
            scope = PurePosixPath(scope.replace('\\', '/'))
            for key in list(paths):
                if PurePosixPath(key).is_relative_to(scope):
                    del paths[key]
        overlay(root)
    if not {'map/provinces.bmp', 'map/definition.csv'} <= paths.keys():
        raise ValueError('incomplete_atlas_map')
    return paths


MORE_PROVINCES_ID = "3313864050"


def more_provinces_preview_files(game):
    """Read just the installed map assets as a standalone, unverified Atlas preview."""
    game = Path(game)
    workshop = game.parents[1] / "workshop" / "content" / "394360" / MORE_PROVINCES_ID
    descriptor = (workshop / "descriptor.mod").read_text(encoding="utf-8-sig")
    if not re.search(rf'^\s*remote_file_id\s*=\s*"{MORE_PROVINCES_ID}"', descriptor, re.M):
        raise ValueError('unverified_more_provinces_source')
    paths = {key:workshop / key for key in ('map/provinces.bmp','map/definition.csv')}
    paths.update((path.relative_to(workshop).as_posix(),path)
                 for path in (workshop/'history/states').glob('*.txt'))
    if not all(path.is_file() for path in paths.values()):
        raise ValueError('incomplete_atlas_map')
    return paths
