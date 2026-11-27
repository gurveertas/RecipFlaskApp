"""Quick drafts held in memory, and the edits that can be made to them.

A Quick draft is never persisted — that is the promise the page makes — so
there is no Article row to edit, no session to commit, and nothing to come
back to tomorrow. The finished draft is kept here for an hour so the buttons
on the results page have something to act on.

Separate from webapp/pipeline.py's rewrite_section and run_prose_review for
the same reason quick_publish is separate from push_to_wordpress: those read
an Article's stored brief, vendor and references. None of that exists here.
What is shared is synthesis/ — the prompt builders, the splice, the QA
harness — because that is the pipeline itself, and the point is that a Quick
draft goes through the same one.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid

logger = logging.getLogger(__name__)

_DRAFT_TTL_SECONDS = 60 * 60
_drafts_lock = threading.Lock()
_drafts: dict[str, dict] = {}


class QuickDraftError(RuntimeError):
    """Anything the operator should read as a sentence, not a traceback."""


def remember_draft(draft, report, keywords=None, bundle=None) -> str:
    """Hold a finished draft so the results page can act on it."""
    draft_id = uuid.uuid4().hex
    with _drafts_lock:
        _prune_locked()
        _drafts[draft_id] = {"draft": draft, "report": report,
                             "keywords": list(keywords or []),
                             # Kept because a section rewrite has to be
                             # grounded in the SAME sources the draft already
                             # cites, exactly as the Studio path rebuilds a
                             # bundle from an article's stored references.
                             "bundle": bundle,
                             "created": time.time()}
    return draft_id


def _prune_locked() -> None:
    cutoff = time.time() - _DRAFT_TTL_SECONDS
    for stale in [k for k, v in _drafts.items() if v["created"] < cutoff]:
        _drafts.pop(stale, None)


def get_draft(draft_id: str) -> dict | None:
    with _drafts_lock:
        _prune_locked()
        return _drafts.get(draft_id)


def _require(draft_id: str) -> dict:
    entry = get_draft(draft_id)
    if entry is None:
        raise QuickDraftError(
            "That draft has expired. Quick drafts are held for an hour and "
            "never saved — generate it again to keep working on it.")
    return entry


def section_headings(draft) -> list[str]:
    """The H2 headings a rewrite can target, in order."""
    import re
    return [m.group(1).strip() for m in
            re.finditer(r"^##\s+(.+)$", draft.body_markdown or "", re.M)]


def rewrite_quick_section(draft_id: str, heading: str,
                          instruction: str | None = None) -> dict:
    """Rewrite ONE H2 in place, then re-run QA on the spliced draft.

    One LLM call and no retry loop, same as the Studio path: QA reports
    honestly and the operator decides whether to go again.

    The ContentBrief is synthesised rather than loaded — a Quick draft has no
    brief — but it is the real type, so the prompt builder is the same one
    Studio uses and the two cannot drift on what a section rewrite is asked
    to do.
    """
    from synthesis.brief import (ContentBrief, build_section_rewrite_prompt,
                                 parse_section_response, replace_section)
    from synthesis.llm import complete_with_configured_provider
    from synthesis.text import refresh_last_updated
    from synthesis.validate import QUICK_PROFILE, validate_draft

    entry = _require(draft_id)
    draft, bundle = entry["draft"], entry["bundle"]
    heading = (heading or "").strip()
    if not heading:
        raise QuickDraftError("Pick a section to rewrite.")
    # Line-anchored, the same rule replace_section uses — a substring check
    # could match "## X" mid-line and burn a call before the splice fails.
    if not any(ln.strip() == f"## {heading}"
               for ln in (draft.body_markdown or "").splitlines()):
        raise QuickDraftError(f'No section titled "{heading}" is in this draft.')
    if bundle is None:
        raise QuickDraftError(
            "This draft's sources are no longer in memory, so a rewrite "
            "could not be grounded in them. Generate it again.")

    brief = ContentBrief(
        title=draft.title, audience="", keyword=(entry.get("keywords") or [""])[0],
        format="explainer", outline=section_headings(draft))
    system_prompt, user_prompt, _tags = build_section_rewrite_prompt(
        draft.body_markdown, heading, brief, bundle, vendor_name="",
        instruction=instruction)
    raw, _model = complete_with_configured_provider(
        system_prompt, user_prompt, purpose="quick_section_rewrite")

    ok, result = parse_section_response(raw, heading)
    if not ok:
        raise QuickDraftError(f"Section rewrite failed: {result}")
    spliced = replace_section(draft.body_markdown, heading, result)
    if spliced is None:
        raise QuickDraftError(f'No section titled "{heading}" is in this draft.')

    draft.body_markdown = refresh_last_updated(spliced)
    # The FULL harness on the spliced draft: a section edit can break
    # article-level invariants (length, keyword coverage), so the verdict is
    # recomputed rather than carried over.
    entry["report"] = validate_draft(draft, bundle,
                                     keywords=entry.get("keywords") or None,
                                     profile=QUICK_PROFILE)
    entry["prose_review"] = None          # the text changed; the notes are stale
    return entry


def proofread_quick_draft(draft_id: str) -> list[dict]:
    """Advisory proofread of the draft body. One LLM call, on demand.

    No pass/fail and no gating flag, exactly as the Studio proofread: nothing
    in this pipeline blocks on a model's opinion of the writing.
    """
    from synthesis.prose import review_prose

    entry = _require(draft_id)
    body = (entry["draft"].body_markdown or "").strip()
    if not body:
        raise QuickDraftError("This draft has no body to review yet.")
    try:
        issues, _model = review_prose(body)      # no vendor on this path
    except Exception as exc:
        # "No issues found" and "the review did not run" must never look the
        # same — see synthesis/prose.review_prose.
        raise QuickDraftError(f"The proofread did not run: {exc}") from exc

    entry["prose_review"] = [
        {"quote": i.quote, "problem": i.problem,
         "suggestion": i.suggestion, "kind": i.kind} for i in issues]
    return entry["prose_review"]
