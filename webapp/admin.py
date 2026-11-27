"""Vendor Knowledge Base admin UI: nested CRUD for vendors -> products,
certifications, and courses.

A Flask Blueprint, not bare functions on `app` directly: this adds ~9 routes to
an app that otherwise has 6, and Flask's endpoint namespace is global
regardless of blueprint use — a Blueprint gets free /admin/ prefixing and
endpoint namespacing (admin.vendor_detail, etc.) so nested-CRUD naming
collisions become structurally impossible instead of "don't forget."

Status messages use Flask's flash() (a signed session cookie), so the address
bar stays clean after a POST/redirect — see create_app() for the SECRET_KEY.
The shared templates/_flashes.html partial renders them.
"""
from __future__ import annotations

from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   url_for)
from sqlalchemy.exc import IntegrityError

from config.db import (AllowlistDomain, ApiUsage, AppConfig, Certification,
                       CertificationStatus, Course, DomainCategory,
                       DomainSighting, DomainSightingStatus, Product,
                       ProductStatus, Vendor, get_session)

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


# --------------------------------------------------------------------------- #
# API usage meter
# --------------------------------------------------------------------------- #
@admin_bp.get("/usage.json")
def usage():
    """The app's own spend meter, as JSON for the Usage modal on the main
    page — per-day and per-feature attribution the provider dashboards can't
    give. These are LOCAL tallies (see ApiUsage's docstring): authoritative
    for what THIS app consumed, an estimate of remaining quota —
    serper.dev / aistudio.google.com stay the balance ground truth.

    "Serper credits left" is derived, not fetched (Serper has no balance
    API): SERPER_CREDITS_START (the dashboard balance when tracking began)
    minus everything tracked since. The operator updates the env var after
    a top-up."""
    import os
    from datetime import date, datetime, timedelta

    from flask import jsonify

    gemini_daily_limit = int(os.getenv("GEMINI_DAILY_LIMIT", "500"))
    serper_start = os.getenv("SERPER_CREDITS_START")
    serper_start = int(serper_start) if serper_start and serper_start.isdigit() else None
    today_start = datetime.combine(date.today(), datetime.min.time())
    week_start = today_start - timedelta(days=6)

    with get_session() as s:
        rows = s.query(ApiUsage).order_by(ApiUsage.created_at.desc()).all()

        llm_rows = [r for r in rows if r.provider in ("gemini", "llm", "anthropic")]
        serper_rows = [r for r in rows if r.provider == "serper"]

        def _since(items, cutoff):
            return [r for r in items if r.created_at >= cutoff]

        serper_total = sum(r.credits for r in serper_rows)

        # A reconciliation, if one has been recorded, beats the env baseline:
        # it says "at this moment the dashboard read N, and we had tracked M",
        # so anything tracked since then comes off N. That survives spend this
        # app never saw, which the env baseline cannot.
        cfg = s.query(AppConfig).first()
        reconciled_at = None
        if cfg is not None and cfg.serper_balance is not None:
            since = serper_total - (cfg.serper_balance_tracked or 0)
            serper_left = cfg.serper_balance - since
            reconciled_at = (cfg.serper_balance_at.isoformat()
                             if cfg.serper_balance_at else None)
        elif serper_start is not None:
            serper_left = serper_start - serper_total
        else:
            serper_left = None
        summary = {
            "llm_today": len(_since(llm_rows, today_start)),
            "llm_week": len(_since(llm_rows, week_start)),
            "llm_tokens_today": sum(r.prompt_tokens + r.completion_tokens
                                    for r in _since(llm_rows, today_start)),
            "serper_today": sum(r.credits for r in _since(serper_rows, today_start)),
            "serper_week": sum(r.credits for r in _since(serper_rows, week_start)),
            "serper_total": serper_total,
            "serper_left": serper_left,
            "serper_reconciled_at": reconciled_at,
            "gemini_daily_limit": gemini_daily_limit,
        }

        # Per-feature split, last 7 days: {(provider, purpose): {...}}
        by_purpose: dict[tuple, dict] = {}
        for r in _since(rows, week_start):
            key = (r.provider, r.purpose)
            agg = by_purpose.setdefault(key, {"calls": 0, "credits": 0, "tokens": 0})
            agg["calls"] += 1
            agg["credits"] += r.credits
            agg["tokens"] += r.prompt_tokens + r.completion_tokens
        purposes = sorted(
            ({"provider": p, "purpose": u, **a} for (p, u), a in by_purpose.items()),
            key=lambda x: (-x["credits"], -x["calls"]))

        # Daily table, last 14 days (newest first)
        daily = []
        for i in range(14):
            day = date.today() - timedelta(days=i)
            d0 = datetime.combine(day, datetime.min.time())
            d1 = d0 + timedelta(days=1)
            day_rows = [r for r in rows if d0 <= r.created_at < d1]
            if not day_rows and i > 0:
                continue   # skip empty past days; always show today
            daily.append({
                "day": day.strftime("%d %b"),
                "llm_calls": sum(1 for r in day_rows
                                 if r.provider in ("gemini", "llm", "anthropic")),
                "tokens": sum(r.prompt_tokens + r.completion_tokens for r in day_rows),
                "serper_credits": sum(r.credits for r in day_rows
                                      if r.provider == "serper"),
            })

        return jsonify(summary=summary, purposes=purposes, daily=daily,
                       tracking_since=(rows[-1].created_at.strftime("%d %b %Y")
                                       if rows else None))


