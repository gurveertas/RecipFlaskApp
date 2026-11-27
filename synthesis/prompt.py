"""Build the generation prompt from a VerifiedBundle.

The prompt hands the model three things: the house style, the reliability rules,
and a clearly-labelled, tagged set of claims + source material. Tagging each
source [S1], [S2]... is what makes inline citation (and later validation)
possible.
"""
from __future__ import annotations

import re

from retrieval.models import VerifiedBundle, Source, Claim
from config import load_course_links
from . import style
from .text import today_block


def _linking_block(bundle: VerifiedBundle, vendor_name: str | None = None) -> str:
    """Give the model the exact course URL to link and the approved CTA phrases,
    so the course-link + CTA check passes instead of the model guessing a URL.

    `vendor_name`, when the caller knows it (the Studio pipeline always does —
    the article is created from a vendor-attached Topic), is authoritative and
    used directly. Only when it's absent (the ad-hoc generator, where the user
    free-types any topic) do we fall back to scanning the topic STRING for a
    vendor alias — which fails silently when the title doesn't name the vendor
    (e.g. a Palo Alto article titled "...Why Panorama is Essential..."),
    dropping the real course link to a generic /courses/ fallback.

    RETURNS NOTHING when the topic is not ours. Red Education sells cyber
    security training, so a course CTA belongs in an article about firewalls
    and not in one about cycling. The test is what actually ranked and
    fetched, not words in the topic — see VerifiedBundle.is_our_subject. A
    Studio bundle can only contain allowlisted sources, so this never fires
    there."""
    if not bundle.is_our_subject():
        return ""
    cfg = load_course_links()
    vendor = None
    if vendor_name:
        # exact match against the known course-link vendors (case-insensitive)
        vendor = next((v for v in cfg["vendors"] if v.lower() == vendor_name.lower()), None)
    if vendor is None:
        vendor = next((v for v in cfg["vendors"] if v.lower() in bundle.topic.lower()), None)
    if vendor:
        url = cfg["base_url"] + cfg["vendors"][vendor]
        label = f"{vendor} training"
    else:
        # /courses/ was the old fallback and it is a HARD 404 on the live site —
        # so every article for a vendor missing from course_links was instructed
        # to link a dead page (and the ad-hoc generator, which often has no
        # vendor at all, got it every time). /certifications/ is a real page,
        # verified 200.
        url = cfg["base_url"] + "/certifications/"
        label = "Red Education certification training"
    ctas = "\n".join(f'       - "{p}"' for p in cfg["cta_phrases"])
    example_cta = cfg["cta_phrases"][0] if cfg["cta_phrases"] else "Explore our courses"
    return (
        "LINKING & CALL TO ACTION (required):\n"
        f"- Link the text \"{label}\" inline to {url} exactly once (markdown link), "
        "the first time it's natural to mention training.\n"
        f"- Do NOT invent any rededucation.com URL. Beyond the one above, link "
        "ONLY to URLs listed under APPROVED INTERNAL LINKS if that section is "
        "present; if it is not, this is the only internal URL you may use.\n"
        "- NEVER use the site root (https://www.rededucation.com/) as a link "
        "target, and never point two different anchor texts at the same URL — "
        "an anchor reading \"65476V Administering Avaya Infinity\" whose href "
        "is the front page is worse than no link at all.\n"
        "- Link OUT at least once to an authoritative external page — use one "
        "of the source URLs listed in the grounding context above, as a normal "
        "markdown link on a phrase that earns it (a spec, a figure, an "
        "official exam page). One or two is right; this is attribution, not a "
        "link farm, and it must never replace the Red Education links.\n"
        # QA rule V9 caps exact-match repetition at 4. This instruction used
        # to ask for "roughly 10-14 times" (a ~1% density target), which meant
        # the writer was being told to break a rule it was then graded on —
        # every article measured came in at 6-11. The two have to move
        # together; if the ceiling changes, change this line with it.
        "- Use the exact target keyword phrase AT MOST 4 TIMES in the body — "
        "ideally the opening, one H2 and the close. This is a hard ceiling, "
        "not a target: everywhere else use natural variations, the vendor or "
        "product name on its own, or a pronoun. A sentence bent around the "
        "phrase is worse than a missed count, and repetition past 4 is "
        "flagged as keyword stuffing.\n"
        "- End the ARTICLE BODY ITSELF with a call to action — write it as the "
        "final paragraph of the article text. If the output format also has a "
        "separate \"cta\" field, that field is a COPY: putting the CTA only "
        "there leaves the published article with no call to action at all, "
        "because only the body is published. The body is what is checked.\n"
        "- That call to action must include ONE of these approved "
        "phrases character-for-character — this is checked by exact substring match, "
        "so any paraphrase (reordered words, changed wording, singular/plural swaps) "
        "FAILS even though it means the same thing:\n"
        f"{ctas}\n"
        f'  Right: "Ready to take the next step? {example_cta} and get started today." '
        "— the approved phrase appears intact; you added words before/after it, not "
        "inside it.\n"
        '  Wrong (illustrative — this pattern fails no matter which approved phrase you '
        'started from): approved phrase "Explore our courses" written as "Explore what '
        'our courses offer" — inserting/rearranging words inside the phrase breaks the '
        "exact match even though a person would read it as the same CTA."
    )


