"""Content Brief + Writer: turns an approved Topic into a structured brief,
then the brief into a full article — ported faithfully from the real,
working source (src/lib/writer.ts). THE CORE PRINCIPLE (same as
synthesis/topics.py's Topic Engine): the model writes; it never invents a
fact not present in the verified research handed to it (enforced by the
existing GROUNDING_RULES + validate.py, not a new mechanism).

Two LLM steps:
  1. generate_brief()   — topic + verified facts -> a ContentBrief (outline,
     key points, FAQ hints, format, CTA intent). A flattened facts summary is
     fine here; the brief itself is never run through validate_draft().
  2. generate_passing_article() — brief + VerifiedBundle -> a full article,
     using the SAME [S#]-tagged grounding context and course-link/CTA
     injection as the ad-hoc pipeline (via prompt.py's render_bundle_context())
     so validate_draft()'s citation/CTA checks can actually pass. Retries up
     to 3x on a QA block, mirroring generate_passing_draft()'s pattern as a
     separate sibling loop (not shared code — different call signature, and
     forcing a shared abstraction over one small loop isn't worth the
     indirection).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from retrieval.models import VerifiedBundle
from synthesis.models import Draft, references_from_tags
from synthesis.prompt import keywords_block, render_bundle_context
from synthesis.reacs import (REACS_PROMPT_ADDENDUM, REACS_THRESHOLD, compute_reacs,
                             reacs_feedback_block)
from synthesis.text import (clamp_title, fix_writing_mechanics, slugify,
                            refresh_last_updated, today_block)
from . import style

# --------------------------------------------------------------------------- #
# Brief
# --------------------------------------------------------------------------- #
_FORMATS = ["data_guide", "listicle", "explainer"]


@dataclass
class BriefFaq:
    question: str
    answer_hint: str


@dataclass
class ContentBrief:
    title: str
    audience: str
    keyword: str
    format: str   # data_guide | listicle | explainer
    outline: list[str] = field(default_factory=list)         # H2 headings, in order
    key_points: list[str] = field(default_factory=list)
    internal_links: list[str] = field(default_factory=list)
    faq: list[BriefFaq] = field(default_factory=list)
    cta: str = ""
    image_brief: str = ""


BRIEF_SYSTEM_PROMPT = """\
You are the Content Brief Agent for Red Education, an authorised IT training \
company.
You turn an approved topic plus verified research facts into a precise \
article brief for a writer.
Respond with JSON only, no prose, no markdown fences:
{
  "title": "final article title",
  "audience": "one sentence naming the reader persona and their decision",
  "format": "data_guide|listicle|explainer",
  "outline": ["H2 heading", "..."],
  "key_points": ["specific point the article must make", "..."],
  "faq": [{ "question": "...", "answer_hint": "what the answer should cover" }],
  "cta": "one sentence CTA pointing to the mapped Red Education course",
  "image_brief": "one sentence describing the hero image"
}
Title guidance: for certification-guide content, prefer a definitive
reference-documentation title ("The Complete <certification> Guide:
Objectives, Study Plan and Exam Domains") over listicle/tip framings — the
definitive form is what search engines and AI answers retrieve as THE
resource for a certification. The title MUST also contain the exact target
keyword phrase given below, in the same word order with nothing inserted
inside it — this is checked afterwards as a literal substring match, so
paraphrasing, reordering, or adding words INSIDE the phrase fails it even
though a person would read it as equivalent. The match IS case-insensitive,
so capitalise the phrase properly as part of the title (Title Case) — never
paste it in lowercase mid-title ("The Complete ncap certification 2026 Guide"
is wrong; "The Complete NCAP Certification 2026 Guide" is right). Work the
phrase in naturally rather than appending it
artificially: keyword "ccsa certification guide 2026" is satisfied by "The
Complete CCSA Certification Guide 2026: Objectives, Study Plan and Exam
Domains" (the exact phrase appears intact) but NOT by "The Complete CCSA
(Certified Security Administrator) Guide: ... 2026 Exam Domains" (the words
are all there, but not contiguous and not in that exact order).
The title must carry BOTH of the following, because the site's SEO plugin
scores them as two separate checks and a title with only one always fails the
other:
  1. a POWER word — Complete, Comprehensive, Definitive, Essential, Proven,
     Practical, Strategic, Expert, Official, Advanced; and
  2. a SENTIMENT word, positive or negative — Best, Easy, Simple, Smart,
     Powerful, Valuable, Worth, or Hard, Difficult, Costly, Common, Mistakes.
It must also contain a NUMBER (the year is the natural one for a
certification guide).
Hard limit: 120 characters, counted including spaces. Compose within it —
anything longer is trimmed to fit, which costs you the end of the title.
With all of the above plus a long keyword this is tight, so keep the
subtitle after the colon short.
Both words carry weight, so choose them to describe the article honestly
rather than to sell it: "The Complete CCTE R82 Certification Guide 2026:
Objectives, Study Plan and a Genuinely Hard Exam" reads as a reference
document; "The Ultimate Amazing CCTE Secret" does not. One power word and one
sentiment word is the target — not a pile of them.
Do not invent internal link URLs — you have no reliable knowledge of which
Red Education pages currently exist. Leave that to the editor; just write
the brief above."""


TITLE_SYSTEM_PROMPT = """\
You rewrite a single article title for Red Education, an authorised IT \
training company.
Respond with JSON only, no prose, no markdown fences: {"title": "..."}
The new title MUST contain the target keyword phrase verbatim, in the same \
word order, with no words inserted inside it — it is checked as a literal \
substring match, so paraphrasing, reordering, or splitting the phrase all \
fail even though a person would read them as equivalent.
The match IS case-insensitive, so capitalise the phrase properly as part of \
the title (Title Case) — never paste it in lowercase mid-title.
Prefer the definitive reference-guide form, "The Complete <keyword phrase>: \
<what it covers>", and keep the existing title's subject matter. Change only \
what is needed to carry the exact phrase naturally. Maximum 120 characters."""


def regenerate_title(current_title: str, keyword: str, vendor_name: str,
                     max_attempts: int = 3) -> str | None:
    """Rewrite an existing article's title so it literally contains `keyword`.

    Exists because REACS's single_search_intent pillar is decided by the
    title, which the Writer cannot touch (ArticlePackage has no title field)
    — so without this, "Rewrite whole article" could never clear that gate
    on an article whose title was already wrong, no matter how many times it
    was pressed. Rewriting just the title leaves the curated brief (outline,
    key points, hand-added internal links) intact, unlike regenerating the
    whole brief.

    Returns the new title, or None if no attempt produced a compliant one —
    callers keep the existing title in that case and let REACS flag it
    rather than substituting a synthetic title nobody reviewed."""
    from .llm import complete_with_configured_provider, extract_json

    kw = (keyword or "").strip()
    if not kw:
        return None

    feedback = ""
    for attempt in range(1, max(1, max_attempts) + 1):
        prompt = "\n".join([
            today_block(),
            f"Vendor: {vendor_name}",
            f"Current title: {current_title}",
            f"Required exact keyword phrase: {kw}",
            feedback,
        ]).strip()
        raw, _model = complete_with_configured_provider(TITLE_SYSTEM_PROMPT, prompt,
                                                        purpose="title_repair")
        try:
            parsed = extract_json(raw)
        except Exception:
            parsed = None
        title = clamp_title(parsed.get("title") or "", 120) if isinstance(parsed, dict) else ""
        if len(title) >= 10 and kw.lower() in title.lower():
            return title
        feedback = (
            f'REJECTED: "{title}" does not contain "{kw}" verbatim as a '
            "contiguous substring, in that exact word order. Try again.")
    return None


