"""Stage 7 — WordPress Publisher.

Split the way the reference implementation splits it, and for the same
reason: `payload.py` is pure (markdown -> HTML, JSON-LD, payload assembly)
and unit-testable without a network or credentials, while `client.py` holds
the only code that talks to WordPress. Everything risky lives in one file.

THE GUARANTEE: this package only ever creates DRAFTS. `status` is hard-coded
to "draft" in build_post_payload and there is no code path that sets it to
"publish" — going live stays a human action inside WordPress.
"""
from .payload import build_post_payload, build_schema, to_wp_html
from .client import (WpError, create_draft_post, set_rankmath_meta,
                     test_connection, wordpress_configured)

__all__ = ["build_post_payload", "build_schema", "to_wp_html",
           "WpError", "create_draft_post", "set_rankmath_meta", "test_connection",
           "wordpress_configured"]
