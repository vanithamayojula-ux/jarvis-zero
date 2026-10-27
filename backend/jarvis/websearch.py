"""Web lookups for things JARVIS can't know by itself (news, prices, scores, weather, recent events), through a search
API you give it a key for. Only the search words leave the laptop. The answer is written by JARVIS's own model, from
the results only, so the search service is used for information, never as the brain.

Keys live in .env (never in settings.json): TAVILY_API_KEY, BRAVE_API_KEY or SERPER_API_KEY. The first one present
is used, or the one named by the search_provider setting."""
from __future__ import annotations

import os
import re
import threading
import time

import httpx

PROVIDERS = {"tavily": "TAVILY_API_KEY", "brave": "BRAVE_API_KEY", "serper": "SERPER_API_KEY"}
SIGNUP = {"tavily": "https://app.tavily.com", "brave": "https://api-dashboard.search.brave.com",
          "serper": "https://serper.dev"}


def search_key(provider: str = "auto") -> tuple[str, str]:
    """(provider, key) for the chosen provider, or the first one with a key; ("", "") when there's none."""
    names = [provider] if provider in PROVIDERS else list(PROVIDERS)
    for name in names:
        key = os.environ.get(PROVIDERS[name], "").strip()
        if key:
            return name, key
    return "", ""


def available(settings) -> bool:
    return settings.get("web_search") is not False and bool(search_key(settings.get("search_provider") or "auto")[1])


class SearchError(Exception):
    pass


class WebSearch:
    """Top results as [{"title", "url", "text"}]. Small cache (same question within 10 minutes = no new request) and a
    pause after a rate limit or a bad key, so a problem never turns into a stream of failing requests."""

    def __init__(self, settings, client: httpx.Client | None = None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=httpx.Timeout(8.0, connect=3.0))
        self._cache: dict[str, tuple[float, list[dict]]] = {}
        self._paused_until = 0.0
        self._lock = threading.Lock()

    def search(self, query: str, n: int = 5) -> list[dict]:
        query = re.sub(r"\s+", " ", query).strip()[:300]
        if not query:
            return []
        provider, key = search_key(self.settings.get("search_provider") or "auto")
        if not key:
            raise SearchError("no key")
        with self._lock:
            hit = self._cache.get(query.lower())
            if hit and time.monotonic() - hit[0] < 600:
                return hit[1]
            if time.monotonic() < self._paused_until:
                raise SearchError("paused")
        try:
            results = getattr(self, f"_{provider}")(query, key, n)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            with self._lock:   # 401/403 = bad key, 429 = out of free searches: stop asking for a while
                self._paused_until = time.monotonic() + (600 if code in (401, 403) else 120 if code == 429 else 20)
            raise SearchError(f"HTTP {code}") from exc
        except httpx.HTTPError as exc:
            raise SearchError(type(exc).__name__) from exc
        results = [r for r in results if r["text"] or r["title"]][:n]
        with self._lock:
            if len(self._cache) > 50:
                self._cache.clear()
            self._cache[query.lower()] = (time.monotonic(), results)
        return results

    def _tavily(self, q: str, key: str, n: int) -> list[dict]:
        r = self.client.post("https://api.tavily.com/search", headers={"Authorization": f"Bearer {key}"},
                             json={"query": q, "max_results": n, "search_depth": "basic", "include_answer": False})
        r.raise_for_status()
        return [{"title": x.get("title", ""), "url": x.get("url", ""), "text": x.get("content", "")}
                for x in r.json().get("results", [])]

    def _brave(self, q: str, key: str, n: int) -> list[dict]:
        r = self.client.get("https://api.search.brave.com/res/v1/web/search", params={"q": q, "count": n},
                            headers={"X-Subscription-Token": key, "Accept": "application/json"})
        r.raise_for_status()
        return [{"title": x.get("title", ""), "url": x.get("url", ""),
                 "text": re.sub(r"<[^>]+>", "", " ".join([x.get("description", "")] + x.get("extra_snippets", [])[:2]))}
                for x in r.json().get("web", {}).get("results", [])]

    def _serper(self, q: str, key: str, n: int) -> list[dict]:
        r = self.client.post("https://google.serper.dev/search", headers={"X-API-KEY": key}, json={"q": q, "num": n})
        r.raise_for_status()
        data = r.json()
        out = []
        box = data.get("answerBox") or {}
        if box.get("answer") or box.get("snippet"):
            out.append({"title": box.get("title", "Answer"), "url": box.get("link", ""),
                        "text": box.get("answer") or box.get("snippet", "")})
        return out + [{"title": x.get("title", ""), "url": x.get("link", ""), "text": x.get("snippet", "")}
                      for x in data.get("organic", [])]


def results_block(results: list[dict], limit: int = 3500, fence: bool = False) -> str:
    """Results as numbered plain text for the model, capped so the prompt stays small (fast, and cheap on tokens).
    fence=True wraps them as untrusted data (a random DATA fence, redacted, invisible characters removed)."""
    out, used = [], 0
    for i, r in enumerate(results, 1):
        text = re.sub(r"\s+", " ", r["text"]).strip()[:900]
        piece = f"[{i}] {r['title']} ({_site(r['url'])})\n{text}"
        if used + len(piece) > limit:
            break
        out.append(piece)
        used += len(piece)
    block = "\n\n".join(out)
    if fence:
        from .untrusted import untrusted
        return untrusted(block, "web search", limit=limit)
    return block


def _site(url: str) -> str:
    m = re.match(r"^https?://(?:www\.)?([^/:]+)", url or "")
    return m.group(1) if m else ""