# --------------------------------------------------------------------------- #
# Slice 3.9 — buyer-question coverage
# --------------------------------------------------------------------------- #
# The questions a reader of THIS KIND of article actually arrived with, and
# which the outline must commit to answering.
#
# This is a BRIEF requirement, not a QA rule, and that is the whole point:
# "cheaper to require up front than to detect its absence later". The article
# that proved it (Cortex XSOAR vs XSIAM) was structurally strong and answered
# none of the three questions a buyer comparing two platforms has — cost,
# migration path, and where the vendor is taking each product. No amount of
# post-hoc checking recovers that; the outline had already decided.
#
# Each requirement carries the terms that count as covering it, so the check
# reads the outline the way a reader would rather than demanding an exact
# heading. Deliberately generous: this gates a retry, and a false miss costs
# an LLM call.
BUYER_QUESTIONS: dict[str, list[tuple[str, tuple[str, ...]]]] = {
    "comparison": [
        ("the commercial model — how each option is licensed and paid for",
         ("licen", "pricing", "price", "cost", "subscription", "commercial",
          "budget", "tco", "spend")),
        ("migration or switching cost — what moving actually takes",
         ("migrat", "switch", "transition", "moving", "move from", "cut over",
          "cutover", "replatform", "adopt")),
        ("vendor direction of travel — roadmap position and successor products",
         ("roadmap", "direction", "successor", "end of life", "end-of-life",
          "future", "strategy", "long term", "long-term", "where next",
          # Natural phrasings a writer reaches for instead of "roadmap".
          # Worth being generous: a false miss here does not merely under-
          # report, it triggers a retry that tells the model to add a section
          # it already wrote — the unfollowable-feedback loop that has bitten
          # this codebase three times. "taking" cannot collide with "taking
          # the exam", since these terms only apply to comparison outlines.
          "taking", "heading", "what next", "what's next", "discontinu",
          "sunset", "deprecat", "invest")),
    ],
    "certification_guide": [
        ("the exam code and how to book it",
         ("exam code", "exam number", "registration", "book", "logistics",
          "156-", "exam detail", "sitting", "pearson")),
        ("prerequisites — what you need before sitting it",
         ("prerequisite", "prior", "before you", "entry requirement", "eligib",
          "who should", "readiness", "required experience")),
        ("how long the certification stays valid",
         ("valid", "expir", "how long", "renew", "recertif", "lapse")),
        ("the retake policy if you fail",
         ("retake", "re-take", "resit", "re-sit", "fail", "second attempt",
          "another attempt", "waiting period")),
    ],
}


def missing_buyer_questions(outline: list[str], content_type: str) -> list[str]:
    """Buyer questions this outline does not commit to answering.

    Empty for a content type with no requirements — most of them. Matching is
    on the outline text as a whole, because a single H2 legitimately answers
    two of these at once ("What does it cost and how long is it valid?")."""
    required = BUYER_QUESTIONS.get((content_type or "").lower(), [])
    if not required:
        return []
    haystack = " | ".join(outline).lower()
    return [label for label, terms in required
            if not any(t in haystack for t in terms)]


def buyer_question_block(content_type: str) -> list[str]:
    """Prompt lines requiring this content type's buyer questions, or []."""
    required = BUYER_QUESTIONS.get((content_type or "").lower(), [])
    if not required:
        return []
    return [
        "",
        "BUYER QUESTIONS THE OUTLINE MUST ANSWER (checked after you reply):",
        *[f"  - {label}" for label, _ in required],
        "These are the questions a reader of this kind of article actually "
        "arrived with. One H2 may answer two of them, and they do not need "
        "these exact words — but a reader must be able to find each answer. "
        "An outline that covers the topic well and skips these has missed "
        "what the reader came for.",
    ]


def entity_contract_block(vendor_name: str,
                          certifications: list[str] | None,
                          courses: list[str] | None) -> str:
    """Render the typed vendor catalogue rules used by every writer path."""
    certs = list(certifications or [])
    course_names = list(courses or [])
    return (
        f"ENTITY CONTRACT FOR {vendor_name.upper()} (authoritative):\n"
        "  CERTIFICATIONS/CREDENTIALS: "
        f"{'; '.join(certs[:20]) or 'none recorded'}\n"
        "  TRAINING COURSES: "
        f"{'; '.join(course_names[:30]) or 'none recorded'}\n"
        "Treat these as different entity types, including acronyms and short "
        "names. Never call a listed course a certification, credential, exam, "
        "framework, methodology or discipline. A course may prepare a learner "
        "for a separately named credential; it does not become that credential.\n"
        "CRITICAL COURSE/CREDENTIAL NAMING RULE: A training course may "
        "include a software release such as R82 or R81.20 in its name, "
        "while the recognised certification has a shorter name. "
        "Never use the exact course title immediately before "
        "certification, credential, or exam. Instead, name the "
        "recognised credential from CERTIFICATIONS/CREDENTIALS and "
        "describe the version separately. For example, if the credential "
        "is Certified Troubleshooting Expert (CCTE) and the course is "
        "Check Point Certified Troubleshooting Expert (CCTE) R82, "
        "write the R82 exam associated with the Certified Troubleshooting "
        "Expert (CCTE) credential, not the Check Point Certified "
        "Troubleshooting Expert (CCTE) R82 exam. "
        "Apply this distinction to the title, SEO fields, opening "
        "paragraph, headings, tables and FAQs.\n"
        "Only state prerequisites, former names, course-to-course relationships, "
        "duration by delivery method, voucher inclusion, prices, promotions or "
        "deadlines when an exact verified source supports that exact subject. "
        "Cite the supporting [S#] in the same sentence. Do not turn a recommended "
        "preparatory course into a mandatory prerequisite. Do not generalise one "
        "delivery method's duration to every format. A promotion must include a "
        "verified full future expiry date; otherwise omit it.\n"
        "Use direct third-person editorial prose. Do not write 'we recommend', "
        "'we often see', 'our experience', or imply an unnamed Red Education "
        "employee supplied the advice. Internal links must be relevant to this "
        "article and vendor; use 2-4 earned internal links unless the brief "
        "explicitly requires more. Never insert another vendor's course as a "
        "generic recommendation. In a single-vendor article, do not introduce "
        "another vendor, its product, or an integration with it unless the "
        "title or brief explicitly commissions a comparison. One competitor "
        "source is not permission to change the article's scope.\n"
        "Treat claims about critical bugs, unusable APIs, outages and affected "
        "software versions as high-risk. Include one only when the subject "
        "vendor's own advisory or release note supports the exact version or "
        "issue identifier and the exact impact; cite it in the same sentence. "
        "Otherwise omit the claim. Scope roles, permissions and deployment "
        "steps to the exact product or management console documented by the "
        "source. Describe guided or automated configuration tools as optional "
        "workflows unless a primary source explicitly makes them mandatory.\n\n"
        + style.TECHNICAL_SCOPE_RULES
    )


