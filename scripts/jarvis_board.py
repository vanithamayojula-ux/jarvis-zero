"""Spidey's picture board: a small window beside spidey that shows a picture (from Wikipedia, or one of your own files)
or a drawing JARVIS made, which draws itself line by line. It runs inside spidey's process (tkinter + Pillow).

It never takes the keyboard from what you're doing: it appears without focus (WS_EX_NOACTIVATE) and stays out of
Alt+Tab. Everything on it is display only. A drawing is checked again here (jarvis.board.validate_drawing) before a
single line is drawn, pictures arrive as plain PNGs from JARVIS on this laptop, and the only buttons are flip, open the
source page (through JARVIS, so its site checks apply), save a copy to Downloads, and close."""
from __future__ import annotations

import ctypes
import io
import queue
import re
import threading
import tkinter as tk
import urllib.request
from datetime import datetime
from pathlib import Path

BG = "#fffaf0"          # spidey's bubble colours (as the panel)
EDGE = "#1a1a1a"
INK = "#1a1a1a"
HINT = "#8a8378"
ACCENT = "#c0141f"
FIELD = "#ffffff"
FONT = "Segoe UI"
KEEP = 12                # pictures and drawings you can flip back to
ANCHOR = {"start": "sw", "middle": "s", "end": "se"}


