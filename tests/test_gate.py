"""SafetyGate: the one choke point for clicks, keys and typed characters. Fake clock, fake Windows: nothing real is touched."""
import ast
from pathlib import Path

import pytest

from gate_fakes import FakeCtrl, make_desktop
from jarvis import desktop
from jarvis.audit import Audit
from jarvis.desktop import DesktopError
from jarvis.gate import ABORT, BLOCKED_PROCESSES, Action, Gate

ROOT = Path(__file__).resolve().parents[1] / "backend" / "jarvis"


class Clock:
    t = 100.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def gate(tmp_path, clock):
    return Gate(None, Audit(tmp_path / "actions.log"), clock=clock)


def click(label, ctype="Button", proc="notepad.exe", title="Untitled - Notepad", **kw):
    return Action("click", 1, proc, title, label, ctype=ctype, **kw)


# ---------- windows that are never touched ----------
@pytest.mark.parametrize("proc", ["wt.exe", "powershell_ise.exe", "wscript.exe", "cscript.exe", "mshta.exe", "mstsc.exe",
                                  "systemsettings.exe", "control.exe", "mmc.exe", "searchhost.exe",
                                  "startmenuexperiencehost.exe", "anydesk.exe", "teamviewer.exe", "keepass.exe",
                                  "keepassxc.exe", "bitwarden.exe", "1password.exe", "lastpass.exe", "dashlane.exe",
                                  "nordpass.exe", "enpass.exe", "roboform.exe", "cmd.exe", "regedit.exe"])
def test_blocked_processes_are_denied_for_every_kind(gate, proc):
    assert proc in BLOCKED_PROCESSES
    for kind in ("click", "type", "key", "scroll"):
        assert gate.authorize(Action(kind, 1, proc, "Whatever")).verdict == "deny"


@pytest.mark.parametrize("title", ["Windows Security", "User Account Control", "Credential Manager", "Enter your PIN",
                                   "Windows Hello setup", "New InPrivate tab - Edge", "New Incognito Tab - Chrome"])
def test_secure_and_private_window_titles_are_denied(gate, title):
    assert gate.authorize(click("OK", title=title, proc="msedge.exe")).verdict == "deny"
    assert gate.authorize(Action("type", 1, "chrome.exe", title)).verdict == "deny"


def test_sign_in_and_banking_pages_deny_typing_but_not_reading_clicks(gate):
    for title in ("SBI Net Banking - Google Chrome", "Sign in to your account - Edge", "HDFC Bank - Chrome",
                  "Checkout - Amazon - Edge", "Pay with UPI - Chrome", "Enter OTP - Edge"):
        assert gate.authorize(Action("type", 1, "chrome.exe", title)).verdict == "deny", title
    assert gate.authorize(click("Next", ctype="Hyperlink", proc="chrome.exe", title="SBI Net Banking - Google Chrome")
                          ).verdict == "allow"


def test_file_named_like_a_bank_in_notepad_is_not_a_sign_in_page(gate):
    assert gate.authorize(Action("type", 1, "notepad.exe", "otp notes.txt - Notepad")).verdict == "allow"
    assert gate.authorize(Action("type", 1, "someapp.exe", "Sign in")).verdict == "deny"       # a dialog called Sign in


def test_classifier_errors_fail_closed(gate, monkeypatch):
    assert gate.authorize(Action("type", 1, "notepad.exe", "x"), {"_error": True}).verdict == "deny"
    monkeypatch.setattr(gate, "_static_block", lambda *a: 1 / 0)
    assert gate.window_block("notepad.exe", "x") is not None
    assert gate.authorize(Action("key", 1, "notepad.exe", "x", key="a")).verdict == "deny"


# ---------- secret boxes ----------
@pytest.mark.parametrize("name,aid", [("Card number", ""), ("Enter OTP", ""), ("", "cvv_field"), ("Password", ""),
                                      ("UPI PIN", "")])
def test_secret_looking_boxes_refuse_typing(gate, name, aid):
    d = gate.authorize(Action("type", 1, "notepad.exe", "Form", text_len=6), {"name": name, "automation_id": aid})
    assert d.verdict == "deny" and "yours to type" in d.say


def test_password_flag_and_help_text_refuse_typing(gate):
    assert gate.authorize(Action("type", 1, "notepad.exe", "x"), {"name": "Body", "is_password": True}).verdict == "deny"
    assert gate.authorize(Action("type", 1, "notepad.exe", "x"), {"name": "Box", "help": "Enter your card number"}
                          ).verdict == "deny"


