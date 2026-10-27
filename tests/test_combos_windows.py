"""Keyboard shortcuts and window control: the fixed shortcut list, modifier handling, the Windows-key rule, window verbs,
and the phrases. A fake user32 and a fake desktop: nothing real is pressed or moved."""
import pytest

from gate_fakes import FakeCtrl, make_desktop
from jarvis import desktop as desktop_mod
from jarvis.browser import BrowserError, EdgeController
from jarvis.conversation import Conversation, grounded, route_all
from jarvis.desktop import (ALLOWED_COMBOS, CONFIRM_COMBOS, Desktop, DesktopError, PRESSABLE_COMBOS, key_vk,
                            normalize_combo)
from jarvis.gate import Action, Gate
from jarvis.tools import Reminders, ToolBox

from conftest import FakeLLM
from test_conv_iterations import FakeEdge, say
from test_desktop_control import FakeDesktop

WINDOWS = {1: ("notepad.exe", "Untitled - Notepad"), 2: ("cmd.exe", "Command Prompt"),
           3: ("windowsterminal.exe", "Windows PowerShell")}


@pytest.fixture
def rig(monkeypatch, settings):
    gate = Gate(settings, None, strict=True)
    d, u = make_desktop(monkeypatch, gate, WINDOWS)
    monkeypatch.setattr(Desktop, "_capture_excluded", staticmethod(lambda h: False))
    u.ShowWindow = lambda h, n: u.calls.append(("show", getattr(h, "value", h), n))
    u.PostMessageW = lambda h, msg, w, l: u.calls.append(("post", getattr(h, "value", h), msg, w))
    return d, u, gate


def keys(u):
    """(virtual key, is_up) for every key event, in order."""
    return [(c[1][0], bool(c[1][2] & 2)) for c in u.sent("key")]


CTRL, SHIFT, ALT, WIN = 0x11, 0x10, 0x12, 0x5B


# ======================= the shortcut list and the parser =======================
@pytest.mark.parametrize("said,want", [("ctrl+s", "ctrl+s"), ("Control S", "ctrl+s"), ("ctrl plus shift plus t", "ctrl+shift+t"),
                                       ("shift ctrl t", "ctrl+shift+t"), ("ALT+F4", "alt+f4"), ("f5", "f5"),
                                       ("alt left", "alt+left"), ("control tab", "ctrl+tab"), ("alt left arrow", "alt+left"),
                                       ("windows r", "win+r"), ("ctrl alt delete", "ctrl+alt+delete")])
def test_the_parser_spells_a_shortcut_one_way(said, want):
    assert normalize_combo(said) == want


@pytest.mark.parametrize("said", ["", "s", "enter", "ctrl", "ctrl+", "ctrl+ctrl", "banana split", "ctrl+shift", "ctrl s t",
                                  "ctrl+f13x", "+s"])
def test_the_parser_rejects_what_is_not_a_shortcut(said):
    assert normalize_combo(said) is None


def test_the_allowed_list_is_exactly_the_documented_one():
    assert ALLOWED_COMBOS == frozenset({"ctrl+s", "ctrl+c", "ctrl+v", "ctrl+x", "ctrl+z", "ctrl+y", "ctrl+a", "ctrl+f", "ctrl+n",
                                        "ctrl+t", "ctrl+w", "ctrl+p", "ctrl+tab", "ctrl+shift+t", "alt+left", "alt+right",
                                        "f5", "f11"})
    assert CONFIRM_COMBOS == frozenset({"ctrl+w", "alt+f4", "ctrl+p", "ctrl+shift+delete", "ctrl+x", "ctrl+shift+n"})
    assert not any("win" in c.split("+") for c in PRESSABLE_COMBOS)
    for forbidden in ("win+r", "win+x", "ctrl+alt+delete", "ctrl+shift+escape", "alt+tab", "win+d", "win+l"):
        assert forbidden not in PRESSABLE_COMBOS


