"""Installed-distribution diagnostics, without developer tests or user state."""
from hashlib import sha256
import importlib
import json
from pathlib import Path, PurePosixPath
import py_compile
import tempfile


def check_distribution(root, game, output):
    from hoi4_static_audit import audit
    rows = []

    def check(name, callback):
        try:
            callback()
            rows.append({'test':name, 'status':'passed'})
        except Exception as error:
            rows.append({'test':name, 'status':'failed', 'error':str(error)})

    def integrity():
        path = root/'RELEASE-MANIFEST.json'
        if not path.is_file():
            path = root/'SOURCE-MANIFEST.json'
        manifest = json.loads(path.read_text(encoding='utf-8'))
        for name, expected in manifest['files'].items():
            path = PurePosixPath(name)
            if path.is_absolute() or '..' in path.parts or ':' in name or '\\' in name:
                raise ValueError('unsafe_manifest_path')
            selected = (root/name).resolve()
            if not selected.is_relative_to(root.resolve()):
                raise ValueError('manifest_path_escape')
            data = selected.read_bytes()
            if sha256(data).hexdigest() != expected['sha256']:
                raise ValueError('distribution_changed: '+name)

    check('distribution_integrity', integrity)
    def syntax():
        with tempfile.TemporaryDirectory(prefix='eyes-compile-') as folder:
            for index, source in enumerate((root/'tools').glob('*.py')):
                py_compile.compile(str(source), cfile=str(Path(folder)/f'{index}.pyc'), doraise=True)
    check('python_syntax', syntax)
    for module in ('experimental_hub', 'hoi4_bridge', 'native_recognition',
                   'country_maker_execution', 'treaty_decision_package'):
        check('import_'+module, lambda module=module: importlib.import_module(module))
    def scripts():
        result = audit(root/'prepared/unified_game_mod/ai_assistant', game)
        if result['errors']:
            raise ValueError('mod_static_validation_failed')
    check('mod_static_audit', scripts)
    summary = {'passed':all(row['status']=='passed' for row in rows),
               'unique_tests':len(rows), 'packets':{}, 'checks':rows,
               'mode':'installed_distribution_diagnostics', 'gameplay_confirmed':False,
               'evidence':'Integrity, Python imports and static scripts only; not gameplay or developer regression tests.'}
    (output/'offline-report.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    return summary
