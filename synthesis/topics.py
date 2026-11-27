"""Topic Engine: pure, testable scoring logic + AI-idea-generation prompt.

Ported faithfully from the original TypeScript (src/lib/topics.ts) — the
business logic (weights, thresholds, prompt wording) is Red Education's, not
invented here. THE CORE PRINCIPLE, preserved from the original: the model
proposes ideas and estimates raw dimensions; the weighting formula below
computes the opportunity score and the create/park/reject decision. The IP
lives here, in code, not in a prompt — the model never decides, it estimates.
"""
from __future__ import annotations

import re

from dataclasses import dataclass

# ---------------- Red Education's weighting (the IP) ----------------
# Mirrors the business objectives in priority order:
# course sales > MQLs > AI visibility > organic traffic.
# Adjust these numbers to change how the whole platform prioritises content.
WEIGHTS = {
    "commercial": 0.4,
    "seo": 0.25,
    "ai_visibility": 0.2,
    "vendor_alignment": 0.15,
    "difficulty_penalty_max": 15,  # subtracted proportionally to difficulty
}

THRESHOLDS = {
    "create": 65,  # at or above: goes to the content pipeline
    "park": 45,    # between park and create: parked for later review
    # below park: retired (rejected weak idea)
}

_PRIORITY_BY_VENDOR_TIER = {1: 100, 2: 80, 3: 60, 4: 40, 5: 20}

INTENTS = ["informational", "commercial", "transactional", "navigational"]
CONTENT_TYPES = [
    "technology_education", "vendor_news", "salary_guide",
    "certification_guide", "career_article", "comparison",
]


def vendor_alignment_from_priority(priority: int | None) -> int:
    """Vendor priority P1..P5 -> alignment score. Unknown/unset -> 60 (a
    sensible middle value), matching the original's `?? 60` fallback."""
    return _PRIORITY_BY_VENDOR_TIER.get(priority, 60)


def clamp(n) -> int:
    try:
        x = float(n)
    except (TypeError, ValueError):
        return 0
    if x != x:  # NaN
        return 0
    return max(0, min(100, round(x)))


@dataclass
class TopicIdea:
    title: str
    keyword: str
    search_intent: str
    content_type: str
    commercial_score: int   # model estimate 0-100: how directly this sells courses
    seo_score: int           # model estimate 0-100: realistic ranking opportunity
    ai_score: int            # model estimate 0-100: likelihood of AI-answer citation
    difficulty: int          # model estimate 0-100: competition/effort to rank
    rationale: str = ""


@dataclass
class ScoredTopic(TopicIdea):
    vendor_alignment: int = 0
    opportunity_score: int = 0
    priority: str = "retire"   # "create" | "parked" | "retire"


def score_topic(idea: TopicIdea, vendor_priority: int | None) -> ScoredTopic:
    """The scoring formula. Deterministic and transparent."""
    vendor_alignment = vendor_alignment_from_priority(vendor_priority)
    base = (
        WEIGHTS["commercial"] * idea.commercial_score
        + WEIGHTS["seo"] * idea.seo_score
        + WEIGHTS["ai_visibility"] * idea.ai_score
        + WEIGHTS["vendor_alignment"] * vendor_alignment
    )
    penalty = (idea.difficulty / 100) * WEIGHTS["difficulty_penalty_max"]
    opportunity_score = clamp(base - penalty)

    if opportunity_score >= THRESHOLDS["create"]:
        priority = "create"
    elif opportunity_score >= THRESHOLDS["park"]:
        priority = "parked"
    else:
        priority = "retire"

    return ScoredTopic(
        title=idea.title, keyword=idea.keyword, search_intent=idea.search_intent,
        content_type=idea.content_type, commercial_score=idea.commercial_score,
        seo_score=idea.seo_score, ai_score=idea.ai_score, difficulty=idea.difficulty,
        rationale=idea.rationale, vendor_alignment=vendor_alignment,
        opportunity_score=opportunity_score, priority=priority,
    )


# ---------------- Prompting ----------------
TOPIC_SYSTEM_PROMPT = """\
You are the Topic Strategy Agent for Red Education, an authorised IT training company.
You propose blog topic ideas that ultimately sell instructor-led vendor certification courses.
Educational value comes before promotion. You estimate raw dimensions only; you never decide
what gets created — a downstream scoring system does that.
Respond with JSON only, no prose, no markdown fences, exactly this shape:
{
  "ideas": [
    {
      "title": "compelling article title",
      "keyword": "primary target keyword phrase",
      "search_intent": "informational|commercial|transactional|navigational",
      "content_type": "technology_education|vendor_news|salary_guide|certification_guide|career_article|comparison",
      "commercial_score": 0-100,
      "seo_score": 0-100,
      "ai_score": 0-100,
      "difficulty": 0-100,
      "rationale": "one or two sentences on why this topic and these scores"
    }
  ]
}
Scoring guidance:
- commercial_score: how directly a reader is likely to enquire about or buy a course
- seo_score: realistic chance a specialist training company ranks for this
- ai_score: likelihood ChatGPT/Gemini/Perplexity/AI Overviews would cite such an article
- difficulty: competition and effort to rank (100 = dominated by vendors/giants)
Title guidance: for certification_guide topics, prefer definitive
reference-documentation titles ("The Complete <certification> Guide:
Objectives, Study Plan and Exam Domains") over listicle/tip framings ("5 Tips
for Passing...") — the definitive form is what search engines and AI answers
retrieve as THE resource for a certification.
Favour a spread of content types. Be honest with scores; inflated scores are useless."""


