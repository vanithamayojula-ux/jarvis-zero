"""Spidey's picture board: a small window beside the floating bot that shows a picture or a drawing.

Pictures come from Wikipedia (free, no key, no account): the lead image of the best-matching article. Only the search
words leave the laptop, the image is only ever downloaded from upload.wikimedia.org, and it is decoded and re-saved as
a plain PNG here (size-capped) before spidey sees it. Nothing is written to disk: the last few pictures live in memory.

Drawings: the model writes a small SVG; svg_to_drawing turns it into a short list of plain shapes (polygons, lines,
ovals, rectangles, short text) and validate_drawing checks that list. Spidey checks it again before drawing, so a bad or
hostile event can only ever become a few coloured shapes inside the board, never a window, a link or a button.
"""
from __future__ import annotations

import io
import math
import re
import threading
import uuid
import xml.etree.ElementTree as ET
from collections import OrderedDict
from pathlib import Path
from urllib.parse import quote, urlparse

W, H = 400, 300                    # the drawing area, in drawing units (spidey scales it)
MAX_SHAPES = 220
MAX_NUMS = 600                     # numbers in one shape (300 points)
MAX_TOTAL = 16000                  # numbers in a whole drawing
MAX_TEXT, MAX_TITLE = 40, 60
MAX_BYTES = 6_000_000              # a downloaded picture
MAX_FILE = 40_000_000              # a picture file of yours
MAX_PIXELS = 50_000_000            # refuse to decode anything bigger (decompression bombs)
OUT_W, OUT_H = 900, 700            # pictures are shrunk to fit this before spidey gets them
IMAGE_HOSTS = {"upload.wikimedia.org", "thumb.wikimedia.org"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
WIKI_API = "https://en.wikipedia.org/w/api.php"
# Wikimedia refuses agents without a link in them (403); this is one person's assistant, one picture at a time
USER_AGENT = "JARVIS-Zero/1.0 (https://localhost/; personal desktop assistant, single user)"
KINDS = {"poly", "oval", "rect", "text"}
_HEX = re.compile(r"^#[0-9a-f]{6}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f ­​-‏‪-‮⁦-⁩]")

NAMED = {
    "black": "#000000", "white": "#ffffff", "red": "#ff0000", "green": "#008000", "blue": "#0000ff", "yellow": "#ffff00",
    "orange": "#ffa500", "purple": "#800080", "pink": "#ffc0cb", "brown": "#a52a2a", "gray": "#808080", "grey": "#808080",
    "silver": "#c0c0c0", "gold": "#ffd700", "navy": "#000080", "teal": "#008080", "maroon": "#800000", "olive": "#808000",
    "lime": "#00ff00", "aqua": "#00ffff", "cyan": "#00ffff", "magenta": "#ff00ff", "fuchsia": "#ff00ff",
    "skyblue": "#87ceeb", "lightblue": "#add8e6", "darkblue": "#00008b", "lightgreen": "#90ee90",
    "darkgreen": "#006400", "forestgreen": "#228b22", "lightgray": "#d3d3d3", "lightgrey": "#d3d3d3",
    "darkgray": "#a9a9a9", "darkgrey": "#a9a9a9", "beige": "#f5f5dc", "tan": "#d2b48c", "khaki": "#f0e68c",
    "salmon": "#fa8072", "coral": "#ff7f50", "tomato": "#ff6347", "crimson": "#dc143c", "violet": "#ee82ee",
    "indigo": "#4b0082", "turquoise": "#40e0d0", "chocolate": "#d2691e", "sienna": "#a0522d", "wheat": "#f5deb3",
    "ivory": "#fffff0", "snow": "#fffafa", "lavender": "#e6e6fa", "peru": "#cd853f", "saddlebrown": "#8b4513",
    "darkorange": "#ff8c00", "steelblue": "#4682b4", "royalblue": "#4169e1", "dodgerblue": "#1e90ff",
    "deepskyblue": "#00bfff", "seagreen": "#2e8b57", "limegreen": "#32cd32", "yellowgreen": "#9acd32",
    "darkred": "#8b0000", "firebrick": "#b22222", "hotpink": "#ff69b4", "slategray": "#708090", "dimgray": "#696969",
}


class PictureError(Exception):
    pass


# ---------------- checking (both ends) ----------------

def clean_text(text, n: int = MAX_TEXT) -> str:
    """Plain text only: control and invisible characters removed, spaces squeezed, at most n characters."""
    return re.sub(r"\s+", " ", _CONTROL.sub(" ", str(text or ""))).strip()[:n]


def colour(v) -> str | None:
    """'#rrggbb', '' for none/transparent, or None when it isn't a colour we know."""
    s = str(v or "").strip().lower()
    if s in ("none", "transparent"):
        return ""
    if s in NAMED:
        return NAMED[s]
    if re.fullmatch(r"#[0-9a-f]{3}", s):
        return "#" + "".join(ch * 2 for ch in s[1:])
    if re.fullmatch(r"#[0-9a-f]{6}", s):
        return s
    m = re.fullmatch(r"rgba?\(\s*([\d.]+%?)\s*,\s*([\d.]+%?)\s*,\s*([\d.]+%?)(?:\s*,\s*[\d.]+%?)?\s*\)", s)
    if m:
        vals = []
        for g in m.groups():
            x = float(g[:-1]) * 2.55 if g.endswith("%") else float(g)
            vals.append(max(0, min(255, round(x))))
        return "#%02x%02x%02x" % tuple(vals)
    return None


def _num(v) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or abs(v) > 1e6:
        return None
    return float(v)


def validate_drawing(d) -> dict | None:
    """{'title', 'bg', 'shapes'} with every shape checked and every number clamped near the drawing area, or None.
    A shape is {'k': 'poly'|'oval'|'rect'|'text', 'p': [numbers], 'fill': '#rrggbb'|'', 'line': '#rrggbb'|'', 'w': width}
    plus 'closed' (poly) or 'text', 'size', 'a' (text: start/middle/end)."""
    if not isinstance(d, dict) or not isinstance(d.get("shapes"), list):
        return None
    out, total = [], 0
    for s in d["shapes"][:MAX_SHAPES]:
        if not isinstance(s, dict) or s.get("k") not in KINDS or not isinstance(s.get("p"), list):
            continue
        k = s["k"]
        nums = [_num(v) for v in s["p"][:MAX_NUMS + 1]]
        if None in nums or len(nums) > MAX_NUMS:
            continue
        if (k == "poly" and (len(nums) < 4 or len(nums) % 2)) or (k in ("oval", "rect") and len(nums) != 4) or \
                (k == "text" and len(nums) != 2):
            continue
        total += len(nums)
        if total > MAX_TOTAL:
            break
        p = [round(max(-40.0, min((W if i % 2 == 0 else H) + 40.0, v)), 1) for i, v in enumerate(nums)]
        fill = s.get("fill", "")
        line = s.get("line", "")
        fill = fill if isinstance(fill, str) and (fill == "" or _HEX.match(fill)) else ""
        line = line if isinstance(line, str) and (line == "" or _HEX.match(line)) else ""
        if k == "text":
            text = clean_text(s.get("text"))
            if not text or not fill:
                continue
            size = _num(s.get("size")) or 14.0
            anchor = s.get("a") if s.get("a") in ("start", "middle", "end") else "start"
            out.append({"k": "text", "p": p, "text": text, "size": round(max(6.0, min(48.0, size)), 1), "fill": fill,
                        "a": anchor})
            continue
        w = _num(s.get("w"))
        w = round(max(0.0, min(16.0, 1.0 if w is None else w)), 2)
        if not fill and not (line and w > 0):
            continue                                  # invisible
        shape = {"k": k, "p": p, "fill": fill, "line": line, "w": w}
        if k == "poly":
            shape["closed"] = bool(s.get("closed"))
        out.append(shape)
    if not out:
        return None
    bg = d.get("bg")
    return {"title": clean_text(d.get("title"), MAX_TITLE),
            "bg": bg if isinstance(bg, str) and _HEX.match(bg) else "#ffffff", "shapes": out}


# ---------------- SVG → shapes ----------------

def _matrix_mul(m, n):
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (a * a2 + c * b2, b * a2 + d * b2, a * c2 + c * d2, b * c2 + d * d2, a * e2 + c * f2 + e, b * e2 + d * f2 + f)


def _apply(m, x, y):
    a, b, c, d, e, f = m
    return a * x + c * y + e, b * x + d * y + f


_NUMBER = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"


def _nums(s: str) -> list[float]:
    return [float(x) for x in re.findall(_NUMBER, s or "")]


def parse_transform(t: str):
    """translate / scale / rotate / skewX / skewY / matrix, in order, as one affine matrix."""
    m = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    for name, args in re.findall(r"(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^)]*)\)", t or ""):
        v = _nums(args)
        if name == "matrix" and len(v) == 6:
            n = tuple(v)
        elif name == "translate" and v:
            n = (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0)
        elif name == "scale" and v:
            n = (v[0], 0, 0, v[1] if len(v) > 1 else v[0], 0, 0)
        elif name == "rotate" and v:
            r = math.radians(v[0])
            n = (math.cos(r), math.sin(r), -math.sin(r), math.cos(r), 0, 0)
            if len(v) == 3:
                n = _matrix_mul(_matrix_mul((1, 0, 0, 1, v[1], v[2]), n), (1, 0, 0, 1, -v[1], -v[2]))
        elif name == "skewX" and v:
            n = (1, 0, math.tan(math.radians(v[0])), 1, 0, 0)
        elif name == "skewY" and v:
            n = (1, math.tan(math.radians(v[0])), 0, 1, 0, 0)
        else:
            continue
        m = _matrix_mul(m, n)
    return m


def _arc(x1, y1, rx, ry, phi, large, sweep, x2, y2, steps=12):
    """Points along an SVG elliptical arc (endpoint form), not including the start point."""
    if rx == 0 or ry == 0 or (x1 == x2 and y1 == y2):
        return [(x2, y2)]
    rx, ry = abs(rx), abs(ry)
    p = math.radians(phi)
    cp, sp = math.cos(p), math.sin(p)
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    x1p, y1p = cp * dx + sp * dy, -sp * dx + cp * dy
    lam = (x1p / rx) ** 2 + (y1p / ry) ** 2
    if lam > 1:
        rx, ry = rx * math.sqrt(lam), ry * math.sqrt(lam)
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    co = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        co = -co
    cxp, cyp = co * rx * y1p / ry, -co * ry * x1p / rx
    cx, cy = cp * cxp - sp * cyp + (x1 + x2) / 2, sp * cxp + cp * cyp + (y1 + y2) / 2

    def ang(ux, uy, vx, vy):
        a = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
        return a
    t1 = ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dt = ang((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dt > 0:
        dt -= 2 * math.pi
    elif sweep and dt < 0:
        dt += 2 * math.pi
    n = max(4, min(32, int(abs(dt) / (math.pi / 2) * steps / 2) + 4))
    out = []
    for i in range(1, n + 1):
        t = t1 + dt * i / n
        out.append((cx + rx * math.cos(t) * cp - ry * math.sin(t) * sp, cy + rx * math.cos(t) * sp + ry * math.sin(t) * cp))
    return out


def parse_path(d: str) -> list[tuple[list[tuple[float, float]], bool]]:
    """An SVG path's subpaths as ([points], closed). Curves and arcs become short straight segments."""
    tokens = re.findall(rf"[MmLlHhVvCcSsQqTtAaZz]|{_NUMBER}", d or "")
    subs: list[tuple[list, bool]] = []
    pts: list[tuple[float, float]] = []
    x = y = sx = sy = 0.0
    last_c = last_q = None
    cmd = None
    i = 0

    def take(n):
        nonlocal i
        if i + n > len(tokens) or any(re.fullmatch(r"[A-Za-z]", t) for t in tokens[i:i + n]):
            raise ValueError
        v = [float(t) for t in tokens[i:i + n]]
        i += n
        return v

    def flush(closed=False):
        nonlocal pts
        if len(pts) >= 2:
            subs.append((pts, closed))
        pts = []
    try:
        while i < len(tokens):
            if re.fullmatch(r"[A-Za-z]", tokens[i]):
                cmd = tokens[i]
                i += 1
                if cmd in "Zz":
                    flush(True)
                    x, y = sx, sy
                    pts = []
                    last_c = last_q = None
                    continue
            elif cmd is None:
                break
            rel = cmd.islower()
            c = cmd.upper()
            ox, oy = (x, y) if rel else (0.0, 0.0)
            if c == "M":
                px, py = take(2)
                flush()
                x, y = ox + px, oy + py
                sx, sy = x, y
                pts = [(x, y)]
                cmd = "l" if rel else "L"          # more pairs after a move are lines
                last_c = last_q = None
                continue
            if not pts:
                pts = [(x, y)]
            if c == "L":
                px, py = take(2)
                x, y = ox + px, oy + py
                pts.append((x, y))
                last_c = last_q = None
            elif c == "H":
                (px,) = take(1)
                x = ox + px
                pts.append((x, y))
                last_c = last_q = None
            elif c == "V":
                (py,) = take(1)
                y = oy + py
                pts.append((x, y))
                last_c = last_q = None
            elif c in "CS":
                if c == "C":
                    x1, y1, x2, y2, ex, ey = take(6)
                    x1, y1 = ox + x1, oy + y1
                else:
                    x2, y2, ex, ey = take(4)
                    x1, y1 = (2 * x - last_c[0], 2 * y - last_c[1]) if last_c else (x, y)
                x2, y2, ex, ey = ox + x2, oy + y2, ox + ex, oy + ey
                for k in range(1, 11):
                    t = k / 10
                    u = 1 - t
                    pts.append((u ** 3 * x + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t ** 3 * ex,
                                u ** 3 * y + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t ** 3 * ey))
                last_c, last_q = (x2, y2), None
                x, y = ex, ey
            elif c in "QT":
                if c == "Q":
                    x1, y1, ex, ey = take(4)
                    x1, y1 = ox + x1, oy + y1
                else:
                    ex, ey = take(2)
                    x1, y1 = (2 * x - last_q[0], 2 * y - last_q[1]) if last_q else (x, y)
                ex, ey = ox + ex, oy + ey
                for k in range(1, 9):
                    t = k / 8
                    u = 1 - t
                    pts.append((u * u * x + 2 * u * t * x1 + t * t * ex, u * u * y + 2 * u * t * y1 + t * t * ey))
                last_q, last_c = (x1, y1), None
                x, y = ex, ey
            elif c == "A":
                rx, ry, phi, large, sweep, ex, ey = take(7)
                ex, ey = ox + ex, oy + ey
                pts.extend(_arc(x, y, rx, ry, phi, int(large) != 0, int(sweep) != 0, ex, ey))
                x, y = ex, ey
                last_c = last_q = None
            else:
                break
            if len(pts) > MAX_NUMS // 2:
                break
    except (ValueError, TypeError, IndexError, ZeroDivisionError):
        pass                                     # a broken path: keep what was drawn up to there
    flush()
    return subs


def _style(el, inherited: dict) -> dict:
    st = dict(inherited)
    for key in ("fill", "stroke", "stroke-width", "font-size", "text-anchor", "opacity", "fill-opacity", "display",
                "visibility"):
        if el.get(key) is not None:
            st[key] = el.get(key)
    for part in (el.get("style") or "").split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            k = k.strip().lower()
            if k in ("fill", "stroke", "stroke-width", "font-size", "text-anchor", "opacity", "fill-opacity", "display",
                     "visibility"):
                st[k] = v.strip()
    return st


def _paint(v, gradients: dict) -> str | None:
    s = str(v or "").strip()
    m = re.match(r"url\(\s*#([^)\s]+)\s*\)", s)
    if m:
        return gradients.get(m.group(1), "#888888")
    return colour(s)


def _length(v, default: float = 0.0) -> float:
    n = _nums(str(v or ""))
    return n[0] if n else default


def svg_to_drawing(svg: str, title: str = "") -> dict | None:
    """The model's SVG as a checked drawing (see validate_drawing), fitted into W x H. None if there's nothing usable."""
    m = re.search(r"<svg\b.*?</svg\s*>", svg or "", re.S | re.I)
    if m:
        src = m.group(0)
    else:                                         # cut off (the model ran out of words): keep the finished part
        start = (svg or "").find("<svg")
        end = (svg or "").rfind(">")
        if start < 0 or end <= start:
            return None
        src = svg[start:end + 1]
        opened = len(re.findall(r"<g\b[^>]*(?<!/)>", src)) - len(re.findall(r"</g\s*>", src))
        src += "</g>" * max(0, opened) + "</svg>"
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", src, re.I) or len(src) > 200_000:
        return None                               # no entity tricks; nothing a model should write is this big
    src = re.sub(r"<\?.*?\?>|<!--.*?-->", "", src, flags=re.S)
    src = re.sub(r"\s(?:xmlns(?::[\w-]+)?|[\w-]+:[\w-]+)\s*=\s*(\"[^\"]*\"|'[^']*')", "", src)   # namespaces, xlink:…
    src = re.sub(r"<(/?)[\w-]+:([\w-]+)", r"<\1\2", src)
    src = re.sub(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", src)          # a bare & in a label
    try:
        root = ET.fromstring(src)
    except ET.ParseError:
        return None

    vb = _nums(root.get("viewBox") or "")
    if len(vb) == 4 and vb[2] > 0 and vb[3] > 0:
        vx, vy, vw, vh = vb
    else:
        vx, vy = 0.0, 0.0
        vw, vh = _length(root.get("width"), W) or W, _length(root.get("height"), H) or H
    k = min(W / vw, H / vh)
    base = (k, 0.0, 0.0, k, (W - vw * k) / 2 - vx * k, (H - vh * k) / 2 - vy * k)

    gradients: dict[str, str] = {}
    for g in root.iter():
        if g.tag in ("linearGradient", "radialGradient") and g.get("id"):
            stops = [s for s in g if s.tag == "stop"]
            if stops:
                pick = stops[len(stops) // 2]           # the middle stop stands in for the whole gradient
                styled = re.search(r"stop-color\s*:\s*([^;]+)", pick.get("style") or "")
                col = colour(pick.get("stop-color") or (styled.group(1) if styled else ""))
                if col:
                    gradients[g.get("id")] = col

    shapes: list[dict] = []
    bg = "#ffffff"

    def scale_of(mat) -> float:
        return math.sqrt(abs(mat[0] * mat[3] - mat[1] * mat[2])) or 1.0

    def add(kind: str, pts_or_box, st: dict, mat, closed=True, text=None):
        if st.get("display") == "none" or st.get("visibility") == "hidden":
            return
        try:
            if float(_length(st.get("opacity"), 1.0)) <= 0.05 or float(_length(st.get("fill-opacity"), 1.0)) <= 0.05 \
                    and str(st.get("stroke") or "none") == "none":
                return
        except ValueError:
            pass
        fill = _paint(st.get("fill", "black"), gradients)
        fill = "#000000" if fill is None else fill
        line = _paint(st.get("stroke", "none"), gradients) or ""
        w = _length(st.get("stroke-width"), 1.0) * scale_of(mat)
        if kind == "text":
            x, y = _apply(mat, *pts_or_box)
            size = _length(st.get("font-size"), 16.0) * scale_of(mat)
            shapes.append({"k": "text", "p": [x, y], "text": text, "size": size, "fill": fill or line or "#000000",
                           "a": st.get("text-anchor") if st.get("text-anchor") in ("start", "middle", "end") else "start"})
            return
        if kind in ("oval", "rect") and abs(mat[1]) < 1e-9 and abs(mat[2]) < 1e-9:
            x0, y0, x1, y1 = pts_or_box
            ax, ay = _apply(mat, x0, y0)
            bx, by = _apply(mat, x1, y1)
            shapes.append({"k": kind, "p": [min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)], "fill": fill,
                           "line": line, "w": w})
            return
        if kind == "oval":                        # rotated or skewed: as a polygon
            x0, y0, x1, y1 = pts_or_box
            cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2
            pts = [(cx + rx * math.cos(2 * math.pi * i / 28), cy + ry * math.sin(2 * math.pi * i / 28)) for i in range(28)]
        elif kind == "rect":
            x0, y0, x1, y1 = pts_or_box
            pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        else:
            pts = pts_or_box
        flat = []
        for px, py in pts[:MAX_NUMS // 2]:
            tx, ty = _apply(mat, px, py)
            flat += [tx, ty]
        if len(flat) >= 4:
            shapes.append({"k": "poly", "p": flat, "fill": fill, "line": line, "w": w, "closed": closed})

    def walk(el, st: dict, mat, depth: int = 0):
        nonlocal bg
        if depth > 12 or len(shapes) >= MAX_SHAPES:
            return
        tag = el.tag
        if tag in ("defs", "style", "script", "title", "desc", "metadata", "clipPath", "mask", "symbol", "pattern",
                   "linearGradient", "radialGradient", "filter", "marker", "image", "use", "foreignObject"):
            return
        st = _style(el, st)
        if el.get("transform"):
            mat = _matrix_mul(mat, parse_transform(el.get("transform")))
        g = lambda name, d=0.0: _length(el.get(name), d)  # noqa: E731
        try:
            if tag == "rect":
                x, y, w, h = g("x"), g("y"), g("width"), g("height")
                if w > 0 and h > 0:
                    if not shapes and w >= vw * 0.95 and h >= vh * 0.95 and (fill := _paint(st.get("fill", "black"),
                                                                                             gradients)):
                        bg = fill                 # a full-size first rectangle is the background
                    add("rect", (x, y, x + w, y + h), st, mat)
            elif tag == "circle":
                cx, cy, r = g("cx"), g("cy"), g("r")
                if r > 0:
                    add("oval", (cx - r, cy - r, cx + r, cy + r), st, mat)
            elif tag == "ellipse":
                cx, cy, rx, ry = g("cx"), g("cy"), g("rx"), g("ry")
                if rx > 0 and ry > 0:
                    add("oval", (cx - rx, cy - ry, cx + rx, cy + ry), st, mat)
            elif tag == "line":
                add("poly", [(g("x1"), g("y1")), (g("x2"), g("y2"))], {**st, "fill": "none"}, mat, closed=False)
            elif tag in ("polyline", "polygon"):
                v = _nums(el.get("points") or "")
                pts = list(zip(v[0::2], v[1::2]))
                if len(pts) >= 2:
                    add("poly", pts, st, mat, closed=(tag == "polygon"))
            elif tag == "path":
                for pts, closed in parse_path(el.get("d") or ""):
                    add("poly", pts, st, mat, closed=closed)
            elif tag == "text":
                words = clean_text("".join(el.itertext()))
                if words:
                    add("text", (g("x"), g("y")), st, mat, text=words)
                return                            # tspans are part of the text
        except (ValueError, TypeError, OverflowError):
            pass
        for child in el:
            walk(child, st, mat, depth + 1)

    walk(root, {}, base)
    return validate_drawing({"title": title, "bg": bg, "shapes": shapes})


DRAW_PROMPT = """You draw simple, clear pictures as SVG for a small picture board, like a friendly children's-book illustration or a neat whiteboard sketch.
Reply with ONLY one <svg> element, viewBox="0 0 400 300", nothing before or after it, no markdown, no comments.
Use only rect, circle, ellipse, line, polyline, polygon, path, text and g (transform translate/scale/rotate is fine). Gradients only inside defs.
Start with a background (a full-size rect: sky, a floor, or a plain soft colour). Flat friendly colours, dark outlines (stroke-width 2 or 3).
Keep the subject big and centred, recognisable at a glance, with the details that make it what it is. At most 70 elements.
For a diagram, chart or process: labelled boxes and arrows, short labels (one to three words), font-size 13 to 18.
No <image>, <style>, <script>, filters, masks, clipPath or links."""


# ---------------- pictures ----------------

def allowed_image_url(url: str) -> bool:
    try:
        u = urlparse(url)
    except ValueError:
        return False
    return u.scheme == "https" and (u.hostname or "") in IMAGE_HOSTS and not u.username and u.port in (None, 443)


def to_png(data: bytes, max_w: int = OUT_W, max_h: int = OUT_H) -> bytes:
    """Any picture → a plain PNG no bigger than max_w x max_h (first frame of an animation, turned upright)."""
    from PIL import Image, ImageOps
    try:
        im = Image.open(io.BytesIO(data))
        if im.width * im.height > MAX_PIXELS:
            raise PictureError("too big")
        im.seek(0)
        im = ImageOps.exif_transpose(im)
        im.thumbnail((max_w, max_h))
        im = im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB")
        out = io.BytesIO()
        im.save(out, "PNG", optimize=False)
        return out.getvalue()
    except PictureError:
        raise
    except Exception as exc:
        raise PictureError("not a picture") from exc


def local_png(path) -> bytes:
    """One of your own picture files as a PNG for the board (never changes the file)."""
    p = Path(path)
    if p.suffix.lower() not in IMAGE_EXT or not p.is_file():
        raise PictureError("not a picture file")
    if p.stat().st_size > MAX_FILE:
        raise PictureError("too big")
    return to_png(p.read_bytes())


class Pictures:
    """The lead picture of the best-matching Wikipedia article. Only the search words are sent."""

    def __init__(self, client=None):
        import httpx
        self.client = client or httpx.Client(timeout=httpx.Timeout(8.0, connect=4.0),
                                             headers={"User-Agent": USER_AGENT}, follow_redirects=False)

    def find(self, query: str) -> dict | None:
        """{'title', 'img', 'page', 'desc'} or None when nothing with a picture matches."""
        q = re.sub(r"\s+", " ", str(query or "")).strip()[:200]
        if not q:
            return None
        params = {"action": "query", "format": "json", "formatversion": "2", "generator": "search", "gsrsearch": q,
                  "gsrlimit": "6", "gsrnamespace": "0", "prop": "pageimages|description|pageprops",
                  "piprop": "thumbnail", "pithumbsize": "800", "ppprop": "disambiguation", "redirects": "1"}
        r = self.client.get(WIKI_API, params=params)
        r.raise_for_status()
        pages = sorted((r.json().get("query") or {}).get("pages") or [], key=lambda p: p.get("index", 99))
        # an article named exactly what was asked for beats a better-ranked one ("red panda" → Red panda, not a zoo star)
        pages.sort(key=lambda p: str(p.get("title", "")).lower() != q.lower())
        for p in pages:
            if "disambiguation" in (p.get("pageprops") or {}):
                continue
            img = (p.get("thumbnail") or {}).get("source") or ""
            if img and allowed_image_url(img):
                title = clean_text(p.get("title"), MAX_TITLE)
                return {"title": title, "img": img, "desc": clean_text(p.get("description"), 80),
                        "page": "https://en.wikipedia.org/wiki/" + quote(title.replace(" ", "_"))}
        return None

    def fetch(self, url: str) -> bytes:
        """The picture at url (Wikimedia's picture servers only, redirects included) as a checked, shrunk PNG."""
        for _ in range(4):
            if not allowed_image_url(url):
                raise PictureError("not an allowed picture address")
            buf = bytearray()
            with self.client.stream("GET", url) as r:
                if r.is_redirect and r.headers.get("location"):
                    url = str(r.url.join(r.headers["location"]))
                    continue
                r.raise_for_status()
                kind = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
                if not kind.startswith("image/") or "svg" in kind:
                    raise PictureError("not a picture")
                for chunk in r.iter_bytes():
                    buf += chunk
                    if len(buf) > MAX_BYTES:
                        raise PictureError("too big")
            return to_png(bytes(buf))
        raise PictureError("too many redirects")


# ---------------- what's on the board (memory only) ----------------

class BoardStore:
    """The last few pictures, by a random id, for spidey to fetch (GET /api/board/<id>). Never written to disk."""

    def __init__(self, keep: int = 8):
        self.keep = keep
        self._items: OrderedDict[str, bytes] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, png: bytes) -> str:
        bid = uuid.uuid4().hex[:16]
        with self._lock:
            self._items[bid] = png
            while len(self._items) > self.keep:
                self._items.popitem(last=False)
        return bid

    def get(self, bid: str) -> bytes | None:
        if not re.fullmatch(r"[0-9a-f]{16}", str(bid or "")):
            return None
        with self._lock:
            return self._items.get(bid)


STORE = BoardStore(keep=24)       # board pictures and spidey's memes (a meme follows every reply)


def render_png(drawing: dict, scale: float = 2.0) -> bytes:
    """A checked drawing as a PNG (spidey's Save button; also handy for looking at a drawing in tests)."""
    from PIL import Image, ImageDraw, ImageFont
    d = validate_drawing(drawing)
    if not d:
        raise PictureError("nothing to draw")
    s = scale
    im = Image.new("RGB", (int(W * s), int(H * s)), d["bg"])
    dr = ImageDraw.Draw(im)
    for sh in d["shapes"]:
        p = [v * s for v in sh["p"]]
        if sh["k"] == "text":
            try:
                font = ImageFont.truetype("segoeui.ttf", max(6, int(sh["size"] * s)))
            except OSError:
                font = ImageFont.load_default()
            anchor = {"start": "ls", "middle": "ms", "end": "rs"}[sh["a"]]
            dr.text((p[0], p[1]), sh["text"], fill=sh["fill"], font=font, anchor=anchor)
            continue
        w = max(1, round(sh["w"] * s)) if sh["line"] else 0
        if sh["k"] in ("oval", "rect"):
            box = [p[0], p[1], max(p[0], p[2]), max(p[1], p[3])]
            (dr.ellipse if sh["k"] == "oval" else dr.rectangle)(box, fill=sh["fill"] or None, outline=sh["line"] or None,
                                                                width=w)
            continue
        pts = list(zip(p[0::2], p[1::2]))
        if sh["fill"] and len(pts) >= 3:
            dr.polygon(pts, fill=sh["fill"])
        if sh["line"] and w:
            dr.line(pts + ([pts[0]] if sh.get("closed") else []), fill=sh["line"], width=w, joint="curve")
    out = io.BytesIO()
    im.save(out, "PNG")
    return out.getvalue()
