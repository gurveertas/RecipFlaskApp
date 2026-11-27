"""Content Brief + Writer admin UI: turn an approved Topic into a structured
brief, then the brief into a full written article — a governed, Topic-driven
pipeline, additive to the ad-hoc Blog Generator search bar (not replacing it).

A Flask Blueprint at /admin/articles, mirroring webapp/topics.py's style
(plain functions, get_session(), redirect-after-POST with flash() messages).
"""
from __future__ import annotations

from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   url_for)
from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError



from config.db import Article, Topic, TopicPriority, get_session
from .pipeline import (PipelineError, create_brief_for_topic, fix_issues,
                       safe_error, write_article)
from .render import render_markdown

articles_bp = Blueprint("articles", __name__, url_prefix="/admin/articles")


@articles_bp.get("")
def article_list():
    with get_session() as s:
        articles = s.scalars(select(Article).order_by(Article.created_at.desc())).all()
        # CREATE-priority topics with no Article yet — a correlated NOT EXISTS,
        # not a Python-side filter, so the DB does the set-difference.
        candidates = s.scalars(
            select(Topic).where(Topic.priority == TopicPriority.CREATE)
            .where(~exists().where(Article.topic_id == Topic.id))
            .order_by(Topic.opportunity_score.desc())
        ).all()
        return render_template(
            "admin/articles.html", articles=articles, candidates=candidates)


@articles_bp.post("/brief")
def create_brief():
    topic_id = request.form.get("topic_id", type=int)
    if not topic_id:
        flash("Choose an approved topic.", "error")
        return redirect(url_for("articles.article_list"))

    with get_session() as s:
        topic = s.get(Topic, topic_id)
        if topic is None:
            flash("That topic no longer exists.", "error")
            return redirect(url_for("articles.article_list"))

        try:
            article = create_brief_for_topic(s, topic)
        except PipelineError as exc:
            flash(str(exc), "error")
            return redirect(url_for("articles.article_list"))
        except Exception as exc:
            flash(safe_error(exc), "error")
            return redirect(url_for("articles.article_list"))
        article_id = article.id

    return redirect(url_for("articles.article_detail", article_id=article_id))


@articles_bp.get("/<int:article_id>")
def article_detail(article_id: int):
    with get_session() as s:
        article = s.get(Article, article_id)
        if article is None:
            abort(404)
        content_html = render_markdown(article.content)
        # Same per-article lock list the Studio page uses. Two pages showing
        # the same panels from two lists is how they drifted apart before.
        from .pipeline import unfixable_checks

        return render_template(
            "admin/article_detail.html", article=article,
            content_html=content_html, unfixable=unfixable_checks(s, article))


@articles_bp.post("/<int:article_id>/write")
def write(article_id: int):
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

    return redirect(url_for("articles.article_detail", article_id=article_id))


@articles_bp.post("/<int:article_id>/fix")
def fix(article_id: int):
    """Same targeted per-issue fix as the Studio article step — this page
    shows the identical QA/REACS panels, so it gets the identical control."""
    qa_names = request.form.getlist("qa")
    reacs_pillars = request.form.getlist("reacs")
    if not qa_names and not reacs_pillars:
        flash("Tick at least one issue to fix.", "error")
        return redirect(url_for("articles.article_detail", article_id=article_id))

    with get_session() as s:
        article = s.get(Article, article_id)
        if article is None:
            abort(404)
        try:
            soft, still_failing = fix_issues(s, article, qa_names, reacs_pillars)
            n = len(qa_names) + len(reacs_pillars)
            msg = f"Fixed {n - len(still_failing)} of {n} selected issue(s)."
            if still_failing:
                msg += (" Still failing after the pass: "
                        + ", ".join(sorted(set(still_failing)))
                        + ". Trying the same one again usually spends another "
                        "AI call for the same result.")
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
    return redirect(url_for("articles.article_detail", article_id=article_id))


@articles_bp.post("/<int:article_id>/delete")
def delete(article_id: int):
    with get_session() as s:
        article = s.get(Article, article_id)
        if article is None:
            abort(404)
        try:
            s.delete(article)
            s.commit()
        except IntegrityError:
            s.rollback()
            flash("Couldn't delete that article.", "error")
    return redirect(url_for("articles.article_list"))
