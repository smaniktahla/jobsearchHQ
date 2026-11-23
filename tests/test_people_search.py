"""People search: per-account sources and keys, credit-saving behaviour, research wiring, API and UI. No network."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import auth
import company_research
import main
import people_search
import research_batch
import storage
from models import AppConfig, CompanyResearch, Contact, Job

HTML = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text()


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}

    def json(self):
        return self._body


class FakeHTTP:
    """Stands in for requests.request: answers by URL substring, records every call."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        for part, resp in self.routes.items():
            if part in url:
                return resp(kw) if callable(resp) else resp
        raise AssertionError(f"unexpected call {url}")

    def urls(self):
        return [c["url"] for c in self.calls]

    def searches(self, part):
        """How many times a source was searched (PDL makes two requests per search)."""
        urls = [c["url"] for c in self.calls if part in c["url"]]
        return len(urls) // 2 if part == "peopledatalabs" else len(urls)


@pytest.fixture
def http(monkeypatch):
    def install(routes):
        fake = FakeHTTP(routes)
        monkeypatch.setattr(people_search.requests, "request", fake)
        return fake
    return install


PDL_BODY = {"status": 200, "total": 2, "data": [
    {"id": "p1", "full_name": "Dana Reyes", "job_title": "VP Data Analytics", "linkedin_url": "linkedin.com/in/dana-x",
     "work_email": True},                                        # free plan: hidden field comes back as `true`
    {"id": "p2", "full_name": "Lee Park", "job_title": "Talent Acquisition Partner", "linkedin_url": None},
]}
HUNTER_BODY = {"data": {"emails": [
    {"value": "dana.reyes@acme.test", "first_name": "Dana", "last_name": "Reyes", "position": "VP Data Analytics",
     "linkedin": None, "confidence": 94},
    {"value": "info@acme.test", "first_name": None, "last_name": None, "position": None},
]}, "meta": {"results": 2}}
APOLLO_BODY = {"total_entries": 1, "people": [
    {"id": "ap1", "first_name": "Sam", "last_name_obfuscated": "Sm***h", "title": "Head of Data Analytics"}]}


def cfg(**kw):
    base = dict(people_search_providers=["pdl"], pdl_api_key="pdl-k", hunter_api_key="hun-k", apollo_api_key="apo-k")
    base.update(kw)
    return AppConfig(**base)


# ── Job title → targets ──────────────────────────────────────────────────────

def test_job_function_strips_levels_and_noise():
    assert people_search.job_function("Senior Director of Data Analytics (Remote)") == "data analytics"
    assert people_search.job_function("VP, Machine Learning Engineering") == "machine learning engineering"
    titles = people_search.target_titles("Director of Data Analytics")
    assert "head of data analytics" in titles and "talent acquisition" in titles


def test_relevance_prefers_same_function_leaders_then_recruiters():
    jt = "Director of Data Analytics"
    r = people_search.relevance
    assert r("VP Data Analytics", jt) > r("Talent Acquisition Partner", jt) > r("Office Manager", jt)


def test_targets_come_from_each_job_not_a_fixed_role():
    nurse = people_search.Targets("Director of Nursing")
    dev = people_search.Targets("Senior Software Engineer")
    assert "director of nursing" in nurse.titles() and not any("software" in t for t in nurse.titles())
    assert "head of software engineer" in dev.titles() and not any("nursing" in t for t in dev.titles())
    assert people_search.relevance("Chief Nursing Officer", nurse) > people_search.relevance("VP Data Analytics", nurse)
    assert people_search.relevance("CTO", dev) == people_search.relevance("CDO", dev)       # no data/tech favourites


def test_account_titles_and_recruiter_choice():
    tg = people_search.targets_for(AppConfig(people_search_titles=["Program Director", " program director "],
                                             people_search_include_recruiters=False), "Nurse Manager")
    assert tg.extra == ["program director"] and "recruiter" not in tg.titles()
    assert people_search.relevance("Program Director, Cardiology", tg) >= 3
    assert people_search.relevance("Senior Recruiter", tg) == 0


