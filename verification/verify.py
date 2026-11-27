"""Verification orchestrator.

Pipeline: dedupe -> recency filter -> currency check -> claim extraction ->
corroboration -> vendor-marketing rule -> assemble VerifiedBundle.

Corroboration rule
------------------
A stat/security claim is VERIFIED only when the same key figure/entity appears
in >= `min_independent_domains` distinct domains. Claims from a single source
stay UNVERIFIED (usable, but the reviewer knows they're single-sourced).

Vendor-marketing rule
---------------------
Vendor blogs are authoritative for their own PRODUCTS (versions, releases,
features) but are also marketing. A SECURITY/threat claim supported only by
vendor source(s) is FLAGGED for independent review — it needs a non-vendor
outlet before we treat it as verified.
"""
from __future__ import annotations

from datetime import date
import re

from retrieval.models import Source, Claim, VerifiedBundle
from config import load_settings
from .dedupe import dedupe
from .recency import filter_recent
from .currency import check_currency
from .claims import extract_claims, llm_extract


_STOP = {"the", "this", "that", "these", "those", "with", "from", "have", "which",
         "their", "there", "than", "then", "they", "such", "into", "your", "about",
         "according", "data", "more", "also", "over", "between", "based", "shows",
         "average", "annual", "annually", "typically", "often", "many", "some"}


def _sig_tokens(text: str) -> set[str]:
    toks = re.findall(r"[a-z0-9%]+", text.lower())
    return {t for t in toks if len(t) > 3 and t not in _STOP}


def _claims_match(a: Claim, b: Claim) -> bool:
    """Do two claims assert 'the same thing' across different sources?"""
    if a.claim_type != b.claim_type:
        return False
    if a.claim_type == "stat" and not (set(a.numbers) & set(b.numbers)):
        return False        # stats must share a figure to corroborate
    ta, tb = _sig_tokens(a.text), _sig_tokens(b.text)
    if not ta or not tb:
        return False
    jaccard = len(ta & tb) / len(ta | tb)
    return jaccard >= 0.25


def _supporting(claim: Claim, claims: list[Claim]) -> list[Claim]:
    """All claims (including itself) that assert the same thing."""
    return [c for c in claims if c is claim or _claims_match(claim, c)]


def _corroborate(claims: list[Claim], min_domains: int) -> None:
    """Pairwise corroboration: set corroborated_by + status on each claim."""
    for c in claims:
        support = _supporting(c, claims)
        domains = {s.source_domain for s in support}
        c.corroborated_by = sorted(domains - {c.source_domain})
        c.status = "verified" if len(domains) >= min_domains else "unverified"


def _apply_vendor_rule(claims: list[Claim]) -> None:
    """Flag security claims supported only by vendor sources."""
    for c in claims:
        if not c.is_security_claim:
            continue
        support = _supporting(c, claims)
        if not any(not s.is_vendor_source for s in support):
            c.status = "flagged"
            c.note = ("security claim supported only by vendor source(s); "
                      "needs independent corroboration before use")


def rebuild_bundle(topic: str, region: str | None, sources: list[Source],
                   today: date | None = None) -> VerifiedBundle:
    """Rebuild a VerifiedBundle from an exact, already-curated source list —
    the section-rewrite path, where sources are re-fetched from an article's
    STORED references and their order must be preserved so [S#] tags line up
    with the citations already in the body.

    Deliberately skips dedupe/recency (verify()'s steps 1-2): those can DROP
    or reorder sources, which would silently shift every downstream tag and
    corrupt citation_integrity for the untouched sections. Every other
    assessment step (currency check, claim extraction, corroboration, vendor
    rule, reviewer warnings) runs identically to verify() — the QA harness
    sees the same strength of bundle either way."""
    bundle = VerifiedBundle(topic=topic, region=region)
    bundle.sources = list(sources)
    if not bundle.sources:
        bundle.warnings.append("No usable sources survived filtering.")
        return bundle
    _assess(bundle, today or date.today())
    return bundle


def _assess(bundle: VerifiedBundle, today: date, use_llm: bool = False) -> None:
    """Steps 3-7 of the pipeline (currency -> claims -> corroboration ->
    vendor rule -> summary warnings), shared verbatim by verify() and
    rebuild_bundle() so the two can't drift."""
    settings = load_settings()

    # 3. currency guardrail
    bundle.warnings.extend(check_currency(bundle.sources, bundle.topic, today=today))

    # 4. claim extraction (LLM optional, heuristic fallback)
    all_claims: list[Claim] = []
    for s in bundle.sources:
        claims = (llm_extract(s) if use_llm else None) or extract_claims(s)
        all_claims.extend(claims)

    # 5. corroboration + 6. vendor rule
    min_domains = settings["corroboration"]["min_independent_domains"]
    _corroborate(all_claims, min_domains)
    _apply_vendor_rule(all_claims)
    bundle.claims = all_claims

    # 7. summary warnings for the reviewer
    single = [c for c in all_claims if c.status == "unverified" and c.claim_type == "stat"]
    if single:
        bundle.warnings.append(
            f"{len(single)} statistic(s) are single-sourced — corroborate or attribute explicitly."
        )
    if bundle.flagged_claims:
        bundle.warnings.append(
            f"{len(bundle.flagged_claims)} security claim(s) flagged as vendor-only — review before use."
        )


def verify(topic: str, region: str | None, sources: list[Source],
           dropped_in: list[dict] | None = None,
           today: date | None = None, use_llm: bool = False,
           vendor_name: str | None = None) -> VerifiedBundle:
    today = today or date.today()
    bundle = VerifiedBundle(topic=topic, region=region)
    bundle.dropped.extend(dropped_in or [])

    # 1. dedupe -> 2. recency
    sources, d1 = dedupe(sources, vendor_name=vendor_name)
    sources, d2 = filter_recent(sources, topic, today=today,
                                vendor_name=vendor_name)
    bundle.dropped.extend(d1 + d2)
    bundle.sources = sources

    if not sources:
        bundle.warnings.append("No usable sources survived filtering.")
        return bundle

    # 3-7: currency -> claims -> corroboration -> vendor rule -> warnings
    # (shared with rebuild_bundle above).
    _assess(bundle, today, use_llm=use_llm)

    return bundle