def build_brief_prompt(topic_title: str, keyword: str, content_type: str,
                       search_intent: str, vendor_name: str,
                       certifications: list[str], courses: list[str],
                       verified_facts: list[str],
                       feedback: str | None = None,
                       theme: str | None = None) -> str:
    lines = [
        today_block(),
        f"Topic: {topic_title}",
        f"Target keyword: {keyword}",
        f"Content type: {content_type} · Search intent: {search_intent}",
        f"Vendor: {vendor_name}",
    ]
    if theme:
        # Carried from the topic batch. The outline is where an article
        # either commits to the theme or quietly drifts into a generic
        # vendor guide, so it has to be stated here and not just upstream.
        lines += [
            f"REQUIRED THEME: {theme}",
            f"This article was commissioned on the theme \"{theme}\". The "
            "outline must cover it substantively — at least one H2 devoted to "
            "it, named specifics rather than a passing mention. Everything "
            "below is supporting material for that theme, not a replacement "
            "for it.",
        ]
    lines += buyer_question_block(content_type)
    lines += [
        "",
        entity_contract_block(vendor_name, certifications, courses),
        "",
        "Verified facts available to the writer (the article may only use "
        "these for specific claims):",
        *[f"- {f}" for f in verified_facts[:25]],
        "",
        "House style to plan for:",
        style.VOICE,
        style.STRUCTURE,
        "",
        REACS_PROMPT_ADDENDUM,
    ]
    if feedback:
        lines += ["", feedback]
    return "\n".join(lines)


def _clamp_str(v, max_len: int) -> str:
    """Trim to `max_len`, at a WORD boundary where one is close enough.

    A hard cut is silent corruption: a 61-character SEO title was clamped to
    60 and "…Administration Career" became "…Administration Ca", which stopped
    the target keyword appearing verbatim and failed a mandatory REACS pillar.
    Nothing downstream can tell a deliberately short title from a guillotined
    one.

    Only backs up to the last space when that space is in the final quarter of
    the allowance — otherwise a string with no spaces near the end would lose
    far more than it should, and a hard cut is the better of two bad options.
    """
    if not isinstance(v, str):
        return ""
    v = v.strip()
    if len(v) <= max_len:
        return v
    cut = v[:max_len]
    space = cut.rfind(" ")
    if space >= int(max_len * 0.75):
        return cut[:space].rstrip(" ,;:-–—")
    return cut


def _str_array(v, max_items: int, max_len: int) -> list[str]:
    if not isinstance(v, list):
        return []
    out = [x.strip()[:max_len] for x in v if isinstance(x, str) and x.strip()]
    return out[:max_items]


def parse_brief_response(raw_text: str) -> tuple[bool, ContentBrief | str]:
    from .llm import extract_json

    try:
        parsed = extract_json(raw_text)
    except Exception:
        return False, "The model did not return valid JSON."
    if not isinstance(parsed, dict):
        return False, "The model did not return a JSON object."

    title = clamp_title(parsed.get("title") or "", 120)
    if len(title) < 10:
        return False, "The brief has no usable title."
    outline = _str_array(parsed.get("outline"), 12, 120)
    if len(outline) < 3:
        return False, "The brief outline needs at least 3 sections."
    fmt = parsed.get("format") if parsed.get("format") in _FORMATS else "explainer"

    faq_raw = parsed.get("faq") if isinstance(parsed.get("faq"), list) else []
    faq = []
    for f in faq_raw:
        if not isinstance(f, dict):
            continue
        q = _clamp_str(f.get("question"), 200)
        if len(q) <= 5:
            continue
        faq.append(BriefFaq(question=q, answer_hint=_clamp_str(f.get("answer_hint"), 300)))
        # REACS's faq_depth mandatory pillar requires >=8 (target 10) real
        # FAQs on the finished article — capping the brief's hints at 6
        # guaranteed that pillar would fail before the writer even ran.
        if len(faq) >= 10:
            break

    return True, ContentBrief(
        # keyword is filled in by generate_brief() from the topic's actual
        # keyword, not parsed here — the schema above never asks the model
        # for one, so parsing parsed.get("keyword") always came back empty.
        title=title, audience=_clamp_str(parsed.get("audience"), 300),
        keyword="", format=fmt,
        outline=outline, key_points=_str_array(parsed.get("key_points"), 15, 300),
        # internal_links starts empty deliberately — the model has no
        # reliable knowledge of which rededucation.com pages exist, so
        # letting it invent URLs here produced broken links. Real ones only
        # come from the verified suggestion panel (synthesis/gaps.py,
        # backed by the PublishedPost scrape) via the Brief step's "+ Add".
        internal_links=[],
        faq=faq, cta=_clamp_str(parsed.get("cta"), 300),
        image_brief=_clamp_str(parsed.get("image_brief"), 300),
    )


def generate_brief(topic_title: str, keyword: str, content_type: str,
                   search_intent: str, vendor_name: str,
                   certifications: list[str], courses: list[str],
                   verified_facts: list[str],
                   max_attempts: int = 3,
                   theme: str | None = None) -> tuple[bool, ContentBrief | str]:
    """Retries up to max_attempts if the parsed title doesn't literally
    contain the target keyword. This matters because REACS's
    single_search_intent mandatory check verifies exactly that on the
    FINISHED ARTICLE — but the article's title is `brief.title`, fixed here,
    at brief-creation time, and the Writer has no ability to change it
    (ArticlePackage, the Writer's return shape, has no title field at all).
    So unlike every other REACS pillar, a title/keyword mismatch can ONLY be
    fixed here — generate_passing_article's own retry loop (synthesis/
    brief.py) retries everything the Writer actually controls, but would
    fail this exact check identically on every attempt no matter how many
    times it ran, since none of them touch the title. This is the one place
    that can actually prevent it."""
    from .llm import complete_with_configured_provider

    feedback = None
    best = None
    attempts = max(1, max_attempts)
    for attempt in range(1, attempts + 1):
        prompt = build_brief_prompt(topic_title, keyword, content_type, search_intent,
                                    vendor_name, certifications, courses, verified_facts,
                                    feedback=feedback, theme=theme)
        raw, _model = complete_with_configured_provider(BRIEF_SYSTEM_PROMPT, prompt,
                                                        purpose="brief")
        ok, result = parse_brief_response(raw)
        if not ok:
            if attempt == attempts:
                # Don't throw away a good brief because the LAST attempt
                # happened to come back unparseable. We only ever retry to
                # improve an already-usable brief's title, so a held `best`
                # is strictly better than failing the whole step.
                if best is not None:
                    return True, best
                return False, result
            continue
        # Authoritative — the topic's own keyword, not a model guess (the
        # schema doesn't even ask the model for one).
        result.keyword = keyword
        missing = missing_buyer_questions(result.outline, content_type)
        if keyword.lower() in result.title.lower() and not missing:
            return True, result
        best = best or result
        problems = []
        if keyword.lower() not in result.title.lower():
            problems.append(
                f'THE TITLE WAS REJECTED: "{result.title}" does not contain '
                f'the exact required phrase "{keyword}" — this is checked '
                "afterwards as a literal substring match. Rewrite the title "
                f'so it contains "{keyword}" verbatim, character-for-'
                "character, in that exact order, while keeping the same "
                "definitive-guide style.")
        if missing:
            # Slice 3.9. Name the gap and say to ADD or RETITLE a section —
            # "cover these" on its own reads as "write more", and the model
            # tends to answer that by padding an existing section rather than
            # committing a heading to the question.
            problems.append(
                "THE OUTLINE SKIPS BUYER QUESTIONS a reader of this article "
                "arrived with: " + "; ".join(missing)
                + ". Add or retitle a section so each is plainly answered. "
                "Keep the rest of the outline as it is.")
        feedback = "PREVIOUS ATTEMPT: " + "\n\n".join(problems)
    # Exhausted attempts without a matching title — return the best
    # available (keyword already set) rather than blocking brief creation
    # over one fixable-but-imperfect title. REACS's single_search_intent
    # check will correctly flag it on the finished article, visibly, instead
    # of this failing silently or blocking the whole brief step.
    return True, best