def test_search_box_and_plain_editor_allow_typing(gate):
    assert gate.authorize(Action("type", 1, "notepad.exe", "x"), {"name": "Search"}).verdict == "allow"
    assert gate.authorize(Action("type", 1, "notepad.exe", "x"), {"name": "Text editor", "ctype": "Document"}).verdict == "allow"


# ---------- which clicks need a yes ----------
@pytest.mark.parametrize("label,ctype", [("Download", "Button"), ("Allow access", "Button"), ("Save", "Button"),
                                         ("Install now", "Hyperlink"), ("Continue", "MenuItem"), ("Clear all", "SplitButton"),
                                         ("Withdraw", "Button"), ("Send", "Button"), ("Delete", "ListItem"), ("Enable", "Text")])
def test_widened_words_ask_first(gate, label, ctype):
    d = gate.authorize(click(label, ctype))
    assert d.verdict == "confirm" and d.fingerprint == (1, "notepad.exe", "Untitled - Notepad", label)


@pytest.mark.parametrize("label,ctype", [("Post Malone", "ListItem"), ("Post Malone - Sunflower", "Text"),
                                         ("Play Delete Me", "Button"), ("Cancel", "Button"), ("File", "MenuItem"),
                                         ("Please click here to save the whole document now", "Button")])
def test_songs_and_long_labels_are_not_risky(gate, label, ctype):
    assert gate.authorize(click(label, ctype)).verdict == "allow"


def test_permissive_gate_keeps_the_old_rules_only():
    g = Gate.permissive()
    assert g.authorize(click("Save", "Button")).verdict == "allow"          # "save" is a new word
    assert g.authorize(click("Send", "Button")).verdict == "confirm"
    assert g.authorize(click("Send", "Hyperlink")).verdict == "allow"       # old rules: real buttons only
    assert g.authorize(Action("click", 1, "cmd.exe", "x")).verdict == "deny"   # blocked apps always stay blocked


def test_a_yes_lets_the_asked_click_through_and_dry_runs_leave_no_trace(gate, tmp_path):
    a = click("Send")
    assert gate.authorize(a, dry=True).verdict == "confirm"
    assert not (tmp_path / "actions.log").exists()
    with gate.scope(confirmed=True):
        assert gate.authorize(a).verdict == "allow"
    assert gate.authorize(a).verdict == "confirm"                           # the yes was for one moment only


# ---------- Enter ----------
def test_enter_in_explorer_address_bar_is_denied_but_search_box_is_fine(gate):
    bar = {"name": "Address", "automation_id": "addressband", "class_name": "Edit", "ancestors": ["ToolbarWindow32 Address"]}
    a = Action("key", 1, "explorer.exe", "Downloads", key="enter")
    assert gate.authorize(a, bar).verdict == "deny"
    assert gate.authorize(Action("type", 1, "explorer.exe", "Downloads"), bar).verdict == "deny"
    assert gate.authorize(a, {"name": "Search Downloads", "class_name": "SearchBox"}).verdict == "allow"


def test_enter_in_chrome_and_other_apps_asks_first(gate):
    assert gate.authorize(Action("key", 1, "chrome.exe", "Page - Google Chrome", key="enter"),
                          {"name": "", "ctype": "Document"}).verdict == "confirm"
    assert gate.authorize(Action("key", 1, "chrome.exe", "Page - Google Chrome", key="enter"),
                          {"name": "Search Google or type a URL"}).verdict == "allow"
    assert gate.authorize(Action("key", 1, "paint.exe", "x", key="enter"), {"name": "Canvas", "ctype": "Pane"}
                          ).verdict == "confirm"
    assert gate.authorize(Action("key", 1, "notepad.exe", "x", key="enter"), {"name": "Text editor", "ctype": "Document"}
                          ).verdict == "allow"                              # a new line, not a submit
    assert gate.authorize(Action("key", 1, "whatsapp.exe", "WhatsApp", key="enter"), {"name": "Type a message"}
                          ).verdict == "confirm"                            # Enter sends
    assert gate.authorize(Action("key", 1, "chrome.exe", "x", key="tab")).verdict == "allow"


def test_vscode_terminal_is_denied_but_the_editor_is_not(gate):
    term = {"name": "Terminal 1, pwsh", "class_name": "xterm-helper-textarea", "ancestors": ["Group Terminal"]}
    assert gate.authorize(Action("type", 1, "code.exe", "main.py - Visual Studio Code"), term).verdict == "deny"
    assert gate.authorize(Action("key", 1, "cursor.exe", "x", key="a"), term).verdict == "deny"
    assert gate.authorize(Action("type", 1, "code.exe", "main.py - Visual Studio Code"),
                          {"name": "Editor content", "ctype": "Edit"}).verdict == "allow"