def _int_or_none(raw: str | None) -> int | None:
    raw = (raw or "").strip()
    return int(raw) if raw else None


@admin_bp.get("/vendors")
def vendor_list():
    with get_session() as s:
        vendors = s.query(Vendor).order_by(
            Vendor.priority.is_(None), Vendor.priority,
            Vendor.display_rank.is_(None), Vendor.display_rank, Vendor.name).all()
        counts = {
            v.id: {
                "products": len(v.products),
                "certifications": len(v.certifications),
                "courses": sum(len(c.courses) for c in v.certifications),
            }
            for v in vendors
        }
        return render_template("admin/vendors.html", vendors=vendors, counts=counts)


@admin_bp.post("/vendors")
def vendor_create():
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Vendor name is required.", "error")
        return redirect(url_for("admin.vendor_list"))
    with get_session() as s:
        if s.query(Vendor).filter_by(name=name).first():
            flash(f'"{name}" already exists.', "error")
            return redirect(url_for("admin.vendor_list"))
        s.add(Vendor(name=name))
        s.commit()
    return redirect(url_for("admin.vendor_list"))


@admin_bp.get("/vendors/<int:vendor_id>")
def vendor_detail(vendor_id: int):
    with get_session() as s:
        vendor = s.get(Vendor, vendor_id)
        if vendor is None:
            abort(404)
        return render_template(
            "admin/vendor_detail.html", vendor=vendor,
            products=sorted(vendor.products, key=lambda p: p.name),
            certifications=sorted(vendor.certifications, key=lambda c: c.name),
            courses=[c for cert in vendor.certifications for c in cert.courses],
        )


@admin_bp.post("/vendors/<int:vendor_id>")
def vendor_update(vendor_id: int):
    with get_session() as s:
        vendor = s.get(Vendor, vendor_id)
        if vendor is None:
            abort(404)
        vendor.priority = _int_or_none(request.form.get("priority"))
        s.commit()
    return redirect(url_for("admin.vendor_detail", vendor_id=vendor_id))


# --------------------------------------------------------------------------- #
# Products
# --------------------------------------------------------------------------- #
@admin_bp.post("/vendors/<int:vendor_id>/products")
def product_create(vendor_id: int):
    name = (request.form.get("name") or "").strip()
    status = request.form.get("status")
    if not name or status not in {s.value for s in ProductStatus}:
        flash("Product name and a valid status are required.", "error")
        return redirect(url_for("admin.vendor_detail", vendor_id=vendor_id))
    with get_session() as s:
        # Validate the parent before inserting: without this an id for a
        # non-existent vendor reaches commit and the NOT NULL FK
        # (PRAGMA foreign_keys=ON) raises an unhandled IntegrityError -> 500.
        if s.get(Vendor, vendor_id) is None:
            abort(404)
        s.add(Product(vendor_id=vendor_id, name=name, status=ProductStatus(status)))
        s.commit()
    return redirect(url_for("admin.vendor_detail", vendor_id=vendor_id))


@admin_bp.post("/products/<int:product_id>/status")
def product_update_status(product_id: int):
    with get_session() as s:
        product = s.get(Product, product_id)
        if product is None:
            abort(404)
        status = request.form.get("status")
        if status in {s.value for s in ProductStatus}:
            product.status = ProductStatus(status)
        vendor_id = product.vendor_id
        s.commit()
    return redirect(url_for("admin.vendor_detail", vendor_id=vendor_id))


@admin_bp.post("/products/<int:product_id>/delete")
def product_delete(product_id: int):
    with get_session() as s:
        product = s.get(Product, product_id)
        if product is None:
            abort(404)
        vendor_id = product.vendor_id
        try:
            s.delete(product)
            s.commit()
        except IntegrityError:
            s.rollback()
            flash("Couldn't delete that product.", "error")
    return redirect(url_for("admin.vendor_detail", vendor_id=vendor_id))


