"""A training course is not a credential.

The Vendor KB was seeded from a scrape of Red Education's own site, and
Course.certification_id is NOT NULL, so every scraped course needed a parent
certification row. For 52 of 121 rows that parent IS the course: identical
name, and a COURSE code in exam_code (SDWSCS, SPCOR, PFAA-400-BVP) rather than
an exam registration code. Cisco, Ping Identity, Extreme Networks and Fortinet
were 100% course rows.

The Topic Engine reads that table as "this vendor's certifications", so it was
being handed course names and asked to propose certification guides about
them. That is the same fusion of course and credential that put "Prisma Access
SSE certification" — a credential that does not exist — into a published
draft, alongside "Prisma Access SSE: Configuration and Deployment", which is
the course.
"""
from config.db import (Certification, CertificationVersion, Vendor,
                       get_session)


import pytest

from config.db import (CertificationStatus, Course,
                       _migrate_flag_course_shaped_certifications)


@pytest.fixture
def kb():
    """A vendor with one real credential and one course filed as a
    certification — the two shapes the rule has to tell apart. Built here
    rather than read from the seeded database, so the test proves the LOGIC
    and not whatever happens to be in the KB today."""
    with get_session() as s:
        v = Vendor(name="Testvendor")
        s.add(v)
        s.flush()

        # A course the scrape filed as a certification: same identity on both
        # rows, with the vendor branding present only on the public course.
        # This is the shape used by Nutanix and is not vendor-specific.
        course_shaped = Certification(
            vendor_id=v.id, name="Implementing Widgets", exam_code="IWID",
            status=CertificationStatus.CURRENT)
        s.add(course_shaped)
        s.flush()
        s.add(Course(certification_id=course_shaped.id,
                     name="Testvendor Implementing Widgets",
                     url="https://www.rededucation.com/coursesvender/iw/"))

        # A real credential, recorded with an exam version, that happens to
        # have a course of the same name hanging off it.
        real = Certification(vendor_id=v.id, name="Widget Expert",
                             exam_code="WEX",
                             status=CertificationStatus.CURRENT)
        s.add(real)
        s.flush()
        s.add(Course(certification_id=real.id, name="Widget Expert",
                     url="https://www.rededucation.com/coursesvender/we/"))
        s.add(CertificationVersion(certification_id=real.id, version_label="1.0",
                                   exam_code="900-100",
                                   source_url="https://vendor.example/exam"))

        # A genuine unversioned credential whose training course uses the
        # same public name. Production EPI/Netskope/Check Point rows have this
        # shape, so same-name alone must never demote it.
        named_credential = Certification(
            vendor_id=v.id, name="Certified Widget Specialist",
            exam_code="CWS", status=CertificationStatus.CURRENT)
        s.add(named_credential)
        s.flush()
        s.add(Course(certification_id=named_credential.id,
                     name="Testvendor Certified Widget Specialist",
                     url="https://www.rededucation.com/coursesvender/cws/"))
        s.commit()
        ids = (v.id, course_shaped.id, real.id, named_credential.id)

    _migrate_flag_course_shaped_certifications()
    yield ids

    with get_session() as s:
        for cid in ids[1:]:
            c = s.get(Certification, cid)
            for co in list(c.courses):
                s.delete(co)
            for ver in s.query(CertificationVersion).filter_by(
                    certification_id=cid).all():
                s.delete(ver)
            s.delete(c)
        s.delete(s.get(Vendor, ids[0]))
        s.commit()


class TestTheFlagIsAccurate:
    def test_a_course_shaped_row_is_not_a_credential(self, kb):
        _v, course_shaped, _real, _named = kb
        with get_session() as s:
            assert s.get(Certification, course_shaped).is_credential is False

    def test_a_row_with_a_recorded_exam_version_is_never_demoted(self, kb):
        # The guard that makes the rule safe. A real credential we have
        # properly recorded keeps its identity even though a course shares
        # its name.
        _v, _cs, real, _named = kb
        with get_session() as s:
            assert s.get(Certification, real).is_credential is True

    def test_a_named_credential_is_not_demoted_for_sharing_a_course_name(self, kb):
        _v, _course, _versioned, named_credential = kb
        with get_session() as s:
            assert s.get(Certification, named_credential).is_credential is True

    def test_nothing_is_deleted_or_renamed(self, kb):
        # Guessing a real credential name from a course name is the exact
        # fabrication this change exists to stop. The rows stay; only the
        # claim that they are credentials goes.
        _v, course_shaped, _real, _named = kb
        with get_session() as s:
            row = s.get(Certification, course_shaped)
            assert row is not None
            assert row.name == "Implementing Widgets"
            assert len(row.courses) == 1

    def test_the_flag_is_derived_not_a_manual_override(self, kb):
        # Flipping it by hand does not stick, and should not: it is computed
        # from the row's own shape. A UI toggle that survived would let a
        # course be declared a credential without anything backing it, which
        # is the fabrication this whole change exists to stop.
        _v, course_shaped, _real, _named = kb
        with get_session() as s:
            s.get(Certification, course_shaped).is_credential = True
            s.commit()
        _migrate_flag_course_shaped_certifications()
        with get_session() as s:
            assert s.get(Certification, course_shaped).is_credential is False

    def test_recording_a_real_exam_version_promotes_the_row(self, kb):
        # The supported way to correct a row: give it a credential's identity
        # from the vendor's own page. Then it stops being course-shaped.
        _v, course_shaped, _real, _named = kb
        with get_session() as s:
            s.add(CertificationVersion(
                certification_id=course_shaped, version_label="2026",
                exam_code="900-200",
                source_url="https://vendor.example/real-exam"))
            s.get(Certification, course_shaped).is_credential = True
            s.commit()
        _migrate_flag_course_shaped_certifications()
        with get_session() as s:
            assert s.get(Certification, course_shaped).is_credential is True