def build_topic_prompt(vendor_name: str, count: int, certifications: list[str],
                       courses: list[str], existing_keywords: list[str],
                       theme: str | None = None,
                       research_summaries: list[str] | None = None,
                       existing_titles: list[str] | None = None) -> str:
    from .text import today_block

    research_summaries = research_summaries or []
    lines = [
        today_block(),
        f"Vendor: {vendor_name}",
        f"Generate exactly {count} topic ideas.",
    ]
    if theme:
        # A single "Focus theme: x" line was too weak to steer the batch: a
        # run themed "acquisitions" produced an idea whose keyword never
        # mentioned acquisitions, so retrieval never searched for them and
        # the finished article contained none. The theme has to bind the
        # KEYWORD, because the keyword is what drives research downstream.
        lines += [
            "",
            f"REQUIRED THEME: {theme}",
            f"Every idea must be genuinely ABOUT \"{theme}\" as it relates to "
            f"{vendor_name} — not a general {vendor_name} topic with the theme "
            "mentioned in passing. If you cannot produce the requested number "
            "of real ideas on this theme, return fewer rather than padding "
            "with off-theme ones.",
            f"Each idea's KEYWORD must contain the theme, or the specific thing "
            f"the theme refers to (a named acquisition, product or event). The "
            "keyword becomes the search query used to research the article, so "
            "a keyword that omits the theme guarantees the article misses it.",
            "",
        ]
    lines += [
        "",
        "Red Education sells these certifications/courses for this vendor "
        "(topics should create demand for them):",
        *[f"- Certification: {c}" for c in certifications[:25]],
        *[f"- Course: {c}" for c in courses[:25]],
        "",
    ]
    if not certifications:
        # Do not let an empty list be read as "invent some". Several vendors
        # hold only courses in the knowledge base, because the rows that
        # looked like certifications were course pages (see
        # Certification.is_credential). A model shown courses and asked for
        # certification topics will name a credential from memory, which is
        # precisely how "Prisma Access SSE certification" — a credential that
        # does not exist — reached a published draft.
        lines += [
            f"NOTE: we hold NO certifications on record for {vendor_name}. "
            "Do NOT propose a certification_guide, and do not name a "
            "certification you have not been shown above. Propose topics "
            "about the courses and the technology instead.",
            "",
        ]
    if research_summaries:
        lines += [
            "Recent verified research (use for timely vendor_news ideas):",
            *[f"- {s[:400]}" for s in research_summaries[:3]],
            "",
        ]
    # Titles as well as keywords. Listing only keywords let the model return
    # the same article under a new keyword and clear the check — the reason
    # successive batches on an unchanged knowledge base looked identical.
    if existing_keywords or existing_titles:
        lines += [
            f"{vendor_name} ALREADY has the topics below. Do not repeat any of "
            "them, and do not re-package one under a different keyword — a "
            "reworded version of an existing idea is a duplicate:",
            *[f"- {k}" for k in (existing_keywords or [])[:60]],
            *[f"- {t}" for t in (existing_titles or [])[:60]],
            "",
        ]
    # Asked for explicitly, because "generate N ideas" against a fixed
    # knowledge base converges on the same obvious few every time. Naming the
    # angles is what makes a later batch differ from an earlier one rather
    # than restating it.
    lines += [
        "Cover DIFFERENT angles from each other and from the list above. "
        "Useful angles include: exam objectives and study planning; cost and "
        "renewal; career outcomes and salary; migration or upgrade paths; "
        "troubleshooting and common failures; comparisons between two "
        "certifications or products; what changed in a recent release; "
        "who the certification is genuinely NOT for.",
        "If the obvious topics for this vendor are already taken, go narrower "
        "and more specific rather than restating a broad one — a single exam "
        "domain, one migration scenario, one real operational problem.",
        "",
    ]
    return "\n".join(l for l in lines if l != "")


