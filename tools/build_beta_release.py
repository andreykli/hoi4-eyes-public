"""Build clean source distributions; never copy an installation wholesale.

No game calls, profile reads, installer calls or network access. The reviewed
manifest, rather than Git tracking alone, authorizes every distributed file.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = '0.9.0-beta.6'
MOD_NAME = 'HOI4-EYES'
PRIVATE_DEVELOPER_FILES = frozenset({
    'tools/developer_desktop.py', 'tools/desktop_acceptance.py',
    'tools/live_qa.py', 'tools/live_qa_scripts.py',
    'tools/release_acceptance.py', 'tools/release_acceptance_offline.py',
    'tools/release_smoke_checks.py', 'tools/static/live_qa.js',
    'tools/static/developer_checks.js',
})
MOD = 'prepared/unified_game_mod/ai_assistant/'
DOCS = ('README.md', 'README_RU.md', 'README_FR.md', 'LICENSE', 'NOTICE',
        'THIRD-PARTY-NOTICES.md', 'CHANGELOG.md', 'docs/BETA_GUIDE.md',
        'docs/BETA_ACCEPTANCE.md', 'docs/WORKSHOP.md', 'docs/LICENSES.md', 'docs/BETA_REPORT_RU.md',
        'CREDITS.md', 'ATTRIBUTION.md', 'THIRD_PARTY_NOTICES.md',
        'docs/GITHUB_PUBLICATION.md', 'docs/GITHUB_RELEASE.md',
        'docs/WORKSHOP_PUBLICATION.md', 'docs/PUBLISH_STEPS_RU.md', 'docs/MODEL_LICENSE.md',
        'docs/STEAM_CHANGELOG_EN.txt', 'docs/RELEASE_CLEANUP_RU.md')


def tracked(root):
    git = root/'.tools/mingit/cmd/git.exe'
    command = str(git) if git.is_file() else 'git'
    result = subprocess.run([command, 'ls-files', '-z'], cwd=root, check=True,
                            capture_output=True)
    return result.stdout.decode('utf-8').strip('\0').split('\0')


def safe_path(root, name):
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(p in ('..', '.') or ':' in p or '\\' in p for p in path.parts):
        raise ValueError('unsafe_release_path')
    selected = root/name
    if any(parent.is_symlink() for parent in (selected, *selected.parents)):
        raise ValueError('release_symlink')
    if not selected.resolve().is_relative_to(root.resolve()):
        raise ValueError('release_path_escape')
    return selected


def payloads(root, inventory):
    policy = json.loads((root/'release/files.json').read_text(encoding='utf-8'))
    allowed = set(policy['mod_files'])
    if not allowed or any(not name.startswith(MOD) for name in allowed):
        raise ValueError('invalid_mod_manifest')
    app_names = set(policy['application_files']) | set(DOCS)
    if app_names & PRIVATE_DEVELOPER_FILES:
        raise ValueError('developer_file_in_public_manifest')
    # Any newly added file must be reviewed, including files tracked by Git.
    required = app_names | allowed
    for name in required:
        path = PurePosixPath(name)
        if (set(path.parts) & {'.git','runtime','backups','control_copies','fixtures','flags','__pycache__'}
                or path.name.startswith(('test_', '.env', 'eyes_agreement_', 'eyes_cm_',
                                          'eyes_treaty_monitor_', 'eyes_territory_', 'eyes_xor_capital_'))
                or path.suffix.lower() in {'.sqlite', '.sqlite3', '.db', '.hoi4', '.zip', '.bak', '.token'}):
            raise ValueError('private_or_test_file_in_manifest: '+name)
    missing = required - set(inventory)
    if missing:
        raise ValueError('untracked_release_files: '+', '.join(sorted(missing)))
    app, workshop = {}, {}
    for name in sorted(required):
        data = safe_path(root, name).read_bytes()
        app[name] = data
        if name in allowed:
            workshop[name[len(MOD):]] = data
    # Public builds contain no grant loader/override and no QA executors.
    app['tools/developer_access.py'] = b'''"""Public build: internal developer operations are not distributed."""
PUBLIC_BUILD = True
def enabled(root=None):
    return False
def require(root=None):
    raise PermissionError('developer_access_required')
'''
    import re
    template = 'tools/templates/desktop.html'
    app[template] = re.sub(r'{% if developer_mode %}.*?{% endif %}', '',
                          app[template].decode('utf-8'), flags=re.S).encode()
    # Launcher-generated paths and remote file IDs must never enter archives.
    from hoi4_compatibility import DEFAULT_VERSION, supported_version
    if policy['version'] != VERSION:
        raise ValueError('release_version_mismatch')
    descriptor = (f'name="{MOD_NAME}"\nsupported_version="{supported_version(DEFAULT_VERSION)}"\n'
                  f'version="{VERSION}"\n').encode()
    app[MOD+'descriptor.mod'] = workshop['descriptor.mod'] = descriptor
    # Legacy internal marker used by launch controllers; never an installed
    # launcher descriptor and deliberately contains no machine-specific path.
    app['HOI4_EYES_Experimental.mod'] = descriptor
    for name in ('LICENSE', 'NOTICE', 'THIRD-PARTY-NOTICES.md', 'THIRD_PARTY_NOTICES.md',
                 'CREDITS.md', 'ATTRIBUTION.md', 'CHANGELOG.md', 'docs/LICENSES.md', 'docs/MODEL_LICENSE.md'):
        workshop[name] = app[name]
    workshop['README.md'] = app['docs/WORKSHOP_PUBLICATION.md']
    workshop['THIRD-PARTY-NOTICES.md'] = app['THIRD-PARTY-NOTICES.md'].replace(
        b'prepared/unified_game_mod/ai_assistant/THIRD_PARTY_NOTICES.md', b'THIRD_PARTY_NOTICES.md')
    workshop['THIRD_PARTY_NOTICES.md'] = workshop['THIRD-PARTY-NOTICES.md']
    # Linked license/provenance files must exist even in the mod-only archive.
    for name, data in app.items():
        if name.startswith(('tools/voices_history/', 'tools/third_party/')):
            if PurePosixPath(name).name in ('LICENSE', 'NOTICE', 'ORIGIN.md', 'ORIGIN.json', 'README.md', 'source_manifest.json'):
                workshop[name] = data
    return {'windows-source': app, 'workshop': workshop}


def write_archive(path, files, revision):
    entries = {name: {'sha256': sha256(data).hexdigest(), 'bytes': len(data)}
               for name, data in sorted(files.items())}
    manifest = json.dumps({'version':VERSION, 'revision':revision,
                          'gameplay_verified_by_build':False, 'files':entries},
                         ensure_ascii=False, indent=2).encode('utf-8')
    with zipfile.ZipFile(path, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
        archive.writestr('RELEASE-MANIFEST.json', manifest)
    verify_archive(path)


def verify_archive(path):
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise ValueError('archive_crc_failed')
        manifest = json.loads(archive.read('RELEASE-MANIFEST.json'))
        names = archive.namelist()
        if len(names) != len(set(names)) or set(names) != set(manifest['files']) | {'RELEASE-MANIFEST.json'}:
            raise ValueError('archive_inventory_mismatch')
        for name, expected in manifest['files'].items():
            data = archive.read(name)
            if sha256(data).hexdigest() != expected['sha256'] or len(data) != expected['bytes']:
                raise ValueError('archive_digest_failed')
    return manifest


def build(root, output):
    root, output = root.resolve(), output.resolve()
    if not output.is_relative_to(root/'runtime/releases'):
        raise ValueError('release_output_must_be_under_runtime_releases')
    inventory = tracked(root)
    distributions = payloads(root, inventory)
    git = root/'.tools/mingit/cmd/git.exe'
    command = str(git) if git.is_file() else 'git'
    dirty = subprocess.check_output([command, 'status', '--porcelain', '--untracked-files=no'], cwd=root)
    if dirty.strip():
        raise ValueError('commit_reviewed_changes_before_release_build')
    revision = subprocess.check_output([command, 'rev-parse', 'HEAD'], cwd=root).decode().strip()
    output.mkdir(parents=True, exist_ok=False)
    report = {}
    for kind, files in distributions.items():
        path = output/f'HOI4-EYES-{VERSION}-{kind}.zip'
        write_archive(path, files, revision)
        report[kind] = {'file':path.name, 'files':len(files), 'bytes':path.stat().st_size,
                        'sha256':sha256(path.read_bytes()).hexdigest()}
    (output/'checksums.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(ROOT, args.output), indent=2))
