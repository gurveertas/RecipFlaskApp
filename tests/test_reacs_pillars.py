"""REACS v1.3 pillar regression tests.

Every test here is derived from a real published defect or a real bug this
gate shipped, and is NAMED after the article or incident that proved it — so
nobody removes one without understanding what it protects. That naming
convention is the standard's own instruction, and it has already earned its
keep twice: two of these rules blocked live articles for the wrong reason.

The gate scores SIX mandatory pillars. v1.3 lists more; the difference is
deliberate and settled, not a backlog. M7 (instructor byline) was switched off
by decision — articles publish under the shared Admin account for now. See
synthesis/reacs.py's module docstring.
"""
from synthesis.reacs import (REACS_THRESHOLD, _internal_links, compute_reacs,
                             pick_instructor)


def answered(n=8):
    return [{"question": f"q{i}", "answer": "a real answer"} for i in range(n)]


class TestGateShape:
    def test_threshold_is_the_v1_3_hard_minimum(self):
        assert REACS_THRESHOLD == 90

    def test_pillar_set_is_six_mandatory_and_six_advisory(self, reacs):
        checks = compute_reacs(content="x", title="t", keyword="k", faq=[],
                               has_byline=False, entities=[]).checks
        mandatory = [c for c in checks if c.tier == "mandatory"]
        advisory = [c for c in checks if c.tier == "advisory"]
        assert len(mandatory) == 6, [c.pillar for c in mandatory]
        assert len(advisory) == 6, [c.pillar for c in advisory]

    def test_instructor_byline_is_off_by_decision(self, reacs):
        # M7 was removed from the gate; publishing goes out as Admin. If this
        # ever fails, someone reinstated the pillar — which is fine, but the
        # score denominator changed with it and past scores stop comparing.
        assert "instructor_byline" not in reacs("x")

    def test_score_is_deterministic(self):
        body = "## Summary of k\nThe k is a thing.\n"
        first = compute_reacs(content=body, title="k", keyword="k", faq=[],
                              has_byline=True, entities=[])
        second = compute_reacs(content=body, title="k", keyword="k", faq=[],
                               has_byline=True, entities=[])
        assert first.score == second.score
        assert [c.passed for c in first.checks] == [c.passed for c in second.checks]


class TestRescoreNeverTouchesContent:
    """The whole promise of Re-score is "changes nothing but the verdicts".

    A structural guard rather than a behavioural one: exercising it properly
    needs a database and an article, which this suite deliberately avoids. But
    the one thing that must never regress is cheap to assert — if someone
    later adds a "tidy the text while we are here" step, this fails loudly.
    """

    def test_rescore_article_assigns_nothing_to_the_body(self):
        import inspect
        from webapp.pipeline import rescore_article
        src = inspect.getsource(rescore_article)
        for forbidden in ("article.content =", "article.content=",
                          "article.word_count =", "article.faq =",
                          "article.title =", "article.meta_title ="):
            assert forbidden not in src, (
                f"rescore_article writes {forbidden.strip(' =')} — it must only "
                "re-run the gates, never edit the article")

    def test_rescore_reports_whether_qa_actually_re_ran(self):
        # QA needs the article's sources rebuilt and that can fail. Returning
        # a flag forces the caller to say so rather than leaving a stale score
        # looking freshly checked.
        import inspect
        from webapp.pipeline import rescore_article
        src = inspect.getsource(rescore_article)
        assert "qa_rechecked = False" in src and "return qa_rechecked" in src


class TestPublishRevalidatesCurrentRules:
    def test_rescore_runs_before_the_stored_gate(self):
        import inspect
        from webapp.pipeline import push_to_wordpress
        src = inspect.getsource(push_to_wordpress)
        assert src.index("rescore_article(session, article)") < src.index(
            "article.qa_passed and article.reacs_passed")


