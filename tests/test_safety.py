"""Security guard: blocked sites never open (in any browser), sensitive boxes are never typed into, Edge is locked down."""
import json

import pytest

from jarvis.browser import BrowserError, EdgeController
from jarvis.safety import SiteGuard, sensitive_field
from jarvis.tools import Reminders, ToolBox


@pytest.fixture
def guard(tmp_path):
    folder = tmp_path / "blocklist"
    folder.mkdir()
    (folder / "lists.txt").write_text("# test list\n0.0.0.0 evil-phish.example\n127.0.0.1 malware.test\n"
                                      "scam-prize.example\n0.0.0.0 casino-win.example\n", encoding="utf-8")
    return SiteGuard(folder, allow=["myschool.example"])


@pytest.mark.parametrize("url, blocked", [
    ("https://evil-phish.example/login", True),
    ("http://login.evil-phish.example/x", True),                 # subdomains of a blocked site too
    ("scam-prize.example", True),
    ("https://www.youtube.com/watch?v=1", False),
    ("https://google.com@evil-phish.example/", True),            # the "real address hidden after @" trick
    ("https://xn--pple-43d.com/", True),                         # look-alike letters
    ("https://thepiratebay.org/search", True),                   # piracy
    ("javascript:alert(1)", True), ("file:///C:/Users/HP/Documents/secret.txt", True),
    ("http://203.0.113.9/download.exe", True),                   # bare IP
    ("http://localhost:8600/", False), ("https://myschool.example/", False),   # allowlisted
])
def test_site_checks(guard, url, blocked):
    assert (guard.check(url) is not None) is blocked, guard.check(url)


@pytest.mark.parametrize("label, type_, auto, name, sensitive", [
    ("Password", "password", "", "", True), ("Enter OTP", "text", "one-time-code", "", True),
    ("Card number", "text", "cc-number", "", True), ("CVV", "text", "", "cvv", True),
    ("Aadhaar number", "text", "", "", True), ("PAN", "text", "", "pan", True), ("UPI PIN", "tel", "", "", True),
    ("Bank account number", "text", "", "", True), ("IFSC code", "text", "", "", True),
    ("Full name", "text", "name", "", False), ("Email address", "email", "email", "", False),
    ("Telephone", "tel", "tel", "custtel", False), ("Company", "text", "", "company", False),
])
def test_sensitive_fields(label, type_, auto, name, sensitive):
    assert sensitive_field(label, type_, auto, name) is sensitive


def test_blocked_site_is_refused_before_loading(guard, tmp_path):
    edge = EdgeController(tmp_path / "edge-agent", guard=guard)
    edge.ensure_running = lambda: (_ for _ in ()).throw(AssertionError("must not even start Edge"))
    with pytest.raises(BrowserError, match="I blocked evil-phish.example"):
        edge.open_url("https://evil-phish.example/login")
    assert edge.blocked and edge.blocked[-1][1].startswith("https://evil-phish.example")


def test_blocked_in_every_browser(settings, tmp_path, guard):
    edge = EdgeController(tmp_path / "edge-agent", guard=guard)
    opened = []
    tb = ToolBox(settings, Reminders(tmp_path / "r.json"), launcher=opened.append, browser=opened.append,
                 opener=opened.append, runner=lambda a: "ok", edge=edge)
    settings.update({"approved_apps": {**settings.get("approved_apps"), "chrome": "chrome.exe"}})
    r = tb.call("open_website", {"url": "https://evil-phish.example", "browser": "chrome"})
    assert r.text.startswith("I blocked") and not opened


def test_typing_into_a_password_box_is_refused(tmp_path):
    edge = EdgeController(tmp_path / "edge-agent")
    edge.active_page = lambda: {"id": "p"}
    edge._focus_window = lambda p: None
    sent = []
    edge._eval = lambda page, js: {"how": "field", "kind": "password", "name": "pwd", "auto": "current-password", "label": ""}
    edge._cdp = lambda page, calls: sent.extend(calls) or [{}]
    with pytest.raises(BrowserError, match="never type into those"):
        edge.type_text("hunter2")
    assert not sent                                                    # nothing reached the page


def test_edge_profile_is_locked_down(tmp_path):
    edge = EdgeController(tmp_path / "edge-agent")
    edge._lock_out_accounts()
    p = json.loads((tmp_path / "edge-agent" / "Default" / "Preferences").read_text(encoding="utf-8"))
    perms = p["profile"]["default_content_setting_values"]
    for k in ("geolocation", "media_stream_camera", "media_stream_mic", "clipboard", "notifications", "popups",
              "automatic_downloads", "usb_guard", "file_system_write_guard"):
        assert perms[k] == 2, k                                        # 2 = blocked
    assert p["credentials_enable_service"] is False and p["autofill"]["credit_card_enabled"] is False
    assert p["download"]["prompt_for_download"] is True and p["signin"]["allowed"] is False


def test_lists_parse_hosts_and_plain_formats():
    text = "0.0.0.0 a.example\n127.0.0.1 b.example # note\nc.example\n0.0.0.0 0.0.0.0\nlocalhost\n# comment"
    assert SiteGuard.parse(text) == {"a.example", "b.example", "c.example"}