def test_key_codes():
    assert key_vk("s") == 0x53 and key_vk("5") == 0x35 and key_vk("f5") == 0x74 and key_vk("f11") == 0x7A
    assert key_vk("tab") == 0x09 and key_vk("left") == 0x25 and key_vk("delete") == 0x2E and key_vk("f13") is None


# ======================= pressing =======================
def test_ctrl_s_presses_ctrl_then_s_then_releases(rig):
    d, u, gate = rig
    assert d.press_combo(1, "ctrl+s") == "ctrl+s"
    assert keys(u) == [(CTRL, False), (0x53, False), (0x53, True), (CTRL, True)]


def test_modifiers_come_up_in_the_reverse_order(rig):
    d, u, gate = rig
    d.press_combo(1, "ctrl+shift+t")
    assert keys(u) == [(CTRL, False), (SHIFT, False), (0x54, False), (0x54, True), (SHIFT, True), (CTRL, True)]


def test_modifiers_are_released_even_when_the_key_press_fails(rig):
    d, u, gate = rig

    def boom(vk):
        raise OSError("keyboard hook went away")
    d._tap = boom
    with pytest.raises(OSError):
        d.press_combo(1, "ctrl+shift+t")
    assert keys(u) == [(CTRL, False), (SHIFT, False), (SHIFT, True), (CTRL, True)]


@pytest.mark.parametrize("combo", ["win+r", "win+x", "ctrl+alt+delete", "ctrl+shift+esc", "alt+tab", "win+d", "ctrl+alt+t",
                                   "ctrl+q", "banana", "ctrl+shift+s"])
def test_forbidden_shortcuts_press_nothing(rig, combo):
    d, u, gate = rig
    with pytest.raises(DesktopError, match="I don't press that one"):
        d.press_combo(1, combo)
    assert u.calls == []


@pytest.mark.parametrize("combo", ["alt+f4", "ctrl+w", "ctrl+p", "ctrl+x", "ctrl+shift+delete", "ctrl+shift+n"])
def test_risky_shortcuts_need_a_yes_and_press_nothing_without_one(rig, combo):
    d, u, gate = rig
    with pytest.raises(DesktopError, match="needs your yes"):
        d.press_combo(1, combo)
    assert u.sent("key") == []
    with gate.scope(confirmed=True):
        d.press_combo(1, combo)
    assert u.sent("key")


def test_alt_f4_with_a_yes_presses_alt_f4(rig):
    d, u, gate = rig
    with gate.scope(confirmed=True):
        d.press_combo(1, "alt+f4")
    assert keys(u) == [(ALT, False), (0x73, False), (0x73, True), (ALT, True)]


def test_shortcuts_are_refused_in_terminals_and_in_the_wrong_foreground(rig):
    d, u, gate = rig
    u.fg = 2
    with pytest.raises(DesktopError, match="Command Prompt"):
        d.press_combo(2, "ctrl+v")
    u.fg = 1
    with pytest.raises(DesktopError):
        d.press_combo(3, "ctrl+c")
    assert u.sent("key") == []


def test_a_window_that_will_not_come_to_the_front_gets_no_keys(rig):
    d, u, gate = rig
    u.fg = 3                                                     # something else keeps the keyboard
    with pytest.raises(DesktopError):
        d.press_combo(1, "ctrl+s")
    assert u.sent("key") == []


def test_paste_into_a_password_box_is_refused(rig, monkeypatch):
    d, u, gate = rig
    from gate_fakes import install_fake_auto
    install_fake_auto(monkeypatch, FakeCtrl(name="Password", aid="pwd", password=True))
    with pytest.raises(DesktopError, match="card/OTP/password"):
        d.press_combo(1, "ctrl+v")
    install_fake_auto(monkeypatch, FakeCtrl(name="Body"))
    d.press_combo(1, "ctrl+v")
    assert u.sent("key")


def test_the_kill_switch_stops_a_shortcut(rig):
    d, u, gate = rig
    gate.kill()
    with pytest.raises(DesktopError):
        d.press_combo(1, "ctrl+s")
    assert u.sent("key") == []


