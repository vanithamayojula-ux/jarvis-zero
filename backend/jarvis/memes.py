"""Spidey's meme reactions: after every reply, a meme that fits the moment pops up in a small panel beside spidey.

The cloud model (a different Groq model from the chat one, so the chat keeps its own free per-minute budget) picks one
of the templates below and writes short captions about what you're talking about. Without the cloud (offline, or its
free limit reached) a mood is guessed from the words and a matching template is shown as a plain reaction picture.

Templates come from Imgflip's public list of popular memes (api.imgflip.com/get_memes: no key, no account), are
downloaded once from i.imgflip.com only, checked as real pictures and kept in data/memes. Captions are drawn here on
the laptop (Pillow, Impact), so nothing about the conversation goes to Imgflip. Display only: a meme can't do anything."""
from __future__ import annotations

import io
import json
import random
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .board import clean_text

LIST_URL = "https://api.imgflip.com/get_memes"
UA = "JARVIS-Zero/1.0 (personal desktop assistant, single user)"
MAX_BYTES = 6_000_000
OUT_MAX = 520                      # the finished meme's longest side, in pixels
MAX_CAPTION = 60

MOODS = ("happy", "laughing", "excited", "proud", "love", "sad", "tired", "annoyed", "confused", "surprised",
         "thinking", "smug", "scared", "awkward", "bored", "agree", "disagree", "waiting", "chaos", "wholesome")

# key: (Imgflip template id, layout, moods it fits, when to use it, as told to the model)
# layouts: "tb" = top/bottom captions over the picture; "right2" = two rows, text on the right half (Drake);
# "left3" / "left4" = three / four rows, text on the left half (Panik Kalm Panik, Expanding Brain)
TEMPLATES: dict[str, tuple[str, str, tuple[str, ...], str]] = {
    "drake": ("181913649", "right2", ("agree", "disagree", "smug"), "rejecting the first thing, loving the second"),
    "tuxedo_pooh": ("178591752", "right2", ("smug", "proud"), "plain way vs the fancy, classy way of saying it"),
    "panik_kalm": ("226297822", "left3", ("scared", "chaos"), "a scare, then relief, then a new scare"),
    "expanding_brain": ("93895088", "left4", ("thinking", "smug"), "four ideas, each more galaxy-brained"),
    "leo_cheers": ("5496396", "tb", ("proud", "happy", "excited"), "congrats, a win, cheers to that"),
    "laughing_leo": ("259237855", "tb", ("laughing",), "something genuinely funny or absurd"),
    "roll_safe": ("89370399", "tb", ("smug", "thinking"), "a clever (or silly) life hack, 'can't X if you don't Y'"),
    "disaster_girl": ("97984", "tb", ("chaos", "smug"), "things going up in flames, mischief"),
    "hide_the_pain": ("27813981", "tb", ("awkward", "tired", "sad"), "smiling through the pain"),
    "this_is_fine": ("55311130", "tb", ("chaos", "tired", "scared"), "everything is going wrong but staying calm"),
    "surprised_pikachu": ("155067746", "tb", ("surprised",), "shocked at a totally predictable result"),
    "waiting_skeleton": ("4087833", "tb", ("waiting", "bored"), "waiting forever for something"),
    "sad_pablo": ("80707627", "tb", ("sad", "bored"), "lonely, sad, waiting around with nothing to do"),
    "bernie_asking": ("222403160", "tb", ("annoyed",), "asking for the same thing once again"),
    "change_my_mind": ("129242436", "tb", ("smug", "agree"), "a strong opinion, 'change my mind'"),
    "woman_yelling_cat": ("188390779", "tb", ("annoyed", "confused"), "an argument, one side yelling, the other confused"),
    "monkey_puppet": ("148909805", "tb", ("awkward",), "awkwardly looking away"),
    "mocking_spongebob": ("102156234", "tb", ("annoyed", "smug"), "sarcastically repeating something silly"),
    "futurama_fry": ("61520", "tb", ("confused", "thinking"), "'not sure if X or Y'"),
    "one_does_not_simply": ("61579", "tb", ("thinking",), "'one does not simply X': something is harder than it sounds"),
    "ancient_aliens": ("101470", "tb", ("confused", "thinking"), "a wild explanation: 'aliens'"),
    "batman_slap": ("438680", "tb", ("annoyed", "disagree"), "shutting down a bad take mid-sentence"),
    "evil_kermit": ("84341851", "tb", ("smug",), "the inner voice tempting you to do the bad thing"),
    "none_of_my_business": ("16464531", "tb", ("smug",), "sipping tea, a judgy observation"),
    "skeptical_kid": ("101288", "tb", ("confused", "disagree"), "doubting a claim that sounds too good"),
    "yoda": ("14371066", "tb", ("thinking", "wholesome"), "wise advice"),
    "absolute_cinema": ("505705955", "tb", ("excited", "love", "happy"), "something is a masterpiece"),
    "spiderman_pointing": ("110133729", "tb", ("laughing", "agree"), "two things that are exactly the same"),
    "scientist_myself": ("133946291", "tb", ("proud", "smug"), "'I'm something of an X myself'"),
    "uno_draw_25": ("217743513", "tb", ("disagree", "tired"), "would rather do anything than the thing"),
    "always_has_been": ("252600902", "tb", ("surprised", "smug"), "a realization: 'wait, it's all X?' 'always has been'"),
    "they_dont_know": ("284929871", "tb", ("smug", "proud", "wholesome"), "secretly proud of something nobody knows"),
    "squidward_window": ("67452763", "tb", ("sad", "bored"), "watching others have fun"),
    "anime_hiding": ("224514655", "tb", ("scared",), "hiding from something scary"),
    "trophy": ("3218037", "tb", ("proud",), "a small achievement nobody rewards"),
}
_BOXES = {"right2": [(0.52, 0.02, 0.46, 0.46), (0.52, 0.52, 0.46, 0.46)],
          "left3": [(0.02, i / 3 + 0.01, 0.47, 1 / 3 - 0.02) for i in range(3)],
          "left4": [(0.02, i / 4 + 0.01, 0.48, 1 / 4 - 0.02) for i in range(4)]}
