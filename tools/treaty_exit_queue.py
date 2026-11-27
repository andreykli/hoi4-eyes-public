"""Stage signed treaty versions for one installation pass after HOI4 closes."""
from hashlib import sha256
import os
import uuid
import json
from pathlib import Path
import re

from treaty_package_install import hoI4_running, install, validate_package


def enqueue(path, package):
    validate_package(package)
    if hoI4_running() is not True:
        raise ValueError('Open HOI4 to schedule installation on exit.')
    path = Path(path)
    with path.open('x', encoding='utf-8') as output:
        json.dump(package, output, ensure_ascii=False)


def install_after_exit(path, mod, get_agreement):
    path = Path(path)
    if not path.is_file() or hoI4_running() is not False:
        return None
    package = json.loads(path.read_text(encoding='utf-8'))
    validate_package(package)
    current = get_agreement(package['chat_id'], package['agreement_id'])
    delivery = current.get('native_delivery') or {}
    if (current.get('status') != 'manual_required' or current.get('execution_result')
            or delivery.get('sent') or delivery.get('packet') != package):
        raise ValueError('Queued treaty is no longer ready for installation.')
    result = install(package, mod_root=mod, confirm=True, allowed_roots=[mod])
    if result.get('installed') or result.get('already_installed'):
        path.unlink()
    return result


def queued_paths(runtime):
    legacy = runtime/'treaty-install-on-exit.json'
    jobs = sorted((runtime/'treaty-install-jobs').glob('*.json'))
    return ([legacy] if legacy.is_file() else []) + jobs


def enqueue_signed(runtime, package):
    """Idempotent and atomic across repeated clicks and application restarts."""
    from bridge_instance import bridge_instance_lock
    identity = validate_package(package)['identity']
    folder = runtime/'treaty-install-jobs'
    key = json.dumps([identity[field] for field in ('campaign_id','agreement_id','version')])
    path = folder/(sha256(key.encode()).hexdigest()+'.json')
    with bridge_instance_lock(folder/'queue.lock') as acquired:
        if not acquired:
            raise ValueError('Another treaty is being queued. Try again.')
        if path.is_file():
            if json.loads(path.read_text(encoding='utf-8')) != package:
                raise ValueError('The queued package differs for this exact treaty version.')
            return 'ALREADY_QUEUED'
        if hoI4_running() is None:
            raise ValueError('Cannot determine whether HOI4 is running.')
        temporary = folder/f'.{uuid.uuid4().hex}.tmp'
        try:
            with temporary.open('x',encoding='utf-8') as output:
                json.dump(package,output,ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary,path)
        finally:
            temporary.unlink(missing_ok=True)
    return 'QUEUED'


def install_all_after_exit(runtime, mod, get_agreement):
    from bridge_instance import bridge_instance_lock
    if hoI4_running() is not False:
        return {'installed':0,'blocked':[]}
    summary = {'installed':0,'blocked':[]}
    with bridge_instance_lock(runtime/'treaty-install-jobs'/'queue.lock') as acquired:
        if not acquired:
            return summary
        for path in queued_paths(runtime):
            try:
                result = install_after_exit(path,mod,get_agreement)
                if result:
                    summary['installed']+=1
            except (OSError,ValueError,KeyError,TypeError) as error:
                # Keep failed jobs for investigation. Do not hide other ready work.
                summary['blocked'].append({'job':path.name,'reason':str(error)})
    return summary


def active_unified_mod(root, documents=None):
    from single_mod import active_source
    from unified_mod_setup import documents_directory
    return active_source(root,documents or documents_directory())
