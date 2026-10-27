"""Shared fakes for the SafetyGate tests: a pretend user32, a pretend uiautomation and a Desktop built on them.
Nothing here touches the real screen, keyboard or mouse."""
import sys
import types

from jarvis import desktop
from jarvis.desktop import Desktop


def _v(h):
    return getattr(h, "value", h)


class FakeUser32:
    def __init__(self):
        self.fg = 1                          # the window that really has the keyboard
        self.roots = {}                      # child -> top-level window
        self.rects = {1: (0, 0, 800, 600)}
        self.at = 1                          # what WindowFromPoint finds at any spot
        self.calls = []
        self.GetForegroundWindow = lambda: self.fg
        self.GetAncestor = lambda h, flag: self.roots.get(_v(h), _v(h))
        self.IsIconic = lambda h: 0
        self.ShowWindow = lambda h, n: 1
        self.WindowFromPoint = lambda pt: self.at
        self.GetCursorPos = lambda ref: None
        self.SetCursorPos = lambda x, y: self.calls.append(("cursor", x, y))
        self.mouse_event = lambda *a: self.calls.append(("mouse", a))
        self.keybd_event = lambda *a: self.calls.append(("key", a))
        self.SendInput = self._send

        def rect(h, ref):
            r = ref._obj
            r.left, r.top, r.right, r.bottom = self.rects[_v(h)]
        self.GetWindowRect = rect
        self.on_send = None                  # called after every chunk of typing

    def _send(self, n, arr, size):
        self.calls.append(("send", n))
        if self.on_send:
            self.on_send(len([c for c in self.calls if c[0] == "send"]))

    def sent(self, kind):
        return [c for c in self.calls if c[0] == kind]


class FakeCtrl:
    def __init__(self, name="Body", ctype="EditControl", aid="", cls="Edit", help="", password=False, parent=None):
        self.Name, self.ControlTypeName, self.AutomationId, self.ClassName = name, ctype, aid, cls
        self.HelpText, self.IsPassword, self._parent = help, password, parent
        self.IsOffscreen = False

    def GetParentControl(self):
        return self._parent

    def SetActive(self):
        pass

    def SetFocus(self):
        pass


class _Init:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def install_fake_auto(monkeypatch, focused):
    mod = types.ModuleType("uiautomation")
    mod.UIAutomationInitializerInThread = _Init
    mod.GetFocusedControl = lambda: focused
    mod.ControlFromHandle = lambda h: FakeCtrl()
    mod.WalkControl = lambda c, maxDepth=0: iter([])
    monkeypatch.setitem(sys.modules, "uiautomation", mod)
    return mod


def make_desktop(monkeypatch, gate, windows, focused=None):
    """A real Desktop (no tracking thread) wired to the fake Windows: windows = {hwnd: (process, title)}."""
    u = FakeUser32()
    d = object.__new__(Desktop)
    d.is_own_window = lambda *a: False
    d.gate = gate
    d.last_hwnd, d.last_changed = None, 0.0
    monkeypatch.setattr(desktop, "_user32", lambda: u)
    monkeypatch.setattr(desktop, "window_process", lambda h: windows[_v(h)][0])
    monkeypatch.setattr(desktop, "window_title", lambda h: windows[_v(h)][1])
    monkeypatch.setattr(desktop, "foreground_root", lambda: u.fg)
    monkeypatch.setattr(desktop, "is_jarvis_edge", lambda h, *a: False)
    monkeypatch.setattr(desktop.time, "sleep", lambda s: None)
    install_fake_auto(monkeypatch, focused if focused is not None else FakeCtrl())
    return d, u
