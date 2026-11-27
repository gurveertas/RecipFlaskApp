"""Targeted fixes must not damage what they were not aimed at.

The complaint this module exists for: tick one check, click Fix selected, and
an unrelated check comes back amber. The cause is structural rather than a
bug — the model is asked for the WHOLE article back with everything it was not
told to touch copied across verbatim, so every unrequested edit it makes
anyway is another chance to trip a check that was passing.

The prompt cannot make that impossible. Taking the unneeded edits back
afterwards can, and costs nothing: these tests are about that reversal, and
about the ranking that decides which attempt is worth keeping.
"""
import pytest

from synthesis.brief import ArticlePackage, ArticlePatch, Edit, split_blocks
from webapp.pipeline import (_Attempt, _drop_harmful_edits, _patched_package,
                             _rejoin, _revert_collateral, _unresolved)


def pkg(content):
    return ArticlePackage(content=content, meta_title="t", meta_description="d",
                          excerpt="e", faq=[], cta="c", word_count=0)


def attempt(content, *, blocking=(), soft=(), unresolved=()):
    return _Attempt(package=pkg(content), model="m", report=None, reacs=None,
                    blocking=list(blocking), soft=list(soft),
                    unresolved=list(unresolved))


ORIGINAL = "Intro paragraph.\n\nBody paragraph, as written.\n\nClosing paragraph."


class TestRevertingUnneededEdits:
    """The whole-article FALLBACK path, used only when the patch schema
    produces nothing usable. There the edits have to be recovered by diffing
    the returned article against the draft: one paragraph carries the fix,
    another was rewritten for no reason and broke something, and only the
    second may be taken back."""

    # The model returned both edits in one response.
    REVISED = ("Intro paragraph.\n\nBody paragraph, WITH THE FIX.\n\n"
               "Closing paragraph, REWRITTEN for no reason.")

    def _evaluate(self, calls=None):
        def evaluate(p):
            if calls is not None:
                calls.append(p.content)
            return attempt(
                p.content,
                soft=[("course_link_cta", "x")] if "REWRITTEN" in p.content else [],
                unresolved=[] if "THE FIX" in p.content else ["citation_integrity"])
        return evaluate

    def test_the_damaging_edit_is_taken_back(self):
        best = _revert_collateral(ORIGINAL, self._evaluate()(pkg(self.REVISED)),
                                  self._evaluate())
        assert "REWRITTEN" not in best.package.content
        assert best.collateral == 0

    def test_the_fix_itself_survives(self):
        best = _revert_collateral(ORIGINAL, self._evaluate()(pkg(self.REVISED)),
                                  self._evaluate())
        assert "WITH THE FIX" in best.package.content
        assert best.unresolved == []

    def test_untouched_paragraphs_are_left_exactly_alone(self):
        best = _revert_collateral(ORIGINAL, self._evaluate()(pkg(self.REVISED)),
                                  self._evaluate())
        assert best.package.content.startswith("Intro paragraph.")
        assert best.package.content.endswith("Closing paragraph.")

    def test_it_rescores_once_per_changed_paragraph_at_most(self):
        # The whole approach only holds up because a rescore is ~12ms. If it
        # ever starts scoring every paragraph in the article, it is no longer
        # free and this stops being the cheap layer.
        calls = []
        _revert_collateral(ORIGINAL, self._evaluate()(pkg(self.REVISED)),
                           self._evaluate(calls))
        assert len(calls) <= 2


class TestItNeverUndoesTheFix:
    def test_a_revert_that_would_reopen_the_ticked_check_is_refused(self):
        # The fix and the damage are in the SAME paragraph — entangled, so
        # there is nothing to take back. Feeding it to the model again is the
        # next layer's job; silently reverting would hand back an article
        # with the warning cleared and the fix gone.
        revised = "Intro paragraph.\n\nBody paragraph, THE FIX.\n\nClosing paragraph."

        def evaluate(p):
            fixed = "THE FIX" in p.content
            return attempt(p.content,
                           soft=[("x", "y")] if fixed else [],
                           unresolved=[] if fixed else ["citation_integrity"])

        best = _revert_collateral(ORIGINAL, evaluate(pkg(revised)), evaluate)
        assert "THE FIX" in best.package.content
        assert best.unresolved == []

    def test_an_attempt_with_no_collateral_is_returned_untouched(self):
        a = attempt("anything at all")
        assert _revert_collateral(ORIGINAL, a, lambda p: 1 / 0) is a

    def test_a_fenced_code_block_is_never_split(self):
        # Blocks are separated by blank lines, and a fence may contain them.
        # Half-reverting a fence corrupts every line after it.
        fenced = "Intro.\n\n```\ncode\n\nmore code\n```\n\nClosing."
        a = attempt(fenced, soft=[("x", "y")])
        assert _revert_collateral(fenced, a, lambda p: 1 / 0) is a


