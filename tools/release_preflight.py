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
        from developer_access import PUBLIC_BUILD, enabled
        if PUBLIC_BUILD:
            import importlib.util
            from windows_desktop import Controls
            if enabled() or any(hasattr(Controls, name) for name in
                    ('start_acceptance','prepare_game_qa','game_qa_status','acceptance_status')):
                raise RuntimeError('public_developer_api_exposed')
            for module in ('live_qa','developer_desktop','desktop_acceptance','release_acceptance'):
                if importlib.util.find_spec(module) is not None:
                    raise RuntimeError('public_developer_module_present')
            if 'live-qa-open' in client.get('/desktop?developer=1').text:
                raise RuntimeError('public_developer_controls_visible')
    print('HOI4-EYES isolated offline preflight passed; no game or model invoked.')


if __name__ == '__main__':
    main()