def test_research_prompts_assume_no_kind_of_job(monkeypatch):
    src = Path(company_research.__file__).read_text().lower()
    for leaning in ("director of data analytics", "chief data officer", "vp analytics", "data/analytics"):
        assert leaning not in src, leaning
    seen = []
    monkeypatch.setattr(company_research, "_claude_search", lambda prompt, max_tokens=0: seen.append(prompt) or "{}")
    company_research._search_contacts("Mercy Health", "Director of Nursing", "mercy.test",
                                      company_research._who_to_find("Director of Nursing", AppConfig(people_search_titles=["CNO"])))
    assert "nursing team" in seen[0] and '"cno"' in seen[0] and "data" not in seen[0].lower()


# ── Per-account configuration ────────────────────────────────────────────────

def test_configured_providers_keep_order_and_need_a_key():
    c = cfg(people_search_providers=["hunter", "pdl", "apollo", "bogus", "pdl"], apollo_api_key=" ")
    assert people_search.configured_providers(c) == ["hunter", "pdl"]
    assert people_search.configured_providers(AppConfig()) == []


def test_no_sources_means_no_calls(http):
    fake = http({})
    assert people_search.search_people(AppConfig(), "Acme", "acme.test", "Director", "u1") == ([], [])
    assert fake.calls == []


# ── Each source ──────────────────────────────────────────────────────────────

def test_pdl_query_and_mapping(http):
    fake = http({"peopledatalabs": FakeResp(200, PDL_BODY)})
    people, notes = people_search.search_people(cfg(), "Acme", "acme.test", "Director of Data Analytics", "u1")
    leaders, recruiters = fake.calls                              # two searches so neither group crowds out the other
    assert leaders["headers"] == {"X-Api-Key": "pdl-k"}
    for call in fake.calls:
        assert {"term": {"job_company_website": "acme.test"}} == call["json"]["query"]["bool"]["must"][0]
    fn_clause = leaders["json"]["query"]["bool"]["must"][1]["bool"]["should"][0]
    assert {"match": {"job_title.text": "analytics"}} in fn_clause["bool"]["must"]
    assert {"match_phrase": {"job_title.text": "recruiter"}} in recruiters["json"]["query"]["bool"]["must"][1]["bool"]["should"]
    assert leaders["json"]["size"] + recruiters["json"]["size"] == 8 and leaders["json"]["size"] == 6
    assert len(people) == 2                                        # the same record from both searches is kept once
    dana = people[0]
    assert dana["name"] == "Dana Reyes" and dana["email"] == ""          # `true` is not an email
    assert dana["linkedin_url"] == "https://linkedin.com/in/dana-x"
    assert dana["sources"] == ["pdl"] and dana["provider_ids"] == {"pdl": "p1"}
    assert notes == ["People search (People Data Labs): 2 found"]


def test_pdl_uses_account_titles_and_can_skip_recruiters(http):
    fake = http({"peopledatalabs": FakeResp(200, {"data": []})})
    c = cfg(people_search_titles=["Program Director"], people_search_include_recruiters=False)
    people_search.search_people(c, "Mercy", "mercy.test", "Nurse Manager", "u1")
    assert len(fake.calls) == 1 and fake.calls[0]["json"]["size"] == 8           # leaders only, all the slots
    should = fake.calls[0]["json"]["query"]["bool"]["must"][1]["bool"]["should"]
    assert {"match_phrase": {"job_title.text": "program director"}} in should
    assert "recruiter" not in str(fake.calls[0]["json"])


def test_apollo_titles_follow_the_job_and_account(http):
    fake = http({"apollo.io": FakeResp(200, {"people": []})})
    people_search.search_people(cfg(people_search_providers=["apollo"], people_search_titles=["Charge Nurse"]),
                                "Mercy", "mercy.test", "Director of Nursing", "u1")
    titles = [v for k, v in fake.calls[0]["params"] if k == "person_titles[]"]
    assert "director of nursing" in titles and "charge nurse" in titles and "recruiter" in titles


