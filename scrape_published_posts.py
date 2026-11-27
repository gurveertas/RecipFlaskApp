#!/usr/bin/env python3
"""Scrape rededucation.com's own post-sitemap into the PublishedPost table —
the real, already-published articles that power Article Studio's content-gap
and internal-link suggestions (synthesis/gaps.py).

Safe to re-run: upserts by URL, so a periodic re-run just picks up new posts
and refreshes lastmod dates.

Usage
-----
    python scrape_published_posts.py
    python scrape_published_posts.py --workers 16
"""
from __future__ import annotations

import argparse
import html
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from config.db import AppConfig, PublishedPost, get_session
from synthesis.gaps import known_acronyms, title_from_url, title_is_unusable

BASE = "https://www.rededucation.com"

# The site's sitemap INDEX. Read it and follow what it lists, rather than
# naming each sub-sitemap here.
#
# The previous version hard-coded Yoast-style names (post-sitemap1.xml,
# product-sitemap1..15.xml, ...). The site moved to WordPress core sitemaps,
# every one of those 404'd, and because a 404 here is a themed HTML page with
# no <url> blocks, the run reported "found 0 URLs" and exited 0 — nightly, for
# weeks, while still stamping the coverage index as fresh. Following the index
# means the next rename, or a 4th product page, is picked up on its own.
SITEMAP_INDEX = f"{BASE}/wp-sitemap.xml"

# sub-sitemap name -> PublishedPost.kind. Only content worth linking TO from
# an article; the taxonomy archives WordPress also publishes (category,
# post_tag, industry, team_group, course_*) are deliberately excluded — an
# editor linking "read more" wants a course or an article, not a listing page.
# `vendors` is the exception, and was a page in the old list too.
#
# Course URLs arrive from four post types; the `product` ones are ~4,300
# region/language variants of the same courses (EDU-210 alone appeared 381
# times as ..._as_mandarin-6, ..._na-3, ...), which _canonical_course_urls
# collapses.
_KIND_BY_SITEMAP: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"/wp-sitemap-posts-post-\d+\.xml$"), "post"),
    (re.compile(r"/wp-sitemap-posts-page-\d+\.xml$"), "page"),
    (re.compile(r"/wp-sitemap-posts-case-studies-\d+\.xml$"), "case_study"),
    (re.compile(r"/wp-sitemap-posts-(?:courses|course-explained|coursesvender"
                r"|product)-\d+\.xml$"), "course"),
    (re.compile(r"/wp-sitemap-taxonomies-vendors-\d+\.xml$"), "page"),
]

# Non-English course variants: the same course, another delivery language.
# Skipped outright — an English article linking a Mandarin course page is
# worse than not linking at all.
# A URL can legitimately appear in two sitemaps — "/course-explained/" is both
# a course listing and a page. Keep the most specific kind rather than letting
# whichever sitemap happened to be read last win.
KIND_PRIORITY = {"post": 0, "course": 1, "case_study": 2, "page": 3}

# Language markers only. Deliberately NOT the region codes that dominate these
# slugs (na, emea, anz, eu, latam, ind, asia, us, aus) — those are the same
# course delivered in another territory and make perfectly good link targets,
# whereas a Mandarin or German page does not for an English article. Includes
# the 3-letter abbreviations the slugs actually use ("...-ccsa_ger"): without
# them the shortest-URL rule picked the GERMAN CCSA page as canonical.
_LANGS = ("mandarin", "portugese", "portuguese", "french", "japanese", "korean",
          "spanish", "german", "thai", "vietnamese", "italian", "russian",
          "turkish", "arabic", "hindi",
          "fre", "ger", "jpn", "kor", "spa", "ita", "por", "chs", "cht")


