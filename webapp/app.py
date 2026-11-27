"""Flask app: serves the UI and the endpoints that run the pipeline.

Generation is exposed two ways:
  • POST /api/generate            — single blocking call, full result (scripts/curl)
  • POST /api/jobs + GET stream   — background job + Server-Sent Events, so the
                                     browser can render real step-by-step progress

The job registry below is an in-memory dict, so it only works if the app runs as
ONE process (many threads is fine — see webapp/README.md). A multi-process
deployment would need the job/queue state in something shared (e.g. Redis)
instead; out of scope for this internal tool's scale.
"""
from __future__ import annotations

import json
import os
import queue
import secrets
import threading
import time
import uuid
from pathlib import Path

from urllib.parse import urlparse

from flask import Flask, Response, abort, jsonify, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix

from synthesis import parse_keywords
from synthesis.reacs import REACS_THRESHOLD

from .admin import admin_bp
from .articles import articles_bp
from .pipeline import safe_error
from .render import render_markdown
from .studio import studio_bp
from .topics import topics_bp
from .quick_drafts import (QuickDraftError, proofread_quick_draft,
                           remember_draft, rewrite_quick_section,
                           section_headings)
from .quick_publish import QuickPublishError, push_quick_draft
from .service import run_generation, draft_to_dict, qa_to_dict

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Repo root (parent of webapp/); the .env, if present, lives here.
_ROOT = Path(__file__).resolve().parent.parent

# ---- in-memory job registry (see module docstring for the single-process caveat) ----
_JOB_TTL_SECONDS = 600
_jobs_lock = threading.Lock()
_jobs: dict[str, dict] = {}   # job_id -> {"queue": Queue, "created": float}


def _create_job() -> tuple[str, "queue.Queue"]:
    job_id = uuid.uuid4().hex
    q: "queue.Queue" = queue.Queue()
    with _jobs_lock:
        _jobs[job_id] = {"queue": q, "created": time.time()}
        _prune_stale_jobs_locked()
    return job_id, q


