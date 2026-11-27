"""Rewrite-a-section and proofread, for Quick drafts.

Separate from webapp/pipeline.py's rewrite_section and run_prose_review, which
read an Article's stored brief, vendor and references. A Quick draft has none
of those. What IS shared is synthesis/ — the prompt builder, the splice, the
QA harness — because that is the pipeline, and the point is that a Quick draft
goes through the same one.
"""
import pytest

from retrieval.models import Source, VerifiedBundle
from synthesis.models import Draft, QAReport
from webapp import quick_drafts as QD

BODY = ("Intro paragraph.\n\n## Bike fit\n\nStart with a frame that fits.\n\n"
        "## Maintenance\n\nClean the chain weekly.\n")


def held(**kw):
    d = Draft(topic="Cycling tips", region=None, title="Essential Cycling Tips",
              slug="c", meta_description="Tips.", body_markdown=BODY)
    r = QAReport(); r.add("structure_length", "pass", "fine")
    b = VerifiedBundle(topic="Cycling tips", region=None, sources=[
        Source(url="https://www.cyclinguk.org/a", domain="www.cyclinguk.org",
               title="t", text="body", tier=3)])
    return QD.remember_draft(d, r, ["cycling tips"], b), d


@pytest.fixture(autouse=True)
def _clean():
    QD._drafts.clear()
    yield
    QD._drafts.clear()


class TestSectionHeadings:
    def test_it_lists_the_h2s_in_order(self):
        _did, d = held()
        assert QD.section_headings(d) == ["Bike fit", "Maintenance"]

    def test_h3s_and_prose_are_not_offered(self):
        d = Draft(topic="t", region=None, title="T", slug="s",
                  meta_description="", body_markdown="### Sub\n\n## Real\n\nx\n")
        assert QD.section_headings(d) == ["Real"]


class TestRewriteSection:
    # Long enough to clear parse_section_response's thin-section guard, which
    # applies on this path too because the parser is shared.
    SECTION = ("## Bike fit\n\nRewritten body. " +
               "A frame that fits prevents most early injuries and makes long "
               "rides comfortable rather than punishing. " * 6 + "\n")

    def _stub_model(self, monkeypatch, section=None):
        section = section or self.SECTION
        # parse_section_response reads a JSON object with one "section" key —
        # returning bare markdown is what a failing model looks like.
        import json
        import synthesis.llm as L
        monkeypatch.setattr(
            L, "complete_with_configured_provider",
            lambda sp, up, purpose="": (json.dumps({"section": section}), "stub"))

    def test_the_named_section_is_replaced(self, monkeypatch):
        self._stub_model(monkeypatch)
        did, d = held()
        QD.rewrite_quick_section(did, "Bike fit")
        assert "Rewritten body." in d.body_markdown
        assert "Clean the chain weekly." in d.body_markdown   # neighbour intact

    def test_qa_is_recomputed_not_carried_over(self, monkeypatch):
        # A section edit can break article-level invariants (length, keyword
        # coverage), so the verdict has to be recalculated.
        self._stub_model(monkeypatch)
        did, _d = held()
        before = QD.get_draft(did)["report"]
        entry = QD.rewrite_quick_section(did, "Bike fit")
        assert entry["report"] is not before

    def test_an_unknown_heading_costs_no_model_call(self, monkeypatch):
        called = []
        import synthesis.llm as L
        monkeypatch.setattr(L, "complete_with_configured_provider",
                            lambda *a, **k: called.append(1))
        did, _d = held()
        with pytest.raises(QD.QuickDraftError) as e:
            QD.rewrite_quick_section(did, "No Such Section")
        assert "No section titled" in str(e.value)
        assert called == []

    def test_a_heading_matching_mid_line_is_not_accepted(self, monkeypatch):
        # Line-anchored, the same rule replace_section uses.
        self._stub_model(monkeypatch)
        did, _d = held()
        with pytest.raises(QD.QuickDraftError):
            QD.rewrite_quick_section(did, "fit")

    def test_stale_proofread_notes_are_dropped(self, monkeypatch):
        self._stub_model(monkeypatch)
        did, _d = held()
        QD.get_draft(did)["prose_review"] = [{"quote": "old"}]
        entry = QD.rewrite_quick_section(did, "Bike fit")
        assert entry["prose_review"] is None

    def test_an_expired_draft_says_so(self):
        with pytest.raises(QD.QuickDraftError) as e:
            QD.rewrite_quick_section("gone", "Bike fit")
        assert "expired" in str(e.value)


class TestProofread:
    def test_issues_are_returned_and_held(self, monkeypatch):
        class _I:
            quote, problem, suggestion, kind = "q", "p", "s", "style"
        monkeypatch.setattr("synthesis.prose.review_prose",
                            lambda body, vendor_name="": ([_I()], "stub"))
        did, _d = held()
        out = QD.proofread_quick_draft(did)
        assert out == [{"quote": "q", "problem": "p", "suggestion": "s",
                        "kind": "style"}]
        assert QD.get_draft(did)["prose_review"] == out

    def test_a_failure_is_not_reported_as_no_issues(self, monkeypatch):
        # "No issues found" and "the review did not run" must never look the
        # same — see synthesis/prose.review_prose.
        def boom(body, vendor_name=""):
            raise RuntimeError("provider down")
        monkeypatch.setattr("synthesis.prose.review_prose", boom)
        did, _d = held()
        with pytest.raises(QD.QuickDraftError) as e:
            QD.proofread_quick_draft(did)
        assert "did not run" in str(e.value)

    def test_an_empty_body_is_refused(self):
        d = Draft(topic="t", region=None, title="T", slug="s",
                  meta_description="", body_markdown="   ")
        r = QAReport()
        did = QD.remember_draft(d, r, [], None)
        with pytest.raises(QD.QuickDraftError):
            QD.proofread_quick_draft(did)


class TestItStaysSeparateFromStudio:
    def test_it_does_not_import_the_studio_pipeline(self):
        import ast, inspect
        tree = ast.parse(inspect.getsource(QD))
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module:
                mods.add(n.module)
        assert not any("pipeline" in m for m in mods), mods
        # synthesis IS shared — that is the pipeline both paths run through.
        assert any(m.startswith("synthesis") for m in mods), mods

    def test_the_studio_rewrite_is_untouched(self):
        import inspect
        from webapp import pipeline
        assert "quick_" not in inspect.getsource(pipeline.rewrite_section)