def test_the_gate_itself_refuses_a_bad_combo_and_a_bad_window_verb():
    g = Gate.permissive()
    assert g.authorize(Action("combo", 1, "notepad.exe", "x", key="win+r")).verdict == "deny"
    assert g.authorize(Action("combo", 1, "notepad.exe", "x", key="ctrl+s")).verdict == "allow"
    assert g.authorize(Action("combo", 1, "notepad.exe", "x", key="alt+f4")).verdict == "confirm"
    assert g.authorize(Action("window", 1, "notepad.exe", "x", key="kill")).verdict == "deny"
    assert g.authorize(Action("window", 1, "notepad.exe", "x", key="close")).verdict == "confirm"
    assert g.authorize(Action("window", 1, "notepad.exe", "x", key="minimize")).verdict == "allow"
    assert g.authorize(Action("combo", 2, "cmd.exe", "x", key="ctrl+c")).verdict == "deny"


# ======================= window control =======================
def test_minimise_maximise_restore_use_show_window(rig):
    d, u, gate = rig
    assert d.window_action(1, "minimize") == "Notepad"
    d.window_action(1, "maximize")
    d.window_action(1, "restore")
    assert [c for c in u.calls if c[0] == "show"] == [("show", 1, 6), ("show", 1, 3), ("show", 1, 9)]


def test_window_actions_refuse_blocked_programs(rig):
    d, u, gate = rig
    for hwnd in (2, 3):
        for verb in ("minimize", "maximize", "restore", "snap_left", "switch", "close"):
            with pytest.raises(DesktopError):
                d.window_action(hwnd, verb)
    assert u.calls == []


def test_close_asks_first_and_sends_the_window_its_own_close_request(rig):
    d, u, gate = rig
    with pytest.raises(DesktopError, match="needs your yes"):
        d.window_action(1, "close")
    assert not [c for c in u.calls if c[0] == "post"]
    with gate.scope(confirmed=True):
        d.window_action(1, "close")
    assert [c for c in u.calls if c[0] == "post"] == [("post", 1, 0x0112, 0xF060)]   # WM_SYSCOMMAND, SC_CLOSE: never a kill


def test_snap_uses_the_windows_key_for_exactly_the_fixed_ones(rig):
    d, u, gate = rig
    d.window_action(1, "snap_left")
    assert keys(u) == [(WIN, False), (0x25, False), (0x25, True), (WIN, True)]
    u.calls.clear()
    d.window_action(1, "snap_right")
    assert keys(u) == [(WIN, False), (0x27, False), (0x27, True), (WIN, True)]
    u.calls.clear()
    d.window_action(None, "show_desktop")
    assert keys(u) == [(WIN, False), (0x44, False), (0x44, True), (WIN, True)]


def test_the_windows_key_appears_nowhere_else():
    src = open(desktop_mod.__file__, encoding="utf-8").read()
    assert src.count('["win"]') == 2 and 'win+r' not in src                # snap left/right (one call) and show desktop


def test_unknown_window_verbs_do_nothing(rig):
    d, u, gate = rig
    for verb in ("kill", "terminate", "", "win+r"):
        with pytest.raises(DesktopError):
            d.window_action(1, verb)
    assert u.calls == []


def test_switch_brings_the_window_to_the_front(rig):
    d, u, gate = rig
    assert d.window_action(1, "switch") == "Notepad"


def test_a_window_that_is_gone_is_said_so(rig, monkeypatch):
    d, u, gate = rig
    monkeypatch.setattr(desktop_mod, "window_title", lambda h: "" if h == 99 else WINDOWS[h][1])
    with pytest.raises(DesktopError, match="can't find that window"):
        d.window_action(99, "minimize")


# ======================= ToolBox: shortcuts and windows by phrase =======================
class KeysDesktop(FakeDesktop):
    def __init__(self):
        super().__init__()
        self.windows[6] = ("windowsterminal.exe", "Windows PowerShell")
        self.windows[7] = ("code.exe", "main.py - VS Code")

    def press_combo(self, hwnd, combo):
        self.check_allowed(hwnd)
        self.log.append(("combo", hwnd, combo))
        return combo

    def window_action(self, hwnd, verb):
        if hwnd is not None:
            self.check_allowed(hwnd)
        self.log.append(("window", hwnd, verb))
        return self.describe(hwnd) if hwnd else "the desktop"

    def focused_info(self, hwnd):
        return None