def _get_job_queue(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    return job["queue"] if job else None


def _drop_job(job_id: str) -> None:
    with _jobs_lock:
        _jobs.pop(job_id, None)


def _prune_stale_jobs_locked() -> None:
    """Remove jobs whose stream was never opened/finished (caller holds the lock)."""
    cutoff = time.time() - _JOB_TTL_SECONDS
    for jid in [j for j, v in _jobs.items() if v["created"] < cutoff]:
        _jobs.pop(jid, None)


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from <repo>/.env into the environment.

    Kept dependency-free and deliberately simple: real environment variables
    (e.g. from systemd) always win, so this only fills what isn't already set.
    """
    env_path = _ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def _render_markdown(text: str) -> str:
    return render_markdown(text)


def _keyword_warning(keyword: str):
    """Jinja filter: the topic-keyword length warning, or None.

    A filter rather than per-route plumbing because the same warning has to
    appear on both topic surfaces (Article Studio's topic step and the admin
    topic board) and they build their rows differently."""
    from synthesis.topics import keyword_length_warning
    return keyword_length_warning(keyword)


def _ago(value) -> str:
    """A datetime as "3 days ago". Used where the point is staleness rather
    than the date itself — "9 days ago" answers "can I trust this number?"
    at a glance in a way that "02 Aug 2026" does not.

    Naive datetimes are read as UTC, which is what everything in this app
    stores (datetime.now(timezone.utc), then SQLite drops the tzinfo).
    """
    from datetime import datetime, timezone
    if value is None:
        return "never"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - value).total_seconds()
    if seconds < 0:
        return "just now"          # clock skew; never say "in 3 hours"
    for limit, divisor, unit in ((90, 1, "second"), (5400, 60, "minute"),
                                 (172800, 3600, "hour"), (1209600, 86400, "day"),
                                 (7257600, 604800, "week")):
        if seconds < limit:
            n = int(seconds // divisor)
            if unit == "second":
                return "just now"
            return f"{n} {unit}{'' if n == 1 else 's'} ago"
    n = int(seconds // 2592000)
    return f"{n} month{'' if n == 1 else 's'} ago"


def create_app() -> Flask:
    _load_dotenv()
    app = Flask(__name__)

    # Served under a path prefix (/blog-generator/) on a shared nginx host, so
    # honour the forwarding headers. x_prefix is the one that matters: nginx
    # strips the prefix before proxying, and without X-Forwarded-Prefix ->
    # SCRIPT_NAME every url_for() would emit "/studio" instead of
    # "/blog-generator/studio" — every link, form action and stylesheet 404s.
    #
    # Trusting these headers is only safe because gunicorn binds 127.0.0.1, so
    # nginx is the only thing that can set them. Never bind this to 0.0.0.0
    # while ProxyFix is active: a direct caller could then forge its own
    # prefix and scheme. Counts are 1 = exactly one proxy hop.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_prefix=1)
    # Needed for flash() — the status messages after a POST now ride in the
    # (signed) session cookie instead of a ?success=/?error= query string, so
    # the address bar stays clean and a bookmark/refresh doesn't re-show a
    # stale banner. Prefer a stable SECRET_KEY from the environment (required
    # if this is ever run with >1 worker, so every process signs cookies the
    # same way); fall back to a per-process random key, which is fine for a
    # single-worker deploy since flashes are consumed on the very next request.
    app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
    app.jinja_env.filters["keyword_warning"] = _keyword_warning
    app.jinja_env.filters["ago"] = _ago
    # Templates state the REACS pass mark in two places. Both said 75 for a
    # while after the v1.3 raise to 90, so the screen told the editor an
    # article had passed a bar it had not. Read it from the engine instead.
    app.jinja_env.globals["REACS_THRESHOLD"] = REACS_THRESHOLD
    app.register_blueprint(admin_bp)
    app.register_blueprint(topics_bp)
    app.register_blueprint(articles_bp)
    app.register_blueprint(studio_bp)

    @app.before_request
    def _csrf_origin_guard():
        """Same-origin check on every state-changing request.

        This app has no auth and no CSRF tokens, so a page the operator visits
        in the same browser could otherwise auto-submit a form to localhost and
        forge a POST — deleting Vendor Centre / Topic / Article data, or firing
        topic-generation / article-writing that silently burns the LLM and
        Serper quota. Browsers always attach an `Origin` header to a
        cross-origin POST (and a `Referer` on same-origin form posts), so we
        reject when that source host doesn't match ours. Non-browser clients
        (curl/scripts hitting the JSON API, per the module docstring) send
        neither header, so this leaves them working — there's nothing to forge
        from without a browser session in the first place."""
        if request.method in _SAFE_METHODS:
            return
        source = request.headers.get("Origin") or request.headers.get("Referer")
        if source is None:
            return  # non-browser client — no ambient session to abuse
        if urlparse(source).netloc != request.host:
            abort(403)

    @app.after_request
    def _security_headers(resp):
        # Complements the origin guard above: DENY framing so a hostile page
        # can't clickjack the (unauthenticated) admin UI, and stop MIME
        # sniffing on responses. Cheap, no behavioral downside for this app.
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        return resp

    @app.get("/")
    def index():
        # Live mode needs Serper (to research) plus an LLM key (to write): Gemini,
        # an OpenAI-compatible override, or Anthropic. Only offer it when both
        # halves are present, so the UI can't invite a doomed request.
        llm_key = (os.getenv("GEMINI_API_KEY")
                   or os.getenv("LLM_API_KEY")
                   or os.getenv("ANTHROPIC_API_KEY"))
        live_available = bool(os.getenv("SERPER_API_KEY") and llm_key)
        return render_template("index.html", live_available=live_available)

    @app.get("/guide")
    def guide():
        """Full how-to: what every step and input does, the QA checks, costs."""
        return render_template("guide.html")

    @app.get("/api/health")
    def health():
        return jsonify(ok=True)

    @app.post("/api/generate")
    def generate():
        data = request.get_json(silent=True) or {}
        topic = (data.get("topic") or "").strip()
        region = (data.get("region") or "").strip()
        keywords = parse_keywords(data.get("keywords"))
        mode = "live" if data.get("mode") == "live" else "demo"

        if mode == "live" and not topic:
            return jsonify(ok=False, error="A topic is required for live generation."), 400

        try:
            draft, report, bundle = run_generation(topic, region, mode, keywords=keywords)
        except Exception as exc:  # missing keys, retrieval/generation failure, etc.
            return jsonify(ok=False, error=safe_error(exc)), 200

        return jsonify(
            ok=True,
            mode=mode,
            draft_id=remember_draft(draft, report, keywords, bundle),
            sections=section_headings(draft),
            draft=draft_to_dict(draft, _render_markdown(draft.body_markdown)),
            qa=qa_to_dict(report),
        )

    @app.post("/api/jobs")
    def start_job():
        """Kick off generation in a background thread; return a job id to stream."""
        data = request.get_json(silent=True) or {}
        topic = (data.get("topic") or "").strip()
        region = (data.get("region") or "").strip()
        keywords = parse_keywords(data.get("keywords"))
        mode = "live" if data.get("mode") == "live" else "demo"

        if mode == "live" and not topic:
            return jsonify(ok=False, error="A topic is required for live generation."), 400

        job_id, q = _create_job()

        def worker() -> None:
            def on_progress(step: str, status: str, message: str, **extra):
                q.put({"type": "progress", "step": step, "status": status,
                       "message": message, **extra})
            try:
                draft, report, bundle = run_generation(
                    topic, region, mode, on_progress=on_progress, keywords=keywords)
                q.put({
                    "type": "done",
                    "mode": mode,
                    # Held in memory for an hour so it can be pushed to
                    # WordPress. Nothing reaches the database — see
                    # quick_publish for why the browser does not post it back.
                    "draft_id": remember_draft(draft, report, keywords, bundle),
                    "sections": section_headings(draft),
                    "draft": draft_to_dict(draft, _render_markdown(draft.body_markdown)),
                    "qa": qa_to_dict(report),
                })
            except Exception as exc:
                q.put({"type": "error", "error": safe_error(exc)})

        threading.Thread(target=worker, daemon=True).start()
        return jsonify(ok=True, job_id=job_id)

    def _quick_result(entry, draft_id: str):
        """The same shape the generation endpoints return, so the page can
        re-render a rewritten draft with the code it already has."""
        return jsonify(
            ok=True, draft_id=draft_id,
            sections=section_headings(entry["draft"]),
            draft=draft_to_dict(entry["draft"],
                                _render_markdown(entry["draft"].body_markdown)),
            qa=qa_to_dict(entry["report"]),
            prose_review=entry.get("prose_review"))

    @app.post("/api/drafts/<draft_id>/rewrite-section")
    def rewrite_quick_section_route(draft_id: str):
        """Rewrite one H2 of a held Quick draft, grounded in its own sources."""
        data = request.get_json(silent=True) or {}
        try:
            entry = rewrite_quick_section(
                draft_id, (data.get("heading") or "").strip(),
                (data.get("instruction") or "").strip() or None)
        except QuickDraftError as exc:
            return jsonify(ok=False, error=str(exc)), 400
        except Exception as exc:
            return jsonify(ok=False, error=safe_error(exc)), 500
        return _quick_result(entry, draft_id)

    @app.post("/api/drafts/<draft_id>/proofread")
    def proofread_quick_draft_route(draft_id: str):
        """Advisory proofread. No pass/fail — nothing here blocks on a
        model's opinion of the writing."""
        try:
            issues = proofread_quick_draft(draft_id)
        except QuickDraftError as exc:
            return jsonify(ok=False, error=str(exc)), 400
        except Exception as exc:
            return jsonify(ok=False, error=safe_error(exc)), 500
        return jsonify(ok=True, prose_review=issues)

    @app.post("/api/drafts/<draft_id>/wordpress")
    def push_quick_draft_route(draft_id: str):
        """Send a Quick draft to WordPress as a DRAFT post.

        Same destination and the same never-publish-live guarantee as Article
        Studio's Publish step, on a separate implementation — see
        webapp/quick_publish.py for what a Quick draft does not have that the
        Studio path reads."""
        try:
            result = push_quick_draft(draft_id)
        except QuickPublishError as exc:
            return jsonify(ok=False, error=str(exc)), 400
        except Exception as exc:
            return jsonify(ok=False, error=safe_error(exc)), 500
        return jsonify(ok=True, **result)

    @app.get("/api/jobs/<job_id>/stream")
    def stream_job(job_id: str):
        """Server-Sent Events: relay this job's queued progress/result events."""
        q = _get_job_queue(job_id)
        if q is None:
            return jsonify(ok=False, error="Unknown or expired job."), 404

        def events():
            try:
                while True:
                    try:
                        item = q.get(timeout=25)
                    except queue.Empty:
                        yield ": keep-alive\n\n"   # comment line: keeps proxies from timing out
                        continue
                    yield f"data: {json.dumps(item)}\n\n"
                    if item["type"] in ("done", "error"):
                        break
            finally:
                _drop_job(job_id)

        return Response(events(), mimetype="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx response buffering, if fronted by one
        })

    return app


# For `flask --app webapp.app` and gunicorn "webapp.app:app"
app = create_app()
