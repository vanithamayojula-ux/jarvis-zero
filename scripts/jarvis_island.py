"""Spidey's floating island: a black pill at the top centre of the screen (like the iPhone's Dynamic Island) that shows
the guide's current step ("Click Personalization on the left"). It grows to fit the step, shrinks back, and goes away
when the guide ends. Buttons: › (look again / next step) and ✕ (stop guiding), which only send those words to Spidey.

Display only: it never takes the keyboard (WS_EX_NOACTIVATE, not in Alt+Tab), and everything it shows is plain text
from Spidey on this laptop. It doesn't look at anything itself; Spidey looks, only while a guide runs."""
from __future__ import annotations

import ctypes
import tkinter as tk

KEY = "#010203"         # see-through (spidey's key colour): only the pill itself shows
PILL = "#0b0b0f"
INK = "#f4f4f6"
DIM = "#9a9aa6"
RED = "#e0242f"
FONT = "Segoe UI"


class Island:
    EX_STYLE = 0x08000000 | 0x80          # WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    MAX_W = 640                           # at 100% scaling

    def __init__(self, root: tk.Tk, link, scale: float, work_area):
        """work_area(x, y) -> (l, t, r, b) of the screen at that point (None = the whole primary screen)."""
        self.root, self.link, self.s, self.work_area = root, link, scale, work_area
        self.shown = False
        self.w = self.target_w = 0
        self.h = int(40 * scale)
        self._anim = self._hide_job = None
        self.text, self.counter, self.mode = "", "", "step"
        win = self.win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-transparentcolor", KEY)
        win.configure(bg=KEY)
        win.withdraw()
        self.c = tk.Canvas(win, bg=KEY, highlightthickness=0, bd=0)
        self.c.pack(fill="both", expand=True)
        self.c.tag_bind("next", "<Button-1>", lambda e: self.link.send("next step"))
        self.c.tag_bind("close", "<Button-1>", lambda e: self.link.send("stop guiding"))
        self._style()

    # ---- what Spidey sends ----
    def on_event(self, ev: dict) -> None:
        if not ev.get("on"):
            self.finish(str(ev.get("step") or "Done!"))
            return
        n = int(ev.get("n") or 1)
        self.mode = "done" if ev.get("done") else "wait" if ev.get("waiting") else "step"
        self.counter = "done" if ev.get("done") else f"step {n}"
        self._set(self._clean(ev.get("step")))

    def finish(self, text: str) -> None:
        """The last line ("Done!" or why it stopped) for a few seconds, then away."""
        if not self.shown:
            return
        self.mode, self.counter = "end", ""
        self._set(self._clean(text) or "Done!")
        if self._hide_job:
            self.root.after_cancel(self._hide_job)
        self._hide_job = self.root.after(3500, self.hide)

    @staticmethod
    def _clean(text) -> str:
        from jarvis.board import clean_text
        return clean_text(str(text or ""), 160)

    # ---- drawing ----
    def _set(self, text: str) -> None:
        if self._hide_job and self.mode != "end":
            self.root.after_cancel(self._hide_job)
            self._hide_job = None
        self.text = text
        s = self.s
        probe = self.c.create_text(0, 0, text=text, font=(FONT, 11), anchor="nw", width=int((self.MAX_W - 150) * s))
        x0, y0, x1, y1 = self.c.bbox(probe)
        self.c.delete(probe)
        self.target_w = int(min(self.MAX_W * s, (x1 - x0) + 150 * s))
        self.h = int(max(40 * s, (y1 - y0) + 20 * s))
        if not self.shown:
            self.w = int(110 * s)                         # starts small and grows, like the real island
            self.win.deiconify()
            self.shown = True
        self._step()

    def _step(self) -> None:
        """Ease the width toward its target, redrawing each frame."""
        self._anim = None
        self.w += round((self.target_w - self.w) * 0.35) or (1 if self.target_w > self.w else -1 if self.target_w < self.w else 0)
        if abs(self.target_w - self.w) <= 2:
            self.w = self.target_w
        self._draw()
        if self.w != self.target_w:
            self._anim = self.root.after(16, self._step)

    def _draw(self) -> None:
        s, w, h, c = self.s, self.w, self.h, self.c
        wa = self.work_area(self.root.winfo_screenwidth() // 2, 0) or (0, 0, self.root.winfo_screenwidth(), 0)
        x = (wa[0] + wa[2]) // 2 - w // 2
        self.win.geometry(f"{w}x{h}+{x}+{wa[1] + int(8 * s)}")
        c.configure(width=w, height=h)
        c.delete("all")
        r = h // 2
        c.create_oval(0, 0, h, h, fill=PILL, outline=PILL)
        c.create_oval(w - h, 0, w, h, fill=PILL, outline=PILL)
        c.create_rectangle(r, 0, w - r, h, fill=PILL, outline=PILL)
        # spidey's mark: a little red mask with two white eyes
        mx, my, mr = r, r, int(11 * s)
        c.create_oval(mx - mr, my - mr, mx + mr, my + mr, fill=RED if self.mode != "end" else DIM, outline="")
        for side in (-1, 1):
            ex = mx + side * int(4.5 * s)
            c.create_polygon(ex - int(3 * s), my - int(2 * s), ex + int(3 * s), my - int(2 * s), ex, my + int(3 * s),
                             fill="white", outline="")
        if w < 160 * s:
            return                                          # still growing: just the mark
        right = w - int(14 * s)
        c.create_text(right, r, text="✕", fill=DIM, font=(FONT, 10), anchor="e", tags=("close",))
        if self.mode in ("step", "wait"):
            right -= int(26 * s)
            c.create_text(right, r, text="›", fill=INK, font=(FONT, 15, "bold"), anchor="e", tags=("next",))
        if self.counter:
            right -= int(22 * s)
            c.create_text(right, r, text=self.counter, fill=DIM, font=(FONT, 8), anchor="e")
            right -= int(48 * s)
        left = h + int(4 * s)
        c.create_text(left, r, text=self.text, fill=INK if self.mode != "wait" else DIM, font=(FONT, 11), anchor="w",
                      width=max(40, right - left))
        for tag in ("close", "next"):
            c.tag_bind(tag, "<Enter>", lambda e: c.configure(cursor="hand2"))
            c.tag_bind(tag, "<Leave>", lambda e: c.configure(cursor=""))

    def hide(self) -> None:
        self._hide_job = None
        if self._anim:
            self.root.after_cancel(self._anim)
            self._anim = None
        if self.shown:
            self.win.withdraw()
            self.shown = False

    def _style(self) -> None:
        try:
            self.win.update_idletasks()
            u = ctypes.windll.user32
            u.GetAncestor.restype = ctypes.c_void_p
            top = u.GetAncestor(ctypes.c_void_p(self.win.winfo_id()), 2)    # GA_ROOT
            if top:
                hwnd = ctypes.c_void_p(top)
                u.SetWindowLongW(hwnd, -20, u.GetWindowLongW(hwnd, -20) | self.EX_STYLE)
        except Exception:
            pass
