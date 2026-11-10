"""Synthetic API security and directory regression coverage; no external calls."""
import ast
import inspect
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import auth
import main
import storage
from models import AppConfig
from network.api import router
from network.linkedin_csv import MAX_BYTES

CSV = b'First Name,Last Name,URL,Email Address,Company,Position\nAda,Example,https://linkedin.com/in/ada-test,ada@example.test,Example Inc,Engineer\n'


@pytest.fixture
def client(monkeypatch):
    # Real signed session cookies, synthetic system configuration and users.
    monkeypatch.setattr(auth, 'is_setup_complete', lambda: True)
    monkeypatch.setattr(auth, 'get_session_secret', lambda: 'synthetic-session-secret')
    monkeypatch.setattr(auth, 'get_agent_api_key', lambda: 'synthetic-agent-key')
    monkeypatch.setattr(main.scheduler, 'update_schedule', lambda *a: None)
    for uid in ('alice', 'bob'):
        storage.save_config(uid, AppConfig(network_enabled=True))
    # Do not start production lifecycle/scheduler in tests.
    return TestClient(main.app)


def login(client, uid='alice'):
    from starlette.responses import Response
    response = Response()
    auth.set_session_cookie(response, uid, 'user@example.test', 'Example')
    from http.cookies import SimpleCookie
    cookies = SimpleCookie(response.headers['set-cookie'])
    client.cookies.set(auth.COOKIE_NAME, cookies[auth.COOKIE_NAME].value)


def test_directory_isolation_preview_edit_export_delete(client):
    login(client)
    assert client.post('/api/network/import/preview', content=CSV).json()['created'] == 1
    assert client.get('/api/network/contacts').json() == []
    for _ in range(2):
        assert client.post('/api/network/import/commit', content=CSV).status_code == 200
    rows = client.get('/api/network/contacts', params={'q': 'ADA', 'employer': 'Example LLC'}).json()
    assert len(rows) == 1
    cid = rows[0]['id']
    assert client.get('/api/network/contacts?q=unmatched').json() == []
    assert client.patch(f'/api/network/contacts/{cid}', json={'notes': 'Synthetic note', 'verified': True}).status_code == 200
    client.post('/api/network/import/commit', content=CSV)
    assert client.get('/api/network/contacts').json()[0]['notes'] == 'Synthetic note'
    assert cid in client.get('/api/network/export').json()['contacts']
    login(client, 'bob')
    assert client.get('/api/network/contacts').json() == []
    assert client.get('/api/network/export').json()['contacts'] == {}
    assert client.patch(f'/api/network/contacts/{cid}', json={'notes': 'attack'}).status_code == 404
    assert client.delete(f'/api/network/contacts/{cid}').status_code == 404
    login(client)
    assert client.delete(f'/api/network/contacts/{cid}').status_code == 200
    assert client.get('/api/network/contacts').json() == []
    client.post('/api/network/import/commit', content=CSV)
    assert client.delete('/api/network/contacts').status_code == 200
    assert client.get('/api/network/export').json()['contacts'] == {}


def route_calls(dependant):
    return {dependant.call} | set().union(*(route_calls(d) for d in dependant.dependencies))


def test_every_route_session_only_flag_off_agent_rejected(client):
    assert auth.verify_agent_api_key('synthetic-agent-key')
    routes = [r for r in main.app.routes if getattr(r, 'path', '').startswith('/api/network')]
    assert len(routes) == len(router.routes) == 9
    for route in routes:
        assert auth.get_current_user in route_calls(route.dependant)
        assert auth.verify_agent_api_key not in route_calls(route.dependant)
        path = route.path.replace('{contact_id}', 'missing').replace('{review_id}', 'missing')
        method = next(iter(route.methods))
        client.cookies.clear()
        assert client.request(method, path, headers={'X-API-Key': 'synthetic-agent-key'}, json={}).status_code == 401
        login(client)
        storage.save_config('alice', AppConfig())
        assert client.request(method, path, json={}).status_code == 404
    # No agent endpoint is wired to network code or a network dependency.
    for route in main.app.routes:
        if not hasattr(route, 'dependant'):
            continue
        calls = route_calls(route.dependant)
        # Existing agent routes verify their header inside the handler.
        source = inspect.getsource(route.endpoint)
        if auth.verify_agent_api_key in calls or 'verify_agent_api_key(' in source:
            assert not route.path.startswith('/api/network')
            assert not any(getattr(c, '__module__', '').startswith('network') for c in calls)
            assert 'network' not in source.casefold()


def test_review_resolution_scoped_and_explicit(client):
    login(client)
    client.post('/api/network/import/commit', content=CSV)
    other = CSV.replace(b'/ada-test', b'/other-test')
    client.post('/api/network/import/commit', content=other)
    queue = client.get('/api/network/review').json()
    rid = next(iter(queue))
    login(client, 'bob')
    assert client.get('/api/network/review').json() == {}
    assert client.post(f'/api/network/review/{rid}/resolve', json={}).status_code == 404
    login(client)
    assert client.post(f'/api/network/review/{rid}/resolve', json={'target_id': 'missing'}).status_code == 404
    assert len(client.get('/api/network/contacts').json()) == 1
    assert client.post(f'/api/network/review/{rid}/resolve', json={}).status_code == 200
    assert client.get('/api/network/review').json() == {}
    assert len(client.get('/api/network/contacts').json()) == 2


def test_config_defaults_round_trip_and_url_validation(client):
    login(client)
    defaults = AppConfig()
    assert not defaults.network_enabled and not defaults.network_ai_opt_in
    assert defaults.application_strategy == 'standard'
    assert defaults.network_ai_url == defaults.network_ai_model == ''
    data = {'network_enabled': True, 'network_ai_opt_in': True, 'application_strategy': 'hybrid',
            'network_ai_url': 'http://localhost:8080/v1', 'network_ai_model': 'synthetic-local'}
    assert client.put('/api/config', json=data).status_code == 200
    result = client.get('/api/config').json()
    assert all(result[k] == v for k, v in data.items())
    for url in ('ftp://localhost', 'not-a-url', 'https://user:secret@localhost', 'http://localhost:bad'):
        assert client.put('/api/config', json={'network_ai_url': url}).status_code == 422
    assert client.put('/api/config', json={'network_ai_url': '', 'network_ai_model': ''}).status_code == 200
    assert client.get('/api/config').json()['network_ai_url'] == ''


def test_import_validation_and_no_contact_logging(client, caplog):
    login(client)
    assert client.post('/api/network/import/commit', content=b'invalid').status_code == 422
    assert client.post('/api/network/import/commit', content=b'x' * (MAX_BYTES + 1)).status_code == 413
    client.post('/api/network/import/commit', content=CSV)
    cid = client.get('/api/network/contacts').json()[0]['id']
    assert client.patch(f'/api/network/contacts/{cid}', json={'id': 'injected'}).status_code == 422
    assert client.patch(f'/api/network/contacts/{cid}', json={'notes': None}).status_code == 422
    assert 'ada@example.test' not in caplog.text
    assert 'Ada' not in caplog.text


def test_no_paid_ai_or_web_search_in_network():
    forbidden = {'anthropic', 'openai', 'google.genai', 'google.generativeai', 'google_genai'}
    for path in Path('network').rglob('*.py'):
        source = path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not any(a.name.split('.')[0] in forbidden or a.name in forbidden for a in node.names)
            if isinstance(node, ast.ImportFrom):
                assert node.module not in forbidden
                assert not (node.module == 'google' and any(a.name in ('genai', 'generativeai') for a in node.names))
        assert 'web_search_chat' not in source
