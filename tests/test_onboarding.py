"""New accounts start blank (no data from any other profile) and the setup wizard endpoints work."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import auth
import main
import storage
from models import AppConfig

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, 'is_setup_complete', lambda: True)
    monkeypatch.setattr(auth, 'get_session_secret', lambda: 'synthetic-session-secret')
    monkeypatch.setattr(main.scheduler, 'update_schedule', lambda *a: None)
    return TestClient(main.app)


def login(client, uid='newbie'):
    from http.cookies import SimpleCookie
    from starlette.responses import Response
    r = Response()
    auth.set_session_cookie(r, uid, 'new@example.test', 'New User')
    client.cookies.set(auth.COOKIE_NAME, SimpleCookie(r.headers['set-cookie'])[auth.COOKIE_NAME].value)


def test_fresh_account_config_is_blank():
    c = storage.load_config('brand-new-user')
    assert c.onboarding_completed is False
    for f in ('author_name', 'author_email', 'author_phone', 'author_location', 'scheduled_search_location',
              'ollama_url', 'ollama_model', 'anthropic_api_key', 'gemini_api_key', 'openai_api_key'):
        assert getattr(c, f) == '', f
    for f in ('deal_breakers', 'preferred_keywords', 'scheduled_search_terms', 'work_experience', 'certifications'):
        assert getattr(c, f) == [], f
    for f in ('w2_salary_min', 'w2_salary_max', 'contract_hourly_min', 'contract_hourly_max',
              'prefilter_w2_floor', 'prefilter_hourly_floor'):
        assert getattr(c, f) == 0, f
    assert not c.prefilter_dc_metro_only and not c.prefilter_manager_plus_only


def test_existing_config_without_flag_counts_as_onboarded():
    assert AppConfig(**{'author_name': 'Existing'}).onboarding_completed is True


def test_no_personal_data_hardcoded_in_source():
    banned = [r'Salil', r'Maniktahla', r'571-215', r'10\.10\.10\.\d', r'Springfield, VA', r'gemma4', r'placeholder="22150"']
    offenders = []
    for path in list(ROOT.glob('*.py')) + [ROOT / 'static' / 'index.html']:
        text = path.read_text()
        for pat in banned:
            for m in re.finditer(pat, text):
                line = text.count('\n', 0, m.start()) + 1
                offenders.append(f'{path.name}:{line}:{pat}')
    # Infra defaults in company_research are env-overridable service endpoints, not user data.
    offenders = [o for o in offenders if not o.startswith('company_research.py')]
    assert offenders == []


def test_wizard_endpoints_round_trip(client):
    login(client)
    assert client.get('/api/config').json()['onboarding_completed'] is False

    assert client.put('/api/onboarding/ai', json={'provider': 'gemini', 'api_key': 'AIza-test'}).status_code == 200
    assert client.put('/api/onboarding/ai', json={'provider': 'nope'}).status_code == 400
    r = client.put('/api/onboarding/preferences', json={
        'job_titles': ['PM', ' pm ', 'Ops Manager', ''], 'location': 'Austin, TX', 'work_mode': 'remote',
        'w2_salary_min': 90000, 'w2_salary_max': 130000, 'deal_breakers': ['relocation'], 'preferred_keywords': ['Excel']})
    assert r.status_code == 200
    c = client.get('/api/config').json()
    assert (c['fast_provider'], c['strong_provider'], c['gemini_api_key']) == ('gemini', 'gemini', 'AIza-test')
    assert c['scheduled_search_terms'] == ['PM', 'Ops Manager']
    assert c['scheduled_search_remote'] is True and c['scheduled_search_hybrid'] is False
    assert c['w2_salary_min'] == 90000 and c['contract_hourly_max'] == 0
    assert c['deal_breakers'] == ['relocation'] and c['onboarding_completed'] is False

    # Saving the regular Settings form must not flip the wizard flag or wipe intake pre-filter knobs.
    cfg = storage.load_config('newbie'); cfg.prefilter_w2_floor = 123
    storage.save_config('newbie', cfg)
    form = client.get('/api/config').json(); form['onboarding_completed'] = True; form['prefilter_w2_floor'] = 0
    assert client.put('/api/config', json=form).status_code == 200
    after = client.get('/api/config').json()
    assert after['onboarding_completed'] is False and after['prefilter_w2_floor'] == 123

    assert client.post('/api/onboarding/complete').json() == {'ok': True}
    assert client.get('/api/config').json()['onboarding_completed'] is True
    client.post('/api/onboarding/reset')
    assert client.get('/api/config').json()['onboarding_completed'] is False
    assert client.get('/api/config').json()['scheduled_search_terms'] == ['PM', 'Ops Manager']  # reset keeps data



def test_new_account_gets_default_ats_template_and_existing_ones_are_untouched(monkeypatch):
    import resume_template as rt
    assert rt.DEFAULT_TEMPLATE.is_file()
    storage.load_config('fresh-user')                      # first touch creates the account's config
    info = rt.template_info('fresh-user')
    assert info['loaded'] and {'name', 'contact', 'section', 'bullet', 'org', 'title'} <= set(info['roles'])
    out = rt.render_sample('fresh-user', Path(storage.get_user_dir('fresh-user')) / 'preview.docx')
    from docx import Document
    text = '\n'.join(p.text for p in Document(out).paragraphs)
    assert 'JANE SAMPLE' in text and 'Jobby' not in text and 'SheetsResume' not in text
    assert 'McIntosh' not in (Document(out).core_properties.author + Document(out).core_properties.last_modified_by)

    rt.delete_template('fresh-user')                       # user removes it: it must not come back
    storage.load_config('fresh-user')
    assert not rt.has_template('fresh-user')

    storage.save_config('old-user', AppConfig())           # pre-existing account: nothing installed
    storage.load_config('old-user')
    assert not rt.has_template('old-user')


def test_missing_default_template_never_blocks_account_creation(monkeypatch):
    import resume_template as rt
    monkeypatch.setenv('JSHQ_DEFAULT_TEMPLATE', '/nonexistent.docx')
    assert storage.load_config('another-new-user').onboarding_completed is False
    assert not rt.has_template('another-new-user')
