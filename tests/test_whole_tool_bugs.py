"""Regressions for the eight faults a whole-tool review turned up.

None of them was covered: the suite passed at 597 before and after the fixes,
which is how each survived. They are unrelated except in that respect, so this
file is organised by fault rather than by module.
"""
import re

import pytest

from synthesis.models import QA_ADVISORY_CHECKS
from synthesis.prose import _prepare
from synthesis.qa_rules import (QaKnowledge, interested_party_only,
                                vague_attributions)
from synthesis.validate import _words

CITED = "Studies show that centralised management helps [S1, S2]."
UNCITED = "Studies show that centralised management helps."


class TestDemoModeStillRuns:
    """service.py used QUICK_PROFILE without importing it, so every demo-mode
    generation raised NameError — and the route's blanket except reported it
    as "the operation failed, check the logs". Demo is the home page default.
    """

    def test_the_name_is_importable_where_it_is_used(self):
        import webapp.service as service
        assert service.QUICK_PROFILE is not None

    def test_a_demo_generation_completes(self):
        from webapp.service import run_generation
        draft, report, _bundle = run_generation(topic="anything", region=None,
                                                mode="demo")
        assert draft.body_markdown
        assert report is not None


class TestCombinedCitationsCount:
    """"[S4, S5]" is in the corpus — webapp/render.py strips exactly that
    shape before publishing. Four checks matched a lone [S1] only, so a
    properly cited sentence read as uncited.
    """

    def test_a_cited_sentence_is_not_called_vague(self):
        assert vague_attributions(CITED) == []

    def test_a_genuinely_uncited_one_is_still_caught(self):
        # The fix must not silence the rule it widened.
        assert vague_attributions(UNCITED)

    def test_citation_integrity_reads_combined_markers(self, qa):
        body = ("Panorama scales to documented limits [S1, S2].\n\n"
                "## Why it matters\n\nIt centralises policy [S1].\n\n"
                "Last updated: 1 January 2026")
        refs = [("S1", "https://a.example/x", "A", "a.example"),
                ("S2", "https://b.example/y", "B", "b.example")]
        sources = [("https://a.example/x", "a.example", "A", "t", None, 1, True, "V"),
                   ("https://b.example/y", "b.example", "B", "t", None, 1, True, "V")]
        check = qa(body, refs=refs, sources=sources)["citation_integrity"]
        assert check.status == "pass", check.detail

    def test_interested_party_sees_the_sources_inside_one_marker(self):
        # Both sources are vendor marketing, so a claim resting on them alone
        # MUST be reported. Unfixed, the tag list came back empty, the rule
        # skipped the sentence, and the check went quiet on exactly the
        # sourcing problem it exists to catch.
        refs = [{"tag": "S1", "domain": "vendor.example"},
                {"tag": "S2", "domain": "marketing.example"}]
        interested = {"vendor.example", "marketing.example"}
        out = interested_party_only(
            "Throughput doubled in testing [S1, S2].", refs, interested)
        assert out, "a claim resting only on vendor marketing went unreported"

    def test_interested_party_stays_quiet_when_an_independent_source_is_cited(self):
        refs = [{"tag": "S1", "domain": "nist.gov"},
                {"tag": "S2", "domain": "vendor.example"}]
        out = interested_party_only(
            "Throughput doubled in testing [S1, S2].", refs, {"vendor.example"})
        assert out == []

    def test_the_originality_word_stream_drops_combined_markers(self):
        # Left whole, the marker tokenised into "s4"/"s5" and joined the word
        # stream, which can break a 10-word run that really is copied.
        words = _words("The exam is ninety minutes long [S4, S5] in every region.")
        assert "s4" not in words and "s5" not in words

    def test_the_proofreader_never_sees_a_marker(self):
        assert "[S" not in _prepare("A sentence [S1] and another [S4, S5].")

    def test_the_demo_reference_filter_is_comma_aware(self):
        # A source cited ONLY as part of "[S4, S5]" was dropped from the
        # reference list while its marker stayed in the body — an orphan
        # citation, which citation_integrity then fails.
        #
        # A SOURCE-LEVEL assertion, and weaker for it: the filter is inline in
        # mock_generate, whose body this test cannot steer. It catches the
        # pattern regressing, not the behaviour.
        import inspect

        from synthesis import generate
        src = inspect.getsource(generate.mock_generate)
        line = [ln for ln in src.splitlines() if "used = " in ln]
        assert line, "the reference filter moved — update this test"
        # The pattern must allow a LIST of tags. Checking for a comma alone is
        # useless: `re.findall(pattern, body)` has one.
        assert r"S\d+(?:" in line[0], line[0]


class TestTheWritePathKnowsTheVendorKb:
    """parse_article_response(raw, acronyms) dropped vendor_terms, so the path
    every article is FIRST written by could not case a brand or protect a
    vendor's own US-spelled product name. The targeted fix passed them."""

    def test_the_first_write_passes_vendor_terms(self):
        import inspect

        from synthesis import brief
        src = inspect.getsource(brief.generate_passing_article)
        call = [ln for ln in src.splitlines() if "parse_article_response(" in ln]
        assert call, "call site moved — update this test"
        assert "entities" in call[0], call[0]


class TestAdvisoryChecksAreTieredCorrectly:
    """Three warn-only checks sat in the 60-point integrity tier and could
    drag an otherwise clean article under the 90 band."""

    @pytest.mark.parametrize("name", ["link_relevance", "unnamed_attribution",
                                      "table_substance"])
    def test_a_warn_only_check_scores_as_advisory(self, name):
        assert name in QA_ADVISORY_CHECKS

    def test_a_blocking_check_is_not_advisory(self):
        for name in ("grounding", "originality", "citation_integrity",
                     "vendor_grounding"):
            assert name not in QA_ADVISORY_CHECKS


class TestRetrievalFallsBackWhenTheOpenSearchFails:
    """off_list conflated "nothing allowlisted ranks" with "the open search
    returned nothing". A failed or unkeyed search returns [], which skipped
    the scoped allowlist search entirely and returned zero sources."""

    class _Client:
        """Records what was asked of it. search_open returns nothing, exactly
        as a rate-limited, unkeyed or failed search does."""

        def __init__(self, open_hits=()):
            self.open_hits = list(open_hits)
            self.scoped_calls = []

        def search_open(self, _topic, **_kw):
            return self.open_hits

        def search(self, topic, domains, **_kw):
            self.scoped_calls.append((topic, tuple(domains)))
            return []

    def test_an_empty_open_search_still_runs_the_scoped_search(self):
        from retrieval import retrieve
        client = self._Client(open_hits=[])
        retrieve("check point certification", client=client, allow_open_web=True)
        assert client.scoped_calls, (
            "the scoped allowlist search was skipped because the open search "
            "came back empty — a transient failure must not empty a retrieval")

    def test_a_genuinely_foreign_topic_still_skips_the_scoped_search(self):
        # The saving this logic exists for: nothing allowlisted ranked, so
        # don't spend credits searching 54 vendor domains for "cycling tips".
        from types import SimpleNamespace

        from retrieval import retrieve
        client = self._Client(open_hits=[
            SimpleNamespace(url="https://cyclingweekly.example/tips", title="t",
                            snippet="")])
        retrieve("cycling tips", client=client, allow_open_web=True)
        assert client.scoped_calls == []
