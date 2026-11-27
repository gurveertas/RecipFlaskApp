"""Slice 3.9 — buyer-question coverage in the Brief.

A prevention rule, not a detection one. The article that proved it (Cortex
XSOAR vs XSIAM) was structurally strong and answered none of the three
questions a buyer comparing two platforms actually has: cost, migration path,
and where the vendor is taking each product. By the time QA could have
noticed, the outline had already decided.

So these tests are about what the Brief REFUSES to leave out, not about what
the finished article contains.
"""
import pytest

from synthesis.brief import (BUYER_QUESTIONS, buyer_question_block,
                             missing_buyer_questions)


class TestComparisonIntent:
    """xsoar-vs-xsiam: the defect that proved the slice."""

    XSOAR_OUTLINE = [
        "What is Cortex XSOAR?",
        "What is Cortex XSIAM?",
        "Feature comparison",
        "Which platform suits which team",
        "Summary",
    ]

    def test_the_xsoar_outline_is_rejected(self):
        missing = missing_buyer_questions(self.XSOAR_OUTLINE, "comparison")
        assert len(missing) == 3
        assert any("commercial model" in m for m in missing)
        assert any("migration" in m for m in missing)
        assert any("direction of travel" in m for m in missing)

    def test_an_outline_answering_all_three_passes(self):
        outline = self.XSOAR_OUTLINE + [
            "What does each platform cost to license?",
            "How much work is migrating from XSOAR?",
            "Where is Palo Alto taking each product?",
        ]
        assert missing_buyer_questions(outline, "comparison") == []

    def test_one_heading_may_answer_two_questions(self):
        # "What does it cost to switch?" is both commercial model and
        # migration cost. Demanding one heading each would force padding.
        outline = ["What does it cost to switch, and what is the roadmap?"]
        assert missing_buyer_questions(outline, "comparison") == []

    def test_partial_coverage_names_only_what_is_missing(self):
        outline = ["Licensing and pricing", "Migration effort"]
        missing = missing_buyer_questions(outline, "comparison")
        assert len(missing) == 1
        assert "direction of travel" in missing[0]


class TestCertificationIntent:
    def test_a_certification_outline_missing_everything_is_rejected(self):
        outline = ["What is the CCME?", "Why it matters", "Summary"]
        assert len(missing_buyer_questions(outline, "certification_guide")) == 4

    def test_a_complete_certification_outline_passes(self):
        outline = [
            "What is the CCME and what is the exam code?",
            "Prerequisites before you sit it",
            "How long the certification stays valid",
            "What happens if you fail: the retake policy",
        ]
        assert missing_buyer_questions(outline, "certification_guide") == []

    def test_the_registration_code_itself_counts_as_exam_logistics(self):
        # V2 warns on a certification article that never names its
        # registration code. Requiring it in the outline is the prevention
        # step that stops that warning ever firing.
        outline = ["Booking exam 156-836 at Pearson VUE", "Prerequisites",
                   "Validity and renewal", "Retake policy"]
        assert missing_buyer_questions(outline, "certification_guide") == []


class TestOtherIntents:
    @pytest.mark.parametrize("content_type", [
        "technology_education", "vendor_news", "salary_guide",
        "career_article", "", None,
    ])
    def test_intents_with_no_requirements_are_never_blocked(self, content_type):
        # A vendor-news piece must not be held to a certification's questions.
        assert missing_buyer_questions(["Anything at all"], content_type) == []
        assert buyer_question_block(content_type) == []


class TestPromptBlock:
    def test_the_block_lists_every_requirement(self):
        block = "\n".join(buyer_question_block("comparison"))
        for label, _ in BUYER_QUESTIONS["comparison"]:
            assert label in block

    def test_the_block_says_the_wording_is_not_prescribed(self):
        # Otherwise the model reproduces the labels as literal headings, which
        # reads as a template rather than an outline.
        block = "\n".join(buyer_question_block("certification_guide"))
        assert "do not need these exact words" in block

    def test_every_requirement_is_reachable_by_its_own_terms(self):
        # Guards against a requirement whose match terms never fire — it would
        # be permanently missing and every brief would retry to exhaustion.
        for content_type, reqs in BUYER_QUESTIONS.items():
            for label, terms in reqs:
                assert terms, f"{content_type}/{label} has no match terms"
                probe = [terms[0]]
                assert label not in missing_buyer_questions(probe, content_type), (
                    f"{content_type}/{label} cannot be satisfied by its own "
                    f"first term {terms[0]!r}")


class TestIntentWiring:
    def test_comparison_is_a_real_content_type(self):
        from config.db import ContentType
        assert ContentType.COMPARISON.value == "comparison"

    def test_the_topic_engine_can_propose_every_content_type(self):
        # If these drift, the Brief can require questions for an intent the
        # Topic Engine is unable to assign, and the requirement never fires.
        from config.db import ContentType
        from synthesis.topics import CONTENT_TYPES
        assert set(CONTENT_TYPES) == {c.value for c in ContentType}

    def test_every_requirement_set_names_a_real_content_type(self):
        from config.db import ContentType
        known = {c.value for c in ContentType}
        assert set(BUYER_QUESTIONS) <= known