class TestWhatTheTopicEngineSees:
    def test_course_rows_are_offered_as_courses_not_certifications(self):
        from webapp.pipeline import _vendor_cert_context
        with get_session() as s:
            v = s.query(Vendor).filter_by(name="Cisco").first()
            if v is None:
                return
            creds, courses = _vendor_cert_context(s, v.id)
            shaped = {c.name.strip().lower() for c in v.certifications
                      if not c.is_credential}
            # Their names must still reach the prompt — as courses.
            assert shaped <= {c.strip().lower() for c in courses}
            assert not (shaped & {c.strip().lower() for c in creds})

    def test_an_empty_credential_list_forbids_inventing_one(self):
        # A model shown courses and asked for certification topics will name a
        # credential from memory. Silence reads as permission.
        from synthesis.topics import build_topic_prompt
        p = build_topic_prompt("Cisco", 5, [], ["Some Cisco Course"], [])
        assert "NO certifications on record" in p
        assert "Do NOT propose a certification_guide" in p

    def test_the_warning_is_absent_when_credentials_exist(self):
        from synthesis.topics import build_topic_prompt
        p = build_topic_prompt("Check Point", 5, ["CCSE"], ["c"], [])
        assert "NO certifications on record" not in p


class TestQaKnowledgeIgnoresCourseCodes:
    def test_a_course_code_is_not_a_certification_acronym(self):
        # all_cert_codes drives V1's "does this article name a certification
        # at all" decision. A course code there makes it believe an article
        # names a certification when it names a class.
        with get_session() as s:
            v = s.query(Vendor).filter_by(name="Cisco").first()
            if v is None:
                return
            course_codes = {c.exam_code for c in v.certifications
                            if not c.is_credential and c.exam_code}
            if not course_codes:
                return
            from webapp.pipeline import _qa_knowledge

            class _A:
                vendor_id = v.id
                vendor = v
                topic = None
                references = []
            kb = _qa_knowledge(s, _A())
            assert not ({c.upper() for c in course_codes} & set(kb.all_cert_codes))


class TestEveryWriterGetsTheTypedCatalogue:
    CERTS = ["Widget Expert"]
    COURSES = ["Testvendor Implementing Widgets"]

    @staticmethod
    def _brief():
        from synthesis.brief import ContentBrief
        return ContentBrief(title="Widget Guide", audience="Engineers",
                            keyword="widget", format="explainer",
                            outline=["A", "B", "C"])

    def test_brief_generation_gets_the_contract(self):
        from synthesis.brief import build_brief_prompt
        prompt = build_brief_prompt(
            "Widget topic", "widget", "explainer", "informational",
            "Testvendor", self.CERTS, self.COURSES, ["A verified fact"])
        assert "ENTITY CONTRACT FOR TESTVENDOR" in prompt
        assert "Widget Expert" in prompt
        assert "Testvendor Implementing Widgets" in prompt

    def test_article_generation_gets_the_contract(self):
        from retrieval.models import VerifiedBundle
        from synthesis.brief import build_article_prompt
        _system, prompt, _tags = build_article_prompt(
            self._brief(), VerifiedBundle(topic="widget", region=None),
            "Testvendor", certifications=self.CERTS, courses=self.COURSES)
        assert "CERTIFICATIONS/CREDENTIALS: Widget Expert" in prompt
        assert "TRAINING COURSES: Testvendor Implementing Widgets" in prompt

    def test_section_rewrite_gets_the_same_contract(self):
        from retrieval.models import VerifiedBundle
        from synthesis.brief import build_section_rewrite_prompt
        _system, prompt, _tags = build_section_rewrite_prompt(
            "## A\n\nText.", "A", self._brief(),
            VerifiedBundle(topic="widget", region=None), "Testvendor",
            certifications=self.CERTS, courses=self.COURSES)
        assert "CERTIFICATIONS/CREDENTIALS: Widget Expert" in prompt
        assert "TRAINING COURSES: Testvendor Implementing Widgets" in prompt
