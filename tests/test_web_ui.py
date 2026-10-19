"""The operator console uses the NexApp shell, theme tokens, and theme script."""

import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
PAGES = ("index.php", "inputs.php", "outputs.php", "monitor.php", "effects.php", "metrics.php", "services.php")
PAGE_SCRIPTS = {
    "index.php": ["assets/theme.js", "assets/console.js"],
    "inputs.php": ["assets/theme.js", "assets/console.js"],
    "outputs.php": ["assets/theme.js", "assets/console.js"],
    "monitor.php": ["assets/theme.js", "assets/console.js", "assets/preview.js"],
    "effects.php": ["assets/theme.js", "assets/console.js", "assets/effects.js"],
    "metrics.php": ["assets/theme.js", "assets/console.js", "assets/metrics.js"],
    "services.php": ["assets/theme.js", "assets/console.js", "assets/services.js"],
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


def _page_html(name: str) -> str:
    text = (WEB / name).read_text(encoding="utf-8")
    return re.sub(r"<\?php.*?\?>\s*", "", text, count=1, flags=re.S)


def test_pages_share_the_nexapp_shell():
    version = _version()
    for name in PAGES:
        text = _page_html(name)
        parser = PageParser()
        parser.feed(text)
        assert parser.html_attrs.get("data-theme-pref") == "system"
        assert "nexapp-theme" in text
        assert "assets/app.css" in parser.links
        assert "assets/encoder.css" in parser.links
        assert parser.scripts == PAGE_SCRIPTS[name]
        assert 'href="metrics.php"' in text and 'href="services.php"' in text
        assert 'href="effects.php"' in text
        for class_name in SHELL:
            assert class_name in parser.classes
        assert parser.active_hrefs == [name]
        assert f"v{version}" in text


def test_apache_serves_pages_and_php_forwards_admin_actions():
    for name in PAGES:
        assert not (WEB / name.replace(".php", ".html")).exists()
    assert (WEB / "cg" / "bug.html").is_file()
    feed = (WEB / "includes" / "feed.php").read_text(encoding="utf-8")
    assert "http://127.0.0.1:8788" in feed
    assert "NexEncoder" in feed
    for script in ("config.php", "bars.php", "service.php", "preset.php", "detect.php", "support.php", "journal-clear.php"):
        text = (WEB / "admin" / script).read_text(encoding="utf-8")
        assert "includes/feed.php" in text
        assert "nexencoder_admin_request" in text
    site = (ROOT / "config" / "apache-nexencoder.conf").read_text(encoding="utf-8")
    assert "DocumentRoot /var/lib/nexencoder/www" in site
    assert "<VirtualHost *:80>" in site
    assert "ProxyPass" not in site
    assert "8780" not in site
    listen = (ROOT / "config" / "apache-nexencoder-listen.conf").read_text(encoding="utf-8")
    assert "Listen " not in listen
    js = (WEB / "assets" / "console.js").read_text(encoding="utf-8")
    assert "/admin/config.php" in js and "/admin/bars.php" in js and "/admin/detect.php" in js
    assert "/api/" not in js
    services = (WEB / "assets" / "services.js").read_text(encoding="utf-8")
    assert "/admin/service.php" in services and "/admin/services.php" in services
    assert "/live/log.php" in services
    assert "/admin/journal.php" not in services and "/admin/support.php" in services
    assert "/admin/journal-clear.php" in services
    assert "X-Journal-Clear" in services
    assert "Journal dumped and cleared" in services
    assert "delete the host journal" in services
    assert 'method: "POST"' in services
    page = (WEB / "services.php").read_text(encoding="utf-8")
    assert 'id="copy-journal"' in page and 'id="download-bundle"' in page
    assert 'id="dump-clear"' in page and "Dump and clear" in page
    assert 'class="support-bundle-bar"' in page
    assert page.index("support-bundle-bar") < page.index('id="unit-list"')
    assert 'id="dump-journal"' not in page
    assert "Download zip" in page
    assert 'value="36"' in page and 'value="all"' in page
    effects = (WEB / "assets" / "effects.js").read_text(encoding="utf-8")
    assert "/admin/preset.php" in effects and "/admin/recall.php" in effects
    assert "/live/still.php" in effects
    preview = (WEB / "assets" / "preview.js").read_text(encoding="utf-8")
    assert "/api/" not in preview
    monitor = (WEB / "monitor.php").read_text(encoding="utf-8")
    assert 'id="sideband-A"' in monitor and 'id="sideband-B"' in monitor
    assert 'id="sideband-log"' not in monitor
    assert 'getElementById("sideband-"' in preview
    assert "track.onended" in preview and "players[id] !== pc" in preview
    inputs = (WEB / "inputs.php").read_text(encoding="utf-8")
    assert 'id="lock-A"' in inputs and 'id="lock-B"' in inputs
    assert 'id="restart-A"' in inputs and 'id="restart-B"' in inputs
    assert "nexencoder-channel@" in js
    assert "/live/decklink.php" in (WEB / "assets" / "console.js").read_text(encoding="utf-8")
    metrics = (WEB / "assets" / "metrics.js").read_text(encoding="utf-8")
    assert "/api/" not in metrics
    assert "Field rate" in metrics and "Raster" in metrics
    assert "color-scheme: normal" in (WEB / "assets" / "encoder.css").read_text(encoding="utf-8")
    assert "color-scheme: normal" in (WEB / "cg" / "bug.html").read_text(encoding="utf-8")
