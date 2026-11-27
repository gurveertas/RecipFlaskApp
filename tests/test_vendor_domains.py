"""The vendor allowlist decides what a certification article can be grounded in.

V11 (vendor_grounding) fails a certification guide that cites no page from the
vendor whose exam it describes. That check is only fair if every vendor's own
site is actually reachable — a vendor whose domains are wrong would fail every
article it could ever produce, with no way for the writer to fix it.

Two ways that went wrong, both caught here.
"""
from config.db import (AllowlistDomain, DomainCategory, Vendor, get_session)


def _vendor_domains():
    """{vendor name: {domain}}. Closes its session — a leaked one per test
    exhausts the connection pool and the suite dies on a timeout, not on
    anything to do with the assertions."""
    with get_session() as s:
        names = {v.id: v.name for v in s.query(Vendor).all()}
        out: dict[str, set[str]] = {}
        for d in s.query(AllowlistDomain).filter_by(
                category=DomainCategory.VENDOR).all():
            if d.vendor_id:
                out.setdefault(names[d.vendor_id], set()).add(d.domain.lower())
        return out


class TestApexDomains:
    def test_check_point_can_reach_its_own_certification_pages(self):
        # It held research.checkpoint.com and blog.checkpoint.com — two
        # research/marketing blogs — but not checkpoint.com, where the
        # certification and SK pages live. Both Check Point certification
        # guides on the estate were written without one page from Check Point.
        assert "checkpoint.com" in _vendor_domains()["Check Point"]

    def test_aws_is_not_given_the_amazon_retail_store(self):
        # aws.amazon.com's apex is amazon.com, which is a different company's
        # site. An unguarded version of the apex migration added it; the brand
        # label has to match the vendor name before an apex is inherited.
        assert "amazon.com" not in _vendor_domains()["AWS"]

    def test_a_vendor_with_two_brands_is_left_alone(self):
        # Avaya has avaya.com and avaya-learning.com. There is no single apex
        # to infer, and guessing one would silently drop the other.
        doms = _vendor_domains()["Avaya"]
        assert {"avaya.com", "avaya-learning.com"} <= doms

    def test_every_vendor_has_at_least_one_domain(self):
        # A vendor with none switches V11 off entirely (the QaKnowledge
        # contract), so its certification articles go ungraded rather than
        # failing. That is the safe direction, but it should never happen
        # silently — this is the tripwire.
        have = _vendor_domains()
        with get_session() as s:
            missing = sorted(v.name for v in s.query(Vendor).all()
                             if v.name not in have)
        assert missing == [], f"vendors with no domain on record: {missing}"


class TestScopingUsesTheKnownVendor:
    def test_a_certification_acronym_alone_cannot_scope_the_search(self):
        # The bug that caused the fabrication: domains_for_topic worked the
        # vendor out by string-matching the topic, and a certification is
        # named by its acronym. "PCNSE certification" contains no Palo Alto
        # alias, so the search fell through to every vendor's domains and
        # competitors outranked the right one.
        from config import load_allowlist
        allow = load_allowlist()
        guessed = allow.domains_for_topic("PCNSE certification")
        told = allow.domains_for_topic("PCNSE certification",
                                       "Palo Alto Networks")
        assert "fortinet.com" in guessed
        assert "fortinet.com" not in told
        assert "paloaltonetworks.com" in told

    def test_an_explicit_vendor_beats_a_name_in_the_text(self):
        # A Palo Alto article may legitimately mention a competitor in its
        # title. The vendor on the topic row is the authority, not the prose.
        from config import load_allowlist
        doms = load_allowlist().domains_for_topic(
            "Palo Alto vs Fortinet firewalls", "Palo Alto Networks")
        assert "fortinet.com" not in doms

    def test_a_vendor_we_hold_no_domains_for_keeps_only_neutral_sources(self):
        # Falling back to the string match would search every OTHER vendor
        # instead, which is worse than not scoping at all.
        from config import load_allowlist
        doms = load_allowlist().domains_for_topic("something", "Nonexistent Co")
        assert not any(d in doms for d in
                       ("fortinet.com", "cisco.com", "paloaltonetworks.com"))


class TestIdiraParentVendorDomains:

    def test_idira_research_includes_parent_vendor(self):
        from config import load_allowlist

        allow = load_allowlist()
        domains = allow.domains_for_topic(
            "IDIRA PAM installation", "IDIRA"
        )

        assert "cyberark.com" in domains
        assert "paloaltonetworks.com" in domains
        assert "fortinet.com" not in domains

    def test_palo_alto_research_remains_separate(self):
        from config import load_allowlist

        domains = load_allowlist().domains_for_topic(
            "PCNSE certification", "Palo Alto Networks"
        )

        assert "paloaltonetworks.com" in domains
        assert "cyberark.com" not in domains

class TestIdiraQaKnowledge:

    def test_idira_parent_domains_are_not_competitors(self):
        from types import SimpleNamespace
        from config.db import get_session
        from webapp.pipeline import _qa_knowledge

        with get_session() as session:
            article = SimpleNamespace(
                id=-1,
                vendor_id=12,
                vendor=SimpleNamespace(name="IDIRA"),
                topic=None
            )

            kb = _qa_knowledge(session, article)

            assert kb.vendor_name == "IDIRA"
            assert "cyberark.com" in kb.vendor_domains
            assert "paloaltonetworks.com" in kb.vendor_domains
            assert "unit42.paloaltonetworks.com" in kb.vendor_domains

            assert "paloaltonetworks.com" not in kb.interested_domains
            assert "unit42.paloaltonetworks.com" not in kb.interested_domains
