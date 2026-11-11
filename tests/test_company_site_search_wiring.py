"""Company career sites must actually be searched (manual Search Boards and the scheduler both depend on it)."""
import threading

import company_site_search
import jobspy_search
from jobspy_search import JobSearchRequest


def _patch(monkeypatch):
    calls = {"company": [], "boards": []}

    def fake_company(sites, search_term, user_id, results_wanted, auto_score, skip_existing, should_stop=None):
        calls["company"].append([s.url for s in sites])
        return {"created": [{"id": "c1", "source": "company"}], "skipped": [], "errors": [], "total_scraped": 1}

    def fake_boards(req, user_id, cancel=None):
        calls["boards"].append(list(req.sites))
        return jobspy_search.JobSearchResult(created=[{"id": "b1"}], total_scraped=2)

    monkeypatch.setattr(company_site_search, "search_company_sites", fake_company)
    monkeypatch.setattr(jobspy_search, "_run_board_search", fake_boards)
    return calls


def test_request_accepts_company_sites_from_ui_and_scheduler_shapes():
    ui = JobSearchRequest(search_term="x", sites=[], company_sites=[{"company": "MITRE", "url": "https://careers.mitre.org"}])
    sched = JobSearchRequest(search_term="x", company_sites=[{"url": "https://example.com/careers"}])
    assert ui.company_sites[0].company == "MITRE" and sched.company_sites[0].url.endswith("/careers")


def test_company_only_search_does_not_scrape_boards(monkeypatch):
    calls = _patch(monkeypatch)
    r = jobspy_search.run_search(JobSearchRequest(search_term="x", sites=[], company_sites=[{"url": "https://a.test/jobs"}]), "u")
    assert calls == {"company": [["https://a.test/jobs"]], "boards": []}
    assert [j["id"] for j in r.created] == ["c1"] and r.total_scraped == 1


def test_mixed_search_does_both_and_merges(monkeypatch):
    calls = _patch(monkeypatch)
    r = jobspy_search.run_search(JobSearchRequest(search_term="x", sites=["indeed", "company_site"], company_sites=[{"url": "https://a.test/jobs"}]), "u")
    assert calls["boards"] == [["indeed"]] and calls["company"] == [["https://a.test/jobs"]]
    assert sorted(j["id"] for j in r.created) == ["b1", "c1"] and r.total_scraped == 3


def test_board_only_search_skips_company_step(monkeypatch):
    calls = _patch(monkeypatch)
    jobspy_search.run_search(JobSearchRequest(search_term="x", sites=["indeed"]), "u")
    assert calls["company"] == [] and calls["boards"] == [["indeed"]]


def test_cancel_before_boards_stops_after_company_step(monkeypatch):
    calls = _patch(monkeypatch)
    ev = threading.Event(); ev.set()
    r = jobspy_search.run_search(JobSearchRequest(search_term="x", sites=["indeed"], company_sites=[{"url": "https://a.test"}]), "u", ev)
    assert r.cancelled and calls["boards"] == []


def test_should_stop_halts_between_company_sites(monkeypatch):
    seen = []
    monkeypatch.setattr(company_site_search, "_search_one_site", lambda site, **kw: (seen.append(site.url) or [], [], [], 0))
    stop = iter([False, True])
    company_site_search.search_company_sites(
        [company_site_search.CompanySiteRequest(url="https://a.test"), company_site_search.CompanySiteRequest(url="https://b.test")],
        "x", "u", should_stop=lambda: next(stop))
    assert seen == ["https://a.test"]
