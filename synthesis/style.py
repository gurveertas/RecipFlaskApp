"""The Red Education house style — RED EDUCATION HOUSE STYLE, ported verbatim
from the real, working source (src/lib/writer.ts's STYLE_PROFILE, derived from
three approved Red Education exemplar articles: a salary guide, a
manager-questions listicle, and a threat listicle). Applies to BOTH the ad-hoc
generator (synthesis/generate.py) and the Brief+Writer pipeline
(synthesis/brief.py) — one consistent voice everywhere.

This is the target the generator writes to. It is intentionally explicit so the
output is consistent and reviewable rather than generic AI prose.
"""

VOICE = """\
Write directly to a named professional persona (an engineer weighing
certification, an IT manager or CIO planning a deployment, a CISO assessing
risk). Use second person where natural.
Plain, confident, expert tone. Short paragraphs (2-4 sentences). No hype, no
filler, no "in today's fast-paced world" style openers.
Educational before promotional: roughly the first 85% of the article must
teach; Red Education promotion is confined to a clearly-signposted final
section and the CTA.
Build trust with honest counsel: where the facts support it, say when a
reader does NOT need a course, product tier, or certification — the approved
exemplars do this deliberately, and it is a deliberate brand signature, not an
oversight.
Australian/British spelling (organisation, behaviour, centre, optimise)."""

STRUCTURE = """\
- Length: 1,500-2,000 words. Treat 1,500 as a firm floor, not an aspiration —
  articles under 1,500 words get sent back for being too short, so budget for
  it deliberately rather than writing until the outline feels "covered": plan
  on 8-10 H2 sections at roughly 180-220 words each, plus a 100-150 word
  opening and a 100-150 word closing/ROI section, on top of the Red Education
  section and FAQ. Before you finish, sanity-check the total: if it reads
  short of 1,500, the fix is to add a concrete example, a counterpoint, a
  second angle, or more named data/detail from the sources to the thinnest
  sections — never filler, repetition, or restating the same point in
  different words.
- Open by naming the reader's real tension or decision in the first two
  sentences.
- DIRECT ANSWERS FIRST (written for search and AI-answer extraction): the
  opening paragraph must work as a standalone 40-60 word direct answer to the
  article's primary question — a clean, quotable, factual summary an AI
  overview could lift verbatim — BEFORE any scene-setting. Likewise, any H2
  phrased as a question must open with a 1-2 sentence direct answer, then
  explain. Never open a section with throat-clearing ("Many organisations
  ask...", "In this section we will...").
- H2 sections with descriptive headings; H3 for sub-points. Whenever a
  section has 3+ genuinely distinct sub-topics — exam tiers, deployment
  scenarios, role-specific advice — break them into H3 sub-headings rather
  than a bullet list or a wall of prose; it reads more clearly and it is
  expected, not optional, when the content has that shape. Number items in
  listicle formats.
- Use concrete specifics everywhere: figures, timeframes, course codes/names,
  certification names, city or sector detail. Vague claims are off-brand.
- Data-guide-style topics open with a "Key facts" summary of 4-6 bolded
  takeaways with named sources, right after the intro.
- Listicle-style topics may use a "Short answer / The detail" pattern per item.
- Where the content is naturally tabular — comparing two courses/certs
  side by side, listing certification tiers, exam codes, durations, or
  region-by-region figures — use a markdown table instead of prose or a
  bullet list. Every figure in a table follows the same sourcing rule as
  prose (see SOURCING RULES below): no invented numbers.
- End with: a summary section framing the ROI of the knowledge, then a short
  Red Education section (authorised training, relevant course names and
  durations), then a 4-5 question FAQ, then one clear call-to-action. Format
  the FAQ as one ### H3 per question with the answer as a plain 2-4 sentence
  paragraph underneath — not a bullet list, not the question bolded inline —
  so each question is independently scannable and matches how the site's own
  FAQ sections read.
- Weave 1-3 internal links to related Red Education pages and cite external
  sources by name.
- Link vendor/certification names inline to the relevant Red Education course
  page the first time they appear (use the provided course-link map)."""

