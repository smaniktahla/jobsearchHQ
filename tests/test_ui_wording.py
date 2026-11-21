"""User-facing wording on the Search page, Settings and the setup wizard."""
import re
from pathlib import Path

HTML = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text()


def test_location_placeholder_has_no_stray_escape():
    m = re.search(r'id="search-location"[^>]*placeholder="([^"]*)"', HTML)
    assert m and m.group(1) == "City, State"


def test_auto_score_label_does_not_claim_a_specific_vendor():
    m = re.search(r'id="search-autoscore"[^>]*>\s*([^<]*)</label>', HTML)
    assert m and "Claude" not in m.group(1) and "selected AI provider" in m.group(1)


def test_no_w2_sniper_jargon_is_shown_to_users():
    # the stored lane value stays w2_sniper; only display text changes
    visible = re.sub(r"w2_sniper", "", HTML)
    assert "Sniper" not in visible


def test_dead_preferred_keywords_field_is_gone():
    assert "cfg-keywords" not in HTML and "wz-keywords" not in HTML and "Preferred Keywords" not in HTML


def test_settings_calls_them_job_titles():
    assert "Job titles to search" in HTML and "Search Terms (one per line)" not in HTML


def test_description_checkbox_says_it_fetches_full_descriptions():
    m = re.search(r'id="search-linkedin-desc"[^>]*>\s*([^<]*)</label>', HTML)
    assert m and "full descriptions" in m.group(1) and "LinkedIn" not in m.group(1)
