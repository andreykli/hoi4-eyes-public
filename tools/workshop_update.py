"""Read-only startup compatibility check. Never execute downloaded update code.

Workshop delivery is not an Authenticode signature. Until a trusted signing
identity is provisioned, the application may OFFER the verified folder, but
cannot silently execute a newly downloaded bootstrap.
"""
import json
from pathlib import Path
import re


def version_key(value):
    match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)(?:-beta\.(\d+))?', value)
    if not match:
        raise ValueError('unknown_release_version')
    major, minor, patch, beta = match.groups()
    return int(major), int(minor), int(patch), int(beta) if beta else 1000000


def inspect_update(app, workshop):
    app, workshop = Path(app), Path(workshop)
    current = json.loads((app/'release/installation_protocol.json').read_text(encoding='utf-8'))
    if workshop.name != '3842411221' or workshop.parent.name != '394360':
        raise ValueError('unexpected_workshop_item')
    metadata = json.loads((workshop/'PUBLICATION.json').read_text(encoding='utf-8-sig'))
    if (metadata.get('project') != 'HOI4-EYES' or metadata.get('author') != 'NightPulse'
            or metadata.get('github_url') != 'https://github.com/andreykli/hoi4-eyes-public'):
        raise ValueError('unknown_workshop_identity')
    newer = version_key(metadata['version']) > version_key(current['version'])
    incoming_file = workshop/'release/installation_protocol.json'
    incoming = json.loads(incoming_file.read_text(encoding='utf-8')) if incoming_file.is_file() else None
    # Absence is allowed only for the exact older published version. A new
    # unknown protocol cannot start services which might write to the profile.
    compatible = (incoming is not None and incoming.get('protocol') == current['protocol'])
    if incoming is None:
        compatible = metadata['version'] == current['version']
    if incoming and incoming.get('version') != metadata['version']:
        compatible = False
    return {'newer': newer, 'compatible': compatible, 'version': metadata['version'],
            'folder': str(workshop.resolve()), 'automatic_execution_allowed': False}


def startup_check(app, documents):
    """Inspect the actual enabled playset; failure is explicit, never a guessed match."""
    app, documents = Path(app), Path(documents)
    if not (app/'release/installation_protocol.json').is_file():
        return {'status': 'legacy_build', 'blocked': False, 'updates': []}
    if app.parent.name=='versions':
        for record in (app.parent.parent/'transactions').glob('*/transaction.json'):
            if json.loads(record.read_text(encoding='utf-8')).get('status') in {'started','pending','needs_review'}:
                return {'status':'interrupted_update_requires_repair','blocked':True,'updates':[]}
    from validate_hoi4_gui import parse_pdx
    load = documents/'dlc_load.json'
    if not load.is_file():
        return {'status': 'no_playset', 'blocked': True, 'updates': []}
    enabled = json.loads(load.read_text(encoding='utf-8-sig')).get('enabled_mods', [])
    updates = []; copies = []
    # Steam may already have updated the subscribed files while the local
    # installed mod remains enabled. Inspect inactive Workshop registrations too.
    seen = set()
    for descriptor in (documents/'mod').glob('*.mod'):
        data = parse_pdx(descriptor.read_text(encoding='utf-8-sig'))
        if str(data.first('remote_file_id') or '').strip('"') == '3842411221':
            path = str(data.first('path') or '').strip('"')
            if path:
                updates.append(inspect_update(app, Path(path))); seen.add(str(Path(path).resolve()))
    for name in enabled:
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('unsafe_playset_entry')
        descriptor = documents/relative
        if not descriptor.is_file():
            continue
        data = parse_pdx(descriptor.read_text(encoding='utf-8-sig'))
        title = str(data.first('name') or '').strip('"')
        item = str(data.first('remote_file_id') or '').strip('"')
        if not (title.startswith('HOI4-EYES') or item == '3842411221'):
            continue
        copies.append(name)
        path = str(data.first('path') or '').strip('"')
        if item == '3842411221' and path:
            if str(Path(path).resolve()) not in seen:
                updates.append(inspect_update(app, Path(path)))
        elif path:
            mod = Path(path)
            protocol = mod/'release/installation_protocol.json'
            expected = json.loads((app/'release/installation_protocol.json').read_text(encoding='utf-8'))
            actual = json.loads(protocol.read_text(encoding='utf-8')) if protocol.is_file() else None
            if actual != expected:
                return {'status': 'local_mod_update_required', 'blocked': True, 'updates': updates}
        else:
            return {'status': 'unknown_mod_path', 'blocked': True, 'updates': updates}
    if len(copies) > 1:
        return {'status': 'multiple_eyes_copies', 'blocked': True, 'updates': updates}
    return {'status': 'ready' if copies else 'eyes_not_enabled',
            'blocked': not copies or any(not row['compatible'] for row in updates), 'updates': updates}