# --------------------------------------------------------------------------- #
# Writer
# --------------------------------------------------------------------------- #
@dataclass
class ArticlePackage:
    content: str            # markdown
    meta_title: str
    meta_description: str
    excerpt: str
    faq: list[dict] = field(default_factory=list)   # [{"question":..,"answer":..}]
    cta: str = ""
    word_count: int = 0


# NOTE: this is intentionally a DIFFERENT shape from style.OUTPUT_SCHEMA (the
# ad-hoc pipeline's title/slug/body_markdown/faq/references_used). The Writer
# here returns "content" (not "body_markdown"), plus meta_title/excerpt/cta
# fields the ad-hoc Draft schema doesn't have — parse_article_response() below
# reads these exact keys. Do NOT swap in style.OUTPUT_SCHEMA here — that was a
# real bug caught during live testing (the model dutifully followed whichever
# schema was in the prompt, and parsing silently failed against the other one).
ARTICLE_OUTPUT_SCHEMA = """\
Return ONLY a JSON object (no markdown fences, no preamble) with keys:
{
  "content": string (the article body ONLY — do not repeat the title as a
    heading, start straight in with the opening paragraph; every main
    section from the brief's outline is a ## H2, H3 is only for sub-points
    inside a section; [S#] citations; including the FAQ section),
  "meta_title": string (<= 60 characters. This is the page's SEO title, and
                 the site's SEO plugin scores FOUR separate things in it.
                 Include them in this priority order when space is tight:
                   1. the target keyword VERBATIM, near the START — scored on
                      an exact match of that phrase;
                   2. a NUMBER, normally the year;
                   3. a SENTIMENT word — Best, Easy, Simple, Smart, Worth,
                      Hard, Costly, Common, Mistakes;
                   4. a POWER word — Complete, Essential, Proven, Practical,
                      Definitive, Expert, Official.
                 Sentiment is ranked above power only because those words are
                 shorter and 60 characters is tight with a long keyword.
                 Do not pad to hit all four — a title that reads as spam is
                 worse than one missing a word. For reference,
                 "<keyword> | Red Education" is what this used to produce and
                 it fails three of the four),
  "meta_description": string (<= 155 characters, includes the keyword),
  "excerpt": string (1-2 sentence article excerpt),
  "faq": [ {"question": string, "answer": string}, ... ],
  "cta": string (a COPY of the article's closing call-to-action sentence —
         the same sentence must ALSO be the final paragraph of "content"
         above; this field is a duplicate for indexing, never the only place
         the CTA appears)
}"""


# --------------------------------------------------------------------------- #
# Targeted fix: a PATCH, not a rewrite
# --------------------------------------------------------------------------- #
# Asking for the whole article back and asking nicely for the untouched parts
# to be reproduced verbatim does not work, and cannot be made to work by
# wording. The model complies mostly, and "mostly" is what the editor sees as
# "I fixed one check and an unrelated one went amber" — every unrequested edit
# is another chance to trip a check that was passing.
#
# A patch removes the question. The draft is numbered block by block, the model
# returns edits keyed to those numbers, and a block it does not name is not
# rewritten — it is not in the response at all. Minimality stops being a
# request and becomes a property of the format.
#
# Numbers rather than find/replace on purpose: a find/replace contract needs
# the model to reproduce a span of the draft character-for-character before it
# can change anything, which is the exact skill this whole problem says it does
# not reliably have. An integer it can copy; the `anchor` field then catches
# the one failure a number still permits, naming the wrong block.
PATCH_OUTPUT_SCHEMA = """\
Return ONLY a JSON object (no markdown fences, no preamble) with keys:
{
  "edits": [
    {
      "block": integer — the [n] of the block you are changing,
      "anchor": string — the first six or so words of that block, copied
                exactly. This proves you are editing the block you think you
                are; an edit whose anchor does not match its block is
                discarded and its problem stays unfixed,
      "action": "replace" | "insert_after" | "delete",
      "text": string — for "replace", the block's new markdown; for
              "insert_after", the new block to place after it; omit for
              "delete",
      "fixes": string — which of the listed problems this edit addresses
    }, ...
  ],
  "meta_title": string or null,
  "meta_description": string or null,
  "excerpt": string or null,
  "cta": string or null,
  "faq": [ {"question": string, "answer": string}, ... ] or null
}
The five fields after "edits" are the article's non-body fields. Return null
for any you are NOT changing — null means "keep exactly what it has now".
Return a full replacement value for any you ARE changing."""


@dataclass
class Edit:
    """One change to one numbered block of the draft."""
    block: int              # 1-based, as numbered in the prompt
    action: str             # replace | insert_after | delete
    text: str = ""
    anchor: str = ""
    fixes: str = ""


@dataclass
class ArticlePatch:
    edits: list[Edit]
    rejected: list[str]                 # edits dropped, and why
    meta_title: str | None = None       # None = keep what the article has
    meta_description: str | None = None
    excerpt: str | None = None
    cta: str | None = None
    faq: list[dict] | None = None


# The article split into the units the patch addresses. One paragraph, one
# heading, one whole table, one whole list — anything separated by a blank
# line. Shared with webapp/pipeline.py so the numbers the model is shown and
# the numbers the patch is applied to can never drift apart.
BLOCK_SPLIT = re.compile(r"\n\s*\n")


def split_blocks(content: str) -> list[str]:
    return [b for b in BLOCK_SPLIT.split(content or "") if b.strip()]


def number_blocks(blocks: list[str]) -> str:
    return "\n\n".join(f"[{i}] {b}" for i, b in enumerate(blocks, 1))


def _anchor_matches(anchor: str, block: str) -> bool:
    """Does this edit name the block it thinks it does?

    Whitespace-insensitive containment rather than a strict prefix: the model
    tends to quote from the start of the prose and skip a leading "## " or
    "**", which is a formatting detail, not a wrong block. Anything under
    eight characters cannot discriminate between blocks, so it is waved
    through rather than used to reject a good edit.
    """
    a = re.sub(r"\s+", " ", (anchor or "")).strip().lower()
    if len(a) < 8:
        return True
    return a[:60] in re.sub(r"\s+", " ", block).strip().lower()