def _course_key(url: str) -> str | None:
    """Collapse a /product/ URL to the course it represents, or None to skip.

    Variant markers begin at the first slug segment containing an underscore
    ("...-ccsa-ccsa_asia-12" -> "...-ccsa"), with any trailing "-N" removed.
    Verified against the live sitemaps: 2,999 URLs -> 141 distinct courses."""
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    if any(l in slug for l in _LANGS):
        return None
    parts = slug.split("-")
    cut = next((i for i, seg in enumerate(parts) if "_" in seg), len(parts))
    return re.sub(r"-\d+$", "", "-".join(parts[:cut])) or None


def _canonical_course_urls(
        entries: list[tuple[str, str | None]]) -> list[tuple[str, str | None, str]]:
    """One representative URL per course, as (url, lastmod, title).

    The shortest URL wins — that is the one carrying the fewest region/sequence
    suffixes. The title comes from the canonical course KEY rather than the
    chosen slug, so it reads "Check Point Security Administration CCSA" instead
    of "Check Point Security Administration Ccsa Ccsa Ger": the variant marker
    is noise in a suggestion label, and worse, noise the matcher would score."""
    best: dict[str, tuple[str, str | None]] = {}
    for url, lastmod in entries:
        key = _course_key(url)
        if key is None:
            continue
        if key not in best or len(url) < len(best[key][0]):
            best[key] = (url, lastmod)
    return [(url, lastmod, title_from_url("/" + key, _ACRONYMS))
            for key, (url, lastmod) in best.items()]


# Vendor-KB acronyms, loaded once per run. Without them every title derived
# from a slug regresses to "Ccse" — the scraper is the ONLY other caller of
# title_from_url, so forgetting it here silently undoes the fix on the next
# scrape.
_ACRONYMS: set[str] = set()


def _load_acronyms() -> None:
    global _ACRONYMS
    with get_session() as s:
        _ACRONYMS = known_acronyms(s)


def _fetch(url: str, timeout: int = 20, required: bool = False) -> str:
    """Fetch a URL. `required` decides what an HTTP error means.

    -f so curl exits non-zero on 4xx/5xx instead of handing back the error
    page. Without it a 404 returned 369KB of themed HTML, which parsed as a
    sitemap containing no URLs — the failure that hid a broken scrape for
    weeks. Sitemaps pass required=True and a bad status stops the run.

    Page fetches stay tolerant: roughly half of them meet a Cloudflare
    challenge, and the caller already falls back to a slug-derived title, so
    an empty string is the right answer there rather than an exception.
    """
    p = subprocess.run(["curl", "-sL", "-f", "-A", "Mozilla/5.0",
                        "--max-time", str(timeout), url],
                       capture_output=True, timeout=timeout + 5)
    if p.returncode != 0:
        if required:
            raise RuntimeError(
                f"{url} could not be fetched (curl exit {p.returncode}). "
                "If the site's sitemap layout changed again, _KIND_BY_SITEMAP "
                "needs the new names.")
        return ""
    return p.stdout.decode("utf-8", errors="replace")


def _discover_sitemaps() -> list[tuple[str, str]]:
    """[(sub-sitemap url, kind)] for the parts of the index we want."""
    index = _fetch(SITEMAP_INDEX, required=True)
    found: list[tuple[str, str]] = []
    for loc in re.findall(r"<loc>([^<]+)</loc>", index):
        url = loc.strip()
        for pattern, kind in _KIND_BY_SITEMAP:
            if pattern.search(url):
                found.append((url, kind))
                break
    return found


