"""Editing the article by hand, when the automated fixes cannot do it.

Fix selected is one model call that can decline, and a section rewrite moves
prose nobody asked it to touch. This is the escape hatch, so its contract is
narrow on purpose: save exactly what was typed, write only the fields that
actually changed, and re-score both gates afterwards — an edit that breaks a
check must show up in the panel, not be refused at the form.
"""
from datetime import date

import pytest

from config.db import (Article, ArticleStatus, ContentType, SearchIntent,
                       Topic, TopicPriority, Vendor, get_session)
from webapp.pipeline import PipelineError, apply_manual_edit

BODY = ("Panorama is a firewall management platform.\n\n"
        "## What does Panorama do?\n\n"
        "It manages firewall policy centrally.\n\n"
        "Last updated: 1 January 2020")


@pytest.fixture
def article():
    """A written draft with NO stored references.

    Deliberate: rescore_article rebuilds the article's sources to re-run QA,
    and an article without references cannot, so the QA half reports that it
    did not run instead of a test reaching the network. REACS needs only the
    text, so the re-score half that matters here still happens.
    """
    with get_session() as s:
        vendor = s.query(Vendor).first()
        topic = Topic(
            vendor_id=vendor.id, title="Panorama management",
            keyword="panorama management",
            search_intent=next(iter(SearchIntent)),
            content_type=next(iter(ContentType)),
            commercial_score=50, seo_score=50, ai_score=50, difficulty=50,
            vendor_alignment=50, opportunity_score=50,
            priority=next(iter(TopicPriority)), rationale="test", model="test")
        s.add(topic)
        s.flush()
        row = Article(
            topic_id=topic.id, vendor_id=vendor.id,
            title="Panorama Management: Complete 2026 Guide",
            slug="manual-edit-test-article", keyword="panorama management",
            brief={"title": "Panorama Management: Complete 2026 Guide",
                   "outline": [], "key_points": []},
            content=BODY, meta_title="Panorama Management Guide",
            meta_description="A guide to Panorama.", references=[],
            faq=[{"question": "Is it hard?", "answer": "No."}],
            word_count=len(BODY.split()), status=ArticleStatus.DRAFT)
        s.add(row)
        s.commit()
        article_id = row.id
    yield article_id
    with get_session() as s:
        row = s.get(Article, article_id)
        topic_id = row.topic_id
        s.delete(row)
        s.commit()
        s.delete(s.get(Topic, topic_id))
        s.commit()


def _get(article_id):
    with get_session() as s:
        return s.get(Article, article_id)


class TestSavingTheBody:
    def test_the_text_is_stored_exactly_as_typed(self, article):
        with get_session() as s:
            row = s.get(Article, article)
            changed, _qa = apply_manual_edit(
                s, row, content=BODY.replace("centrally", "from one console"))
        assert changed == ["body"]
        assert "from one console" in _get(article).content

    def test_the_word_count_follows_the_new_body(self, article):
        with get_session() as s:
            row = s.get(Article, article)
            apply_manual_edit(s, row, content=BODY + "\n\nOne more sentence here.")
        assert _get(article).word_count > len(BODY.split())

    def test_the_freshness_line_is_brought_forward(self, article):
        # REACS reads this line, and a hand edit is as real as a model's.
        with get_session() as s:
            row = s.get(Article, article)
            apply_manual_edit(s, row, content=BODY.replace("platform", "system"))
        today = date.today()
        assert (f"Last updated: {today.day} {today.strftime('%B %Y')}"
                in _get(article).content)

    def test_both_gates_are_re_scored(self, article):
        with get_session() as s:
            row = s.get(Article, article)
            _changed, qa_rechecked = apply_manual_edit(
                s, row, content=BODY.replace("platform", "system"))
        # QA cannot run without rebuildable sources, and says so rather than
        # silently leaving the old verdict looking fresh.
        assert qa_rechecked is False
        assert _get(article).reacs_report is not None

    def test_an_empty_body_is_refused(self, article):
        with get_session() as s:
            row = s.get(Article, article)
            with pytest.raises(PipelineError):
                apply_manual_edit(s, row, content="   \n  ")
        assert _get(article).content.startswith("Panorama is a firewall")

    def test_only_a_written_draft_can_be_edited(self, article):
        with get_session() as s:
            row = s.get(Article, article)
            row.status = ArticleStatus.BRIEF
            s.commit()
            with pytest.raises(PipelineError):
                apply_manual_edit(s, row, content="Anything at all.")
            row.status = ArticleStatus.DRAFT
            s.commit()