SLOTS = {"tb": 2, "right2": 2, "left3": 3, "left4": 4}

PICK_PROMPT = (
    "You are Spidey's meme sense. Spidey is a friendly AI that lives on someone's laptop as a little Spider-Man mask "
    "and reacts to conversations like a funny friend sending a meme. Pick the ONE template that fits the moment best and "
    "write captions about what they're actually talking about.\n"
    "Write it like a meme people really share: short (at most 7 words a caption), punchy, relatable, often from their "
    "point of view (me, my brain, my code), with a funny twist. For two-row and multi-row templates the captions are just "
    "the things themselves: never write words like 'rejecting', 'loving', 'plain', 'fancy' or 'panik'. Never just repeat "
    "the conversation back. Be kind: never mock the person, no slurs, nothing adult, no emojis, hashtags or links.\n"
    "Examples:\n"
    'exam tomorrow, not started -> {{"t":"panik_kalm","mood":"scared","c":["Exam tomorrow","Only three topics matter",'
    '"I haven\'t opened the book"]}}\n'
    'fixed a bug after 6 hours -> {{"t":"hide_the_pain","mood":"tired","c":["Fixed it after 6 hours","It was one typo"]}}\n'
    'open youtube -> {{"t":"roll_safe","mood":"smug","c":["Can\'t be bored","If YouTube is always one word away"]}}\n'
    'set a timer for 10 minutes -> {{"t":"waiting_skeleton","mood":"waiting","c":["Me after setting a 10 minute timer",'
    '"Still waiting"]}}\n'
    "For a plain command, joke about the situation behind it (waiting, procrastinating, being lazy, needing a break), "
    "never Spidey's own reply words.\n"
    'tea or coffee -> {{"t":"drake","mood":"smug","c":["Coffee jitters at 2am","Tea and inner peace"]}}\n'
    "Templates (key: when it fits [how many captions]):\n{templates}\n"
    "Moods: " + ", ".join(MOODS) + "\n"
    'Reply with ONLY JSON: {{"t": "<template key>", "mood": "<mood>", "c": ["caption", ...]}}')


def prompt(avoid: list[str] | tuple = ()) -> str:
    """avoid = templates shown lately (so it doesn't send the same meme every time)."""
    lines = [f"{k}: {use} [{SLOTS[layout]}]" for k, (_, layout, _, use) in TEMPLATES.items()]
    text = PICK_PROMPT.format(templates="\n".join(lines))
    keys = [k for k in avoid if k in TEMPLATES]
    return text + (f"\nDon't use these, they were just shown: {', '.join(keys)}." if keys else "")


