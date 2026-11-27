"""Push a Quick draft to WordPress as a draft post.

Deliberately separate from webapp/pipeline.py's push_to_wordpress. That one
works from an Article row — it re-scores from stored sources, checks REACS,
records wp_post_id and pushed_at, and refuses a second push by looking the
existing post up. None of that exists here: a Quick draft is never persisted,
which is the promise the page makes ("Nothing here is saved"), so there is no
row to re-score, no REACS verdict, and nowhere to record a post id.

What IS shared is publish/ — the payload builder and the WordPress client.
That is transport, not policy, and build_post_payload hard-codes
status="draft" with no parameter to change it, so this path cannot publish
live either.
"""
from __future__ import annotations

import logging

# The store lives in quick_drafts because the rewrite and proofread buttons
# act on the same held draft. One place, or a push and a rewrite could end up
# looking at different copies of the same article.
from .quick_drafts import QuickDraftError, get_draft

logger = logging.getLogger(__name__)


class QuickPublishError(QuickDraftError):
    """Kept as its own type so callers can catch publish failures alone."""

def quick_payload(draft, keywords: list[str] | None = None) -> dict:
    """The exact JSON body this draft would send to WordPress.

    Separate assembly from wp_payload_for, because a Quick draft has none of
    the things that one reads: no vendor, so no category; no byline
    instructor, so no author and no reviewed-by line.

    The FAQ keys are translated, and that is not cosmetic. The ad-hoc
    pipeline's schema emits {"q", "a"} (synthesis/style.py) while
    build_post_payload and build_schema read {"question", "answer"} — passing
    it through untouched produces a post whose FAQ accordion has no questions
    and whose FAQPage schema is empty, silently.
    """
    from publish import build_post_payload
    from synthesis.text import slugify

    faq = [{"question": f.get("question") or f.get("q") or "",
            "answer": f.get("answer") or f.get("a") or ""}
           for f in (draft.faq or [])]
    faq = [f for f in faq if f["question"] and f["answer"]]

    return build_post_payload(
        title=draft.title,
        slug=draft.slug or slugify(draft.title),
        content_markdown=draft.body_markdown or "",
        # A Draft carries no excerpt or meta_title of its own; the meta
        # description is the one summary it does have, and the title is a
        # better SEO title than an empty string.
        excerpt=draft.meta_description or "",
        meta_title=draft.title,
        meta_description=draft.meta_description or "",
        keyword=(keywords or [""])[0],
        faq=faq,
        category_ids=[],        # no vendor, so nothing to file it under
        author_id=None,         # no instructor roster on this path
        byline=None)            # and therefore no "Technically reviewed by"


def push_quick_draft(draft_id: str) -> dict:
    """Create the WordPress draft. Returns {"post_id", "link"}.

    Gated on QA passing, which is the same bar push_to_wordpress applies —
    "nothing is broken", not "nothing warns". Studio also requires REACS;
    there is no REACS verdict on this path, so QA is the whole gate and the
    error says so rather than implying a check ran.
    """
    from publish import create_draft_post, set_rankmath_meta, wordpress_configured

    entry = get_draft(draft_id)
    if entry is None:
        raise QuickPublishError(
            "That draft has expired. Quick drafts are held for an hour and "
            "never saved — generate it again to push it.")
    if not wordpress_configured():
        raise QuickPublishError(
            "WordPress credentials are not configured on this server.")

    draft, report = entry["draft"], entry["report"]
    if not report.passed:
        failed = ", ".join(sorted({c.name for c in report.checks
                                   if c.status == "fail"}))
        raise QuickPublishError(
            f"Reliability QA is not passing ({failed}). Nothing reaches "
            "WordPress until it does.")
    if entry.get("wp_post_id"):
        raise QuickPublishError(
            f"Already pushed as WordPress post {entry['wp_post_id']}. Edit it "
            "there — pushing again would create a duplicate rather than "
            "update it.")

    try:
        created = create_draft_post(quick_payload(draft, entry.get("keywords")))
    except Exception as exc:
        raise QuickPublishError(str(exc)) from exc

    post_id = created.get("id")
    # Best-effort and AFTER the post exists, exactly as the Studio path does:
    # a missing focus keyword is a warning, not a reason to lose the draft.
    try:
        set_rankmath_meta(post_id, keyword=(entry.get("keywords") or [""])[0],
                          title=draft.title,
                          description=draft.meta_description or "")
    except Exception:
        logger.exception("Rank Math meta not set for post %s", post_id)

    link = created.get("link") or (created.get("guid") or {}).get("rendered", "")
    entry["wp_post_id"] = post_id
    entry["wp_link"] = link
    return {"post_id": post_id, "link": link}
