"""Load and index config data.

Foundation stage: this used to read sources.yaml/settings.yaml/course_links.yaml
directly. It now reads from the database (config/db.py), seeded once from those
same YAML files on first run (see db.py's module docstring for the seeding
contract). Every function here reconstructs the EXACT dict/object shape it
returned before the migration, so nothing outside this file changed — see
retrieval/, verification/, synthesis/ for the (unmodified) consumers.
"""
from __future__ import annotations

from functools import lru_cache

from sqlalchemy import select

from .db import (AllowlistDomain, AppConfig, Certification, CertificationStatus,
                 CtaPhrase, DomainCategory, EvergreenKeyword, SecurityKeyword,
                 Vendor, get_session, seed_if_empty)

seed_if_empty()


@lru_cache(maxsize=1)
def load_settings() -> dict:
    with get_session() as s:
        cfg = s.get(AppConfig, 1)
        evergreen = [k.phrase for k in s.scalars(
            select(EvergreenKeyword).order_by(EvergreenKeyword.id))]
        security = [k.keyword for k in s.scalars(
            select(SecurityKeyword).order_by(SecurityKeyword.id))]
        return {
            "recency": {
                "max_age_days": cfg.max_age_days,
                "evergreen_keywords": evergreen,
            },
            "corroboration": {
                "min_independent_domains": cfg.min_independent_domains,
                "security_keywords": security,
            },
            "stale_year_lookback": cfg.stale_year_lookback,
        }


def _certification_warning(cert: Certification) -> str:
    if cert.notes:
        return cert.notes
    tail = (f" and replaced by the {cert.replaced_by.name} credential."
            if cert.replaced_by else ".")
    return (f"{cert.name} was retired by {cert.vendor.name} in {cert.retired_year}"
            + tail + f" Verify whether the source treats {cert.name} as current "
            "before repeating that framing.")


def load_retired_certifications() -> list[dict]:
    """Vendor Knowledge Base: retired certifications as currency-warning
    tripwires — the structured replacement for the old freeform CurrencyFlag
    table. Same [{"pattern":..., "warning":...}] shape verification/currency.py
    already consumes, so that file needed only a one-line change.

    Deliberately NOT @lru_cache'd, unlike the other loaders above: those read
    Foundation data that's seed-once-then-restart-to-reload by design, but
    Certification rows are live-edited through the admin UI (webapp/admin.py)
    while the app is running — marking a cert retired needs to affect the very
    next generation request, not wait for a restart. This query is cheap
    (small table, no join fan-out) so paying it per-call is a non-issue.

    Ordered by id, not name: order is part of check_currency()'s user-visible
    output (a warnings list), and alphabetical order would silently reorder it
    relative to today's insertion-order behavior.
    """
    with get_session() as s:
        certs = s.scalars(
            select(Certification)
            .where(Certification.status == CertificationStatus.RETIRED)
            .order_by(Certification.id))
        return [{"pattern": c.name, "warning": _certification_warning(c)}
                for c in certs]


@lru_cache(maxsize=1)
def load_course_links() -> dict:
    with get_session() as s:
        cfg = s.get(AppConfig, 1)
        # Compatibility-critical: only vendors WITH a course_path, matching the
        # 5-entry dict course_links.yaml had today. Reconstructing all vendors
        # here would silently widen synthesis/generate.py's _vendor_of() name
        # matching from 5 to ~23 vendors as a side effect of this migration —
        # a real behaviour change, not something to let fall out by accident.
        vendors = {v.name: v.course_path for v in s.scalars(
            select(Vendor).where(Vendor.course_path.isnot(None)))}
        ctas = [c.phrase for c in s.scalars(
            select(CtaPhrase).order_by(CtaPhrase.position))]
        return {
            "base_url": cfg.course_base_url,
            "vendors": vendors,
            "cta_phrases": ctas,
        }


@lru_cache(maxsize=1)
def _raw_sources() -> dict:
    """Rebuild the exact {"tier1_vendor": [...], ...} shape Allowlist expects."""
    category_to_key = {
        DomainCategory.VENDOR: "tier1_vendor",
        DomainCategory.AUTHORITY: "tier1_authority",
        DomainCategory.PRESS: "tier2_press",
        DomainCategory.DATA: "tier2_data",
        DomainCategory.JOBS: "tier2_jobs",
    }
    raw: dict[str, list[dict]] = {k: [] for k in category_to_key.values()}
    with get_session() as s:
        rows = s.scalars(
            select(AllowlistDomain).order_by(AllowlistDomain.position))
        for row in rows:
            entry = {"domain": row.domain}
            if row.category == DomainCategory.VENDOR:
                entry["vendor"] = row.vendor.name
            raw[category_to_key[row.category]].append(entry)
    return raw


# Topic wording that makes salary/vacancy sources relevant.
_JOBS_INTENT = ("salary", "salaries", "pay", "paid", "compensation", "career",
                "careers", "job", "jobs", "hiring", "demand", "worth it",
                "roi", "earn", "earning", "wage", "market rate")