class TestWhatCountsAsAChange:
    def test_resubmitting_the_same_text_changes_nothing(self, article):
        with get_session() as s:
            row = s.get(Article, article)
            changed, qa_rechecked = apply_manual_edit(
                s, row, title=row.title, content=BODY,
                meta_title=row.meta_title, meta_description=row.meta_description,
                faq=list(row.faq))
        assert changed == []
        assert qa_rechecked is False

    def test_a_meta_edit_leaves_the_freshness_line_alone(self, article):
        # Only a body change is a content change; moving the date because
        # someone retyped a meta description would be a lie to REACS.
        with get_session() as s:
            row = s.get(Article, article)
            changed, _qa = apply_manual_edit(
                s, row, content=BODY, meta_description="A better summary.")
        assert changed == ["meta description"]
        assert "Last updated: 1 January 2020" in _get(article).content

    def test_the_title_is_written_to_the_brief_as_well(self, article):
        # The Brief step edits both; two pages of one flow must not show two
        # different titles for the same article.
        with get_session() as s:
            row = s.get(Article, article)
            changed, _qa = apply_manual_edit(s, row, title="A Corrected Title")
        assert changed == ["title"]
        row = _get(article)
        assert row.title == "A Corrected Title"
        assert row.brief["title"] == "A Corrected Title"

    def test_every_edited_field_is_named_once(self, article):
        with get_session() as s:
            row = s.get(Article, article)
            changed, _qa = apply_manual_edit(
                s, row, title="Another Title", content=BODY + "\n\nMore.",
                meta_title="Another Meta Title")
        assert changed == ["body", "title", "meta title"]


class TestTheFaqEditor:
    def test_a_pair_missing_either_half_is_dropped(self, article):
        # REACS counts ANSWERS: a question with no answer would raise the
        # count while giving a reader nothing.
        with get_session() as s:
            row = s.get(Article, article)
            apply_manual_edit(s, row, faq=[
                {"question": "Is it hard?", "answer": "No."},
                {"question": "Unanswered?", "answer": "  "},
                {"question": "", "answer": "Orphan answer."},
                {"question": "", "answer": ""}])
        assert _get(article).faq == [{"question": "Is it hard?", "answer": "No."}]

    def test_a_new_pair_is_added(self, article):
        with get_session() as s:
            row = s.get(Article, article)
            changed, _qa = apply_manual_edit(s, row, faq=[
                {"question": "Is it hard?", "answer": "No."},
                {"question": "How long?", "answer": "Three days."}])
        assert changed == ["FAQ"]
        assert len(_get(article).faq) == 2

    def test_clearing_them_all_is_allowed_and_visible(self, article):
        # Not refused: the gate reports faq_depth, which is the point — the
        # editor sees the consequence instead of being blocked at the form.
        with get_session() as s:
            row = s.get(Article, article)
            apply_manual_edit(s, row, faq=[{"question": "", "answer": ""}])
        assert _get(article).faq == []


class TestTheRoute:
    @pytest.fixture
    def client(self):
        from webapp.app import create_app
        return create_app().test_client()

    def test_a_missing_article_is_404(self, client):
        assert client.post("/studio/article/999999/edit",
                           data={"content": "x"}).status_code == 404

    def test_a_posted_edit_lands_and_returns_to_the_editor(self, client, article):
        r = client.post(f"/studio/article/{article}/edit",
                        data={"content": BODY.replace("centrally", "in one place")})
        assert r.status_code in (302, 303)
        assert r.headers["Location"].endswith("#edit")
        assert "in one place" in _get(article).content

    def test_the_faq_is_only_touched_when_the_form_carried_it(self, client, article):
        client.post(f"/studio/article/{article}/edit",
                    data={"content": BODY.replace("centrally", "centrally.")})
        assert _get(article).faq == [{"question": "Is it hard?", "answer": "No."}]

    def test_the_form_is_on_the_article_page(self, client, article):
        html = client.get(f"/studio/article/{article}").get_data(as_text=True)
        assert 'name="content"' in html
        assert 'name="faq_question"' in html
