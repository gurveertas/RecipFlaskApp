"""Live-SERP signals: what actually ranks for a keyword right now.

Two consumers, one Serper query pattern (a PLAIN search, deliberately NOT the
allowlist-filtered site: search retrieval/__init__.py uses — the whole point
is seeing the real competitive landscape, not just our trusted sources):

  competitor_snapshot() — top ranking pages + their word counts, feeding
      validate.py's serp_competitiveness check (WARN-only: "your draft is
      thinner than what currently ranks").
  serp_difficulty()     — a measured difficulty score for topic scoring,
      replacing the LLM's guessed `difficulty` estimate with observed SERP
      occupancy (synthesis/topics.py's formula stays untouched; only the
      input number gets real).

Competitor pages are arbitrary domains, so fetching happens here with plain
requests + trafilatura and the text is used for COUNTING ONLY — it must never
be fed to the Writer as source material. retrieval/fetch.py's allowlist gate
stays the only door into the grounding pipeline.

Everything returns None on any failure (no key, network error, empty SERP):
these are enrichment signals, and their absence must never break topic
generation or article writing.
"""
from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urlparse

# Domains whose presence in a top-10 makes it materially harder for a
# specialist training company to rank. Two kinds: global giants (below), and
# any tier-1 vendor domain from our own allowlist (checked via config at call
# time — an official paloaltonetworks.com doc in the top 10 crowds out
# third-party content for exactly the product keywords we target).
_GIANTS = {
    "wikipedia.org", "youtube.com", "reddit.com", "linkedin.com",
    "medium.com", "coursera.org", "udemy.com", "pluralsight.com",
    "forbes.com", "gartner.com", "techtarget.com", "geeksforgeeks.org",
    "stackoverflow.com", "github.com", "amazon.com", "google.com",
    "microsoft.com", "ibm.com",
}

_OWN_SITE = "rededucation.com"

_UA = "Mozilla/5.0 (compatible; RedEd-BlogGen/1.0; +serp-analysis)"


@dataclass
class CompetitorPage:
    url: str
    title: str
    word_count: int


@dataclass
class SerpSnapshot:
    keyword: str
    pages: list[CompetitorPage] = field(default_factory=list)

    @property
    def median_word_count(self) -> int:
        counts = sorted(p.word_count for p in self.pages)
        if not counts:
            return 0
        mid = len(counts) // 2
        return counts[mid] if len(counts) % 2 else (counts[mid - 1] + counts[mid]) // 2


# One unfiltered SERP, shared by everything in this module. competitor_snapshot
# and serp_difficulty both ask for the top 10 of a keyword, for different
# reasons, so an article's keyword was bought twice: once when its topic was
# scored and again at QA. Caching at this boundary — the only place a credit is
# actually spent — collapses that, and covers every future caller for free.
#
# Keyed on num as well as the query, because a top-10 is not a top-20.
_SEARCH_TTL = 6 * 3600
_SEARCH_FAILURE_TTL = 600
_search_cache: dict[tuple[str, int], tuple[float, "list[dict] | None"]] = {}


def _plain_search(query: str, num: int = 10,
                  purpose: str = "serp") -> list[dict] | None:
    """One unfiltered Serper query -> organic results, or None if unavailable.
    `purpose` labels the spent credit in the ApiUsage meter (recorded only on
    a successful request — a failed call that Serper rejected isn't billed).

    Cached for _SEARCH_TTL. A cache hit spends no credit and records none,
    which is correct: nothing was bought."""
    import time

    key = ((query or "").strip().lower(), num)
    cached = _search_cache.get(key)
    if cached is not None:
        ttl = _SEARCH_TTL if cached[1] is not None else _SEARCH_FAILURE_TTL
        if time.monotonic() - cached[0] < ttl:
            return cached[1]

    def _remember_search(value):
        _search_cache[key] = (time.monotonic(), value)
        return value

    api_key = os.getenv("SERPER_API_KEY")
    if not api_key:
        return None                      # not cached: config, not a result
    import requests
    try:
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": num},
            timeout=15,
        )
        resp.raise_for_status()
    except Exception:
        return _remember_search(None)
    from config.db import record_api_usage
    record_api_usage("serper", purpose, credits=1)
    return _remember_search(resp.json().get("organic", []) or None)


def _domain(url: str) -> str:
    d = urlparse(url).netloc.lower()
    return d[4:] if d.startswith("www.") else d


def _is_authority(domain: str) -> bool:
    parts = domain.split(".")
    candidates = {".".join(parts[i:]) for i in range(len(parts) - 1)}
    if candidates & _GIANTS:
        return True
    if domain.endswith((".gov", ".edu")):
        return True
    # Tier-1 vendor domains from our own allowlist (official product docs
    # dominating a SERP is the classic hard-to-crack pattern in this niche).
    from config import load_allowlist
    meta = load_allowlist().match(domain)
    return bool(meta and meta["is_vendor"])


