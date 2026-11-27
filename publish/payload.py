"""Pure payload assembly for the WordPress Publisher — no I/O, no credentials.

Ported from the reference `src/lib/wordpress.ts`, with three deliberate
divergences, each recorded where it happens:

  * markdown -> HTML reuses webapp/render.py rather than the reference's
    hand-rolled subset parser, so published HTML is identical to what the
    Preview step showed and goes through the same sanitiser.
  * tables get the site's own inline styling re-applied (see _style_tables).
  * categories are resolved from stored IDs, and tags are not sent at all —
    the reference creates terms by name, which would grow the taxonomy.
"""
from __future__ import annotations

import json

# The live site's table treatment, measured off published posts (see
# webapp/static/css/preview.css for the full sample). Re-applied INLINE here
# because a WordPress post carries no stylesheet of ours: the Preview step can
# style tables from a CSS file, but a published post only keeps what is in the
# HTML itself. Without this, generated tables render as unstyled default rows
# while hand-written posts have the red header — the visible difference
# between "written by the team" and "generated".
_TABLE_STYLE = ("width:100%;border-collapse:collapse;"
                "font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.6;")
_WRAP_STYLE = "overflow-x:auto;width:100%;margin:30px 0;"
_TH_ROW_STYLE = "background:#d32f2f;color:#ffffff;"
_CELL_STYLE = "padding:14px;border:1px solid #ddd;"
_TH_STYLE = _CELL_STYLE + "text-align:left;"


def _render(markdown_text: str, questions: list[str] | None = None) -> str:
    """Imported inside the call, not at module scope, to break a cycle:
    publish -> webapp.render -> webapp.app -> webapp.studio -> publish.
    The web app survived it only because gunicorn happens to import webapp
    first; any script that imported `publish` first died on it."""
    from webapp.render import (accordionise_faq, render_markdown,
                               strip_citation_markers)
    # Same reader boundary as the Preview — see strip_citation_markers.
    return accordionise_faq(
        strip_citation_markers(render_markdown(markdown_text)), questions)


def _style_tables(html: str) -> str:
    """Give every table the house styling, inline, and a scroll wrapper."""
    if "<table>" not in html:
        return html
    html = html.replace(
        "<table>", f'<div style="{_WRAP_STYLE}"><table style="{_TABLE_STYLE}">')
    html = html.replace("</table>", "</table></div>")
    # Header cells carry both the fill and the padding; the fill is set on the
    # cell rather than the row because a row background does not survive some
    # editors' cleanup, and cell-level styling is what the live posts use.
    html = html.replace("<th>", f'<th style="{_TH_ROW_STYLE}{_TH_STYLE}">')
    html = html.replace("<td>", f'<td style="{_CELL_STYLE}">')
    return html


# The FAQ accordion's look, shipped as ONE scoped <style> block in the post
# content. Inline styles were tried first and cannot express Elementor's +/-
# marker: that needs a ::after pseudo-element and an [open] state selector,
# neither of which exists as an inline style.
#
# The obvious alternative — writing Elementor's own nested-accordion widget
# into _elementor_data — is impossible over REST: Elementor registers that
# meta with an auth callback that returns
# "Sorry, you are not allowed to edit the _elementor_data custom field"
# even for an Editor. A <style> block is what remains, and it survives intact
# (verified live: ::after, [open] and the class attributes all persisted).
# It requires the pushing account to hold `unfiltered_html`, which the
# Editor-role credential does.
#
# Values match webapp/static/css/preview.css, so the Preview and the
# published page render identically.
_FAQ_CSS = (
    "<style>"
    ".re-faq{border-bottom:1px solid #e6e6e6}"
    ".re-faq__q{cursor:pointer;list-style:none;position:relative;"
    "font-family:Roboto,'Helvetica Neue',Arial,sans-serif;font-size:18px;"
    "font-weight:600;color:#262626;padding:18px 28px 18px 0}"
    ".re-faq__q::-webkit-details-marker{display:none}"
    ".re-faq__q::marker{content:''}"
    ".re-faq__q::after{content:'+';position:absolute;right:4px;top:50%;"
    "transform:translateY(-50%);font-size:22px;font-weight:400;"
    "color:#d32f2f;line-height:1}"
    ".re-faq[open]>.re-faq__q::after{content:'\\2013'}"
    ".re-faq__a{padding:0 0 18px}"
    ".re-faq__a>p:last-child{margin-bottom:0}"
    "</style>")