class Allowlist:
    """Indexed view over the allowlist domains for O(1) lookups."""

    def __init__(self, raw: dict):
        self.raw = raw
        self._by_domain: dict[str, dict] = {}
        self._ingest(raw.get("tier1_vendor", []), tier=1, vendor=True)
        self._ingest(raw.get("tier1_authority", []), tier=1, vendor=False)
        self._ingest(raw.get("tier2_press", []), tier=2, vendor=False)
        self._ingest(raw.get("tier2_data", []), tier=2, vendor=False)
        self._ingest(raw.get("tier2_jobs", []), tier=2, vendor=False, jobs=True)

    def _ingest(self, entries: list, tier: int, vendor: bool, jobs: bool = False):
        for e in entries or []:
            domain = e["domain"].lower().strip()
            self._by_domain[domain] = {
                "tier": tier,
                "is_vendor": vendor,
                "is_jobs": jobs,
                "vendor": e.get("vendor"),
            }

    def domains(self) -> list[str]:
        return list(self._by_domain.keys())

    def domains_for_topic(self, topic: str,
                          vendor_name: str | None = None) -> list[str]:
        """Scope the search to this topic's vendor.

        `vendor_name` is the vendor the CALLER already knows — every Studio
        path has one on the topic row. Pass it. Deriving the vendor by
        string-matching the topic text is a fallback for callers that have
        nothing else, and it fails on exactly the topics that most need
        scoping: a certification is named by its ACRONYM, and an acronym never
        contains its vendor's name. "PCNSE certification", "CCTE R82
        certification" and "prisma access sse certification" all matched no
        vendor, fell through to all 53 domains, and searched every competitor
        alongside the right one. Measured on the estate: 4 of 4 certification
        guides ended up with zero sources from the vendor whose exam they
        describe, and the Prisma Access guide cited a Fortinet page comparing
        Fortinet's firewall to Palo Alto's as [S1].

        A topic like "Cisco CCNP salary" should not search Fortinet/AWS vendor
        sites — that only pulls in off-topic cert pages. So we keep the matched
        vendor's domain(s) and drop every OTHER vendor's domains, while always
        keeping the non-vendor domains (press, salary/cert data, authorities)
        because those are what corroboration draws on. If no known vendor is
        named, we fall back to all domains (unchanged behaviour).
        """
        t = (topic or "").lower()
        # Salary/vacancy aggregators only for topics actually about pay or
        # careers. They have a page for practically any string, so on an
        # unrelated topic they don't return nothing — they return a confident
        # match to a different organisation. A Nutanix "NMCAA certification
        # guide" was handed "39 preschool teacher jobs in Cadillac, MI",
        # because NMCAA is also the Northwest Michigan Community Action
        # Agency. It became [S1], the article's first listed reference.
        wants_jobs = any(w in t for w in _JOBS_INTENT)
        neutral = [d for d, m in self._by_domain.items()
                   if not m["is_vendor"] and (wants_jobs or not m.get("is_jobs"))]

        def aliases(domain: str, meta: dict) -> set[str]:
            name = (meta.get("vendor") or "").lower().strip()
            cands = {domain.split(".")[0]}          # e.g. "cisco" from cisco.com
            if name:
                cands.add(name)                      # "palo alto networks"
                cands.add(name.replace(" ", ""))     # "paloaltonetworks"
                words = name.split()
                if len(words) >= 2:
                    cands.add(" ".join(words[:2]))   # "palo alto", "check point"
            return {c for c in cands if len(c) >= 3}

        if vendor_name:
            want = vendor_name.lower().strip()
            matched = [d for d, m in self._by_domain.items()
                       if m["is_vendor"]
                       and (m.get("vendor") or "").lower().strip() == want]
            if matched:
                # IDIRA is part of Palo Alto Networks.
                # Include its official parent-vendor documentation.
                if want == "idira":
                    parent = "paloaltonetworks.com"
                    if parent in self._by_domain and parent not in matched:
                        matched.append(parent)
                return matched + neutral
            # A vendor we hold no domains for. Falling through to the string
            # match would search every OTHER vendor instead, which is worse
            # than not scoping — keep the neutral corroboration sources only.
            return neutral

        matched = [d for d, m in self._by_domain.items()
                   if m["is_vendor"] and any(a in t for a in aliases(d, m))]
        if not matched:
            # No known vendor named — every vendor domain stays in play, but
            # the jobs scoping above still applies, so an unrecognised topic
            # can't quietly pull the aggregators back in.
            return [d for d, m in self._by_domain.items()
                    if m["is_vendor"]] + neutral
        return matched + neutral

    def match(self, domain: str) -> dict | None:
        """Return metadata for a domain if it (or its parent) is allowlisted."""
        domain = (domain or "").lower().lstrip(".")
        # exact, then progressively broader parent-domain match
        parts = domain.split(".")
        for i in range(len(parts) - 1):
            candidate = ".".join(parts[i:])
            if candidate in self._by_domain:
                return self._by_domain[candidate]
        return None


@lru_cache(maxsize=1)
def load_allowlist() -> Allowlist:
    return Allowlist(_raw_sources())