class TestWhichAttemptIsKept:
    """Three attempts, and the first tolerable one is not necessarily the best
    — a retry can trade one problem for another."""

    def test_blocking_damage_outranks_everything(self):
        blocks = attempt("a", blocking=[("citation_integrity", "")])
        soft_only = attempt("b", soft=[("x", "")] * 5, unresolved=["y"] * 5)
        assert soft_only.rank < blocks.rank

    def test_less_damage_wins_before_a_more_complete_fix(self):
        # Deliberate: an article that still warns is the editor's to judge,
        # but one the tool silently broke is a bug they did not ask for.
        tidy = attempt("a", unresolved=["still_warning"])
        thorough = attempt("b", soft=[("broke_something", "")])
        assert tidy.rank < thorough.rank

    def test_a_clean_attempt_outranks_a_damaged_one(self):
        assert attempt("a").rank < attempt("b", soft=[("x", "")]).rank

    def test_collateral_counts_both_kinds(self):
        assert attempt("a", blocking=[("x", "")], soft=[("y", "")]).collateral == 2


class TestRejoin:
    def test_reverts_apply_highest_index_first(self):
        # Applying low-index first shifts the slice a later revert aims at,
        # so the wrong paragraph gets replaced.
        blocks = ["one", "two", "three", "four"]
        out = _rejoin(blocks, [(0, 1, ["ONE"]), (3, 4, ["FOUR"])])
        assert out == "ONE\n\ntwo\n\nthree\n\nFOUR"

    def test_a_revert_may_change_the_paragraph_count(self):
        blocks = ["one", "two"]
        assert _rejoin(blocks, [(1, 2, ["a", "b"])]) == "one\n\na\n\nb"
        assert _rejoin(blocks, [(1, 2, [])]) == "one"


class TestUnresolvedNamesBothGates:
    def test_a_warn_counts_as_unresolved(self):
        # A WARN the user explicitly ticked was not fixed. Reporting it as
        # cleared is what made a wasted call invisible.
        class C:
            def __init__(self, name, status):
                self.name, self.status = name, status

        class R:
            checks = [C("citation_integrity", "warn"), C("originality", "pass")]

        class RC:
            checks = []

        assert _unresolved(R, RC, {"citation_integrity", "originality"}) == \
            ["citation_integrity"]

    def test_a_failing_reacs_pillar_counts_too(self):
        class P:
            def __init__(self, pillar, passed):
                self.pillar, self.passed = pillar, passed

        class R:
            checks = []

        class RC:
            checks = [P("direct_answer", False), P("faq_block", True)]

        assert _unresolved(R, RC, {"direct_answer", "faq_block"}) == ["direct_answer"]


