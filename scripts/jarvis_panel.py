"""JARVIS's small panel, opened from spidey: the conversation, a message box, Chat/Voice mode and status in a little
window beside spidey, instead of the full dashboard in a browser tab. It runs inside spidey's process (tkinter, no
extra program and no browser), and talks to JARVIS through spidey's link."""
from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
import urllib.request
import webbrowser
from datetime import datetime
from urllib.parse import quote

BG = "#fffaf0"          # spidey's bubble colours
EDGE = "#1a1a1a"
INK = "#1a1a1a"
HINT = "#8a8378"
ACCENT = "#c0141f"
SOFT = "#efe7d8"
FIELD = "#ffffff"
LINE = "#ddd4c4"
FONT = "Segoe UI"
STATES = {"idle": ("Ready", "#3aa655"), "listening": ("Listening…", "#2f80ed"), "thinking": ("Thinking…", "#e0a800"),
          "speaking": ("Talking…", ACCENT)}


class Panel:
    W, H = 340, 480

    def __init__(self, root: tk.Tk, link, url: str, scale: float, place, reminders=None, icon_font: str = "Segoe MDL2 Assets"):
        """place(w, h) -> (x, y) puts the panel beside spidey; reminders() -> [(due datetime, text)] soonest first."""
        self.link, self.url, self.s, self.reminders = link, url, scale, reminders
        self.alive = True
        self.inbox: queue.Queue = queue.Queue()        # results from background requests, used on the Tk thread
        self.last_role = ""
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

        # header: status dot, name, state · close (drag the header to move the panel)
        head = tk.Frame(body, bg=BG)
        head.pack(fill="x", padx=pad, pady=(int(10 * s), int(6 * s)))
        self.dot = tk.Canvas(head, width=int(12 * s), height=int(12 * s), bg=BG, highlightthickness=0)
        self.dot_id = self.dot.create_oval(1, 1, int(11 * s), int(11 * s), fill=HINT, outline="")
        self.dot.pack(side="left")
        tk.Label(head, text="JARVIS", bg=BG, fg=INK, font=(FONT, 12, "bold")).pack(side="left", padx=(int(6 * s), 0))
        self.state_lbl = tk.Label(head, text="", bg=BG, fg=HINT, font=(FONT, 9))
        self.state_lbl.pack(side="left", padx=(int(8 * s), 0))
        close = tk.Label(head, text="✕", bg=BG, fg=HINT, font=(FONT, 11), cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: self.close())
        for wdg in (head, self.state_lbl):
            wdg.bind("<ButtonPress-1>", self._grab)
            wdg.bind("<B1-Motion>", self._drag)

        # mode switch + brain / web status
        row = tk.Frame(body, bg=BG)
        row.pack(fill="x", padx=pad, pady=(0, int(8 * s)))
        seg = tk.Frame(row, bg=LINE, padx=1, pady=1)
        seg.pack(side="left")
        self.mode_btns = {}
        for mode, label in (("chat", "Chat"), ("voice", "Voice")):
            b = tk.Label(seg, text=label, font=(FONT, 9, "bold"), padx=int(12 * s), pady=int(3 * s), cursor="hand2")
            b.pack(side="left")
            b.bind("<Button-1>", lambda e, m=mode: self.set_mode(m))
            self.mode_btns[mode] = b
        self.info_lbl = tk.Label(row, text="", bg=BG, fg=HINT, font=(FONT, 8))
        self.info_lbl.pack(side="right")

        # the conversation
        box = tk.Frame(body, bg=LINE, padx=1, pady=1)
        box.pack(fill="both", expand=True, padx=pad)
        self.log = tk.Text(box, height=1, bg=FIELD, fg=INK, relief="flat", bd=0, wrap="word", font=(FONT, 10), padx=int(10 * s),
                           pady=int(8 * s), cursor="arrow", highlightthickness=0, spacing1=int(2 * s))
        self.log.pack(fill="both", expand=True)
        self.log.tag_configure("who_you", foreground=ACCENT, font=(FONT, 8, "bold"), justify="right", spacing1=int(8 * s))
        self.log.tag_configure("you", foreground=INK, justify="right", rmargin=0, lmargin1=int(40 * s), lmargin2=int(40 * s))
        self.log.tag_configure("who_jarvis", foreground=HINT, font=(FONT, 8, "bold"), spacing1=int(8 * s))
        self.log.tag_configure("jarvis", foreground=INK, rmargin=int(30 * s))
        self.log.tag_configure("note", foreground=HINT, font=(FONT, 9, "italic"), justify="center", spacing1=int(8 * s))
        self.log.configure(state="disabled")

        self.rem_lbl = tk.Label(body, text="", bg=BG, fg=HINT, font=(FONT, 8), anchor="w")
        self.rem_lbl.pack(fill="x", padx=pad, pady=(int(4 * s), 0))
        self.kill_lbl = tk.Label(body, text="", bg=BG, fg=HINT, font=(FONT, 8), anchor="w")   # the emergency-stop chord
        self.kill_lbl.pack(fill="x", padx=pad)

        # message box: mic (voice mode) · text · send
        inp = tk.Frame(body, bg=LINE, padx=1, pady=1)
        inp.pack(fill="x", padx=pad, pady=(int(4 * s), int(6 * s)))
        inner = tk.Frame(inp, bg=FIELD)
        inner.pack(fill="x")
        self.mic = tk.Label(inner, text="", font=(icon_font, 12), bg=FIELD, fg=ACCENT, cursor="hand2",
                            padx=int(8 * s))
        self.mic.bind("<Button-1>", lambda e: self._post("/api/listen/once"))
        self.entry = tk.Entry(inner, relief="flat", bd=0, bg=FIELD, fg=INK, insertbackground=ACCENT, font=(FONT, 10),
                              highlightthickness=0)
        self.entry.pack(side="left", fill="x", expand=True, padx=(int(10 * s), 0), ipady=int(7 * s))
        send = tk.Label(inner, text="➤", font=("Segoe UI Symbol", 11), bg=ACCENT, fg="white", cursor="hand2",
                        padx=int(10 * s))
        send.pack(side="right", fill="y")
        send.bind("<Button-1>", lambda e: self.send())
        self.entry.bind("<Return>", lambda e: self.send())
        self.entry.bind("<Escape>", lambda e: self.close())
        win.bind("<Escape>", lambda e: self.close())

        # footer
        foot = tk.Frame(body, bg=BG)
        foot.pack(fill="x", padx=pad, pady=(0, int(8 * s)))
        for text, fn, side in (("New chat", self.new_chat, "left"), ("Stop", lambda: self._post("/api/interrupt"), "left"),
                               ("Full dashboard ↗", self.dashboard, "right")):
            lbl = tk.Label(foot, text=text, bg=BG, fg=ACCENT if side == "left" else HINT, font=(FONT, 9), cursor="hand2")
            lbl.pack(side=side, padx=(0, int(12 * s)) if side == "left" else 0)
            lbl.bind("<Button-1>", lambda e, f=fn: f())
        # "Screen: on/off": may JARVIS read a window when you ask (stays on this laptop). Sends the same words you could type.
        self.screen_lbl = tk.Label(foot, text="", bg=BG, fg=HINT, font=(FONT, 9), cursor="hand2")
        self.screen_lbl.pack(side="left")
        self.screen_lbl.bind("<Button-1>", lambda e: self.toggle_screen())

        self._shown_mode = None
        self._load_history()
        self._refresh()
        win.after(60, lambda: (win.focus_force(), self.entry.focus_set()))

    # ---- actions ----
    def send(self):
        text = self.entry.get().strip()
        if not text:
            return
        if not self.link.online:
            self._note("Can't reach JARVIS right now. Is it running?")
            return
        self.entry.delete(0, "end")
        self.link.send(text)                       # JARVIS echoes it back as a transcript, which adds it to the log

    def set_mode(self, mode: str):
        if mode != self.link.io_mode:
            self.link.post("/api/settings", {"changes": {"io_mode": mode}}, method="PATCH")

    def toggle_screen(self):
        if not self.link.online:
            self._note("Can't reach JARVIS right now. Is it running?")
            return
        on = getattr(self.link, "screen_reading", True)
        self.link.send("stop looking at the screen" if on else "you can look at the screen again")

    def new_chat(self):
        self._post("/api/conversation/new")
        self._clear()
        self._note("New conversation")

    def dashboard(self):
        token = self.link.token
        webbrowser.open(f"{self.url}/?token={quote(token)}" if token else self.url)

    def close(self):
        if self.alive:
            self.alive = False
            self.win.destroy()

    def lift(self):
        self.win.deiconify()
        self.win.lift()
        self.win.after(30, lambda: (self.win.focus_force(), self.entry.focus_set()))

    def _post(self, path: str):
        if self.link.online:
            self.link.post(path)

    # ---- events from JARVIS (forwarded by spidey) ----
    def on_event(self, ev: dict):
        if not self.alive:
            return
        kind = ev.get("type")
        if kind == "transcript" and ev.get("text"):
            self._add(ev.get("role", "assistant"), str(ev["text"]))
        elif kind == "conversation":
            self._clear()
        elif kind == "note":
            self._note(str(ev.get("text", "")))
        elif kind == "confirm" and ev.get("prompt"):
            choices = ev.get("choices") or []
            self._note("Answer " + (" / ".join(choices[:4]) if choices else "yes or no") + " (type it, or use spidey's buttons)")

    # ---- log ----
    def _add(self, role: str, text: str):
        lg = self.log
        lg.configure(state="normal")
        if role == "user":
            lg.insert("end", "You\n", "who_you")
            lg.insert("end", text + "\n", "you")
        elif self.last_role == "assistant":           # the next sentence of the same reply: same paragraph
            lg.delete("end-2c")
            lg.insert("end", " " + text + "\n", "jarvis")
        else:
            lg.insert("end", "JARVIS\n", "who_jarvis")
            lg.insert("end", text + "\n", "jarvis")
        self.last_role = "user" if role == "user" else "assistant"
        lg.configure(state="disabled")
        lg.see("end")

    def _note(self, text: str):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n", "note")
        self.log.configure(state="disabled")
        self.log.see("end")
        self.last_role = ""

    def _clear(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.last_role = ""

    def _load_history(self):
        """The current conversation so far (JARVIS keeps it in data/conversations), fetched in the background."""
        def get(path):
            req = urllib.request.Request(self.url + path, headers=self.link._headers())
            with urllib.request.urlopen(req, timeout=4) as r:
                return json.loads(r.read())

        def go():
            try:
                cid = get("/api/status")["engine"]["conversation_id"]
                msgs = get(f"/api/conversations/{quote(cid)}")["messages"]
            except Exception:
                msgs = []
            self.inbox.put(("history", [m for m in msgs if m.get("role") in ("user", "assistant")][-30:]))
        threading.Thread(target=go, daemon=True).start()

    # ---- live status ----
    def _refresh(self):
        if not self.alive:
            return
        try:
            while True:
                kind, data = self.inbox.get_nowait()
                if kind == "history":
                    for m in data:
                        self.last_role = ""                # stored replies are already whole
                        self._add(m["role"], str(m.get("text", "")))
                    if not data:
                        self._note("Say hi, or try “type Hello World” or “look up today's news”.")
        except queue.Empty:
            pass
        link = self.link
        label, colour = ("Offline", HINT) if not link.online else STATES.get(link.state, (link.state.title(), HINT))
        self.dot.itemconfigure(self.dot_id, fill=colour)
        self.state_lbl.configure(text=label)
        mode = link.io_mode
        if mode != self._shown_mode:
            self._shown_mode = mode
            for m, b in self.mode_btns.items():
                b.configure(bg=ACCENT if m == mode else BG, fg="white" if m == mode else INK)
            if mode == "voice":
                self.mic.pack(side="left", before=self.entry)
            else:
                self.mic.pack_forget()
        brain = "" if link.cloud is None else ("cloud brain" if link.cloud else "local brain")
        web = "web on" if getattr(link, "web", False) else "web: no key"
        self.info_lbl.configure(text=" · ".join(x for x in (brain, web) if x))
        self.rem_lbl.configure(text=self._next_reminder())
        reading = getattr(link, "screen_reading", True)
        self.screen_lbl.configure(text="Screen reading: on" if reading else "Screen reading: off", fg=HINT if reading else ACCENT)
        chord = " + ".join(w.capitalize() for w in str(getattr(link, "kill_chord", "")).split("+") if w)
        if getattr(link, "killed", False):
            self.kill_lbl.configure(text="PAUSED: say or type resume to let JARVIS act again", fg="#d93025")
        else:
            self.kill_lbl.configure(text=f"Stop everything: {chord}" if chord else "", fg=HINT)
        self.win.after(400, self._refresh)

    def _next_reminder(self) -> str:
        try:
            soon = (self.reminders() if self.reminders else [])[:1]
        except Exception:
            return ""
        if not soon:
            return ""
        due, text = soon[0]
        when = due.strftime("%I:%M %p").lstrip("0") if due.date() == datetime.now().date() else due.strftime("%a %I:%M %p")
        return f"Next reminder: {when} · {text}"

    # ---- moving ----
    def _grab(self, e):
        self._off = (e.x_root - self.win.winfo_x(), e.y_root - self.win.winfo_y())

    def _drag(self, e):
        self.win.geometry(f"+{e.x_root - self._off[0]}+{e.y_root - self._off[1]}")
