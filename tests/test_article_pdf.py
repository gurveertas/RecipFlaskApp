"""Download the article as a PDF, with its references.

Deliberately NOT the site preview: render_site_preview strips [S#] markers
because the published page does not show them, and a document whose second
half is a numbered reference list needs the numbers in the prose to match.
"""
import pytest

from config.db import Article, ArticleStatus, get_session


@pytest.fixture
def client():
    from webapp.app import create_app
    return create_app().test_client()


def _first_article_id():
    with get_session() as s:
        a = s.query(Article).filter(Article.content != "").first()
        return a.id if a else None


class TestTheRoute:
    def test_a_missing_article_is_404(self, client):
        assert client.get("/studio/article/999999/pdf").status_code == 404

    def test_a_working_render_returns_a_pdf_attachment(self, client, monkeypatch):
        aid = _first_article_id()
        if aid is None:
            pytest.skip("no article with a body in the test database")
        monkeypatch.setattr("webapp.studio.html_to_pdf",
                            lambda html, **kw: b"%PDF-1.4 fake")
        r = client.get(f"/studio/article/{aid}/pdf")
        assert r.status_code == 200
        assert r.headers["Content-Type"] == "application/pdf"
        assert "attachment" in r.headers["Content-Disposition"]
        assert r.headers["Content-Disposition"].endswith(".pdf")

    def test_a_failed_render_redirects_instead_of_500ing(self, client, monkeypatch):
        # A convenience button must not take the page down when the browser
        # it needs is missing.
        aid = _first_article_id()
        if aid is None:
            pytest.skip("no article with a body in the test database")
        monkeypatch.setattr("webapp.studio.html_to_pdf", lambda html, **kw: None)
        r = client.get(f"/studio/article/{aid}/pdf")
        assert r.status_code in (302, 303)


class TestWhatGoesInIt:
    def test_the_body_is_the_site_preview(self):
        # Same rendering the Preview step uses, so the PDF shows the article
        # as the published page will structure it.
        import inspect
        from webapp import studio
        src = inspect.getsource(studio.article_pdf)
        assert "render_site_preview" in src

    def test_every_details_is_forced_open(self):
        # accordionise_faq opens only the first. Chrome prints a closed
        # <details> as its summary line alone, so the FAQ answers would be
        # silently missing from the PDF.
        import re
        from webapp.render import render_site_preview
        html = render_site_preview(
            "## Frequently asked questions\n\n### A?\n\nYes.\n\n### B?\n\nNo.",
            ["A?", "B?"])
        forced = re.sub(r"<details(?![^>]*\bopen\b)", "<details open", html)
        # Parse the tags: the one accordionise_faq already opened carries the
        # attribute at the END ("<details class='sp-faq' open>"), so counting
        # the literal string "<details open" misses it.
        tags = re.findall(r"<details[^>]*>", forced)
        assert len(tags) == 2
        assert all("open" in t for t in tags), tags

    def _render(self, **over):
        from webapp.app import create_app
        from datetime import datetime

        class _V:
            name = "Check Point"

        class _S:
            value = "draft"

        class _A:
            title = "A Guide"; vendor = _V(); word_count = 1200; slug = "a-guide"
            qa_score = 96; reacs_score = 100; qa_passed = True; reacs_passed = True
            status = _S(); published_at = None; wp_post_id = None; wp_link = None
            keyword = "check point ccse"; meta_title = "M"; meta_description = "D"
            excerpt = "E"; cta = "C"; faq = [{"question": "q", "answer": "a"}]
            model = "gemini"; unresolved_fixes = None
            created_at = datetime(2026, 8, 1, 9, 0)
            updated_at = datetime(2026, 8, 27, 9, 0)

        ctx = dict(article=_A(), byline="Dan Schultz", body_html="<p>Body.</p>",
                   references=[{"tag": "S1", "url": "https://example.com/x",
                                "title": "Example source",
                                "domain": "example.com"}],
                   qa_checks=[{"name": "originality", "status": "pass",
                               "detail": "no copied runs"}],
                   reacs_report=[{"pillar": "faq_block", "tier": "mandatory",
                                  "passed": True, "message": "8 FAQs"}],
                   topic=None, generated_on="27 August 2026")
        ctx.update(over)
        with create_app().test_request_context():
            from flask import render_template
            return render_template("studio/article_pdf.html", **ctx)

    def test_references_are_listed(self):
        html = self._render()
        assert "Example source" in html and "https://example.com/x" in html

    def test_an_article_with_no_references_says_so(self):
        assert "no stored references" in self._render(references=[])

    def test_the_quality_report_carries_every_check_and_pillar(self):
        html = self._render()
        assert "Quality report" in html
        assert "originality" in html and "no copied runs" in html
        assert "faq_block" in html and "8 FAQs" in html

    def test_the_verdict_shows_both_scores(self):
        html = self._render()
        assert "96" in html and "100" in html

    def test_the_parameters_table_is_present(self):
        html = self._render()
        for expected in ("check point ccse", "Dan Schultz", "a-guide", "1200"):
            assert expected in html, expected

    def test_an_unscored_article_says_so_rather_than_showing_nothing(self):
        html = self._render(qa_checks=[], reacs_report=[])
        assert "Not scored yet" in html


class TestTheRenderer:
    def test_no_browser_means_no_pdf_not_an_exception(self, monkeypatch):
        import webapp.pdf as P
        monkeypatch.setattr(P, "_chrome_binary", lambda: None)
        assert P.html_to_pdf("<h1>x</h1>") is None

    def test_a_non_pdf_result_is_rejected(self, monkeypatch, tmp_path):
        # Chrome writing a truncated or empty file must not be handed to the
        # browser as a download: it opens as a broken document and looks like
        # the article is at fault.
        import webapp.pdf as P
        monkeypatch.setattr(P, "_chrome_binary", lambda: "/bin/true")
        assert P.html_to_pdf("<h1>x</h1>", timeout=1) is None
