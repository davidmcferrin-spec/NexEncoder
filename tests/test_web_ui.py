"""The operator console uses the NexApp shell, theme tokens, and theme script."""

import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
PAGES = ("index.html", "inputs.html", "outputs.html", "monitor.html", "metrics.html", "services.html")
PAGE_SCRIPTS = {
    "index.html": ["assets/theme.js", "assets/console.js"],
    "inputs.html": ["assets/theme.js", "assets/console.js"],
    "outputs.html": ["assets/theme.js", "assets/console.js"],
    "monitor.html": ["assets/theme.js", "assets/console.js", "assets/preview.js"],
    "metrics.html": ["assets/theme.js", "assets/console.js", "assets/metrics.js"],
    "services.html": ["assets/theme.js", "assets/console.js", "assets/services.js"],
}
SHELL = ("page-shell", "shell-top", "shell-sidebar", "shell-main", "shell-footer")


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.html_attrs = {}
        self.links = []
        self.scripts = []
        self.classes = []
        self.active_hrefs = []

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag == "html":
            self.html_attrs = attr
        if tag == "link" and attr.get("href"):
            self.links.append(attr["href"])
        if tag == "script" and attr.get("src"):
            self.scripts.append(attr["src"])
        classes = attr.get("class", "").split()
        self.classes.extend(classes)
        if tag == "a" and "is-active" in classes:
            self.active_hrefs.append(attr.get("href"))


def _version():
    text = (ROOT / "nexencoder" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    assert match, "package version missing"
    return match.group(1)


def test_theme_kit_is_the_nexapp_sheet():
    theme = (WEB / "assets" / "nexapp-theme.css").read_text(encoding="utf-8")
    app = (WEB / "assets" / "app.css").read_text(encoding="utf-8")
    script = (WEB / "assets" / "theme.js").read_text(encoding="utf-8")
    assert "--nx-accent" in theme and ".nx-btn" in theme
    assert app.lstrip().startswith('@import url("nexapp-theme.css")')
    assert ".page-shell" in app and ".portal-hero" in app and ".btn-text" in app
    assert "nexapp-theme" in script and "NexAppTheme" in script


def test_pages_share_the_nexapp_shell():
    version = _version()
    for name in PAGES:
        text = (WEB / name).read_text(encoding="utf-8")
        parser = PageParser()
        parser.feed(text)
        assert parser.html_attrs.get("data-theme-pref") == "system"
        assert "nexapp-theme" in text
        assert "assets/app.css" in parser.links
        assert "assets/encoder.css" in parser.links
        assert parser.scripts == PAGE_SCRIPTS[name]
        assert 'href="metrics.html"' in text and 'href="services.html"' in text
        for class_name in SHELL:
            assert class_name in parser.classes
        assert parser.active_hrefs == [name]
        assert f"v{version}" in text
