"""Spidey as a study buddy: study mode (pomodoro). "start a pomodoro" / "study mode for 50 minutes": focus rounds with
short breaks (every fourth break is a long one), the time left on spidey's status pill, a spoken nudge and a meme at
each change, and "stop the pomodoro" ends it. Only a timer: it never touches anything on the laptop.

The other companion bits (break nudges after two hours at the laptop, "go to sleep" late at night, the morning hello)
live in spidey, which already knows when you're at the keyboard; it asks for their memes through POST /api/react."""
from __future__ import annotations

import threading
import time
from typing import Callable

FOCUS_MIN, BREAK_MIN, LONG_BREAK_MIN, LONG_EVERY = 25, 5, 15, 4


class Pomodoro:
    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.on_phase: Callable[[dict], None] | None = None   # set by the server: say it, show it, meme it
        self.phase = "off"                                     # off | focus | break
        self.round = 0                                         # the focus round you're on (1, 2, …)
        self.done = 0                                          # focus rounds finished
        self.focus_min, self.break_min = FOCUS_MIN, BREAK_MIN
        self.ends_at = 0.0
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def start(self, focus_min: int = FOCUS_MIN, break_min: int = BREAK_MIN) -> dict:
        with self._lock:
            self._cancel()
            self.focus_min = max(1, min(180, int(focus_min or FOCUS_MIN)))
            self.break_min = max(1, min(60, int(break_min or BREAK_MIN)))
            self.round, self.done = 1, 0
            self._enter("focus", self.focus_min)
            return self.status()

    def stop(self) -> int:
        """Ends study mode; returns how many focus rounds were finished."""
        with self._lock:
            self._cancel()
            done, self.phase, self.ends_at = self.done, "off", 0.0
            return done

    def status(self) -> dict:
        left = max(0, int(self.ends_at - self.clock())) if self.phase != "off" else 0
        return {"type": "pomodoro", "phase": self.phase, "round": self.round, "done": self.done,
                "ends_at": self.ends_at if self.phase != "off" else 0, "left_s": left}

    # ---- the clock ----
    def _enter(self, phase: str, minutes: int) -> None:
        self.phase = phase
        self.ends_at = self.clock() + minutes * 60
        self._timer = threading.Timer(minutes * 60, self._next)
        self._timer.daemon = True
        self._timer.start()

    def _cancel(self) -> None:
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def _next(self) -> None:
        with self._lock:
            if self.phase == "focus":
                self.done += 1
                long = self.done % LONG_EVERY == 0
                minutes = LONG_BREAK_MIN if long else self.break_min
                say = (f"Round {self.round} done! That's {self.done} in a row, take a proper {minutes} minute break."
                       if long else f"Round {self.round} done. Take {minutes}, stretch, drink some water.")
                moment = "focus_done"
                self._enter("break", minutes)
            elif self.phase == "break":
                self.round += 1
                say = f"Break's over. Round {self.round}: {self.focus_min} minutes of focus. You've got this."
                moment = "break_done"
                self._enter("focus", self.focus_min)
            else:
                return
            event = {**self.status(), "say": say, "moment": moment}
        if self.on_phase:
            try:
                self.on_phase(event)
            except Exception:
                pass


def spoken_left(seconds: int) -> str:
    m, s = divmod(max(0, int(seconds)), 60)
    if m == 0:
        return f"{s} seconds"
    return f"{m} minute{'s' if m != 1 else ''}" + (f" and {s} seconds" if m < 3 and s else "")
