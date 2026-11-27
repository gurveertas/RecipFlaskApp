"""Core data models shared across retrieval and verification.

These dataclasses are the contract between pipeline stages. The synthesis
module (built later) will consume a `VerifiedBundle` and nothing else, so the
provenance captured here is what ends up defending every published claim.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, date, timezone
from typing import Optional
import json


# A source found on the open web, outside the curated allowlist. Tier 1 is
# authoritative and 2 established press; 3 says nobody vetted this. Lives here
# rather than in fetch.py because it is a property of a Source, and because
# both the prompt builder and the QA harness need to reason about it without
# importing the fetcher.
OPEN_WEB_TIER = 3


@dataclass
class Source:
    """A single retrieved article from a trusted domain."""
    url: str
    domain: str
    title: str
    text: str
    published: Optional[date] = None
    tier: int = 2                       # 1 = authoritative, 2 = established press, 3 = open web
    is_vendor: bool = False             # vendor's own blog/newsroom (authoritative
                                        # for its products, but also marketing)
    vendor: Optional[str] = None        # e.g. "Palo Alto Networks"

    def age_days(self, today: Optional[date] = None) -> Optional[int]:
        if self.published is None:
            return None
        return ((today or date.today()) - self.published).days


@dataclass
class Claim:
    """A factual assertion pulled from a source, tagged with its origin."""
    text: str
    claim_type: str                     # "stat" | "quote" | "fact"
    source_url: str
    source_domain: str
    source_tier: int
    is_vendor_source: bool
    numbers: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    is_security_claim: bool = False     # threat / vuln / breach / CVE etc.

    # populated during verification
    corroborated_by: list[str] = field(default_factory=list)  # other domains
    status: str = "unverified"          # "verified" | "unverified" | "flagged"
    note: str = ""


@dataclass
class VerifiedBundle:
    """Everything synthesis needs: verified sources, tagged claims, warnings."""
    topic: str
    region: Optional[str]
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    sources: list[Source] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)   # {url, reason}
    claims: list[Claim] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_json(self, indent: int = 2) -> str:
        def _default(o):
            if isinstance(o, (date, datetime)):
                return o.isoformat()
            raise TypeError(f"not serialisable: {type(o)}")
        return json.dumps(asdict(self), indent=indent, default=_default)

    def is_our_subject(self) -> bool:
        """Did anything on the curated allowlist have something to say?

        The allowlist is 61 IT-security and vendor-training domains, so this
        is a workable proxy for "is this topic cyber security". It is decided
        by what actually RANKED and fetched, not by matching words in the
        topic: "CCSE certification" names no vendor anywhere in its text, and
        a string test would call it foreign.

        Used to decide whether a Red Education course CTA belongs in the
        article at all. An Article Studio bundle can only ever contain
        allowlisted sources — its retrieval refuses everything else — so this
        is always True there and Studio is unaffected.
        """
        return any(s.tier < OPEN_WEB_TIER for s in self.sources)

    @property
    def verified_claims(self) -> list[Claim]:
        return [c for c in self.claims if c.status == "verified"]

    @property
    def flagged_claims(self) -> list[Claim]:
        return [c for c in self.claims if c.status == "flagged"]
