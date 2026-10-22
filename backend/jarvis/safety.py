"""Security guard for JARVIS's own Edge window: bad sites are blocked before they load, and your sensitive details
are never typed by JARVIS.

* Site blocklists (free, stored on this laptop in data/blocklist/, refreshed weekly): StevenBlack (malware, adware,
  gambling, adult), Phishing Army, malware-filter's URLhaus and phishing lists. About 340,000 domains.
  Every navigation JARVIS starts is checked first; a watchdog also catches pages reached by clicking a link.
* Also blocked: non-web schemes (file:, javascript:…), addresses with a user name in them (a phishing trick),
  look-alike international domains (xn--…), and well-known piracy sites.
* Sensitive fields (passwords, OTPs, card numbers/CVV, Aadhaar, PAN, bank details, UPI PIN) are never typed by JARVIS,
  whatever the command says: you type those yourself.
"""
from __future__ import annotations

import ipaddress
import re
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

LISTS = {
    "stevenblack": "https://raw.githubusercontent.com/StevenBlack/hosts/master/alternates/gambling-porn/hosts",
    "phishing-army": "https://phishing.army/download/phishing_army_blocklist.txt",
    "urlhaus": "https://malware-filter.gitlab.io/malware-filter/urlhaus-filter-hosts.txt",
    "phishing-filter": "https://malware-filter.gitlab.io/malware-filter/phishing-filter-hosts.txt",
}
# Piracy and other plainly illegal-content sites (the lists above don't cover piracy).
ILLEGAL_PATTERNS = re.compile(r"(?:^|\.)(?:thepiratebay|piratebay|1337x|rarbg|yts|eztv|limetorrents|torrentz\d*|kickass\w*|"
                              r"fmovies|123movies|putlocker|solarmovie|gomovies|soap2day|tamilrockers|filmyzilla|"
                              r"movierulz|filmywap|9xmovies|skidrowreloaded|igg-games|fitgirl-repacks|crackwatch|"
                              r"warez\w*|keygen\w*|z-lib|libgen)\.", re.I)
LOCAL_OK = {"localhost", "127.0.0.1"}

SENSITIVE_LABEL = re.compile(r"\b(?:pass ?word|passcode|pass ?phrase|pin|m-?pin|upi pin|otp|one[- ]time|verification code|"
                             r"security code|auth(?:entication)? code|2fa|cvv|cvc|card (?:number|no)|credit card|debit card|"
                             r"expiry|expiration|aadhaa?r|\bpan\b|pan (?:card|number)|ssn|social security|bank account|"
                             r"account (?:number|no)|ifsc|iban|swift|routing number|net ?banking|security answer|"
                             r"mother'?s maiden)\b", re.I)
SENSITIVE_AUTOCOMPLETE = re.compile(r"\b(?:cc-[\w-]+|one-time-code|current-password|new-password)\b", re.I)


def sensitive_field(label: str = "", type_: str = "", autocomplete: str = "", name: str = "") -> bool:
    """Is this a box JARVIS must never type into?"""
    if (type_ or "").lower() == "password":
        return True
    if SENSITIVE_AUTOCOMPLETE.search(autocomplete or ""):
        return True
    return bool(SENSITIVE_LABEL.search(f"{label} {name}".replace("_", " ").replace("-", " ")))


class SiteGuard:
    REFRESH_S = 7 * 24 * 3600

    def __init__(self, folder: Path, allow: list[str] | None = None):
        self.folder = Path(folder)
        self.allow = {a.lower().strip() for a in (allow or []) if a}
        self.domains: set[str] = set()
        self.loaded_at = 0.0
        self._lock = threading.Lock()
        self.load()

    # ---- lists ----
    @staticmethod
    def parse(text: str) -> set[str]:
        out = set()
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip().lower()
            if not line:
                continue
            parts = line.split()
            host = parts[1] if len(parts) >= 2 and parts[0] in ("0.0.0.0", "127.0.0.1") else parts[0]
            if "." in host and host not in ("0.0.0.0", "localhost", "broadcasthost", "local") and \
                    re.fullmatch(r"[a-z0-9.-]+", host):
                out.add(host.strip("."))
        return out

    def load(self) -> int:
        domains = set()
        for f in self.folder.glob("*.txt"):
            try:
                domains |= self.parse(f.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                pass
        with self._lock:
            self.domains = domains
            self.loaded_at = time.time()
        return len(domains)

    def needs_refresh(self) -> bool:
        files = list(self.folder.glob("*.txt"))
        return not files or time.time() - min(f.stat().st_mtime for f in files) > self.REFRESH_S

    def refresh(self) -> dict:
        """Download the free lists (only these four public URLs; nothing about you is sent)."""
        self.folder.mkdir(parents=True, exist_ok=True)
        result = {}
        for name, url in LISTS.items():
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "JARVIS-Zero-blocklist/1.0"})
                for attempt in range(3):                     # networks hiccup: try again before giving up
                    try:
                        with urllib.request.urlopen(req, timeout=60) as r:
                            data = r.read(30_000_000).decode("utf-8", errors="ignore")
                        break
                    except OSError:
                        if attempt == 2:
                            raise
                        time.sleep(3 * (attempt + 1))
                if len(self.parse(data)) > 500:             # a broken/empty download must not wipe a good list
                    (self.folder / f"{name}.txt").write_text(data, encoding="utf-8")
                    result[name] = "ok"
                else:
                    result[name] = "too small, kept the old copy"
            except Exception as exc:
                result[name] = f"failed ({type(exc).__name__}), kept the old copy"
        self.load()
        return result

    # ---- checking ----
    def check(self, url: str) -> str | None:
        """None if fine, else a short spoken reason it's blocked."""
        url = (url or "").strip()
        m = re.match(r"^([a-z][a-z0-9+.-]*):(?!\d)", url, re.I)        # "javascript:…", "file:…" (not "site.com:8080")
        if m and "://" not in url[:len(m.group(0)) + 2] and m.group(1).lower() not in ("http", "https", "about"):
            return f"I only open normal web pages, not {m.group(1).lower()}: links"
        try:
            p = urlparse(url if "://" in url else f"https://{url}")
        except ValueError:
            return "that address doesn't look right"
        scheme, host = (p.scheme or "").lower(), (p.hostname or "").lower().strip(".")
        if scheme in ("about", "edge", "chrome") and url.startswith(("about:blank", "edge://newtab", "chrome://newtab")):
            return None
        if scheme not in ("http", "https"):
            return f"I only open normal web pages, not {scheme}: links"
        if not host:
            return "that address has no website in it"
        if "@" in (p.netloc or ""):
            return "that link hides its real address (a common phishing trick)"
        if host in LOCAL_OK or host in self.allow or any(host.endswith("." + a) for a in self.allow):
            return None
        try:
            ip = ipaddress.ip_address(host)
            return None if ip.is_loopback else "it's a bare IP address, which scam and malware links often use"
        except ValueError:
            pass
        if any(label.startswith("xn--") for label in host.split(".")):
            return "it uses look-alike letters in its name (a common phishing trick)"
        if ILLEGAL_PATTERNS.search(host + "."):
            return "it's a known piracy site"
        labels = host.split(".")
        with self._lock:
            for i in range(len(labels) - 1):
                if ".".join(labels[i:]) in self.domains:
                    return "it's on the malware, phishing, scam, gambling or adult blocklist"
        return None
