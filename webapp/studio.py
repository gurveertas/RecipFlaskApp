"""Article Studio: the primary content-creation UX — one guided flow
(Vendor -> Topic -> Brief -> Article -> Publish) replacing the three separate
destinations (Vendor Centre, Topic Engine, Brief+Writer) as the main way to
go from "which vendor" to "a QA'd article". Each step's approved output
(vendor id, topic id, article id) carries forward as the next step's input.

Vendor/cert/course CRUD isn't gone — it's still webapp/admin.py's
/admin/vendors pages, just no longer a top-level nav destination; Step 1
links to it as a "Manage vendors" drawer for setup work, off the main path.

Reuses webapp/pipeline.py's shared orchestration (generate_topics_for_vendor,
create_brief_for_topic, write_article) — the exact same governed logic the
classic /admin/topics and /admin/articles pages call, so the two UIs can't
silently drift apart on governance behavior.

The two suggestion surfaces ("applicable steps... suggestions based on
existing blogs") are the Topic step (a gap/overlap tag per topic, from
synthesis/gaps.py's best_match against real rededucation.com posts) and the
Brief step (candidate internal links, from the same module's related_posts).
Vendor and Article steps deliberately don't get this treatment — see each
step's own comment for why.
"""
from __future__ import annotations

import io
import json
from collections import Counter

from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   send_file, url_for)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError



from config.db import (AppConfig, Article, ArticleStatus, CertificationStatus,
                       Topic, TopicPriority, Vendor, get_session)
from synthesis.gaps import published_counts_by_vendor, best_match, related_posts
from synthesis.text import clamp_title, title_readability_issues
from publish import wordpress_configured
from .pdf import html_to_pdf
from .render import render_markdown, render_site_preview
from .pipeline import (wp_author_for, wp_category_for, wp_edit_link,
                       wp_payload_for,
                       push_to_wordpress, PipelineError, create_brief_for_topic, fix_issues,
                       generate_topics_for_vendor, rewrite_section,
                       apply_manual_edit, apply_prose_fixes, rescore_article,
                       run_prose_review, safe_error, unfixable_checks,
                       write_article)

studio_bp = Blueprint("studio", __name__, url_prefix="/studio")

# How many internal-link candidates the Brief step offers.
SUGGESTION_COUNT = 5


# The five stages an article can be in, in the order it moves through them.
# ONE definition, because the vendor card counts them and the topic list
# labels them, and two implementations of "is this ready" would eventually
# disagree on the same screen.
#
# Mutually exclusive by construction: an article is in exactly one, so the
# card's buckets still partition the total. Order matters — a live article is
# also pushed, and a pushed one is also QA-passed, so the most advanced state
# has to win.
ARTICLE_STAGES = ("brief", "needs fixes", "ready", "awaiting publish", "live")


def article_stage(article) -> str:
    """Which stage `article` is in. See ARTICLE_STAGES."""
    if article.published_at is not None:
        return "live"                      # a human published it
    if article.wp_post_id is not None:
        return "awaiting publish"          # sitting in WordPress as a draft
    if article.status == ArticleStatus.BRIEF:
        return "brief"                     # never written
    return "ready" if (article.qa_passed and article.reacs_passed) \
        else "needs fixes"                 # written, a gate is holding it


def _preferred_article(articles) -> Article | None:
    """Choose completed work over an empty brief if legacy duplicates exist."""
    return max(
        articles,
        key=lambda article: (
            article.status == ArticleStatus.DRAFT,
            article.id,
        ),
        default=None,
    )


