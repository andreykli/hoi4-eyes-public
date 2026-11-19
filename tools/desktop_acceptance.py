"""Launch the existing acceptance suite in a separate, disposable user profile."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from uuid import uuid4

from user_storage import APP_ROOT
from validate_hoi4_gui import discover_hoi4_install


class DesktopAcceptance:
    def __init__(self):
        self.process = None
        self.sandbox = None
        self.output = None

    def start(self):
        if self.process and self.process.poll() is None:
            return self.status()
        self.status()
        game = discover_hoi4_install()
        if not game:
            return {'state':'failed','error':'game_install_unavailable'}
        parent = APP_ROOT/'runtime'/'tmp'
        parent.mkdir(parents=True, exist_ok=True)
        self.output = parent/f'desktop-qa-{uuid4().hex}'
        self.sandbox = tempfile.TemporaryDirectory(prefix='eyes-qa-')
        env = {key: value for key,value in os.environ.items()
               if not key.startswith(('EYES_', 'OLLAMA_'))}
        env.update(EYES_PROFILE_DIR=self.sandbox.name, EYES_RESPONSE_PROVIDER='template',
                   EYES_STATE_DB=str(Path(self.sandbox.name)/'fixture.sqlite3'),
                   OLLAMA_URL='http://127.0.0.1:1', PYTHONIOENCODING='utf-8')
        log = self.output.with_suffix('.log')
        try:
            with log.open('x', encoding='utf-8') as stream:
                self.process = subprocess.Popen(
                    [sys.executable,str(APP_ROOT/'tools/release_acceptance.py'),'prepare-offline',
                     '--game-dir',str(game),'--output',str(self.output)],
                    cwd=APP_ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        except OSError:
            self.sandbox.cleanup()
            self.sandbox = None
            self.process = None
            return {'state':'failed','error':'qa_start_failed','log':str(log)}
        return self.status()

    def status(self):
        if not self.process:
            return {'state':'idle'}
        code = self.process.poll()
        if code is None:
            return {'state':'running'}
        if self.sandbox is not None:
            self.sandbox.cleanup()
            self.sandbox = None
        try:
            report_path = self.output/'offline-report.json'
            data = json.loads(report_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {'state':'failed','error':'qa_report_unavailable','log':str(self.output.with_suffix('.log'))}
        return {'state':'passed' if code == 0 and data['passed'] else 'failed',
                'tests':data['unique_tests'],'packages':len(data['packets']),
                'gameplay_confirmed':False,'report':str(report_path)}
