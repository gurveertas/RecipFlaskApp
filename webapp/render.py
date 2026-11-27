"""Markdown -> safe HTML for article bodies — the ONE place model-written
text becomes browser-rendered HTML.

Article content is LLM output built from scraped web sources, and
Python-Markdown passes raw HTML through untouched — so without sanitising,
a hostile source page could plant markup the model echoes (a <script>, an
onerror= handler) that then executes in the reviewer's browser via the
templates' `|safe` / innerHTML rendering. nh3 strips scripts and event
handlers while keeping the article structure QA relies on (headings,
tables, links, [S#] text markers).

Every route that renders article markdown must go through this helper —
don't call markdown.markdown() directly in webapp code.
"""
from __future__ import annotations

import re

import markdown as md
import nh3


def render_markdown(text: str) -> str:
    if not text:
        return ""
    return nh3.clean(md.markdown(text, extensions=["extra", "sane_lists"]))


_FAQ_HEAD_RE = re.compile(
    r"<(h2|h3)>\s*(?:frequently\s+asked\s+questions|faqs?|common\s+questions)"
    r"\s*</\1>", re.I)
_HEADING_RE = re.compile(r"<(h[234])>(.*?)</\1>", re.S)


def _plain(html_fragment: str) -> str:
    """Heading text with markup and entities flattened, for comparison."""
    txt = re.sub(r"<[^>]+>", "", html_fragment)
    txt = (txt.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
              .replace("&quot;", '"').replace("&#39;", "'")
              .replace("\u2019", "'").replace("\u2018", "'"))
    return re.sub(r"\s+", " ", txt).strip().lower().rstrip("?").strip()


# A marker may cite SEVERAL sources: "[S4, S5]" and "[S2, S4]" are both in the
# corpus. The original pattern matched a lone [S4] only, so every combined
# marker survived into Preview, the PDF and the WordPress payload — the very
# defect this function exists to prevent, in a shape it did not cover.
# Separators seen or plausible: comma, semicolon, and "and".
_CITATION_RE = re.compile(
    r"[ \t]*\[\s*S\d+(?:\s*(?:,|;|and)\s*S\d+)*\s*\]", re.I)


# The LINK form, "[S1](https://vendor.example/page)", becomes an anchor whose
# visible text is the tag, so the bracket rule above never sees it: it reached
# a pushed WordPress draft (post 182934) as a link labelled "S1". Removed
# whole — the tag is not copy, and the published page carries no reference
# list for it to point into.
_CITATION_LINK_RE = re.compile(
    r"[ \t]*<a\b[^>]*>\s*S\d+(?:\s*(?:,|;|and)\s*S\d+)*\s*</a>", re.I)


def strip_citation_markers(text: str) -> str:
    """Remove the [S1]-style citation markers from reader-facing output.

    The markers exist so validate.py's citation_integrity can verify every
    claim traces to a retrieved source — they are a QA device, not copy. They
    were reaching WordPress verbatim: post 182579 published "[S1]" eighteen
    times with no reference list and no outbound link, so a reader saw an
    unresolvable code eighteen times.

    Stripped at the READER boundary only (Preview and the WordPress payload).
    article.content keeps them, so QA, the targeted-fix loop and section
    rewrites all still see the citations they depend on — removing them at
    source would silently disable citation_integrity.

    Eats the whitespace BEFORE the marker so "deployments [S4]." closes up to
    "deployments." rather than leaving a space before the full stop."""
    out = re.sub(r"[ \t]{2,}", " ",
                 _CITATION_LINK_RE.sub("", _CITATION_RE.sub("", text or "")))
    # A paragraph that OPENED with a citation is left with a leading space.
    # HTML collapses it, but the raw source is what an editor reads in
    # WordPress, so close it up there too.
    return re.sub(r"(<(?:p|li|td|th|h[1-6])>)[ \t]+", r"\1", out)


def accordionise_faq(html: str, questions: list[str] | None = None) -> str:
    """Render the FAQ as the <details>/<summary> accordion the live site uses.

    Identification is by DATA first: a heading counts as an FAQ question when
    its text matches one of `questions` (the article's stored faq list). That
    is exact rather than heuristic, and it is what makes the accordion
    reliable — the writer does not always emit a "Frequently Asked Questions"
    heading, and an earlier marker-only version silently did nothing on such
    articles while the Preview claimed otherwise.

    Falls back to the marker-heading rule when no questions are supplied, so
    ad-hoc drafts with no stored FAQ still work. Returns the html untouched
    when neither applies — this must never be the thing that loses content.
    """
    known = {_plain(q) for q in (questions or []) if q}
    marker = _FAQ_HEAD_RE.search(html)
    marker_level = marker.group(1).lower() if marker else None
    marker_end = marker.end() if marker else -1

    def is_question(tag: str, inner: str, pos: int) -> bool:
        if known:
            return _plain(inner) in known
        # Legacy path: same level as the marker, and after it.
        return marker_level is not None and tag.lower() == marker_level and pos > marker_end

    # Walk headings, collecting (start, end, tag, inner) so a question's body
    # is everything up to the next heading of any level.
    heads = [(m.start(), m.end(), m.group(1), m.group(2))
             for m in _HEADING_RE.finditer(html)]
    if not heads:
        return html

    out, cursor, i, opened, inserted_marker = [], 0, 0, 0, marker is not None
    while i < len(heads):
        h_start, h_end, tag, inner = heads[i]
        if not is_question(tag, inner, h_start):
            i += 1
            continue
        body_end = heads[i + 1][0] if i + 1 < len(heads) else len(html)
        out.append(html[cursor:h_start])
        if not inserted_marker:
            # The site always labels the block; a bare run of accordions
            # reads as unrelated collapsibles.
            out.append("<h2>Frequently Asked Questions</h2>")
            inserted_marker = True
        question = _plain(inner)
        label = re.sub(r"<[^>]+>", "", inner).strip()
        out.append(
            f"<details class='sp-faq'{' open' if opened == 0 else ''}>"
            f"<summary class='sp-faq__q'>{label}</summary>"
            f"<div class='sp-faq__a'>{html[h_end:body_end]}</div></details>")
        opened += 1
        cursor = body_end
        i += 1
    if not opened:
        return html
    out.append(html[cursor:])
    return "".join(out)


def _wrap_tables(html: str) -> str:
    """Give every table the horizontal-scroll wrapper the site's own tables
    ship with (`overflow-x:auto`), so a wide comparison table scrolls inside
    the column instead of stretching the page."""
    return re.sub(r"<table>", "<div class='sp-tablewrap'><table>", html)\
             .replace("</table>", "</table></div>")


def render_site_preview(text: str, questions: list[str] | None = None) -> str:
    """Article markdown as the published page will structure it.

    Same sanitiser as render_markdown — nh3 strips style attributes, so the
    site's inline table styling cannot survive and is reapplied from CSS
    instead (webapp/static/css/preview.css). That is deliberate: allowing
    style through the sanitiser to gain fidelity would widen the security
    boundary that exists because this text is model output built from
    scraped pages."""
    return _wrap_tables(accordionise_faq(
        strip_citation_markers(render_markdown(text)), questions))
