"""Readiness checks use a disposable profile, never the player's database."""
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile


@contextmanager
def isolated_environment(folder):
    values = {'EYES_PROFILE_DIR':folder, 'EYES_STATE_DB':folder+'/preflight.sqlite3',
              'EYES_RESPONSE_PROVIDER':'template', 'OLLAMA_URL':'http://127.0.0.1:1'}
    previous = {key:os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main():
    with tempfile.TemporaryDirectory(prefix='eyes-preflight-') as folder, isolated_environment(folder):
        # Import only after setting paths: storage modules bind paths at import.
        from mock_ai_server import create_app
        from hub_backend import HubBackend
        from experimental_stack import stack_environment
        root = Path(__file__).resolve().parents[1]
        HubBackend(root)
        stack_environment(root)
        app = create_app()
        app.testing = True
        client = app.test_client()
        for path in ('/desktop', '/world', '/fleet', '/meetings', '/chats'):
            response = client.get(path)
            if response.status_code != 200:
                raise RuntimeError('preflight_route_failed: '+path)
        if not all(client.get('/health?features=1').json['features'].values()):
            raise RuntimeError('preflight_features_unavailable')
    print('HOI4-EYES isolated offline preflight passed; no game or model invoked.')


if __name__ == '__main__':
    main()
