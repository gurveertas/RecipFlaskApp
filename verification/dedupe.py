"""Deduplicate sources so corroboration counts INDEPENDENT outlets.

Two articles from the same domain don't corroborate each other, and syndicated
copies with near-identical titles shouldn't inflate confidence. We keep the
best (most recent, then longest) representative per cluster.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from retrieval.models import Source


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (t or "").lower()).strip()


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


# How many pages one vendor may contribute. Enough for the credential page,
# the exam datasheet and the certification handbook — the three documents a
# certification article genuinely needs — without letting one domain crowd out
# the independent sources the rest of the pipeline depends on.
MAX_OWN_VENDOR_SOURCES = 3


def dedupe(sources: list[Source], title_threshold: float = 0.85,
           vendor_name: str | None = None
           ) -> tuple[list[Source], list[dict]]:
    """Collapse sources that would corroborate each other falsely.

    `vendor_name` lets THIS article's vendor contribute more than one page.
    The same-domain rule exists so corroboration counts independent outlets,
    and for a vendor's own documentation that reasoning does not apply: its
    credential page and its exam datasheet are not two copies of one story,
    they are different documents. Collapsing them cost the Prisma Access SSE
    article its datasheet — the only source carrying the real exam domains and
    their weightings, which the finished article then fabricated.

    Corroboration is unaffected: _corroborate counts DISTINCT DOMAINS, so two
    pages from paloaltonetworks.com still count once and cannot inflate a
    claim's status. Near-duplicate titles are still collapsed for the vendor
    too — that is the syndication case, and it is real on vendor sites.
    """
    kept: list[Source] = []
    dropped: list[dict] = []
    want = (vendor_name or "").strip().lower()
    own_vendor_kept = 0

    # sort so the "best" candidate in each cluster is seen first
    ordered = sorted(
        sources,
        key=lambda s: (s.published or __import__("datetime").date.min, len(s.text)),
        reverse=True,
    )

    for s in ordered:
        nt = _norm_title(s.title)
        is_own_vendor = (bool(want) and s.is_vendor
                         and (s.vendor or "").strip().lower() == want
                         and own_vendor_kept < MAX_OWN_VENDOR_SOURCES)
        clash = None
        for k in kept:
            if s.domain == k.domain and not is_own_vendor:
                clash = ("same domain", k)
                break
            if _similar(nt, _norm_title(k.title)) >= title_threshold:
                clash = ("near-duplicate title", k)
                break
        if clash:
            dropped.append({"url": s.url, "reason": f"deduped ({clash[0]} as {clash[1].url})"})
        else:
            kept.append(s)
            if is_own_vendor:
                own_vendor_kept += 1

    return kept, dropped