def _list_urls(only_kinds: set[str] | None = None
               ) -> list[tuple[str, str | None, str, str | None]]:
    """Return [(url, lastmod_iso_or_None, kind, title_hint_or_None), ...] across
    every configured sitemap, with /product/ course variants collapsed."""
    out: list[tuple[str, str | None, str, str | None]] = []
    by_kind: dict[str, list[tuple[str, str | None]]] = {}
    for sm, kind in _discover_sitemaps():
        if only_kinds and kind not in only_kinds:
            continue
        xml = _fetch(sm, required=True)
        # <url><loc>...</loc><lastmod>...</lastmod>...</url>, in document order —
        # a plain regex per <url> block is enough for this one-shot scrape.
        for block in re.findall(r"<url>(.*?)</url>", xml, re.S):
            loc = re.search(r"<loc>([^<]+)</loc>", block)
            lastmod = re.search(r"<lastmod>([^<]+)</lastmod>", block)
            if loc:
                by_kind.setdefault(kind, []).append(
                    (loc.group(1).strip(), lastmod.group(1).strip() if lastmod else None))

    seen: dict[str, str] = {}      # url -> winning kind
    for kind, entries in by_kind.items():
        raw_products = [e for e in entries if "/product/" in e[0]]
        others = [(u, lm, None) for u, lm in entries if "/product/" not in u]
        products: list[tuple[str, str | None, str | None]] = []
        if raw_products:
            canon = _canonical_course_urls(raw_products)
            print(f"  {kind}: {len(raw_products)} product URLs -> "
                  f"{len(canon)} canonical courses")
            products = [(u, lm, title) for u, lm, title in canon]
        for url, lastmod, title_hint in products + others:
            prev = seen.get(url)
            if prev is not None and KIND_PRIORITY[prev] <= KIND_PRIORITY[kind]:
                continue               # already have it under a more specific kind
            seen[url] = kind
            out.append((url, lastmod, kind, title_hint))
    # One row per URL: the same URL cannot be inserted twice in a run, and the
    # table has a UNIQUE constraint on it.
    best: dict[str, tuple[str, str | None, str, str | None]] = {}
    for url, lastmod, kind, hint in out:
        if url not in best or KIND_PRIORITY[kind] < KIND_PRIORITY[best[url][2]]:
            best[url] = (url, lastmod, kind, hint)
    return list(best.values())