def parse_patch_response(raw_text: str, blocks: list[str],
                         acronyms: set[str] | None = None,
                         vendor_terms: list[str] | None = None
                         ) -> tuple[bool, ArticlePatch | str]:
    """Read a patch, discarding any edit that cannot be applied safely.

    Fails outright only when there is nothing usable at all — bad JSON, or
    every edit rejected — because a partial patch is still worth applying and
    the rejects are reported so a retry can be told what went wrong.
    """
    from .llm import extract_json

    try:
        parsed = extract_json(raw_text)
    except Exception:
        return False, "The model did not return valid JSON."
    if not isinstance(parsed, dict):
        return False, "The model did not return a JSON object."
    raw_edits = parsed.get("edits")
    if not isinstance(raw_edits, list):
        return False, "The model returned no edits list."

    def _fix(s: str) -> str:
        return fix_writing_mechanics(s, acronyms, vendor_terms)

    edits, rejected = [], []
    for e in raw_edits:
        if not isinstance(e, dict):
            continue
        action = str(e.get("action") or "replace").strip().lower()
        if action not in ("replace", "insert_after", "delete"):
            rejected.append(f"unknown action {action!r}")
            continue
        try:
            n = int(e.get("block"))
        except (TypeError, ValueError):
            rejected.append(f"edit with no usable block number: {e.get('anchor')!r}")
            continue
        if not 1 <= n <= len(blocks):
            rejected.append(f"block {n} does not exist (the draft has "
                            f"{len(blocks)})")
            continue
        if not _anchor_matches(e.get("anchor") or "", blocks[n - 1]):
            # Almost always an off-by-one, and applying it would rewrite a
            # paragraph nobody complained about — the exact failure this
            # whole format exists to prevent.
            rejected.append(f"block {n}: anchor {str(e.get('anchor'))[:40]!r} "
                            f"is not in that block")
            continue
        text = _fix(str(e.get("text") or "").strip())
        if action != "delete" and not text:
            rejected.append(f"block {n}: {action} with no text")
            continue
        edits.append(Edit(block=n, action=action, text=text,
                          anchor=str(e.get("anchor") or ""),
                          fixes=str(e.get("fixes") or "")))

    if not edits and not any(parsed.get(k) is not None for k in
                             ("meta_title", "meta_description", "excerpt",
                              "cta", "faq")):
        return False, ("No usable edit in the response"
                       + (": " + "; ".join(rejected[:3]) if rejected else "."))

    def _opt(key: str, limit: int) -> str | None:
        v = parsed.get(key)
        return _clamp_str(_fix(v), limit) if isinstance(v, str) and v.strip() else None

    faq = None
    if isinstance(parsed.get("faq"), list):
        faq = []
        for f in parsed["faq"]:
            if not isinstance(f, dict):
                continue
            q = _clamp_str(_fix(f.get("question")), 200)
            a = _clamp_str(_fix(f.get("answer")), 800)
            if q and a:
                faq.append({"question": q, "answer": a})
            if len(faq) >= 10:
                break

    return True, ArticlePatch(
        edits=edits, rejected=rejected,
        meta_title=_opt("meta_title", 60),
        meta_description=_opt("meta_description", 155),
        excerpt=_opt("excerpt", 400),
        cta=_opt("cta", 300),
        faq=faq or None)


def apply_patch(blocks: list[str], edits: list[Edit]) -> str:
    """`blocks` with `edits` applied, as markdown.

    Highest block number first, so applying one edit never shifts the block a
    later edit is aiming at. Within one block an insert_after is applied
    before a replace of that same block, for the same reason: the insert
    lands at n+1 and would otherwise be displaced by the replacement.
    """
    out = list(blocks)
    for e in sorted(edits, key=lambda e: (-e.block,
                                          0 if e.action == "insert_after" else 1)):
        i = e.block - 1
        if e.action == "replace":
            out[i] = e.text
        elif e.action == "insert_after":
            out.insert(i + 1, e.text)
        else:
            out[i] = ""
    return "\n\n".join(b for b in out if b.strip())