def test_changing_who_to_look_for_is_a_new_search_not_a_saved_one(http):
    fake = http({"peopledatalabs": FakeResp(200, PDL_BODY)})
    people_search.search_people(cfg(), "Acme", "acme.test", "Director of Data Analytics", "u1")
    people_search.search_people(cfg(people_search_titles=["CFO"]), "Acme", "acme.test", "Director of Data Analytics", "u1")
    assert fake.searches("peopledatalabs") == 2


def test_pdl_small_limit_and_titles_without_a_function(http):
    fake = http({"peopledatalabs": FakeResp(200, {"data": []})})
    people_search.search_people(cfg(people_search_max_results=2), "Acme", "acme.test", "Director of Data", "u1")
    assert [c["json"]["size"] for c in fake.calls] == [2]          # too few to split: leaders only
    fake.calls.clear()
    people_search.search_people(cfg(), "Beta", "beta.test", "Director", "u1")
    assert [c["json"]["size"] for c in fake.calls] == [8]          # no function words: recruiters only
    assert "should" in fake.calls[0]["json"]["query"]["bool"]["must"][1]["bool"]


def test_pdl_without_domain_matches_company_name(http):
    fake = http({"peopledatalabs": FakeResp(200, {"data": []})})
    people_search.search_people(cfg(), "Acme Corp", None, "Director", "u1")
    assert {"match_phrase": {"job_company_name": "acme corp"}} in fake.calls[0]["json"]["query"]["bool"]["must"]


def test_pdl_404_is_no_results_not_an_error(http):
    http({"peopledatalabs": FakeResp(404, {"error": {"type": "not_found"}})})
    people, notes = people_search.search_people(cfg(), "Acme", "acme.test", "Director", "u1")
    assert people == [] and notes == ["People search (People Data Labs): 0 found"]


def test_hunter_mapping_skips_unnamed_and_retries_free_plan_limit(http):
    def hunter(kw):
        if kw["params"]["limit"] > 10:
            return FakeResp(400, {"errors": [{"id": "pagination_error", "details": "limit too high"}]})
        return FakeResp(200, HUNTER_BODY)
    fake = http({"hunter.io": hunter})
    people, _ = people_search.search_people(cfg(people_search_providers=["hunter"], people_search_max_results=20),
                                            "Acme", "acme.test", "Director of Data Analytics", "u1")
    assert [c["params"]["limit"] for c in fake.calls] == [20, 10]
    assert fake.calls[0]["headers"] == {"X-API-KEY": "hun-k"}
    assert [p["name"] for p in people] == ["Dana Reyes"] and people[0]["email"] == "dana.reyes@acme.test"


def test_apollo_masked_names_and_free_plan_message(http):
    http({"apollo.io": FakeResp(200, APOLLO_BODY)})
    people, _ = people_search.search_people(cfg(people_search_providers=["apollo"]), "Acme", "acme.test",
                                            "Director of Data Analytics", "u1")
    assert people[0]["name"] == "Sam Sm***h" and people[0]["masked"] and people[0]["provider_ids"] == {"apollo": "ap1"}

    http({"apollo.io": FakeResp(403, {"error": "forbidden"})})
    people, notes = people_search.search_people(cfg(people_search_providers=["apollo"], people_search_cache_days=0),
                                                "Other", "other.test", "Director", "u1")
    assert people == [] and "free plan has no API access" in notes[0]


def test_errors_become_notes_and_never_include_the_key(http):
    http({"peopledatalabs": FakeResp(402, {}), "hunter.io": FakeResp(401, {"errors": [{"id": "authentication_failed"}]})})
    people, notes = people_search.search_people(cfg(people_search_providers=["pdl", "hunter"]), "Acme", "acme.test", "D", "u1")
    assert people == []
    assert "out of credits" in notes[0] and "key rejected by Hunter" in notes[1]
    assert not any("pdl-k" in n or "hun-k" in n for n in notes)


# ── Credit saving ────────────────────────────────────────────────────────────

