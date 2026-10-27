"""The Apps list, opened from Spidey's panel (or the right-click menu): every app on this laptop, and what Spidey may do
with each one. Off = never touched · Open = may open it and move its window · Full = may also click and type in it.

It runs inside spidey's process (tkinter, no browser). It only shows choices and sends them to JARVIS
(PATCH /api/settings app_permissions): anything that gives Spidey MORE access is held until you say or type "yes" to
Spidey, so no other program on the laptop can hand itself access through this window. Taking access away is instant."""
from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
import urllib.error
import urllib.request

from jarvis_panel import ACCENT, BG, EDGE, FIELD, FONT, HINT, INK, LINE, SOFT

LEVELS = (("off", "Off"), ("open", "Open"), ("full", "Full"))
HELP = ("Off: I never open, read or touch it.  Open: I may open it and move its window.  Full: I may also click and type "
        "in it (never password or card boxes, and I ask before Send, Buy or Delete). Giving more access asks you for a yes.")


class AppsWindow:
    W, H = 430, 590

    def __init__(self, root: tk.Tk, link, url: str, scale: float, place):
        self.link, self.url, self.s = link, url, scale
        self.alive = True
        self.inbox: queue.Queue = queue.Queue()
        self.apps: list[dict] = []
        self.rows: dict[str, dict] = {}            # key -> {"frame", "buttons", "entry"}
        self.changes: dict[str, str] = {}          # key -> new level (not saved yet)
        s = scale
        w, h = int(self.W * s), int(self.H * s)
        win = self.win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=EDGE)
        x, y = place(w, h)
        win.geometry(f"{w}x{h}+{x}+{y}")
        body = tk.Frame(win, bg=BG)
        body.place(x=2, y=2, width=w - 4, height=h - 4)
        pad = int(12 * s)

        head = tk.Frame(body, bg=BG)
        head.pack(fill="x", padx=pad, pady=(int(10 * s), int(2 * s)))
        title = tk.Label(head, text="Apps Spidey may use", bg=BG, fg=INK, font=(FONT, 12, "bold"))
        title.pack(side="left")
        close = tk.Label(head, text="✕", bg=BG, fg=HINT, font=(FONT, 11), cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: self.close())
        for wdg in (head, title):
            wdg.bind("<ButtonPress-1>", self._grab)
            wdg.bind("<B1-Motion>", self._drag)
        tk.Label(body, text=HELP, bg=BG, fg=HINT, font=(FONT, 8), justify="left", anchor="w",
                 wraplength=w - 2 * pad - 8).pack(fill="x", padx=pad, pady=(0, int(6 * s)))

        find = tk.Frame(body, bg=LINE, padx=1, pady=1)
        find.pack(fill="x", padx=pad)
        self.query = tk.StringVar()
        self.search = tk.Entry(find, textvariable=self.query, relief="flat", bd=0, bg=FIELD, fg=INK, insertbackground=ACCENT,
                               font=(FONT, 10), highlightthickness=0)
        self.search.pack(fill="x", ipady=int(5 * s), padx=(int(8 * s), 0))
        self.query.trace_add("write", lambda *_: self._filter())

        box = tk.Frame(body, bg=LINE, padx=1, pady=1)
        box.pack(fill="both", expand=True, padx=pad, pady=(int(6 * s), 0))
        self.canvas = tk.Canvas(box, bg=FIELD, highlightthickness=0, bd=0)
        bar = tk.Scrollbar(box, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=FIELD)
        self._inner_id = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._inner_id, width=e.width))
        win.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"))
        self.loading = tk.Label(self.inner, text="Finding your apps…", bg=FIELD, fg=HINT, font=(FONT, 9, "italic"))
        self.loading.pack(pady=int(20 * s))

        foot = tk.Frame(body, bg=BG)
        foot.pack(fill="x", padx=pad, pady=(int(6 * s), int(8 * s)))
        refresh = tk.Label(foot, text="Refresh list", bg=BG, fg=HINT, font=(FONT, 9), cursor="hand2")
        refresh.pack(side="left")
        refresh.bind("<Button-1>", lambda e: self.load(refresh=True))
        self.save_btn = tk.Label(foot, text="Save", bg=SOFT, fg=HINT, font=(FONT, 9, "bold"), padx=int(14 * s),
                                 pady=int(3 * s), cursor="hand2")
        self.save_btn.pack(side="right")
        self.save_btn.bind("<Button-1>", lambda e: self.save())
        self.info = tk.Label(body, text="", bg=BG, fg=HINT, font=(FONT, 8), anchor="w", justify="left",
                             wraplength=w - 2 * pad)
        self.info.pack(fill="x", padx=pad, pady=(0, int(8 * s)), before=foot)
        win.bind("<Escape>", lambda e: self.close())

        self.load()
        self._pump()
        win.after(60, lambda: (win.focus_force(), self.search.focus_set()))

    # ---- talking to JARVIS (in the background; results come back through the inbox) ----
    def _request(self, path: str, body: dict | None = None, method: str = "GET"):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={**self.link._headers(), "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=70) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"{}")
            except ValueError:
                return exc.code, {}

    def load(self, refresh: bool = False):
        if refresh:
            self.info.configure(text="Asking Windows for your apps again (a few seconds)…", fg=HINT)

        def go():
            try:
                self.inbox.put(("apps", self._request("/api/apps" + ("?refresh=true" if refresh else ""))))
            except Exception:
                self.inbox.put(("apps", (0, {})))
        threading.Thread(target=go, daemon=True).start()

    def save(self):
        if not self.changes:
            self.info.configure(text="Nothing changed yet.", fg=HINT)
            return
        changes = dict(self.changes)
        self.info.configure(text="Saving…", fg=HINT)

        def go():
            try:
                self.inbox.put(("saved", self._request("/api/settings", {"changes": {"app_permissions": changes}}, "PATCH")))
            except Exception:
                self.inbox.put(("saved", (0, {})))
        threading.Thread(target=go, daemon=True).start()

    def _pump(self):
        if not self.alive:
            return
        try:
            while True:
                kind, (status, data) = self.inbox.get_nowait()
                if kind == "apps":
                    if status == 200:
                        self._show(data.get("apps") or [])
                        if data.get("access") == "any":
                            self.info.configure(text="Right now I may control every app that isn't locked (app_access "
                                                     "is “any”). Your choices here still count for Off.", fg=ACCENT)
                        elif not self.changes:
                            self.info.configure(text=f"{len(self.apps)} apps. Pick a level, then Save.", fg=HINT)
                    else:
                        self.loading.configure(text="Can't reach Spidey right now. Is it running?")
                elif kind == "saved":
                    if status == 200:
                        self.info.configure(text="Saved.", fg="#3aa655")
                        self.changes.clear()
                        self.load()
                    elif status == 202:
                        self.info.configure(text="Almost done: say or type “yes” to Spidey to allow it. (Nothing changes "
                                                 "until you do. That's so no other program can give itself access.)",
                                            fg=ACCENT)
                        self.changes.clear()
                        self.win.after(1500, self.load)
                    else:
                        self.info.configure(text=f"Couldn't save: {data.get('detail') or 'Spidey is not answering'}",
                                            fg="#d93025")
                    self._paint_save()
        except queue.Empty:
            pass
        self.win.after(150, self._pump)

    # ---- the list ----
    def _show(self, apps: list[dict]):
        seen: set = set()
        apps = [a for a in apps if not (a["key"] in seen or seen.add(a["key"]))]   # one row per program
        self.apps = apps
        self.loading.pack_forget()
        for row in self.rows.values():
            row["frame"].destroy()
        self.rows.clear()
        s = self.s
        for a in apps:
            fr = tk.Frame(self.inner, bg=FIELD)
            left = tk.Frame(fr, bg=FIELD)
            left.pack(side="left", fill="x", expand=True, padx=(int(8 * s), 0), pady=int(3 * s))
            tk.Label(left, text=a["name"], bg=FIELD, fg=INK if a["kind"] != "locked" else HINT, font=(FONT, 9),
                     anchor="w").pack(fill="x")
            if a.get("note"):
                tk.Label(left, text=a["note"], bg=FIELD, fg=HINT, font=(FONT, 7), anchor="w", justify="left",
                         wraplength=int(230 * s)).pack(fill="x")
            buttons = {}
            if a["kind"] == "locked":
                tk.Label(fr, text="locked", bg=FIELD, fg=HINT, font=(FONT, 8, "italic"), padx=int(8 * s)).pack(side="right")
            else:
                seg = tk.Frame(fr, bg=LINE, padx=1, pady=1)
                seg.pack(side="right", padx=int(6 * s))
                for level, label in LEVELS:
                    if level == "full" and (a["kind"] == "web" or not a.get("exe")):
                        continue                         # can't tell which program it is: no control inside it
                    b = tk.Label(seg, text=label, font=(FONT, 8, "bold"), padx=int(7 * s), pady=int(1 * s), cursor="hand2")
                    b.pack(side="left")
                    b.bind("<Button-1>", lambda e, k=a["key"], lv=level: self._pick(k, lv))
                    buttons[level] = b
            self.rows[a["key"]] = {"frame": fr, "buttons": buttons, "entry": a}
            self._paint(a["key"])
        self._filter()

    def _pick(self, key: str, level: str):
        entry = self.rows[key]["entry"]
        if level == entry["level"]:
            self.changes.pop(key, None)
        else:
            self.changes[key] = level
        self._paint(key)
        self._paint_save()
        n = len(self.changes)
        self.info.configure(text=f"{n} change{'s' if n != 1 else ''} not saved yet." if n else "No changes.", fg=HINT)

    def _paint(self, key: str):
        row = self.rows[key]
        now = self.changes.get(key, row["entry"]["level"])
        for level, b in row["buttons"].items():
            on = level == now
            colour = {"off": INK, "open": "#2f80ed", "full": ACCENT}[level]
            b.configure(bg=colour if on else FIELD, fg="white" if on else HINT)

    def _paint_save(self):
        self.save_btn.configure(bg=ACCENT if self.changes else SOFT, fg="white" if self.changes else HINT)

    def _filter(self):
        want = self.query.get().strip().lower()
        for row in self.rows.values():
            row["frame"].pack_forget()
        for a in self.apps:
            row = self.rows.get(a["key"])
            if row and (not want or want in a["name"].lower() or want in (a.get("exe") or "")):
                row["frame"].pack(fill="x")
        self.canvas.yview_moveto(0)

    def close(self):
        if self.alive:
            self.alive = False
            self.win.destroy()

    def lift(self):
        self.win.deiconify()
        self.win.lift()
        self.load()

    def _grab(self, e):
        self._off = (e.x_root - self.win.winfo_x(), e.y_root - self.win.winfo_y())

    def _drag(self, e):
        self.win.geometry(f"+{e.x_root - self._off[0]}+{e.y_root - self._off[1]}")
