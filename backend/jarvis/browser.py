"""JARVIS's own Microsoft Edge window, controlled through the Chrome DevTools Protocol (local only).

Edge runs with its own clean profile in data/edge-agent (no Microsoft account: sign-in is blocked) and a debugging
port bound to 127.0.0.1. JARVIS can open pages, type, press keys, click visible things, scroll, control videos
and read the page. It never reads your normal Edge/Chrome profile.
"""
from __future__ import annotations

import itertools
import json
import os
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlparse

from websockets.sync.client import connect

from .gate import ABORT, Action, Gate, text_class
from .safety import sensitive_field
from .untrusted import clean_label as _plain      # tab titles and page text are data: plain, short, no control characters

BLOCKED_PAGE = """<!doctype html><meta name=viewport content="width=device-width"><title>Blocked by JARVIS</title>
<body style="font:16px system-ui;background:#111;color:#eee;display:grid;place-items:center;height:90vh;margin:0">
<div style="max-width:520px;text-align:center"><div style="font-size:54px">&#128737;</div>
<h2 style="color:#ff5c5c">JARVIS blocked this page</h2><p><b>{host}</b></p><p>Reason: {reason}.</p>
<p style="color:#999">JARVIS stopped it within a second of it opening, and nothing was typed or sent to it.
If you're sure it's safe, add it to <code>site_allowlist</code> in Spidey's settings.</p></div>"""

EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

# Pages where pressing Enter or clicking Send/Post publishes something to other people.
MESSAGING_HOSTS = ("web.whatsapp.com", "mail.google.com", "x.com", "twitter.com", "instagram.com", "facebook.com",
                   "linkedin.com", "web.telegram.org", "discord.com", "outlook.live.com", "outlook.office.com")
PUBLISH_WORDS = ("send", "post", "comment", "reply", "submit", "publish", "tweet", "share", "upload")

# --- page-side helpers (run inside the page) ---
JS_FIND_INPUT = r"""
(() => {
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 4 && r.height > 4 && getComputedStyle(e).visibility !== 'hidden'; };
  const a = document.activeElement;
  const editable = e => e && (e.isContentEditable || /^(INPUT|TEXTAREA)$/.test(e.tagName));
  // Search-style inputs get their old text selected so typing replaces it; chat boxes keep what's there.
  const prep = e => {
    const info = {how: 'field', kind: e.type || e.tagName.toLowerCase(), name: e.name || e.id || '',
                  auto: e.getAttribute('autocomplete') || '',
                  label: ((e.labels && e.labels[0] && e.labels[0].innerText) || e.getAttribute('aria-label') || e.placeholder || '').slice(0, 80)};
    if (/^(INPUT|TEXTAREA)$/.test(e.tagName) && e.select && e.type !== 'password') e.select();
    return info; };
  if (editable(a) && vis(a)) return prep(a);
  const sels = ['input[name="search_query"]', 'input[type="search"]', 'textarea[name="q"]', 'input[name="q"]',
                'div[contenteditable="true"][role="textbox"]', '[contenteditable="true"]', 'input[type="text"]', 'textarea'];
  for (const s of sels) {
    const e = [...document.querySelectorAll(s)].find(vis);
    if (e) { e.scrollIntoView({block: 'center'}); e.focus(); if (e.click) e.click(); return prep(e); }
  }
  return null;
})()
"""

# "type X in the search bar": the page's search box even when another box has the focus
JS_FIND_SEARCH = r"""
(() => {
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 4 && r.height > 4 && getComputedStyle(e).visibility !== 'hidden'; };
  const sels = ['input[name="search_query"]', 'input[type="search"]', 'textarea[name="q"]', 'input[name="q"]',
                'input[role="searchbox"]', '[role="searchbox"]', 'input[role="combobox"]', '[role="combobox"] input',
                'input[aria-label*="search" i]', 'input[placeholder*="search" i]', 'input[name*="search" i]'];
  for (const s of sels) {
    const e = [...document.querySelectorAll(s)].find(vis);
    if (!e) continue;
    e.scrollIntoView({block: 'center'}); e.focus(); if (e.click) e.click();
    if (e.select) e.select();
    return {how: 'field', kind: e.type || 'search', name: e.name || e.id || '', auto: e.getAttribute('autocomplete') || '',
            label: (e.getAttribute('aria-label') || e.placeholder || '').slice(0, 80)};
  }
  return null;
})()
"""

JS_IS_SEARCH_FIELD = r"""
(() => { const a = document.activeElement; if (!a) return false;
  const n = (a.name || '') + ' ' + (a.type || '') + ' ' + (a.getAttribute('aria-label') || '') + ' ' + (a.placeholder || '');
  return /search|query|\bq\b/i.test(n) || a.name === 'q'; })()
"""

JS_FIND_CLICK = r"""
((want, nth) => {
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 2 && r.height > 2 && r.bottom > 0 && r.top < innerHeight * 3; };
  want = want.toLowerCase().trim();
  const special = {
    'first video': 'ytd-video-renderer a#video-title, ytd-rich-item-renderer a#video-title-link, a#video-title',
    'video': 'ytd-video-renderer a#video-title, ytd-rich-item-renderer a#video-title-link, a#video-title',
    'first result': '#search a h3, a h3',
    'result': '#search a h3, a h3',
    'skip ad': '.ytp-skip-ad-button, .ytp-ad-skip-button, .ytp-ad-skip-button-modern',
    'search box': 'input[name="search_query"], input[type="search"], textarea[name="q"], input[name="q"], [role="searchbox"], [role="combobox"] input',
    'search bar': 'input[name="search_query"], input[type="search"], textarea[name="q"], input[name="q"], [role="searchbox"]',
    'search': 'input[name="search_query"], input[type="search"], textarea[name="q"], input[name="q"], [role="searchbox"]',
    'message box': 'footer [contenteditable="true"], div[contenteditable="true"][role="textbox"], textarea',
    'comment box': '#simplebox-placeholder, #contenteditable-root, [contenteditable="true"]',
  };
  let list = [];
  if (want === 'most viewed video') {
    // YouTube results show views, not likes: "3.1B" (newer layout, no word), "1.2M views", "12 lakh views", "870K views"
    const unit = {k: 1e3, m: 1e6, b: 1e9, l: 1e5, lakh: 1e5, cr: 1e7, crore: 1e7};
    const num = (n, u) => parseFloat(n.replace(/,/g, '')) * (unit[(u || '').toLowerCase()] || 1);
    const views = r => {
      for (const s of r.querySelectorAll('#metadata-line span, .inline-metadata-item')) {
        const m = /^\s*([\d.,]+)\s*(k|m|b|lakh|l|crore|cr)?\s*(?:views?)?\s*$/i.exec(s.textContent || '');
        if (m) return num(m[1], m[2]);
      }
      const m = /([\d.,]+)\s*(k|m|b|lakh|l|crore|cr)?\s+views/i.exec(r.innerText || '');
      return m ? num(m[1], m[2]) : -1;
    };
    list = [...document.querySelectorAll('ytd-video-renderer, ytd-rich-item-renderer')]
      .map(r => [views(r), r.querySelector('a#video-title, a#video-title-link')])
      .filter(x => x[1] && vis(x[1]) && x[0] >= 0).sort((a, b) => b[0] - a[0]).map(x => x[1]);
    nth = 0;
  }
  if (!list.length && special[want]) list = [...document.querySelectorAll(special[want])].filter(vis);
  if (!list.length && want === 'most viewed video') return null;
  if (!list.length) {
    const cands = [...document.querySelectorAll('a, button, [role="button"], [role="link"], [role="tab"], [role="menuitem"], input[type="submit"], [aria-label], [title], h3, span')].filter(vis);
    const label = e => ((e.innerText || '') + ' ' + (e.getAttribute('aria-label') || '') + ' ' + (e.title || '') + ' ' +
                        (e.value || '') + ' ' + (e.placeholder || '')).toLowerCase().replace(/\s+/g, ' ').trim();
    const scored = [];
    for (const e of cands) {
      const t = label(e);
      if (!t || t.length > 300) continue;
      let s = t === want ? 0 : t.startsWith(want) ? 1 : t.includes(want) ? 2 : -1;
      if (s < 0) continue;
      if (!/^(A|BUTTON)$/.test(e.tagName) && !e.getAttribute('role')) s += 1;
      scored.push([s, t.length, e]);
    }
    scored.sort((x, y) => x[0] - y[0] || x[1] - y[1]);
    list = scored.map(x => x[2]);
  }
  const e = list[Math.min(nth, list.length - 1)];
  if (!e) return null;
  e.scrollIntoView({block: 'center'});
  const r = e.getBoundingClientRect();
  const text = ((e.innerText || e.getAttribute('aria-label') || e.title || '').trim()).slice(0, 80);
  // a real button (Send, Post…) rather than a link to a video or page whose title just happens to contain "post"
  const button = /^(BUTTON|INPUT)$/.test(e.tagName) || /^(button|menuitem)$/.test(e.getAttribute('role') || '');
  return {x: r.left + r.width / 2, y: r.top + r.height / 2, text, button};
})
"""