class TestMessagesReadCorrectlyInBothStates:
    """A passing check must not read as an outstanding task.

    Found live: `common_mistakes` carried one unconditional message —
    'Include a "Common mistakes" section' — shown whether it passed or failed,
    so a green tick sat next to an instruction to add something the article
    already had. `comparison_table` had the same shape. These messages are
    also fed to the writer on retry, so a to-do that fires on success is the
    unfollowable-feedback loop in a new place.
    """

    PILLARS_WITH_A_FIXED_ANSWER = [
        ("comparison_table", "| A | B |\n| --- | --- |\n| x | y |", "x"),
        ("common_mistakes",
         "## Common mistakes in preparation\n"
         "**Skipping the baseline** can hide regressions and disrupt users. "
         "Correct it by recording normal results before changes and comparing "
         "the same representative workflow afterwards.\n\n"
         "**Enforcing too soon** may block legitimate work and cause outages. "
         "Correct it by reviewing staged events first, resolving false positives "
         "and documenting approval before enforcement.\n\n"
         "**Ignoring monitoring** leaves failures invisible and risks delay. "
         "Correct it by validating useful logs and monitoring application and "
         "platform telemetry throughout each rollout.",
         "x"),
    ]

    def test_message_differs_between_pass_and_fail(self, reacs):
        for pillar, good, bad in self.PILLARS_WITH_A_FIXED_ANSWER:
            passing, failing = reacs(good)[pillar], reacs(bad)[pillar]
            assert passing.passed and not failing.passed, pillar
            assert passing.message != failing.message, (
                f"{pillar} shows the same message whether it passed or failed")

    def test_a_passing_message_does_not_open_with_an_instruction(self, reacs):
        for pillar, good, _bad in self.PILLARS_WITH_A_FIXED_ANSWER:
            msg = reacs(good)[pillar].message.strip().lower()
            assert not msg.startswith(("include", "at least", "add ")), (
                f"{pillar}'s passing message reads as a to-do: {msg[:60]}")

    def test_common_mistakes_rejects_a_heading_with_generic_advice(self, reacs):
        check = reacs("## Common pitfalls\nGeneric advice.")["common_mistakes"]
        assert not check.passed
        assert "consequence" in check.message
        assert "corrective action" in check.message

    def test_entity_coverage_names_no_dangling_list_when_the_kb_is_empty(self, reacs):
        # Used to trail off into "by name: ." — reads as truncated output
        # rather than an instruction, and no rewrite can act on it.
        msg = reacs("nothing", entities=[])["entity_coverage"].message
        assert "by name: ." not in msg
        assert "Vendor Centre" in msg


class TestM1KeywordInTitleAndMeta:
    def test_passes_when_the_keyword_is_in_both(self, reacs):
        c = reacs("x", title="The k guide", keyword="k", meta_title="k in 2026")
        assert c["single_search_intent"].passed

    def test_fails_when_missing_from_the_meta_title(self, reacs):
        # v1.0 checked the H1 alone. The meta title is separately written and
        # is what the buyer and the vendor see in the SERP.
        c = reacs("x", title="The k guide", keyword="k", meta_title="Buy now")
        assert not c["single_search_intent"].passed

    def test_falls_back_to_the_h1_when_no_meta_title_is_supplied(self, reacs):
        c = reacs("x", title="The k guide", keyword="k")
        assert c["single_search_intent"].passed


class TestM2DirectAnswerOpening:
    def test_fails_an_opening_that_describes_the_article(self, reacs):
        # v1.3: "Meta-commentary fails. 'This is a reference-style breakdown
        # of...' is not an answer, it is a description of the article."
        c = reacs("This guide breaks down everything about the k.", keyword="k")
        assert not c["direct_answer_first"].passed

    def test_passes_an_opening_that_answers(self, reacs):
        c = reacs("The k is a certification that validates firewall skills.",
                  keyword="k")
        assert c["direct_answer_first"].passed


class TestM4FaqAnswersNotQuestions:
    def test_fails_eight_questions_with_empty_answers_accordion_case(self, reacs):
        # The standard is explicit: "Count the answers, not the questions.
        # Accordion widgets can render questions with empty bodies."
        empty = [{"question": f"q{i}", "answer": ""} for i in range(8)]
        assert not reacs("x", faq=empty)["faq_depth"].passed

    def test_passes_eight_answered(self, reacs):
        assert reacs("x", faq=answered())["faq_depth"].passed