class Board:
    MAX_W, MAX_H = 440, 330      # the picture area, at 100% scaling
    EX_STYLE = 0x08000000 | 0x80  # WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW: never steals the keyboard, not in Alt+Tab

    def __init__(self, root: tk.Tk, link, url: str, scale: float, place):
        """place(w, h) -> (x, y) puts the board beside spidey (or beside the panel when that's open)."""
        self.root, self.link, self.url, self.s, self.place = root, link, url, scale, place
        self.items: list[dict] = []          # {"kind": "image"|"drawing", "title", "source", "page", "png"|"drawing"}
        self.i = -1
        self.busy = ""                       # "Drawing a cat…" while JARVIS works on it
        self.inbox: queue.Queue = queue.Queue()
        self.shown = self.moved = False
        self._photo = None                   # keeps the Tk image alive
        self._anim = self._dots = None
        self.note_job = None
        s = scale
        win = self.win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=EDGE)
        win.withdraw()
        self._style()                        # before the first show, or it would already have taken the focus
        body = tk.Frame(win, bg=BG)
        body.pack(padx=2, pady=2)
        pad = int(10 * s)

        head = tk.Frame(body, bg=BG)
        head.pack(fill="x", padx=pad, pady=(int(8 * s), int(6 * s)))
        self.title_lbl = tk.Label(head, text="", bg=BG, fg=INK, font=(FONT, 10, "bold"), anchor="w")
        self.title_lbl.pack(side="left")
        self.src_lbl = tk.Label(head, text="", bg=BG, fg=HINT, font=(FONT, 8), anchor="w")
        self.src_lbl.pack(side="left", padx=(int(6 * s), 0))
        close = tk.Label(head, text="✕", bg=BG, fg=HINT, font=(FONT, 11), cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: self.hide())
        self.next_lbl = tk.Label(head, text="›", bg=BG, fg=INK, font=(FONT, 13), cursor="hand2", padx=int(4 * s))
        self.count_lbl = tk.Label(head, text="", bg=BG, fg=HINT, font=(FONT, 8))
        self.prev_lbl = tk.Label(head, text="‹", bg=BG, fg=INK, font=(FONT, 13), cursor="hand2", padx=int(4 * s))
        for w in (self.next_lbl, self.count_lbl, self.prev_lbl):
            w.pack(side="right", padx=(0, int(4 * s)))
        self.prev_lbl.bind("<Button-1>", lambda e: self.flip(-1))
        self.next_lbl.bind("<Button-1>", lambda e: self.flip(1))
        for w in (head, self.title_lbl, self.src_lbl, self.count_lbl):
            w.bind("<ButtonPress-1>", self._grab)
            w.bind("<B1-Motion>", self._drag)

        self.canvas = tk.Canvas(body, width=int(320 * s), height=int(200 * s), bg=FIELD, highlightthickness=1,
                                highlightbackground="#ddd4c4", bd=0)
        self.canvas.pack(padx=pad)

        foot = tk.Frame(body, bg=BG)
        foot.pack(fill="x", padx=pad, pady=(int(6 * s), int(8 * s)))
        self.page_lbl = tk.Label(foot, text="Open page ↗", bg=BG, fg=ACCENT, font=(FONT, 9), cursor="hand2")
        self.page_lbl.bind("<Button-1>", lambda e: self.open_page())
        self.redraw_lbl = tk.Label(foot, text="Draw it again", bg=BG, fg=ACCENT, font=(FONT, 9), cursor="hand2")
        self.redraw_lbl.bind("<Button-1>", lambda e: self.replay())
        self.save_lbl = tk.Label(foot, text="Save", bg=BG, fg=HINT, font=(FONT, 9), cursor="hand2")
        self.save_lbl.bind("<Button-1>", lambda e: self.save())
        self.note_lbl = tk.Label(foot, text="", bg=BG, fg=HINT, font=(FONT, 8))
        self.save_lbl.pack(side="right")
        self.note_lbl.pack(side="right", padx=(0, int(8 * s)))
        self._poll()

    # ---- what JARVIS sends ----
    def on_event(self, ev: dict) -> None:
        kind = ev.get("kind")
        title = _clean(ev.get("title"), 60)
        if kind == "busy":
            self.busy = title or "Working on it…"
            self._render()
        elif kind == "failed":
            self.busy = ""
            if self.shown:
                self._note(title or "That didn't work.")
                self._render()
        elif kind == "drawing":
            from jarvis.board import validate_drawing
            d = validate_drawing(ev.get("drawing"))
            self.busy = ""
            if d:
                self._add({"kind": "drawing", "title": title or d["title"] or "A drawing", "source": "drawn by Spidey",
                           "drawing": d})
        elif kind == "image":
            bid = str(ev.get("id") or "")
            if re.fullmatch(r"[0-9a-f]{16}", bid):
                page = str(ev.get("page") or "")
                page = page if re.fullmatch(r"https://[a-z]{2,3}\.wikipedia\.org/wiki/[^\s\"'<>]{1,300}", page) else ""
                meta = {"kind": "image", "title": title or "A picture", "source": _clean(ev.get("source"), 30), "page": page}
                threading.Thread(target=self._fetch, args=(bid, meta), daemon=True).start()

    def show_file(self, path) -> None:
        """A picture file you dropped on spidey (read here, never changed)."""
        def go():
            try:
                from jarvis.board import local_png
                png = local_png(path)
                self.inbox.put(("image", {"kind": "image", "title": _clean(Path(path).stem, 60), "source": "your file",
                                          "page": "", "png": png, "img": _decode(png)}))
            except Exception:
                self.inbox.put(("note", f"Couldn't open {Path(path).name} as a picture."))
        self.busy = f"Opening {Path(path).name}…"
        self._render()
        threading.Thread(target=go, daemon=True).start()

    def _fetch(self, bid: str, meta: dict) -> None:
        try:
            req = urllib.request.Request(f"{self.url}/api/board/{bid}", headers=self.link._headers())
            with urllib.request.urlopen(req, timeout=8) as r:
                png = r.read(12_000_000)
            self.inbox.put(("image", {**meta, "png": png, "img": _decode(png)}))
        except Exception:
            self.inbox.put(("note", "Couldn't load that picture."))

    def _poll(self):
        try:
            while True:
                kind, data = self.inbox.get_nowait()
                if kind == "image":
                    self.busy = ""
                    self._add(data)
                elif kind == "note":
                    self.busy = ""
                    self._note(data)
                    self._render()
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    # ---- showing ----
    def _add(self, item: dict) -> None:
        self.items = (self.items + [item])[-KEEP:]
        self.i = len(self.items) - 1
        self._render()

    def flip(self, step: int) -> None:
        if self.items and not self.busy:
            self.i = max(0, min(len(self.items) - 1, self.i + step))
            self._render()

    def open_empty(self) -> None:
        """Right-click → Picture board: the last thing shown, or how to use it."""
        self._render()

    def _render(self) -> None:
        s, c = self.s, self.canvas
        self._stop()
        c.delete("all")
        item = self.items[self.i] if self.items and 0 <= self.i < len(self.items) else None
        if self.busy or not item:
            w, h = int(320 * s), int(200 * s)
            c.configure(width=w, height=h, bg=FIELD)
            if self.busy:
                self.title_lbl.configure(text=self.busy)
                self.src_lbl.configure(text="")
                pencil = c.create_text(w / 2, h / 2 - 10 * s, text="✏", font=("Segoe UI Emoji", 22), fill=INK)
                dots = c.create_text(w / 2, h / 2 + 24 * s, text="", font=(FONT, 14, "bold"), fill=ACCENT)
                self._wiggle(pencil, dots, 0)
            else:
                self.title_lbl.configure(text="Picture board")
                self.src_lbl.configure(text="")
                c.create_text(w / 2, h / 2, width=w - 40 * s, justify="center", fill=HINT, font=(FONT, 10),
                              text="Ask Spidey “show me a picture of a red panda” or “draw a rocket”, "
                                   "or drop a picture on spidey.")
            for lbl in (self.page_lbl, self.redraw_lbl):
                lbl.pack_forget()
            self.save_lbl.configure(fg=HINT, cursor="arrow")
        else:
            self.title_lbl.configure(text=item["title"])
            self.src_lbl.configure(text=item.get("source", ""))
            self.save_lbl.configure(fg=ACCENT, cursor="hand2")
            self.page_lbl.pack_forget()
            self.redraw_lbl.pack_forget()
            if item["kind"] == "image":
                self._show_image(item)
                if item.get("page"):
                    self.page_lbl.pack(side="left")
            else:
                self._draw(item["drawing"])
                self.redraw_lbl.pack(side="left")
        many = len(self.items) > 1 and not self.busy
        self.count_lbl.configure(text=f"{self.i + 1}/{len(self.items)}" if many else "")
        self.prev_lbl.configure(fg=INK if many and self.i > 0 else BG, cursor="hand2" if many and self.i > 0 else "arrow")
        self.next_lbl.configure(fg=INK if many and self.i < len(self.items) - 1 else BG,
                                cursor="hand2" if many and self.i < len(self.items) - 1 else "arrow")
        self._show()

    def _show_image(self, item: dict) -> None:
        from PIL import ImageTk
        img = item["img"]
        k = min(self.MAX_W * self.s / img.width, self.MAX_H * self.s / img.height, 1.0 * self.s)
        w, h = max(1, int(img.width * k)), max(1, int(img.height * k))
        shown = img if (w, h) == img.size else img.resize((w, h))
        self._photo = ImageTk.PhotoImage(shown)
        self.canvas.configure(width=w, height=h, bg=FIELD)
        self.canvas.create_image(0, 0, anchor="nw", image=self._photo)

    def _draw(self, d: dict) -> None:
        """The drawing, a few shapes at a time (about two seconds in all), like it's being drawn."""
        k = min(self.MAX_W / 400, self.MAX_H / 300) * self.s
        c = self.canvas
        c.configure(width=int(400 * k), height=int(300 * k), bg=d["bg"])
        shapes = d["shapes"]
        per = max(1, -(-len(shapes) // 60))

        def step(j: int):
            for sh in shapes[j:j + per]:
                _shape(c, sh, k)
            if j + per < len(shapes):
                self._anim = self.root.after(30, step, j + per)
            else:
                self._anim = None
        step(0)

    def replay(self) -> None:
        if self.items and self.items[self.i]["kind"] == "drawing":
            self._render()

    def _wiggle(self, pencil, dots, n: int) -> None:
        try:
            self.canvas.move(pencil, 3 if n % 2 else -3, 0)
            self.canvas.itemconfigure(dots, text="•" * (n % 4))
        except tk.TclError:
            return
        self._dots = self.root.after(220, self._wiggle, pencil, dots, n + 1)

    def _stop(self) -> None:
        for job in (self._anim, self._dots):
            if job:
                self.root.after_cancel(job)
        self._anim = self._dots = None

    def _show(self) -> None:
        win = self.win
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        if self.moved and self.shown:
            x, y = win.winfo_x(), win.winfo_y()
        else:
            x, y = self.place(w, h)
        win.geometry(f"{w}x{h}+{x}+{y}")
        if not self.shown:
            win.deiconify()                  # shown without the focus: whatever you're typing in keeps it
            self.shown = True

    def hide(self) -> None:
        self._stop()
        self.busy = ""
        if self.shown:
            self.win.withdraw()
            self.shown = False
            self.moved = False

    # ---- buttons ----
    def open_page(self) -> None:
        item = self.items[self.i] if self.items else None
        if item and item.get("page") and self.link.online:
            self.link.send(f"open {item['page']}")   # through JARVIS: its site checks apply

    def save(self) -> None:
        item = self.items[self.i] if self.items and not self.busy else None
        if not item:
            return
        try:
            if item["kind"] == "image":
                png = item["png"]
            else:
                from jarvis.board import render_png
                png = render_png(item["drawing"])
            folder = Path.home() / "Downloads"
            folder = folder if folder.is_dir() else Path.home()
            name = re.sub(r"[^\w\- ]+", "", item["title"]).strip()[:40] or "picture"
            path = folder / f"Spidey {name} {datetime.now():%Y-%m-%d %H%M%S}.png"
            path.write_bytes(png)
            self._note(f"Saved to {folder.name}")
        except Exception:
            self._note("Couldn't save it.")

    def _note(self, text: str) -> None:
        self.note_lbl.configure(text=text)
        if self.note_job:
            self.root.after_cancel(self.note_job)
        self.note_job = self.root.after(4000, lambda: self.note_lbl.configure(text=""))

    # ---- window ----
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

    def _grab(self, e):
        self._off = (e.x_root - self.win.winfo_x(), e.y_root - self.win.winfo_y())

    def _drag(self, e):
        self.moved = True
        self.win.geometry(f"+{e.x_root - self._off[0]}+{e.y_root - self._off[1]}")


def _clean(text, n: int) -> str:
    from jarvis.board import clean_text
    return clean_text(text, n)


def _decode(png: bytes):
    """PNG bytes → a Pillow image (in a background thread; Tk only gets the finished picture)."""
    from PIL import Image
    im = Image.open(io.BytesIO(png))
    if im.width * im.height > 4_000_000:
        raise ValueError("too big")
    im.load()
    return im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")


def _shape(c: tk.Canvas, sh: dict, k: float) -> None:
    """One checked shape on the canvas, at scale k."""
    p = [v * k for v in sh["p"]]
    if sh["k"] == "text":
        c.create_text(p[0], p[1], text=sh["text"], fill=sh["fill"], anchor=ANCHOR.get(sh["a"], "sw"),
                      font=(FONT, -max(6, int(sh["size"] * k))))
        return
    line, w = sh["line"], max(1.0, sh["w"] * k) if sh["line"] else 0
    if sh["k"] == "rect":
        c.create_rectangle(*p, fill=sh["fill"], outline=line, width=w)
    elif sh["k"] == "oval":
        c.create_oval(*p, fill=sh["fill"], outline=line, width=w)
    elif sh["fill"] and len(p) >= 6:
        c.create_polygon(*p, fill=sh["fill"], outline=line if sh.get("closed") else "", width=w if sh.get("closed") else 0)
        if line and not sh.get("closed"):
            c.create_line(*p, fill=line, width=w, capstyle="round", joinstyle="round")
    elif line:
        c.create_line(*(p + p[:2] if sh.get("closed") else p), fill=line, width=w, capstyle="round", joinstyle="round")
