"""A vendor's own page is not disqualified by its age.

Palo Alto's page for the Security Service Edge Engineer credential reports a
published date of 2020-01-01 and was dropped at 2,430 days against a 540-day
cap. It is the single most authoritative source for an article about that
credential — the page that names it — and the pipeline threw it away for being
old, while keeping a competitor's comparison page and a NIST PDF.

The date is almost certainly a template default rather than a real
publication date, which is the second reason it has no business deciding
anything.
"""
from datetime import date

import pytest

from retrieval.models import Source
from verification.recency import filter_recent

TODAY = date(2026, 8, 27)


def src(domain, published, *, is_vendor=False, vendor=None):
    return Source(url=f"https://{domain}/x", domain=domain, title="t",
                  text="body", published=published, tier=1,
                  is_vendor=is_vendor, vendor=vendor)


TOPIC = "Mastering Prisma Access SSE: Configuration and Deployment"


class TestTheArticlesOwnVendorIsExempt:
    def test_an_old_vendor_page_survives(self):
        s = src("www.paloaltonetworks.com", date(2020, 1, 1),
                is_vendor=True, vendor="Palo Alto Networks")
        kept, dropped = filter_recent([s], TOPIC, today=TODAY,
                                      vendor_name="Palo Alto Networks")
        assert kept == [s] and dropped == []

    def test_the_same_page_is_still_dropped_without_a_vendor_named(self):
        # The exemption is opt-in. A caller that does not know whose article
        # this is gets the old behaviour, not a silently weakened cap.
        s = src("www.paloaltonetworks.com", date(2020, 1, 1),
                is_vendor=True, vendor="Palo Alto Networks")
        kept, dropped = filter_recent([s], TOPIC, today=TODAY)
        assert kept == [] and "stale" in dropped[0]["reason"]


class TestTheCapStillBites:
    def test_a_stale_competitor_page_is_still_dropped(self):
        # The whole point of scoping to THIS vendor. "Vendor" is not a synonym
        # for "relevant" — a Fortinet page in a Palo Alto article is exactly
        # what the cap should catch, and it is what article 5 cited as [S1].
        s = src("www.fortinet.com", date(2020, 1, 1),
                is_vendor=True, vendor="Fortinet")
        kept, dropped = filter_recent([s], TOPIC, today=TODAY,
                                      vendor_name="Palo Alto Networks")
        assert kept == [] and "stale" in dropped[0]["reason"]

    def test_a_stale_third_party_page_is_still_dropped(self):
        s = src("www.csoonline.com", date(2019, 5, 1))
        kept, dropped = filter_recent([s], TOPIC, today=TODAY,
                                      vendor_name="Palo Alto Networks")
        assert kept == [] and dropped

    def test_a_recent_page_is_untouched(self):
        s = src("www.csoonline.com", date(2026, 6, 1))
        kept, _ = filter_recent([s], TOPIC, today=TODAY,
                                vendor_name="Palo Alto Networks")
        assert kept == [s]

    def test_an_undated_page_is_still_kept(self):
        # The datasheet PDF has no date. Dropping undated sources would
        # silently discard the one source that actually names the credential.
        s = src("www.paloaltonetworks.com", None)
        kept, _ = filter_recent([s], TOPIC, today=TODAY)
        assert kept == [s]


class TestVendorMatching:
    @pytest.mark.parametrize("named", ["palo alto networks", "  Palo Alto Networks  "])
    def test_the_match_is_case_and_space_insensitive(self, named):
        s = src("www.paloaltonetworks.com", date(2020, 1, 1),
                is_vendor=True, vendor="Palo Alto Networks")
        kept, _ = filter_recent([s], TOPIC, today=TODAY, vendor_name=named)
        assert kept == [s]

    def test_a_non_vendor_domain_gets_no_exemption(self):
        # pearsonvue.com delivers the exam but is not Palo Alto, and
        # fetch_source records it as is_vendor=False with vendor=None.
        s = src("www.pearsonvue.com", date(2019, 1, 1))
        kept, dropped = filter_recent([s], TOPIC, today=TODAY,
                                      vendor_name="Palo Alto Networks")
        assert kept == [] and dropped


class TestVendorDocumentsAreNotEachOthersDuplicates:
    """dedupe collapsed the exam datasheet into the credential page.

    Both live on paloaltonetworks.com, so the same-domain rule treated them as
    two copies of one story. They are not: one names the credential, the other
    carries the exam domains and their weightings — and the article, having
    been handed neither in full, fabricated the weightings table.

    The rule exists so corroboration counts INDEPENDENT OUTLETS. That
    reasoning is about corroboration, and _corroborate counts distinct
    domains, so letting a second vendor page through cannot inflate anything.
    """
    from verification.dedupe import dedupe as _d

    def _pan(self, name, text, published=None):
        return Source(url=f"https://www.paloaltonetworks.com/{name}",
                      domain="www.paloaltonetworks.com", title=name,
                      text=text, published=published, tier=1,
                      is_vendor=True, vendor="Palo Alto Networks")

    def test_two_different_vendor_documents_both_survive(self):
        from verification.dedupe import dedupe
        pages = [self._pan("sse-engineer", "the credential is called X" * 20),
                 self._pan("sse-datasheet", "domain weightings 25% 30%" * 20)]
        kept, _ = dedupe(pages, vendor_name="Palo Alto Networks")
        assert len(kept) == 2

    def test_without_a_vendor_named_the_old_collapse_still_happens(self):
        from verification.dedupe import dedupe
        pages = [self._pan("sse-engineer", "a" * 400),
                 self._pan("sse-datasheet", "b" * 400)]
        kept, dropped = dedupe(pages)
        assert len(kept) == 1 and "same domain" in dropped[0]["reason"]

    def test_a_competitor_domain_still_collapses(self):
        from verification.dedupe import dedupe
        two = [Source(url=f"https://www.fortinet.com/{n}", domain="www.fortinet.com",
                      title=n, text="x" * 400, tier=1, is_vendor=True,
                      vendor="Fortinet") for n in ("a", "b")]
        kept, _ = dedupe(two, vendor_name="Palo Alto Networks")
        assert len(kept) == 1

    def test_a_near_duplicate_title_is_still_collapsed_for_the_vendor(self):
        # Syndication is real on vendor sites; only the same-DOMAIN rule is
        # relaxed, not the title-similarity one.
        from verification.dedupe import dedupe
        pages = [self._pan("SSE Engineer Certification", "a" * 400),
                 self._pan("SSE Engineer Certification", "b" * 400)]
        kept, _ = dedupe(pages, vendor_name="Palo Alto Networks")
        assert len(kept) == 1

    def test_one_vendor_cannot_crowd_out_everything(self):
        from verification.dedupe import dedupe, MAX_OWN_VENDOR_SOURCES
        pages = [self._pan(f"doc-{i}", f"content {i} " * 80) for i in range(8)]
        kept, _ = dedupe(pages, vendor_name="Palo Alto Networks")
        assert len(kept) == MAX_OWN_VENDOR_SOURCES

    def test_corroboration_still_counts_one_domain_once(self):
        # The safety property the relaxation rests on.
        from retrieval.models import Claim
        from verification.verify import _corroborate
        claims = [Claim(text="The exam has four domains.", claim_type="fact",
                        source_url=f"https://www.paloaltonetworks.com/{n}",
                        source_domain="www.paloaltonetworks.com",
                        source_tier=1, is_vendor_source=True)
                  for n in ("a", "b")]
        _corroborate(claims, min_domains=2)
        assert all(c.status == "unverified" for c in claims)
        assert all(c.corroborated_by == [] for c in claims)
