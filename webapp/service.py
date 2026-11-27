"""Generation service: the single call the web layer makes into the pipeline.

Reuses the exact same functions the CLIs use, so the web app can never drift
from `run.py` / `generate.py`:
  demo mode -> fixtures -> verify -> mock_generate -> validate
  live mode -> retrieve -> verify -> generate_passing_draft (LLM + QA retry)
"""
from __future__ import annotations

from dataclasses import asdict

from generate import _load_fixture_bundle          # reuse CLI's fixture loader
from verification import verify
from synthesis import mock_generate, validate_draft, generate_passing_draft
# Not re-exported by the package, and the demo branch below needs it: without
# this import every demo-mode generation died with NameError, which the route's
# blanket except turned into "the operation failed, check the logs".
from synthesis.validate import QUICK_PROFILE

DEFAULT_FIXTURES = "fixtures/sample_sources.json"
LIVE_SOURCE_LIMIT = 8


def run_generation(topic: str, region: str | None, mode: str,
                   fixtures: str = DEFAULT_FIXTURES, on_progress=None,
                   keywords: list[str] | None = None):
    """Return (draft, report, bundle) for the given request.

    Raises on failure (e.g. missing API keys in live mode); the caller turns
    that into a friendly error for the browser.

    `on_progress(step, status, message, **extra)` is called at each pipeline
    stage, if given, so a caller (the web layer) can stream real step-by-step
    progress instead of blocking silently until the whole thing finishes.
    Steps: retrieve/load -> verify -> generate -> qa. status is one of
    "active" / "done" / "warn" / "error".

    `keywords`, if given, are required SEO terms the draft must contain —
    enforced by the QA harness's keyword_coverage check in both modes.
    """
    def emit(step, status, message, **extra):
        if on_progress:
            on_progress(step, status, message, **extra)

    region = region or None

    if mode == "live":
        from retrieval import retrieve                # lazy: needs requests/trafilatura

        # allow_open_web is Quick draft's ONLY difference from Article Studio,
        # and it is passed here and nowhere else. The allowlist is 61
        # IT-security and vendor-training domains: "cycling tips" searched
        # every one of them, matched nothing, and had every real result
        # refused at the fetch gate, so Live mode could only ever work for
        # vendor topics. This page already says it is ungoverned and saves
        # nothing; Studio's governed path keeps the allowlist untouched.
        #
        # It only takes effect when NOTHING allowlisted ranks for the topic,
        # so an IT subject typed here is still researched from the curated
        # domains exactly as before.
        emit("retrieve", "active",
             "Searching trusted sources — the open web if the topic is "
             "outside them…")
        sources, dropped = retrieve(topic, limit=LIVE_SOURCE_LIMIT,
                                    allow_open_web=True)
        off_list = [s for s in sources if s.tier >= 3]
        emit("retrieve", "done",
             f"Found {len(sources)} usable source(s)"
             + (f", {len(off_list)} from the open web" if off_list else "")
             + (f" ({len(dropped)} dropped)" if dropped else ""))

        emit("verify", "active", "Verifying claims & cross-source corroboration…")
        bundle = verify(topic, region, sources, dropped_in=dropped)
        emit("verify", "done", f"{len(bundle.claims)} claim(s) extracted")

        # Regenerate on a QA block (free models trip the checks more often).
        # generate_passing_draft's on_progress signature matches ours exactly
        # (step, status, message, **extra), so it's passed straight through.
        result = generate_passing_draft(bundle, on_progress=on_progress, keywords=keywords)
        if result is None:
            raise RuntimeError("Generation failed on every attempt.")
        draft, report = result
    else:  # demo / offline
        emit("load", "active", "Loading bundled sample sources…")
        bundle = _load_fixture_bundle(fixtures, region)
        emit("load", "done", f"{len(bundle.sources)} sample source(s) loaded")

        emit("verify", "done", f"{len(bundle.claims)} claim(s) extracted")

        emit("generate", "active", "Generating draft…")
        draft = mock_generate(bundle, keywords=keywords)
        emit("generate", "done", "Draft generated")

        emit("qa", "active", "Running reliability QA…")
        # Same profile as the live path: Quick draft is one product, and demo
        # mode differing from live on which rules apply would be a trap.
        report = validate_draft(draft, bundle, keywords=keywords,
                                profile=QUICK_PROFILE)
        emit("qa", "done" if report.passed else "warn",
             "All QA checks passed" if report.passed
             else f"{len(report.failed)} check(s) failed")

    return draft, report, bundle


def draft_to_dict(draft, body_html: str) -> dict:
    return {
        "topic": draft.topic,
        "region": draft.region,
        "title": draft.title,
        "slug": draft.slug,
        "meta_description": draft.meta_description,
        "meta_len": len(draft.meta_description),
        "word_count": draft.word_count(),
        "body_markdown": draft.body_markdown,
        "body_html": body_html,
        "references": [asdict(r) for r in draft.references],
        "faq": draft.faq,
    }


def qa_to_dict(report) -> dict:
    return {
        "checks": [asdict(c) for c in report.checks],
        "passed": report.passed,
        "failed": len(report.failed),
        "warned": len(report.warned),
    }