# ============================================================ STEP 1: VENDOR
@studio_bp.get("")
def vendor_step():
    # No existing-post suggestions here — content gaps are only meaningful
    # once a topic is on the table (see topic_step below).
    with get_session() as s:
        vendors = s.query(Vendor).order_by(
            Vendor.priority.is_(None), Vendor.priority,
            Vendor.display_rank.is_(None), Vendor.display_rank, Vendor.name).all()

        articles_by_vendor: dict[int, list[Article]] = {}
        for a in s.query(Article).all():
            articles_by_vendor.setdefault(a.vendor_id, []).append(a)

        # What already exists ON THE SITE for each vendor. Without this the
        # card only knew about work started in this tool, so every vendor read
        # "0 articles" — identical for Palo Alto Networks, which has 43 live
        # posts, and Ivanti, which has none. That is the single most useful
        # signal for choosing what to write next.
        published = published_counts_by_vendor(s)

        stats = {}
        for v in vendors:
            arts = articles_by_vendor.get(v.id, [])
            # Three real stages, not a flat count that hides where the work
            # actually is: still at brief (never written), written but QA
            # blocked (needs fixes), or QA-passed (a genuine publish
            # candidate — still not "published", Stage 7 doesn't exist yet).
            # MUTUALLY EXCLUSIVE stages that partition the total — one
            # article appears in exactly one bucket, and brief + blocked +
            # ready + pushed == articles. Adding "pushed" as an independent
            # count broke that: a pushed article is also QA-passed, so Avaya's
            # single article rendered as "1 in this tool · 1 pushed · 1 ready"
            # and read like three.
            #
            # "ready" means genuinely publishable — BOTH gates, matching what
            # the Publish step actually requires. Anything written but not yet
            # publishable is blocked, whichever gate is holding it.
            staged = Counter(article_stage(a) for a in arts)
            brief = staged["brief"]
            blocked = staged["needs fixes"]
            ready = staged["ready"]
            # "pushed" splits again now that sync_wp_state.py observes what a
            # human did with the draft: sitting in WordPress unpublished, or
            # actually live. Kept mutually exclusive so the buckets still
            # partition the total — a live article is no longer counted as
            # pushed, or one article would render as both.
            live = staged["live"]
            pushed = staged["awaiting publish"]
            # CURRENT and a real CREDENTIAL — the same two conditions
            # _vendor_cert_context applies before handing this list to the
            # Topic Engine. Counting every row instead had the card and the
            # engine disagreeing: Check Point advertised 14 when 4 were
            # retired (two of them older versions of credentials already
            # counted as current, so the same exam appeared twice), and Cisco
            # advertised 15 when every one of them is a training course and
            # the engine correctly sees no credentials at all. Overstated for
            # 10 of 14 vendors.
            #
            # Courses stay a count of every course row: a course hanging off a
            # retired or course-shaped certification is still a real course we
            # sell, and it is the honest other half of "0 certifications".
            sellable = [c for c in v.certifications
                        if c.status == CertificationStatus.CURRENT
                        and c.is_credential]
            stats[v.id] = {
                "certifications": len(sellable),
                "courses": sum(len(c.courses) for c in v.certifications),
                "articles": len(arts),
                "brief": brief, "blocked": blocked, "ready": ready,
                # Pushed to WordPress as a draft — a real state now Stage 7
                # exists, and distinct from "ready" (QA-passed but never sent).
                "pushed": pushed,
                # Confirmed live on the site by sync_wp_state.py.
                "live": live,
                "published": published.get(v.id, 0),
            }
        # For the danger-zone purge confirmation — real current totals, not
        # a guess, so the confirm dialog states exactly what's about to go.
        topics_total = s.query(Topic).count()
        articles_total = s.query(Article).count()
        # These counts are only as current as the last site scrape, so say
        # when that was. Undated, a six-week-old number looks exactly like a
        # fresh one — and this screen's whole job is choosing from them.
        cfg = s.get(AppConfig, 1)

        return render_template(
            "studio/vendor.html", vendors=vendors, stats=stats,
            coverage_scraped_at=(cfg.coverage_scraped_at if cfg else None),
            topics_total=topics_total, articles_total=articles_total)


@studio_bp.post("/purge")
def purge():
    """Danger-zone reset: delete every Topic and every Article, across every
    vendor. Articles are deleted first — Topic is RESTRICT-referenced by
    Article.topic_id under PRAGMA foreign_keys=ON, so deleting topics first
    would raise. Vendors, certifications, courses, and instructors are the
    Knowledge Base this content is built FROM, not generated content
    themselves — deliberately untouched."""
    with get_session() as s:
        n_articles = s.query(Article).delete(synchronize_session=False)
        n_topics = s.query(Topic).delete(synchronize_session=False)
        s.commit()
    flash(f"Purged {n_articles} article(s) and {n_topics} topic idea(s). "
          "Vendors, certifications, and courses are untouched.", "success")
    return redirect(url_for("studio.vendor_step"))


