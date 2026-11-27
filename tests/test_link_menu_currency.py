"""The approved-link menu must not offer pages that are real but wrong.

An article linked readers to "a free webinar ... EDU-270", a rededucation.com
page announcing a course as forthcoming. The course has since been retired and
the webinar is long past. Nothing upstream catches this: the page exists,
resolves, is on our own domain, and is genuinely about the vendor.

Two rules, and the difference between them matters. Retirement is matched
against the KB's RETIRED certifications, so it only knows what the knowledge
base has been told — EDU-270 is not in the KB at all. The event rule is what
actually catches that one.
"""
from datetime import date, datetime

from config.db import (Certification, CertificationStatus, PublishedPost,
                       Vendor, get_session)
from synthesis.gaps import _retired_link_terms, _unlinkable

TODAY = date(2026, 8, 27)


def post(title, url, published=None):
    return PublishedPost(title=title, url=url, kind="post",
                         published_at=published)


class TestStaleEventPages:
    def test_an_old_webinar_announcement_is_excluded(self):
        p = post("Free webinar: Cortex XSIAM EDU-270",
                 "https://www.rededucation.com/free-webinar-edu-270/",
                 datetime(2025, 8, 20))
        assert "event announcement" in _unlinkable(p, set(), TODAY)

    def test_a_recent_webinar_is_still_offered(self):
        p = post("Free webinar: new course",
                 "https://www.rededucation.com/free-webinar-new/",
                 datetime(2026, 7, 1))
        assert _unlinkable(p, set(), TODAY) is None

    def test_an_undated_event_page_is_excluded(self):
        # No date means no way to know it has not already happened.
        p = post("Join us for a live session",
                 "https://www.rededucation.com/live/", None)
        assert _unlinkable(p, set(), TODAY) == "undated event announcement"

    def test_an_ordinary_course_page_is_untouched_by_the_event_rule(self):
        p = post("Palo Alto EDU-330 Firewall Troubleshooting",
                 "https://www.rededucation.com/edu-330/", None)
        assert _unlinkable(p, set(), TODAY) is None


class TestRetiredCourses:
    def test_a_page_for_a_retired_course_is_excluded(self):
        p = post("PCNSE Certification Guide",
                 "https://www.rededucation.com/pcnse-guide/", datetime(2026, 1, 1))
        why = _unlinkable(p, {"pcnse"}, TODAY)
        assert why and "retired" in why

    def test_a_current_course_page_is_offered(self):
        p = post("EDU-330 Troubleshooting",
                 "https://www.rededucation.com/edu-330/", datetime(2026, 1, 1))
        assert _unlinkable(p, {"pcnse"}, TODAY) is None

    def test_a_five_character_code_is_found(self):
        # A 6-character floor silently excluded PCNSE and PCNSA — both five —
        # so the rule matched nothing at all for Palo Alto while looking fine.
        with get_session() as s:
            v = Vendor(name="Fivechar Vendor")
            s.add(v); s.flush()
            s.add(Certification(vendor_id=v.id, name="PCNSE",
                                status=CertificationStatus.RETIRED))
            s.commit(); vid = v.id
        try:
            with get_session() as s:
                assert "pcnse" in _retired_link_terms(s, "Fivechar Vendor")
        finally:
            with get_session() as s:
                for c in s.query(Certification).filter_by(vendor_id=vid).all():
                    s.delete(c)
                s.delete(s.get(Vendor, vid))
                s.commit()

    def test_a_vendor_with_nothing_retired_yields_no_terms(self):
        with get_session() as s:
            assert _retired_link_terms(s, "Nonexistent Vendor") == set()


class TestThePromptNoLongerSuppliesTheBoilerplate:
    def test_the_addendum_does_not_hand_over_the_phrase(self):
        # "Red Education instructors regularly see" appeared three times in
        # one article. It was not the model reaching for a cheap template —
        # the prompt gave it that exact string as the worked example.
        from synthesis.reacs import REACS_PROMPT_ADDENDUM
        assert "Red Education instructors regularly see" not in REACS_PROMPT_ADDENDUM

    def test_the_addendum_asks_for_attribution_or_nothing(self):
        from synthesis.reacs import REACS_PROMPT_ADDENDUM
        assert "named instructor" in REACS_PROMPT_ADDENDUM

    def test_the_a3_failure_message_does_not_reintroduce_it(self, reacs):
        # reacs_feedback_block feeds this message straight back to the writer,
        # so a phrase in the message is a phrase in the next prompt. Tested by
        # provoking the failure rather than grepping the module, which would
        # also match the comment recording why the phrase was removed.
        checks = reacs("In my experience the firewall drops these packets.",
                       title="t", keyword="t")
        msg = checks["voice_consistency"].message
        assert "Red Education instructors regularly see" not in msg
        assert "named instructor" in msg