# Spotify's web player has no plain <video>/<audio> to drive, so its own buttons are pressed instead.
JS_SPOTIFY = r"""
((action) => {
  const q = s => document.querySelector(s);
  const pp = q('[data-testid="control-button-playpause"]');
  // logged out, Spotify shows a disabled player button that still says "Pause": that is not playing
  const locked = !!pp && (pp.disabled || pp.getAttribute('aria-disabled') === 'true');
  const playing = !!pp && !locked && /pause/i.test(pp.getAttribute('aria-label') || '');
  const press = e => { if (!e) return false; e.click(); return true; };
  if (action === 'is_playing') return playing ? 'playing' : 'paused';
  if (locked || (!pp && q('[data-testid="login-button"]'))) return 'login';   // pressing would only pop up "Log in"
  if (action === 'play_result')     // top result on a search page, else the page's big green play button
    return press(q('main [data-testid="top-result-card"] [data-testid="play-button"]') ||
                 q('main [data-testid="play-button"]')) ? 'pressed' : 'none';
  if (action === 'next') return press(q('[data-testid="control-button-skip-forward"]')) ? 'next' : 'none';
  if (action === 'mute' || action === 'unmute') return press(q('[data-testid="volume-bar-toggle-mute-button"]')) ? action + 'd' : 'none';
  if (!pp) return 'none';
  if (action === 'pause') { if (playing) pp.click(); return 'paused'; }
  if (action === 'play') { if (!playing) pp.click(); return 'pressed'; }
  if (action === 'toggle') { pp.click(); return playing ? 'paused' : 'pressed'; }
  return 'unsupported';
})
"""

JS_MEDIA = r"""
((action) => {
  const v = [...document.querySelectorAll('video, audio')].sort((a, b) => (b.clientWidth * b.clientHeight) - (a.clientWidth * a.clientHeight))[0];
  if (!v) return 'none';
  if (action === 'pause') { v.pause(); return 'paused'; }
  if (action === 'play') { v.play(); return 'playing'; }
  if (action === 'toggle') { if (v.paused) { v.play(); return 'playing'; } v.pause(); return 'paused'; }
  if (action === 'mute') { v.muted = true; return 'muted'; }
  if (action === 'unmute') { v.muted = false; return 'unmuted'; }
  if (action === 'forward') { v.currentTime += 10; return 'forward'; }
  if (action === 'back') { v.currentTime -= 10; return 'back'; }
  return 'unknown';
})
"""

JS_READ = r"""
((offset) => {
  const main = document.querySelector('main, article, #content, [role="main"]') || document.body;
  const text = (main.innerText || '').replace(/\n{3,}/g, '\n\n').trim();
  return {title: document.title, url: location.href, text: text.slice(offset, offset + 6000), total: text.length, offset: offset};
})
"""

# Find text on the page. Walks the page's own text nodes only: never what is typed into a box (input values, textareas,
# editable areas, password fields are skipped), never scripts, styles or anything hidden. At most 5000 nodes and 150 ms.
# The word arrives through json.dumps, so quotes, backticks and "</script>" are just characters. Matches are shown with the
# browser's own highlight (or an outline JARVIS adds and removes itself), then cleared after 20 seconds.
JS_FIND = r"""
((term, n) => {
  const t0 = performance.now(), want = String(term).toLowerCase().slice(0, 80);
  if (!want) return {count: 0, index: 0, snippet: ''};
  const skip = 'script,style,noscript,template,textarea,input,select,option,[contenteditable],[hidden],[aria-hidden="true"]';
  const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
  const found = [];
  let nodes = 0, node;
  while ((node = walker.nextNode())) {
    if (++nodes > 5000 || performance.now() - t0 > 150 || found.length >= 500) break;
    const el = node.parentElement;
    if (!el || el.closest(skip)) continue;
    const text = node.nodeValue || '';
    const low = text.toLowerCase();
    let at = low.indexOf(want);
    if (at < 0) continue;
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    if ((r.width < 1 && r.height < 1) || s.visibility === 'hidden' || s.display === 'none') continue;
    while (at >= 0 && found.length < 500) { found.push([node, at]); at = low.indexOf(want, at + want.length); }
  }
  const count = found.length;
  if (!count) return {count: 0, index: 0, snippet: ''};
  const index = ((n % count) + count) % count;
  const range = (m) => { const r = document.createRange(); r.setStart(m[0], m[1]); r.setEnd(m[0], m[1] + want.length); return r; };
  const [cn, at] = found[index];
  const el = cn.parentElement;
  el.scrollIntoView({block: 'center', inline: 'nearest'});
  try {
    if (window.__jarvisFindTimer) { clearTimeout(window.__jarvisFindTimer); window.__jarvisFindClear && window.__jarvisFindClear(); }
    if (window.CSS && CSS.highlights && window.Highlight) {
      let st = document.getElementById('__jarvis_find_style');
      if (!st) { st = document.createElement('style'); st.id = '__jarvis_find_style';
        st.textContent = '::highlight(jarvis-find){background:#ffe14d;color:#000}::highlight(jarvis-find-now){background:#ff9f1a;color:#000}';
        document.head.appendChild(st); }
      CSS.highlights.set('jarvis-find', new Highlight(...found.map(range)));
      CSS.highlights.set('jarvis-find-now', new Highlight(range(found[index])));
      window.__jarvisFindClear = () => { CSS.highlights.delete('jarvis-find'); CSS.highlights.delete('jarvis-find-now');
        const s = document.getElementById('__jarvis_find_style'); if (s) s.remove(); };
    } else {
      const old = el.style.outline;
      el.style.outline = '3px solid #ff2e42';
      window.__jarvisFindClear = () => { el.style.outline = old; };
    }
    window.__jarvisFindTimer = setTimeout(() => { window.__jarvisFindClear(); window.__jarvisFindTimer = null; }, 20000);
  } catch (e) {}
  const text = cn.nodeValue || '';
  const from = Math.max(0, at - 30);
  return {count, index, snippet: text.slice(from, at + want.length + 40).replace(/\s+/g, ' ').trim()};
})
"""

