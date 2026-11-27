"""The only code that talks to WordPress.

Credentials come from the environment, never the database:
    WORDPRESS_URL            e.g. https://www.rededucation.com
    WORDPRESS_USERNAME       a WP user login
    WORDPRESS_APP_PASSWORD   an Application Password, NOT the login password

Ported from the reference `src/lib/wordpress-client.ts`. Kept deliberately
small: every network call and every credential read is in this file, so the
blast radius of "what can talk to the live site" is one module.
"""
from __future__ import annotations

import base64
import logging
import os

import requests

logger = logging.getLogger(__name__)

_TIMEOUT = 30


class WpError(Exception):
    """A WordPress call failed, with a message safe to show a user."""


def _config() -> tuple[str, str]:
    """(base_url, auth_header). Raises WpError when unconfigured."""
    url = os.getenv("WORDPRESS_URL", "").strip()
    user = os.getenv("WORDPRESS_USERNAME", "").strip()
    password = os.getenv("WORDPRESS_APP_PASSWORD", "").strip()
    if not (url and user and password):
        raise WpError(
            "WordPress is not configured. Set WORDPRESS_URL, "
            "WORDPRESS_USERNAME and WORDPRESS_APP_PASSWORD in .env.")
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return url.rstrip("/") + "/wp-json", "Basic " + token


def wordpress_configured() -> bool:
    try:
        _config()
        return True
    except WpError:
        return False


def _call(path: str, method: str = "GET", body: dict | None = None) -> dict:
    """One HTTP call to the WP REST API.

    Uses `requests` like the rest of the codebase (retrieval/search.py,
    retrieval/fetch.py) rather than urllib. That is not cosmetic: urllib
    verifies TLS against a store macOS does not populate for Python, so every
    call failed with CERTIFICATE_VERIFY_FAILED against the real site while
    curl succeeded. requests ships certifi and just works.
    """
    base, auth = _config()
    try:
        resp = requests.request(
            method, base + path,
            json=body,
            headers={
                "Authorization": auth,
                "Content-Type": "application/json",
                # A default UA is rejected by some WAFs; the site is behind one.
                "User-Agent": "RedEducation-BlogGenerator/1.0",
            },
            timeout=_TIMEOUT,
        )
    except requests.RequestException as e:
        raise WpError(f"Could not reach WordPress: {e}") from e

    if resp.status_code >= 400:
        detail = ""
        try:
            detail = (resp.json() or {}).get("message", "")
        except Exception:
            pass
        if resp.status_code in (401, 403):
            raise WpError(
                f"WordPress rejected the credentials (HTTP {resp.status_code}). "
                "Check the Application Password, and that the account may "
                f"create posts for other authors (edit_others_posts). {detail}".strip())
        raise WpError(f"WordPress API error: HTTP {resp.status_code}. {detail}".strip())

    try:
        return resp.json() or {}
    except ValueError as e:
        raise WpError("WordPress returned a non-JSON response.") from e


def test_connection() -> dict:
    """Verify reachability AND the Application Password in one call.

    /users/me is the reference's choice and a good one: it fails loudly on a
    bad password rather than appearing to work and silently creating nothing.
    Returns the WP user the credential authenticates as — worth showing,
    because that account is who a post belongs to unless `author` is set."""
    return _call("/wp/v2/users/me")


def create_draft_post(payload: dict) -> dict:
    """Create the post. The caller builds the payload (publish/payload.py);
    this refuses anything that is not a draft as a last line of defence, so a
    future edit to the payload builder cannot quietly start publishing live."""
    if payload.get("status") != "draft":
        raise WpError(
            "Refusing to send a post whose status is not 'draft'. This tool "
            "never publishes live; going live is a human action in WordPress.")
    return _call("/wp/v2/posts", method="POST", body=payload)


def set_rankmath_meta(post_id: int, *, keyword: str, title: str,
                      description: str) -> None:
    """Set Rank Math's focus keyword, SEO title and meta description.

    This site runs Rank Math, not Yoast. The reference implementation sends
    `meta: {yoast_title, yoast_description}` on the post itself, which does
    NOTHING here twice over: those are Yoast's keys, and WordPress silently
    drops any meta key that is not registered with show_in_rest — the first
    real push landed with "Keyword: Not Set" and no SEO score.

    Rank Math's own fields are likewise not writable through the core `meta`
    field, so this goes through the endpoint its admin UI uses. Best-effort by
    contract: the draft already exists by the time this runs, and failing to
    set a keyword is not a reason to lose the post.
    """
    _call("/rankmath/v1/updateMeta", method="POST", body={
        "objectType": "post",
        "objectID": int(post_id),
        "meta": {
            "rank_math_focus_keyword": keyword or "",
            "rank_math_title": title or "",
            "rank_math_description": description or "",
        },
    })