class TestDroppingEditsThatBrokeSomething:
    """The patch format already guarantees an unnamed block is untouched. What
    is left to go wrong is an edit that WAS needed for one check and trips
    another — so each edit is dropped in turn and kept out only if that
    removes damage without leaving more of the request undone."""

    FIXES = Edit(block=2, action="replace", text="Body paragraph, WITH THE FIX.")
    BREAKS = Edit(block=3, action="replace", text="Closing paragraph, REWRITTEN.")

    def _patch(self):
        return ArticlePatch(edits=[self.FIXES, self.BREAKS], rejected=[])

    def _build(self, blocks):
        def build(edits, drop_fields=()):
            from synthesis.brief import apply_patch
            return pkg(apply_patch(blocks, edits))
        return build

    def _evaluate(self, calls=None):
        def evaluate(p):
            if calls is not None:
                calls.append(p.content)
            return attempt(
                p.content,
                soft=[("locale_spelling", "x")] if "REWRITTEN" in p.content else [],
                unresolved=[] if "THE FIX" in p.content else ["citation_integrity"])
        return evaluate

    def _run(self, calls=None):
        blocks = split_blocks(ORIGINAL)
        build, ev = self._build(blocks), self._evaluate(calls)
        return _drop_harmful_edits(self._patch(), ev(build(self._patch().edits)),
                                   ev, build)

    def test_the_damaging_edit_is_dropped(self):
        best = self._run()
        assert "REWRITTEN" not in best.package.content
        assert best.collateral == 0

    def test_the_fixing_edit_is_kept(self):
        best = self._run()
        assert "WITH THE FIX" in best.package.content
        assert best.unresolved == []

    def test_the_blocks_neither_edit_named_are_untouched(self):
        best = self._run()
        assert best.package.content.startswith("Intro paragraph.")
        assert best.package.content.endswith("Closing paragraph.")

    def test_it_rescores_once_per_edit_at_most(self):
        # This layer is only worth having because it is free. A patch is a
        # handful of edits; if this ever scales with the article it is not.
        calls = []
        self._run(calls)
        assert len(calls) <= 1 + 2      # the attempt itself, then one per edit

    def test_an_edit_carrying_both_the_fix_and_the_damage_is_kept(self):
        # Entangled: dropping it would clear the warning by throwing away
        # what the editor actually asked for. That case belongs to the retry.
        blocks = split_blocks(ORIGINAL)
        both = Edit(block=2, action="replace", text="Body, THE FIX, REWRITTEN.")
        patch = ArticlePatch(edits=[both], rejected=[])
        build, ev = self._build(blocks), self._evaluate()
        best = _drop_harmful_edits(patch, ev(build(patch.edits)), ev, build)
        assert "THE FIX" in best.package.content
        assert best.unresolved == []


class TestPatchedPackage:
    """A null field means keep. This is why a fix aimed at the body cannot
    disturb the SEO title, which is half the complaint."""

    class _Article:
        meta_title = "Existing Title"
        meta_description = "Existing description"
        excerpt = "Existing excerpt"
        cta = "Existing CTA"
        faq = [{"question": "q", "answer": "a"}]

    def test_null_fields_come_from_the_article(self):
        out = _patched_package(self._Article(), ["one", "two"],
                               ArticlePatch(edits=[], rejected=[]), [])
        assert out.meta_title == "Existing Title"
        assert out.meta_description == "Existing description"
        assert out.excerpt == "Existing excerpt"
        assert out.cta == "Existing CTA"
        assert out.faq == [{"question": "q", "answer": "a"}]

    def test_a_supplied_field_replaces_it(self):
        out = _patched_package(self._Article(), ["one"],
                               ArticlePatch(edits=[], rejected=[],
                                            meta_title="New Title"), [])
        assert out.meta_title == "New Title"
        assert out.meta_description == "Existing description"

    def test_an_empty_edit_list_reproduces_the_body_exactly(self):
        blocks = split_blocks(ORIGINAL)
        out = _patched_package(self._Article(), blocks,
                               ArticlePatch(edits=[], rejected=[]), [])
        assert out.content == ORIGINAL


