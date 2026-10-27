"""Windows settings pages, the Recycle Bin and This PC (fixed tables, literal addresses only), the honest 'turn on wifi'
answer, and the launch hardening for links that go through cmd.exe. startfile and the launcher are mocked."""
import re

import pytest

from jarvis.browser import BrowserError
from jarvis.conversation import Conversation, route_all
from jarvis.tools import (SETTINGS_PAGES, SHELL_FOLDERS, Reminders, ToolBox, _cmd_safe, settings_key)

from conftest import FakeLLM
from test_conv_iterations import FakeEdge, say


@pytest.fixture
def tb(settings, tmp_path):
    settings.update({"browser": "edge", "save_conversations": False})
    box = ToolBox(settings, Reminders(tmp_path / "r.json"), launcher=lambda a: box.launched.append(a),
                  browser=lambda u: box.launched.append(u), opener=lambda p: box.opened.append(p), runner=lambda a: "ok",
                  edge=FakeEdge())
    box.opened, box.launched = [], []
    return box


# ======================= the table =======================
def test_every_settings_address_is_a_literal_ms_settings_uri():
    assert len(SETTINGS_PAGES) >= 14
    for key, uri in SETTINGS_PAGES.items():
        assert uri.startswith("ms-settings:") and not re.search(r"[\s&?=/\\%]", uri[len("ms-settings:"):]), (key, uri)
    for key in ("wifi", "bluetooth", "display", "sound", "battery", "storage", "windows update", "default apps",
                "night light", "privacy", "network", "notifications", "apps", "about"):
        assert key in SETTINGS_PAGES
    assert SETTINGS_PAGES["battery"] == "ms-settings:batterysaver"
    assert all(v.startswith("shell:") and " " not in v for v in SHELL_FOLDERS.values())


@pytest.mark.parametrize("said,key", [("display", "display"), ("blue tooth", "bluetooth"), ("bluetooth", "bluetooth"),
                                      ("wi-fi", "wifi"), ("wi fi", "wifi"), ("wifi", "wifi"), ("the sound", "sound"),
                                      ("windows update", "windows update"), ("update", "windows update"),
                                      ("windows updates", "windows update"), ("default apps", "default apps"),
                                      ("night light", "night light"), ("battery saver", "battery"), ("battery", "battery"),
                                      ("notification", "notifications"), ("privicy", "privacy"), ("airplane", "airplane mode")])
def test_settings_names_are_matched_exactly_or_closely(said, key):
    assert settings_key(said) == key


@pytest.mark.parametrize("said", ["registry", "task manager", "control panel", "", "regedit", "group policy", "cmd",
                                  "display & calc", "powershell", "security", "ms-settings:display"])
def test_things_that_are_not_in_the_table_are_not_matched(said):
    assert settings_key(said) is None


# ======================= phrases =======================
@pytest.mark.parametrize("text,page", [("open display settings", "display"), ("open the display settings", "display"),
                                       ("open blue tooth settings", "bluetooth"), ("take me to sound settings", "sound"),
                                       ("show me wifi settings", "wifi"), ("go to windows update settings", "windows update"),
                                       ("open my privacy settings", "privacy"), ("show battery settings", "battery"),
                                       ("Open Night Light settings.", "night light")])
def test_open_settings_phrases(settings, text, page):
    assert route_all(text, settings, None) == [("open_settings", {"page": page})]


@pytest.mark.parametrize("text", ["open task manager", "open registry settings", "open control panel", "open regedit",
                                  "open cmd settings", "open group policy settings"])
def test_system_tools_are_still_not_opened(settings, tb, text):
    steps = route_all(text, settings, tb.find_app)
    assert not steps or steps[0][0] not in ("open_settings", "open_app", "run_command")
    assert tb.opened == [] and tb.launched == []


@pytest.mark.parametrize("text,page", [("turn on wifi", "wifi"), ("turn off wifi", "wifi"), ("turn on bluetooth", "bluetooth"),
                                       ("turn wifi off", "wifi"), ("switch off airplane mode", "airplane mode"),
                                       ("turn on airplane mode", "airplane mode"), ("toggle the night light", "night light"),
                                       ("turn on battery saver", "battery")])
def test_flipping_a_setting_opens_its_page_with_an_honest_sentence(settings, text, page):
    assert route_all(text, settings, None) == [("open_settings", {"page": page, "flip": True})]


def test_other_wifi_talk_is_still_the_old_cant_answer(settings):
    steps = route_all("what's my wifi password", settings, None)
    assert steps and steps[0][0] == "cant"


