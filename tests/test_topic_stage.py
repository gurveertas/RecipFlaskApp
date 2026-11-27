"""The vendor topic list has to say what already exists for a topic.

Before this it could only say WHETHER something existed: "View brief" looked
identical for a draft holding a failing check and for an article already live
on rededucation.com.

The stages are the same five the vendor card counts, from one function, so the
two screens cannot describe the same article differently.
"""
from datetime import datetime

import pytest

from config.db import ArticleStatus
from webapp.studio import ARTICLE_STAGES, article_stage


class _A:
    def __init__(self, **kw):
        self.published_at = None
        self.wp_post_id = None
        self.status = ArticleStatus.DRAFT
        self.qa_passed = False
        self.reacs_passed = False
        self.__dict__.update(kw)


class TestTheStages:
    def test_a_brief_that_was_never_written(self):
        assert article_stage(_A(status=ArticleStatus.BRIEF)) == "brief"

    def test_a_draft_a_gate_is_holding(self):
        assert article_stage(_A(qa_passed=True, reacs_passed=False)) == "needs fixes"

    def test_a_draft_that_passes_both_gates(self):
        assert article_stage(_A(qa_passed=True, reacs_passed=True)) == "ready"

    def test_a_draft_sitting_in_wordpress(self):
        assert article_stage(_A(qa_passed=True, reacs_passed=True,
                                wp_post_id=42)) == "awaiting publish"

    def test_one_a_human_published(self):
        assert article_stage(_A(qa_passed=True, reacs_passed=True, wp_post_id=42,
                                published_at=datetime(2026, 8, 1))) == "live"

    def test_the_most_advanced_state_wins(self):
        # A live article is also pushed, and a pushed one is also QA-passed.
        # If order were wrong it would report as merely "ready".
        a = _A(qa_passed=True, reacs_passed=True, wp_post_id=1,
               published_at=datetime(2026, 8, 1))
        assert article_stage(a) == "live"

    @pytest.mark.parametrize("a", [
        _A(status=ArticleStatus.BRIEF),
        _A(qa_passed=True, reacs_passed=True),
        _A(wp_post_id=1),
        _A(published_at=datetime(2026, 8, 1)),
    ])
    def test_every_article_lands_in_a_known_stage(self, a):
        # The card's buckets only partition the total if this is exhaustive.
        assert article_stage(a) in ARTICLE_STAGES


class TestTheCardAndTheListAgree:
    def test_the_card_counts_through_the_same_function(self):
        # Two implementations of "is this ready" would eventually disagree on
        # the same screen.
        import inspect
        from webapp import studio
        src = inspect.getsource(studio.vendor_step)
        assert "article_stage(" in src
        assert "qa_passed and a.reacs_passed" not in src

    def test_the_topic_route_supplies_a_stage(self):
        import inspect
        from webapp import studio
        assert "article_stage(article)" in inspect.getsource(
            studio.topics_for_vendor)


class TestThePageRenders:
    @pytest.fixture
    def client(self):
        from webapp.app import create_app
        return create_app().test_client()

    def test_a_vendor_with_topics_renders(self, client):
        from config.db import Vendor, get_session
        with get_session() as s:
            v = s.query(Vendor).first()
            vid = v.id if v else None
        if vid is None:
            pytest.skip("no vendors in the test database")
        assert client.get(f"/studio/vendor/{vid}/topics").status_code == 200
