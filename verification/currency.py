"""Currency guardrail — catches the 'stale fact' failure mode.

Two checks:
  1. Known superseded facts (e.g. PCNSE retired -> NGFW Engineer), driven by
     the Vendor Knowledge Base's retired Certification records (config/db.py,
     entered/maintained via webapp/admin.py). If a source or the topic
     mentions one, emit the warning so a human confirms the source isn't
     repeating outdated framing.
  2. Stale-year framing, driven by config/settings.yaml: a source that
     presents an old year as "the" current guide/data when we're well past it.

These are warnings, not hard drops — the human reviewer decides.
"""
from __future__ import annotations

import re
from datetime import date

from retrieval.models import Source
from config import load_settings, load_retired_certifications


def check_currency(sources: list[Source], topic: str,
                   today: date | None = None) -> list[str]:
    settings = load_settings()
    today = today or date.today()
    warnings: list[str] = []

    # ---- 1. superseded-fact patterns -------------------------------------
    haystacks = [(topic, "topic")] + [(s.text, s.domain) for s in sources]
    for flag in load_retired_certifications():
        pattern = flag["pattern"]
        rx = re.compile(re.escape(pattern), re.I)
        where = [name for text, name in haystacks if rx.search(text or "")]
        if where:
            locations = ", ".join(sorted(set(where))[:4])
            warnings.append(
                f"[currency] '{pattern}' appears in: {locations}. "
                + " ".join(flag["warning"].split())
            )

    # ---- 2. stale-year framing -------------------------------------------
    lookback = settings.get("stale_year_lookback", 2)
    stale_before = today.year - lookback
    year_rx = re.compile(r"\b(20\d{2})\b")
    guide_rx = re.compile(r"\b(guide|roadmap|latest|complete)\b", re.I)
    for s in sources:
        title = s.title or ""
        years = [int(y) for y in year_rx.findall(title)]
        if years and guide_rx.search(title) and max(years) <= stale_before:
            warnings.append(
                f"[currency] Source title looks stale for {today.year}: "
                f"\"{title[:90]}\" — frames a {max(years)} guide as current."
            )

    return warnings
