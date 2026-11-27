"""Test fixtures for the REACS + QA gates.

THE ORDER OF THE FIRST FEW LINES MATTERS. config/db.py resolves APP_DB_PATH
and builds its engine at IMPORT time (`_engine = create_engine(...)` at module
level), so the environment variable has to be set before anything imports it
— and validate.py imports config transitively. pytest loads conftest before
any test module, so setting it here at module scope is early enough; moving
it into a fixture would not be.

The suite runs against a throwaway seeded database rather than the
developer's config/app.db, so a test can never depend on, or damage, real
article data.
"""
import os
import tempfile

_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="reacs-tests-"), "test.db")
os.environ["APP_DB_PATH"] = _TMP_DB

import pytest  # noqa: E402

from config.db import seed_if_empty  # noqa: E402

seed_if_empty()


# Certification versions as CHECK POINT PUBLISHES THEM (sk163417 and their
# Pearson VUE listing), matching what config/db.py seeds. Keyed on the exam
# code, and that keying IS the rule under test: CCSE is at R82 while CCME is
# at R81, so a vendor-level check would clear the exact sentence V1 exists to
# catch.
#
# Note CCME is "R81". The v1.3 standard document says "R81.X" and it is wrong
# — see test_does_not_accept_a_version_the_vendor_does_not_publish.
CERT_VERSIONS = {
    "CCME": ["R81"],                 # Check Point publishes R81, NOT "R81.X"
    "CCSE": ["R82", "R81.20"],
    "CCTE": ["R82", "R81.20"],
}

VENDOR_NAMES = ["Palo Alto Networks", "Check Point", "Extreme Networks",
                "Cisco", "F5", "Nutanix"]


@pytest.fixture
def cert_versions():
    return dict(CERT_VERSIONS)


@pytest.fixture
def vendors():
    return list(VENDOR_NAMES)


@pytest.fixture
def qa():
    """Run the QA harness over a body and return {check name: Check}.

    Keyword arguments map onto QaKnowledge fields, so a test names only the
    knowledge its rule actually needs — which doubles as documentation of
    what each rule depends on, and proves the "empty field switches the rule
    off" contract when a test omits one.
    """
    from retrieval.models import Claim, Source, VerifiedBundle
    from synthesis.models import Draft, Reference
    from synthesis.qa_rules import QaKnowledge
    from synthesis.validate import validate_draft

    def run(body="", *, title="A guide", meta="", seo=None, keywords=None,
            quotes=(), refs=(), vendor="Check Point",
            sources=(), faq=None,
            internal_links=None, allowed_link_urls=None, outline=None,
            entities=None, **knowledge):
        knowledge.setdefault("exam_codes", {})
        kb = QaKnowledge(vendor_name=vendor,
                         cert_versions=knowledge.pop("cert_versions",
                                                     dict(CERT_VERSIONS)),
                         **knowledge)
        bundle = VerifiedBundle(
            topic="t", region=None,
            sources=[Source(*source) for source in sources],
            claims=[Claim(text=q, claim_type="fact", source_url="u",
                          source_domain="d", source_tier=1,
                          is_vendor_source=True) for q in quotes])
        draft = Draft(topic="t", region=None, title=title, slug="s",
                      meta_description=meta, body_markdown=body,
                      faq=list(faq or []),
                      references=[Reference(*r) for r in refs])
        report = validate_draft(draft, bundle, keywords=keywords,
                                seo_title=seo, kb=kb,
                                internal_links=internal_links,
                                allowed_link_urls=allowed_link_urls,
                                outline=outline, entities=entities)
        return {c.name: c for c in report.checks}

    return run


@pytest.fixture
def reacs():
    """Score a body against the REACS pillars and return {pillar: ReacsCheck}."""
    from synthesis.reacs import compute_reacs

    def run(content="", *, title="t", keyword="k", faq=None, has_byline=True,
            entities=(), meta_title="", cta_phrases=None):
        report = compute_reacs(content=content, title=title, keyword=keyword,
                               faq=list(faq or []), has_byline=has_byline,
                               entities=list(entities), meta_title=meta_title,
                               cta_phrases=cta_phrases)
        return {c.pillar: c for c in report.checks}

    return run