def test_recycle_bin_and_this_pc(settings):
    assert route_all("open the recycle bin", settings, None) == [("open_shell_folder", {"name": "recycle bin"})]
    assert route_all("open this pc", settings, None) == [("open_shell_folder", {"name": "this pc"})]
    assert route_all("show my computer", settings, None) == [("open_shell_folder", {"name": "my computer"})]
    assert route_all("open the downloads folder", settings, None)[0][0] == "open_file"       # the existing, confirmed path


# ======================= the tools =======================
def test_open_settings_opens_only_the_literal_address(tb):
    assert tb.call("open_settings", {"page": "display"}).text == "Opening Display settings."
    assert tb.opened == ["ms-settings:display"]
    assert tb.call("open_settings", {"page": "windows update"}).text == "Opening Windows Update settings."
    assert tb.opened[-1] == "ms-settings:windowsupdate"


def test_flip_says_it_cannot_flip_it(tb):
    r = tb.call("open_settings", {"page": "wifi", "flip": True})
    assert r.text == "I can't flip it, but I opened Wi-Fi settings for you." and tb.opened == ["ms-settings:network-wifi"]
    assert tb.call("open_settings", {"page": "bluetooth", "flip": True}).text == \
        "I can't flip it, but I opened Bluetooth settings for you."


def test_no_text_from_the_person_ever_reaches_the_address(tb):
    for page in ("display&calc.exe", "display ms-settings:privacy", "../../windows", "%windir%", "display;whoami", "x" * 500,
                 "ms-settings:display?x=1", "registry", ""):
        tb.call("open_settings", {"page": page})
    assert all(o in SETTINGS_PAGES.values() for o in tb.opened)
    r = tb.call("open_settings", {"page": "registry"})
    assert "don't have a settings page called registry" in r.text and "Display" in r.text


def test_open_shell_folder_uses_the_two_literals(tb):
    tb.call("open_shell_folder", {"name": "the recycle bin"})
    tb.call("open_shell_folder", {"name": "this pc"})
    assert tb.opened == ["shell:RecycleBinFolder", "shell:MyComputerFolder"]
    tb.call("open_shell_folder", {"name": "c:\\windows\\system32"})
    assert len(tb.opened) == 2


def test_the_conversation_says_it_honestly(tb, settings):
    conv = Conversation(settings, FakeLLM(["no"]), tb)
    assert say(conv, "turn on wifi") == "I can't flip it, but I opened Wi-Fi settings for you."
    assert say(conv, "open display settings") == "Opening Display settings."
    assert tb.opened == ["ms-settings:network-wifi", "ms-settings:display"]


# ======================= launch hardening =======================
@pytest.mark.parametrize("url,ok", [("https://example.com/", True), ("https://example.com/a?b=1", True),
                                    ("https://example.com/a%20b", True), ("https://example.com/?q=caf%C3%A9", True),
                                    ("https://example.com/?a=1&calc", False), ("https://example.com/a|b", False),
                                    ("https://example.com/a^b", False), ("https://example.com/<x>", False),
                                    ('https://example.com/"x', False), ("https://example.com/a b", False),
                                    ("https://example.com/%PATH%", False), ("https://example.com/%", False),
                                    ("https://example.com/a\tb", False), ("https://example.com/a\nb", False)])
def test_what_may_go_to_cmd_start(url, ok):
    assert _cmd_safe(url) is ok


def test_an_ampersand_link_never_reaches_cmd(tb):
    with pytest.raises(BrowserError, match="won't hand to the command line"):
        tb._browse_in("https://example.com/?a=1&calc", "chrome")
    assert tb.launched == []
    r = tb.call("open_website", {"url": "https://example.com/?a=1&calc.exe", "browser": "chrome"})
    run = r.run_confirmed() if r.needs_confirmation else r
    assert "command line" in run.text and tb.launched == []


def test_a_clean_link_still_opens_in_chrome(tb):
    assert tb._browse_in("https://example.com/a%20b?x=1", "chrome") == "Chrome"
    assert tb.launched == [["cmd", "/c", "start", "", "chrome", "https://example.com/a%20b?x=1"]]


def test_an_exe_target_never_goes_through_cmd_so_it_is_not_blocked(tb, settings):
    settings.update({"approved_apps": {"chrome": r"C:\Program Files\Google\Chrome\Application\chrome.exe"}})
    assert tb._browse_in("https://example.com/?a=1&b=2", "chrome") == "Chrome"
    assert tb.launched[-1][0].endswith("chrome.exe") and tb.launched[-1][1] == "https://example.com/?a=1&b=2"


def test_a_search_with_special_characters_is_encoded_not_refused(tb):
    r = tb.call("web_search", {"query": "fish & chips | \"best\" 100%", "browser": "chrome"})
    assert r.text.startswith("Searching") and tb.launched
    url = tb.launched[-1][-1]
    assert _cmd_safe(url) and "&chips" not in url and "%26" in url
