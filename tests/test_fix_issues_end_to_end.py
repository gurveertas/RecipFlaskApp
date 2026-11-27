"""Fix selected, driven end to end with a stubbed model.

Everything else is real: the deterministic pre-pass, the patch parser and its
anchor check, _patched_package, both gates, and the storage at the end. Only
two things are replaced — the provider call, and the live SERP lookup, which
would otherwise spend a Serper credit per run.

The case that matters is the one that shipped broken: the pre-pass corrects a
non-body field, the model's patch touches only the body, and the correction
has to survive. It used to be computed, scored, reported to the model as
already clean, and then rebuilt from the article — leaving the check failing
and the panel claiming it "did not clear last time".
"""
import json

import pytest

from config.db import (Article, ArticleStatus, ContentType, SearchIntent,
                       SourceSnapshot, Topic, TopicPriority, Vendor,
                       get_session)
from webapp.pipeline import fix_issues

SOURCE_URL = "https://www.checkpoint.com/fixtures/e2e-source"
SOURCE_TEXT = ("Check Point publishes the CCTE exam under code 156-836. "
               "Organisations running R82 gateways use it for troubleshooting "
               "certification. The exam lasts 90 minutes.")

# Block 2 is the one the fake model edits. Block 1 carries the keyword so the
# opening keeps satisfying direct_answer_first.
BODY = """Check point troubleshooting is the subject of this guide, and this
opening paragraph answers the question directly in fewer than seventy words so
the standard's direct-answer pillar stays satisfied throughout the run [S1].

## What does the exam cover?

The exam covers troubleshooting workflows end to end [S1].

Last updated: 1 January 2026"""


@pytest.fixture
def article():
    """A written draft whose META DESCRIPTION carries a US spelling.

    locale_spelling is the pre-pass's territory: deterministic, free, and
    outside the body — exactly the correction that used to be thrown away.
    """
    with get_session() as s:
        vendor = s.query(Vendor).filter_by(name="Check Point").first()
        s.add(SourceSnapshot(url=SOURCE_URL, domain="checkpoint.com",
                             title="CCTE", text=SOURCE_TEXT, tier=1,
                             is_vendor=True, vendor="Check Point"))
        topic = Topic(
            vendor_id=vendor.id, title="Check Point troubleshooting",
            keyword="check point troubleshooting",
            search_intent=next(iter(SearchIntent)),
            content_type=ContentType.CERTIFICATION_GUIDE,
            commercial_score=50, seo_score=50, ai_score=50, difficulty=50,
            vendor_alignment=50, opportunity_score=50,
            priority=next(iter(TopicPriority)), rationale="t", model="t")
        s.add(topic)
        s.flush()
        row = Article(
            topic_id=topic.id, vendor_id=vendor.id,
            title="Check Point Troubleshooting Guide 2026",
            slug="e2e-fix-issues", keyword="check point troubleshooting",
            brief={"title": "Check Point Troubleshooting Guide 2026",
                   "audience": "engineers", "keyword": "check point troubleshooting",
                   "format": "explainer", "outline": ["What does the exam cover?"],
                   "key_points": [], "internal_links": [], "faq": [],
                   "cta": "Explore our courses", "image_brief": ""},
            content=BODY,
            meta_title="Check Point Troubleshooting Guide 2026",
            # The US spelling the pre-pass will correct.
            meta_description="How organizations approach troubleshooting.",
            excerpt="", cta="Explore our courses",
            faq=[{"question": "How long is it?", "answer": "90 minutes."}],
            references=[{"tag": "S1", "url": SOURCE_URL, "title": "CCTE",
                         "domain": "checkpoint.com"}],
            word_count=len(BODY.split()), status=ArticleStatus.DRAFT)
        s.add(row)
        s.commit()
        ids = (row.id, topic.id)
    yield ids[0]
    with get_session() as s:
        s.delete(s.get(Article, ids[0]))
        s.commit()
        s.delete(s.get(Topic, ids[1]))
        snap = s.query(SourceSnapshot).filter_by(url=SOURCE_URL).first()
        if snap is not None:
            s.delete(snap)
        s.commit()