def _style_faq(html: str) -> str:
    """Swap the preview's class names for the published ones and prepend the
    stylesheet, once, only when there is actually an accordion."""
    if "sp-faq" not in html:
        return html
    html = (html.replace("class='sp-faq'", "class='re-faq'")
                .replace("class='sp-faq__q'", "class='re-faq__q'")
                .replace("class='sp-faq__a'", "class='re-faq__a'"))
    return _FAQ_CSS + html


def to_wp_html(markdown_text: str, byline: tuple[str, str] | None = None,
               questions: list[str] | None = None) -> str:
    """Article markdown as the HTML WordPress will store.

    `byline` is (name, credentials). It is prepended as a paragraph here
    rather than being part of the stored article body, matching how the
    Preview step renders it — the body stays clean markdown so QA word counts
    and originality checks never see boilerplate.
    """
    html = _style_faq(_style_tables(_render(markdown_text, questions)))
    if byline:
        name, credentials = byline
        who = f"{name}, {credentials}" if credentials else name
        html = (f'<p class="article-byline"><em>Technically reviewed by '
                f'{who}</em></p>\n' + html)
    return html


def build_schema(*, title: str, description: str, keyword: str,
                 faq: list[dict], byline: tuple[str, str] | None = None,
                 organisation: str = "Red Education") -> str:
    """JSON-LD @graph: an Article node, plus an FAQPage node when there are
    FAQs. This is the FAQ schema deliberately deferred from the AI-search
    work — it belongs on the published page, not in the draft body."""
    if byline:
        name, credentials = byline
        author: dict = {"@type": "Person", "name": name,
                        "jobTitle": credentials,
                        "worksFor": {"@type": "Organization", "name": organisation}}
    else:
        author = {"@type": "Organization", "name": organisation}

    graph: list[dict] = [{
        "@type": "Article",
        "headline": title[:110],       # schema.org caps headline at 110
        "description": description,
        "keywords": keyword,
        "author": author,
        "publisher": {"@type": "Organization", "name": organisation},
    }]
    entries = [f for f in (faq or []) if f.get("question") and f.get("answer")]
    if entries:
        graph.append({
            "@type": "FAQPage",
            "mainEntity": [{
                "@type": "Question",
                "name": f["question"],
                "acceptedAnswer": {"@type": "Answer", "text": _render(f["answer"])},
            } for f in entries[:10]],
        })
    # SECURITY: escape the characters that can terminate the surrounding
    # <script> block. json.dumps escapes quotes but NOT "</script>", and an
    # HTML parser ends a script element at the first literal </script>
    # regardless of JSON context — so a title or FAQ answer containing
    # `</script><script>...` becomes EXECUTABLE JavaScript on the published
    # page. That is stored XSS on rededucation.com, and it is reachable:
    # article text is model output derived from scraped third-party pages,
    # and this tool publishes as an Editor holding `unfiltered_html`, so
    # WordPress does not strip it either.
    #
    # \u003c / \u003e / \u0026 are valid JSON string escapes — the document
    # parses identically, it just can no longer close the script element.
    raw = json.dumps({"@context": "https://schema.org", "@graph": graph},
                     ensure_ascii=False)
    return (raw.replace("<", "\\u003c")
               .replace(">", "\\u003e")
               .replace("&", "\\u0026"))


def build_post_payload(*, title: str, slug: str, content_markdown: str,
                       excerpt: str, meta_title: str, meta_description: str,
                       keyword: str, faq: list[dict],
                       category_ids: list[int],
                       author_id: int | None = None,
                       byline: tuple[str, str] | None = None) -> dict:
    """The exact JSON body sent to POST /wp-json/wp/v2/posts.

    status is hard-coded "draft" and there is no parameter to change it —
    this tool never publishes live, and that guarantee should be impossible
    to override by passing an argument.
    """
    schema = build_schema(title=title,
                          description=meta_description or excerpt,
                          keyword=keyword, faq=faq, byline=byline)
    html = (to_wp_html(content_markdown, byline,
                       [f.get('question', '') for f in (faq or [])])
            + f'\n<script type="application/ld+json">{schema}</script>')
    payload: dict = {
        "title": title,
        "slug": slug,
        "status": "draft",          # never "publish". Going live is a human act.
        "content": html,
        "excerpt": excerpt,
        "categories": [c for c in category_ids if c],
        # Tags deliberately omitted: the site's tag vocabulary is legacy news
        # terms and the reference's approach (create a tag per content type
        # and per keyword) would grow a one-use long tail.
        # No "meta" here. The reference sends Yoast keys; this site runs
        # Rank Math, and WordPress drops unregistered meta keys anyway — the
        # SEO fields are set after creation via publish.set_rankmath_meta.
    }
    if author_id:
        payload["author"] = author_id
    return payload