# ============================================================= STEP 2: TOPIC
@studio_bp.get("/vendor/<int:vendor_id>/topics")
def topics_for_vendor(vendor_id: int):
    with get_session() as s:
        vendor = s.get(Vendor, vendor_id)
        if vendor is None:
            abort(404)
        topics = s.scalars(
            select(Topic).where(Topic.vendor_id == vendor_id)
            .order_by(Topic.opportunity_score.desc()).limit(50)).all()
        article_by_topic = {}
        for article in s.query(Article).filter_by(vendor_id=vendor_id).all():
            candidates = [article_by_topic.get(article.topic_id), article]
            article_by_topic[article.topic_id] = _preferred_article(
                candidate for candidate in candidates if candidate is not None)
        rows = []
        for t in topics:
            # Content-gap suggestion: does a real, already-published post on
            # rededucation.com already cover this? Deterministic keyword-
            # overlap match (synthesis/gaps.py), not an LLM guess.
            match = best_match(s, f"{t.title} {t.keyword}")
            article = article_by_topic.get(t.id)
            rows.append({"topic": t, "article": article, "match": match,
                         # The same stage the vendor card counts. Without it
                         # this page could only say whether an article existed
                         # — "View brief" looked identical for a draft holding
                         # a failing check and one already live on the site.
                         "stage": article_stage(article) if article else None})

        return render_template("studio/topic.html", vendor=vendor, rows=rows)


@studio_bp.post("/vendor/<int:vendor_id>/generate")
def generate_topics(vendor_id: int):
    count = request.form.get("count", type=int) or 10
    # Floor of 1, not 3: the picker offers "1 idea" for a cheap probe of a
    # new vendor or theme, and clamping it to 3 would silently spend 3x the
    # Serper credits the operator chose to spend.
    count = max(1, min(25, count))
    theme = (request.form.get("theme") or "").strip() or None

    with get_session() as s:
        vendor = s.get(Vendor, vendor_id)
        if vendor is None:
            abort(404)
        try:
            result = generate_topics_for_vendor(s, vendor, count, theme)
        except PipelineError as exc:
            flash(str(exc), "error")
            return redirect(url_for("studio.topics_for_vendor", vendor_id=vendor_id))
        except Exception as exc:
            flash(safe_error(exc), "error")
            return redirect(url_for("studio.topics_for_vendor", vendor_id=vendor_id))

    msg = (f"{len(result.inserted)} idea(s) scored: {result.created} to create, "
          f"{result.parked} parked, {result.retired} rejected.")
    if result.skipped:
        msg += f" ({result.skipped} skipped as duplicate keywords.)"
    # Said out loud so a short batch reads as the duplicate filter doing its
    # job, not as the generator quietly underdelivering.
    if result.repeats:
        msg += (f" {result.repeats} repeated a topic this vendor already has "
                "and were dropped.")
    flash(msg, "success")
    return redirect(url_for("studio.topics_for_vendor", vendor_id=vendor_id))


@studio_bp.post("/topic/<int:topic_id>/remove")
def remove_topic(topic_id: int):
    """Drop a single proposed idea from the vendor's topic list.

    The Topic step is where a batch of AI-proposed ideas gets triaged, and
    until now the only per-idea action here was "create the brief" — an
    unwanted idea could only be removed from the separate admin board, which
    is not where this flow lives. Same rule as that board's delete: an idea
    that already has a brief or article is REFUSED rather than cascaded away,
    because those represent real LLM spend. Redirects back to this vendor's
    topic list so the batch can be triaged in one pass."""
    with get_session() as s:
        topic = s.get(Topic, topic_id)
        if topic is None:
            abort(404)
        vendor_id = topic.vendor_id
        title = topic.title
        try:
            s.delete(topic)
            s.commit()
            flash(f"Removed \u201c{title}\u201d from the ideas for this vendor.",
                  "success")
        except IntegrityError:
            # RESTRICT under PRAGMA foreign_keys=ON — a Brief/Article points at
            # this topic. Blocking beats orphaning or cascading real work away.
            s.rollback()
            flash(f"Can\u2019t remove \u201c{title}\u201d — it already has a "
                  "brief or article. Delete that first.", "error")
    return redirect(url_for("studio.topics_for_vendor", vendor_id=vendor_id))