# The reliability contract the model MUST follow. These map 1:1 to QA checks,
# and a draft that breaks them is AUTOMATICALLY REJECTED — so the rules are
# written to pre-empt the exact ways a draft gets blocked.
TECHNICAL_SCOPE_RULES = """\
TECHNICAL CLAIM SCOPE (all vendors, including tables and FAQ answers)
- Licensing: distinguish the licensed management instance, its device/user/core
  entitlement and separate product subscriptions. Never convert a capacity
  licence into a separate licence for each monitored appliance without evidence.
- Capacity: qualify numerical limits by supported model, operating mode,
  resources and licence. HA provides redundancy; it does not automatically
  increase capacity. Keep vendor-supported HA claims with their conditions.
- Migration: distinguish importing from committing/pushing configuration.
  Identify which settings can be overwritten and the relevant method/options;
  do not imply all migrations overwrite everything or that import alone does so.
- Management integrations: name the deployment mode and required plugin/console.
  A supported integration is not available in every management mode. Mention
  documented alternative management paths when answering whether it is required.
- Cite licensing rules, capacity limits, overwrite behaviour and release-specific
  capabilities in the same sentence (or table row), using the exact primary
  documentation page that supports the claim. A valid tag, a documentation
  homepage, or an unrelated page mentioning the same number is insufficient.
  If the available material cannot support the claim, omit it or state the
  evidence limitation. Brief key points and FAQ hints are not evidence.
- Describe adoption using the reader's deployment needs. Avoid 'essential
  regardless of company size' or 'the standard for enterprise resilience'.
- Apply corrections consistently to the body, comparison tables and stored FAQ.
"""

GROUNDING_RULES = """\
SOURCING RULES (mandatory — an automated checker REJECTS the draft if any is broken):

NUMBERS & FACTS (hallucination guard)
1. Never write a statistic — a percentage (e.g. "12%"), a salary or currency
   amount ($/£/€/₹/AUD), a count, a specific date, or a CVE id — UNLESS that exact
   figure appears verbatim in the VERIFIED CLAIMS, UNVERIFIED CLAIMS, or SOURCE
   MATERIAL below. Do not estimate, round, infer, extrapolate, or rely on
   "common knowledge" for any number. If you don't have a figure, write
   qualitatively ("a substantial premium", "several years") instead of inventing
   one. Every number in your draft must be traceable to the material provided.
2. Cite every factual/statistical claim inline with its tag, e.g. [S1]. Use only
   tags that exist in the provided sources. A tag is a POINTER, never a word in
   the sentence: it is stripped out before the article is published, so the
   sentence must still read correctly with every tag deleted. Never write
   "According to [S1]," or "[S1] states that", and never make a tag the visible
   text of a link — "[S1](https://example.com)" publishes as a link labelled
   S1, which means nothing to a reader.
3. VERIFIED claims may be stated directly (with a citation).
4. UNVERIFIED (single-sourced) claims must be attributed in-text by NAMING the
   source, then citing it: "according to Pearson VUE [S2]". The name is the
   part that does the work — "according to [S2]" tells the reader nothing and
   becomes "according to ," once the tag is stripped.
5. FLAGGED claims must NOT appear in the article at all.

ORIGINALITY (plagiarism guard)
6. Write entirely in your own words. NEVER reproduce more than 8 consecutive
   words from any source. This is checked mechanically, so it applies especially
   to things that are tempting to copy: lists, definitions, exam-topic
   enumerations, job-title or degree-name lists, AND technical formulas,
   redundancy/HA rules, and precise specifications (e.g. an "N+1" or "2N+1"
   sizing rule, a config syntax example, a hardware spec table). These are
   the easiest ones to accidentally lift verbatim because rephrasing them
   feels risky — but describe the same rule in different words instead (e.g.
   "you need a bit more than double the minimum machine count for full
   redundancy" rather than repeating the source's exact phrasing of the
   formula). Do not lift them verbatim — rewrite in a different order and
   phrasing, or fold them into prose.
7. Do not fabricate quotes.""" + "\n\n" + TECHNICAL_SCOPE_RULES

# Output contract — the generator must return this JSON shape (nothing else).
OUTPUT_SCHEMA = """\
Return ONLY a JSON object (no markdown fences, no preamble) with keys:
{
  "title": string,
  "slug": string (lowercase, hyphenated, no stop-word padding),
  "meta_description": string (<= 155 characters),
  "body_markdown": string (the article body ONLY — do not repeat the title as
    a heading, start straight in with the opening paragraph; every main
    section is a ## H2, H3 is only for sub-points inside a section; [S#]
    citations),
  "faq": [ {"q": string, "a": string}, ... ],
  "references_used": [ "S1", "S2", ... ]
}"""