def _body_only_patch(_system, _user, **_kw):
    """A model that edits one body block and sets no fields — the normal case."""
    return json.dumps({
        "edits": [{
            "block": 3,
            "anchor": "The exam covers troubleshooting workflows",
            "action": "replace",
            "text": ("The exam covers troubleshooting workflows end to end, "
                     "including policy installation and connectivity faults "
                     "[S1]."),
            "fixes": "structure_length",
        }],
        "meta_title": None, "meta_description": None, "excerpt": None,
        "cta": None, "faq": None,
    }), "fake-model"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    # _serp_snapshot spends a Serper credit per call; the provider costs money
    # and determinism. Nothing else is stubbed.
    monkeypatch.setattr("webapp.pipeline._serp_snapshot", lambda keyword: None)


class TestAPrePassCorrectionSurvivesABodyOnlyPatch:
    def test_the_meta_description_keeps_its_correction(self, monkeypatch, article):
        monkeypatch.setattr("webapp.pipeline.complete_with_configured_provider",
                            _body_only_patch)
        with get_session() as s:
            fix_issues(s, s.get(Article, article), ["structure_length"], [])

        with get_session() as s:
            row = s.get(Article, article)
        # "organizations" -> "organisations", made by the pre-pass, in a field
        # the patch never mentioned.
        assert "organisations" in row.meta_description
        assert "organizations" not in row.meta_description

    def test_the_model_edit_still_lands(self, monkeypatch, article):
        monkeypatch.setattr("webapp.pipeline.complete_with_configured_provider",
                            _body_only_patch)
        with get_session() as s:
            fix_issues(s, s.get(Article, article), ["structure_length"], [])

        with get_session() as s:
            row = s.get(Article, article)
        assert "policy installation and connectivity faults" in row.content

    def test_blocks_the_patch_never_named_are_untouched(self, monkeypatch, article):
        monkeypatch.setattr("webapp.pipeline.complete_with_configured_provider",
                            _body_only_patch)
        with get_session() as s:
            fix_issues(s, s.get(Article, article), ["structure_length"], [])

        with get_session() as s:
            row = s.get(Article, article)
        assert "## What does the exam cover?" in row.content
        assert "the standard's direct-answer pillar" in row.content


class TestTheParserIsReallyInTheLoop:
    def test_an_edit_whose_anchor_names_another_block_is_discarded(
            self, monkeypatch, article):
        """The anchor check is what stops an off-by-one rewriting good prose."""
        def wrong_anchor(_system, _user, **_kw):
            return json.dumps({
                "edits": [{"block": 3, "anchor": "Check point troubleshooting is the subject",
                           "action": "replace", "text": "REPLACED WRONGLY.",
                           "fixes": "structure_length"}],
                "meta_title": None, "meta_description": None,
                "excerpt": None, "cta": None, "faq": None,
            }), "fake-model"

        monkeypatch.setattr("webapp.pipeline.complete_with_configured_provider",
                            wrong_anchor)
        with get_session() as s:
            before = s.get(Article, article).content
            # Every edit rejected leaves no usable patch, so the call falls
            # back to the whole-article path and fails there too rather than
            # reporting a fix it did not make.
            with pytest.raises(Exception):
                fix_issues(s, s.get(Article, article), ["structure_length"], [])

        with get_session() as s:
            row = s.get(Article, article)
        assert "REPLACED WRONGLY" not in row.content
        assert row.content == before

    def test_unparseable_output_does_not_leave_a_half_written_article(
            self, monkeypatch, article):
        monkeypatch.setattr("webapp.pipeline.complete_with_configured_provider",
                            lambda *_a, **_kw: ("not json at all", "fake-model"))
        with get_session() as s:
            before = s.get(Article, article).content
            with pytest.raises(Exception):
                fix_issues(s, s.get(Article, article), ["structure_length"], [])

        with get_session() as s:
            assert s.get(Article, article).content == before