def build_article_prompt(brief: ContentBrief, bundle: VerifiedBundle,
                         vendor_name: str, feedback: str | None = None,
                         entities: list[str] | None = None,
                         previous_content: str | None = None,
                         link_menu: list[tuple[str, str]] | None = None,
                         previous_fields: dict | None = None,
                         patch_mode: bool = False,
                         certifications: list[str] | None = None,
                         courses: list[str] | None = None,
                         ) -> tuple[str, str, dict]:
    """Return (system_prompt, user_prompt, tags) — reuses the exact same
    [S#]-tagged grounding context and course-link/CTA injection as the ad-hoc
    pipeline's build_prompt(), layering the brief's outline/key points/format/
    FAQ/CTA guidance on top. Without this, validate_draft()'s
    citation_integrity/course_link_cta checks would hard-fail on every
    attempt for reasons unrelated to article quality — see the module
    docstring and synthesis/prompt.py's render_bundle_context().

    `feedback`, when given (from QAReport.feedback_block()), is the previous
    attempt's specific QA failures — appended so a retry can target-fix
    them instead of blindly regenerating from the same prompt."""
    sources_block, claims_block, linking_block, tags = render_bundle_context(bundle, vendor_name)
    warnings_block = "\n".join(f"     - {w}" for w in bundle.warnings) or "     (none)"

    system_prompt = (
        "You are the Writer Agent for Red Education, an authorised IT "
        "training company. You write one complete blog article from a "
        "brief, using ONLY the verified facts provided for any specific "
        "claim.\n\n"
        f"HOUSE VOICE:\n{style.VOICE}\n\n"
        f"STRUCTURE:\n{style.STRUCTURE}\n\n"
        f"{style.GROUNDING_RULES}\n\n"
        f"{REACS_PROMPT_ADDENDUM}\n\n"
        + (PATCH_OUTPUT_SCHEMA if patch_mode else ARTICLE_OUTPUT_SCHEMA)
    )

    brief_block = (
        f"BRIEF:\n"
        f"  Title: {brief.title}\n"
        f"  Audience: {brief.audience}\n"
        f"  Format: {brief.format}\n"
        f"  Target keyword: {brief.keyword}\n"
        f"  Outline: {'; '.join(brief.outline)}\n"
        f"  Key points to make: {'; '.join(brief.key_points)}\n"
        f"  Internal links to weave in (REQUIRED: each URL below must appear "
        f"as a markdown link somewhere natural in the body — the editor chose "
        f"these deliberately): {'; '.join(brief.internal_links) or '(none)'}\n"
        f"  FAQ to cover: "
        + "; ".join(f"{f.question} ({f.answer_hint})" for f in brief.faq)
        + f"\n  CTA intent: {brief.cta}\n"
    )

    # The verbatim-keyword requirement (validate_draft grades this article
    # with keywords=[brief.keyword], an exact substring match) — omitting
    # this block was a real bug: the model was graded on a phrase it was
    # never shown, so keyword_coverage failed on most Studio articles.
    kw_block = keywords_block([brief.keyword] if brief.keyword else None)

    # Semantic-SEO entity coverage: real product/cert/course names from the
    # Vendor KB, so the article covers the topic's related entities the way
    # ranking pages do — graded WARN-only by validate_draft's entity_coverage.
    entities_block = ""
    if entities:
        listed = "\n".join(f"  - {e}" for e in entities[:25])
        entities_block = (
            "RELATED VENDOR ENTITIES (semantic coverage): where they genuinely "
            "fit the outline, reference at least 3 of these by their exact "
            "name — products, certifications, and courses readers and search "
            "engines associate with this topic. Do NOT force ones that don't "
            f"belong.\n{listed}"
        )

    # A retry gets the previous draft back, not just a list of complaints.
    # Without it the feedback's "edit the previous approach, don't just
    # regenerate" was impossible to obey — the model had never been shown
    # that approach, so every attempt was an INDEPENDENT sample of a dozen
    # simultaneous structural constraints. In practice checks flipped at
    # random between attempts (a draft would gain the required table and
    # lose the "Last updated" line it already had), and the loop oscillated
    # instead of converging. Handing back the draft turns a retry into a
    # targeted repair of known-good text.
    revision_block = ""
    if feedback and previous_content:
        # EVERY FIELD THE CALLER MAY WRITE BACK HAS TO BE VISIBLE HERE.
        # Handing back only the body meant a fix aimed at the SEO title was
        # impossible to make: the model was told "the SEO title fails a Rank
        # Math check", never shown what that title said, and regenerated the
        # field blind — landing on something near-identical that missed the
        # same check. Three attempts in a row on one live article.
        #
        # The same trap was waiting for the FAQ and the CTA: faq_depth and
        # course_link_cta are both offered as fixable and neither field was in
        # the prompt. Anything writable must be shown, or the fix is a lottery.
        # tests/test_qa_rules.py asserts writable and visible stay in step.
        meta_block = ""
        if previous_fields:
            lines = []
            for key in ("meta_title", "meta_description", "excerpt", "cta"):
                if previous_fields.get(key):
                    lines.append(f'  {key}: "{previous_fields[key]}"')
            _faq = previous_fields.get("faq") or []
            if _faq:
                lines.append(f"  faq ({len(_faq)} entries):")
                for f in _faq:
                    q = (f.get("question") or "") if isinstance(f, dict) else ""
                    a = (f.get("answer") or "") if isinstance(f, dict) else ""
                    lines.append(f'    Q: {q}')
                    lines.append(f'    A: {a}')
            if lines:
                meta_block = (
                    "YOUR PREVIOUS NON-BODY FIELDS (revise these too — they "
                    "are part of the article, and a problem below may be about "
                    "one of them rather than the body. Reproduce any you are "
                    "not asked to change exactly as they are):\n"
                    + "\n".join(lines) + "\n\n")
        if patch_mode:
            revision_block = (
                "YOUR PREVIOUS DRAFT, numbered one block per paragraph. Return "
                "a LIST OF EDITS against these numbers — not the article:\n"
                f"{number_blocks(split_blocks(previous_content))}\n\n"
                + meta_block +
                "Edit as FEW blocks as you can. Every block you do not list is "
                "kept exactly as it is, character for character — that is the "
                "point of this format, and it is what protects the checks this "
                "draft already passes. Do not list a block you are not "
                "changing, and do not renumber anything.\n"
                "This matters more than it looks: the draft above already "
                "satisfies checks you cannot see, and rewriting settled prose "
                "silently breaks them — most often by re-deriving a sentence "
                "from the source material and landing on its exact wording, "
                "which the plagiarism check then rejects. (Quoting the DRAFT "
                "is what these edits are built on; copying the SOURCES "
                "verbatim is still forbidden.)\n"
                "If a problem needs a whole new paragraph, use insert_after on "
                "the block it should follow. If a problem is in the SEO title, "
                "the meta description, the excerpt, the CTA or the FAQ, change "
                "that field instead — those are not blocks.\n")
        else:
            revision_block = (
            "YOUR PREVIOUS DRAFT (revise THIS text — do not start over):\n"
            f"{previous_content}\n\n"
            + meta_block +
            "Return the COMPLETE corrected article. Reproduce every sentence "
            "NOT named in the problems below character-for-character from the "
            "draft above — copy them across unchanged; do not re-word, "
            "re-order, condense or otherwise 'improve' them. Change only what "
            "is needed to fix the listed problems.\n"
            "This matters more than it looks: the draft above already "
            "satisfies checks you cannot see, and rewriting settled prose "
            "silently breaks them — most often by re-deriving a sentence from "
            "the source material and landing on its exact wording, which the "
            "plagiarism check then rejects. (Copying the DRAFT verbatim is "
            "required; copying the SOURCES verbatim is still forbidden.)\n"
            )

    # Real URLs for the vendor's own pages. Without this the writer was asked
    # to name specific courses, given URLs for almost none of them, and told
    # not to invent any — so it aimed ten different anchors at the site root
    # and the vendor page. A menu of real targets is the only way "link the
    # courses you mention" is an instruction it can actually follow.
    link_block = ""
    if link_menu:
        listed = "\n".join(f"  - {title} -> {url}" for title, url in link_menu)
        link_block = (
            "APPROVED INTERNAL LINKS (real pages on rededucation.com):\n"
            f"{listed}\n"
            "Rules: copy a URL character-for-character from this list; link at "
            "least 2 DIFFERENT ones; use each URL at most once; and if you name "
            "a course that is not on this list, mention it as plain text rather "
            "than linking it to something else.\n"
            # Scoped to anchor text originally, which is exactly why body
            # prose kept shipping "ccte r82 certification" while every link
            # was correct: the model was following the rule it was given.
            "Capitalisation, THROUGHOUT the article and not only in link "
            "anchors: keep certification codes, product names and release "
            "numbers spelled exactly as this list spells them — CCSE, CCME, "
            "CCSA, CCTE, PCNSE, EDU-210, BIG-IP, R82. Never title-case an "
            "acronym (\"Ccse\", \"Pcnse\") and never lower-case one "
            "(\"ccte r82\"); these are initialisms and read as errors to the "
            "reader. This applies in headings, body sentences, the FAQ and "
            "the meta fields alike.\n"
        )

    user_prompt = (
        f"{today_block()}\n\n"
        f"Write the article defined by this brief, for vendor: {vendor_name}.\n\n"
        f"{entity_contract_block(vendor_name, certifications, courses)}\n\n"
        f"{brief_block}\n"
        + (f"{entities_block}\n\n" if entities_block else "")
        + f"SOURCE MATERIAL:\n{sources_block}\n\n"
        f"{claims_block}\n\n"
        f"REVIEWER WARNINGS TO RESPECT:\n{warnings_block}\n\n"
        f"{linking_block}\n\n"
        + (f"{kw_block}\n\n" if kw_block else "")
        + (f"{link_block}\n" if link_block else "")
        + (f"{revision_block}\n" if revision_block else "")
        + (f"{feedback}\n\n" if feedback else "")
        + ("Now return your edits as JSON. Only the blocks that must change."
           if patch_mode and revision_block else
           "Now return the corrected article as JSON, in full."
           if revision_block else
           "Now write the article following every sourcing rule and the brief "
           "above. Return only the JSON.")
    )
    return system_prompt, user_prompt, tags


def count_words(markdown: str) -> int:
    # Strips table pipes/separator dashes too (matches synthesis/models.py's
    # Draft.word_count()) — otherwise a table's "| --- | --- |" syntax gets
    # counted as extra words, inflating the count for exactly the tabular
    # content this pipeline now encourages (see synthesis/style.py's
    # STRUCTURE).
    text = re.sub(r"[#*_`>\[\]()|-]", " ", markdown)
    return len([w for w in text.split() if w])