def test_first_mode_stops_at_first_source_with_results(http):
    fake = http({"peopledatalabs": FakeResp(200, PDL_BODY), "hunter.io": FakeResp(200, HUNTER_BODY)})
    people_search.search_people(cfg(people_search_providers=["pdl", "hunter"]), "Acme", "acme.test", "D", "u1")
    assert fake.searches("peopledatalabs") == 1 and fake.searches("hunter.io") == 0


def test_first_mode_moves_on_when_a_source_finds_nobody(http):
    fake = http({"peopledatalabs": FakeResp(200, {"data": []}), "hunter.io": FakeResp(200, HUNTER_BODY)})
    people, _ = people_search.search_people(cfg(people_search_providers=["pdl", "hunter"]), "Acme", "acme.test", "D", "u1")
    assert fake.searches("peopledatalabs") == 1 and fake.searches("hunter.io") == 1
    assert people[0]["sources"] == ["hunter"]


def test_all_mode_merges_the_same_person(http):
    http({"peopledatalabs": FakeResp(200, PDL_BODY), "hunter.io": FakeResp(200, HUNTER_BODY)})
    people, _ = people_search.search_people(cfg(people_search_providers=["pdl", "hunter"], people_search_mode="all"),
                                            "Acme", "acme.test", "Director of Data Analytics", "u1")
    dana = [p for p in people if p["name"] == "Dana Reyes"]
    assert len(dana) == 1
    assert dana[0]["sources"] == ["pdl", "hunter"] and dana[0]["email"] == "dana.reyes@acme.test"
    assert dana[0]["linkedin_url"] and dana[0]["provider_ids"] == {"pdl": "p1"}


def test_cache_is_reused_per_account_only(http):
    fake = http({"peopledatalabs": FakeResp(200, PDL_BODY)})
    c = cfg()
    people_search.search_people(c, "Acme", "acme.test", "Director of Data Analytics", "alice")
    people, notes = people_search.search_people(c, "Acme", "acme.test", "Senior Director, Data Analytics", "alice")
    assert fake.searches("peopledatalabs") == 1 and len(people) == 2 and "saved result" in notes[0]
    people_search.search_people(c, "Acme", "acme.test", "Director of Data Analytics", "bob")
    assert fake.searches("peopledatalabs") == 2                   # another account never sees alice's results
    people_search.search_people(cfg(people_search_cache_days=0), "Acme", "acme.test", "Director of Data Analytics", "alice")
    assert fake.searches("peopledatalabs") == 3


def test_max_results_caps_output(http):
    http({"peopledatalabs": FakeResp(200, PDL_BODY)})
    people, _ = people_search.search_people(cfg(people_search_max_results=1), "Acme", "acme.test", "D", "u1")
    assert len(people) == 1


def test_masked_names_never_merge():
    merged = people_search.merge_contacts([{"name": "Sam Sm***h", "masked": True, "sources": ["apollo"]}],
                                          [{"name": "Sam Sm***h", "masked": True, "sources": ["apollo"]}])
    assert len(merged) == 2


# ── Key checks spend nothing ─────────────────────────────────────────────────

def test_check_key_pdl_uses_a_query_that_cannot_match(http):
    fake = http({"peopledatalabs": FakeResp(404, {})})
    assert people_search.check_key(cfg(), "pdl") == "key works"
    assert fake.calls[0]["json"]["query"] == {"term": {"id": "jshq-key-check-no-match"}}


def test_check_key_hunter_and_apollo(http):
    http({"hunter.io/v2/account": FakeResp(200, {"data": {"plan_name": "Free", "requests": {"credits": {"used": 3, "available": 50}}}})})
    assert people_search.check_key(cfg(), "hunter") == "key works (Free; 3 of 50 credits used this month)"
    fake = http({"auth/health": FakeResp(200, {"is_logged_in": True}), "mixed_people": FakeResp(403, {})})
    with pytest.raises(people_search.PeopleSearchError, match="free plan"):
        people_search.check_key(cfg(), "apollo")
    assert len(fake.calls) == 2
    with pytest.raises(people_search.PeopleSearchError, match="no Hunter key"):
        people_search.check_key(AppConfig(), "hunter")


# ── Wiring into company research ─────────────────────────────────────────────