def test_window_checks_are_cached_for_two_seconds(gate, clock, monkeypatch):
    calls = []
    real = gate._static_block
    monkeypatch.setattr(gate, "_static_block", lambda *a: calls.append(a) or real(*a))
    for _ in range(3):
        gate.window_block("notepad.exe", "A", hwnd=7)
    assert len(calls) == 1
    clock.t += 2.5
    gate.window_block("notepad.exe", "A", hwnd=7)
    assert len(calls) == 2


# ---------- the breaker ----------
def test_seventh_agent_action_in_ten_seconds_trips_the_breaker_until_resume(gate, clock):
    act = Action("browser_click", None, "msedge.exe", "", "Next", source="agent")
    for i in range(6):
        assert gate.authorize(act).verdict == "allow"
        clock.t += 1.0
    d = gate.authorize(act)
    assert d.verdict == "deny" and d.say == "I paused myself, say resume to continue."
    assert gate.killed.is_set() and ABORT.is_set()
    assert gate.authorize(act).say == "Automation is stopped. Say resume to continue."
    assert gate.authorize(Action("click", 1, "notepad.exe", "x", "File")).verdict == "deny"      # nothing acts: not even you
    gate.resume()
    assert not gate.killed.is_set() and not ABORT.is_set()
    assert gate.authorize(act).verdict == "allow"


def test_person_actions_are_not_rate_limited_but_agent_enter_is(gate, clock):
    for _ in range(20):
        assert gate.authorize(Action("browser_click", None, "msedge.exe", "", "x")).verdict == "allow"
    enter = Action("browser_key", None, "msedge.exe", "", key="enter", source="agent")
    for _ in range(3):
        assert gate.authorize(enter).verdict == "allow"
        clock.t += 5
    assert gate.authorize(enter).verdict == "deny"          # a 4th Enter within 30 s


def test_agent_scope_counts_like_source_agent(gate, clock):
    with gate.scope(source="agent"):
        verdicts = [gate.authorize(Action("browser_click", None, "msedge.exe", "", "x")).verdict for _ in range(7)]
    assert verdicts[:6] == ["allow"] * 6 and verdicts[6] == "deny"


def test_kill_latch_denies_everything(gate):
    gate.kill()
    for kind in ("click", "type", "key", "scroll", "media", "browser_click", "browser_fill", "browser_key"):
        assert gate.authorize(Action(kind, 1, "notepad.exe", "x")).say == "Automation is stopped. Say resume to continue."


def test_every_decision_is_audited(gate, tmp_path):
    gate.authorize(click("File", "MenuItem"))
    gate.authorize(click("Send"))
    gate.authorize(Action("click", 1, "cmd.exe", "Command Prompt", "x"))
    rows = Audit(tmp_path / "actions.log").tail(10)
    assert [r["verdict"] for r in rows] == ["allow", "confirm", "deny"]


# ---------- the real Desktop on a fake Windows ----------
WINDOWS = {1: ("notepad.exe", "Untitled - Notepad"), 2: ("cmd.exe", "Command Prompt"), 3: ("notepad.exe", "Other - Notepad")}


def test_foreground_that_never_becomes_the_window_types_nothing(gate, monkeypatch):
    d, u = make_desktop(monkeypatch, gate, WINDOWS)
    u.fg = 3                                                 # another window keeps the keyboard
    with pytest.raises(DesktopError, match="couldn't bring Notepad to the front, so I didn't type anything"):
        d.type_text(1, "hello")
    assert not u.sent("send")


def test_foreground_that_is_a_terminal_refuses_even_if_asked_for_notepad(gate, monkeypatch):
    d, u = make_desktop(monkeypatch, gate, WINDOWS)
    u.fg = 2
    monkeypatch.setattr(d, "focus", lambda hwnd: True)       # pretend the focus call "worked"
    with pytest.raises(DesktopError, match="won't click or type in Command Prompt"):
        d.type_text(1, "del *")
    with pytest.raises(DesktopError):
        d.press(1, "enter")
    with pytest.raises(DesktopError):
        d.scroll(1, "down")
    assert not u.sent("send") and not u.sent("key")


def test_typing_goes_in_and_the_audit_line_has_no_text(gate, monkeypatch, tmp_path):
    d, u = make_desktop(monkeypatch, gate, WINDOWS, focused=FakeCtrl(name="Text editor", ctype="DocumentControl"))
    assert d.type_text(1, "hunter2 and more") == "document"
    assert sum(c[1] for c in u.sent("send")) == len("hunter2 and more") * 2
    raw = (tmp_path / "actions.log").read_text(encoding="utf-8")
    assert "hunter2" not in raw and '"text_len": 16' in raw and '"text_class": "mixed"' in raw