def parse_article_response(raw_text: str,
                           acronyms: set[str] | None = None,
                           vendor_terms: list[str] | None = None
                           ) -> tuple[bool, ArticlePackage | str]:
    """Fails only on genuinely unusable output — unparseable JSON, or no
    body at all.

    There used to be a <1000-word fast-fail here, meant to skip a needless QA
    run on an obviously short draft. It cost far more than it saved: a parse
    failure carries no QAReport, so generate_passing_article's retry path had
    no feedback and no previous draft to hand back, and three attempts became
    three blind identical re-rolls that raised "Writing failed after 3
    attempt(s): The draft is too short (922 words)". Letting a short draft
    through to validate_draft's structure_length check (fail under 1300,
    target 1500-2100) costs nothing — QA is local, deterministic computation,
    no LLM call — and produces exactly the targeted "too short: N words"
    feedback plus the draft itself, so the retry EXPANDS it instead of
    starting over. Length was never this function's call to make; the
    docstring already said validate_draft was the authoritative gate."""
    from .llm import extract_json

    try:
        parsed = extract_json(raw_text)
    except Exception:
        return False, "The model did not return valid JSON."
    if not isinstance(parsed, dict):
        return False, "The model did not return a JSON object."

    content = parsed.get("content") if isinstance(parsed.get("content"), str) else ""
    content = content.strip()
    wc = count_words(content)
    if wc == 0:
        return False, "The model returned no article body."

    faq_raw = parsed.get("faq") if isinstance(parsed.get("faq"), list) else []
    faq = []
    for f in faq_raw:
        if not isinstance(f, dict):
            continue
        q, a = _clamp_str(f.get("question"), 200), _clamp_str(f.get("answer"), 800)
        if q and a:
            faq.append({"question": q, "answer": a})
        # See parse_brief_response's identical comment — REACS requires >=8
        # FAQs (target 10); this used to cap at 6, guaranteeing that pillar
        # would fail regardless of what the model produced.
        if len(faq) >= 10:
            break

    # Mechanical corrections applied HERE, at the single point every write
    # path funnels through (first draft, targeted fix, whole rewrite), so no
    # future caller can add a fourth path that skips them. The model writes
    # "ccte r82 certification" often enough that flagging it was never going
    # to be enough — the KB already knows the correct spelling, so apply it.
    # Every field below is published, so all of them get the same treatment.
    def _fix(s: str) -> str:
        return fix_writing_mechanics(s, acronyms, vendor_terms)

    corrected_content = refresh_last_updated(_fix(content))
    return True, ArticlePackage(
        content=corrected_content,
        meta_title=_clamp_str(_fix(parsed.get("meta_title") or ""), 60),
        meta_description=_clamp_str(_fix(parsed.get("meta_description") or ""), 155),
        excerpt=_clamp_str(_fix(parsed.get("excerpt") or ""), 400),
        faq=[{"question": _fix(f.get("question", "")),
              "answer": _fix(f.get("answer", ""))} for f in faq],
        cta=_clamp_str(_fix(parsed.get("cta") or ""), 300),
        word_count=count_words(corrected_content),
    )


# --------------------------------------------------------------------------- #
# Section rewrite
# --------------------------------------------------------------------------- #
SECTION_REWRITE_SCHEMA = """\
Return ONLY a JSON object (no markdown fences, no preamble) with one key:
{
  "section": string (the complete replacement for that ONE section — it MUST
    begin with the exact same "## <heading>" line, keep [S#] citations for
    every specific claim, and use H3 only for sub-points inside the section)
}"""


def build_section_rewrite_prompt(article_markdown: str, heading: str,
                                 brief: ContentBrief, bundle: VerifiedBundle,
                                 vendor_name: str,
                                 instruction: str | None = None,
                                 certifications: list[str] | None = None,
                                 courses: list[str] | None = None,
                                 ) -> tuple[str, str, dict]:
    """Rewrite ONE H2 section in place, grounded in the SAME tagged sources
    the article originally cited (the caller rebuilds the bundle from the
    article's stored references, in tag order — see webapp/pipeline.py's
    rewrite_section). The full article is shown for context so the new
    section doesn't repeat or contradict its neighbours, but the model is
    told to return only the one section."""
    sources_block, claims_block, linking_block, tags = render_bundle_context(bundle, vendor_name)

    system_prompt = (
        "You are the Writer Agent for Red Education, an authorised IT "
        "training company. You rewrite ONE section of an existing blog "
        "article, using ONLY the verified facts provided for any specific "
        "claim.\n\n"
        f"HOUSE VOICE:\n{style.VOICE}\n\n"
        f"{style.GROUNDING_RULES}\n\n"
        f"{REACS_PROMPT_ADDENDUM}\n\n"
        f"{SECTION_REWRITE_SCHEMA}"
    )

    user_prompt = (
        f"{today_block()}\n\n"
        f"{entity_contract_block(vendor_name, certifications, courses)}\n\n"
        f"Here is the full current article (vendor: {vendor_name}, target "
        f"keyword: {brief.keyword}) — for context only, do NOT return it:\n\n"
        f"{article_markdown}\n\n"
        f"---\n"
        f"Rewrite ONLY the section titled \"## {heading}\". Aim for roughly "
        "180-250 words of concrete, sourced detail. Don't repeat points other "
        "sections already make; keep the article's flow into the next "
        "section.\n"
        + (f"\nEDITOR'S GUIDANCE for the rewrite: {instruction}\n" if instruction else "")
        + f"\nSOURCE MATERIAL (cite these, same tags as the article):\n{sources_block}\n\n"
        f"{claims_block}\n\n"
        f"{linking_block}\n"
        "(The linking/CTA rules above apply to the ARTICLE as a whole — if "
        "the course link or CTA currently lives in the section you are "
        "rewriting, keep it there; otherwise do not add a duplicate.)\n\n"
        "Now return only the JSON with the rewritten section."
    )
    return system_prompt, user_prompt, tags


def parse_section_response(raw_text: str, heading: str) -> tuple[bool, str]:
    """Returns (True, section_markdown) or (False, error). The section must
    open with the same H2 heading line so it can be spliced back in place."""
    from .llm import extract_json

    try:
        parsed = extract_json(raw_text)
    except Exception:
        return False, "The model did not return valid JSON."
    section = parsed.get("section") if isinstance(parsed, dict) else None
    if not isinstance(section, str) or not section.strip():
        return False, "The model returned no section content."
    section = section.strip()
    first_line = section.splitlines()[0].strip()
    if not first_line.startswith("## "):
        # Salvage: prepend the heading rather than failing the whole call.
        section = f"## {heading}\n\n{section}"
    if count_words(section) < 60:
        return False, f"The rewritten section is too thin ({count_words(section)} words)."
    return True, section


def replace_section(article_markdown: str, heading: str, new_section: str) -> str | None:
    """Splice `new_section` over the H2 section titled `heading`. Returns the
    new article markdown, or None if no such section exists. Plain
    line-anchored string matching (headings can contain regex metacharacters,
    so no regex on the heading itself)."""
    lines = article_markdown.splitlines()
    target = f"## {heading.strip()}"
    start = next((i for i, ln in enumerate(lines) if ln.strip() == target), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines))
                if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[:start] + new_section.splitlines() + lines[end:])


def _rank(candidate: tuple) -> tuple:
    """Ranking key for "best attempt so far" when nothing clears both gates
    in the attempt budget. `candidate` is (article, draft, report, model_used,
    reacs_report). QA-passing beats QA-failing outright; among QA-failures,
    fewer failed checks is better; among QA-passes, a higher REACS score is
    better (REACS is only ever computed once QA has already passed, so a
    QA-failing candidate always has reacs_report=None, scoring -1 here —
    never mistaken for a real, if low, REACS score)."""
    _, _, report, _, reacs_report = candidate
    reacs_score = reacs_report.score if reacs_report is not None else -1
    return (report.passed, -len(report.failed), reacs_score)


