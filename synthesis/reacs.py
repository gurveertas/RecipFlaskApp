"""REACS v1.3 — the Red Education AI Content Standard, article pillars.

Upgraded from v1.0 against the v1.3 assessor prompt. The two gates split like
this, and the split is deliberate:

  THIS MODULE   the article PILLARS — structure, framing, freshness. Twelve
                checks, six mandatory and six advisory, scored 0-100 with
                mandatory weighted 60 and advisory 40. Any mandatory failure
                blocks regardless of score, and the score must also reach
                REACS_THRESHOLD.
  validate.py   the QA RISK rules V1-V10 — whether the article is TRUE and
                properly sourced. All built. Reported separately and never
                folded into the score, because "structure and truth are
                orthogonal": the worst factual defect in the standard's own
                register scored 79 on structure and would have scored 96 after
                remediation while still carrying the wrong version number.

WHAT CHANGED FROM v1.0, and why each mattered:
  M1  the keyword must appear in the meta title too, not just the H1
  M2  an opening that DESCRIBES the article now fails; it must answer
  M4  FAQ ANSWERS are counted, not questions — an accordion renders an empty
      body quite happily, and counting questions scored that as a pass
  M5  the site root is not a countable internal link
  M6  a full date is required; "August 2026" fails
  A3  voice_consistency REPLACED experience_signals, which awarded a point for
      the exact boilerplate the standard bans. The pillar was scoring the
      defect, and retry feedback fed the phrase back to the writer
  A6  exactly one CTA in the body; stacked closes and zero both fail
  --  threshold raised 75 -> 90, and pick_instructor() no longer falls back to
      a random instructor when none is certified for the vendor

THE GATE SCORES SIX MANDATORY PILLARS, and that is settled rather than
pending. v1.3 lists more; the difference is decided and is not a backlog item.
M7 (instructor byline) was removed from the gate by decision — articles
publish under the shared Admin WordPress account for now, and pick_instructor
below still refuses a non-vendor-matched byline, so a correct byline appears
where one exists and none appears where it does not. Nothing blocks on it.

Mandatory weight is still 60 points across six pillars rather than seven, so
each is worth proportionally more and scores are NOT comparable with v1.0 or
with a full v1.3 run. Nothing records which standard produced a stored number:
there is still no standard_version column.

Ported originally from the real, working source (src/lib/reacs.ts).

Merges the 21-pillar article framework and the AI-visibility audit into one
enforced standard. Article-level pillars are deterministic checks here;
site-architecture items (server rendering, canonical consolidation,
llms.txt) are web-development work on rededucation.com and are deliberately
NOT in this module.

A SEPARATE gate from synthesis/validate.py's QA harness — never merged with
it. QA and REACS answer different questions (QA: "is this draft internally
sound, grounded, non-plagiarized, on-brief?"; REACS: "does this draft meet
the AI-search-visibility content standard?") and blog_generator already had
3 QA checks (direct_answer, entity_coverage, internal_links, added during an
earlier "AI-search content params" pass) that independently approximate a
few REACS pillars under different names/thresholds before REACS existed as a
named standard here — they are NOT the same checks and are NOT replaced by
this module. Keep both: Article gets a `qa_checks`/`qa_passed` pair AND a
separate `reacs_report`/`reacs_score`/`reacs_passed` pair (see config/db.py's
Article model), shown in two separate UI panels.

Separate gates, but ONE retry budget: synthesis/brief.py's
generate_passing_article() scores REACS immediately after a draft clears QA
(inside the same up-to-3-attempts loop, not after it returns), and retries
with reacs_feedback_block()'s targeted fixes on a REACS miss exactly like it
already retried on a QA miss. Before this, REACS was computed only once,
after the loop had already returned on the first QA pass — a table/heading/
structure miss had no correction mechanism at all, however many attempts
were nominally available.

Two-tier gate, per the reference implementation's policy:
  MANDATORY pillars  -> any failure locks marketing review (hard gate)
  ADVISORY pillars   -> scored; overall score must also clear the threshold
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

REACS_THRESHOLD = 90


@dataclass
class ReacsCheck:
    pillar: str
    tier: str          # "mandatory" | "advisory"
    passed: bool
    message: str


@dataclass
class ReacsReport:
    score: int                    # 0-100, computed here — never by a model
    passed: bool                  # all mandatory passed AND score >= threshold
    mandatory_failed: int
    checks: list[ReacsCheck] = field(default_factory=list)
    checked_at: str = ""


def _first_paragraph(content: str) -> str:
    """Walk lines, skipping headings/blank lines before the first real
    paragraph; stop at the next heading or blank line. Matches
    reacs.ts's firstParagraph() exactly — this is what an AI engine's
    snippet extraction actually sees first."""
    lines = content.replace("\r\n", "\n").split("\n")
    para: list[str] = []
    for raw in lines:
        line = raw.strip()
        if line.startswith("#"):
            if para:
                break
            continue
        if line == "":
            if para:
                break
            continue
        para.append(line)
    return " ".join(para)


def _word_count(s: str) -> int:
    return len([w for w in s.split() if w])


_ACRONYM_RE = re.compile(r"\(([A-Za-z][A-Za-z0-9/-]{1,15})\)")
_CODE_RE = re.compile(r"\b([A-Za-z]{2,6}[-–]\d{2,4})\b")


def _entity_alias(entity: str) -> str | None:
    """The short, distinctive name an article would actually use for this
    entity, or None if the full name is already it.

    The Vendor KB stores formal catalogue names, but prose uses the short
    form, so matching the full string alone made entity_coverage unpassable
    by construction — twice over, with two different catalogue shapes:
      - Check Point: "Check Point Certified Security Expert (CCSE)" -> prose
        writes "CCSE". An article citing CCSA 10x, CCSE 4x, CCTE and CCME
        scored 0 of 20.
      - Palo Alto: "EDU-210 Firewall Essentials Configuration and Management"
        -> prose writes "EDU-210"; "Panorama: Centralized Network Security
        Administration" -> prose writes "Panorama". An article covering
        EDU-210, EDU-330, Panorama and NGFW scored 1 of 25.
    Priority is most-distinctive first: a parenthesised acronym, then a
    course code, then the segment before a colon."""
    e = entity.strip()
    m = _ACRONYM_RE.search(e)
    if m:
        return m.group(1)
    m = _CODE_RE.search(e)
    if m:
        return m.group(1)
    head = e.split(":")[0].strip()
    if 3 <= len(head) < len(e):
        return head
    return None


def _entity_key(entity: str) -> str:
    """Identity used to count DISTINCT entities. The KB holds both the bare
    and vendor-prefixed forms of the same item ("Panorama: ..." and "Palo
    Alto Networks Panorama: ..."), so counting raw strings double-counts one
    real entity and would let a thin article clear the threshold on half the
    coverage. Suffix-collapsing the keys afterwards (see compute_reacs) folds
    the prefixed variant into the bare one."""
    return (_entity_alias(entity) or entity.strip()).lower()


def _entity_mentioned(entity: str, haystack_lower: str) -> bool:
    """Covered if the full catalogue name appears, or its short alias does.
    Aliases match on word boundaries so a short code can't be picked up
    inside an unrelated word."""
    e = entity.strip()
    if len(e) >= 3 and e.lower() in haystack_lower:
        return True
    alias = _entity_alias(e)
    if alias and len(alias) >= 3:
        return bool(re.search(rf"\b{re.escape(alias.lower())}\b", haystack_lower))
    return False


# A3. v1.3's first-person markers, checked against the whole body. Every
# byline this pipeline renders is a "Technically reviewed by" line
# (publish/payload.py and all three templates hardcode the verb), so there is
# no "I" for any of these to refer to and first person is unconditionally
# wrong until a written_by mode exists.
FIRST_PERSON_MARKERS = (
    "i have observed", "i have seen", "in my experience",
    "i have found", "i have delivered", "when i teach",
    "we recommend", "we often recommend", "we typically recommend",
    "we see", "we often see", "we regularly see", "we have seen",
    "we observe", "we often observe", "our experience",
)

# The fragment v1.3 names by name as a template artefact. Matched on its
# distinctive middle rather than verbatim, because the defect is the model
# reaching for a fabricated teaching volume, not this exact wording.
_BOILERPLATE_EXPERIENCE_RE = re.compile(
    r"after (?:delivering|teaching|training).{0,40}"
    r"(?:thousands|hundreds|dozens) of "
    r"(?:engineers|students|learners|professionals)", re.I)

# M2. Openings that DESCRIBE the article instead of answering it. v1.3 fails
# these explicitly: "This is a reference-style breakdown of..." is not an
# answer. Anchored to the start of the opening paragraph — the same words
# mid-paragraph are ordinary prose.
_META_COMMENTARY_RE = re.compile(
    r"^\s*(?:this (?:article|guide|post|page|blog|piece)\b"
    r"|this is (?:a|an|the)\b"
    r"|in this (?:article|guide|post|page)\b"
    r"|below,? (?:we|you)\b"
    r"|here(?:'s| is) (?:a|an|the)\b"
    r"|we(?:'ll| will) (?:cover|explore|look|walk|break)\b)", re.I)

# M6. A FULL date, in any of the three shapes the site actually uses. Month
# and year alone fails: v1.3 calls it out by name, and it is not a
# machine-readable freshness signal.
_FULL_DATE_RE = re.compile(
    r"last updated[:\s]*"
    r"(?:\d{1,2}\s+[A-Za-z]{3,9},?\s+\d{4}"
    r"|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}"
    r"|\d{4}-\d{2}-\d{2})", re.I)

# M5. The bare site root, in every spelling. Not a countable internal link:
# synthesis/prompt.py already calls a root-targeted anchor "worse than no link
# at all", and QA warns on it — but REACS counted it, so the two gates
# disagreed about the same body.
_SITE_ROOT_RE = re.compile(r"^(?:https?://(?:www\.)?rededucation\.com)?/?$", re.I)

_MD_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)\s]+)\)")
_HTML_LINK_RE = re.compile(r"""<a\s[^>]*href\s*=\s*["']([^"']+)["']""", re.I)
_INTERNAL_RE = re.compile(r"^(/|https?://(www\.)?rededucation\.com)", re.I)


def _internal_links(content: str) -> list[str]:
    """Internal-link targets in the body, from markdown OR raw-HTML anchors.

    De-duplicated by target: the same page linked twice is one internal link,
    not two, so a draft can't clear the threshold by repeating one URL."""
    targets = _MD_LINK_RE.findall(content) + _HTML_LINK_RE.findall(content)
    seen, out = set(), []
    for t in targets:
        bare = t.split("#")[0].split("?")[0]
        # v1.3's M5 counts links to genuinely relevant pages. The site root is
        # not one, and counting it was how REACS passed a body QA was already
        # warning about.
        if _SITE_ROOT_RE.match(bare):
            continue
        key = bare.rstrip("/").lower()
        if _INTERNAL_RE.match(t) and key not in seen:
            seen.add(key)
            out.append(t)
    return out


def compute_reacs(content: str, title: str, keyword: str, faq: list,
                  has_byline: bool, entities: list[str],
                  meta_title: str = "", cta_phrases: list[str] | None = None
                  ) -> ReacsReport:
    """Score a draft against REACS. `faq` is the FAQ list itself, not a count:
    M4 counts populated ANSWERS, which a count cannot express. `meta_title`
    and `cta_phrases` are optional — when a caller omits them the pillars that
    need them fall back to what they can decide rather than failing on a field
    they were never given."""
    checks: list[ReacsCheck] = []
    lower = content.lower()
    kw = (keyword or "").lower().strip()

    # ---------- MANDATORY (hard gate) ----------
    # M1 One primary search intent: the keyword appears in the H1 AND in the
    # meta title. v1.0 checked the H1 alone, but the meta title is a
    # separately-written field and it is what the searcher and the vendor
    # actually see in the SERP, so a keyword-perfect H1 says nothing about it.
    in_h1 = bool(kw) and kw in title.lower()
    in_meta = bool(kw) and kw in (meta_title or "").lower()
    if not kw:
        _m1_ok, _m1_msg = False, "Article has no target keyword."
    elif not meta_title:
        _m1_ok = in_h1
        _m1_msg = (f'Title must target the primary keyword ("{keyword}"). No '
                   "meta title was supplied, so only the H1 was checked.")
    elif in_h1 and in_meta:
        _m1_ok, _m1_msg = True, "Keyword present in both the H1 and the meta title."
    else:
        _missing = " and ".join(([] if in_h1 else ["the H1"])
                                + ([] if in_meta else ["the meta title"]))
        _m1_ok = False
        _m1_msg = (f'The exact phrase "{keyword}" is missing from {_missing}. '
                   "It must appear verbatim in both.")
    checks.append(ReacsCheck("single_search_intent", "mandatory", _m1_ok, _m1_msg))

    # P2 Direct answer first: opening paragraph answers within ~70 words and
    # names the keyword.
    opening = _first_paragraph(content)
    opening_words = _word_count(opening)
    opening_has_kw = bool(kw) and kw in opening.lower()
    opening_is_meta = bool(_META_COMMENTARY_RE.match(opening))
    opening_ok = (0 < opening_words <= 70 and opening_has_kw
                  and not opening_is_meta)
    # Name the ACTUAL defect (and the measured length) rather than restating
    # the rule. This message is fed verbatim to the writer on retry by
    # reacs_feedback_block(), and a generic restatement gave the model no
    # idea whether it was 3 words over or had simply omitted the keyword —
    # so retries kept missing by the same margin.
    if opening_ok:
        _open_msg = f"Opening is a {opening_words}-word direct answer naming the keyword."
    elif opening_words == 0:
        _open_msg = "The article has no opening paragraph before its first heading."
    elif opening_is_meta:
        # v1.3: "Meta-commentary fails... it is a description of the article."
        _open_msg = (
            f'The opening describes the article instead of answering it ("'
            f'{opening[:60].strip()}…"). Lead with the answer itself — say what '
            f'the {keyword} IS, or answer the title\'s question outright, in '
            "the first sentence, then explain.")
    elif opening_words > 70 and not opening_has_kw:
        _open_msg = (f"Opening is {opening_words} words (limit 70) AND does not "
                     f'contain "{keyword}". Cut it to 70 words or fewer and name '
                     "the keyword in the first sentence.")
    elif opening_words > 70:
        _open_msg = (f"Opening is {opening_words} words — cut it to 70 or fewer "
                     f"({opening_words - 70} too many). Keep the first sentence's "
                     "direct answer and drop the rest into the body.")
    else:
        _open_msg = (f'Opening is {opening_words} words but does not contain '
                     f'"{keyword}" — name the target keyword in the first sentence.')
    checks.append(ReacsCheck("direct_answer_first", "mandatory", opening_ok, _open_msg))

    # P3 At least one table — markdown syntax OR a raw <table>. Same lesson as
    # internal_linking below: this writer emits HTML inside otherwise-markdown
    # content, and webapp/render.py passes it straight through to the page, so
    # an HTML table is a REAL table to every reader and every AI crawler.
    # Counting markdown alone would hard-block a mandatory pillar on an
    # article that visibly has the table it is being told to add — feedback no
    # rewrite can satisfy.
    has_table = bool(re.search(r"\|[^\n]+\|\n\s*\|[\s:-]+\|", content)
                     or re.search(r"<table[\s>]", content, re.I))
    # A single unconditional message meant a PASSING pillar still read as a
    # to-do — the reviewer sees "At least one table is required" next to a
    # green tick and reasonably wonders what is missing. Every pillar states
    # what it FOUND when it passes and what to do only when it does not.
    checks.append(ReacsCheck(
        "comparison_table", "mandatory", has_table,
        "A table is present." if has_table else
        "At least one table is required (courses, comparisons, exam domains) "
        "— tables are the easiest structure for AI to extract."))

    # M4 FAQ depth: 8 is the floor, target 10, and every one must have a
    # POPULATED ANSWER. v1.3 is explicit that the count is of answers, not
    # questions — an accordion renders a question with an empty body quite
    # happily, and counting len(faq) scored that as a pass.
    faq_count = len(faq or [])
    answered = sum(1 for f in (faq or [])
                   if isinstance(f, dict) and (f.get("answer") or "").strip())
    if answered >= 8:
        _faq_msg = f"FAQ has {answered} questions with populated answers."
    elif faq_count >= 8:
        _faq_msg = (f"FAQ has {faq_count} questions but only {answered} carry "
                    "an answer. The standard counts ANSWERS, not questions, "
                    "and needs at least 8 (target 10) — write the missing "
                    "answers or drop those questions.")
    else:
        _faq_msg = (f"FAQ has {faq_count} entries ({answered} answered) — the "
                    "standard requires at least 8 answered questions (target 10).")
    checks.append(ReacsCheck("faq_depth", "mandatory", answered >= 8, _faq_msg))

    # P5 Internal links: at least 2 links pointing at rededucation.com or
    # relative paths. BOTH markdown and raw-HTML anchors count — the writer
    # frequently emits <a href="..."> inside otherwise-markdown content, and
    # webapp/render.py passes that through to the page (it even adds
    # rel="noopener"), so those links genuinely work. Counting only markdown
    # meant an article carrying three real internal links — including a
    # correctly chosen /nutanix/ one — scored 0 and hard-blocked on a
    # mandatory pillar, while QA's own internal_links check (plain substring
    # match) passed the identical content. Two gates disagreeing about the
    # same body is unfixable from the editor's seat: every retry was told to
    # add links that were already there.
    internal = _internal_links(content)
    checks.append(ReacsCheck(
        "internal_linking", "mandatory", len(internal) >= 2,
        f"{len(internal)} internal link(s) found — at least 2 required. A link "
        "counts only if its target starts with https://www.rededucation.com or "
        "with '/' — an external source link does not, however relevant. Link "
        "related guides and course pages inline, using markdown: "
        "[anchor text](https://www.rededucation.com/page/)."))

    # M6 Freshness signal: a visible "Last updated" line carrying a FULL
    # date. v1.0 matched the bare phrase, so "Last updated: August 2026"
    # passed — the exact format v1.3 fails by name.
    _stamp = re.search(r"last updated[:\s]*([^\n<]*)", content, re.I)
    _stamp_full = bool(_FULL_DATE_RE.search(content))
    if _stamp_full:
        _fresh_msg = 'A full-date "Last updated" line is present.'
    elif _stamp:
        _fresh_msg = (
            f'The "Last updated" line reads "{_stamp.group(1).strip()}" — a '
            "month and year is not a full date and is not machine-readable. "
            'Write it as "Last updated: 19 August 2026".')
    else:
        _fresh_msg = ('A visible "Last updated: 19 August 2026" line is '
                      "required — a FULL date, not a month and year. Freshness "
                      "is weighed by both Google and AI engines.")
    checks.append(ReacsCheck("freshness_stamp", "mandatory", _stamp_full, _fresh_msg))

    # M7 Instructor byline — REMOVED FROM THE GATE, 2026-08-24, by decision.
    #
    # v1.3 makes this mandatory and calls a wrong byline "a live
    # misrepresentation". It is off here anyway because enforcing it blocked
    # articles for the 18 of 24 vendors that have no certified instructor in
    # instructor_vendors, and the agreed interim is to publish under the
    # shared Admin WordPress account instead. That is a KNOWN DIVERGENCE from
    # the standard, and the same defect its own register logs as CASE 1
    # ("Author was admin") — recorded here rather than quietly dropped.
    #
    # `has_byline` is deliberately still a parameter: every call site already
    # passes it, and reinstating this pillar is re-adding the four lines
    # below. Nothing else needs to change.
    #
    #   checks.append(ReacsCheck(
    #       "instructor_byline", "mandatory", has_byline,
    #       "No instructor byline is assigned. ..."))
    #
    # pick_instructor() below prefers a vendor-certified instructor and falls
    # back to the whole roster only when none is — a separate decision taken
    # 2026-08-25 so every article carries a byline. That fallback waives
    # M7(b), the vendor match, which is the one part of M7 the standard's
    # register logs as an actual live defect. It is coherent only while M7 is
    # off the gate: REINSTATING THE PILLAR MEANS REMOVING THE FALLBACK TOO,
    # or the gate blocks on a byline this module deliberately assigned.

    # ---------- ADVISORY (scored) ----------
    # A1 Question-style H2s (AI-extractable section framing).
    # Markdown H2s plus raw <h2> — see comparison_table above for why HTML
    # counts. Advisory rather than mandatory, so a miss costs score instead of
    # blocking, but the blindness is identical and worth closing here too.
    question_h2s = (len(re.findall(r"^##\s+[^\n]*\?\s*$", content, re.M))
                    + len(re.findall(r"<h2[^>]*>[^<]*\?\s*</h2>", content, re.I)))
    # Naming the count alone left the writer to guess the mechanism, and on a
    # TARGETED fix — where the revision block also says "keep everything not
    # named in the feedback exactly as it is" — "add more question headings"
    # reads as "add sections", which the model then refuses to do. So say how
    # many are missing, say to REPHRASE rather than add, and name the actual
    # candidates from this article.
    if question_h2s >= 3:
        _qh_msg = f"{question_h2s} question-style H2 heading(s)."
    else:
        _plain = [h.strip() for h in re.findall(r"^##\s+([^\n]*[^?\s])\s*$",
                                               content, re.M)
                  if not h.strip().lower().startswith(
                      ("summary", "conclusion", "final", "next steps"))]
        _qh_msg = (
            f"{question_h2s} question-style H2 heading(s) — at least 3 are "
            f"required, so {3 - question_h2s} more is needed. Do NOT add a new "
            "section: REPHRASE an existing H2 as the question a reader would "
            "actually type, keeping that section's text unchanged"
            + (" — e.g. " + ", ".join(f'"{h}"' for h in _plain[:3])
               + " could each become a question." if _plain else "."))
    checks.append(ReacsCheck(
        "question_headings", "advisory", question_h2s >= 3, _qh_msg))

    # A2 Entity coverage: related products/certifications from the
    # knowledge base.
    keys = {_entity_key(e) for e in entities if _entity_mentioned(e, lower)}
    # Collapse vendor-prefixed duplicates onto their bare form ("palo alto
    # networks panorama" -> "panorama"), keeping only the shortest variant of
    # each, so one real entity is never counted twice.
    mentioned = {k for k in keys if not any(o != k and k.endswith(o) for o in keys)}
    # Naming the count alone left the writer guessing which names count —
    # the KB list in the prompt is up to 25 formal catalogue names, and the
    # matcher accepts short aliases. List the actual missing ones so a retry
    # has something to act on.
    if len(mentioned) >= 4:
        _ent_msg = (f"{len(mentioned)} knowledge-base entities mentioned "
                    "(4+ required).")
    else:
        # "Covered" must use the SAME suffix-collapse the count uses. Testing
        # `key in mentioned` alone listed an entity as missing whenever its
        # vendor-prefixed twin was the one that collapsed away: with "Panorama"
        # plainly in the text, the message still demanded the writer add "Palo
        # Alto Networks Panorama". That is unfollowable feedback — the thing is
        # already there, so no rewrite can clear it and the check never
        # settles.
        def _covered(key: str) -> bool:
            return any(key == m or key.endswith(m) for m in mentioned)

        missing, seen_missing = [], set()
        for e in entities:
            key = _entity_key(e)
            if _covered(key) or key in seen_missing:
                continue
            seen_missing.add(key)
            missing.append(_entity_alias(e) or e.strip())
        # With no KB entities supplied there is nothing to name, and the old
        # wording trailed off into "by name: ." — feedback that reads as
        # truncated output rather than an instruction.
        _ent_msg = (
            f"Only {len(mentioned)} knowledge-base entities mentioned — at "
            "least 4 required."
            + (" Work in whichever of these genuinely fit the article, by "
               "name: " + ", ".join(f'"{m}"' for m in missing[:8])
               + ". Do not force ones that don't belong." if missing else
               " The Vendor Knowledge Base lists none for this vendor, so "
               "this cannot be cleared from the article — add the vendor's "
               "products and certifications in the Vendor Centre."))
    checks.append(ReacsCheck(
        "entity_coverage", "advisory", len(mentioned) >= 4, _ent_msg))

    # A3 Experience voice, CORRECTLY ATTRIBUTED. v1.3 inverts what v1.0 did
    # here, and the inversion is the single most important change in this
    # file. v1.0 awarded a point for matching "after delivering..." — which is
    # precisely the boilerplate the standard now names as a template artefact
    # the generator inserts to satisfy this pillar cheaply. The pillar was
    # scoring the defect, and reacs_feedback_block() fed the phrase back to
    # the writer on every retry that omitted it.
    #
    # The rule is integrity, not presence: first-person experience under a
    # byline reading "Technically reviewed by" leaves the reader no "I" to
    # attach the claim to. Absence of experience voice is no longer a miss;
    # unattributable experience voice is.
    #
    # And the third person is not automatically attributable. One article
    # carried "Red Education instructors regularly see", "...note that" and
    # "...frequently observe" — three unattributable observations, all
    # passing this pillar. They were not the model reaching for a cheap
    # template: REACS_PROMPT_ADDENDUM and this very message both handed it
    # "Red Education instructors regularly see..." as the worked example, in
    # every article's prompt. Neither does any more. QA's
    # unnamed_attribution catches what still gets through.
    _voice_hits = [m for m in FIRST_PERSON_MARKERS if m in lower]
    if _BOILERPLATE_EXPERIENCE_RE.search(content):
        _voice_hits.append("a fabricated volume of learners taught")
    if _voice_hits:
        _voice_msg = (
            "First-person experience under a \"Technically reviewed by\" "
            "byline: " + ", ".join(f'"{m}"' for m in _voice_hits)
            + ". State the point directly instead, with no attribution, "
            "unless a named instructor can be credited with it. Never state "
            "a volume of learners taught or a set of environments that is "
            "not in the research facts.")
    else:
        _voice_msg = ("Experience voice is third person, consistent with the "
                      "reviewed-by byline.")
    checks.append(ReacsCheck("voice_consistency", "advisory",
                             not _voice_hits, _voice_msg))

    # A4 Common mistakes section (blueprint requirement). A heading alone is
    # not substance: article 11 passed with three generic sentences that gave
    # the reader neither consequences nor corrective actions. Keep this
    # deterministic by looking for structured entries plus consequence/action
    # language, not by asking another model to grade style.
    _mistakes = re.search(r"^##\s*(.*(?:mistake|pitfall).*)$", content,
                          re.I | re.M)
    _mistake_body = ""
    if _mistakes:
        start = _mistakes.end()
        following = re.search(r"^##\s+", content[start:], re.M)
        end = start + following.start() if following else len(content)
        _mistake_body = content[start:end]
    _mistake_items = len(re.findall(
        r"(?m)^\s*(?:[-*]\s+|\d+[.)]\s+|\*\*[^*]{3,}\*\*)",
        _mistake_body))
    _consequences = len(re.findall(
        r"\b(?:can|may|causes?|leads? to|results? in|leaves?|risks?|"
        r"prevents?|interrupts?|disrupts?)\b", _mistake_body, re.I))
    _corrections = len(re.findall(
        r"\b(?:correct(?: it)? by|fix(?: it)? by|instead|avoid|review|verify|"
        r"monitor|validate|start with|use)\b", _mistake_body, re.I))
    _mistake_words = len(re.findall(r"\b\w+\b", _mistake_body))
    _mistake_substantive = bool(
        _mistakes and _mistake_words >= 60
        and (_mistake_items >= 3
             or (_consequences >= 3 and _corrections >= 3)))
    checks.append(ReacsCheck(
        "common_mistakes", "advisory", _mistake_substantive,
        f'Found {max(_mistake_items, _consequences)} concrete mistake '
        f'item(s) with consequences and corrective actions in "'
        f'{_mistakes.group(1).strip()}".' if _mistake_substantive else
        (f'The section "{_mistakes.group(1).strip()}" is too generic. Give at '
         "least 3 specific mistakes, and for each state the consequence and "
         "the corrective action." if _mistakes else
        'Include a "Common mistakes" section — an H2 whose heading contains '
        '"mistake" or "pitfall". It is a blueprint element AI Overviews '
        "frequently quote, and it should name specific failure modes.")))

    # A5 Definitional citable fact: a clean "X is ..." sentence containing
    # the keyword.
    citable = False
    if kw:
        pattern = re.escape(keyword) + r"[^.!?\n]{0,40}\bis\b"
        citable = bool(re.search(pattern, content, re.I))
    # Same lesson as direct_answer_first: this message is fed to the writer on
    # retry, so it has to state the mechanical requirement, not the intent.
    # The old wording only asked for "a clean definitional sentence", and
    # drafts kept missing on a detail nothing told them about — writing "The
    # <keyword> verifies/validates your skills" (no "is"), or splitting the
    # phrase with a parenthetical. Both read fine; neither matches.
    if citable:
        _cite_msg = "A quotable definitional sentence names the keyword."
    else:
        _cite_msg = (
            'Include one definitional sentence an AI can quote: the exact '
            f'phrase "{keyword}" followed within 40 characters by the word '
            f'"is" — e.g. "The {keyword} is the ...". Synonyms for "is" '
            '("verifies", "validates", "covers") do not count, and nothing '
            'may split the phrase (an inserted "(F5-CA)" breaks it).')
    checks.append(ReacsCheck("citable_definition", "advisory", citable, _cite_msg))

    # A6 Summary + a SINGLE call to action. v1.0 checked only that a closing
    # summary H2 existed and never counted CTAs, so a stacked close — a sales
    # line in the summary, a course-link block, a trailing "get started today"
    # — passed cleanly. v1.3 counts them: one close, not four, and not zero.
    # Zero matters because only the BODY is published; a CTA living solely in
    # the separate `cta` field ships an article with no call to action at all.
    _has_summary = bool(re.search(
        r"^##\s+(summary|final|conclusion|next steps)", content, re.I | re.M))
    _n_cta = (sum(lower.count(p.lower()) for p in cta_phrases)
              if cta_phrases else None)
    _summary_note = (
        "No closing summary H2. The heading must START with one of the words "
        "\"Summary\", \"Conclusion\", \"Final\" or \"Next steps\" — this is "
        "matched literally, so \"## The ROI of X Expertise\" does not count "
        "however good the section is. Put it before the FAQ and the single CTA.")
    if _n_cta is None:
        _cta_ok, _cta_note = True, ""
    elif _n_cta == 1:
        _cta_ok, _cta_note = True, ""
    elif _n_cta == 0:
        _cta_ok = False
        _cta_note = ("No approved call-to-action phrase appears in the BODY. "
                     "Only the body is published, so a CTA in the separate "
                     "`cta` field alone ships an article with no close.")
    else:
        _cta_ok = False
        _cta_note = (f"{_n_cta} calls to action in the body — v1.3 allows "
                     "exactly one. Stacked CTAs read as a link farm and "
                     "undercut the educational credibility the whole approach "
                     "depends on. Keep the course-link close; delete the rest.")
    _a6_msg = " ".join(x for x in
                       ([] if _has_summary else [_summary_note]) + [_cta_note] if x)
    checks.append(ReacsCheck(
        "summary_and_cta", "advisory", _has_summary and _cta_ok,
        _a6_msg or "Closing summary H2 present with a single call to action."))

    # ---------- Score & verdict (ours, never the model's) ----------
    mandatory = [c for c in checks if c.tier == "mandatory"]
    advisory = [c for c in checks if c.tier == "advisory"]
    mandatory_failed = sum(1 for c in mandatory if not c.passed)
    m_pts = (sum(1 for c in mandatory if c.passed) / len(mandatory)) * 60
    a_pts = (sum(1 for c in advisory if c.passed) / len(advisory)) * 40 if advisory else 0
    score = round(m_pts + a_pts)

    return ReacsReport(
        score=score,
        passed=(mandatory_failed == 0 and score >= REACS_THRESHOLD),
        mandatory_failed=mandatory_failed,
        checks=checks,
        checked_at=datetime.now(timezone.utc).isoformat(),
    )


def pick_instructor(vendor_matched: list[str], all_active: list[str],
                    usage: dict[str, int] | None = None) -> str | None:
    """An instructor byline, rotated so it spreads evenly across the roster.

    WHO IS ELIGIBLE. Instructors certified for this article's vendor, and if
    NONE are, the whole active roster. That fallback is a decision taken on
    2026-08-25: every article gets a byline rather than publishing
    unattributed.

    Know what it costs, because the standard is blunt about it. v1.3's M7
    requires the byline instructor to be certified for the article's vendor
    and calls a byline that is not "a live misrepresentation, not a formatting
    error". Its defect register logs the failure twice — Travis Loke credited
    on a Palo Alto article, Dan Schultz on a Cisco one — and 10 posts on the
    live estate carry it today. This is a deliberate divergence, and it is
    coherent only because M7 was already removed from the gate: nothing here
    claims to enforce vendor matching any more.

    What the fallback does NOT do is misstate anyone's credentials. The
    rendered line lists the vendors that instructor actually holds, from
    instructor_vendors, so an Avaya article reviewed by Travis Loke reads
    "Certified Instructor, AWS, Palo Alto Networks" — true, if not obviously
    relevant. M7(c) stays satisfied even while M7(b) is waived.

    WHICH ONE, when several qualify. The least-used, with a random tie-break
    among those on the lowest count. That gives round-robin without storing a
    cursor: everyone reaches N articles before anyone reaches N+1. Plain
    random.choice() could plausibly give one name six in a row.

    `usage` is counted PER VENDOR by the caller. Rotation is about spreading a
    vendor's articles across people; whether Dan Schultz already carries ten
    Check Point articles says nothing about whose turn it is on Avaya."""
    pool = vendor_matched or all_active
    if not pool:
        return None
    counts = usage or {}
    fewest = min(counts.get(name, 0) for name in pool)
    return random.choice([n for n in pool if counts.get(n, 0) == fewest])


def reacs_feedback_block(report: ReacsReport) -> str:
    """Turn REACS's failed/missed checks into targeted retry instructions —
    same purpose as synthesis/models.py's QAReport.feedback_block(), for the
    sibling gate. Used by synthesis/brief.py's generate_passing_article() to
    retry a draft that passed QA but was blocked by REACS (a missing table,
    too few question-style headings, etc.) — previously REACS was scored
    only once, after that loop already returned, so a miss here had no
    correction mechanism at all. Empty string if nothing needs fixing."""
    issues = [c for c in report.checks if not c.passed]
    if not issues:
        return ""
    lines = [f"- [{c.tier.upper()}] {c.pillar}: {c.message}" for c in issues]
    return (
        f"PREVIOUS ATTEMPT PASSED QA BUT WAS BLOCKED BY REACS (score "
        f"{report.score}, needs {REACS_THRESHOLD}+ to pass; "
        f"{report.mandatory_failed} mandatory check(s) failed) — fix these "
        "specific gaps (edit the previous approach, don't just regenerate "
        "and hope for a different result):\n" + "\n".join(lines)
    )


# ---------- Prompt addendum (feeds Brief + Writer upstream) ----------
# The reference's addendum hardcodes "Last updated: <Month Year>" as literal
# instruction text; here the model already receives today's REAL date via
# synthesis/text.py's today_block() (injected separately into every prompt),
# so this addendum references that instead of repeating a stale instruction.
#
# PRECEDENCE MATTERS. This addendum sits in the same system prompt as
# synthesis/style.py's STRUCTURE block, which is shared with the ungoverned
# ad-hoc pipeline and was written before REACS existed — so it contradicted
# this standard outright in three places, and the model reasonably followed
# the more detailed house-style wording:
#   - STRUCTURE: "Not every article needs [a table]; force-fitting a table
#     where the content doesn't call for it is worse than skipping it."
#     -> mandatory comparison_table failed, and NO number of retries could
#        fix it: the retry feedback was arguing against the prompt itself.
#   - STRUCTURE: "a 4-5 question FAQ" vs faq_depth's minimum of 8.
#   - STRUCTURE: "End with: a summary section framing the ROI..." produced
#     "## The ROI of <vendor> Expertise", which summary_and_cta's regex
#     (summary|final|conclusion|next steps) cannot match.
# The items below state precedence explicitly and name the exact heading words
# the checks look for. The table exemption is additionally deleted at source in
# style.py (a flat contradiction helps neither pipeline); the FAQ count and
# summary heading are overridden here rather than there, because 4-5 FAQs and
# an ROI-framed close are genuinely the house norm for the UNgoverned ad-hoc
# pipeline, which never sees this addendum.
#
# KEEP THIS TERSE. An earlier revision argued its case in long prose bullets
# and measurably backfired: compliance on unrelated items collapsed in the
# same draft (internal_linking and freshness_stamp went from passing to
# failing) because the verbosity crowded them out. Terse imperatives, one line
# each — this is a checklist the model works through, not an explanation.
REACS_PROMPT_ADDENDUM = """\
REACS v1.3 - MANDATORY article standard (the draft is machine-checked against every item).
Where anything above conflicts with an item below, THE ITEM BELOW WINS:
- Open with a direct answer: 70 words or fewer, naming the target keyword, before any other content. ANSWER the question; do not describe the article. "This guide breaks down..." is a description and fails.
- One primary search intent only; the target keyword appears VERBATIM in both the article title and the meta title.
- Begin each H2 section with the direct answer, then explain. Answer key questions in 40-60 words so they can be lifted as snippets.
- At least 3 H2 headings phrased as questions ending in "?" ("What is X?", "How hard is the X exam?").
- Include at least one markdown table. Required, not optional: use exam domains and weightings, exam logistics, certification tiers, or course options.
- Include a "Common mistakes" section with at least 3 concrete mistakes. For each, state the operational consequence and the corrective action; a heading followed by generic advice does not satisfy this.
- Do NOT attribute observations to unnamed staff. If you have a named instructor and a specific thing they observed, attribute it to them by name. If you do not — and unless the brief supplies one, you do not — make the point directly, with no attribution at all. An observation credited to "our instructors" borrows authority from nobody and is worth less than the same sentence stated plainly.
- The byline reads "Technically reviewed by", so there is no "I": never write "I have observed", "in my experience" or similar. Never invent a volume of learners taught or a set of environments.
- Never state a software or certification version number unless it appears in the supplied research facts. If the version is not supplied, write the sentence without a version rather than guessing. Do not infer that a certification tracks the vendor's latest release.
- Never state that a bug is critical, an API is unusable, or a version range is affected unless the subject vendor's own advisory or release note supports the exact version/issue identifier and impact. Cite that primary source in the same sentence; otherwise omit the claim.
- Keep single-vendor articles single-vendor. Do not add another vendor, product, integration, section, table row or FAQ unless the title or brief explicitly commissions a comparison.
- Scope roles and permissions to the exact product or management console in the source. Present guided or automated configuration as one available workflow unless the primary source explicitly says it is required.
- Cover related entities naturally: the vendor's products, certifications and adjacent technologies provided in the context.
- Include one clean definitional sentence an AI can quote ("X is the ...").
- Use 2-4 relevant internal links to DIFFERENT rededucation.com pages, written as markdown — [anchor text](https://www.rededucation.com/page/), not raw <a href> HTML — plus named external sources. Every link must support a concept discussed in the prose. Never link the site root: it does not count and is worse than no link.
- Close with, in order: a closing H2 whose heading STARTS with "Summary", "Conclusion", "Final" or "Next steps" (e.g. "## Summary: Is the X certification worth it?" — it may still frame ROI, but must start with one of those four words); then around 10 genuine FAQs, MINIMUM 8, not 4-5, each with a WRITTEN ANSWER (a question with an empty answer does not count); then EXACTLY ONE course CTA, in the body itself. One close, not four: no extra sales line in the summary and nothing appended after the course link. The first 90% educates; the last 10% converts.
- End with a visible line: "Last updated: <the current FULL date as day Month Year, e.g. 19 August 2026 — see the date given above, never a past or future one>". A month and year alone fails."""