JS_ACTIVE_FIELD = r"""
(() => { const a = document.activeElement;
  if (!a || a === document.body) return null;
  return {tag: a.tagName.toLowerCase(), type: (a.type || '').toLowerCase(), name: a.name || a.id || '',
          auto: a.getAttribute('autocomplete') || '',
          label: ((a.labels && a.labels[0] && a.labels[0].innerText) || a.getAttribute('aria-label') || a.placeholder || '').slice(0, 80)}; })()
"""

# document.visibilityState / hasFocus: which tab is the one in front, asked of each tab (once a second at most)
JS_IN_FRONT = "(document.visibilityState === 'visible' && document.hasFocus())"

# Everything you could act on, numbered (data-jarvis-id), with the label a person would read: form fields first,
# then buttons, then links. Password values are never read back, only whether one is filled.
JS_SNAPSHOT = r"""
(() => {
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
                     return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const clean = t => (t || '').replace(/\s+/g, ' ').trim().slice(0, 70);
  const labelOf = e => {
    let t = '';
    if (e.id) { const l = document.querySelector(`label[for="${CSS.escape(e.id)}"]`); if (l) t = l.innerText; }
    if (!t && e.closest('label')) t = e.closest('label').innerText;
    if (!t && e.getAttribute('aria-labelledby')) {
      t = e.getAttribute('aria-labelledby').split(/\s+/).map(i => (document.getElementById(i) || {}).innerText || '').join(' '); }
    return clean(t || e.getAttribute('aria-label') || e.placeholder || e.getAttribute('title') || e.name ||
                 e.innerText || e.value || e.getAttribute('alt'));
  };
  document.querySelectorAll('[data-jarvis-id]').forEach(e => e.removeAttribute('data-jarvis-id'));
  const groups = [
    'input:not([type=hidden]), textarea, select, [contenteditable="true"], [role="textbox"], [role="combobox"], [role="checkbox"], [role="radio"], [role="switch"]',
    'button, input[type=submit], input[type=button], [role="button"], [role="tab"], [role="menuitem"]',
    'a[href]'];
  const out = [], seen = new Set(); let n = 0;
  groups.forEach((sel, gi) => {
    for (const e of document.querySelectorAll(sel)) {
      if (seen.has(e) || !vis(e) || n >= (gi === 2 ? 90 : 70)) continue;
      seen.add(e); e.setAttribute('data-jarvis-id', ++n);
      const tag = e.tagName.toLowerCase(), type = (e.getAttribute('type') || e.getAttribute('role') || '').toLowerCase();
      const it = {id: n, kind: gi === 0 ? 'field' : gi === 1 ? 'button' : 'link', tag, type, label: labelOf(e)};
      if (tag === 'select') it.options = [...e.options].slice(0, 20).map(o => clean(o.text));
      if (/^(input|textarea)$/.test(tag)) {
        if (type === 'checkbox' || type === 'radio') it.checked = e.checked;
        else it.value = type === 'password' ? (e.value ? '(filled)' : '') : clean(e.value);
      }
      if (e.form && e.form.querySelector('input[type=password]')) it.pwform = true;   // part of a sign-in / sign-up form
      if (e.getAttribute('autocomplete')) it.auto = e.getAttribute('autocomplete');
      if (e.name) it.name = e.name;
      if (e.required || e.getAttribute('aria-required') === 'true') it.required = true;
      if (e.disabled) it.disabled = true;
      out.push(it);
    }
  });
  return {title: document.title, url: location.href, elements: out,
          text: clean((document.querySelector('main, form, [role="main"]') || document.body).innerText).slice(0, 2000)};
})()
"""

JS_ACT = r"""
((id, action, value) => {
  const e = document.querySelector(`[data-jarvis-id="${id}"]`);
  if (!e) return {ok: false, why: 'that element is gone (the page changed)'};
  e.scrollIntoView({block: 'center'});
  const label = (e.getAttribute('aria-label') || e.innerText || e.value || e.placeholder || e.name || '').replace(/\s+/g, ' ').trim().slice(0, 60);
  if (action === 'select') {
    const want = String(value).toLowerCase().trim();
    if (e.tagName === 'SELECT') {
      const o = [...e.options].find(o => o.text.trim().toLowerCase() === want) || [...e.options].find(o => o.text.toLowerCase().includes(want));
      if (!o) return {ok: false, why: 'no such option'};
      e.value = o.value; e.dispatchEvent(new Event('input', {bubbles: true})); e.dispatchEvent(new Event('change', {bubbles: true}));
      return {ok: true, label: o.text.trim()};
    }
    e.click();                                   // a radio option / custom option list: choosing it is a click
    return {ok: true, label};
  }
  if (action === 'setvalue') {                   // time/date/colour/range boxes ignore typed keys: set the value
    const proto = e.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(e, String(value));
    e.dispatchEvent(new Event('input', {bubbles: true})); e.dispatchEvent(new Event('change', {bubbles: true}));
    return {ok: true, label};
  }
  if (action === 'readback') return {ok: true, value: e.isContentEditable ? e.innerText : (e.value || '')};
  if (action === 'check') {
    const on = !(String(value).toLowerCase() === 'false' || value === false || String(value) === '0');
    const now = e.checked !== undefined ? e.checked : e.getAttribute('aria-checked') === 'true';
    if (now !== on) e.click();
    return {ok: true, label};
  }
  if (action === 'focus') {
    e.focus();
    if (e.select) e.select(); else if (e.isContentEditable) { const r = document.createRange(); r.selectNodeContents(e); const s = getSelection(); s.removeAllRanges(); s.addRange(r); }
    return {ok: true, label, password: e.type === 'password', type: (e.type || '').toLowerCase(), name: e.name || e.id || '',
            auto: e.getAttribute('autocomplete') || '',
            full: ((e.labels && e.labels[0] && e.labels[0].innerText) || '') + ' ' + (e.getAttribute('aria-label') || '') + ' ' + (e.placeholder || '')};
  }
  const r = e.getBoundingClientRect();
  const button = /^(BUTTON|INPUT)$/.test(e.tagName) || /^(button|menuitem|tab)$/.test(e.getAttribute('role') || '');
  return {ok: true, x: r.left + r.width / 2, y: r.top + r.height / 2, label, button};
})
"""