class TestM5InternalLinks:
    def test_site_root_is_not_a_countable_internal_link(self):
        body = ("[a](https://www.rededucation.com/) [b](https://www.rededucation.com) "
                "[c](/) [d](/checkpoint/) [e](/f5/)")
        assert _internal_links(body) == ["/checkpoint/", "/f5/"]

    def test_two_real_internal_links_pass(self, reacs):
        c = reacs("[a](/checkpoint/) and [b](/f5/)")
        assert c["internal_linking"].passed


class TestM6FullDate:
    def test_fails_a_month_only_stamp(self, reacs):
        # Live on both drafts and article 1: "Last updated: August 2026".
        assert not reacs("Last updated: August 2026")["freshness_stamp"].passed

    def test_accepts_every_full_date_shape_the_site_uses(self, reacs):
        for stamp in ("Last updated: 19 August 2026",
                      "Last updated: August 19, 2026",
                      "Last updated: 2026-08-19"):
            assert reacs(stamp)["freshness_stamp"].passed, stamp

    def test_an_edit_replaces_an_old_stamp(self):
        from datetime import date
        from synthesis.text import refresh_last_updated
        body = refresh_last_updated("Body.\n\nLast updated: 27 August 2026",
                                    date(2026, 8, 31))
        assert body.endswith("Last updated: 31 August 2026")
        assert "27 August" not in body

    def test_an_edit_adds_the_stamp_when_the_writer_omits_it(self):
        from datetime import date
        from synthesis.text import refresh_last_updated
        assert refresh_last_updated("Body.", date(2026, 8, 31)).endswith(
            "Last updated: 31 August 2026")


class TestA3VoiceConsistency:
    def test_fails_first_person_under_a_reviewed_by_byline(self, reacs):
        c = reacs("In my experience the k exam is hard.")
        assert not c["voice_consistency"].passed

    def test_fails_the_boilerplate_experience_fragment_xsoar_regression(self, reacs):
        # The exact string slice 3.1 orders purged, and which REACS v1.0
        # awarded a point for. Found live in two WordPress drafts.
        c = reacs("After delivering this course to thousands of engineers, we see")
        assert not c["voice_consistency"].passed

    def test_fails_unattributed_house_recommendations(self, reacs):
        c = reacs("We often recommend taking the course before the exam.")
        assert not c["voice_consistency"].passed

    def test_fails_our_experience(self, reacs):
        assert not reacs("In our experience, this path is easier.")[
            "voice_consistency"].passed

    def test_absence_of_experience_voice_is_no_longer_a_miss(self, reacs):
        # v1.0 REQUIRED the fragment. The pillar now tests attribution, not
        # presence, so plain factual prose is fine.
        assert reacs("Plain factual prose about the exam.")["voice_consistency"].passed

    def test_third_person_experience_passes(self, reacs):
        c = reacs("Red Education instructors regularly see this in the lab.")
        assert c["voice_consistency"].passed


class TestA6SummaryAndSingleCta:
    CTAS = ["Advance your career with Red Education", "Explore our courses"]

    def test_fails_zero_ctas_in_the_body(self, reacs):
        # Only the body is published; a CTA living solely in the `cta` field
        # ships an article with no close. Observed live on article 1.
        c = reacs("## Summary of k\n", cta_phrases=self.CTAS)
        assert not c["summary_and_cta"].passed

    def test_passes_exactly_one(self, reacs):
        c = reacs("## Summary of k\n" + self.CTAS[0], cta_phrases=self.CTAS)
        assert c["summary_and_cta"].passed

    def test_fails_stacked_ctas(self, reacs):
        body = "## Summary of k\n" + self.CTAS[0] + " " + self.CTAS[1]
        assert not reacs(body, cta_phrases=self.CTAS)["summary_and_cta"].passed

    def test_skips_the_count_when_no_phrase_list_is_supplied(self, reacs):
        assert reacs("## Summary of k\n")["summary_and_cta"].passed


class TestQuestionHeadingsFeedback:
    def test_feedback_says_rephrase_not_add_and_names_candidates(self, reacs):
        # A targeted fix also tells the model to keep everything unchanged, so
        # "add more question headings" read as "add sections" and the model
        # did nothing — click after click. Observed live on article 2.
        body = ("## What is the k?\nText.\n\n"
                "## Exam logistics and registration\nText.\n\n"
                "## Common mistakes in preparation\nText.\n")
        msg = reacs(body, keyword="k")["question_headings"].message
        assert "REPHRASE" in msg
        assert "Do NOT add a new section" in msg
        assert "Exam logistics and registration" in msg


