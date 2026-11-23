"""Retire completed menu definitions, keeping treaty events and monitoring intact."""
from contextlib import closing
from hashlib import sha256
import json
import re
import sqlite3
from uuid import uuid4

from bridge_instance import bridge_instance_lock
import treaty_package_install as installer


def completed_reviews(database):
    if not database.is_file():
        return {}
    with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True)) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='chat_state'").fetchone():
            return {}
        row=connection.execute('SELECT payload FROM chat_state WHERE singleton=1').fetchone()
    result={}
    for chat in (json.loads(row[0]).get('chats',{}).values() if row else ()):
        for agreement in chat.get('agreements',{}).values():
            effect=agreement.get('execution_result') or {}
            package=agreement.get('execution_package') or {}
            event=package.get('event_id','')
            if (not re.fullmatch(r'eyes_[a-z0-9_]+\.1',event)
                    or agreement.get('status')!='execution_confirmed'
                    or effect.get('status')!='execution_confirmed'):
                continue
            identity={k:agreement.get(k) for k in ('campaign_id','agreement_id','execution_request_id')}
            identity['version']=agreement.get('current_version')
            result[event[:-2]]=identity
    return result


def retire(mod, database, *, confirm=False):
    mod=installer._check_mod_root(mod)
    terminal=completed_reviews(database)
    path=installer._manifest_path(mod)
    if not confirm:
        return _collect(mod,terminal,installer.read_manifest(mod))[0]
    if installer.hoI4_running() is not False:
        raise ValueError('HOI4 must be closed before pruning reviews.')
    with bridge_instance_lock(path.parent/'package.lock') as acquired:
        if not acquired or installer.hoI4_running() is not False:
            raise ValueError('Treaty install is busy or HOI4 is open.')
        manifest=installer.read_manifest(mod)
        names, owners=_collect(mod,terminal,manifest)
        if not names:
            return []
        backup=path.parent/'backups'/('completed-reviews-'+uuid4().hex)
        if not backup.resolve().is_relative_to(mod):
            raise ValueError('unsafe_backup_path')
        moved=[]
        try:
            for name in names:
                target=backup/name
                target.parent.mkdir(parents=True,exist_ok=True)
                (mod/name).rename(target)
                moved.append(name)
                if name in owners:
                    owners[name].setdefault('retired_reviews',[]).append(name)
            if owners:
                installer._write_manifest(mod,manifest)
        except OSError:
            for name in reversed(moved):
                (backup/name).rename(mod/name)
            raise
        return names


def _collect(mod,terminal,manifest):
    entries=manifest['installed']
    owned=installer._owned_files(manifest)
    matches=[]; owners={}
    for namespace,identity in terminal.items():
        for relative in (f'common/decisions/{namespace}.txt',f'common/decisions/categories/{namespace}.txt'):
            path=mod/relative
            if not path.resolve().is_relative_to(mod) or not path.is_file():
                continue
            data=path.read_bytes()
            if relative in owned:
                entry=next((e for e in entries if e.get('identity')==identity and relative in e.get('files',{})),None)
                if entry and sha256(data).hexdigest()==entry['files'][relative]:
                    owners[relative]=entry
                    matches.append(relative)
            elif sha256(data.replace(b'\r\n',b'\n')).hexdigest()==installer.LEGACY_REVIEW_HASHES.get(relative):
                matches.append(relative)
    return sorted(matches),owners
