"""Quick drafts can be pushed to WordPress as drafts, on their own path.

Deliberately not webapp/pipeline.py's push_to_wordpress. That one works from
an Article row — re-scores from stored sources, checks REACS, records
wp_post_id, and looks up the existing post to refuse a second push. A Quick
draft is never persisted, which is the promise the page makes, so none of
those exist here.
"""
import pytest

from synthesis.models import Draft, QAReport
from webapp import quick_drafts as QD
from webapp import quick_publish as QP


def draft_of(**kw):
    base = dict(topic="Cycling tips", region=None,
                title="Essential Cycling Tips", slug="essential-cycling-tips",
                meta_description="Cycling tips for new riders.",
                body_markdown="## Fit\n\nStart with a bike that fits [S1].\n",
                faq=[{"q": "How often should I service it?", "a": "Every six months."}])
    base.update(kw)
    return Draft(**base)


def report_with(*failures):
    r = QAReport()
    r.add("structure_length", "pass", "fine")
    for name in failures:
        r.add(name, "fail", "broken")
    return r


@pytest.fixture(autouse=True)
def _clean():
    QD._drafts.clear()
    yield
    QD._drafts.clear()


class TestTheFaqKeysAreTranslated:
    """The ad-hoc pipeline emits {"q","a"} (synthesis/style.py) while
    build_post_payload and build_schema read {"question","answer"}. Passing it
    through untouched produces a post whose FAQ accordion has no questions and
    whose FAQPage schema is empty — silently."""

    def test_q_and_a_become_question_and_answer(self):
        payload = QP.quick_payload(draft_of(), ["cycling tips"])
        assert "How often should I service it?" in payload["content"]

    def test_the_schema_is_not_empty(self):
        payload = QP.quick_payload(draft_of(), ["cycling tips"])
        assert "FAQPage" in payload["content"]

    def test_an_incomplete_faq_entry_is_dropped(self):
        payload = QP.quick_payload(draft_of(faq=[{"q": "Only a question?"}]),
                                   ["k"])
        assert "Only a question?" not in payload["content"]


class TestThePayload:
    def test_it_is_always_a_draft(self):
        # build_post_payload hard-codes this and takes no parameter for it.
        assert QP.quick_payload(draft_of(), ["k"])["status"] == "draft"

    def test_no_category_and_no_byline(self):
        # A Quick draft has no vendor and no instructor roster.
        payload = QP.quick_payload(draft_of(), ["k"])
        assert payload["categories"] == []
        assert "Technically reviewed by" not in payload["content"]

    def test_citation_markers_do_not_reach_the_post(self):
        # Same reader boundary as Preview and the PDF.
        assert "[S1]" not in QP.quick_payload(draft_of(), ["k"])["content"]

    def test_a_missing_slug_is_derived_from_the_title(self):
        payload = QP.quick_payload(draft_of(slug=""), ["k"])
        assert payload["slug"] == "essential-cycling-tips"


class TestTheGate:
    def _push(self, monkeypatch, report, created=None):
        monkeypatch.setattr("publish.wordpress_configured", lambda: True)
        monkeypatch.setattr("publish.create_draft_post",
                            lambda payload: created or {"id": 7, "link": "https://x/p"})
        monkeypatch.setattr("publish.set_rankmath_meta",
                            lambda *a, **k: None)
        did = QD.remember_draft(draft_of(), report, ["cycling tips"])
        return did

    def test_a_failing_draft_is_refused(self, monkeypatch):
        did = self._push(monkeypatch, report_with("grounding"))
        with pytest.raises(QP.QuickPublishError) as e:
            QP.push_quick_draft(did)
        assert "grounding" in str(e.value)

    def test_a_passing_draft_is_pushed(self, monkeypatch):
        did = self._push(monkeypatch, report_with())
        assert QP.push_quick_draft(did) == {"post_id": 7, "link": "https://x/p"}

    def test_pushing_twice_is_refused(self, monkeypatch):
        did = self._push(monkeypatch, report_with())
        QP.push_quick_draft(did)
        with pytest.raises(QP.QuickPublishError) as e:
            QP.push_quick_draft(did)
        assert "Already pushed" in str(e.value)

    def test_an_expired_draft_says_so(self):
        with pytest.raises(QP.QuickPublishError) as e:
            QP.push_quick_draft("no-such-id")
        assert "expired" in str(e.value)

    def test_unconfigured_wordpress_is_a_sentence_not_a_traceback(self, monkeypatch):
        monkeypatch.setattr("publish.wordpress_configured", lambda: False)
        did = QD.remember_draft(draft_of(), report_with(), ["k"])
        with pytest.raises(QP.QuickPublishError) as e:
            QP.push_quick_draft(did)
        assert "credentials" in str(e.value)

    def test_rank_math_failing_does_not_lose_the_post(self, monkeypatch):
        # Best-effort and after the post exists, same as the Studio path.
        monkeypatch.setattr("publish.wordpress_configured", lambda: True)
        monkeypatch.setattr("publish.create_draft_post",
                            lambda p: {"id": 9, "link": "https://x/9"})
        def boom(*a, **k):
            raise RuntimeError("rank math down")
        monkeypatch.setattr("publish.set_rankmath_meta", boom)
        did = QD.remember_draft(draft_of(), report_with(), ["k"])
        assert QP.push_quick_draft(did)["post_id"] == 9


class TestNothingIsPersisted:
    def test_the_draft_lives_only_in_memory(self):
        did = QD.remember_draft(draft_of(), report_with(), ["k"])
        assert QD.get_draft(did) is not None
        QD._drafts.clear()
        assert QD.get_draft(did) is None

    def test_it_does_not_reach_into_the_studio_path(self):
        # Separate implementation, tested on the IMPORTS rather than by
        # grepping the source — the docstring names push_to_wordpress to
        # explain what it is not, and a text search cannot tell prose from a
        # call.
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(QP))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
        assert not any("pipeline" in m for m in imported), imported
        # publish/ IS shared, and should be: it is transport, not policy.
        assert any(m == "publish" for m in imported), imported

    def test_the_studio_push_still_works_from_an_article(self):
        # The other half: nothing here changed the governed path.
        import inspect
        from webapp import pipeline
        assert "quick_publish" not in inspect.getsource(pipeline.push_to_wordpress)