def keywords_block(keywords: list[str] | None) -> str:
    """Tell the model exactly which SEO keywords must appear in the body.

    This is checked mechanically afterwards (validate.py's keyword_coverage
    check), so the instruction has to be unambiguous: every keyword, verbatim,
    somewhere in the body — not just the meta description. Shared by BOTH
    pipelines' prompts (build_prompt below, and brief.py's
    build_article_prompt) — the Studio Writer originally omitted it, which
    meant the model was graded on an exact phrase it was never shown.
    """
    if not keywords:
        return ""
    kw_list = "\n".join(f'  - "{kw}"' for kw in keywords)
    return (
        "SEO KEYWORDS (required — this is checked mechanically afterwards):\n"
        "Work every one of the following keywords/phrases verbatim into the "
        "article body at least once, wherever they fit naturally (a heading, "
        "the intro, or body prose). Do not keyword-stuff or list them "
        "separately — weave each into a real sentence. If a keyword doesn't "
        "fit anywhere naturally, still include it in as natural a sentence as "
        "you can rather than omitting it.\n"
        f"{kw_list}"
    )


def _tag_sources(bundle: VerifiedBundle) -> dict[str, Source]:
    """Assign stable [S#] tags to each kept source."""
    return {f"S{i+1}": s for i, s in enumerate(bundle.sources)}


def _domain_tag_map(tags: dict[str, Source]) -> dict[str, str]:
    return {s.url: tag for tag, s in tags.items()}


# A claim is one sentence of evidence, not a document. Retrieval that lands
# on a large PDF turns its table of contents and tables into thousands of
# "claims": an F5 certification article pulled in a 563KB NIST ABAC PDF and a
# 269KB case study, yielding 2,101 claims / 470KB of claims block — a ~485KB
# (~120K token) prompt on EVERY attempt. It half-worked, which is why it went
# unnoticed: attempt 1 squeaked through and the retry, now carrying the
# previous draft too, tipped the provider into an error that aborted the loop
# ("Provider error on retry — using best attempt"), so the QA failure it was
# retrying to fix never got fixed. Caps are generous enough that a normal
# bundle (tens of claims) is untouched.
CLAIM_MAX_CHARS = 320
MAX_CLAIMS_PER_GROUP = 40
# PDF table-of-contents dot leaders, and lines that are mostly punctuation or
# digits — table scrapings, page-number columns. Never real evidence.
_JUNK_CLAIM_RE = re.compile(r"\.{4,}|…{2,}")


def _is_junk_claim(text: str) -> bool:
    t = text.strip()
    if len(t) < 25 or _JUNK_CLAIM_RE.search(t):
        return True
    letters = sum(ch.isalpha() for ch in t)
    return letters < len(t) * 0.55      # mostly digits/pipes/dots


def clamp_claim(text: str) -> str:
    t = " ".join(text.split())
    return t if len(t) <= CLAIM_MAX_CHARS else t[:CLAIM_MAX_CHARS].rstrip() + "…"


def usable_claims(claims: list[Claim], status: str,
                  limit: int = MAX_CLAIMS_PER_GROUP) -> list[Claim]:
    """Claims of `status` worth putting in a prompt: junk dropped, capped.
    Shared by the prompt builders and webapp/pipeline.py's brief step so a
    runaway bundle can't blow up either one."""
    return [c for c in claims
            if c.status == status and not _is_junk_claim(c.text)][:limit]