@pytest.fixture
def web(monkeypatch):
    monkeypatch.setattr(company_research, "_find_domain", lambda c: "acme.test")
    monkeypatch.setattr(company_research, "_search_contacts", lambda c, t, d, who="": {
        "company_summary": "Acme makes things.", "contacts": [{"name": "Dana Reyes", "title": "VP Data Analytics"}]})
    monkeypatch.setattr(company_research, "_try_scrape", lambda d: ("", ""))
    monkeypatch.setattr(company_research, "_enrich_contacts_with_emails", lambda cs, co: cs)


def test_research_adds_people_and_labels_sources(web, http):
    http({"peopledatalabs": FakeResp(200, PDL_BODY)})
    r = company_research.research_company("Acme", "Director of Data Analytics", "u1", config=cfg())
    by_name = {c["name"]: c for c in r["contacts"]}
    assert by_name["Dana Reyes"]["sources"] == ["web", "pdl"]
    assert by_name["Lee Park"]["sources"] == ["pdl"]
    assert "People search (People Data Labs): 2 found" in r["searches_run"]
    job_research = CompanyResearch(contacts=[Contact(**c) for c in r["contacts"]])   # saves cleanly
    assert job_research.contacts[1].sources


def test_research_can_skip_people_search(web, http):
    fake = http({})
    r = company_research.research_company("Acme", "D", "u1", people_search_enabled=False, config=cfg())
    assert fake.calls == [] and [c["name"] for c in r["contacts"]] == ["Dana Reyes"]


def test_research_loads_the_accounts_own_settings(web, http):
    storage.save_config("alice", cfg())
    storage.save_config("bob", AppConfig())
    fake = http({"peopledatalabs": FakeResp(200, PDL_BODY)})
    company_research.research_company("Acme", "D", "bob")
    assert fake.calls == []
    company_research.research_company("Acme", "D", "alice")
    assert fake.calls[0]["headers"] == {"X-Api-Key": "pdl-k"}


def _web_down(monkeypatch):
    def down(*a):
        raise company_research.ResearchError("credit balance too low")
    for f in ("_find_domain", "_local_find_domain"):
        monkeypatch.setattr(company_research, f, down)


def test_people_search_still_answers_when_web_research_is_down(monkeypatch, http):
    _web_down(monkeypatch)
    http({"peopledatalabs": FakeResp(200, PDL_BODY)})
    r = company_research.research_company("Acme", "D", "u1", config=cfg())
    assert len(r["contacts"]) == 2 and any("people search only" in s for s in r["searches_run"])


def test_web_down_and_no_people_search_still_raises(monkeypatch, http):
    _web_down(monkeypatch)
    http({"peopledatalabs": FakeResp(402, {})})
    with pytest.raises(company_research.ResearchError):
        company_research.research_company("Acme", "D", "u1", config=AppConfig())
    with pytest.raises(company_research.ResearchError, match="out of credits"):
        company_research.research_company("Acme", "D", "u1", config=cfg())


def test_auto_research_spends_credits_only_when_opted_in(monkeypatch):
    seen = []
    monkeypatch.setattr(company_research, "research_company",
                        lambda **kw: seen.append(kw["people_search_enabled"]) or {"contacts": []})
    storage.save_config("alice", cfg())
    research_batch.research_job("alice", Job(title="D", company="Acme"))
    storage.save_config("alice", cfg(people_search_in_auto_research=True))
    research_batch.research_job("alice", Job(title="D", company="Acme"))
    assert seen == [False, True]


# ── API ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "is_setup_complete", lambda: True)
    monkeypatch.setattr(auth, "get_session_secret", lambda: "synthetic-session-secret")
    monkeypatch.setattr(main.scheduler, "update_schedule", lambda *a: None)
    for uid in ("alice", "bob"):
        storage.save_config(uid, AppConfig())
    return TestClient(main.app)


def login(client, uid):
    from http.cookies import SimpleCookie
    from starlette.responses import Response
    response = Response()
    auth.set_session_cookie(response, uid, f"{uid}@example.test", uid)
    client.cookies.set(auth.COOKIE_NAME, SimpleCookie(response.headers["set-cookie"])[auth.COOKIE_NAME].value)


