#!/usr/bin/env python3
"""Ask WordPress which of our pushed drafts have since been published.

This tool creates drafts and a human publishes them, so nothing here ever
learned that a draft went live — every pushed article read the same forever.
This reconciles that by reading WordPress's own `status` field and stamping
Article.published_at on the ones that are live.

Read-only against WordPress: it fetches post status and writes nothing there.

    python sync_wp_state.py
    python sync_wp_state.py --limit 200     # bigger one-off sweep

Meant for cron; see deploy/DEPLOY.md. Exits non-zero only on a real failure,
so a scheduled run staying quiet means "nothing to do".
"""
from __future__ import annotations

import argparse
import sys

# Load .env the same way the web app does, so a cron run (which inherits none
# of the shell's environment) still finds WORDPRESS_URL and the credentials.
from webapp.app import _load_dotenv
from config.db import get_session
from webapp.pipeline import sync_published_state


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=50,
                    help="max articles to check in one run (default 50)")
    ap.add_argument("--quiet", action="store_true",
                    help="print only when something changed — for cron")
    args = ap.parse_args(argv)

    _load_dotenv()
    with get_session() as s:
        r = sync_published_state(s, limit=args.limit)

    if args.quiet and not r["published"]:
        return 0
    print(f"checked {r['checked']}: {r['published']} newly published, "
          f"{r['still_draft']} still draft, {r['missing']} missing, "
          f"{r['unknown']} unknown")
    return 0


if __name__ == "__main__":
    sys.exit(main())