def render_bundle_context(bundle: VerifiedBundle, vendor_name: str | None = None
                          ) -> tuple[str, str, str, dict[str, Source]]:
    """Render the tagged sources/claims blocks + the linking/CTA instructions
    from a VerifiedBundle. `vendor_name` (Studio knows it authoritatively;
    the ad-hoc generator doesn't) is threaded to _linking_block so the real
    course URL is used even when the topic title doesn't name the vendor.
    Returns (sources_block, claims_block, linking_block,
    tags).

    Shared by build_prompt() (the ad-hoc one-shot pipeline) AND
    synthesis/brief.py's build_article_prompt() (the Brief+Writer pipeline) —
    both need the SAME [S#]-tagged grounding context and the SAME exact
    course-URL/approved-CTA-phrase injection, because validate.py's
    citation_integrity/course_link_cta checks require them. A prompt that
    skips this renders prose with no citations and no guaranteed CTA match,
    which fails those checks for reasons unrelated to actual content quality —
    do not let a new caller bypass this helper.
    """
    tags = _tag_sources(bundle)
    url_to_tag = _domain_tag_map(tags)

    # ---- render sources block ----
    src_lines = []
    for tag, s in tags.items():
        vendor = f" (VENDOR source: {s.vendor})" if s.is_vendor else ""
        src_lines.append(
            f"[{tag}] {s.title}{vendor}\n"
            f"     url: {s.url}\n"
            f"     published: {s.published or 'unknown'}\n"
            f"     excerpt: {s.text[:900].strip()}"
        )
    sources_block = "\n\n".join(src_lines)

    # ---- render claims block, grouped by status ----
    def render_claim(c: Claim) -> str:
        tag = url_to_tag.get(c.source_url, "?")
        extra = f"  (also corroborated by: {', '.join(c.corroborated_by)})" if c.corroborated_by else ""
        return f"     - [{tag}] {clamp_claim(c.text)}{extra}"

    verified = usable_claims(bundle.claims, "verified")
    unverified = usable_claims(bundle.claims, "unverified")
    flagged = usable_claims(bundle.claims, "flagged")

    claims_block = "VERIFIED CLAIMS (state directly, with citation):\n"
    claims_block += "\n".join(render_claim(c) for c in verified) or "     (none)"
    claims_block += "\n\nUNVERIFIED CLAIMS (single-sourced — attribute in-text):\n"
    claims_block += "\n".join(render_claim(c) for c in unverified) or "     (none)"
    claims_block += "\n\nFLAGGED CLAIMS (DO NOT use in the article):\n"
    claims_block += "\n".join(render_claim(c) for c in flagged) or "     (none)"

    return sources_block, claims_block, _linking_block(bundle, vendor_name), tags


def build_prompt(bundle: VerifiedBundle, keywords: list[str] | None = None,
                 feedback: str | None = None) -> tuple[str, str, dict[str, Source]]:
    """Return (system_prompt, user_prompt, tag_map).

    `feedback`, when given (from QAReport.feedback_block()), is the previous
    attempt's specific QA failures — appended so a retry can target-fix them
    instead of blindly regenerating from the same prompt."""
    sources_block, claims_block, linking_block, tags = render_bundle_context(bundle)

    # ---- warnings the writer should heed ----
    warnings_block = "\n".join(f"     - {w}" for w in bundle.warnings) or "     (none)"

    region = bundle.region or "a global audience"

    system_prompt = (
        "You are a senior content writer for Red Education, a cybersecurity "
        "training provider. You write accurate, well-sourced, genuinely useful "
        "blog posts that help professionals make certification and career "
        "decisions.\n\n"
        f"HOUSE VOICE:\n{style.VOICE}\n\n"
        f"STRUCTURE:\n{style.STRUCTURE}\n\n"
        f"{style.GROUNDING_RULES}\n\n"
        f"{style.OUTPUT_SCHEMA}"
    )

    kw_block = keywords_block(keywords)

    user_prompt = (
        f"{today_block()}\n\n"
        f"Write a blog post on the topic: \"{bundle.topic}\".\n"
        f"Target region: {region}. Localise salary/market context to this region "
        f"only where the sources support it; otherwise keep it general.\n\n"
        f"SOURCE MATERIAL:\n{sources_block}\n\n"
        f"{claims_block}\n\n"
        f"REVIEWER WARNINGS TO RESPECT:\n{warnings_block}\n\n"
        f"{linking_block}\n\n"
        + (f"{kw_block}\n\n" if kw_block else "")
        + (f"{feedback}\n\n" if feedback else "")
        + "Now write the post following every sourcing rule. Return only the JSON."
    )

    return system_prompt, user_prompt, tags