class TestAHarmfulFieldChangeCanBeTakenBack:
    """The bug that made heading_length unfixable.

    Asked to shorten one over-long heading, the model also volunteered a new
    meta_title. Prepending "The " pushed it past 60 characters, _clamp_str cut
    the final word off, the target keyword stopped appearing verbatim, and
    single_search_intent — a mandatory REACS pillar — went from pass to fail.

    Only EDITS were droppable, so the meta_title could not be taken back. All
    three attempts were discarded for a regression none of them needed, taking
    with them a heading fix that was correct every time. Three calls spent,
    nothing changed, and nothing recorded either — the discard path rolls back
    before the attempt counter is written, which is why the panel never even
    warned that it had been tried.
    """

    KEYWORD = "panorama centralized security administration career"
    GOOD_META = "Complete Panorama Centralized Security Administration Career"
    CLAMPED = "The Complete Panorama Centralized Security Administration"

    class _Article:
        meta_title = "Complete Panorama Centralized Security Administration Career"
        meta_description = "d"
        excerpt = "e"
        cta = "c"
        faq = []

    def _run(self):
        from synthesis.brief import ArticlePatch, Edit, apply_patch
        blocks = ["Intro.", "## A very long heading indeed", "Body."]
        patch = ArticlePatch(
            edits=[Edit(block=2, action="replace", text="## Short heading")],
            rejected=[], meta_title=self.CLAMPED)

        def build(edits, drop_fields=()):
            return _patched_package(self._Article(), blocks, patch, edits,
                                    drop_fields)

        def evaluate(p):
            # Must carry the package it was given, not a copy of its text:
            # the whole point here is which non-body FIELDS survived.
            broke = self.KEYWORD not in (p.meta_title or "").lower()
            unfixed = "long heading" in p.content
            return _Attempt(
                package=p, model="m", report=None, reacs=None,
                blocking=[("single_search_intent", "x")] if broke else [],
                soft=[],
                unresolved=["heading_length"] if unfixed else [])

        return _drop_harmful_edits(patch, evaluate(build(patch.edits)),
                                   evaluate, build)

    def test_the_harmful_meta_title_is_dropped(self):
        best = self._run()
        assert best.package.meta_title == self.GOOD_META
        assert best.blocking == []

    def test_the_heading_fix_survives(self):
        # The whole point: the fix that was asked for must not be discarded
        # alongside the damage that came with it.
        best = self._run()
        assert "Short heading" in best.package.content
        assert best.unresolved == []

    def test_fields_the_patch_left_null_are_untouched(self):
        best = self._run()
        assert best.package.meta_description == "d"
        assert best.package.excerpt == "e"


class TestPatchedPackageDropFields:
    class _Article:
        meta_title = "kept title"
        meta_description = "kept description"
        excerpt = "kept excerpt"
        cta = "kept cta"
        faq = [{"question": "q", "answer": "a"}]

    def test_a_dropped_field_falls_back_to_the_article(self):
        from synthesis.brief import ArticlePatch
        patch = ArticlePatch(edits=[], rejected=[], meta_title="new title",
                             cta="new cta")
        out = _patched_package(self._Article(), ["a"], patch, [],
                               drop_fields={"meta_title"})
        assert out.meta_title == "kept title"     # dropped
        assert out.cta == "new cta"               # not dropped

    def test_dropping_nothing_keeps_every_change(self):
        from synthesis.brief import ArticlePatch
        patch = ArticlePatch(edits=[], rejected=[], meta_title="new title")
        out = _patched_package(self._Article(), ["a"], patch, [])
        assert out.meta_title == "new title"


class TestThePrePassCorrectionsSurvive:
    """A free fix outside the body must not be computed and then reverted.

    The pre-pass corrects the SEO title, meta description, excerpt, CTA and
    FAQ as well as the body, but only the body was carried forward: the
    corrected title was scored, reported to the model as already clean — so it
    was never asked to fix it — and then rebuilt from the article here. The
    check failed again and the panel told the editor it "did not clear last
    time". Measured on a Fortinet article whose meta title read
    "Fortimanager".
    """

    class _Article:
        meta_title = "Fortimanager Integration Guide 2026"
        meta_description = "fortimanager and fortianalyzer integration."
        excerpt = "A fortimanager overview."
        cta = "Explore our courses today."
        faq = [{"question": "What is fortimanager?", "answer": "A thing."}]

    CORRECTED = {
        "meta_title": "FortiManager Integration Guide 2026",
        "meta_description": "FortiManager and FortiAnalyzer integration.",
        "excerpt": "A FortiManager overview.",
        "cta": "Explore our courses today.",
        "faq": [{"question": "What is FortiManager?", "answer": "A thing."}],
    }

    def _patch(self, **fields):
        from synthesis.brief import ArticlePatch
        return ArticlePatch(edits=[], rejected=[], **fields)

    def test_a_body_only_patch_keeps_the_corrected_title(self):
        out = _patched_package(self._Article(), ["Body."], self._patch(), [],
                               base=self.CORRECTED)
        assert out.meta_title == "FortiManager Integration Guide 2026"
        assert out.meta_description == "FortiManager and FortiAnalyzer integration."
        assert out.excerpt == "A FortiManager overview."
        assert out.faq == [{"question": "What is FortiManager?", "answer": "A thing."}]

    def test_the_model_still_wins_when_it_sets_the_field(self):
        # The pre-pass is a fallback, not an override: an edit the model was
        # asked for must not be reverted to the mechanical version.
        out = _patched_package(self._Article(), ["Body."],
                               self._patch(meta_title="A Better Title 2026"),
                               [], base=self.CORRECTED)
        assert out.meta_title == "A Better Title 2026"

    def test_a_dropped_field_falls_back_to_the_correction_not_the_original(self):
        # _drop_harmful_edits takes back a damaging field change; what it
        # lands on should be the corrected value, never the miscased one.
        out = _patched_package(self._Article(), ["Body."],
                               self._patch(meta_title="Broken Title"), [],
                               drop_fields={"meta_title"}, base=self.CORRECTED)
        assert out.meta_title == "FortiManager Integration Guide 2026"

    def test_without_a_base_the_article_is_still_the_fallback(self):
        # Every other caller passes no base and must behave exactly as before.
        out = _patched_package(self._Article(), ["Body."], self._patch(), [])
        assert out.meta_title == "Fortimanager Integration Guide 2026"