@studio_bp.post("/topic/<int:topic_id>/advance")
def advance_topic(topic_id: int):
    """Approve (if not already) and create the brief in one step — this is
    what "choosing a topic" means in Studio, collapsing what used to be two
    separate actions (approve, then separately create a brief) on the old
    Topic Engine / Brief+Writer pages."""
    with get_session() as s:
        topic = s.get(Topic, topic_id)
        if topic is None:
            abort(404)
        vendor_id = topic.vendor_id

        existing = _preferred_article(
            s.query(Article).filter_by(topic_id=topic_id).all())
        if existing is not None:
            return redirect(url_for("studio.brief_step", article_id=existing.id))

        if topic.priority != TopicPriority.CREATE:
            topic.priority = TopicPriority.CREATE
            s.commit()

        try:
            article = create_brief_for_topic(s, topic)
        except PipelineError as exc:
            flash(str(exc), "error")
            return redirect(url_for("studio.topics_for_vendor", vendor_id=vendor_id))
        except Exception as exc:
            flash(safe_error(exc), "error")
            return redirect(url_for("studio.topics_for_vendor", vendor_id=vendor_id))
        article_id = article.id

    return redirect(url_for("studio.brief_step", article_id=article_id))


# ============================================================= STEP 3: BRIEF
@studio_bp.get("/brief/<int:article_id>")
def brief_step(article_id: int):
    with get_session() as s:
        article = s.get(Article, article_id)
        if article is None:
            abort(404)
        # Internal-link suggestions: real posts related to this brief's
        # keyword/vendor, feeding brief["internal_links"] with a live URL
        # instead of the model guessing one.
        #
        # Fetch a wider pool, drop the ones already added, THEN cap at
        # SUGGESTION_COUNT. Limiting first and filtering second silently
        # returned fewer than the intended number — every link the editor
        # accepted permanently shrank the list they had left to choose from.
        added = set(article.brief.get("internal_links") or [])
        pool = related_posts(s, f"{article.keyword} {article.vendor.name}",
                             limit=SUGGESTION_COUNT + len(added) + 5)
        suggestions = [r for r in pool if r.post.url not in added][:SUGGESTION_COUNT]
        return render_template(
            "studio/brief.html", article=article, suggestions=suggestions,
            # Shown against the title field so a Rank Math problem is fixable
            # HERE, before the article is written — rather than surfacing as a
            # QA warning afterwards, or in WordPress after it is pushed.
            title_issues=title_readability_issues(article.title, article.keyword))


@studio_bp.post("/brief/<int:article_id>")
def update_brief(article_id: int):
    with get_session() as s:
        article = s.get(Article, article_id)
        if article is None:
            abort(404)

        brief = dict(article.brief)
        # The title IS editable here now. The page has always claimed every
        # field is, but the title was the one it never offered — and it is the
        # field with the most rules on it (the exact keyword phrase, a power
        # word, a sentiment word, a number, 120 characters). It is also the
        # one that can arrive truncated from the writer.
        title = clamp_title((request.form.get("title") or "").strip(), 120)
        keyword = (request.form.get("keyword") or "").strip()
        cta = (request.form.get("cta") or "").strip()
        outline = [ln.strip() for ln in (request.form.get("outline") or "").splitlines() if ln.strip()]
        key_points = [ln.strip() for ln in (request.form.get("key_points") or "").splitlines() if ln.strip()]

        if title:
            brief["title"] = title
            article.title = title
        if keyword:
            brief["keyword"] = keyword
            article.keyword = keyword
        if cta:
            brief["cta"] = cta
        if outline:
            brief["outline"] = outline
        if key_points:
            brief["key_points"] = key_points
        article.brief = brief  # reassign the whole dict — JSON column dirty-tracking needs this
        s.commit()

    flash("Brief updated.", "success")
    return redirect(url_for("studio.brief_step", article_id=article_id))