class EdgeWithKeys(FakeEdge):
    def press_combo(self, combo):
        self.log.append(("combo", combo))

    def tab_list(self):
        return []


@pytest.fixture
def tb(settings, tmp_path, monkeypatch):
    monkeypatch.setattr("jarvis.tools.time.sleep", lambda s: None)
    settings.update({"browser": "edge", "save_conversations": False})
    d, edge = KeysDesktop(), EdgeWithKeys()
    box = ToolBox(settings, Reminders(tmp_path / "r.json"), launcher=lambda a: None, browser=lambda u: None,
                  opener=lambda p: None, runner=lambda a: "ok", edge=edge, desktop=d)
    box.d, box.edge_log = d, edge.log
    return box


def test_ctrl_s_goes_to_the_app_you_were_using(tb):
    tb.d.use(1)
    r = tb.call("browser_key", {"key": "ctrl+s"})
    assert r.text.startswith("Pressed ctrl+s (save) in Notepad.") and ("combo", 1, "ctrl+s") in tb.d.log


def test_forbidden_shortcuts_are_refused_before_anything_happens(tb):
    tb.d.use(1)
    for key in ("win+r", "ctrl+alt+delete", "ctrl+shift+esc", "win+x", "alt+tab"):
        assert tb.call("browser_key", {"key": key}).text.startswith("I don't press that one.")
    assert not [e for e in tb.d.log if e[0] == "combo"]


def test_a_risky_shortcut_asks_for_a_whole_sentence_yes(tb):
    tb.d.use(1)
    r = tb.call("browser_key", {"key": "alt+f4"})
    assert r.needs_confirmation and r.risk == "high" and "alt+f4" in r.confirm_prompt
    assert not [e for e in tb.d.log if e[0] == "combo"]
    assert r.verify() is True
    assert r.run_confirmed().text.startswith("Pressed alt+f4")
    assert ("combo", 1, "alt+f4") in tb.d.log


def test_a_risky_shortcut_is_dropped_if_the_window_changed(tb):
    tb.d.use(1)
    r = tb.call("browser_key", {"key": "ctrl+w"})
    tb.d.windows[1] = ("notepad.exe", "Different - Notepad")
    assert r.verify() is False


def test_shortcuts_in_a_terminal_are_refused(tb):
    tb.d.use(4)
    assert tb.call("browser_key", {"key": "ctrl+v"}).text.startswith("I won't click or type in Command Prompt")
    assert not tb.d.log


def test_shortcuts_in_jarvis_edge_use_the_edge_path(tb):
    tb.call("web_search", {"query": "lofi", "site": "youtube"})
    assert tb.call("browser_key", {"key": "f5"}).text == "Pressed f5 (refresh) in Edge."
    assert ("combo", "f5") in tb.edge_log
    r = tb.call("browser_key", {"key": "ctrl+w"})
    assert r.needs_confirmation and ("combo", "ctrl+w") not in tb.edge_log
    assert "find bar" in tb.call("browser_key", {"key": "ctrl+f"}).text