def test_settings_are_per_account_and_cleaned(client):
    login(client, "alice")
    body = client.get("/api/config").json()
    body.update(people_search_providers=["hunter", "nope", "hunter", "pdl"], people_search_mode="weird",
                people_search_max_results=500, hunter_api_key="  hun-k  ", pdl_api_key="pdl-k")
    saved = client.put("/api/config", json=body).json()
    assert saved["people_search_providers"] == ["hunter", "pdl"]
    assert saved["people_search_mode"] == "first" and saved["people_search_max_results"] == 25
    assert saved["hunter_api_key"] == "hun-k"
    body = saved | {"people_search_titles": ["  Hiring   Manager ", "hiring manager", "", "CNO"]}
    assert client.put("/api/config", json=body).json()["people_search_titles"] == ["Hiring Manager", "CNO"]
    login(client, "bob")
    bob = client.get("/api/config").json()
    assert bob["people_search_providers"] == [] and bob["hunter_api_key"] == ""


def test_test_key_endpoint(client, http):
    http({"hunter.io/v2/account": FakeResp(200, {"data": {"plan_name": "Free"}})})
    login(client, "alice")
    assert client.post("/api/config/test-people-search?provider=hunter").status_code == 400   # no key yet
    storage.save_config("alice", cfg())
    r = client.post("/api/config/test-people-search?provider=hunter")
    assert r.status_code == 200 and r.json()["message"].startswith("key works")
    assert client.post("/api/config/test-people-search?provider=zoominfo").status_code == 400


def test_reveal_apollo_contact(client, http, monkeypatch):
    monkeypatch.setattr(main, "enrich_job", lambda j: j.model_dump())
    storage.save_config("alice", cfg(people_search_providers=["apollo"]))
    job = Job(title="D", company="Acme", research=CompanyResearch(contacts=[
        Contact(name="Web Person", sources=["web"]),
        Contact(name="Sam Sm***h", title="Head of Data", masked=True, sources=["apollo"], provider_ids={"apollo": "ap1"})]))
    storage.save_job("alice", job)
    fake = http({"people/match": FakeResp(200, {"person": {"name": "Sam Smith", "email": "sam@acme.test",
                                                          "linkedin_url": "http://www.linkedin.com/in/sam"}})})
    login(client, "alice")
    assert client.post(f"/api/jobs/{job.id}/research/contacts/0/reveal").status_code == 400   # not Apollo
    r = client.post(f"/api/jobs/{job.id}/research/contacts/1/reveal")
    assert r.status_code == 200
    c = r.json()["research"]["contacts"][1]
    assert c["name"] == "Sam Smith" and c["email"] == "sam@acme.test" and not c["masked"]
    assert fake.calls[0]["params"]["id"] == "ap1" and fake.calls[0]["headers"]["x-api-key"] == "apo-k"
    assert storage.load_job("alice", job.id).research.contacts[1].name == "Sam Smith"
    login(client, "bob")
    assert client.post(f"/api/jobs/{job.id}/research/contacts/1/reveal").status_code == 404     # not bob's job


# ── UI ───────────────────────────────────────────────────────────────────────

def test_settings_page_and_job_page_are_wired():
    assert 'id="sx-people"' in HTML and 'data-sx="people"' in HTML
    for p in ("pdl", "hunter", "apollo"):
        for needle in (f'id="cfg-ps-use-{p}"', f'id="cfg-ps-order-{p}"', f'id="cfg-{p}-key"', f"testPeopleSearch('{p}')"):
            assert needle in HTML, needle
    assert "...peopleSearchSettings()," in HTML and "loadPeopleSearchSettings(c);" in HTML
    assert 'id="cfg-ps-titles"' in HTML and 'id="cfg-ps-recruiters"' in HTML and "Who to look for" in HTML
    assert "revealContact(${idx})" in HTML and "src-badge" in HTML
    assert re.search(r'type="password" id="cfg-(pdl|hunter|apollo)-key"', HTML)