def test_typing_refuses_card_boxes_and_passwords_on_the_real_path(gate, monkeypatch):
    d, u = make_desktop(monkeypatch, gate, WINDOWS, focused=FakeCtrl(name="Card number"))
    with pytest.raises(DesktopError, match="card/OTP/password"):
        d.type_text(1, "4111")
    d, u = make_desktop(monkeypatch, gate, WINDOWS, focused=FakeCtrl(name="Body", password=True))
    with pytest.raises(DesktopError, match="password box"):
        d.type_text(1, "x")
    assert not u.sent("send")


def test_enter_press_needs_a_yes_at_the_desktop_level(gate, monkeypatch):
    d, u = make_desktop(monkeypatch, gate, {1: ("paint.exe", "Drawing - Paint")}, focused=FakeCtrl(name="Canvas", ctype="PaneControl"))
    with pytest.raises(DesktopError, match="needs your yes"):
        d.press(1, "enter")
    with gate.scope(confirmed=True):
        d.press(1, "enter")
    assert len(u.sent("key")) == 2                            # key down and up


def test_mouse_click_refuses_when_another_window_is_on_top(gate, monkeypatch):
    d, u = make_desktop(monkeypatch, gate, WINDOWS)
    u.at, u.roots = 55, {55: 3}                               # a pop-up (root 3) covers the spot
    with pytest.raises(DesktopError, match="covered by another window"):
        d.mouse_click(100, 100, 1)
    u.at, u.roots = 55, {55: 1}                               # the spot belongs to our window: fine
    d.mouse_click(100, 100, 1)
    assert u.sent("mouse")
    u.calls.clear()
    with pytest.raises(DesktopError, match="covered"):        # outside the window's rectangle
        d.mouse_click(5000, 5000, 1)
    assert not u.sent("mouse")


def test_click_falls_back_to_the_mouse_only_after_the_gate(gate, monkeypatch):
    d, u = make_desktop(monkeypatch, gate, WINDOWS)
    u.at, u.roots = 55, {55: 1}

    class Ctl:
        def GetInvokePattern(self):
            raise RuntimeError("no invoke")
    hit = {"name": "File", "type": "MenuItem", "button": True, "x": 10, "y": 10, "control": Ctl()}
    assert d.click(1, hit) == "File" and u.sent("mouse")
    u.calls.clear()
    hit["name"] = "Send"
    with pytest.raises(DesktopError, match="needs your yes"):
        d.click(1, hit)
    assert not u.sent("mouse")


# ---------- no input without the gate: an AST check ----------
DESKTOP_PRIMITIVES = {"SendInput", "keybd_event", "mouse_event", "_send_unicode", "_tap", "mouse_click"}
DESKTOP_WRAPPERS = {"_send_unicode", "_tap", "mouse_click"}        # the primitives themselves; their callers must be gated


def _functions(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _names(fn) -> set[str]:
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, ast.Name):
            out.add(n.id)
    return out


def _strings(fn) -> list[str]:
    return [n.value for n in ast.walk(fn) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def test_every_desktop_input_goes_through_authorize():
    for fn in _functions(ROOT / "desktop.py"):
        if fn.name in DESKTOP_WRAPPERS:
            continue
        if _names(fn) & DESKTOP_PRIMITIVES:
            assert "_authorize" in _names(fn) or "authorize" in _names(fn), f"desktop.{fn.name} sends input without the gate"


def test_every_browser_input_goes_through_authorize():
    seen = 0
    for fn in _functions(ROOT / "browser.py"):
        if any(s.startswith("Input.") for s in _strings(fn)):
            seen += 1
            assert "_authorize" in _names(fn), f"browser.{fn.name} sends input without the gate"
    assert seen >= 4                                          # type_text, press, click, act


def test_the_ast_check_would_catch_a_leak(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text("class D:\n    def sneaky(self):\n        self._tap(1)\n")
    leaks = [fn.name for fn in _functions(bad) if _names(fn) & DESKTOP_PRIMITIVES and "_authorize" not in _names(fn)]
    assert leaks == ["sneaky"]


def test_no_input_primitive_is_used_elsewhere():
    for path in ROOT.rglob("*.py"):
        if path.name in ("desktop.py", "browser.py"):
            continue
        text = path.read_text(encoding="utf-8")
        for prim in ("SendInput", "keybd_event", "mouse_event", "Input.dispatch", "Input.insertText"):
            assert prim not in text, f"{path.name} uses {prim} outside the gated modules"
    assert desktop.ABORT is ABORT