def test_window_phrases_call_window_control_not_browser_click(settings):
    cases = {"press ctrl+s": ("browser_key", {"key": "ctrl+s"}), "save this": ("browser_key", {"key": "ctrl+s"}),
             "copy that": ("browser_key", {"key": "ctrl+c"}), "paste": ("browser_key", {"key": "ctrl+v"}),
             "undo": ("browser_key", {"key": "ctrl+z"}), "select all": ("browser_key", {"key": "ctrl+a"}),
             "find in this page": ("browser_key", {"key": "ctrl+f"}), "print": ("browser_key", {"key": "ctrl+p"}),
             "press control shift t": ("browser_key", {"key": "ctrl+shift+t"}), "press f5": ("browser_key", {"key": "f5"}),
             "minimise this": ("window_control", {"verb": "minimize"}),
             "maximise notepad": ("window_control", {"verb": "maximize", "app": "notepad"}),
             "restore it": ("window_control", {"verb": "restore"}),
             "snap this left": ("window_control", {"verb": "snap_left"}),
             "snap notepad right": ("window_control", {"verb": "snap_right", "app": "notepad"}),
             "show the desktop": ("window_control", {"verb": "show_desktop"}),
             "switch to notepad": ("window_control", {"verb": "switch", "app": "notepad"}),
             "close this window": ("window_control", {"verb": "close"})}
    for text, want in cases.items():
        steps = route_all(text, settings, None)
        assert steps == [want], (text, steps)
        assert steps[0][0] != "browser_click"


def test_the_forbidden_phrases_route_but_are_refused_by_the_tool(settings, tb):
    for text in ("press win r", "press windows x", "press ctrl alt delete", "press control shift escape"):
        steps = route_all(text, settings, None)
        assert steps and steps[0][0] == "browser_key"
        tb.d.use(1)
        assert tb.call(*steps[0]).text.startswith("I don't press that one.")
    assert not [e for e in tb.d.log if e[0] == "combo"]


def test_go_to_still_opens_websites_unless_it_is_a_running_app(settings, tb):
    assert route_all("go to youtube", settings, tb.find_app)[0][0] == "open_website"
    assert route_all("go to notepad", settings, tb.find_app) == [("window_control", {"verb": "switch", "app": "notepad"})]
    assert route_all("switch to chat mode", settings, None)[0][0] == "set_io_mode"
    assert route_all("close this tab", settings, None) == [("browser_nav", {"action": "close"})]
    assert route_all("press enter", settings, None) == [("browser_key", {"key": "enter"})]


def test_minimise_this_acts_on_the_app_you_were_using(tb):
    tb.d.use(1)
    assert tb.call("window_control", {"verb": "minimize"}).text == "Minimised Notepad."
    assert ("window", 1, "minimize") in tb.d.log


def test_restore_it_brings_back_what_was_just_minimised(tb):
    tb.d.use(1)
    tb.call("window_control", {"verb": "minimize"})
    tb.d.use(2)                                                  # you moved on to Spotify
    assert tb.call("window_control", {"verb": "restore"}).text == "Restored Notepad."
    assert ("window", 1, "restore") in tb.d.log


def test_switch_to_terminal_is_refused(tb):
    r = tb.call("window_control", {"verb": "switch", "app": "terminal"})
    assert "I won't click or type in the terminal" in r.text
    assert not [e for e in tb.d.log if e[0] == "window"]
    assert "can't see a nonexistent window" in tb.call("window_control", {"verb": "switch", "app": "nonexistent"}).text


def test_close_window_asks_first(tb):
    tb.d.use(1)
    r = tb.call("window_control", {"verb": "close"})
    assert r.needs_confirmation and r.risk == "high" and "Close Notepad" in r.confirm_prompt
    assert not [e for e in tb.d.log if e[0] == "window"]
    assert "asked Notepad to close" in r.run_confirmed().text and ("window", 1, "close") in tb.d.log


def test_show_the_desktop_and_unknown_verbs(tb):
    assert tb.call("window_control", {"verb": "show_desktop"}).text == "Showing the desktop."
    assert tb.call("window_control", {"verb": "annihilate"}).text == "I don't do that to a window."
    assert tb.call("window_control", {"verb": "minimize"}).text.startswith("I'm not sure which window")


def test_snap_left_names_what_it_snapped(tb):
    tb.d.use(1)
    assert tb.call("window_control", {"verb": "snap_left"}).text == "Snapped Notepad to the left."


