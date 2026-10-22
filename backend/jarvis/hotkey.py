"""System-wide hotkey (works whichever window is in front) using the Win32 RegisterHotKey API. No extra packages."""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading
from typing import Callable

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
VK = {"space": 0x20, **{chr(c): c for c in range(ord("A"), ord("Z") + 1)}, **{str(d): 0x30 + d for d in range(10)}}
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
        raise ValueError(f"bad hotkey {combo!r}: use modifiers + a letter/digit/space, e.g. ctrl+alt+space")
    return mods, key


class GlobalHotkey:
    """Calls `callback` (from a background thread) each time the combo is pressed anywhere in Windows."""

    def __init__(self, combos: list[str], callback: Callable[[], None]):
        self.combos = combos
        self.callback = callback
        self.active: str | None = None      # the combo that registered successfully
        self.error: str = ""
        self._thread_id = 0
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="jarvis-hotkey")

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
            if user32.RegisterHotKey(None, 1, mods | MOD_NOREPEAT, key):
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
        user32.UnregisterHotKey(None, 1)

    def stop(self) -> None:
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