# ---------------- Parsing ----------------
def parse_topic_response(raw_text: str) -> tuple[bool, list[TopicIdea] | str]:
    """Parse the model's {"ideas": [...]} JSON. Returns (True, ideas) or
    (False, error_message). Drops malformed entries, in-batch-dedupes
    keywords, clamps scores, falls back to safe enum defaults — mirrors
    parseTopicResponse in the original 1:1."""
    from .llm import extract_json

    try:
        parsed = extract_json(raw_text)
    except Exception:
        return False, "The model did not return valid JSON."

    ideas_raw = parsed.get("ideas") if isinstance(parsed, dict) else None
    if not isinstance(ideas_raw, list):
        return False, "The model returned no ideas array."

    seen: set[str] = set()
    ideas: list[TopicIdea] = []
    for raw in ideas_raw[:50]:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title") or "").strip()
        keyword = str(raw.get("keyword") or "").strip().lower()
        if len(title) < 8 or len(keyword) < 3:
            continue
        if keyword in seen:  # in-batch dedupe
            continue
        seen.add(keyword)
        search_intent = raw.get("search_intent") if raw.get("search_intent") in INTENTS else "informational"
        content_type = raw.get("content_type") if raw.get("content_type") in CONTENT_TYPES else "technology_education"
        ideas.append(TopicIdea(
            title=title, keyword=keyword, search_intent=search_intent,
            content_type=content_type,
            commercial_score=clamp(raw.get("commercial_score")),
            seo_score=clamp(raw.get("seo_score")),
            ai_score=clamp(raw.get("ai_score")),
            difficulty=clamp(raw.get("difficulty")),
            rationale=str(raw.get("rationale") or "").strip()[:500],
        ))

    if not ideas:
        return False, "No usable ideas in the model response."
    return True, ideas


# An SEO title is capped at ~60 characters, and Rank Math scores a page on
# whether the focus keyword appears in it — so a keyword that cannot fit
# alongside anything readable makes that check unwinnable no matter how good
# the article is. Thresholds calibrated on real generated topics:
#   worked   "pcnse certification guide 2026"        30 chars / 4 words
#            "nmcaa certification guide"             25 chars / 3 words
#   failed   "f5 certified administrator certification guide"    46 / 6
#            "77202v integrating and configuring ip office platform"  53 / 7
# The 53-char one scored 26/100 in Rank Math and could never have scored well:
# the keyword alone would have consumed 88% of the title.
KEYWORD_MAX_CHARS = 40
KEYWORD_MAX_WORDS = 5


def keyword_length_warning(keyword: str) -> str | None:
    """A human-readable warning if this keyword is too long to rank well, or
    None if it's fine.

    Deliberately a WARNING, not a rejection. A long keyword is a judgement
    call about search demand, not a correctness error, and the person
    approving the topic is better placed to make it than a character count —
    the guard exists so the cost is visible BEFORE the keyword propagates
    into the brief, the article title, the slug and the SEO title, where it
    is expensive to undo."""
    kw = (keyword or "").strip()
    if not kw:
        return None
    chars, words = len(kw), len(kw.split())
    if chars <= KEYWORD_MAX_CHARS and words <= KEYWORD_MAX_WORDS:
        return None
    pct = round(chars / 60 * 100)
    reason = (f"{chars} characters" if chars > KEYWORD_MAX_CHARS
              else f"{words} words")
    return (f"Long focus keyword ({reason}) — it would use {pct}% of a "
            "60-character SEO title, so it can't sit there alongside "
            "anything readable and will score badly. Prefer 2-4 words that "
            "someone would actually search.")


# --------------------------------------------------------------------------- #
# Near-duplicate detection
# --------------------------------------------------------------------------- #
# Words that carry no distinguishing signal in this corpus. Two ideas sharing
# only these are not related; two sharing everything else are the same idea
# with different packaging.
_TOPIC_STOPWORDS = frozenset({
    # Grammatical words only. Domain words — "guide", "certification",
    # "exam", the year — were stripped in a first pass and that was wrong:
    # they carry real identity here, and removing them reduced a keyword like
    # "ccsa certification guide 2026" to just {ccsa, certification}, so
    # "ccsa certification cost breakdown" scored as fully contained in it and
    # a genuinely different angle was rejected as a duplicate.
    "the", "a", "an", "and", "or", "for", "to", "in", "on", "of", "with",
    "your", "you", "is", "are", "was", "were", "be", "by", "at", "from",
    "how", "what", "why", "when", "which", "does", "do", "can", "should",
    "about", "into", "that", "this", "it", "its",
})


def _idea_tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if w not in _TOPIC_STOPWORDS and len(w) > 2}


def near_duplicate_of(candidate: str, existing: list[str],
                      threshold: float = 0.7) -> str | None:
    """The existing entry `candidate` substantially repeats, or None.

    Exact-keyword matching was the only duplicate check, and it is far too
    weak: "ccsa certification guide" and "ccsa exam guide 2026" are different
    strings and the same article. Successive runs on an unchanged knowledge
    base converge on the same handful of obvious topics, so without this the
    tenth batch looks like the first.

    Overlap is measured against the SHORTER side (not the union), because a
    short keyword is routinely compared with a long title — "ccsa exam costs"
    is fully contained in "The Complete CCSA Guide: Exam Costs and Domains"
    and should count as covered, which a Jaccard union score would miss.
    """
    ct = _idea_tokens(candidate)
    if not ct:
        return None
    for item in existing:
        et = _idea_tokens(item)
        if not et:
            continue
        if len(ct & et) / min(len(ct), len(et)) >= threshold:
            return item
    return None