# --------------------------------------------------------------------------- #
# Certifications
# --------------------------------------------------------------------------- #
@admin_bp.post("/vendors/<int:vendor_id>/certifications")
def certification_create(vendor_id: int):
    name = (request.form.get("name") or "").strip()
    status = request.form.get("status")
    if not name or status not in {s.value for s in CertificationStatus}:
        flash("Certification name and a valid status are required.", "error")
        return redirect(url_for("admin.vendor_detail", vendor_id=vendor_id))
    with get_session() as s:
        # See product_create: guard the parent FK so a bad vendor_id is a 404,
        # not an unhandled IntegrityError at commit.
        if s.get(Vendor, vendor_id) is None:
            abort(404)
        s.add(Certification(
            vendor_id=vendor_id, name=name, status=CertificationStatus(status),
            exam_code=(request.form.get("exam_code") or "").strip() or None,
            retired_year=_int_or_none(request.form.get("retired_year")),
        ))
        s.commit()
    return redirect(url_for("admin.vendor_detail", vendor_id=vendor_id))


@admin_bp.post("/certifications/<int:cert_id>/status")
def certification_update_status(cert_id: int):
    with get_session() as s:
        cert = s.get(Certification, cert_id)
        if cert is None:
            abort(404)
        vendor_id = cert.vendor_id
        status = request.form.get("status")
        if status in {s.value for s in CertificationStatus}:
            cert.status = CertificationStatus(status)
        if cert.status == CertificationStatus.RETIRED:
            cert.retired_year = _int_or_none(request.form.get("retired_year"))
            # Only accept a replacement that actually exists AND belongs to the
            # same vendor: an unchecked id either points at nothing (FK
            # IntegrityError -> 500 at commit) or at another vendor's cert
            # (silent cross-vendor corruption). Anything invalid clears it.
            replaced_by_id = _int_or_none(request.form.get("replaced_by_id"))
            replacement = s.get(Certification, replaced_by_id) if replaced_by_id else None
            cert.replaced_by_id = (
                replacement.id
                if replacement is not None
                and replacement.vendor_id == cert.vendor_id
                and replacement.id != cert.id
                else None)
        else:
            # A cert marked current again doesn't carry stale retirement facts.
            cert.retired_year = None
            cert.replaced_by_id = None
        notes = (request.form.get("notes") or "").strip()
        cert.notes = notes or None
        s.commit()
    return redirect(url_for("admin.vendor_detail", vendor_id=vendor_id))


@admin_bp.post("/certifications/<int:cert_id>/delete")
def certification_delete(cert_id: int):
    with get_session() as s:
        cert = s.get(Certification, cert_id)
        if cert is None:
            abort(404)
        vendor_id = cert.vendor_id
        try:
            s.delete(cert)
            s.commit()
        except IntegrityError:
            # Either this cert still has Courses attached, or another
            # Certification's replaced_by_id still points at it — both raise
            # here under PRAGMA foreign_keys=ON, at commit time, not before.
            s.rollback()
            flash("Couldn't delete that certification — remove its courses "
                  "first, or check whether another certification lists it "
                  "as a replacement.", "error")
    return redirect(url_for("admin.vendor_detail", vendor_id=vendor_id))


# --------------------------------------------------------------------------- #
# Courses
# --------------------------------------------------------------------------- #
@admin_bp.post("/vendors/<int:vendor_id>/courses")
def course_create(vendor_id: int):
    cert_id = _int_or_none(request.form.get("certification_id"))
    name = (request.form.get("name") or "").strip()
    url = (request.form.get("url") or "").strip()
    with get_session() as s:
        cert = s.get(Certification, cert_id) if cert_id is not None else None
        valid_cert = cert is not None and cert.vendor_id == vendor_id
        if not (valid_cert and name and url):
            flash("A course needs a name, a URL, and one of this vendor's "
                  "certifications — add a certification first if none exist yet.",
                  "error")
            return redirect(url_for("admin.vendor_detail", vendor_id=vendor_id))
        s.add(Course(certification_id=cert_id, name=name, url=url,
                     duration=(request.form.get("duration") or "").strip() or None))
        s.commit()
    return redirect(url_for("admin.vendor_detail", vendor_id=vendor_id))


@admin_bp.post("/courses/<int:course_id>/delete")
def course_delete(course_id: int):
    with get_session() as s:
        course = s.get(Course, course_id)
        if course is None:
            abort(404)
        vendor_id = course.certification.vendor_id
        try:
            s.delete(course)
            s.commit()
        except IntegrityError:
            s.rollback()
            flash("Couldn't delete that course.", "error")
    return redirect(url_for("admin.vendor_detail", vendor_id=vendor_id))