def _fetch_title(url: str) -> str:
    """The page's real <title>, or a slug-derived one when the fetch comes
    back with a bot-challenge/error page instead of the article.

    Roughly half of a full run hits Cloudflare's "Just a moment..."
    interstitial, and storing that verbatim poisoned the data: 111 of 217 rows
    carried the identical placeholder title, and synthesis/gaps.py matched on
    titles, so those posts were invisible to the suggestion engine. The slug
    is right there in the URL and carries the real keywords, so prefer it over
    a title we know is meaningless."""
    html_text = _fetch(url)
    m = re.search(r"<title>([^<]*)</title>", html_text)
    title = html.unescape(m.group(1).strip()) if m else ""
    title = re.sub(r"\s*[-|]\s*Red Education\s*$", "", title, flags=re.I)
    return title if not title_is_unusable(title) else title_from_url(url, _ACRONYMS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--repair-titles-only", action="store_true",
                        help="No network: re-derive unusable titles from their "
                             "URL slug for rows already in the table.")
    parser.add_argument("--kinds", default="",
                        help="Comma-separated kinds to scrape (post,course,page,"
                             "case_study). Default: all.")
    parser.add_argument("--titles", choices=("fetch", "slug"), default="fetch",
                        help="'fetch' requests each page for its <title>; "
                             "'slug' derives titles from the URL with no "
                             "network at all. Use 'slug' for the bulk course/"
                             "page kinds: roughly half of a fetch run hits "
                             "Cloudflare's challenge and falls back to the slug "
                             "anyway, so the fetch buys little for hundreds of "
                             "extra requests.")
    parser.add_argument("--no-prune", action="store_true",
                        help="Keep rows that no longer appear in any sitemap. "
                             "Pruning only ever runs on a full scrape, never "
                             "on a --kinds subset.")
    args = parser.parse_args()

    _load_acronyms()
    print(f"loaded {len(_ACRONYMS)} vendor acronyms for title casing")

    if args.repair_titles_only:
        fixed = 0
        with get_session() as s:
            for row in s.query(PublishedPost).all():
                if title_is_unusable(row.title):
                    row.title = title_from_url(row.url, _ACRONYMS)
                    fixed += 1
            s.commit()
        print(f"repaired {fixed} unusable title(s) from their URL slug")
        return

    only = {k.strip() for k in args.kinds.split(",") if k.strip()} or None
    entries = _list_urls(only)
    print(f"found {len(entries)} URLs across the sitemaps")

    # Stop before touching the database. A run that found nothing has learned
    # nothing, and the old behaviour — upsert zero rows, then stamp
    # coverage_scraped_at — left the vendor step reporting "checked today"
    # over an index that had not been refreshed since the sitemaps moved.
    # Non-zero exit so cron mails it instead of logging a quiet success.
    if not entries:
        raise SystemExit(
            f"no URLs found via {SITEMAP_INDEX} — refusing to stamp the "
            "coverage index as fresh. Check that the index still lists the "
            "post types _KIND_BY_SITEMAP matches.")

    titled: list[tuple[str, str, str | None, str]] = []  # (url, title, lastmod, kind)
    if args.titles == "slug":
        print("deriving titles from URL slugs (no page fetches)")
        titled = [(u, hint or title_from_url(u, _ACRONYMS), lm, k)
                  for u, lm, k, hint in entries]
    else:
        print(f"fetching titles with {args.workers} workers...")
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(_fetch_title, u): (u, lm, k, h) for u, lm, k, h in entries}
            for i, fut in enumerate(as_completed(futs)):
                url, lastmod, kind, hint = futs[fut]
                fetched = fut.result()
                titled.append((url, fetched if not title_is_unusable(fetched)
                               else (hint or fetched), lastmod, kind))
                if (i + 1) % 25 == 0:
                    print(f"  {i + 1}/{len(entries)}")

    inserted = updated = 0
    with get_session() as s:
        existing = {p.url: p for p in s.query(PublishedPost).all()}
        for url, title, lastmod, kind in titled:
            published_at = None
            if lastmod:
                try:
                    published_at = datetime.fromisoformat(lastmod)
                except ValueError:
                    published_at = None
            row = existing.get(url)
            if row is None:
                s.add(PublishedPost(url=url, title=title,
                                    published_at=published_at, kind=kind))
                inserted += 1
            else:
                # Never downgrade a real fetched title to a slug-derived one.
                if not (args.titles == "slug" and not title_is_unusable(row.title)):
                    row.title = title
                row.published_at = published_at
                row.kind = kind
                updated += 1

        # Drop rows the site no longer lists. The upsert alone never deletes,
        # so a rename or an unpublish left the row behind forever: after the
        # sitemap migration 94 were stranded, and two of them 404 — which the
        # internal-link menu would happily have offered an article.
        #
        # ONLY on a full scrape. With --kinds post the entries hold no course
        # URLs, and pruning against that set would delete every course in the
        # table. The empty-run guard above covers the other direction, so by
        # here the set is known to be both complete and non-empty.
        removed = 0
        if only is None and not args.no_prune:
            current = {url for url, _lm, _k, _h in entries}
            gone = [row for url, row in existing.items() if url not in current]
            for row in gone:
                s.delete(row)
            removed = len(gone)
            if gone:
                print(f"pruning {removed} row(s) absent from every sitemap:")
                for row in gone[:5]:
                    print(f"  - [{row.kind}] {row.url}")
                if removed > 5:
                    print(f"  ... and {removed - 5} more")

        # Stamp the run so the vendor step can say how current its "live on
        # rededucation.com" counts are. Set here, at the end, rather than at
        # startup: a run that dies part-way through must not claim the
        # coverage index is fresh.
        cfg = s.get(AppConfig, 1)
        if cfg is not None:
            cfg.coverage_scraped_at = datetime.now()
        s.commit()

    print(f"done: {inserted} inserted, {updated} updated, {removed} removed, "
          f"{len(titled)} total")


if __name__ == "__main__":
    main()