class TestInstructorSelection:
    """Who is eligible, and which one — two separate rules."""

    def test_f5_has_rodolfo_as_its_only_eligible_instructor(self):
        from sqlalchemy import select
        from config.db import (Instructor, InstructorVendor, Vendor,
                               get_session)

        with get_session() as session:
            f5 = session.scalar(select(Vendor).where(Vendor.name == "F5"))
            names = session.scalars(
                select(Instructor.name)
                .join(InstructorVendor,
                      InstructorVendor.instructor_id == Instructor.id)
                .where(InstructorVendor.vendor_id == f5.id)
            ).all()
            rodolfo = session.scalar(
                select(Instructor).where(Instructor.name == "Rodolfo Nutzmann"))
        assert names == ["Rodolfo Nutzmann"]
        assert "F5" in rodolfo.credentials

    def test_an_uncovered_vendor_falls_back_to_the_whole_roster(self):
        # DECISION 2026-08-25: every article gets a byline rather than
        # publishing unattributed, so a vendor with no certified instructor
        # rotates through the full active roster.
        #
        # This is a KNOWN divergence from v1.3's M7, which calls a byline
        # crediting an instructor with a vendor they do not hold "a live
        # misrepresentation" — the defect its register logs twice
        # (xsoar-travis-loke, cisco-dan-schultz). It is coherent only because
        # M7 is off the gate. If M7 is ever reinstated, this test and the
        # fallback have to go together.
        assert pick_instructor([], ["Someone Active"]) == "Someone Active"

    def test_a_certified_instructor_still_beats_the_fallback(self):
        # The fallback is a last resort, not a shortcut: where anyone IS
        # certified for the vendor, the roster is never consulted.
        for _ in range(20):
            assert pick_instructor(["Dan Schultz"], ["Jerry", "Travis", "Dan Schultz"],
                                   usage={"Dan Schultz": 50}) == "Dan Schultz"

    def test_no_instructors_at_all_still_yields_no_byline(self):
        assert pick_instructor([], []) is None

    def test_the_fallback_rotates_too(self):
        import collections
        roster = ["Jerry", "Travis", "Dan", "Rodolfo"]
        usage: collections.Counter = collections.Counter()
        for _ in range(8):
            usage[pick_instructor([], roster, usage=dict(usage))] += 1
        assert set(usage) == set(roster)
        assert max(usage.values()) - min(usage.values()) <= 1, dict(usage)

    def test_returns_a_vendor_matched_instructor(self):
        assert pick_instructor(["Dan Schultz"], ["Dan Schultz", "X"]) == "Dan Schultz"

    def test_the_only_eligible_instructor_wins_however_busy(self):
        assert pick_instructor(["Dan Schultz"], ["Dan Schultz", "X"],
                               usage={"Dan Schultz": 99}) == "Dan Schultz"

    def test_always_picks_from_the_least_used(self):
        usage = {"Jerry": 3, "Travis": 1, "Dan": 1}
        for _ in range(30):
            assert pick_instructor(["Jerry", "Travis", "Dan"], [], usage=usage) in {
                "Travis", "Dan"}

    def test_rotation_spreads_evenly_across_a_shared_vendor(self):
        # Palo Alto Networks is taught by three instructors. random.choice on
        # every article could plausibly give one of them six in a row; picking
        # the least-used gives round-robin without storing a cursor.
        import collections
        pool = ["Jerry", "Travis", "Dan"]
        usage: collections.Counter = collections.Counter()
        for _ in range(9):
            usage[pick_instructor(pool, pool, usage=dict(usage))] += 1
        assert set(usage) == set(pool)
        assert max(usage.values()) - min(usage.values()) <= 1, dict(usage)

    def test_an_instructor_with_no_history_is_preferred(self):
        # A newly added instructor should be picked up immediately rather than
        # waiting for the incumbents to level with them.
        assert pick_instructor(["Old Hand", "New Starter"], [],
                               usage={"Old Hand": 4}) == "New Starter"
