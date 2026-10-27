"""System-wide hotkey (works whichever window is in front) using the Win32 RegisterHotKey API. No extra packages."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
from typing import Callable

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
VK = {"space": 0x20, "esc": 0x1B, "backspace": 0x08, "pause": 0x13, **{chr(c): c for c in range(ord("A"), ord("Z") + 1)}, **{str(d): 0x30 + d for d in range(10)}}
_MODS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}


def parse(combo: str) -> tuple[int, int]:
    """'ctrl+alt+space' → (modifiers, virtual key)."""
    mods, key = 0, None
    for part in combo.lower().replace(" ", "").split("+"):
        if part in _MODS:
            mods |= _MODS[part]
        else:
            key = VK.get(part.upper() if len(part) == 1 else part)
    if key is None or not mods:
        raise ValueError(f"bad hotkey {combo!r}: use modifiers + a letter/digit/space/esc/backspace/pause, e.g. ctrl+alt+space")
    return mods, key


class GlobalHotkey:
    """Calls `callback` (from a background thread) each time the combo is pressed anywhere in Windows."""

    HOTKEY_ID, THREAD_NAME = 1, "jarvis-hotkey"

    def __init__(self, combos: list[str], callback: Callable[[], None]):
        self.combos = combos
        self.callback = callback
        self.active: str | None = None      # the combo that registered successfully
        self.error: str = ""
        self._thread_id = 0
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name=self.THREAD_NAME)

    def start(self) -> "GlobalHotkey":
        self._thread.start()
        self._ready.wait(3)
        return self

    def _run(self) -> None:
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        self._thread_id = kernel32.GetCurrentThreadId()
        for combo in self.combos:  # first combo nobody else has taken
            try:
                mods, key = parse(combo)
            except ValueError as exc:
                self.error = str(exc)
                continue
            if user32.RegisterHotKey(None, self.HOTKEY_ID, mods | MOD_NOREPEAT, key):
                self.active = combo
                break
            self.error = f"{combo} is already used by another app"
        self._ready.set()
        if not self.active:
            return
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                try:
                    self.callback()
                except Exception:
                    pass
        user32.UnregisterHotKey(None, self.HOTKEY_ID)

    def stop(self) -> None:
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)


class KillSwitch(GlobalHotkey):
    """The emergency stop chord. Same Win32 RegisterHotKey as the talk hotkey (id 2): Windows hands over exactly this
    chord and nothing else, so there is no keyboard hook and no way to see any other keystroke. The callback runs on
    this hotkey thread, so the first thing it does (setting the gate's latch) never waits for the app's event loop."""
    HOTKEY_ID, THREAD_NAME = 2, "jarvis-killswitch"
    DEFAULT = "ctrl+alt+backspace"
    FALLBACKS = ("ctrl+alt+pause", "ctrl+shift+alt+backspace")

    def __init__(self, chord: str, callback: Callable[[], None]):
        super().__init__(list(dict.fromkeys([c for c in (chord or self.DEFAULT, self.DEFAULT, *self.FALLBACKS) if c])),
                         callback)
        self.wanted = chord or self.DEFAULT

    @property
    def fallback_used(self) -> bool:
        return bool(self.active) and self.active != self.wanted


VK_RCONTROL = 0xA3
_WH_KEYBOARD_LL, _LLKHF_INJECTED = 13, 0x10
_KEY_DOWN, _KEY_UP = {0x100, 0x104}, {0x101, 0x105}     # WM_(SYS)KEYDOWN / WM_(SYS)KEYUP


class _KBD(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class KeyTap:
    """Calls `callback` when one key is tapped on its own: pressed and released quickly with no other key in between
    (so Right Ctrl + C still copies, and holding it does nothing). RegisterHotKey can't take a lone modifier, so this
    watches the keyboard with a low-level hook. It only compares key codes as they pass: nothing is stored or
    blocked, and keys typed by programs (JARVIS's own typing) are ignored."""

    def __init__(self, callback: Callable[[], None], vk: int = VK_RCONTROL, max_hold_s: float = 0.45):
        self.callback, self.vk, self.max_hold_ms = callback, vk, int(max_hold_s * 1000)
        self.active, self.error = False, ""
        self._down_at: int | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="jarvis-keytap")

    def start(self) -> "KeyTap":
        self._thread.start()
        self._ready.wait(3)
        return self

    def _on_key(self, msg: int, vk: int, t: int) -> None:
        if vk == self.vk:
            if msg in _KEY_DOWN:
                if self._down_at is None:              # ignore auto-repeat while held
                    self._down_at = t
            elif msg in _KEY_UP:
                down, self._down_at = self._down_at, None
                if down is not None and (t - down) & 0xFFFFFFFF <= self.max_hold_ms:
                    try:
                        self.callback()
                    except Exception:
                        pass
        elif msg in _KEY_DOWN:
            self._down_at = None                       # another key while held: a shortcut, not a tap

    def _run(self) -> None:
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        self._thread_id = kernel32.GetCurrentThreadId()
        proc_t = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wt.WPARAM, wt.LPARAM)
        user32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, proc_t, wt.HINSTANCE, wt.DWORD]
        user32.SetWindowsHookExW.restype = wt.HHOOK
        kernel32.GetModuleHandleW.restype = wt.HMODULE

        def hook(code, wparam, lparam):
            if code == 0:
                k = ctypes.cast(lparam, ctypes.POINTER(_KBD)).contents
                if not k.flags & _LLKHF_INJECTED:
                    self._on_key(wparam, k.vkCode, k.time)
            return user32.CallNextHookEx(None, code, wparam, lparam)   # never swallow the key

        self._proc = proc_t(hook)                      # keep a reference, or Windows calls freed memory
        handle = user32.SetWindowsHookExW(_WH_KEYBOARD_LL, self._proc, kernel32.GetModuleHandleW(None), 0)
        self.active = bool(handle)
        self.error = "" if handle else f"keyboard hook refused (error {kernel32.GetLastError()})"
        self._ready.set()
        if not handle:
            return
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass
        user32.UnhookWindowsHookEx(handle)

    def stop(self) -> None:
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
