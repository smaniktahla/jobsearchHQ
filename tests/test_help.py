"""Contextual help drawer: every page has plain-English help, links resolve, nothing personal, drawer is wired in."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text()
HELP = ROOT / "static" / "help"


def registry():
    m = re.search(r"const HELP_TOPICS=\{(.*?)\};", HTML, re.S)
    assert m, "HELP_TOPICS registry not found"
    return re.findall(r"'([a-z-]+)':", m.group(1))


def files():
    return {p.stem: p.read_text() for p in HELP.glob("*.html")}


def test_every_topic_has_a_real_help_file_and_no_orphans():
    topics, found = registry(), files()
    assert len(topics) == len(set(topics))
    for t in topics:
        assert t in found, f"missing static/help/{t}.html"
        body = found[t]
        assert body.startswith("<h2>") and "<h3>" in body and len(body) > 400, t
    assert set(found) == set(topics), f"help files without a topic: {set(found) - set(topics)}"


def test_every_page_and_settings_page_has_help():
    topics = set(registry())
    views = set(re.findall(r'id="view-([a-z-]+)"', HTML)) - {"settings"}
    sx = set(re.findall(r'id="sx-([a-z-]+)"', HTML)) - {"nav", "nav-admin"}
    assert views <= topics, f"pages without help: {views - topics}"
    assert {f"settings-{x}" for x in sx} <= topics, f"settings pages without help: {sx}"


def test_help_links_point_at_real_topics():
    topics = set(registry()) | {"index"}
    for name, body in files().items():
        for target in re.findall(r'data-goto="([a-z-]+)"', body):
            assert target in topics, f"{name}: link to unknown topic {target}"


def test_help_is_static_text_only():
    for name, body in files().items():
        assert not re.search(r"<script|<iframe|<img|<form|<input|\son[a-z]+\s*=|javascript:", body, re.I), name
        assert not re.search(r'(href|src)="https?:', body, re.I), f"{name}: no external links or resources"


def test_help_is_plain_english():
    jargon = re.compile(r"\b(cron|curl|endpoint|json|regex|docker|container|ssh|localhost|token|oidc|ats|smtp|api(?! key))\b", re.I)
    for name, body in files().items():
        text = re.sub(r"<[^>]+>", " ", body)
        hit = jargon.search(text)
        assert not hit, f"{name}: technical term {hit.group(0)!r}"


def test_help_contains_no_personal_data():
    for name, body in files().items():
        assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", body), f"{name}: email address"
        assert not re.search(r"\b\d{3}[-. ]\d{3}[-. ]\d{4}\b", body), f"{name}: phone number"
        assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", body), f"{name}: IP address"


def test_help_uses_the_words_the_screen_uses():
    f = files()
    assert "Resume Lane" in f["tracker"] and "Your score" in f["detail"] and "Score history" in f["detail"]
    assert "Job titles to search" in f["settings-automation"] and "Automatically score new jobs" in f["settings-automation"]
    assert "Test Fast Provider" in f["settings-ai"] and "Send Test Email" in f["settings-email"]
    for label in ("Test Fast Provider", "Send Test Email", "Automatically score new jobs", "Job titles to search", "Save my score"):
        assert label in HTML, f"help mentions {label!r} but the page no longer has it"


def test_drawer_is_wired_into_the_page():
    for needle in ('id="help-drawer"', 'role="complementary"', 'id="help-body"', 'id="help-feedback"', 'id="nav-help"',
                   'id="help-topbar-btn"', 'onclick="helpToggle()"', "All help topics"):
        assert needle in HTML, needle
    assert "showView=function(n){_helpShowView(n);helpUpdate()}" in HTML       # follows page changes
    assert "applySettingsPage=function(){_helpApplySP" in HTML                 # and settings sub-pages
    assert "e.key==='Escape'" in HTML and "e.key==='?'" in HTML                # keyboard
    assert "(min-width:1100px)" in HTML and "body.help-open .main{margin-right:380px;" in HTML   # pushes content on wide screens
    assert "localStorage" in HTML and "try{if(v===undefined)return localStorage" in HTML          # storage guarded
    assert "prefers-reduced-motion" in HTML
    nav = re.search(r"\.nav-item-help\{([^}]*)\}", HTML).group(1)
    assert "color:var(--accent)" in nav and "color-mix" in nav            # Help is tinted with the accent so it is easy to spot
    assert "d.style.transition='none';helpOpen(null,true)" in HTML       # restoring 'open' does not animate
    assert "max-width:92vw" in HTML and "overflow-x:auto}" in HTML      # phone keeps a tappable strip of page; wide tables scroll


def test_help_text_is_inserted_as_dom_not_built_from_user_input():
    js = HTML[HTML.index("// ── Contextual help"):]
    assert "template" in js and "t.innerHTML=html" in js                 # trusted static file only
    assert "textContent" in js
    assert "innerHTML=" not in js.replace("t.innerHTML=html", "")        # nothing else uses innerHTML