# --------------------------------------------------------------------------- #
# Allowlist gap report
# --------------------------------------------------------------------------- #
# Retrieval runs one unconstrained search alongside its scoped one and filters
# BOTH through the allowlist, so nothing new is ingested. This page shows what
# that second channel had to throw away — the only honest way to learn that
# gartner.com ranks for our topics and nobody ever added it.
#
# A queue for a human, never an input to retrieval. The same channel also
# surfaces exam-dump farms and, for "CCME certification exam", a Cellebrite
# page about a completely different credential. Frequency here is evidence to
# look at, not a score to act on.
@admin_bp.get("/domains")
def domain_gaps():
    with get_session() as s:
        pending = s.query(DomainSighting).filter(
            DomainSighting.status == DomainSightingStatus.PENDING
        ).order_by(DomainSighting.hits.desc(),
                   DomainSighting.last_seen.desc()).all()
        ignored = s.query(DomainSighting).filter(
            DomainSighting.status == DomainSightingStatus.IGNORED
        ).order_by(DomainSighting.hits.desc()).all()
        vendors = s.query(Vendor).order_by(Vendor.name).all()
        return render_template("admin/domain_gaps.html", pending=pending,
                               ignored=ignored, vendors=vendors,
                               categories=list(DomainCategory))


@admin_bp.post("/domains/<int:sighting_id>/ignore")
def domain_ignore(sighting_id: int):
    with get_session() as s:
        row = s.get(DomainSighting, sighting_id)
        if row is None:
            abort(404)
        row.status = DomainSightingStatus.IGNORED
        s.commit()
        flash(f"Ignoring {row.domain}. It stays out of the allowlist and off "
              "this list.", "success")
    return redirect(url_for("admin.domain_gaps"))


@admin_bp.post("/domains/<int:sighting_id>/restore")
def domain_restore(sighting_id: int):
    with get_session() as s:
        row = s.get(DomainSighting, sighting_id)
        if row is None:
            abort(404)
        row.status = DomainSightingStatus.PENDING
        s.commit()
    return redirect(url_for("admin.domain_gaps"))


@admin_bp.post("/domains/<int:sighting_id>/approve")
def domain_approve(sighting_id: int):
    """Add a sighted domain to the allowlist, then drop the sighting.

    Category and tier are asked for rather than guessed: they decide whether a
    domain can corroborate a claim and whether it counts as a vendor source,
    and neither is inferable from the fact that Google ranked it.
    """
    with get_session() as s:
        row = s.get(DomainSighting, sighting_id)
        if row is None:
            abort(404)
        try:
            category = DomainCategory(request.form.get("category") or "press")
        except ValueError:
            flash("Unknown category.", "error")
            return redirect(url_for("admin.domain_gaps"))
        try:
            tier = int(request.form.get("tier") or 2)
        except ValueError:
            tier = 2
        vendor_id = request.form.get("vendor_id") or None
        if category is not DomainCategory.VENDOR:
            vendor_id = None            # only a vendor domain belongs to one
        last = s.query(AllowlistDomain).order_by(
            AllowlistDomain.position.desc()).first()
        try:
            s.add(AllowlistDomain(
                domain=row.domain, category=category, tier=tier,
                vendor_id=int(vendor_id) if vendor_id else None,
                position=(last.position + 1) if last else 1))
            s.delete(row)
            s.commit()
        except IntegrityError:
            s.rollback()
            flash(f"{row.domain} is already on the allowlist.", "error")
            return redirect(url_for("admin.domain_gaps"))
        flash(f"Added {row.domain} to the allowlist as {category.value} "
              f"tier {tier}. Retrieval can cite it from now on.", "success")
    return redirect(url_for("admin.domain_gaps"))


@admin_bp.post("/usage/reconcile")
def usage_reconcile():
    """Record the real balance from serper.dev.

    "Credits left" is derived, because Serper has no balance API, and it
    drifts whenever credits are spent outside this server — a diagnostic
    script run from a laptop hits the same account and lands in a different
    database. It had drifted by 97, more than half of what it was reporting,
    and correcting it meant editing .env and restarting the service.

    Storing the tracked total ALONGSIDE the balance is what makes this hold:
    the pair is a fixed point, so everything tracked afterwards is subtracted
    from it and the next reconciliation is only needed when something spends
    off-server again.
    """
    from datetime import datetime, timezone

    raw = (request.form.get("balance") or "").strip()
    if not raw.isdigit():
        flash("Enter the credits-left number shown on serper.dev.", "error")
        return redirect(url_for("admin.vendor_list"))

    with get_session() as s:
        cfg = s.query(AppConfig).first()
        if cfg is None:
            cfg = AppConfig()
            s.add(cfg)
        tracked = sum(r.credits for r in s.query(ApiUsage).all()
                      if r.provider == "serper")
        cfg.serper_balance = int(raw)
        cfg.serper_balance_tracked = tracked
        cfg.serper_balance_at = datetime.now(timezone.utc)
        s.commit()
        flash(f"Serper balance set to {raw}. Anything spent through this app "
              "from now on comes off that; reconcile again after a top-up or "
              "after running searches outside the server.", "success")
    return redirect(url_for("admin.vendor_list"))
