"""python-jobspy >= 1.5: per-board errors/warnings are surfaced, and the renamed description flag is used."""
import pandas as pd

import jobspy_search


def df_with_attrs(errors=None, warnings=None, rows=0):
    df = pd.DataFrame([{"title": f"t{i}", "company": "c", "site": "indeed", "job_url": f"http://x/{i}",
                        "description": "d" * 300} for i in range(rows)])
    df.attrs = {"errors": errors or {}, "warnings": warnings or {}}
    return df


def test_board_issues_reports_errors_and_warnings():
    out = jobspy_search._board_issues(df_with_attrs({"google": "no job data returned"}, {"linkedin": ["a 429", "b 429"]}))
    assert {"error": "google: no job data returned"} in out
    assert {"error": "linkedin: 2 warning(s), e.g. a 429"} in out


def test_board_issues_tolerates_missing_attrs():
    assert jobspy_search._board_issues(None) == []
    assert jobspy_search._board_issues(pd.DataFrame()) == []


def test_zero_row_search_still_reports_which_board_failed(monkeypatch):
    seen = {}
    def fake_scrape(**kwargs):
        seen.update(kwargs)
        return df_with_attrs({"google": "no job data returned"})
    monkeypatch.setattr(jobspy_search, "scrape_jobs", fake_scrape)
    req = jobspy_search.JobSearchRequest(search_term="t", location="Washington, DC", sites=["google"])
    result = jobspy_search._run_board_search(req, "u1")
    assert result.total_scraped == 0
    assert result.errors == [{"error": "google: no job data returned"}]
    # 1.2+ renamed the flag; the old name is deprecated
    assert "fetch_description" in seen and "linkedin_fetch_description" not in seen
    assert seen["fetch_description"] is True


def test_description_flag_follows_the_users_checkbox(monkeypatch):
    seen = {}
    monkeypatch.setattr(jobspy_search, "scrape_jobs", lambda **kw: seen.update(kw) or df_with_attrs())
    req = jobspy_search.JobSearchRequest(search_term="t", sites=["indeed"], linkedin_fetch_description=False)
    jobspy_search._run_board_search(req, "u1")
    assert seen["fetch_description"] is False
