"""Spidey's meme panel: a small window beside spidey where its reaction to each moment pops up as a meme.

Spidey picks the meme (backend/jarvis/memes.py) after every reply; it arrives here as a plain PNG from Spidey on this
laptop (the same picture store as the picture board). The panel never takes the keyboard (WS_EX_NOACTIVATE, not in
Alt+Tab), fades out by itself after a while (not while the mouse is on it), and a click puts it away. Display only."""
from __future__ import annotations

import ctypes
import io
import queue
import re
import threading
import tkinter as tk
import urllib.request

BG = "#fffaf0"          # spidey's bubble colours
EDGE = "#1a1a1a"
HINT = "#8a8378"
ACCENT = "#c0141f"
FONT = "Segoe UI"
MOOD_FACE = {"happy": "🙂", "laughing": "😂", "excited": "🤩", "proud": "😎", "love": "😍", "sad": "🥺", "tired": "😴",
             "annoyed": "😤", "confused": "🤔", "surprised": "😮", "thinking": "🧐", "smug": "😏", "scared": "😱",
             "awkward": "😬", "bored": "🥱", "agree": "👍", "disagree": "🙅", "waiting": "⏳", "chaos": "🔥",
             "wholesome": "🥹"}


class MemePanel:
    MAX = 300                                 # the meme's longest side, at 100% scaling
    SHOW_S = 14                               # seconds on screen (longer while the mouse is on it)
    KEEP = 10                                 # memes you can flip back to
    EX_STYLE = 0x08000000 | 0x80              # WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW

    def __init__(self, root: tk.Tk, link, url: str, scale: float, place):
        """place(w, h) -> (x, y): where it goes beside spidey."""
        self.root, self.link, self.url, self.s, self.place = root, link, url, scale, place
        self.inbox: queue.Queue = queue.Queue()
        self.shown = self.hovered = False
        self._photo = None
        self._hide_job = None
        self._note_job = None
        self.last: dict = {}                  # what's showing: {"mood", "template"}
        self.history: list[tuple[dict, object, bytes]] = []   # the last KEEP memes: (meta, picture, png) to flip back to
        self.i = -1
        win = self.win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=EDGE)
        win.withdraw()
        self._style()
        body = tk.Frame(win, bg=BG)
        body.pack(padx=2, pady=2)
        self.pic = tk.Label(body, bg=BG, bd=0, cursor="hand2")
        self.pic.pack(padx=int(6 * scale), pady=(int(6 * scale), int(2 * scale)))
        foot = tk.Frame(body, bg=BG)
        foot.pack(fill="x", padx=int(8 * scale), pady=(0, int(4 * scale)))
        self.mood_lbl = tk.Label(foot, text="", bg=BG, fg=HINT, font=(FONT, 8))
        self.mood_lbl.pack(side="left")
        close = tk.Label(foot, text="✕", bg=BG, fg=HINT, font=(FONT, 9), cursor="hand2")
        close.pack(side="right")
        self.save_lbl = tk.Label(foot, text="Save", bg=BG, fg=ACCENT, font=(FONT, 8), cursor="hand2")
        self.save_lbl.pack(side="right", padx=(0, int(8 * scale)))
        self.save_lbl.bind("<Button-1>", lambda e: self.save())
        self.next_lbl = tk.Label(foot, text="›", bg=BG, fg=EDGE, font=(FONT, 11), cursor="hand2")
        self.count_lbl = tk.Label(foot, text="", bg=BG, fg=HINT, font=(FONT, 8))
        self.prev_lbl = tk.Label(foot, text="‹", bg=BG, fg=EDGE, font=(FONT, 11), cursor="hand2")
        for w in (self.next_lbl, self.count_lbl, self.prev_lbl):
            w.pack(side="right", padx=(0, int(3 * scale)))
        self.prev_lbl.bind("<Button-1>", lambda e: self.flip(-1))
        self.next_lbl.bind("<Button-1>", lambda e: self.flip(1))
        for w in (self.pic, close):
            w.bind("<Button-1>", lambda e: self.hide())
        for w in (win, self.pic):
            w.bind("<Enter>", lambda e: self._hover(True))
            w.bind("<Leave>", lambda e: self._hover(False))
        self._poll()

    # ---- what Spidey sends ----
    def on_event(self, ev: dict) -> None:
        bid = str(ev.get("id") or "")
        if not re.fullmatch(r"[0-9a-f]{16}", bid):
            return
        mood = str(ev.get("mood") or "")
        meta = {"mood": mood if mood in MOOD_FACE else "", "template": re.sub(r"[^a-z0-9_]", "", str(ev.get("template") or ""))}
        threading.Thread(target=self._fetch, args=(bid, meta), daemon=True).start()

    def _fetch(self, bid: str, meta: dict) -> None:
        try:
            req = urllib.request.Request(f"{self.url}/api/board/{bid}", headers=self.link._headers())
            with urllib.request.urlopen(req, timeout=8) as r:
                png = r.read(8_000_000)
            from PIL import Image
            im = Image.open(io.BytesIO(png))
            if im.width * im.height > 4_000_000:
                return
            im.load()
            self.inbox.put((meta, im.convert("RGB"), png))
        except Exception:
            pass                               # a meme that didn't arrive just isn't shown

    def _poll(self):
        try:
            while True:
                meta, im, png = self.inbox.get_nowait()
                self.history = (self.history + [(meta, im, png)])[-self.KEEP:]
                self.i = len(self.history) - 1
                self._show(meta, im)
        except queue.Empty:
            pass
        self.root.after(120, self._poll)

    # ---- showing ----
    def _show(self, meta: dict, im) -> None:
        from PIL import ImageTk
        k = min(self.MAX * self.s / im.width, self.MAX * self.s / im.height, 1.0)
        if k < 1.0:
            im = im.resize((max(1, int(im.width * k)), max(1, int(im.height * k))))
        self._photo = ImageTk.PhotoImage(im)
        self.pic.configure(image=self._photo)
        mood = meta.get("mood") or ""
        self.mood_lbl.configure(text=f"{MOOD_FACE.get(mood, '🕷')}  Spidey feels {mood}" if mood else "🕷  Spidey")
        self.last = meta
        many = len(self.history) > 1
        self.count_lbl.configure(text=f"{self.i + 1}/{len(self.history)}" if many else "")
        self.prev_lbl.configure(fg=EDGE if many and self.i > 0 else BG)
        self.next_lbl.configure(fg=EDGE if many and self.i < len(self.history) - 1 else BG)
        win = self.win
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        x, y = self.place(w, h)
        win.geometry(f"{w}x{h}+{x}+{y}")
        if not self.shown:
            win.deiconify()                    # without the focus: whatever you're typing in keeps it
            self.shown = True
        self._arm()

    def flip(self, step: int) -> None:
        """‹ › through the last few memes (the panel stays while you look)."""
        if len(self.history) > 1:
            self.i = max(0, min(len(self.history) - 1, self.i + step))
            meta, im, _ = self.history[self.i]
            self._show(meta, im)

    def save(self) -> None:
        """A copy of the meme on screen in Downloads (a new file each time; nothing is overwritten)."""
        from datetime import datetime
        from pathlib import Path
        if not (0 <= self.i < len(self.history)):
            return
        meta, _, png = self.history[self.i]
        try:
            folder = Path.home() / "Downloads"
            folder = folder if folder.is_dir() else Path.home()
            name = (meta.get("template") or "meme").replace("_", " ")
            path = folder / f"Spidey meme {name} {datetime.now():%Y-%m-%d %H%M%S}.png"
            n = 2
            while path.exists():
                path = path.with_name(f"{path.stem} ({n}).png")
                n += 1
            path.write_bytes(png)
            self._note("Saved to Downloads")
        except Exception:
            self._note("Couldn't save it")
        self._arm()

    def _note(self, text: str) -> None:
        self.save_lbl.configure(text=text)
        if self._note_job:
            self.root.after_cancel(self._note_job)
        self._note_job = self.root.after(2500, lambda: self.save_lbl.configure(text="Save"))

    def _arm(self) -> None:
        if self._hide_job:
            self.root.after_cancel(self._hide_job)
        self._hide_job = self.root.after(self.SHOW_S * 1000, self._timeout)

    def _timeout(self) -> None:
        self._hide_job = None
        if self.hovered:
            self._arm()                        # you're looking at it: stay
        else:
            self.hide()

    def _hover(self, on: bool) -> None:
        self.hovered = on

    def hide(self) -> None:
        if self._hide_job:
            self.root.after_cancel(self._hide_job)
            self._hide_job = None
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