def parse_pick(text: str) -> dict | None:
    """The model's answer → {"t", "mood", "c"} with a known template, a known mood and safe short captions."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return None
    key = str(d.get("t") or "").strip().lower()
    if key not in TEMPLATES:
        return None
    _, layout, moods, _ = TEMPLATES[key]
    mood = str(d.get("mood") or "").strip().lower()
    mood = mood if mood in MOODS else moods[0]
    raw = d.get("c") if isinstance(d.get("c"), list) else []
    caps = [_caption(c) for c in raw[:SLOTS[layout]]]
    caps += [""] * (SLOTS[layout] - len(caps))
    return {"t": key, "mood": mood, "c": caps}


def _caption(c) -> str:
    c = clean_text(str(c or ""), MAX_CAPTION)
    c = re.sub(r"(?:https?://|www\.)\S+|#\w+|[\"“”]", "", c)     # no links, hashtags or stray quotes
    return re.sub(r"\s+", " ", c).strip(" '")


# ---- a mood from the words alone (no cloud): spidey reacts at once, and it's the meme when the cloud can't help ----
_MOOD_WORDS = [
    ("laughing", r"\b(?:lol|lmao|rofl|haha\w*|hehe\w*|funny|hilarious|joke|kidding|😂|🤣)"),
    ("love", r"\b(?:love|cute|adorable|sweet|crush|❤|😍)"),
    ("proud", r"\b(?:i (?:did it|passed|won|finished|cleared|got (?:the|a|an) (?:job|offer|internship))|nailed it|proud|"
              r"got (?:selected|accepted|in|placed|hired|promoted))\b"),
    ("excited", r"\b(?:awesome|amazing|let'?s go|hyped|can'?t wait|yay|woo+|epic|insane|fire)\b"),
    ("sad", r"\b(?:sad|miss (?:her|him|them|you)|lonely|crying|cried|depress\w*|broke up|heartbroken|failed)\b"),
    ("tired", r"\b(?:tired|sleepy|exhausted|long day|no sleep|so done|drained)\b"),
    ("annoyed", r"\b(?:annoying|annoyed|hate|ugh+|angry|mad|frustrat\w*|wtf|irritat\w*)\b"),
    ("scared", r"\b(?:scared|afraid|nervous|anxious|panic\w*|exam (?:tomorrow|today)|deadline)\b"),
    ("surprised", r"\b(?:wow|whoa|no way|seriously|omg|really\?|can'?t believe)"),
    ("confused", r"\b(?:confused|don'?t (?:get|understand)|makes no sense|huh)\b"),
    ("bored", r"\b(?:bored|boring|nothing to do)\b"),
    ("waiting", r"\b(?:waiting|still loading|taking forever|so slow)\b"),
    ("happy", r"\b(?:great|nice|good|glad|cool|thanks|thank you|sweet)\b"),
    ("thinking", r"\b(?:why|how come|what if|should i|which (?:one|is)|wondering)\b"),
]


def guess_mood(*texts: str) -> str:
    t = " ".join(x or "" for x in texts).lower()
    for mood, pat in _MOOD_WORDS:
        if re.search(pat, t):
            return mood
    return "happy"


def fallback_pick(mood: str, rng: random.Random | None = None) -> dict:
    """A plain reaction picture (no captions) for the mood."""
    r = rng or random
    keys = [k for k, (_, layout, moods, _) in TEMPLATES.items() if mood in moods and layout == "tb"] or ["leo_cheers"]
    key = r.choice(keys)
    return {"t": key, "mood": mood if mood in MOODS else "happy", "c": [""] * SLOTS[TEMPLATES[key][1]]}


# ---- drawing the captions ----
_FONTS = {"meme": [r"C:\Windows\Fonts\impact.ttf", r"C:\Windows\Fonts\arialbd.ttf"],
          "plain": [r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\segoeuib.ttf"]}


def _font(style: str, size: int):
    from PIL import ImageFont
    for path in _FONTS[style]:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _fit(draw, text: str, style: str, w: int, h: int, max_size: int = 0):
    """The biggest font (and wrapped lines) that fits the box."""
    words = text.split()
    for size in range(min(max(12, h // 2), max_size or 999), 9, -2):
        font = _font(style, size)
        lines, cur = [], ""
        for word in words:
            trial = f"{cur} {word}".strip()
            if cur and draw.textlength(trial, font=font) > w:
                lines.append(cur)
                cur = word
            else:
                cur = trial
        lines.append(cur)
        line_h = int(size * 1.12)
        if len(lines) * line_h <= h and all(draw.textlength(ln, font=font) <= w for ln in lines):
            return font, lines, line_h
    font = _font(style, 10)
    return font, [text[:40]], 12


def render(picture: bytes, layout: str, captions: list[str]) -> bytes:
    """The template with its captions drawn on, as a PNG no bigger than OUT_MAX on its longest side."""
    from PIL import Image, ImageDraw
    im = Image.open(io.BytesIO(picture))
    im = im.convert("RGB")
    k = min(1.0, OUT_MAX / max(im.size))
    if k < 1.0:
        im = im.resize((max(1, int(im.width * k)), max(1, int(im.height * k))))
    W, H = im.size
    d = ImageDraw.Draw(im)
    if layout == "tb":
        boxes = [(0.04, 0.02, 0.92, 0.26, "top"), (0.04, 0.72, 0.92, 0.26, "bottom")]
        style = "meme"
    else:
        boxes = [(*b, "middle") for b in _BOXES[layout]]
        style = "plain"
    jobs = []
    for (bx, by, bw, bh, align), text in zip(boxes, captions):
        if text:
            pad = int(0.04 * bw * W)                 # a little room inside the box
            jobs.append((text.upper() if style == "meme" else text, int(bx * W) + pad, int(by * H),
                         int(bw * W) - 2 * pad, int(bh * H), align))
    # rows of one meme share a font size (one short caption shouldn't shout over the others)
    size = min((_fit(d, t, style, w, h)[0].size for t, _, _, w, h, _ in jobs if style == "plain"), default=0)
    for text, x0, y0, w, h, align in jobs:
        font, lines, line_h = _fit(d, text, style, w, h, max_size=size)
        total = len(lines) * line_h
        y = y0 if align == "top" else y0 + h - total if align == "bottom" else y0 + (h - total) // 2
        for ln in lines:
            x = x0 + (w - d.textlength(ln, font=font)) / 2
            if style == "meme":
                d.text((x, y), ln, font=font, fill="white", stroke_width=max(2, font.size // 12), stroke_fill="black")
            else:
                d.text((x, y), ln, font=font, fill="black")
            y += line_h
    out = io.BytesIO()
    im.save(out, "PNG", optimize=True)
    return out.getvalue()


def allowed_template_url(url: str) -> bool:
    p = urlparse(url or "")
    return p.scheme == "https" and p.hostname == "i.imgflip.com" and not p.username and p.port in (None, 443) and \
        bool(re.fullmatch(r"/[A-Za-z0-9]{1,12}\.(?:jpg|jpeg|png)", p.path))


class MemeMaker:
    """Finds a template's picture (downloaded once into data/memes) and draws the captions on it."""
    LIST_MAX_AGE = 7 * 86400

    def __init__(self, folder: Path, client: httpx.Client | None = None):
        self.folder = Path(folder)
        self.client = client or httpx.Client(timeout=10.0, headers={"user-agent": UA}, follow_redirects=False)
        self._lock = threading.Lock()

    def _urls(self) -> dict[str, str]:
        """Imgflip template id → picture address, from its public list (kept for a week)."""
        cache = self.folder / "templates.json"
        try:
            if time.time() - cache.stat().st_mtime < self.LIST_MAX_AGE:
                return json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        r = self.client.get(LIST_URL)
        r.raise_for_status()
        urls = {str(m.get("id")): str(m.get("url")) for m in (r.json().get("data") or {}).get("memes", [])
                if allowed_template_url(str(m.get("url")))}
        self.folder.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(urls), encoding="utf-8")
        return urls

    def picture(self, key: str) -> bytes:
        tid = TEMPLATES[key][0]
        with self._lock:
            have = next(iter(self.folder.glob(f"{tid}.*")), None) if self.folder.exists() else None
            if have:
                return have.read_bytes()
            url = self._urls().get(tid)
            if not url:
                raise LookupError(f"template {key} isn't in Imgflip's list right now")
            r = self.client.get(url)
            r.raise_for_status()
            if not r.headers.get("content-type", "").startswith("image/") or len(r.content) > MAX_BYTES:
                raise ValueError("not a picture")
            from PIL import Image
            Image.open(io.BytesIO(r.content)).verify()            # a real picture, or it isn't kept
            self.folder.mkdir(parents=True, exist_ok=True)
            (self.folder / f"{tid}{Path(urlparse(url).path).suffix.lower()}").write_bytes(r.content)
            return r.content

    def make(self, pick: dict) -> bytes:
        key = pick["t"]
        return render(self.picture(key), TEMPLATES[key][1], pick.get("c") or [])