# So you can see what JARVIS does: a red ripple where it clicks, a glowing outline on the box it types into.
# Drawn on top of the page with pointer-events off, so it never gets in the way of the page itself.
JS_FX = r"""
((mode, x, y, id) => {
  const layer = document.documentElement;
  if (mode === 'click') {
    const d = document.createElement('div');
    d.style.cssText = `position:fixed;left:${x - 18}px;top:${y - 18}px;width:36px;height:36px;border-radius:50%;` +
      'border:3px solid #ff2e42;background:rgba(255,46,66,.25);z-index:2147483647;pointer-events:none;' +
      'transition:transform .45s ease-out, opacity .45s ease-out;transform:scale(.4);opacity:1';
    layer.appendChild(d);
    requestAnimationFrame(() => { d.style.transform = 'scale(1.6)'; d.style.opacity = '0'; });
    setTimeout(() => d.remove(), 600);
  }
  if (mode === 'field') {
    const e = document.querySelector(`[data-jarvis-id="${id}"]`) || document.activeElement;
    if (!e || !e.style) return;
    const old = e.style.boxShadow;
    e.style.boxShadow = '0 0 0 3px #ff2e42, 0 0 14px 4px rgba(255,46,66,.55)';
    setTimeout(() => { e.style.boxShadow = old; }, 1400);
  }
})
"""

JS_BANNER = r"""
((on, text) => {
  let b = document.getElementById('__jarvis_banner');
  if (!on) { if (b) b.remove(); return; }
  if (!b) { b = document.createElement('div'); b.id = '__jarvis_banner'; document.documentElement.appendChild(b); }
  b.style.cssText = 'position:fixed;top:0;left:50%;transform:translateX(-50%);z-index:2147483647;pointer-events:none;' +
    'background:#c0141f;color:#fff;font:600 14px system-ui;padding:6px 16px;border-radius:0 0 10px 10px;' +
    'box-shadow:0 2px 10px rgba(0,0,0,.4)';
  b.textContent = '🕷 ' + text;
})
"""

# Keeps scrolling by itself until told to stop (feeds that load more as you go keep going).
JS_AUTOSCROLL = r"""
((on, step) => {
  if (window.__jarvisScroll) { clearInterval(window.__jarvisScroll); window.__jarvisScroll = null; }
  if (on) window.__jarvisScroll = setInterval(() => window.scrollBy(0, step), 30);
  return !!window.__jarvisScroll;
})
"""

KEYS = {
    "enter": ("Enter", "Enter", 13), "escape": ("Escape", "Escape", 27), "tab": ("Tab", "Tab", 9),
    "space": (" ", "Space", 32), "backspace": ("Backspace", "Backspace", 8),
    "down": ("ArrowDown", "ArrowDown", 40), "up": ("ArrowUp", "ArrowUp", 38),
    "left": ("ArrowLeft", "ArrowLeft", 37), "right": ("ArrowRight", "ArrowRight", 39),
    "page down": ("PageDown", "PageDown", 34), "page up": ("PageUp", "PageUp", 33),
}


class BrowserError(RuntimeError):
    pass


