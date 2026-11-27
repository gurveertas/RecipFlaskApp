"""A check the fix button cannot clear must not be offered.

vendor_grounding reads the article's SOURCES, not its text, and a targeted fix
rebuilds the bundle from the references the article already has. With no
reference on the vendor's own domain, no wording satisfies it: ticking it
spends a model call and changes nothing. It stays offered when a vendor source
is there and was simply not cited, which a rewrite CAN fix — so the lock is
per-article, not a name on a list.
"""
import pytest
from sqlalchemy import select

from config.db import (AllowlistDomain, Article, ArticleStatus, Certification,
                       CertificationStatus, CertificationVersion, ContentType,
                       DomainCategory, SearchIntent, Topic, TopicPriority,
                       get_session)
from webapp.pipeline import PipelineError, fix_issues, unfixable_checks

STATIC = {"instructor_byline", "single_search_intent", "serp_competitiveness"}
OTHER_REF = {"tag": "S1", "url": "https://example.com/a", "title": "A",
             "domain": "example.com"}


def _make(session, content_type, refs):
    row = session.scalars(select(AllowlistDomain).where(
        AllowlistDomain.category == DomainCategory.VENDOR,
        AllowlistDomain.vendor_id.isnot(None))).first()
    topic = Topic(
        vendor_id=row.vendor_id, title="Lock test", keyword="lock test",
        search_intent=next(iter(SearchIntent)), content_type=content_type,
        commercial_score=50, seo_score=50, ai_score=50, difficulty=50,
        vendor_alignment=50, opportunity_score=50,
        priority=next(iter(TopicPriority)), rationale="t", model="t")
    session.add(topic)
    session.flush()
    article = Article(
        topic_id=topic.id, vendor_id=row.vendor_id, title="Guide",
        slug="unfixable-lock-test", keyword="lock test",
        brief={"title": "Guide", "outline": [], "key_points": []},
        content="Body [S1].", references=refs, status=ArticleStatus.DRAFT)
    session.add(article)
    session.commit()
    return article, row.domain


@pytest.fixture
def cert_article():
    """A certification guide citing nothing from the vendor's own domain."""
    with get_session() as s:
        article, domain = _make(s, ContentType.CERTIFICATION_GUIDE, [OTHER_REF])
        ids = (article.id, article.topic_id, domain)
    yield ids
    with get_session() as s:
        s.delete(s.get(Article, ids[0]))
        s.commit()
        s.delete(s.get(Topic, ids[1]))
        s.commit()


class TestWhatIsLocked:
    def test_the_static_three_are_always_locked(self, cert_article):
        with get_session() as s:
            locked = unfixable_checks(s, s.get(Article, cert_article[0]))
        assert STATIC <= locked

    def test_a_cert_guide_with_no_vendor_source_locks_vendor_grounding(
            self, cert_article):
        with get_session() as s:
            locked = unfixable_checks(s, s.get(Article, cert_article[0]))
        assert "vendor_grounding" in locked

    def test_citing_the_vendor_unlocks_it(self, cert_article):
        # A vendor source among the references is one a rewrite can cite.
        article_id, _topic_id, domain = cert_article
        with get_session() as s:
            article = s.get(Article, article_id)
            article.references = [OTHER_REF, {"tag": "S2", "title": "V",
                                              "url": f"https://{domain}/x",
                                              "domain": domain}]
            s.commit()
            locked = unfixable_checks(s, s.get(Article, article_id))
        assert "vendor_grounding" not in locked

    def test_a_subdomain_of_the_vendor_counts(self, cert_article):
        article_id, _topic_id, domain = cert_article
        with get_session() as s:
            article = s.get(Article, article_id)
            article.references = [{"tag": "S1", "title": "V",
                                   "url": f"https://docs.{domain}/x",
                                   "domain": f"docs.{domain}"}]
            s.commit()
            locked = unfixable_checks(s, s.get(Article, article_id))
        assert "vendor_grounding" not in locked

    def test_a_non_certification_article_is_never_locked_for_it(self):
        # The check only runs on certification guides, so locking it
        # elsewhere would hide a control that was never going to fire.
        with get_session() as s:
            article, _ = _make(s, ContentType.COMPARISON, [OTHER_REF])
            article_id, topic_id = article.id, article.topic_id
            locked = unfixable_checks(s, article)
        assert "vendor_grounding" not in locked
        with get_session() as s:
            s.delete(s.get(Article, article_id))
            s.commit()
            s.delete(s.get(Topic, topic_id))
            s.commit()