_MAX_PAGE_BYTES = 2_000_000   # competitor pages are for COUNTING; cap the read


def _page_word_count(url: str, timeout: int = 12) -> int | None:
    """Fetch + extract one competitor page, return its main-text word count.
    None on any failure — a competitor page we can't read is just skipped.

    Guarded: these are arbitrary SERP-result URLs, so only text/html is
    accepted and at most _MAX_PAGE_BYTES are read (streamed) — a ranking URL
    that turns out to be a giant PDF/download must not balloon memory or
    stall a write for the full timeout."""
    import requests
    try:
        import trafilatura
        with requests.get(url, headers={"User-Agent": _UA}, timeout=timeout,
                          stream=True) as resp:
            resp.raise_for_status()
            if "html" not in resp.headers.get("content-type", ""):
                return None
            total, chunks = 0, []
            for chunk in resp.iter_content(65536):
                chunks.append(chunk)
                total += len(chunk)
                if total >= _MAX_PAGE_BYTES:
                    break
            html = b"".join(chunks).decode(resp.encoding or "utf-8",
                                           errors="replace")
        text = trafilatura.extract(html, include_comments=False,
                                   favor_precision=True)
        if not text:
            return None
        return len(re.findall(r"\w+", text))
    except Exception:
        return None


# How long a SERP snapshot stays usable. Google's top ten for a keyword does
# not move meaningfully within a working session, and this feeds
# serp_competitiveness, which is WARN-ONLY by design — it can never block an
# article, so paying for a fresh one on every QA run buys nothing.
#
# It was costing real money: 71 of the last 245 Serper credits, 29% of all
# spend, and the log shows "edu-330 firewall troubleshooting" fetched 13 times
# in one afternoon because every write, targeted fix and re-score refetched it.
_SNAPSHOT_TTL = 6 * 3600
# A failure is cached too, briefly. Without that, a SERP outage means every
# QA run pays for another timeout; with a long TTL, one blip would blind the
# check for the rest of the day.
_SNAPSHOT_FAILURE_TTL = 600
_snapshot_cache: dict[str, tuple[float, "SerpSnapshot | None"]] = {}


def competitor_snapshot(keyword: str, top_n: int = 5) -> SerpSnapshot | None:
    """The top-N ranking pages for `keyword` with their word counts.
    Skips our own site (comparing a draft against the page it might replace
    tells us nothing) and pages that fail to fetch. None if the SERP itself
    is unavailable or nothing could be measured.

    Cached per keyword for _SNAPSHOT_TTL — see the note there."""
    import time

    key = (keyword or "").strip().lower()
    cached = _snapshot_cache.get(key)
    if cached is not None:
        age = time.monotonic() - cached[0]
        ttl = _SNAPSHOT_TTL if cached[1] is not None else _SNAPSHOT_FAILURE_TTL
        if age < ttl:
            return cached[1]

    def _remember(value):
        _snapshot_cache[key] = (time.monotonic(), value)
        return value

    organic = _plain_search(keyword, num=10, purpose="competitor_snapshot")
    if not organic:
        return _remember(None)
    hits = [(h.get("link", ""), h.get("title", "")) for h in organic
            if h.get("link") and _OWN_SITE not in _domain(h.get("link", ""))][:top_n]
    if not hits:
        return _remember(None)

    snapshot = SerpSnapshot(keyword=keyword)
    with ThreadPoolExecutor(max_workers=5) as ex:
        counts = list(ex.map(lambda h: _page_word_count(h[0]), hits))
    for (url, title), wc in zip(hits, counts):
        if wc and wc >= 200:   # ignore stubs/blocked pages
            snapshot.pages.append(CompetitorPage(url=url, title=title, word_count=wc))
    return _remember(snapshot if snapshot.pages else None)


def serp_difficulty(keyword: str) -> tuple[int, str] | None:
    """Measured ranking difficulty (0-100) for a keyword, from who actually
    occupies its top 10 — authority-domain density plus exact-match titles.
    A transparent heuristic, deliberately simple: its value over the LLM's
    guess is that it's OBSERVED, not estimated. Returns (score, detail) or
    None when the SERP is unavailable."""
    organic = _plain_search(keyword, num=10, purpose="serp_difficulty")
    if not organic:
        return None
    top = organic[:10]
    authority = sum(1 for h in top if _is_authority(_domain(h.get("link", ""))))
    exact = sum(1 for h in top
                if keyword.lower() in (h.get("title") or "").lower())
    # Base 20 (nothing is free), +8 per authority occupant, +4 per
    # exact-title competitor — 10 authority domains saturates at 100.
    score = max(0, min(100, 20 + authority * 8 + exact * 4))
    detail = (f"{authority} authority domain(s), {exact} exact-title match(es) "
              f"in top {len(top)}")
    return score, detail
