"""Every page ships the light/dark theme hook, and the light palette defines every colour token."""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / 'static'


def tokens(block):
    return set(re.findall(r'(--[\w-]+):', block))


def test_pages_load_theme_script_before_styles_and_define_light_palette():
    for name in ('index.html', 'login.html', 'setup.html'):
        html = (STATIC / name).read_text()
        assert html.index('/static/theme.js') < html.index('<style>'), name
        dark = re.search(r':root\{([^}]*)\}', html).group(1)
        light = re.search(r':root\[data-theme="light"\]\{([^}]*)\}', html).group(1)
        missing = {t for t in tokens(dark) if t != '--radius'} - tokens(light)
        assert not missing, (name, missing)


def test_theme_js_persists_choice_and_falls_back_to_system():
    js = (STATIC / 'theme.js').read_text()
    assert "'jshq-theme'" in js and 'prefers-color-scheme' in js and 'window.setTheme' in js
