"""Data models for the synthesis stage."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import TYPE_CHECKING
import json

if TYPE_CHECKING:
    from retrieval.models import Source


@dataclass
class Reference:
    tag: str          # e.g. "S1"
    url: str
    title: str
    domain: str = ""


def references_from_tags(tags: "dict[str, Source]") -> list[Reference]:
    """Shared by generate.py (ad-hoc Writer) and brief.py (Brief+Writer
    pipeline) — both build a Draft-shaped result from the same {tag: Source}
    map that build_prompt()/build_article_prompt() produce."""
    return [Reference(tag=t, url=s.url, title=s.title, domain=s.domain)
            for t, s in tags.items()]


@dataclass
class Draft:
    topic: str
    region: str | None
    title: str
    slug: str
    meta_description: str
    body_markdown: str                      # contains [S1]-style citation markers
    faq: list[dict] = field(default_factory=list)   # [{"q":..., "a":...}]
    references: list[Reference] = field(default_factory=list)
    raw: str = ""                           # raw model output, for debugging

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(asdict(self), indent=indent)

    def word_count(self) -> int:
        import re
        text = re.sub(r"[#*_>\[\]()`|-]", " ", self.body_markdown)
        return len(text.split())


@dataclass
class Check:
    name: str
    status: str        # "pass" | "warn" | "fail"
    detail: str = ""


# Checks validate.py never emits "fail" for, by design — they inform the
# reviewer rather than block the draft. Everything NOT listed here is
# integrity-tier and can block.
#
# This list only weights the SCORE; it does not gate anything. If a check
# here ever did emit "fail", `passed` would still go False (the gate counts
# fails regardless of tier) — the only damage would be that the failure was
# weighted at 40% instead of 60%. Verified against validate.py's actual
# r.add(...) calls when written; re-check it there if you add a check.
QA_ADVISORY_CHECKS = frozenset({
    "meta_description",       # a length nit, not an integrity problem
    "serp_competitiveness",   # measures external SERP state, not the draft
    "direct_answer",          # editorial style
    "entity_coverage",        # KB breadth, an editorial call
    "internal_links",         # brief-requested links the writer may reasonably drop
    "internal_link_targets",  # a link an editor would swap, not a broken article
    "internal_link_density",  # link count is editorial; relevance is checked separately
    "link_relevance",         # whether the prose earned a link is an editorial call
    "unnamed_attribution",    # house-voice problem; naming the instructor fixes it
    "table_substance",        # a table's cells are editorial, not a grounding claim
    "technical_claim_scope",  # qualification and citation specificity need editorial review
    "title_subject_coverage", # a term may be covered by a synonym; human call
    "title_readability",      # SEO title polish; an editor fixes it in seconds
    "promise_gaps",           # V4: editorial — add the figure or retitle
    "keyword_repetition",     # V9: SEO polish, not a truth claim
    "brand_casing",           # V5: presentation, though the standard rates
                              #     title/meta high — the message says so
    "locale_spelling",        # V6: house style, not a truth claim
    "exam_code_presence",     # V2: completeness; becomes integrity once the
                              #     registration codes are backfilled
    # attribution_match (V7a), vague_attribution (V7b), interested_party (V8)
    # and corpus_consistency (V10) are NOT here. Every one of them is about
    # whether a claim is true and properly sourced, which is integrity — they
    # warn only because knowledge-base coverage is partial, and they carry
    # integrity weight while they do. named_individual (V7c) already FAILS.
    # NOTE: version_strings (V1) is deliberately NOT here. It ships as a WARN
    # only because the knowledge base is still being backfilled, but it is an
    # INTEGRITY check — whether the article states a true version — and it
    # should carry integrity weight while it warns and become a FAIL when the
    # backfill lands.
})

# Integrity is 60 of the 100 points and advisory the other 40 — the same split
# REACS uses for mandatory/advisory, so the two scores in the UI are read on
# comparable terms. They remain SEPARATE engines (see synthesis/reacs.py's
# module docstring); this shares the weighting convention, not the code.
QA_INTEGRITY_WEIGHT = 60
QA_ADVISORY_WEIGHT = 40

# A WARN is a real quality signal, not a pass — but it isn't a failure either,
# so it takes half credit. Without this a draft whose course link has no CTA
# would score identically to one that nails it.
_STATUS_CREDIT = {"pass": 1.0, "warn": 0.5, "fail": 0.0}


@dataclass
class QAReport:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = ""):
        self.checks.append(Check(name, status, detail))

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == "fail"]

    @property
    def warned(self) -> list[Check]:
        return [c for c in self.checks if c.status == "warn"]

    @property
    def passed(self) -> bool:
        """A draft is publishable-candidate only if nothing failed.

        Deliberately NOT a function of `score`: the score grades quality, the
        gate answers "is anything actually broken?". Wiring a threshold in
        here would start blocking drafts that pass every integrity check
        purely for accumulating advisory warnings."""
        return len(self.failed) == 0

    @property
    def score(self) -> int:
        """0-100 quality score across the checks that actually RAN.

        Several checks are conditional (keyword_coverage only with keywords,
        entity_coverage only with entities, serp_competitiveness only with a
        SERP snapshot), so the denominator is what ran, not a fixed 13 — a
        check that couldn't apply must neither reward nor punish the draft.

        Returns -1 for a report with no checks, matching the "never computed"
        sentinel Article.reacs_score already uses."""
        if not self.checks:
            return -1
        integrity = [c for c in self.checks if c.name not in QA_ADVISORY_CHECKS]
        advisory = [c for c in self.checks if c.name in QA_ADVISORY_CHECKS]

        def tier(checks: list[Check]) -> float:
            return sum(_STATUS_CREDIT.get(c.status, 0.0) for c in checks) / len(checks)

        # If a tier had no checks at all, its weight goes to the other tier
        # rather than being lost — otherwise an article that skipped every
        # advisory check would be capped at 60 for no fault of its own.
        if not advisory:
            return round(tier(integrity) * 100)
        if not integrity:
            return round(tier(advisory) * 100)
        return round(tier(integrity) * QA_INTEGRITY_WEIGHT
                     + tier(advisory) * QA_ADVISORY_WEIGHT)

    @property
    def score_band(self) -> str:
        return qa_score_band(self.score, bool(self.failed))

    def feedback_block(self) -> str:
        """Turn fail/warn checks into targeted, actionable fix instructions
        for a retry prompt — e.g. originality's detail already names the
        exact copied phrase, so a retry can rephrase that specific passage
        instead of regenerating the whole draft and hoping sampling variance
        does better. Shared by both generate.py's and brief.py's retry
        loops. Empty string if nothing needs fixing (or the report is a
        fresh, unchecked one)."""
        issues = [c for c in self.checks if c.status in ("fail", "warn")]
        if not issues:
            return ""
        lines = []
        for c in issues:
            line = f"- [{c.status.upper()}] {c.name}: {c.detail}"
            # Models reliably undershoot a vague "write ~1500 words" target —
            # naming the concrete fix (more sections, more depth) works far
            # better than repeating the number, which the model already saw
            # once in STRUCTURE and still missed.
            if c.name == "structure_length":
                # SCALE THE ASK TO THE ACTUAL SHORTFALL. This used to say
                # "add 2-3 more H2 sections" unconditionally — 400-600 words
                # — even when the draft was 43 words short. On a TARGETED fix
                # that instruction also contradicts the revision block, which
                # tells the model to keep everything not named in the feedback
                # exactly as it is. Asked to add six hundred words and change
                # nothing, the model reasonably did neither, and the check
                # survived click after click. Observed on a live 1457-word
                # draft.
                import re as _re
                m = _re.search(r"(\d[\d,]*)\s+words", c.detail or "")
                have = int(m.group(1).replace(",", "")) if m else 0
                short = 1500 - have if have else 0
                if 0 < short <= 150:
                    line += (f" — you are only {short} words short of the 1500 "
                            "floor. Do NOT add new sections. Deepen one or two "
                            "existing sections with a concrete example, a "
                            "figure, or a counterpoint from the sources, and "
                            "leave every other sentence exactly as it is.")
                else:
                    line += (" — the target is 1500-2000 words: add 2-3 more H2 "
                            "sections of roughly 200 words each with concrete, "
                            "sourced detail (an example, a counterpoint, more "
                            "named data), and deepen any thin existing sections "
                            "the same way. Do not pad with filler or repetition.")
            elif c.name == "keyword_coverage":
                line += (" — this is an exact substring match: write each "
                        "missing phrase character-for-character inside a "
                        "natural sentence in the article body (rewording or "
                        "splitting the phrase does NOT count).")
            elif c.name == "title_subject_coverage":
                line += (" — the article must deliver what its headline "
                        "promises. Add real, sourced coverage of the named "
                        "subject (a dedicated H2 is usually right); if the "
                        "sources genuinely do not support it, say so rather "
                        "than padding with generic material.")
            elif c.name == "direct_answer":
                line += (" — rewrite the opening so the FIRST paragraph is a "
                        "standalone 40-60 word direct answer to the article's "
                        "primary question; scene-setting comes after.")
            elif c.name == "originality":
                # The detail names the copied run but never said what to DO
                # about it, so a retry saw the phrase quoted at it with no
                # imperative — and the revision prompt's "keep everything not
                # named here exactly as it is" pushed the other way. The
                # model had lifted the phrase straight out of the source
                # excerpt we handed it, then kept re-emitting it.
                line += (" — that wording was lifted from the source text in "
                        "this prompt. Find EVERY sentence listed above in the "
                        "draft and DELETE it, then write a new one from "
                        "scratch stating the same fact in your own words, "
                        "keeping its [S#] citation and sharing no run of more "
                        "than a few consecutive words with the source. Fix "
                        "them ALL in this pass — they are listed together for "
                        "that reason. Reproducing any of them unchanged, "
                        "anywhere in the article including inside an FAQ "
                        "answer, fails again. Source material is evidence to "
                        "restate, never phrasing to reuse.")
            elif c.name == "grounding":
                line += (" — that figure appears nowhere in the source "
                        "material, so delete it. Either cite a real number "
                        "from the VERIFIED/UNVERIFIED claims with its [S#] "
                        "tag, or rewrite the sentence qualitatively ('a "
                        "substantial premium', 'several years'). Do not "
                        "estimate, round or infer a replacement.")
            elif c.name == "flagged_leakage":
                line += (" — that data came from a FLAGGED claim, which must "
                        "not appear in the article at all. Remove the figure "
                        "and the sentence that carries it; do not substitute "
                        "another number for it.")
            elif c.name == "meta_description":
                line += (" — shorten the meta_description field to 155 "
                        "characters or fewer, keeping the target keyword in it.")
            elif c.name == "serp_competitiveness":
                line += (" — informational only, never blocking. Add depth "
                        "only where you have sourced material to add; do not "
                        "pad to hit a competitor's word count.")
            elif c.name == "internal_link_targets":
                line += (" — REPOINT each bad link, do not delete it. Find the "
                        "matching URL under APPROVED INTERNAL LINKS and use it, "
                        "copied character-for-character, one anchor per URL. "
                        "Deleting links is not a fix: successive passes that "
                        "each dropped a link took an article from 14 links to 2, "
                        "which is the bare minimum and loses real value. Keep "
                        "every URL the brief requested. Only if a course has NO "
                        "URL anywhere in that list should you leave its name as "
                        "plain text. Never link the site root, and never point a "
                        "second anchor at a URL you have already used — pick a "
                        "different page from the list instead. One anchor names "
                        "exactly ONE course: if a sentence mentions two ("
                        "\"Administering BIG-IP & Configuring BIG-IP DNS\"), "
                        "link each course name separately to its own URL rather "
                        "than wrapping both in one link.")
            elif c.name == "citation_integrity":
                # The bare detail ("1 sources cited; unused refs: ['S2','S3']")
                # is a status line, not an instruction — the model was left to
                # infer that it should go and use those tags, and reliably
                # didn't. This survived every targeted-fix attempt on an
                # article whose other issues cleared on the first try.
                line += (" — every [S#] tag listed as unused must appear "
                        "inline in the body. Find a claim that source actually "
                        "supports, state it in your own words, and cite it "
                        "with that exact tag. Do NOT invent tags, do not cite "
                        "a source for something it doesn't say, and do not "
                        "add a bare reference list — the tag has to sit on a "
                        "real sentence.")
            elif c.name == "course_link_cta":
                line += (" — the closing CTA must use one of these approved "
                        "phrases VERBATIM: \"Advance your career with Red "
                        "Education\", \"Explore our courses\", \"Learn "
                        "real-world skills with Red Education\", \"Speak with "
                        "Our Team\", \"Download the Course Outline\". This is "
                        "an exact substring match, so a reworded equivalent "
                        "does not count.")
            elif c.name == "title_readability":
                # Names the FIELD and the constraint. Without this the model
                # was told an "SEO title" check failed and left to guess which
                # of the four Rank Math elements to add, inside a 60-character
                # cap it also could not see — so it regenerated something
                # near-identical and the check survived attempt after attempt.
                line += (" — this is the meta_title field, NOT the article "
                        "heading. Return a corrected meta_title of 60 "
                        "characters or fewer that keeps the target keyword "
                        "verbatim near the start and adds ONLY the missing "
                        "element named above. Rank Math scores a power word "
                        "(Complete, Essential, Expert, Proven) and a "
                        "sentiment word (Best, Easy, Hard, Worth, Smart) as "
                        "TWO SEPARATE checks, so a title with one still fails "
                        "the other — keep whichever it already has.")
            elif c.name == "internal_links":
                line += (" — each listed URL must appear character-for-"
                        "character as a markdown link target somewhere natural "
                        "in the body.")
            elif c.name == "entity_coverage":
                line += (" — weave 3+ of the listed vendor products/"
                        "certifications/courses in by their exact names where "
                        "they genuinely fit; do not force irrelevant ones.")
            lines.append(line)
        return (
            "PREVIOUS ATTEMPT WAS BLOCKED — fix these specific issues (edit "
            "the previous approach, don't just regenerate and hope for a "
            "different result):\n" + "\n".join(lines)
        )


def qa_score_band(score: int | None, has_failure: bool) -> str:
    """Coarse label for a QA score, so exactly one place decides what counts
    as good. A free function because it is needed from three angles — a live
    QAReport, a stored Article row, and the review template — and three copies
    of these thresholds is how they end up disagreeing.

    A blocked draft can NEVER band green. With 7 integrity checks, a single
    failure still arithmetically scores ~91; showing that as a green "pass"
    beside a draft the gate is blocking is a contradictory signal, and readers
    believe the colour before they read the number. The score stays honest at
    91 — only the colour defers."""
    if score is None or score < 0:
        return "none"
    if has_failure:
        return "fail"
    return "pass" if score >= 90 else "warn" if score >= 75 else "fail"
