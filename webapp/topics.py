"""Topic Engine admin UI: generate AI-proposed topic ideas, score them with
Red Education's deterministic weighting formula, and review/approve them.

A Flask Blueprint, mirroring webapp/admin.py's pattern (mounted at
/admin/topics, alongside /admin/vendors). THE CORE PRINCIPLE (ported from the
original TS source): the model estimates raw dimensions only; the scoring
formula in synthesis/topics.py decides create/park/reject, not the model.
"""
from __future__ import annotations

from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   url_for)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from config.db import Topic, TopicPriority, Vendor, get_session
from .pipeline import PipelineError, generate_topics_for_vendor, safe_error

topics_bp = Blueprint("topics", __name__, url_prefix="/admin/topics")


@topics_bp.get("")
def topic_board():
    active_filter = request.args.get("filter", "all")
    with get_session() as s:
        # This app's equivalent of the original's `vendors.active = true`:
        # only vendors with a Vendor Centre priority set are real, curated
        # entries — not the full 23-vendor research-trust allowlist.
        vendors = s.scalars(
            select(Vendor).where(Vendor.priority.isnot(None))
            .order_by(Vendor.priority,
                      Vendor.display_rank.is_(None), Vendor.display_rank,
                      Vendor.name)).all()

        all_topics = s.scalars(
            select(Topic).order_by(Topic.opportunity_score.desc()).limit(200)).all()
        counts = {"create": 0, "parked": 0, "retire": 0}
        for t in all_topics:
            counts[t.priority.value] += 1

        topics = (all_topics if active_filter == "all"
                 else [t for t in all_topics if t.priority.value == active_filter])

        return render_template(
            "admin/topics.html", vendors=vendors, topics=topics, counts=counts,
            total=len(all_topics), active_filter=active_filter,
        )


@topics_bp.post("/generate")
def generate():
    vendor_id = request.form.get("vendor_id", type=int)
    count = request.form.get("count", type=int) or 10
    # Floor of 1, not 3: the picker offers "1 idea" for a cheap probe of a
    # new vendor or theme, and clamping it to 3 would silently spend 3x the
    # Serper credits the operator chose to spend.
    count = max(1, min(25, count))
    theme = (request.form.get("theme") or "").strip() or None

    if not vendor_id:
        flash("Choose a vendor.", "error")
        return redirect(url_for("topics.topic_board"))

    with get_session() as s:
        vendor = s.get(Vendor, vendor_id)
        if vendor is None:
            flash("That vendor no longer exists.", "error")
            return redirect(url_for("topics.topic_board"))

        try:
            result = generate_topics_for_vendor(s, vendor, count, theme)
        except PipelineError as exc:
            flash(str(exc), "error")
            return redirect(url_for("topics.topic_board"))
        except Exception as exc:
            flash(safe_error(exc), "error")
            return redirect(url_for("topics.topic_board"))

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
    return redirect(url_for("topics.topic_board"))


@topics_bp.post("/<int:topic_id>/priority")
def set_priority(topic_id: int):
    value = request.form.get("priority")
    if value not in {p.value for p in TopicPriority}:
        abort(400)
    with get_session() as s:
        topic = s.get(Topic, topic_id)
        if topic is None:
            abort(404)
        topic.priority = TopicPriority(value)
        s.commit()
    return redirect(url_for("topics.topic_board"))


@topics_bp.post("/<int:topic_id>/delete")
def delete(topic_id: int):
    with get_session() as s:
        topic = s.get(Topic, topic_id)
        if topic is None:
            abort(404)
        try:
            s.delete(topic)
            s.commit()
        except IntegrityError:
            # A Brief/Article (Stage 5) references this topic — RESTRICT under
            # PRAGMA foreign_keys=ON blocks the delete rather than silently
            # orphaning or cascading away real LLM-generated content.
            s.rollback()
            flash("Can't delete that topic — it already has a brief or "
                  "article. Delete those first.", "error")
    return redirect(url_for("topics.topic_board"))