class EdgeController:
    FEATURES_OFF = "msEdgeSidebarV2,msImplicitSignin"     # Edge keeps only the last --disable-features, so one list

    def __init__(self, profile_dir: Path, port: int = 0, edge_path: str | None = None, guard=None,
                 gate: Gate | None = None):
        self.guard = guard                 # SiteGuard: bad sites are refused before they load (None = no checks)
        self.gate = gate or Gate.permissive()   # every click / key / typed character passes it (the server passes its own)
        self.blocked: list[tuple[float, str, str]] = []   # (when, url, reason) of what the guard stopped
        self._watching = False
        self.profile_dir = Path(profile_dir)
        # 0 = a new random port every time Edge starts (council decision, 22 Oct): no fixed, guessable control port.
        # Edge writes the port it picked into <profile>/DevToolsActivePort; JARVIS reads it from there.
        self.fixed_port = int(port or 0)
        self.edge_path = edge_path or next((p for p in EDGE_PATHS if os.path.exists(p)), None)
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self.account_alert = ""            # set if Edge ever signed itself in (it was closed)

    # ---------- process & targets ----------
    @property
    def port(self) -> int:
        if self.fixed_port:
            return self.fixed_port
        try:
            first = (self.profile_dir / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()[0].strip()
            return int(first) if first.isdigit() else 0
        except (OSError, IndexError, ValueError):
            return 0

    def _http(self, path: str, method: str = "GET", timeout: float = 2.0):
        if not self.port:
            raise BrowserError("My Edge window isn't running.")
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
        try:
            return json.loads(body or b"null")
        except ValueError:          # /json/close and /json/activate answer in plain text ("Target is closing")
            return None

    def running(self) -> bool:
        try:
            self._http("/json/version", timeout=0.6)
            return True
        except Exception:
            return False

    # Windows Edge signs every new profile into the Windows Microsoft account by itself ("implicit sign-in") and then
    # pulls that account's saved passwords in, even with --disable-sync. Tested 22 Oct: a fresh profile was signed in
    # within 25 s; with signin.allowed = false written before the first start it stayed signed out.
    def _lock_out_accounts(self) -> None:
        prefs_path = self.profile_dir / "Default" / "Preferences"
        prefs_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            prefs = json.loads(prefs_path.read_text(encoding="utf-8")) if prefs_path.exists() else {}
        except ValueError:
            prefs = {}
        prefs.setdefault("signin", {}).update(allowed=False, allowed_on_next_startup=False)
        prefs.setdefault("sync", {})["requested"] = False
        prefs.setdefault("browser", {})["has_seen_welcome_page"] = True
        # Security lockdown: no saved passwords, no autofill, and websites get no camera, mic, location, clipboard,
        # notifications, USB/Bluetooth/serial devices, file writing, pop-ups or silent downloads.
        prefs["credentials_enable_service"] = False
        prefs["credentials_enable_autosignin"] = False
        prefs.setdefault("autofill", {}).update(enabled=False, profile_enabled=False, credit_card_enabled=False)
        prefs.setdefault("payments", {})["can_make_payment_enabled"] = False
        prefs.setdefault("download", {}).update(prompt_for_download=True)
        prefs.setdefault("smartscreen", {})["enabled"] = True       # Microsoft's own malicious-site check stays on
        blocked = {k: 2 for k in ("geolocation", "notifications", "media_stream_camera", "media_stream_mic", "clipboard",
                                  "sensors", "usb_guard", "serial_guard", "hid_guard", "bluetooth_guard",
                                  "bluetooth_scanning", "file_system_write_guard", "automatic_downloads", "popups",
                                  "payment_handler", "idle_detection", "window_placement", "local_fonts", "midi_sysex",
                                  "nfc", "vr", "ar", "protocol_handlers", "storage_access", "captured_surface_control")}
        prefs.setdefault("profile", {}).setdefault("default_content_setting_values", {}).update(blocked)
        prefs_path.write_text(json.dumps(prefs), encoding="utf-8")

    def signed_in_account(self) -> str:
        """The Microsoft/Google account this profile is signed into ('' = none, as it should be)."""
        try:
            local = json.loads((self.profile_dir / "Local State").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        info = local.get("profile", {}).get("info_cache", {})
        return next((v.get("user_name") for v in info.values() if v.get("user_name")), "")

    def _check_signed_out(self) -> None:
        account = self.signed_in_account()
        if account:
            self.close_browser()
            self.account_alert = account
            raise BrowserError(f"My Edge window signed itself into {account}, so I closed it to keep your accounts out. "
                               f"Tell me and I'll reset it.")

    def close_browser(self) -> None:
        """Close JARVIS's Edge (only the one using this profile; your own Edge is never touched)."""
        marker = str(self.profile_dir).lower()
        try:
            import psutil
            for p in psutil.process_iter(["name", "cmdline"]):
                if (p.info["name"] or "").lower() == "msedge.exe" and marker in " ".join(p.info["cmdline"] or []).lower():
                    p.kill()
        except Exception:
            pass

    def ensure_running(self) -> None:
        if self.running():
            return
        if not self.edge_path:
            raise BrowserError("Microsoft Edge isn't installed.")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._lock_out_accounts()
        try:
            (self.profile_dir / "DevToolsActivePort").unlink()      # a stale port from a crashed Edge must not be used
        except OSError:
            pass
        # Never --remote-allow-origins: without it Edge refuses control connections coming from web pages.
        subprocess.Popen([self.edge_path, f"--remote-debugging-port={self.fixed_port}", "--remote-debugging-address=127.0.0.1",
                          f"--user-data-dir={self.profile_dir}", "--no-first-run", "--no-default-browser-check",
                          "--disable-sync",  # keep this profile separate: don't pull in your Microsoft account data
                          f"--disable-features={self.FEATURES_OFF}",
                          # keep working when the window is behind others (auto-scroll, waiting pages, timers)
                          "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                          "--disable-backgrounding-occluded-windows", *self._window_args()],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            time.sleep(0.25)
            if self.running():
                self._check_signed_out()
                # and once more after Edge has settled (implicit sign-in comes a few seconds in)
                threading.Timer(30.0, self._late_check).start()
                self._start_watchdog()
                return
        raise BrowserError("Edge didn't start.")

    def _window_args(self) -> list[str]:
        """How the first window opens (the mini TV opens a small app window instead)."""
        return ["--start-maximized", "about:blank"]

    # ---------- the site guard ----------
    def check_url(self, url: str) -> None:
        """Refuse a blocked site before anything loads."""
        reason = self.guard.check(url) if self.guard else None
        if reason:
            host = urlparse(url if "://" in url else f"https://{url}").hostname or url
            self.blocked.append((time.time(), url, reason))
            raise BrowserError(f"I blocked {host}: {reason}.")

    def _start_watchdog(self) -> None:
        """Pages reached by clicking a link are checked too: within about a second a blocked page is replaced."""
        if not self.guard or self._watching:
            return
        self._watching = True

        def watch():
            while self._watching:
                time.sleep(0.8)
                try:
                    if not self.running():
                        continue
                    for t in self._http("/json/list") or []:
                        url = t.get("url", "")
                        if t.get("type") != "page" or not url.startswith("http"):
                            continue
                        reason = self.guard.check(url)
                        if reason:
                            self.blocked.append((time.time(), url, reason))
                            page = BLOCKED_PAGE.replace("{host}", (urlparse(url).hostname or "")[:80]).replace(
                                "{reason}", reason)
                            self._cdp(t, [("Page.navigate", {"url": "data:text/html;charset=utf-8," + quote(page)})])
                except Exception:
                    pass
        threading.Thread(target=watch, daemon=True, name="edge-site-guard").start()

    def _late_check(self) -> None:
        try:
            self._check_signed_out()
        except BrowserError:
            pass

    def pages(self) -> list[dict]:
        """Real web pages, most recently used first. Edge's own pop-ups (sync, welcome) are closed or skipped."""
        out = []
        for t in self._http("/json/list"):
            url = t.get("url", "")
            if t.get("type") != "page" or url.startswith("devtools://"):
                continue
            if url.startswith(("edge://sync", "edge://welcome", "edge://signin", "https://www.msn.com/")) and "newtab" not in url:
                try:
                    self._http(f"/json/close/{t['id']}")
                except Exception:
                    pass
                continue
            if url.startswith(("edge://", "chrome://")) and "newtab" not in url:
                continue
            out.append(t)
        return out

    def active_page(self) -> dict:
        """The tab JARVIS last opened, else the most recently used real page. A stray blank tab listed first must
        not win (that made "play" look at about:blank while the Spotify tab sat right next to it)."""
        self.ensure_running()
        pages = self.pages()  # most recently used first
        if not pages:
            return self._http("/json/new?about:blank", method="PUT")
        real = [p for p in pages if p.get("url", "").startswith("http")]
        front = self._tab_in_front(pages)     # the tab you can actually see beats "the one JARVIS opened last"
        mine = next((p for p in real if p.get("id") == getattr(self, "_last_opened", None)), None)
        return front or mine or (real[0] if real else pages[0])

    def _tab_in_front(self, pages: list[dict]) -> dict | None:
        """The tab that is visible and has the focus, found by asking up to the first six tabs once (cached for a second).
        With a single tab there is nothing to ask. None when no tab claims the focus (another app is in front)."""
        if len(pages) < 2:
            return None
        now = time.monotonic()
        cached = getattr(self, "_front_cache", None)
        if cached and now - cached[0] < 1.0:
            return next((p for p in pages if p.get("id") == cached[1]), None)
        found = None
        for page in pages[:6]:
            if not page.get("url", "").startswith(("http", "file:", "data:")):
                continue
            try:
                if self._eval(page, JS_IN_FRONT):
                    found = page
                    break
            except Exception:
                continue
        self._front_cache = (now, found.get("id") if found else None)
        return found

    # ---------- tabs (titles and addresses are DATA: spoken only, control characters stripped, never routed) ----------
    def tabs(self) -> list[dict]:
        """Real pages in a steady order: the order JARVIS first saw them, which is the order they were opened in."""
        pages = self.pages()
        seq = self.__dict__.setdefault("_tab_seq", {})
        for p in reversed(pages):                 # the list is most recent first, so this is oldest first
            if p.get("id") not in seq:
                seq[p.get("id")] = self._tab_n = getattr(self, "_tab_n", -1) + 1
        for gone in [i for i in seq if i not in {p.get("id") for p in pages}]:
            del seq[gone]
        return sorted(pages, key=lambda p: seq[p.get("id")])

    def tab_list(self) -> list[dict]:
        """At most 15 tabs: {n, title (60 characters), host, active}."""
        self.ensure_running()
        pages = self.tabs()
        try:
            active = self.active_page().get("id")
        except Exception:
            active = None
        return [{"n": i, "title": _plain(p.get("title") or "", 60) or "(no title)",
                 "host": _plain(urlparse(p.get("url", "")).hostname or "", 60), "active": p.get("id") == active}
                for i, p in enumerate(pages[:15], 1)]

    def _resolve_tab(self, which) -> tuple[dict, list[dict]]:
        """A number (1-based), 'last', 'next', 'previous', or words from a title/host → (that page, all pages)."""
        pages = self.tabs()
        if not pages:
            raise BrowserError("I don't have any tabs open.")
        if isinstance(which, int) or (isinstance(which, str) and which.strip().lstrip("-").isdigit()):
            n = int(which)
            if not 1 <= n <= len(pages):
                raise BrowserError(f"I only have {len(pages)} tab{'s' if len(pages) != 1 else ''} open.")
            return pages[n - 1], pages
        word = str(which or "").strip().lower()
        if word in ("last", "final"):
            return pages[-1], pages
        if word in ("next", "previous", "prev"):
            cur = next((i for i, p in enumerate(pages) if p.get("id") == self.active_page().get("id")), 0)
            return pages[(cur + (1 if word == "next" else -1)) % len(pages)], pages
        if not word:
            raise BrowserError("Which tab? Say its number or a word from its title.")
        hits = [p for p in pages if word in (p.get("title") or "").lower() or word in (urlparse(p.get("url", "")).hostname or "")]
        if len(hits) == 1:
            return hits[0], pages
        if not hits:
            raise BrowserError(f"I can't find a tab about {_plain(word, 30)}.")
        raise BrowserError(f"{len(hits)} tabs match {_plain(word, 30)}. Say the number instead: " + "; ".join(
            f"{pages.index(p) + 1}, {_plain(p.get('title') or p.get('url', ''), 30)}" for p in hits[:4]) + ".")

    def switch_tab(self, which) -> str:
        """Bring a tab to the front. Returns its title (data: the caller only speaks it)."""
        self.ensure_running()
        page, _ = self._resolve_tab(which)
        self._authorize("browser_tab", "switch")
        self._http(f"/json/activate/{page['id']}")
        self._last_opened = page["id"]
        self._front_cache = None
        return _plain(page.get("title") or page.get("url", ""), 60)

    def close_tab(self, which) -> str:
        """Close one tab. The last tab is never closed (an empty browser has nothing for me to work in)."""
        self.ensure_running()
        page, pages = self._resolve_tab(which)
        if len(pages) <= 1:
            raise BrowserError("That's my only tab, so I'll leave it open.")
        self._authorize("browser_tab", "close")
        self._http(f"/json/close/{page['id']}")
        self._front_cache = None
        return _plain(page.get("title") or page.get("url", ""), 60)

    def close_others(self) -> int:
        """Close every tab except the one in front. Returns how many were closed (the caller asked first)."""
        self.ensure_running()
        pages = self.tabs()
        if len(pages) <= 1:
            return 0
        keep = self.active_page().get("id")
        if not any(p.get("id") == keep for p in pages):
            keep = pages[0].get("id")
        self._authorize("browser_tab", "close others")
        closed = 0
        for p in pages:
            if p.get("id") != keep:
                self._http(f"/json/close/{p['id']}")
                closed += 1
        self._last_opened = keep
        self._front_cache = None
        return closed

    # ---------- find on the page ----------
    def find(self, term: str, n: int = 0) -> dict:
        """Count and show matches of a word on the current page: {count, index, snippet}. n picks which match to show
        (wraps around). Page text only: never what is typed into a box. The page must pass the same read check as a window."""
        term = str(term or "").strip()[:80]
        if not term:
            raise BrowserError("What should I look for?")
        page = self.active_page()
        why = self.gate.read_allowed(None, {"proc": "msedge.exe", "title": page.get("title") or page.get("url") or "",
                                            "address": page.get("url", ""), "own": False, "excluded": False})
        if why:
            raise BrowserError(why)
        self._authorize("browser_find", "", term)            # the kill switch counts; only the length is logged
        res = self._eval(page, f"{JS_FIND}({json.dumps(term)}, {int(n)})") or {}
        return {"count": int(res.get("count") or 0), "index": int(res.get("index") or 0),
                "snippet": _plain(res.get("snippet") or "", 80)}

    def page_info(self) -> dict:
        """Title and address of the current tab, for 'what's the page title / address'."""
        p = self.active_page()
        u = urlparse(p.get("url", ""))
        where = f"{u.hostname or ''}{u.path if len(u.path) > 1 else ''}"[:100]      # no query string: it can carry tokens
        return {"title": _plain(p.get("title") or "", 100), "address": _plain(where or p.get("url", "")[:60], 100)}

    # ---------- keyboard shortcuts (through the gate; many browser keys don't work from outside, and we say so) ----------
    _NAV_COMBOS = {"alt+left": "back", "alt+right": "forward", "f5": "reload", "ctrl+t": "new tab", "ctrl+w": "close"}
    _EDIT_COMBOS = {"ctrl+a": "selectAll", "ctrl+c": "copy", "ctrl+x": "cut", "ctrl+v": "paste", "ctrl+z": "undo",
                    "ctrl+y": "redo"}

    def press_combo(self, combo: str) -> str:
        """A shortcut from the allowed list, done in the way that works for a page controlled from outside. Raises a spoken
        BrowserError for the ones that can't be done here (the person is told, nothing is pretended)."""
        norm = combo.lower().strip()
        if norm in self._NAV_COMBOS:
            self._authorize("browser_key", key=norm)
            self.navigate(self._NAV_COMBOS[norm])
            return norm
        if norm == "ctrl+tab":
            self.switch_tab("next")
            return norm
        if norm in self._EDIT_COMBOS:
            page = self.active_page()
            field = self._eval(page, JS_ACTIVE_FIELD) or {}
            if norm == "ctrl+v" and sensitive_field(field.get("label", ""), field.get("type", ""), field.get("auto", ""),
                                                    field.get("name", "")):
                raise BrowserError("That's a password, PIN, OTP, card or ID-number box. I never paste into those.")
            self._authorize("browser_key", key=norm)
            vk = ord(norm[-1].upper())
            base = {"key": norm[-1], "code": f"Key{norm[-1].upper()}", "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk,
                    "modifiers": 2, "commands": [self._EDIT_COMBOS[norm]]}
            self._cdp(page, [("Input.dispatchKeyEvent", {**base, "type": "rawKeyDown"}),
                             ("Input.dispatchKeyEvent", {**{k: v for k, v in base.items() if k != "commands"}, "type": "keyUp"})])
            return norm
        raise BrowserError("I can't press that one inside my Edge window from the outside. I can do back, forward, refresh, "
                           "new tab, close tab, and copy, paste, cut, undo, redo or select all.")

    # ---------- CDP ----------
    def _cdp(self, page: dict, calls: list[tuple[str, dict]], timeout: float = 8.0) -> list[dict]:
        out = []
        with self._lock, connect(page["webSocketDebuggerUrl"], max_size=20_000_000, open_timeout=4) as ws:
            for method, params in calls:
                cid = next(self._ids)
                ws.send(json.dumps({"id": cid, "method": method, "params": params}))
                deadline = time.monotonic() + timeout
                while True:
                    msg = json.loads(ws.recv(timeout=max(0.1, deadline - time.monotonic())))
                    if msg.get("id") == cid:
                        if "error" in msg:
                            raise BrowserError(msg["error"].get("message", "browser error"))
                        out.append(msg.get("result", {}))
                        break
        return out

    def _eval(self, page: dict, expression: str):
        res = self._cdp(page, [("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True,
                                                    "userGesture": True})])[0]
        if res.get("exceptionDetails"):
            raise BrowserError("page script failed")
        return res.get("result", {}).get("value")

    def _authorize(self, kind: str, label: str = "", text: str = "", key: str = "") -> None:
        """The gate's yes/no for anything that sends a click or key into the page. 'Ask the person first' for page
        buttons is the caller's job (it asked before getting here); the gate's kill switch, breaker and secret-box
        rules apply here no matter who calls."""
        if ABORT.is_set():
            raise BrowserError("Stopped.")
        d = self.gate.authorize(Action(kind, None, "msedge.exe", "", label, len(text), key, "user", "", text_class(text)))
        if d.verdict == "deny":
            raise BrowserError(d.say)

    def _focus_window(self, page: dict) -> None:
        try:
            self._http(f"/json/activate/{page['id']}")
        except Exception:
            pass

    # ---------- actions ----------
    def open_url(self, url: str, new_tab: bool = False) -> str:
        self.check_url(url)
        self.ensure_running()
        pages = self.pages()
        reuse = pages[0] if pages and not new_tab and pages[0].get("url", "") in ("about:blank", "edge://newtab/", "") else None
        if reuse:
            self._cdp(reuse, [("Page.navigate", {"url": url})])
            page = reuse
        else:
            page = self._http(f"/json/new?{url}", method="PUT")
        self._last_opened = page.get("id")
        self._focus_window(page)
        return urlparse(url).hostname or url

    def current(self) -> dict:
        p = self.active_page()
        return {"title": p.get("title", ""), "url": p.get("url", ""), "host": urlparse(p.get("url", "")).hostname or ""}

    def type_text(self, text: str, search: bool = False) -> dict:
        page = self.active_page()
        self._focus_window(page)
        where = (self._eval(page, JS_FIND_SEARCH) if search else None) or self._eval(page, JS_FIND_INPUT)
        if not where:
            raise BrowserError("I can't find a text box on this page.")
        if sensitive_field(where.get("label", ""), where.get("kind", ""), where.get("auto", ""), where.get("name", "")):
            raise BrowserError("That's a password, PIN, OTP, card or ID-number box. I never type into those, "
                               "so please type it yourself.")
        self._authorize("browser_fill", where.get("label", ""), text)
        self._fx(page, "field")
        self._cdp(page, [("Input.insertText", {"text": text})])
        is_search = bool(self._eval(page, JS_IS_SEARCH_FIELD))
        return {"field": where.get("kind", "box"), "search": is_search}

    def press(self, key: str, shift: bool = False) -> None:
        page = self.active_page()
        name = key.lower()
        if name in KEYS:
            k, code, vk = KEYS[name]
        elif len(key) == 1:
            k, code, vk = key, f"Key{key.upper()}", ord(key.upper())
        else:
            raise BrowserError(f"Unknown key {key}.")
        self._authorize("browser_key", key=name)
        mods = 8 if shift else 0
        base ={"key": k, "code": code, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk, "modifiers": mods}
        down = {**base, "type": "keyDown"}
        if name == "enter":
            down["text"] = "\r"
        elif len(k) == 1:
            down["text"] = k
        self._cdp(page, [("Input.dispatchKeyEvent", down), ("Input.dispatchKeyEvent", {**base, "type": "keyUp"})])

    def find_clickable(self, target: str, nth: int = 0) -> dict | None:
        page = self.active_page()
        return self._eval(page, f"{JS_FIND_CLICK}({json.dumps(target)}, {int(nth)})")

    def click(self, target: str, nth: int = 0) -> str:
        page = self.active_page()
        self._focus_window(page)
        hit = self._eval(page, f"{JS_FIND_CLICK}({json.dumps(target)}, {int(nth)})")
        if not hit:
            raise BrowserError(f"I can't see “{target}” on this page.")
        time.sleep(0.15)  # let scrollIntoView settle
        hit = self._eval(page, f"{JS_FIND_CLICK}({json.dumps(target)}, {int(nth)})") or hit
        x, y = hit["x"], hit["y"]
        self._authorize("browser_click", hit.get("text") or target)
        self._fx(page, "click", 0, x, y)
        ev = {"x": x, "y": y, "button": "left", "clickCount": 1}
        self._cdp(page, [("Input.dispatchMouseEvent", {**ev, "type": "mouseMoved"}),
                         ("Input.dispatchMouseEvent", {**ev, "type": "mousePressed"}),
                         ("Input.dispatchMouseEvent", {**ev, "type": "mouseReleased"})])
        return hit.get("text") or target

    def scroll(self, direction: str) -> None:
        page = self.active_page()
        amount = {"down": 700, "up": -700, "top": -10**7, "bottom": 10**7}.get(direction, 700)
        self._eval(page, f"window.scrollBy({{top: {amount}, behavior: 'smooth'}})")

    def media(self, action: str) -> str:
        page = self.active_page()
        if "spotify.com" in page.get("url", ""):
            return self._eval(page, f"{JS_SPOTIFY}({json.dumps(action)})") or "none"
        if action == "play_result":
            return "none"
        if action == "next":
            if "youtube.com" in page.get("url", ""):
                self.press("n", shift=True)
                return "next"
            return "none"
        return self._eval(page, f"{JS_MEDIA}({json.dumps(action)})")

    def navigate(self, action: str) -> None:
        page = self.active_page()
        if action == "back":
            self._eval(page, "history.back()")
        elif action == "forward":
            self._eval(page, "history.forward()")
        elif action == "reload":
            self._cdp(page, [("Page.reload", {})])
        elif action == "close":
            self._http(f"/json/close/{page['id']}")
        elif action == "new tab":
            self._http("/json/new?edge://newtab/", method="PUT")

    def is_playing(self) -> bool:
        """Is any tab in JARVIS's Edge playing audible video/audio right now? (cached for 2 s)"""
        now = time.monotonic()
        if now - getattr(self, "_playing_at", 0) < 2:
            return self._playing
        playing, title = False, ""
        try:
            if self.running():
                js = "[...document.querySelectorAll('video,audio')].some(v => !v.paused && !v.muted && v.volume > 0 && v.currentTime > 0)"
                for page in self.pages()[:6]:
                    if page.get("url", "").startswith("http") and self._eval(page, js):
                        playing, title = True, page.get("title", "")
                        break
        except Exception:
            playing = False
        self._playing, self._playing_at, self.playing_title = playing, now, title
        return playing

    def read(self, offset: int = 0) -> dict:
        """Title, address and up to 6000 characters of the page's text from `offset` on ('total' = how long it is)."""
        return self._eval(self.active_page(), f"{JS_READ}({max(0, int(offset))})") or {}

    # ---------- Puppeteer-style: see the page as numbered elements, act on them ----------
    def snapshot(self) -> dict:
        return self._eval(self.active_page(), JS_SNAPSHOT) or {"elements": []}

    def act(self, el_id: int, action: str, value=None) -> dict:
        """fill (types like a person: focus, replace, insert), select (dropdown option), check (tick/untick),
        click (real mouse event at the element)."""
        if ABORT.is_set():
            raise BrowserError("Stopped.")
        page = self.active_page()
        self._focus_window(page)
        js = lambda a, v:self._eval(page, f"{JS_ACT}({int(el_id)}, {json.dumps(a)}, {json.dumps(v)})") or {}  # noqa: E731
        if action == "fill":
            hit = js("focus", None)
            if not hit.get("ok"):
                return hit
            if sensitive_field(hit.get("full", "") + " " + hit.get("label", ""), hit.get("type", ""), hit.get("auto", ""),
                               hit.get("name", "")):
                self._fx(page, "field", el_id)
                return {**hit, "ok": False, "sensitive": True,
                        "why": "it's a password, PIN, OTP, card or ID-number box, which I never type into. Please type that yourself"}
            self._authorize("browser_fill", hit.get("label", ""), str(value))
            self._fx(page, "field", el_id)
            if hit.get("type") in ("time", "date", "datetime-local", "month", "week", "color", "range"):
                js("setvalue", str(value))
            else:
                self._cdp(page, [("Input.insertText", {"text": str(value)})])
            got = js("readback", None).get("value", "")
            if str(value).strip() and not str(got).strip():        # say so, so nothing retries blindly
                return {**hit, "ok": False, "why": "the value didn't stick"}
            return {**hit, "ok": True}
        if action in ("select", "check"):
            self._authorize("browser_click", f"{action} {el_id}")
            return js(action, value)
        hit = js("click", None)
        if not hit.get("ok") or "x" not in hit:
            return hit
        time.sleep(0.12)                              # let scrollIntoView settle
        hit = js("click", None) or hit
        self._authorize("browser_click", hit.get("label", "") or f"element {el_id}")
        self._fx(page, "click", el_id, hit["x"], hit["y"])
        ev = {"x": hit["x"], "y": hit["y"], "button": "left", "clickCount": 1}
        self._cdp(page, [("Input.dispatchMouseEvent", {**ev, "type": "mouseMoved"}),
                         ("Input.dispatchMouseEvent", {**ev, "type": "mousePressed"}),
                         ("Input.dispatchMouseEvent", {**ev, "type": "mouseReleased"})])
        return hit

    # ---------- research mode: one tab of its own, read only (never a click or a key) ----------
    def research_tab(self) -> "ResearchTab":
        """A new tab for a research run, so a video or page you have open is never navigated away."""
        self.ensure_running()
        page = self._http("/json/new?about:blank", method="PUT")
        self._focus_window(page)
        return ResearchTab(self, page["id"])

    def _fx(self, page: dict, mode: str, el_id=0, x: float = 0, y: float = 0) -> None:
        """The visible ripple / outline. Never allowed to break the action itself."""
        try:
            self._eval(page, f"{JS_FX}({json.dumps(mode)}, {float(x)}, {float(y)}, {int(el_id or 0)})")
        except Exception:
            pass

    def banner(self, text: str | None) -> None:
        """'JARVIS is filling this form…' across the top of the page while a task runs (None removes it)."""
        try:
            self._eval(self.active_page(), f"{JS_BANNER}({'true' if text else 'false'}, {json.dumps(text or '')})")
        except Exception:
            pass

    def auto_scroll(self, on: bool, speed: str = "normal") -> bool:
        step = {"slow": 2, "normal": 5, "fast": 12}.get(speed, 5) * (1 if on else 0) or 5
        self.scrolling = bool(self._eval(self.active_page(), f"{JS_AUTOSCROLL}({'true' if on else 'false'}, {step})"))
        self.scroll_speed = speed
        return self.scrolling

    def auto_scrolling(self) -> bool:
        try:
            return bool(self._eval(self.active_page(), "!!window.__jarvisScroll"))
        except Exception:
            return False

    def status(self) -> dict:
        return {"installed": bool(self.edge_path), "running": self.running(), "port": self.port,
                "profile": str(self.profile_dir)}


class ResearchTab:
    """The tab a research run reads in: open the next address, read the text, scroll, list the links, show a banner.
    Nothing here clicks, types or presses a key. Every address goes through the blocklists first."""

    def __init__(self, edge: EdgeController, page_id: str):
        self.edge, self.page_id = edge, page_id

    def _page(self) -> dict:
        page = next((p for p in self.edge.pages() if p.get("id") == self.page_id), None)
        if page is None:
            raise BrowserError("The research tab was closed.")
        return page

    def open_url(self, url: str) -> str:
        self.edge.check_url(url)
        self.edge._cdp(self._page(), [("Page.navigate", {"url": url})])
        return urlparse(url).hostname or url

    def read(self, offset: int = 0) -> dict:
        return self.edge._eval(self._page(), f"{JS_READ}({max(0, int(offset))})") or {}

    def scroll(self, direction: str) -> None:
        amount = {"down": 700, "up": -700, "top": -10**7, "bottom": 10**7}.get(direction, 700)
        self.edge._eval(self._page(), f"window.scrollBy({{top: {amount}, behavior: 'smooth'}})")

    def links(self) -> list[dict]:
        from .research import JS_LINKS
        return self.edge._eval(self._page(), JS_LINKS) or []

    def banner(self, text: str | None) -> None:
        try:
            self.edge._eval(self._page(), f"{JS_BANNER}({'true' if text else 'false'}, {json.dumps(text or '')})")
        except Exception:
            pass

    def results(self) -> list[dict]:
        """A search results page's organic results (title + link), for research mode's own searches."""
        from .research import JS_RESULTS
        return self.edge._eval(self._page(), JS_RESULTS) or []
