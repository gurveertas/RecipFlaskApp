"""Quick draft and Article Studio are held to different standards.

Studio produces governed, vendor-tied, publishable articles. Quick draft says
on its own page that it takes any topic, saves nothing, and needs no vendor or
brief. Holding the second to the first's standard produced two failures that
were not defects: a 1300-word floor on "a draft in seconds", and — on an
article about cycling — "no inline Red Education course link", which BLOCKED.

Every test here checks both directions. The point is not that Quick draft is
easier; it is that Studio must be exactly as it was.
"""
import pytest

from retrieval.models import VerifiedBundle
from synthesis.models import Draft, Reference
from synthesis.validate import (QUICK_PROFILE, STUDIO_PROFILE, QaProfile,
                                validate_draft)


SENTENCE = ("Cycling tips for new riders begin with a proper bike fit and a "
            "saddle height that leaves the knee slightly bent. ")          # 20 words


def draft_of(words: int, *, title="Essential Cycling Tips"):
    """A draft of roughly `words` words with real H2 headings.

    Headings must start a LINE — an inline "##" is not a heading, and a body
    without any silently skips heading_length, which is how the first version
    of this helper produced a passing test that proved nothing.
    """
    per_section = 10
    sections = max(1, round(words / 20) // per_section)
    body = ""
    for i in range(sections):
        body += f"## Section {i + 1}\n\n" + SENTENCE * per_section + "\n\n"
    return Draft(topic="Cycling tips", region=None, title=title, slug="c",
                 meta_description="Cycling tips for new riders.",
                 body_markdown=body, references=[])


def run(d, profile, *, curated: bool = False):
    """Validate `d`. `curated` says whether the bundle holds an allowlisted
    source, which is what decides if a course CTA is expected at all."""
    from retrieval.models import Source
    tier = 1 if curated else 3
    bundle = VerifiedBundle(
        topic="t", region=None, claims=[],
        sources=[Source(url="https://x/", domain="x", title="t", text="body",
                        tier=tier)])
    return validate_draft(d, bundle, profile=profile)


def status_of(report, name):
    return next((c.status for c in report.checks if c.name == name), None)


class TestTheStudioProfileIsUnchanged:
    def test_it_is_the_default_when_no_profile_is_passed(self):
        d = draft_of(200)
        assert status_of(run(d, None), "structure_length") == \
            status_of(run(d, STUDIO_PROFILE), "structure_length")

    def test_the_word_floor_is_still_1300(self):
        assert STUDIO_PROFILE.min_words == 1300
        assert STUDIO_PROFILE.target_words == (1500, 2100)

    def test_the_course_cta_is_still_required(self):
        # A Studio bundle can only ever hold allowlisted sources, so it is
        # always "our subject" and the check always runs.
        assert status_of(run(draft_of(1400), STUDIO_PROFILE, curated=True),
                         "course_link_cta") == "fail"

    def test_studio_call_sites_do_not_pass_a_profile(self):
        # brief.py is the governed path. If it ever starts choosing a profile,
        # that is a change to Studio and this should be the thing that says so.
        import inspect
        from synthesis import brief
        src = inspect.getsource(brief.generate_passing_article)
        assert "profile=" not in src


class TestTheQuickProfile:
    def test_the_course_cta_check_does_not_run_for_a_foreign_subject(self):
        # Not "passes" — absent. Requiring a Red Education course link in an
        # article about cycling is the wrong question, not a lowered bar.
        assert status_of(run(draft_of(1000), QUICK_PROFILE, curated=False),
                         "course_link_cta") is None

    def test_the_course_cta_IS_checked_for_a_cyber_subject(self):
        # A cyber topic typed into Quick draft should still carry one — the
        # rule follows the subject, not the page.
        assert status_of(run(draft_of(1000), QUICK_PROFILE, curated=True),
                         "course_link_cta") == "fail"

    def test_a_short_draft_is_accepted(self):
        # Not blocked. It may still warn for being under the target band —
        # the point is that it is no longer a FAIL that stops the draft.
        assert status_of(run(draft_of(1000), QUICK_PROFILE),
                         "structure_length") != "fail"

    def test_but_a_very_short_draft_is_still_short(self):
        # A floor, not an aspiration.
        assert status_of(run(draft_of(120), QUICK_PROFILE),
                         "structure_length") == "fail"

    def test_the_same_draft_fails_under_studio(self):
        d = draft_of(1000)
        assert status_of(run(d, STUDIO_PROFILE), "structure_length") == "fail"
        assert status_of(run(d, QUICK_PROFILE), "structure_length") != "fail"


class TestWhatIsDeliberatelyNotConfigurable:
    """A profile that could switch grounding off would be a way to publish
    unchecked prose, which is the opposite of the point."""

    @pytest.mark.parametrize("field", ["grounding", "originality",
                                       "citation_integrity", "locale_spelling",
                                       "writing_mechanics", "heading_length"])
    def test_integrity_checks_run_under_both_profiles(self, field):
        d = draft_of(1000)
        assert status_of(run(d, QUICK_PROFILE), field) is not None
        assert status_of(run(d, STUDIO_PROFILE), field) is not None

    def test_the_profile_has_no_switch_for_them(self):
        # The course CTA is deliberately absent too: whether an article should
        # advertise a course is a question about the SUBJECT, not about which
        # page produced it.
        assert set(QaProfile.__dataclass_fields__) == {
            "name", "min_words", "target_words"}


class TestTheAdHocPipelineUsesIt:
    def test_generate_passing_draft_defaults_to_quick(self):
        import inspect
        from synthesis import generate
        src = inspect.getsource(generate.generate_passing_draft)
        assert "profile or QUICK_PROFILE" in src

    def test_the_demo_path_uses_the_same_profile_as_live(self):
        # Demo differing from live on which rules apply would be a trap.
        import inspect
        from webapp import service
        assert "profile=QUICK_PROFILE" in inspect.getsource(service.run_generation)


class TestTheWriterIsOnlyAskedForACtaWhenItBelongs:
    """The prompt and the check read the same signal, so they cannot disagree
    about whether the article was supposed to carry a course link."""

    def _linking(self, *tiers):
        from retrieval.models import Source, VerifiedBundle
        from synthesis.prompt import _linking_block
        b = VerifiedBundle(topic="cycling tips", region=None, sources=[
            Source(url="https://x/", domain="x", title="t", text="b", tier=t)
            for t in tiers])
        return _linking_block(b)

    def test_no_cta_instruction_for_a_foreign_subject(self):
        assert self._linking(3, 3) == ""

    def test_a_cta_instruction_for_a_cyber_subject(self):
        out = self._linking(1, 3)
        assert "rededucation.com" in out.lower()

    def test_one_allowlisted_source_is_enough(self):
        # Mixed bundles happen: the open-web channel supplements a scoped
        # search. One curated source means the topic is ours.
        assert self._linking(3, 2) != ""


class TestArticleStudioIsUntouched:
    """The whole change rests on one claim: a Studio bundle is always "our
    subject", so both guards are no-ops there.

    That is not a matter of taste — Studio's retrieval never passes
    allow_open_web, so fetch_source refuses every domain off the allowlist and
    every source it can possibly hold is tier 1 or 2.
    """

    def _studio_bundle(self):
        from retrieval.models import Source, VerifiedBundle
        return VerifiedBundle(topic="check point ccse", region=None, sources=[
            Source(url="https://blog.checkpoint.com/a", domain="blog.checkpoint.com",
                   title="t", text="body", tier=1),
            Source(url="https://www.pearsonvue.com/b", domain="www.pearsonvue.com",
                   title="t", text="body", tier=2)])

    def test_a_studio_bundle_is_always_our_subject(self):
        assert self._studio_bundle().is_our_subject() is True

    def test_the_writer_is_still_asked_for_a_course_cta(self):
        from synthesis.prompt import _linking_block
        out = _linking_block(self._studio_bundle(), "Check Point")
        assert "rededucation.com" in out.lower()
        assert out.strip() != ""

    def test_the_course_check_still_runs(self):
        from synthesis.validate import validate_draft
        rep = validate_draft(draft_of(1400), self._studio_bundle(),
                             profile=STUDIO_PROFILE)
        assert status_of(rep, "course_link_cta") == "fail"

    def test_studio_retrieval_cannot_produce_an_open_web_source(self):
        # The guarantee underneath: _live_bundle never opts in, so nothing
        # off the allowlist can reach a Studio bundle in the first place.
        import inspect
        from webapp import pipeline
        assert "allow_open_web" not in inspect.getsource(pipeline._live_bundle)

    def test_the_only_change_to_the_linking_block_is_an_early_return(self):
        # If the body of _linking_block ever diverges for a curated bundle,
        # Studio's prompt has changed and this is the thing that says so.
        from retrieval.models import Source, VerifiedBundle
        from synthesis.prompt import _linking_block
        curated = self._studio_bundle()
        # Same bundle with one extra open-web source is still "ours", so the
        # output must be identical — the guard is the only difference.
        mixed = VerifiedBundle(topic=curated.topic, region=None,
                               sources=curated.sources + [
                                   Source(url="https://x/", domain="x", title="t",
                                          text="b", tier=3)])
        assert _linking_block(curated, "Check Point") == \
            _linking_block(mixed, "Check Point")