def generate_passing_article(brief: ContentBrief, bundle: VerifiedBundle,
                             vendor_name: str, max_attempts: int = 3,
                             on_progress=None, serp=None,
                             entities: list[str] | None = None,
                             certifications: list[str] | None = None,
                             courses: list[str] | None = None,
                             has_byline: bool = False,
                             seed_content: str | None = None,
                             seed_feedback: str | None = None,
                             link_menu: list[tuple[str, str]] | None = None,
                             allowed_link_urls: set[str] | None = None,
                             acronyms: set[str] | None = None,
                             kb=None,
                             outline: list[str] | None = None):
    """Write the article from the brief, retrying up to max_attempts on
    EITHER a QA block or a REACS block — same pattern as
    generate_passing_draft() in generate.py, kept as a deliberate sibling
    (not shared code; see module docstring). Returns (ArticlePackage, Draft,
    QAReport, model_used, ReacsReport | None) — the Draft is a QA-shaped
    wrapper around the article content, reusing the existing validate_draft()
    unchanged; ArticlePackage carries the extra fields (excerpt, meta_title,
    cta) validate_draft doesn't need. reacs_report is None only if QA never
    passed in any attempt (REACS is never computed against a draft QA has
    already rejected).

    REACS used to be scored once, AFTER this loop returned — meaning a draft
    that passed QA on attempt 1 got exactly one shot at REACS, with no
    correction mechanism, even on an obvious miss (e.g. missing the mandatory
    comparison table). Scoring REACS INSIDE the loop and retrying on its
    failure too — same budget, same targeted-feedback pattern QA already
    uses — closes that gap: a table/heading/structure miss now gets fixed
    the same way a QA miss always has.

    `serp` (an optional retrieval.serp.SerpSnapshot) is computed once by the
    caller and reused across every retry — each snapshot costs a live Serper
    query plus competitor-page fetches, and the SERP doesn't change between
    attempts. `has_byline` reflects the article's already-assigned instructor
    byline (assigned once, before this is ever called — see webapp/
    pipeline.py's _assign_byline) so REACS's instructor_byline check is
    accurate on every attempt without recomputing it."""
    from .llm import complete_with_configured_provider
    from .validate import validate_draft

    def emit(step, status, message, **extra):
        if on_progress:
            on_progress(step, status, message, **extra)

    best = None
    # The draft a retry revises — see build_article_prompt. Seeded by the
    # caller on a REWRITE so attempt 1 repairs the article that already
    # exists instead of re-rolling every structural requirement from a blank
    # page (webapp/pipeline.py's _reacs_seed_feedback explains when).
    feedback = seed_feedback
    previous_content = seed_content if seed_feedback else None
    attempts = max(1, max_attempts)
    for attempt in range(1, attempts + 1):
        suffix = f" (attempt {attempt}/{attempts})" if attempts > 1 else ""
        if feedback:
            emit("write", "active", f"Writing article{suffix} (fixing prior issues)…",
                attempt=attempt, max_attempts=attempts)
        else:
            emit("write", "active", f"Writing article{suffix}…", attempt=attempt, max_attempts=attempts)
        try:
            system_prompt, user_prompt, tags = build_article_prompt(
                brief, bundle, vendor_name, feedback, entities=entities,
                previous_content=previous_content, link_menu=link_menu,
                certifications=certifications, courses=courses)
            raw, model_used = complete_with_configured_provider(system_prompt, user_prompt,
                                                                purpose="article")
        except Exception as exc:
            if best is not None:
                emit("write", "warn", "Provider error on retry — using best attempt so far")
                break
            emit("write", "error", f"Article generation failed: {exc}")
            raise

        # entities too, not just acronyms: fix_writing_mechanics needs the KB
        # names to case a brand like "FortiManager" and to know which US
        # spellings are part of a vendor's own product name. The targeted-fix
        # path passed them; this, the path every article is first written by,
        # did not — so the first draft was the one written blind.
        ok, result = parse_article_response(raw, acronyms, entities)
        if not ok:
            emit("write", "warn", f"Write attempt {attempt} failed: {result}")
            if best is not None:
                break   # an earlier attempt already produced a usable article — keep it
            if attempt < attempts:
                # Tell the retry what was wrong with the RESPONSE. There is no
                # draft to revise here (nothing parsed), but without even this
                # the next attempt re-sent an identical prompt and re-rolled
                # the same failure until the budget ran out.
                feedback = (f"PREVIOUS ATTEMPT'S RESPONSE WAS UNUSABLE: {result} "
                            "Return one complete, valid JSON object with every "
                            "key from the schema, and nothing else.")
                previous_content = None
                continue
            raise RuntimeError(f"Writing failed after {attempts} attempt(s): {result}")
        article = result
        emit("write", "done", "Article written", attempt=attempt)

        draft = Draft(
            topic=bundle.topic, region=bundle.region, title=brief.title,
            slug=slugify(brief.title), meta_description=article.meta_description,
            body_markdown=article.content, faq=article.faq,
            references=references_from_tags(tags), raw=raw,
        )

        emit("qa", "active", "Running reliability QA…", attempt=attempt)
        report = validate_draft(draft, bundle, keywords=[brief.keyword], serp=serp,
                                entities=entities,
                                internal_links=brief.internal_links or None,
                                allowed_link_urls=allowed_link_urls,
                                acronyms=acronyms,
                                seo_title=result.meta_title, kb=kb,
                                outline=outline)
        if not report.passed:
            will_retry = attempt < attempts
            emit("qa", "warn",
                 f"QA blocked ({len(report.failed)} check(s) failed)"
                 + (" — regenerating with targeted fixes…" if will_retry else " — using best attempt"),
                 attempt=attempt)
            feedback = report.feedback_block()
            previous_content = article.content
            candidate = (article, draft, report, model_used, None)
            if best is None or _rank(candidate) > _rank(best):
                best = candidate
            continue
        emit("qa", "done", "All QA checks passed", attempt=attempt)

        emit("reacs", "active", "Scoring against the REACS content standard…", attempt=attempt)
        from config import load_course_links
        reacs_report = compute_reacs(
            content=article.content, title=brief.title, keyword=brief.keyword,
            faq=article.faq, has_byline=has_byline, entities=entities or [],
            meta_title=article.meta_title or "",
            cta_phrases=load_course_links()["cta_phrases"])
        candidate = (article, draft, report, model_used, reacs_report)
        if reacs_report.passed:
            emit("reacs", "done", f"REACS passed (score {reacs_report.score}, pass ≥{REACS_THRESHOLD})", attempt=attempt)
            return candidate
        if best is None or _rank(candidate) > _rank(best):
            best = candidate

        will_retry = attempt < attempts
        emit("reacs", "warn",
             f"QA passed but REACS blocked (score {reacs_report.score}, pass ≥{REACS_THRESHOLD}; "
             f"{reacs_report.mandatory_failed} mandatory check(s) failed)"
             + (" — regenerating with targeted fixes…" if will_retry else " — using best attempt"),
             attempt=attempt)
        feedback = reacs_feedback_block(reacs_report)
        previous_content = article.content

    return best
