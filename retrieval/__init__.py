"""Retrieval stage: topic -> list of clean, allowlisted Source objects.

Heavy dependencies (requests, trafilatura) are imported lazily inside
`retrieve()` so that importing the data models for offline verification does
not require the live-fetch stack to be installed.
"""
from __future__ import annotations

from .models import Source, Claim, VerifiedBundle


def retrieve(topic: str, client=None, limit: int = 8,
             vendor_name: str | None = None,
             allow_open_web: bool = False):
    """Return (sources, dropped) for a topic.

    `dropped` records URLs that were found but couldn't be used, with reasons,
    so the run report is transparent about what was excluded.

    `vendor_name` scopes the search to that vendor's own domains. Pass it
    whenever the caller knows the vendor — see Allowlist.domains_for_topic for
    why inferring it from the topic text is not good enough.

    `allow_open_web` lets a topic that is NOT one of ours be researched from
    the open web. The allowlist is 61 IT-security and vendor-training domains,
    so "cycling tips" searched every one of them, matched nothing, and had
    every real result refused at the fetch gate — Live mode could only ever
    work for vendor topics. Quick draft passes it; Article Studio does not,
    and its governed path is unchanged.

    Even then, the allowlist is preferred: a topic that DOES name a known
    vendor still gets the scoped search, because for those topics the curated
    list is better than Google. The open web is the fallback for everything
    else, not a replacement.
    """
    from .search import SerperClient          # lazy: needs `requests`
    from .fetch import fetch_source           # lazy: needs `trafilatura`
    from config import load_allowlist

    import os
    from urllib.parse import urlparse
    from config.db import record_domain_sighting

    client = client or SerperClient()
    allow = load_allowlist()

    # Does the allowlist know anything about this topic? A vendor named by the
    # caller, or one recognised in the topic text, means the curated domains
    # are the right place to look. Nothing matched means they are the wrong
    # place entirely, and searching 54 firewall vendors for cycling tips costs
    # 7 Serper credits to return nothing.
    # ASK THE OPEN WEB FIRST, then decide. Whether a topic is "ours" is not a
    # string question: "CCSE certification" names no vendor anywhere in its
    # text, and inferring from the text sent it to the open web where the exam
    # dump farms live. What settles it is whether anything on the allowlist
    # ACTUALLY RANKS for the query.
    #
    # One credit, and it is a credit already being spent — the union channel
    # ran this search anyway. For a genuinely foreign topic it now also SAVES
    # the 4-7 credits of a scoped search across 54 firewall vendors that was
    # never going to match "cycling tips".
    open_hits = []
    if allow_open_web or os.getenv("RETRIEVAL_OPEN_SEARCH", "1").strip() != "0":
        open_hits = client.search_open(topic)

    def _allowlisted(hit) -> bool:
        return bool(allow.match(urlparse(hit.url).netloc.lower()))

    # Zero, not "few": every IT topic measured returns at least one
    # allowlisted result (1 of 8 at the low end), so a threshold above zero
    # would push real vendor topics off the list. Nothing at all is the honest
    # signal that the curated domains have nothing to say.
    # `open_hits` must be non-empty for its silence to mean anything. A failed
    # search, a missing key or a rate limit all return [], and treating that as
    # "the allowlist has nothing to say" skipped the scoped search entirely and
    # returned zero sources — when falling back to the curated domains is
    # exactly what should happen.
    off_list = (allow_open_web and bool(open_hits)
                and not any(_allowlisted(h) for h in open_hits))

    # SCOPE THE SEARCH FROM WHAT ACTUALLY RANKED. domains_for_topic works the
    # vendor out of the topic STRING, and a certification is named by its
    # acronym: "CCSE certification" matched no vendor, fell through to all 54
    # vendor domains, and cost 7 credits in site:-batches instead of 4. The
    # open search above has already told us whose pages rank for this query,
    # so use that when the caller did not name a vendor.
    #
    # Only when exactly ONE vendor's domains rank. Two or more means the topic
    # genuinely spans vendors (a comparison, say), and narrowing to whichever
    # happened to rank first would quietly drop the other.
    if vendor_name is None and not off_list:
        ranked = {allow.match(urlparse(h.url).netloc.lower()).get("vendor")
                  for h in open_hits if _allowlisted(h)}
        ranked.discard(None)
        if len(ranked) == 1:
            vendor_name = ranked.pop()

    hits = [] if off_list else client.search(
        topic, allow.domains_for_topic(topic, vendor_name), limit=limit * 2)

    # UNION CHANNEL. One unconstrained search alongside the scoped one, both
    # filtered through the same allowlist below, so this cannot widen what is
    # ingested — only what is CONSIDERED. The scoped search takes each batch of
    # 8 domains' own top 10 and never sees one global ranking, which is how
    # docs-cortex.paloaltonetworks.com gets beaten by a weaker page from a
    # busier batch. Costs 1 credit against the scoped search's 4.
    #
    # Scoped hits go first: they are the deliberately trusted set, and
    # everything after them is a supplement whose only claim is Google's
    # ranking. RETRIEVAL_OPEN_SEARCH=0 turns it off.
    seen = {h.url for h in hits}
    for hit in open_hits:
        if hit.url in seen:
            continue
        seen.add(hit.url)
        domain = urlparse(hit.url).netloc.lower()
        if off_list or _allowlisted(hit):
            hits.append(hit)
        else:
            # Not ingested — recorded, so the allowlist's gaps become visible
            # instead of silently costing us the source. Only for governed
            # topics: off the list there is no allowlist to have a gap in.
            record_domain_sighting(domain, hit.url, hit.title, topic)

    sources: list[Source] = []
    dropped: list[dict] = []

    for hit in hits:
        if len(sources) >= limit:
            break
        src = fetch_source(hit.url, allow_any_domain=off_list)
        if src is None:
            dropped.append({
                "url": hit.url,
                "reason": ("unfetchable / too thin / not a public http url"
                           if off_list else
                           "not allowlisted / unfetchable / too thin")})
            continue
        sources.append(src)

    return sources, dropped


__all__ = ["retrieve", "Source", "Claim", "VerifiedBundle"]