@studio_bp.post("/brief/<int:article_id>/add-link")
def add_internal_link(article_id: int):
    """Add an internal link to the brief — from a suggestion's "+ Add" button
    or the manual-entry field. Only real http(s) URLs are accepted: the link
    is handed to the Writer prompt and rendered on this page, so junk or a
    javascript: value must be rejected at the door (the browser's type=url
    check is client-side convenience only)."""
    from urllib.parse import urlparse

    url = (request.form.get("url") or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        flash("That doesn't look like a valid link — it must start with "
              "http:// or https://.", "error")
        return redirect(url_for("studio.brief_step", article_id=article_id))

    with get_session() as s:
        article = s.get(Article, article_id)
        if article is None:
            abort(404)
        brief = dict(article.brief)
        links = list(brief.get("internal_links") or [])
        if url in links:
            flash("That link is already in the brief.", "error")
            return redirect(url_for("studio.brief_step", article_id=article_id))
        links.append(url)
        brief["internal_links"] = links
        article.brief = brief
        s.commit()
    flash("Link added to brief.", "success")
    return redirect(url_for("studio.brief_step", article_id=article_id))


@studio_bp.post("/brief/<int:article_id>/remove-link")
def remove_internal_link(article_id: int):
    url = (request.form.get("url") or "").strip()
    with get_session() as s:
        article = s.get(Article, article_id)
        if article is None:
            abort(404)
        brief = dict(article.brief)
        links = [u for u in (brief.get("internal_links") or []) if u != url]
        brief["internal_links"] = links
        article.brief = brief
        s.commit()
    flash("Link removed.", "success")
    return redirect(url_for("studio.brief_step", article_id=article_id))


# =========================================================== STEP 4: ARTICLE
@studio_bp.get("/article/<int:article_id>")
def article_step(article_id: int):
    with get_session() as s:
        article = s.get(Article, article_id)
        if article is None:
            abort(404)
        if article.status == ArticleStatus.BRIEF:
            # Not written yet — bounce back to the Brief step. Any flashed
            # message a write attempt just set survives this extra redirect
            # (flash messages persist in the session until a page consumes
            # them via get_flashed_messages).
            return redirect(url_for("studio.brief_step", article_id=article_id))
        # No existing-post suggestions here: originality is already checked
        # against source material by the QA harness; a second check against
        # the site's own back catalogue is worth doing later, not folded in.
        content_html = render_markdown(article.content)
        # The H2 headings, for the targeted section-rewrite control — one
        # LLM call for one section instead of regenerating the whole article.
        import re as _re
        headings = _re.findall(r"^##\s+(.+)$", article.content, _re.M)
        return render_template(
            "studio/article.html", article=article, content_html=content_html,
            headings=headings, unfixable=unfixable_checks(s, article))


@studio_bp.post("/article/<int:article_id>/write")
def write_step(article_id: int):
    with get_session() as s:
        article = s.get(Article, article_id)
        if article is None:
            abort(404)
        try:
            write_article(s, article)
        except PipelineError as exc:
            flash(str(exc), "error")
        except Exception as exc:
            flash(safe_error(exc), "error")
    return redirect(url_for("studio.article_step", article_id=article_id))


@studio_bp.post("/article/<int:article_id>/fix")
def fix_issues_step(article_id: int):
    """Fix only the QA/REACS issues the editor ticked, leaving the rest of
    the article as written. Several checks are WARN-only or advisory and are
    often a deliberate call to keep, so the selection is the point."""
    qa_names = request.form.getlist("qa")
    reacs_pillars = request.form.getlist("reacs")
    if not qa_names and not reacs_pillars:
        flash("Tick at least one issue to fix.", "error")
        return redirect(url_for("studio.article_step", article_id=article_id))

    with get_session() as s:
        article = s.get(Article, article_id)
        if article is None:
            abort(404)
        try:
            soft, still_failing = fix_issues(s, article, qa_names, reacs_pillars)
            n = len(qa_names) + len(reacs_pillars)
            cleared = n - len(still_failing)
            msg = f"Fixed {cleared} of {n} selected issue(s)."
            if still_failing:
                # Name them. A check that cannot be fixed by a rewrite must
                # not look like one that just needs another attempt.
                msg += (" Still failing after the pass: "
                        + ", ".join(sorted(set(still_failing)))
                        + ". Trying the same one again usually spends another "
                        "AI call for the same result — read its message and "
                        "edit by hand, or report it if it looks unfixable.")
            if soft:
                # Survived BOTH the paragraph-level revert and the retries, so
                # it is genuinely entangled with the fix rather than a stray
                # edit. Saying that stops it reading as the tool going in
                # circles, and points at the one move that can still work.
                msg += (" Could not avoid re-introducing "
                        + ", ".join(sorted(set(soft)))
                        + " (non-blocking) — the same sentences carry both. "
                          "Tick those together with the rest to fix them in "
                          "one pass.")
            flash(msg, "success")
        except PipelineError as exc:
            flash(str(exc), "error")
        except Exception as exc:
            flash(safe_error(exc), "error")
    return redirect(url_for("studio.article_step", article_id=article_id))


@studio_bp.post("/article/<int:article_id>/prose-review")
def prose_review_step(article_id: int):
    """Advisory proofread of the article body — one LLM call, on demand.

    A button rather than part of the write: this is the only model-judged
    check in the app, and running it inside the write/retry loop would triple
    the cost of every generation and make QA's verdict non-reproducible. It
    can never block publishing.
    """
    with get_session() as s:
        article = s.get(Article, article_id)
        if article is None:
            abort(404)
        try:
            n = run_prose_review(s, article)
        except PipelineError as exc:
            flash(str(exc), "error")
        except Exception as exc:
            # "The review did not run" must never look like "no issues found".
            flash(f"Prose review failed — {safe_error(exc)}", "error")
        else:
            flash("Proofread found no language issues." if not n
                  else f"Proofread found {n} language issue"
                       f"{'' if n == 1 else 's'} to review.",
                  "success" if not n else "info")
    return redirect(url_for("studio.article_step", article_id=article_id)
                    + "#prose-review")


@studio_bp.post("/article/<int:article_id>/rescore")
def rescore_step(article_id: int):
    """Re-run both gates against the article as it stands. Changes nothing.

    Free and instant — no model call. The case it exists for: the gates
    themselves changed, so an article written under an older standard is still
    showing the older verdict, and until now the only way to refresh it was a
    rewrite that costs a generation and alters the very text you wanted
    re-measured.
    """
    with get_session() as s:
        article = s.get(Article, article_id)
        if article is None:
            abort(404)
        try:
            qa_rechecked, moved = rescore_article(s, article)
        except PipelineError as exc:
            flash(str(exc), "error")
        except Exception as exc:
            flash(safe_error(exc), "error")
        else:
            def _shift(label, pair):
                before, after = pair
                if before == after:
                    return f"{label} {after}, unchanged"
                return f"{label} {before} \u2192 {after}"

            parts = [_shift("REACS", moved["reacs"])]
            if qa_rechecked:
                parts.append(_shift("QA", moved["qa"]))
            msg = "Re-scored against the current checks \u2014 " + ", ".join(parts) + "."
            if not qa_rechecked:
                # Never let a stale number sit next to a fresh one unlabelled.
                msg += (" QA could NOT be re-checked \u2014 the article's sources "
                        "could not be rebuilt, so its score is unchanged from "
                        "before.")
            flash(msg, "success" if qa_rechecked else "error")
        return redirect(url_for("studio.article_step", article_id=article_id))


@studio_bp.post("/article/<int:article_id>/prose-apply")
def prose_apply_step(article_id: int):
    """Apply ticked proofread suggestions straight to the body.

    No LLM call: each stored issue quotes text already verified to be in the
    article, so applying one is an exact replace. That makes this the cheap,
    predictable option next to "Rewrite one section", which is a model call
    that can change prose nobody asked it to touch.
    """
    try:
        indexes = [int(v) for v in request.form.getlist("fix")]
    except ValueError:
        indexes = []
    with get_session() as s:
        article = s.get(Article, article_id)
        if article is None:
            abort(404)
        try:
            applied, skipped, qa_rechecked = apply_prose_fixes(
                s, article, indexes)
        except PipelineError as exc:
            flash(str(exc), "error")
        except Exception as exc:
            flash(safe_error(exc), "error")
        else:
            msg = (f"Applied {applied} correction"
                   f"{'' if applied == 1 else 's'} to the article.")
            if skipped:
                msg += (f" {skipped} could not be applied — the text they "
                        "quote has since changed, and they are still listed "
                        "so you can see which.")
            if not qa_rechecked:
                # Never let a stale score sit next to edited text pretending
                # to be current.
                msg += (" REACS was re-scored; QA could NOT be re-checked "
                        "because the article's sources could not be rebuilt — "
                        "its score is from before this edit.")
            flash(msg, "success" if not skipped else "error")
        return redirect(url_for("studio.article_step", article_id=article_id)
                        + "#prose-review")


@studio_bp.post("/article/<int:article_id>/edit")
def edit_step(article_id: int):
    """Save the editor's own corrections to the article.

    The manual counterpart to Fix selected: when the automated fix cannot do
    it — or keeps not doing it, which the panel now says outright — the person
    reading the finding is allowed to just correct the text. No model call, no
    search credits, and both gates re-score immediately so the verdict beside
    the article is never older than the article.
    """
    # Only treat the FAQ as submitted when the form actually carried those
    # inputs. Absent means "not edited here"; present-but-empty means the
    # editor cleared them, which is a different instruction.
    faq = None
    if "faq_question" in request.form:
        faq = [{"question": q, "answer": a} for q, a in
               zip(request.form.getlist("faq_question"),
                   request.form.getlist("faq_answer"))]

    with get_session() as s:
        article = s.get(Article, article_id)
        if article is None:
            abort(404)
        try:
            changed, qa_rechecked = apply_manual_edit(
                s, article,
                title=request.form.get("title"),
                content=request.form.get("content"),
                meta_title=request.form.get("meta_title"),
                meta_description=request.form.get("meta_description"),
                faq=faq)
        except PipelineError as exc:
            flash(str(exc), "error")
        except Exception as exc:
            flash(safe_error(exc), "error")
        else:
            if not changed:
                flash("Nothing to save — the article is exactly as it was.",
                      "success")
            else:
                fields = (", ".join(changed[:-1]) + " and " + changed[-1]
                          if len(changed) > 1 else changed[0])
                msg = (f"Saved your edit to the {fields}. "
                       f"QA {article.qa_score}, REACS {article.reacs_score}.")
                if not qa_rechecked:
                    # Never let a stale score sit next to edited text
                    # pretending to be current.
                    msg += (" REACS was re-scored; QA could NOT be re-checked "
                            "because the article's sources could not be "
                            "rebuilt — its score is from before this edit.")
                flash(msg, "success" if qa_rechecked else "error")
        return redirect(url_for("studio.article_step", article_id=article_id)
                        + "#edit")


@studio_bp.post("/article/<int:article_id>/rewrite-section")
def rewrite_section_step(article_id: int):
    """Targeted rewrite of one H2 section — grounded in the article's own
    stored references (same [S#] tags), full QA re-run on the result."""
    heading = (request.form.get("heading") or "").strip()
    instruction = (request.form.get("instruction") or "").strip() or None
    if not heading:
        flash("Choose a section to rewrite.", "error")
        return redirect(url_for("studio.article_step", article_id=article_id))

    with get_session() as s:
        article = s.get(Article, article_id)
        if article is None:
            abort(404)
        try:
            rewrite_section(s, article, heading, instruction)
            flash(f'Section "{heading}" rewritten — QA re-run below.', "success")
        except PipelineError as exc:
            flash(str(exc), "error")
        except Exception as exc:
            flash(safe_error(exc), "error")
    return redirect(url_for("studio.article_step", article_id=article_id))


# =========================================================== STEP 5: PREVIEW
@studio_bp.get("/article/<int:article_id>/pdf")
def article_pdf(article_id: int):
    """The article as it stands, plus its reference list, as a PDF.

    The body is the SITE PREVIEW — the same render_site_preview() the Preview
    step uses, so the document shows the article as the published page will
    structure it rather than as raw markdown.

    Every <details> is forced open. accordionise_faq marks only the first one
    open, which is right on a web page and wrong on paper: Chrome prints a
    closed <details> as its summary line alone, so the FAQ answers would be
    silently missing from the PDF.

    Works at any stage after the body exists — reviewers ask for the draft
    while it still has findings against it, which is the point of sending it
    to somebody.
    """
    import re as _re
    from datetime import datetime, timezone

    with get_session() as s:
        article = s.get(Article, article_id)
        if article is None:
            abort(404)
        if not (article.content or "").strip():
            flash("This article has no body to export yet.", "error")
            return redirect(url_for("studio.article_step", article_id=article_id))
        byline = (article.byline_instructor.name
                  if article.byline_instructor else "")
        body_html = render_site_preview(
            article.content,
            [f.get("question", "") for f in (article.faq or [])])
        body_html = _re.sub(r"<details(?![^>]*\bopen\b)", "<details open",
                            body_html)
        html = render_template(
            "studio/article_pdf.html", article=article, byline=byline,
            body_html=body_html,
            references=article.references or [],
            qa_checks=article.qa_checks or [],
            reacs_report=article.reacs_report or [],
            topic=article.topic,
            generated_on=datetime.now(timezone.utc).strftime("%d %B %Y"))
        filename = (article.slug or f"article-{article_id}") + ".pdf"

    pdf = html_to_pdf(html)
    if pdf is None:
        flash("Could not produce the PDF — the headless browser it needs is "
              "unavailable on this server. The article itself is unaffected.",
              "error")
        return redirect(url_for("studio.article_step", article_id=article_id))
    return send_file(io.BytesIO(pdf), mimetype="application/pdf",
                     as_attachment=True, download_name=filename)


@studio_bp.get("/article/<int:article_id>/preview")
def preview_step(article_id: int):
    """How the article will look once it is on rededucation.com.

    Sits before Publish deliberately: this is the last point where the draft
    can still be sent back for a section rewrite, and formatting problems
    (a table that overflows on mobile, a wall of H3s where the site expects
    an FAQ accordion) are invisible in the editor's own markdown view.

    Renders through render_site_preview(), which applies the published page's
    structure, against preview.css, whose values were measured off a live
    post rather than approximated."""
    with get_session() as s:
        article = s.get(Article, article_id)
        if article is None:
            abort(404)
        if article.status == ArticleStatus.BRIEF:
            return redirect(url_for("studio.brief_step", article_id=article_id))
        byline = None
        if article.byline_instructor is not None:
            i = article.byline_instructor
            byline = f"{i.name}, {i.credentials}" if i.credentials else i.name
        return render_template("studio/preview.html", article=article,
                               preview_html=render_site_preview(
                                   article.content,
                                   [f.get('question', '') for f in (article.faq or [])]),
                               byline=byline)


# =========================================================== STEP 6: PUBLISH
@studio_bp.get("/article/<int:article_id>/publish")
def publish_step(article_id: int):
    """Stage 7 (WordPress Publisher) isn't built yet — kept visible as a
    real step, plainly labelled as not-yet-built, rather than hidden, so the
    flow's mental model stays complete: every article is heading somewhere."""
    with get_session() as s:
        article = s.get(Article, article_id)
        if article is None:
            abort(404)
        return render_template("studio/publish.html", article=article,
                               wp_author=wp_author_for(article),
                               wp_category=wp_category_for(article),
                               wp_edit_url=wp_edit_link(article),
                               wp_ready=wordpress_configured(),
                               commercially_active=article.vendor.priority is not None,
                               publish_ready=bool(article.vendor.priority is not None
                                                  and article.qa_passed
                                                  and article.reacs_passed),
                               payload_json=json.dumps(wp_payload_for(article),
                                                       indent=2)[:20000])


@studio_bp.post("/article/<int:article_id>/push")
def push_step(article_id: int):
    """Create the WordPress draft. POST-only and single-shot: a GET would let
    a refresh or a crawler create duplicate drafts on the live site."""
    with get_session() as s:
        article = s.get(Article, article_id)
        if article is None:
            abort(404)
        try:
            link = push_to_wordpress(s, article)
            flash(f"Draft created in WordPress. {link}", "success")
        except PipelineError as exc:
            flash(str(exc), "error")
        except Exception as exc:
            flash(safe_error(exc), "error")
        return redirect(url_for("studio.publish_step", article_id=article_id))