class TestTheExamCodeLock:
    """exam_code_presence warns in two situations, and only one is wording.

    With a registration code on record, the finding names the exact string
    ("CCME: 156-836") and a fix can print it. With none on record for the
    vendor, missing_exam_codes() skips every certification — there is nothing
    to print, and the check is reporting a gap in the Vendor Centre instead.
    """

    def _add_code(self, session, vendor_id, version_code):
        cert = Certification(vendor_id=vendor_id, name="Test Expert",
                             exam_code="TCSE",
                             status=CertificationStatus.CURRENT)
        session.add(cert)
        session.flush()
        session.add(CertificationVersion(
            certification_id=cert.id, version_label="R99",
            exam_code=version_code, source_url="https://example.com/exam"))
        session.commit()
        return cert.id

    def _drop_code(self, cert_id):
        with get_session() as s:
            for v in s.scalars(select(CertificationVersion).where(
                    CertificationVersion.certification_id == cert_id)).all():
                s.delete(v)
            s.commit()
            s.delete(s.get(Certification, cert_id))
            s.commit()

    def test_no_code_on_record_locks_it(self, cert_article):
        with get_session() as s:
            locked = unfixable_checks(s, s.get(Article, cert_article[0]))
        assert "exam_code_presence" in locked

    def test_a_recorded_code_keeps_it_offered(self, cert_article):
        article_id = cert_article[0]
        with get_session() as s:
            article = s.get(Article, article_id)
            cert_id = self._add_code(s, article.vendor_id, "156-999")
            locked = unfixable_checks(s, s.get(Article, article_id))
        self._drop_code(cert_id)
        assert "exam_code_presence" not in locked

    def test_a_version_label_without_a_registration_code_does_not_count(
            self, cert_article):
        # "R99" is a release, not the string a candidate books against. V2
        # accepts only the registration code, so a version row without one
        # leaves nothing for a fix to print.
        article_id = cert_article[0]
        with get_session() as s:
            article = s.get(Article, article_id)
            cert_id = self._add_code(s, article.vendor_id, None)
            locked = unfixable_checks(s, s.get(Article, article_id))
        self._drop_code(cert_id)
        assert "exam_code_presence" in locked

    def test_a_non_certification_article_is_not_locked_for_it(self):
        with get_session() as s:
            article, _ = _make(s, ContentType.COMPARISON, [OTHER_REF])
            article_id, topic_id = article.id, article.topic_id
            locked = unfixable_checks(s, article)
        assert "exam_code_presence" not in locked
        with get_session() as s:
            s.delete(s.get(Article, article_id))
            s.commit()
            s.delete(s.get(Topic, topic_id))
            s.commit()


class TestTheFixEndpointAgrees:
    def test_ticking_only_a_locked_check_is_refused_with_the_reason(
            self, cert_article):
        # Refused BEFORE any model call: the guard runs at the top of
        # fix_issues, so this costs nothing to assert.
        with get_session() as s:
            article = s.get(Article, cert_article[0])
            with pytest.raises(PipelineError) as exc:
                fix_issues(s, article, ["vendor_grounding"], [])
        assert "vendor's own" in str(exc.value)
        assert "Rewrite whole article" in str(exc.value)


class TestThePageShowsIt:
    @pytest.fixture
    def client(self):
        from webapp.app import create_app
        return create_app().test_client()

    def test_a_locked_check_has_no_checkbox(self, client, cert_article):
        with get_session() as s:
            article = s.get(Article, cert_article[0])
            article.qa_checks = [
                {"name": "vendor_grounding", "status": "fail",
                 "detail": "no source from the vendor's own site"},
                {"name": "originality", "status": "fail", "detail": "copied run"},
            ]
            article.qa_score = 70
            article.qa_passed = False
            s.commit()
        html = client.get(f"/studio/article/{cert_article[0]}").get_data(as_text=True)
        assert "not fixable by rewriting" in html
        # The fixable one beside it is still offered.
        assert 'value="originality"' in html
