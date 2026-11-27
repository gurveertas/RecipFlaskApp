"""Recency filtering.

Time-sensitive topics (cert changes, threat news, salary data) must not lean on
stale sources. Evergreen topics (fundamentals, exam guides) are exempt from the
age cap. A source with no detectable date is kept but noted, since dropping it
outright would silently discard usable material.

So is the article's OWN VENDOR — see filter_recent.
"""
from __future__ import annotations

from datetime import date

from retrieval.models import Source
from config import load_settings


def is_evergreen(topic: str) -> bool:
    kw = load_settings()["recency"]["evergreen_keywords"]
    low = topic.lower()
    return any(k in low for k in kw)


def filter_recent(sources: list[Source], topic: str,
                  today: date | None = None,
                  vendor_name: str | None = None) -> tuple[list[Source], list[dict]]:
    """Drop sources older than the cap, except where age proves nothing.

    `vendor_name` exempts THIS article's vendor from the age cap. Palo Alto's
    own page for the Security Service Edge Engineer credential reports a
    published date of 2020-01-01 and was being dropped at 2,430 days — the
    single most authoritative page for the article, discarded for being old.
    It is the vendor's own documentation of its own certification: it is
    correct because of who published it, not when. That 2020-01-01 is almost
    certainly a template default rather than a real date, which is the second
    reason it has no business disqualifying the page.

    Scoped to the article's vendor, not to is_vendor generally: a stale
    competitor page is exactly what the cap should still be catching, and
    "vendor" is not a synonym for "relevant".

    Version staleness is not what this protects against anyway — that is
    version_strings (V1), which checks release claims against the KB and a
    verbatim source quote, and it does that whatever the page's date says.
    """
    settings = load_settings()["recency"]
    max_age = settings["max_age_days"]
    today = today or date.today()

    if is_evergreen(topic):
        return sources, []   # age cap not applied to evergreen topics

    want = (vendor_name or "").strip().lower()

    kept, dropped = [], []
    for s in sources:
        if want and s.is_vendor and (s.vendor or "").strip().lower() == want:
            kept.append(s)
            continue
        age = s.age_days(today)
        if age is not None and age > max_age:
            dropped.append({
                "url": s.url,
                "reason": f"stale: {age} days old (cap {max_age}) for non-evergreen topic",
            })
        else:
            kept.append(s)
    return kept, dropped
