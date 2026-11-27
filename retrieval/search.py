"""Domain-constrained web search.

Default implementation targets the Serper API (google.serper.dev) because it
supports `site:` operators cleanly and returns structured results. The
`SearchClient` protocol means Brave / Bing / SerpAPI can be dropped in without
touching the rest of the pipeline.

No key? Retrieval can't run live, but the offline demo (run.py --offline) does
not use this module at all — it feeds fixtures straight into verification.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

import requests


@dataclass
class SearchHit:
    url: str
    title: str
    snippet: str

    @property
    def domain(self) -> str:
        return urlparse(self.url).netloc.lower()


class SearchClient(Protocol):
    def search(self, query: str, allowed_domains: list[str], limit: int) -> list[SearchHit]:
        ...


class SerperClient:
    """Serper.dev search. Set SERPER_API_KEY in the environment."""

    ENDPOINT = "https://google.serper.dev/search"

    def __init__(self, api_key: str | None = None, timeout: int = 15):
        self.api_key = api_key or os.getenv("SERPER_API_KEY")
        self.timeout = timeout

    def search_open(self, query: str, num: int = 20) -> list[SearchHit]:
        """ONE unconstrained query — the whole web, no site: filters.

        The second half of retrieval's union channel. The scoped search splits
        its domains into batches of 8 and takes each batch's own top 10, so it
        never sees a single global ranking; a strong page can be beaten by a
        weaker one simply for being in a busier batch. This adds the ranking
        Google actually produced, for 1 credit against the 4 the scoped search
        now costs.

        It does NOT widen what can be ingested: every hit still goes through
        fetch_source, which refuses anything off the allowlist. What it earns
        besides ordering is visibility — see record_domain_sighting.

        Measured before building: on product topics 8 of 9 open results were
        already allowlisted, on certification topics 1-2 of 8, the remainder
        being exam-dump farms and, for "CCME certification exam", Cellebrite's
        entirely different Certified Mobile Examiner. Which is exactly why
        this feeds the same allowlist rather than bypassing it.
        """
        if not self.api_key:
            return []
        try:
            resp = requests.post(
                self.ENDPOINT,
                headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
                json={"q": query, "num": num},
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except requests.RequestException:
            return []          # best-effort: the scoped search still stands
        from config.db import record_api_usage
        record_api_usage("serper", "retrieval_open", credits=1)
        return [
            SearchHit(url=item.get("link", ""), title=item.get("title", ""),
                      snippet=item.get("snippet", ""))
            for item in resp.json().get("organic", [])
        ]

    def search(self, query: str, allowed_domains: list[str], limit: int = 12) -> list[SearchHit]:
        if not self.api_key:
            raise RuntimeError(
                "SERPER_API_KEY not set. Export it, or run with --offline to use "
                "the bundled sample sources."
            )
        # Constrain the query to allowlisted domains with an OR of site: filters.
        # Serper caps query length, so we batch domains if the list is large.
        # Keep each batch's hits separate so we can interleave them below.
        batch_hits: list[list[SearchHit]] = []
        credits_spent = 0
        try:
            for domain_batch in _batched(allowed_domains, 8):
                site_filter = " OR ".join(f"site:{d}" for d in domain_batch)
                payload = {"q": f"{query} ({site_filter})", "num": 10}
                resp = requests.post(
                    self.ENDPOINT,
                    headers={"X-API-KEY": self.api_key, "Content-Type": "application/json"},
                    json=payload,
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                credits_spent += 1   # one credit per request at num=10
                batch_hits.append([
                    SearchHit(
                        url=item.get("link", ""),
                        title=item.get("title", ""),
                        snippet=item.get("snippet", ""),
                    )
                    for item in resp.json().get("organic", [])
                ])
        finally:
            # finally, NOT after the loop: this is a MULTI-request loop, and
            # recording only on full success meant one failed batch (a 429, a
            # timeout, a dropped connection) discarded the credits every
            # earlier batch had already spent. Serper had billed them; the
            # meter never saw them, so "credits left" drifted above the real
            # balance and kept drifting, since each loss is permanent.
            #
            # Only requests that got past raise_for_status() are counted, so a
            # rejected call — which Serper does not bill — is still not
            # counted here.
            if credits_spent:
                # One row per retrieve() call, credits = number of domain
                # batches — this is why "create brief" costs 3-4 credits, not
                # 1, and the usage page should show that honestly.
                from config.db import record_api_usage
                record_api_usage("serper", "retrieval", credits=credits_spent)
        return _interleave(batch_hits, limit)


def _batched(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def _interleave(batch_hits: list[list[SearchHit]], limit: int,
                per_domain: int = 2) -> list[SearchHit]:
    """Round-robin across batches so domain diversity survives the `limit` cut.

    Taking the first `limit` hits would let a single dominant domain (e.g. the
    vendor whose site: batch runs first) fill the whole quota, leaving nothing
    for the independent press/salary domains that corroboration needs. Instead
    we take one eligible hit from each batch per round, and cap how many hits any
    single domain contributes, so the result spans as many domains as possible.
    """
    seen_urls: set[str] = set()
    per_domain_count: dict[str, int] = {}
    cursors = [0] * len(batch_hits)
    out: list[SearchHit] = []

    progressed = True
    while progressed and len(out) < limit:
        progressed = False
        for b, hits in enumerate(batch_hits):
            while cursors[b] < len(hits):
                h = hits[cursors[b]]
                cursors[b] += 1
                if not h.url or h.url in seen_urls:
                    continue
                if per_domain_count.get(h.domain, 0) >= per_domain:
                    continue
                seen_urls.add(h.url)
                per_domain_count[h.domain] = per_domain_count.get(h.domain, 0) + 1
                out.append(h)
                progressed = True
                break
            if len(out) >= limit:
                break
    return out