# ======================= JARVIS's Edge: what a shortcut can really do from outside =======================
@pytest.fixture
def edge(tmp_path, monkeypatch, settings):
    gate = Gate(settings, None, strict=True)
    e = EdgeController(tmp_path / "profile", port=9999, gate=gate)
    box = type("Box", (), {})()
    box.calls, box.field, box.gate = [], None, gate
    monkeypatch.setattr(e, "active_page", lambda: {"id": "a", "webSocketDebuggerUrl": "ws://x", "url": "http://x.test/",
                                                   "title": "t"})
    monkeypatch.setattr(e, "_eval", lambda page, js: box.field)
    monkeypatch.setattr(e, "_cdp", lambda page, calls, timeout=8.0: box.calls.append(calls) or [{}] * len(calls))
    monkeypatch.setattr(e, "navigate", lambda action: box.calls.append(("nav", action)))
    monkeypatch.setattr(e, "switch_tab", lambda which: box.calls.append(("switch", which)))
    box.edge = e
    return box


def test_edge_navigation_shortcuts_use_the_real_navigation(edge):
    for combo, action in (("alt+left", "back"), ("alt+right", "forward"), ("f5", "reload"), ("ctrl+t", "new tab"),
                          ("ctrl+w", "close")):
        edge.edge.press_combo(combo)
        assert edge.calls[-1] == ("nav", action)
    edge.edge.press_combo("ctrl+tab")
    assert edge.calls[-1] == ("switch", "next")


def test_edge_editing_shortcuts_send_the_edit_command(edge):
    edge.edge.press_combo("ctrl+a")
    down, up = edge.calls[-1]
    assert down[0] == "Input.dispatchKeyEvent" and down[1]["commands"] == ["selectAll"] and down[1]["modifiers"] == 2
    assert up[1]["type"] == "keyUp" and "commands" not in up[1]


def test_edge_refuses_to_paste_into_a_password_box(edge):
    edge.field = {"tag": "input", "type": "password", "name": "pw", "auto": "", "label": ""}
    with pytest.raises(BrowserError, match="never paste"):
        edge.edge.press_combo("ctrl+v")
    assert not edge.calls
    edge.field = {"tag": "input", "type": "text", "name": "q", "auto": "", "label": "Search"}
    edge.edge.press_combo("ctrl+v")
    assert edge.calls and edge.calls[-1][0][1]["commands"] == ["paste"]


def test_edge_is_honest_about_shortcuts_it_cannot_do_from_outside(edge):
    for combo in ("ctrl+p", "ctrl+s", "f11", "ctrl+shift+t", "ctrl+n"):
        with pytest.raises(BrowserError, match="can't press that one inside my Edge window"):
            edge.edge.press_combo(combo)
    assert not edge.calls


def test_the_kill_switch_stops_edge_shortcuts(edge):
    edge.gate.kill()
    with pytest.raises(BrowserError):
        edge.edge.press_combo("f5")
    assert not edge.calls


# ======================= interpreted commands must be rooted in what the person said =======================
def test_grounding_rejects_a_combo_nobody_asked_for():
    assert grounded([("browser_key", {"key": "ctrl+w"})], "press control w")
    assert grounded([("browser_key", {"key": "ctrl+s"})], "save my work")
    assert not grounded([("browser_key", {"key": "ctrl+w"})], "make the thing go away please")
    assert grounded([("browser_key", {"key": "enter"})], "do the thing")              # plain keys were always fine
    assert not grounded([("window_control", {"verb": "close"})], "tidy up my laptop")
    assert grounded([("window_control", {"verb": "minimize"})], "minimise the window")
    assert not grounded([("browser_tabs", {"action": "close_others"})], "clean up")
    assert not grounded([("screen_pick", {"n": 2})], "yes please")


def test_an_interpreted_combo_is_dropped_when_the_words_did_not_ask_for_it(settings, tb):
    class Cloud(FakeLLM):
        async def cloud_only(self, messages, max_tokens=120):
            return "press ctrl+w"
    conv = Conversation(settings, Cloud(["Sure, happy to chat."]), tb)
    tb.d.use(1)
    say(conv, "make the thing vanish and stop all this")
    assert not [e for e in tb.d.log if e[0] == "combo"]