class TestClampDoesNotGuillotineWords:
    def test_a_title_is_cut_at_a_word_boundary(self):
        from synthesis.brief import _clamp_str
        t = "The Complete Panorama Centralized Security Administration Career"
        out = _clamp_str(t, 60)
        assert not out.endswith("Ca")
        assert out == "The Complete Panorama Centralized Security Administration"

    def test_a_string_with_no_late_space_is_still_hard_cut(self):
        # Backing up to a distant space would lose far more than it saves.
        from synthesis.brief import _clamp_str
        assert len(_clamp_str("a" * 90, 60)) == 60

    def test_a_short_string_is_untouched(self):
        from synthesis.brief import _clamp_str
        assert _clamp_str("  short  ", 60) == "short"


class TestTheWriterIsToldWhatNotToBreak:
    """A fix discarded for breaking something it was never warned about.

    Twice, measured: a fix to entity_type rewrote the opening and broke
    direct_answer_first (70-word ceiling, keyword verbatim); a fix to
    heading_length rewrote the SEO title and broke single_search_intent. Both
    mandatory, so every attempt was discarded and the article rolled back —
    three model calls, no change, check still failing.

    The writer was told what to fix and nothing about what to preserve.
    """

    class _Check:
        def __init__(self, pillar, tier, passed, message):
            self.pillar, self.tier = pillar, tier
            self.passed, self.message = passed, message

    class _Report:
        def __init__(self, checks):
            self.checks = checks

    def _report(self):
        C = self._Check
        return self._Report([
            C("direct_answer_first", "mandatory", True,
              "Opening is a 44-word direct answer naming the keyword."),
            C("single_search_intent", "mandatory", True,
              "Keyword present in both the H1 and the meta title."),
            C("comparison_table", "mandatory", False, "No table found."),
            C("entity_coverage", "advisory", True, "6 of 9 entities mentioned."),
        ])

    def test_passing_mandatory_pillars_are_named(self):
        from webapp.pipeline import _constraints_to_keep
        out = _constraints_to_keep(self._report())
        assert "direct_answer_first" in out
        assert "44-word direct answer" in out

    def test_a_failing_pillar_is_not_listed_as_a_constraint(self):
        # It is what the fix may need to change.
        from webapp.pipeline import _constraints_to_keep
        assert "comparison_table" not in _constraints_to_keep(self._report())

    def test_advisory_pillars_are_left_out(self):
        # Only mandatory breaches discard the attempt, and the list has to
        # stay short enough to be read.
        from webapp.pipeline import _constraints_to_keep
        assert "entity_coverage" not in _constraints_to_keep(self._report())

    def test_it_says_what_happens_if_one_breaks(self):
        from webapp.pipeline import _constraints_to_keep
        assert "discarded" in _constraints_to_keep(self._report())

    def test_nothing_is_added_when_no_pillar_passes(self):
        from webapp.pipeline import _constraints_to_keep
        empty = self._Report([self._Check("x", "mandatory", False, "no")])
        assert _constraints_to_keep(empty) == ""
