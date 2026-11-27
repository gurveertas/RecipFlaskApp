"""Reliability QA harness.

Given a Draft and the VerifiedBundle it was written from, run a battery of
checks and return a QAReport. Nothing here needs an API key. A draft with any
FAIL is not a publish candidate; WARNs are for the human reviewer's attention.

Two profiles
------------
Article Studio and Quick draft are different products and are held to
different standards — see QaProfile. Studio is the default and is unchanged:
1300-word floor, Red Education course link required. Quick draft lowers the
floor and drops the course-link requirement, because that page takes any
topic and an article about cycling has no business advertising a firewall
course. Everything about grounding, citations, originality, spelling and
attribution is identical under both, and deliberately not configurable.

Checks
------
1. structure_length     — headings, word count, meta length, title present
2. citation_integrity   — every [S#] marker resolves; references are real bundle
                          sources; post is actually sourced
3. grounding            — no statistic (%/currency/CVE) appears in the draft that
                          isn't present in the source material (hallucination guard)
4. flagged_leakage      — no FLAGGED claim's figures/CVEs leak into the draft
5. originality          — no long verbatim run copied from any source
6. course_link_cta      — inline course link + a call-to-action present
7. keyword_coverage     — every requested SEO keyword actually appears in the
                          draft (only run when keywords are supplied)
8. serp_competitiveness — draft length vs the pages actually ranking for the
                          keyword (only run when a SerpSnapshot is supplied;
                          WARN-only by design — competitor fetches are flaky
                          and external SERP state must never BLOCK an
                          otherwise-sound article)
9. direct_answer        — the opening paragraph works as a standalone 40-60
                          word answer (AI-overview extraction pattern);
                          WARN-only — an editorial-style measure, not integrity
10. entity_coverage     — related Vendor KB entities (products/certs/courses)
                          actually mentioned (only when entities supplied;
                          WARN-only)
11. internal_links      — every internal link the brief requested appears in
                          the body (only when requested links supplied;
                          WARN-only)
12. title_subject_coverage — the subject the TITLE promises actually appears
                          in the body. Catches an article that is well
                          sourced and on-keyword but simply not about the
                          thing it is headlined as (WARN-only)
13. writing_mechanics   — acronym casing, repeated words, spacing (FAIL)
14. title_readability   — Rank Math's four SEO-title checks (WARN-only)
15. version_strings     — QA rule V1: every vendor release token in the body
                          is either recorded for a certification THIS article
                          is about, or backed by a verbatim source quote.
                          FAILS, i.e. blocks publication — the standard rates
                          it critical. Check Point only; see VERSION_PATTERNS
16. promise_gaps        — QA rule V4: an H2 promising a quantity (weightings,
                          cost, salary, duration) whose section contains no
                          number at all (WARN-only, editorial)
16c. entity_type        — the article's own subject is defined as the kind
                          of thing the Vendor KB says it is. A three-day
                          course described as a "framework" lands in the
                          opening sentence, which is the block an AI
                          assistant quotes. FAILS
16d. link_relevance     — an internal link whose subject the prose never
                          names outside the link itself (WARN-only)
16e. unnamed_attribution — an observation credited to "our instructors" with
                          nobody named (WARN-only)
16b. table_substance    — the same rule inside a TABLE: a column headed
                          "Logistics"/"Cost"/"Duration" whose cells carry no
                          figure. A table looks like reference data whether or
                          not it holds any (WARN-only)
17. keyword_repetition  — QA rule V9: the primary keyword appears more than
                          4 times as an exact match (WARN-only)
18. exam_code_presence  — V2: a certification article names its registration
                          code (WARN-only; skipped without one on record)
19. brand_casing        — V5: vendor names cased correctly in title, meta
                          title, meta description and body (WARN-only)
20. locale_spelling     — V6: Australian English, with vendor product names
                          exempted from the knowledge base (WARN-only)
21. attribution_match   — V7a: a claim credited to a named entity cites that
                          entity's own domain (WARN-only)
22. vague_attribution   — V7b: "studies show" with no citation (WARN-only)
23. named_individual    — V7c: a private individual named alongside a career
                          claim. THE ONE NEW RULE THAT FAILS, per the
                          standard: it needs no knowledge-base work and the
                          harm is not a quality nit
24. interested_party    — V8: a claim resting only on a competitor or
                          vendor-marketing domain (WARN-only)
25. corpus_consistency  — V10: version claims that contradict another article
                          on the estate (WARN-only)
26. heading_length      — a heading longer than 100 characters, which means
                          the paragraph after it was written on the same line
                          and markdown swallowed it into the heading. FAILS
27. vendor_grounding    — V11: a certification guide that cites no page from
                          the vendor whose exam it describes. FAILS: the
                          credential name, level, exam code and domain
                          weightings are only confirmable against the vendor,
                          and an article that never read one cannot have
                          checked any of them
28. vendor_scope        — a single-vendor brief grows a competitor section or
                          repeated competitor recommendation. FAILS unless the
                          other vendor was commissioned in the title/brief
29. severe_defect_claims — absolute bug/outage claims need a precise version
                           or advisory identifier and exact support from the
                           subject vendor's own cited source. FAILS
30. product_scope       — vendor-specific console roles and optional helpers
                          must be described in their actual management scope.
                          FAILS on known, deterministic conflations
31. internal_link_density — more than four internal links is an editorial
                            warning; links should be earned by the prose

32. citation_as_prose   — a [S#] tag is a pointer, never a word in the
                          sentence, and never a link's visible text. FAILS

Rules V1-V10 are the standard's QA RISK rules, reported separately from the
REACS pillar score and never folded into it. V3 (author identity) is
deliberately absent: it is REACS pillar M7, which was removed from the gate
by decision — see synthesis/reacs.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from synthesis.text import (title_readability_issues,
                            writing_mechanics_issues)

from retrieval.models import VerifiedBundle
from config import load_course_links
from .models import Draft, QAReport
from .qa_rules import (QaKnowledge, attribution_mismatches,
                       brand_casing, corpus_contradictions,
                       interested_party_only, missing_exam_codes,
                       named_private_individuals, unnamed_attributions,
                       us_spellings, vague_attributions)
from .text import distinctive_terms
from .technical_claims import technical_claim_findings, panorama_scope_problems

_PCT = re.compile(r"\d[\d,]*(?:\.\d+)?\s?%")
_CUR = re.compile(r"(?:\$|£|€|₹|INR|AUD|USD|SGD)\s?\d[\d,]*(?:\.\d+)?")
_CVE = re.compile(r"CVE-\d{4}-\d{4,7}", re.I)

VERBATIM_RUN_LIMIT = 10   # consecutive shared words that count as copying

# --------------------------------------------------------------------------- #
# QA rule V1 — unverified vendor version strings
# --------------------------------------------------------------------------- #
# Deliberately NARROW, one pattern per vendor. A loose "version-like" regex
# matches dates, prices and CVE identifiers, and a rule that cries wolf gets
# switched off.
#
# CHECK POINT ONLY, AND THAT IS A DECISION, NOT A BACKLOG. Patterns for Palo
# Alto Networks, Cisco, Nutanix and F5 were removed on 25 Aug 2026 because
# V1 verifies against certification_versions, and that table only makes sense
# for a vendor who versions THE EXAM:
#
#   Check Point does. CCTE R81.20 and CCTE R82 are different exams with
#   different registration numbers (156-587, 156-588). Recording "which
#   release does this exam test" is a real, sourceable fact.
#
#   The other four version their PRODUCTS, not their exams. Cisco exams carry
#   an exam number and an exam version (ENCOR 350-401 v1.1); "IOS-XE 17.9" is
#   a product release that turns up in technical prose. Same for PAN-OS,
#   BIG-IP and AOS — the Vendor KB even proves it, with "Configuring BIG-IP
#   Advanced WAF: Web Application Firewall 17.1" stored as a COURSE name.
#   certification_versions has nowhere to put a release that is not tied to a
#   certification, so those four had a live pattern and zero rows: the check
#   never ran for them, while every dashboard read as though it did. Coverage
#   that only looks like coverage is worse than none.
#
# TO POLICE PRODUCT RELEASES for those vendors, do not just re-add a pattern
# here — it will be silent again. Make certification_versions.certification_id
# nullable so a row can record a vendor-level release, and this check's
# "no certification named" branch below will finally have something to verify
# against.
VERSION_PATTERNS: dict[str, re.Pattern] = {
    "Check Point": re.compile(r"\bR8\d(?:\.\d{1,2}|\.X)?\b", re.I),
}

# QA rule V4 — a heading that promises a quantity and delivers none.
#
# The kind of number matters, which the reference implementation's blanket
# /\d/ test misses. Proved live: a section headed "Exam Domains and Technical
# Weighting" said only that "weighting per domain is managed by the vendor"
# and contained NO percentage — but it mentioned R82, so a bare digit test
# passed it. A weighting heading owes the reader a percentage, a cost heading
# owes a figure, and neither is satisfied by a release number.
PROMISE_WORDS: dict[str, re.Pattern] = {
    "weighting":  re.compile(r"\d\s?(?:%|percent)"),
    "weightings": re.compile(r"\d\s?(?:%|percent)"),
    "percentage": re.compile(r"\d\s?(?:%|percent)"),
    "cost":       re.compile(r"[$£€₹]\s?\d|\d[\d,]*\s?(?:AUD|USD|SGD|INR)|\bfree\b", re.I),
    "price":      re.compile(r"[$£€₹]\s?\d|\d[\d,]*\s?(?:AUD|USD|SGD|INR)|\bfree\b", re.I),
    "fee":        re.compile(r"[$£€₹]\s?\d|\d[\d,]*\s?(?:AUD|USD|SGD|INR)|\bfree\b", re.I),
    "salary":     re.compile(r"[$£€₹]\s?\d|\d[\d,]*\s?(?:AUD|USD|SGD|INR)", re.I),
    "duration":   re.compile(r"\d"),
    "how long":   re.compile(r"\d"),
    "how many":   re.compile(r"\d"),
    "how much":   re.compile(r"[$£€₹]\s?\d|\d"),
    "score":      re.compile(r"\d"),
    # "Exam logistics" means the bookable facts — a code, a duration, a
    # question count, a fee. A column headed Logistics whose every cell is
    # prose ("Proctored evaluation") promises those and delivers none.
    "logistics":  re.compile(r"\d"),
}

# Release tokens are digits that answer none of the promises above, so they
# are removed before the generic \d tests. "R82" is not a duration.
_RELEASE_TOKEN_RE = re.compile(r"\bR\s?8\d(?:\.\d{1,2}|\.X)?\b", re.I)

# QA rule V9 — exact-match primary keyword ceiling.
KEYWORD_REPEAT_MAX = 4


def _strip_code_and_links(text: str) -> str:
    """Body prose only. Version-like strings inside fenced code, inline code,
    link targets and bare URLs are addresses and samples, not editorial
    claims, and flagging them is how V1 earns a reputation for noise."""
    t = re.sub(r"```.*?```", " ", text or "", flags=re.S)
    t = re.sub(r"`[^`]*`", " ", t)
    t = re.sub(r"\]\([^)]*\)", "] ", t)          # markdown link TARGETS only
    t = re.sub(r"<[^>]+>", " ", t)                # raw href/src attributes
    return re.sub(r"https?://\S+", " ", t)


def extract_version_tokens(body: str, vendor_name: str | None) -> dict[str, int]:
    """{normalised token: occurrences} for this vendor's version pattern.

    Repeat mentions of ONE token collapse to one finding with a count, so an
    article saying R82 forty-eight times reports one problem, not forty-eight.
    """
    pattern = VERSION_PATTERNS.get(vendor_name or "")
    if not pattern:
        return {}
    out: dict[str, int] = {}
    for m in pattern.finditer(_strip_code_and_links(body)):
        token = re.sub(r"\s+", " ", m.group(0)).upper()
        out[token] = out.get(token, 0) + 1
    return out


def _ref_dicts(references) -> list[dict]:
    """Draft.references holds Reference dataclasses, but the same rules run
    against Article.references, which is stored JSON (plain dicts). Normalise
    once here so the rule functions only ever see dicts."""
    out = []
    for ref in references or []:
        out.append(ref if isinstance(ref, dict) else
                   {"tag": getattr(ref, "tag", ""), "url": getattr(ref, "url", ""),
                    "domain": getattr(ref, "domain", "")})
    return out


def _subject_certifications(title: str, body: str,
                            cert_versions: dict[str, list[str]]) -> list[str]:
    """Which certifications this article is actually ABOUT.

    This is the whole reason V1 is keyed on certification rather than vendor.
    Check Point ships R82, but the CCME exam is R81.X while CCSE moved to
    R82 — so verifying "does this vendor have an R82 anywhere?" would clear
    the exact sentence the rule exists to catch. The title is the strongest
    signal and is tried first; the body is the fallback. If neither names a
    certification we know versions for, the caller falls back to the vendor's
    whole set and says so, because a vague finding is worse than none."""
    t, b = title.lower(), body.lower()
    in_title = [c for c in cert_versions if re.search(rf"\b{re.escape(c.lower())}\b", t)]
    if in_title:
        return in_title
    return [c for c in cert_versions if re.search(rf"\b{re.escape(c.lower())}\b", b)]


def _norm_link(u: str) -> str:
    """One canonical form for comparing internal URLs: absolute, no fragment
    or query, single trailing slash, lowercased. Shared so the known-page
    check and the anchor-title lookup can never disagree about whether two
    spellings of the same URL are the same page."""
    u = (u or "").split("#")[0].split("?")[0]
    if u.startswith("/"):
        u = "https://www.rededucation.com" + u
    return u.rstrip("/").lower() + "/"



def _tables(body: str) -> list[list[list[str]]]:
    """Markdown tables as row-lists of cells, header first, delimiter dropped.

    A table is the most authoritative-looking thing on a page — it reads as
    reference data whether or not it holds any, which is exactly why its
    contents deserve checking rather than only its presence."""
    tables: list[list[list[str]]] = []
    current: list[list[str]] = []
    for line in (body or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if not all(set(c) <= set("-: ") and c for c in cells):   # delimiter
                current.append(cells)
        else:
            if len(current) >= 2:
                tables.append(current)
            current = []
    if len(current) >= 2:
        tables.append(current)
    return tables


# Nouns that describe a body of knowledge or a way of working. A training
# course is none of them, and neither is a certification. Deliberately a SHORT
# closed list of type nouns rather than a phrase blocklist: the failure is
# always "X is a <wrong type>", so the wrong types are the checkable part.
_WRONG_ENTITY_TYPE_RE = re.compile(
    r"\b(?:framework|discipline|methodolog(?:y|ies)|philosoph(?:y|ies)|"
    r"paradigm|approach|practice|mindset|body of knowledge|"
    r"school of thought)\b", re.I)

# What a course or certification may legitimately be called.
_RIGHT_ENTITY_TYPE_RE = re.compile(
    r"\b(?:course|training|class|workshop|certification|credential|exam|"
    r"qualification|programme|program)\b", re.I)


def _defining_sentences(body: str, subject: str) -> list[str]:
    """Every sentence that says what the subject IS.

    Only "<subject> ... is/are ..." counts. A sentence merely mentioning the
    subject is not a definition, and this rule has no business grading prose
    that never claimed to define anything.

    A few words are allowed between the subject and the verb, because the
    subject we resolve is the KB's ALIAS and the article writes the full name:
    the alias is "EDU-330" while the sentence opens "EDU-330 Firewall
    Troubleshooting is a technical framework". Requiring the verb immediately
    after the alias matched nothing on the very article this rule was built
    for. Capped at four, so an "is" further down the sentence cannot be
    mistaken for the definition.

    ALL of them, not the first. The article this rule was built for defined
    its subject twice - "is a technical framework" in the opening and "is a
    specialised technical discipline" further down. Returning only the first
    meant a fix to the opening made the check PASS while the second stayed
    wrong, which is exactly what a repair attempt produced.
    """
    if not subject:
        return []
    pattern = re.compile(
        re.escape(subject) + r"(?:\s+[\w-]+){0,4}\s+(?:is|are)\b[^.]{0,300}\.",
        re.I)
    return [m.group(0) for m in pattern.finditer(body or "")]


def _catalogue_aliases(name: str, vendor_name: str | None = None) -> set[str]:
    """Useful exact aliases for a structured course or credential name."""
    from .reacs import _entity_alias

    values = {(name or "").strip()}
    values.add(re.sub(r"\s*\([^)]{2,20}\)\s*$", "", name or "").strip())
    alias = _entity_alias(name or "")
    if alias:
        values.add(alias.strip())
    vendor = (vendor_name or "").strip()
    if vendor:
        for value in list(values):
            if value.lower().startswith(vendor.lower() + " "):
                values.add(value[len(vendor):].strip())
    return {v for v in values if len(v) >= 4}


def _course_credential_mislabels(text: str, kb: QaKnowledge) -> list[tuple[str, str]]:
    """Course aliases used directly as a certification, credential or exam."""
    credential_aliases = {
        alias.lower()
        for name in kb.certification_names
        for alias in _catalogue_aliases(name, kb.vendor_name)
    }
    found: list[tuple[str, str]] = []
    for course in kb.course_names:
        for alias in sorted(_catalogue_aliases(course, kb.vendor_name), key=len,
                            reverse=True):
            if alias.lower() in credential_aliases:
                continue
            pattern = re.compile(
                rf"(?<!\w){re.escape(alias)}(?!\w)"
                r"(?:\s*\([^)]{2,20}\))?\s+"
                r"(?:(?:is|was|as)\s+)?(?:(?:a|an|the)\s+)?"
                r"(?:professional\s+|technical\s+)?"
                r"(certification|credential|exam)\b", re.I)
            if pattern.search(text or ""):
                found.append((course, alias))
                break
    return found


_SENSITIVE_FACT_RE = re.compile(
    r"\b(?:formerly known as|previously (?:known as|called)|renamed (?:as|to)|"
    r"prerequisit\w*|must (?:first )?(?:complete|hold|take)|required before|"
    r"voucher\w*|(?:instructor-led|virtual|classroom|self-paced|on-demand)"
    r"[^.\n]{0,80}\b(?:day|days|hour|hours|week|weeks))\b", re.I)
_PROMOTION_RE = re.compile(r"\b(?:promotion|promotional|promo|discount|special offer)\b",
                           re.I)
_FULL_DATE_PATTERNS = (
    re.compile(r"\b(\d{1,2}\s+[A-Za-z]+\s+\d{4})\b"),
    re.compile(r"\b([A-Za-z]+\s+\d{1,2},?\s+\d{4})\b"),
)


_SEVERE_DEFECT_RE = re.compile(
    r"\b(?:critical\s+(?:bug|defect|issue|vulnerabilit\w*)|"
    r"(?:renders?|makes?|made)\b[^.!?\n]{0,100}\b(?:completely\s+)?"
    r"(?:unusable|inoperable|nonfunctional)|completely\s+unusable|"
    r"(?:API|service|feature|product)\b[^.!?\n]{0,80}\b(?:cannot|can't)\s+be\s+used)\b",
    re.I,
)
_PRECISE_DEFECT_TOKEN_RE = re.compile(
    r"\b(?:CVE-\d{4}-\d{4,7}|K\d{5,}|BUG[-\s]?\d{3,}|"
    r"\d+(?:\.\d+){1,3})\b",
    re.I,
)
_DEFECT_SUPPORT_WORDS = ("critical", "bug", "defect", "issue", "unusable",
                         "inoperable", "nonfunctional", "cannot be used")


def _domain_is(domain: str, candidates: set[str]) -> bool:
    domain = (domain or "").lower().split(":")[0]
    return any(domain == item or domain.endswith("." + item)
               for item in candidates)


def _other_vendor_scope_problems(draft: Draft, kb: QaKnowledge,
                                 outline: list[str] | None) -> list[tuple[str, int]]:
    """Repeated/structural competitor coverage not commissioned by the brief.

    One incidental interoperability mention is allowed. Two mentions, or one
    in a heading/table/FAQ question, is substantive editorial scope and must
    have been named in the title, topic or outline first. This catches an
    entire competitor-integration section without blocking a factual sentence
    such as a supported-cloud example.
    """
    if not kb.vendor_name or not kb.canonical_names:
        return []
    faq_questions = "\n".join(
        str(item.get("question") or item.get("q") or "")
        for item in (draft.faq or []) if isinstance(item, dict))
    faq_all = "\n".join(
        str(item.get("question") or item.get("q") or "") + " "
        + str(item.get("answer") or item.get("a") or "")
        for item in (draft.faq or []) if isinstance(item, dict))
    text = "\n".join((draft.body_markdown or "", faq_all))
    structural = "\n".join(
        line for line in (draft.body_markdown or "").splitlines()
        if line.lstrip().startswith(("#", "|"))) + "\n" + faq_questions
    commissioned = "\n".join(
        [draft.title or "", draft.topic or ""] + list(outline or []))
    problems = []
    for vendor in kb.canonical_names:
        if vendor.lower() == kb.vendor_name.lower():
            continue
        if (kb.vendor_name.lower() == "idira"
                and vendor.lower() == "palo alto networks"):
            continue
        pattern = re.compile(
            rf"(?<![A-Za-z0-9]){re.escape(vendor)}(?![A-Za-z0-9])", re.I)
        if pattern.search(commissioned):
            continue
        count = len(pattern.findall(text))
        if count >= 2 or (count and pattern.search(structural)):
            problems.append((vendor, count))
    return problems


# A citation tag doing a WORD's job. Three shapes, all of them shipped:
#
#   "According to [S1], the exam is 120 minutes."  publishes as
#       "According to, the exam is 120 minutes."
#   "[S1] states that the exam is 120 minutes."    publishes as
#       "states that the exam is 120 minutes."
#   "[S1](https://vendor.example/page)"            publishes as a LINK
#       labelled S1 — webapp/render.py's stripper only knew the bracket form,
#       so this one survived to a pushed WordPress draft (post 182934).
#
# The prompt asks for "according to <source name> [S2]" and the model kept the
# shape while dropping the name, which is the part that carries the meaning.
#
# Bracketed forms ONLY. A bare "S3" is Amazon S3 far more often than it is
# source 3 — one article says "Objects for S3-compatible storage" — and a
# check that cannot tell them apart would fail an article for naming a
# product.
_TAG_RE = r"\[\s*S\d+(?:\s*(?:,|;|and)\s*S\d+)*\s*\]"
_CITATION_AS_LINK_RE = re.compile(_TAG_RE + r"\(")
_CITATION_LEADIN_RE = re.compile(
    r"\b(?:according to|per|as per|in|from|by|see|citing|cited in|"
    r"detailed in|outlined in|described in|noted in|shown in|stated in|"
    r"reported in|reported by|source:?)\s+" + _TAG_RE, re.I)
_CITATION_SUBJECT_RE = re.compile(
    _TAG_RE + r"\s+(?:states?|says?|notes?|reports?|confirms?|shows?|"
    r"describes?|explains?|recommends?)\b", re.I)


def _citation_as_prose(text: str) -> list[str]:
    """Fragments where a [S#] tag is standing in for words of its own."""
    found: list[str] = []
    for pattern in (_CITATION_AS_LINK_RE, _CITATION_LEADIN_RE,
                    _CITATION_SUBJECT_RE):
        for m in pattern.finditer(text or ""):
            start = max(0, m.start() - 40)
            found.append(re.sub(r"\s+", " ", text[start:m.end() + 40]).strip())
    return list(dict.fromkeys(found))


def _severe_defect_problems(body: str, references, bundle: VerifiedBundle,
                            kb: QaKnowledge) -> list[str]:
    """Absolute product-defect claims need exact first-party evidence."""
    if not kb.vendor_name or not kb.vendor_domains:
        return []
    refs = {r.get("tag", ""): r for r in _ref_dicts(references)}
    sources = {s.url: s for s in bundle.sources}
    problems = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", body or ""):
        sentence = sentence.strip()
        if not sentence or not _SEVERE_DEFECT_RE.search(sentence):
            continue
        precise = _PRECISE_DEFECT_TOKEN_RE.findall(sentence)
        tags = set(re.findall(r"S\d+", sentence))
        own_refs = [refs[tag] for tag in tags if tag in refs and
                    _domain_is(refs[tag].get("domain", ""), kb.vendor_domains)]
        if not precise:
            problems.append(
                f'no precise version, CVE or vendor advisory ID: "{sentence[:105]}"')
            continue
        if not own_refs:
            problems.append(
                f'no same-sentence source from {kb.vendor_name}: "{sentence[:105]}"')
            continue
        severity = [word for word in _DEFECT_SUPPORT_WORDS
                    if word in sentence.lower()]
        supported = False
        for ref in own_refs:
            source = sources.get(ref.get("url", ""))
            source_text = (source.text if source else "").lower()
            if (all(token.lower() in source_text for token in precise)
                    and all(word in source_text for word in severity)):
                supported = True
                break
        if not supported:
            problems.append(
                f'cited first-party source does not support the exact scope and impact: '
                f'"{sentence[:105]}"')
    return problems


def _product_scope_problems(body: str, vendor_name: str | None) -> list[str]:
    """Known management-scope conflations with deterministic wording."""
    if vendor_name != "F5":
        return []
    problems = []
    for paragraph in re.split(r"\n\s*\n", body or ""):
        plain = re.sub(r"^#{1,6}\s*", "", paragraph.strip())
        if (re.search(r"\bWeb App Security (?:Manager|Deployer)\b", plain, re.I)
                and not re.search(r"\bBIG-IQ\b", plain, re.I)):
            problems.append(
                "Web App Security Manager/Deployer roles are BIG-IQ roles; name "
                "BIG-IQ Centralized Management in the same paragraph")
        if (re.search(r"\bGuided Configuration\b", plain, re.I)
                and re.search(r"\b(?:relies on|depends on|requires|must use|"
                              r"only way|essential)\b", plain, re.I)
                and not re.search(r"\b(?:does not|doesn't|not)\s+(?:require|"
                                  r"required|essential)\b", plain, re.I)):
            problems.append(
                "Guided Configuration is presented as mandatory; describe it as "
                "one structured workflow that can be used where appropriate")
    return problems


def _editorial_fact_problems(body: str, today: date | None = None) -> list[str]:
    """Claims that need exact, same-sentence support or current dates."""
    today = today or date.today()
    problems: list[str] = []
    lines = (body or "").splitlines()
    table_headers = {
        i for i in range(len(lines) - 1)
        if lines[i].lstrip().startswith("|")
        and re.match(r"^\s*\|?\s*:?-{3}", lines[i + 1])
    }
    claim_text = "\n".join(
        "" if i in table_headers
        or re.match(r"^\s*\|?\s*:?-{3}", line)
        or line.lstrip().startswith("#")
        else line
        for i, line in enumerate(lines))
    citation_re = re.compile(r"\[S\d+(?:\s*,\s*S\d+)*\]")
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", claim_text)
                 if s.strip()]
    for sentence in sentences:
        if _SENSITIVE_FACT_RE.search(sentence) and not citation_re.search(sentence):
            problems.append(
                f'unsupported relationship/logistics claim: "{sentence[:110]}"')

        if not _PROMOTION_RE.search(sentence):
            continue
        if re.search(r"\b(?:ended|expired|was available|historical)\b", sentence,
                     re.I):
            continue
        parsed = None
        for pattern in _FULL_DATE_PATTERNS:
            match = pattern.search(sentence)
            if not match:
                continue
            raw = match.group(1).replace(",", "")
            for fmt in ("%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y"):
                try:
                    parsed = datetime.strptime(raw, fmt).date()
                    break
                except ValueError:
                    pass
            if parsed:
                break
        if parsed is None:
            problems.append(
                f'promotion has no verified full future expiry date: "{sentence[:110]}"')
        elif parsed <= today:
            problems.append(
                f'promotion expired on {parsed.strftime("%d %B %Y")}: '
                f'"{sentence[:110]}"')
        elif not citation_re.search(sentence):
            problems.append(
                f'future promotion has no same-sentence citation: "{sentence[:110]}"')
    return problems


@dataclass(frozen=True)
class QaProfile:
    """Which standard a draft is being held to.

    Article Studio and Quick draft are different products. Studio produces
    governed, vendor-tied, publishable articles; Quick draft says on its own
    page that it takes any topic, saves nothing, and needs no vendor or brief.
    Holding the second to the first's standard produced two failures that were
    not defects: a 1300-word floor on "a draft in seconds", and — on an
    article about cycling — "no inline Red Education course link".

    Only the rules that genuinely differ live here. Grounding, originality,
    citation integrity, spelling, attribution and heading sanity are the same
    question whatever the subject, and are deliberately NOT configurable: a
    profile that could switch those off would be a way to publish unchecked
    prose, which is the opposite of the point.

    The course CTA is NOT a profile setting either, though it started as one.
    Whether an article should advertise a course is a question about the
    SUBJECT, not about which page produced it — a cyber topic typed into
    Quick draft should carry one, and a cycling topic should not, so it is
    decided per bundle by VerifiedBundle.is_our_subject instead.
    """
    name: str
    min_words: int
    target_words: tuple[int, int]


# The governed default. Every existing caller gets exactly this, unchanged.
STUDIO_PROFILE = QaProfile("studio", min_words=1300, target_words=(1500, 2100))

# Quick draft. The floor is lower because the page promises a draft in
# seconds, not a finished article; it is a floor, not an aspiration, and a
# 400-word draft is still reported as short.
QUICK_PROFILE = QaProfile("quick", min_words=600, target_words=(900, 2100))


MAX_HEADING_CHARS = 100


def split_overlong_headings(content: str, outline: list[str] | None) -> str:
    """Put back the newline the model left out.

    A heading that swallowed its paragraph is not a judgement call when the
    brief's outline names the heading: the outline item is the heading, the
    remainder is the paragraph, and the repair is exactly the blank line that
    should have been there. Measured on the article that prompted this, the
    model was asked three times to make that split and returned the same
    500-character heading with "provide" changed to "provides" — it is not
    work a language model should be doing.

    Only fires where the outline names the heading. Without that anchor, where
    to cut IS a judgement, and a wrong guess invents a heading — which is
    worse than leaving the finding for a human.
    """
    if not outline:
        return content

    def repair(m: "re.Match") -> str:
        hashes, text = m.group(1), m.group(2).strip()
        if len(text) <= MAX_HEADING_CHARS:
            return m.group(0)
        item = _outline_for_heading(text, outline)
        if not item:
            return m.group(0)
        rest = text[len(item):].lstrip(" .,;:—–-")
        if not rest:
            return m.group(0)
        return f"{hashes} {item}\n\n{rest}"

    return re.sub(r"^(#{1,6})\s*(.+)$", repair, content, flags=re.M)


def _outline_for_heading(heading: str, outline: list[str] | None) -> str | None:
    """The brief's outline item this over-long heading grew out of.

    The model does not invent a 500-character heading from nothing — it takes
    the outline item it was given and keeps writing. So the outline item is
    almost always a prefix of the wreckage, and naming it turns "cut this
    down" into "use exactly this"."""
    h = re.sub(r"\s+", " ", (heading or "")).strip().lower()
    best = None
    for item in outline or []:
        norm = re.sub(r"\s+", " ", (item or "")).strip()
        if len(norm) < 8:
            continue
        if h.startswith(norm.lower()) and (best is None or len(norm) > len(best)):
            best = norm
    return best


def _slug_key(url: str) -> str:
    """A URL's identity ignoring separators.

    Exists to spot a NEAR MISS. The writer produced
    ".../palo-alto-networks/certification-restructure-2026/" where the brief
    asked for ".../palo-alto-networks-certification-restructure-2026/" — one
    character, a slash where a hyphen belongs. Two checks then fired about the
    same link from opposite ends: internal_links said the requested URL was
    missing, internal_link_targets said the one in the body was invented. Both
    true, and together they read as a contradiction rather than as "you typed
    it slightly wrong".
    """
    return re.sub(r"[^a-z0-9]", "", (url or "").lower())


def _norm_words(phrases: list[str]) -> set[str]:
    """Lowercased word set across the requested keywords — the title's own
    keyword words are excluded from the subject check because keyword_coverage
    already tests those, and double-reporting one miss as two failures reads
    as two separate problems."""
    out: set[str] = set()
    for p in phrases:
        out.update(re.findall(r"[a-z0-9]+", (p or "").lower()))
    return out

def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


def _stat_tokens(text: str) -> list[str]:
    out = []
    for rx in (_PCT, _CUR, _CVE):
        # rstrip trailing punctuation so "$400," matches the source's "$400"
        # (otherwise a legitimately-sourced figure looks hallucinated).
        out += [m.group(0).rstrip(".,") for m in rx.finditer(text)]
    return out


def _words(text: str) -> list[str]:
    # strip markdown, lowercase, keep %/- inside tokens
    # _TAG_RE, so a combined "[S4, S5]" is stripped too. Left whole, its text
    # tokenised into "s4"/"s5" and joined the word stream the originality
    # comparison runs over, which can break a 10-word run that really is copied.
    text = re.sub(_TAG_RE, " ", text)
    text = re.sub(r"[#*_>`|]", " ", text)
    text = re.sub(r"\(https?://[^)]+\)", " ", text)   # drop link targets
    return re.findall(r"[a-z0-9%\-$]+", text.lower())


def _containing_sentence(body: str, run_words: list[str]) -> str:
    """The full sentence in `body` that holds this copied word run.

    The run itself comes from _words()-normalised tokens, so it is usually
    NOT a literal substring of the body (markdown, citations and punctuation
    were stripped) — which made it unquotable back to the model. Naming only
    the run left the writer unable to locate it: handed a 10-word fragment
    buried in an FAQ answer, it returned the offending sentence
    byte-identical on every retry. Matching sentence-by-sentence on
    normalised words finds it regardless of markup. Empty string if the
    sentence can't be pinned down — the caller just omits it then."""
    if not run_words:
        return ""
    needle = " ".join(run_words)
    for raw in re.split(r"(?<=[.!?])\s+|\n+", body):
        sentence = raw.strip()
        if sentence and needle in " ".join(_words(sentence)):
            return " ".join(sentence.split())
    return ""


def _copied_sentences(body: str, body_words: list[str],
                      source_word_lists: list[list[str]]) -> list[str]:
    """Every sentence of `body` containing a run of VERBATIM_RUN_LIMIT+ words
    shared with any source, most-copied first.

    Replaces a per-source longest-common-run scan that reported only the
    single WORST run. That turned a retry into whack-a-mole: the model
    rewrote the one sentence it was given, the next-longest copied run took
    its place, and with a 3-attempt budget the draft could never converge —
    verified live, one fix at a time, on the same article. Naming every
    offending sentence lets one retry clear them all.

    Same gate as before, by construction: a shared run of >= LIMIT words
    contains a shared LIMIT-gram, and a shared LIMIT-gram IS a shared run of
    >= LIMIT words, so the pass/fail verdict is identical. It is also far
    cheaper — hashing n-grams is linear in the source text, where the old
    LCS was O(body x source) per source and spent real time on the oversized
    PDFs retrieval sometimes returns."""
    n = VERBATIM_RUN_LIMIT
    if len(body_words) < n:
        return []
    grams = set()
    for words in source_word_lists:
        for i in range(len(words) - n + 1):
            grams.add(" ".join(words[i:i + n]))
    if not grams:
        return []

    hits: list[tuple[int, str]] = []
    seen: set[str] = set()
    for raw in re.split(r"(?<=[.!?])\s+|\n+", body):
        sentence = raw.strip()
        if not sentence:
            continue
        sw = _words(sentence)
        if len(sw) < n:
            continue
        copied = sum(1 for i in range(len(sw) - n + 1)
                     if " ".join(sw[i:i + n]) in grams)
        if copied:
            clean = " ".join(sentence.split())
            if clean not in seen:
                seen.add(clean)
                hits.append((copied, clean))
    hits.sort(key=lambda h: -h[0])
    return [s for _c, s in hits]


def _longest_common_run(a: list[str], b: list[str]) -> tuple[int, int]:
    """Longest common contiguous run of words. Returns (length, a_start_index)."""
    if not a or not b:
        return 0, 0
    prev = [0] * (len(b) + 1)
    best, best_i = 0, 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best, best_i = cur[j], i - cur[j]
        prev = cur
    return best, best_i


# --------------------------------------------------------------------------- #
def validate_draft(draft: Draft, bundle: VerifiedBundle,
                   keywords: list[str] | None = None,
                   serp=None,
                   entities: list[str] | None = None,
                   internal_links: list[str] | None = None,
                   allowed_link_urls: set[str] | None = None,
                   acronyms: set[str] | None = None,
                   seo_title: str | None = None,
                   kb: QaKnowledge | None = None,
                   outline: list[str] | None = None,
                   profile: "QaProfile | None" = None) -> QAReport:
    """`serp` is an optional retrieval.serp.SerpSnapshot, computed ONCE per
    write by the caller (not per QA retry — each snapshot costs a Serper
    query plus competitor-page fetches) and passed through the retry loop.

    `kb` is a QaKnowledge (synthesis/qa_rules.py) carrying everything the QA
    RISK rules need that is not in the draft: vendor name, sourced
    certification versions, registration codes, canonical brand names, vendor
    terms, entity domains, interested-party domains, permitted people, and
    the corpus's existing version claims. It is optional, and every field
    inside it is optional: an empty field switches its rule off rather than
    making it guess. That is deliberate — the knowledge base is still being
    backfilled, and a rule with no data must produce no findings, not false
    ones.

    `entities` (Vendor KB product/cert/course names) and `internal_links`
    (the URLs the brief asked the writer to weave in) power the WARN-only
    semantic-coverage and link-presence checks — both editorial quality
    measures, deliberately never FAIL: a retry burns real quota, and these
    are reviewer-attention items, not integrity violations."""
    r = QAReport()
    body = draft.body_markdown
    kb = kb or QaKnowledge()
    profile = profile or STUDIO_PROFILE

    # 1. structure & length -------------------------------------------------
    # Business target: 1500-2000 words. Below 1300 is a FAIL, not a warn —
    # deliberately, because the retry loop only regenerates (with targeted
    # feedback) on failed checks; when short drafts merely warned, they
    # passed QA on attempt 1 and the length feedback never fired, which is
    # exactly why output sat consistently under target.
    wc = draft.word_count()
    h2 = len(re.findall(r"^##\s", body, re.M))
    lo, hi = profile.target_words
    if not draft.title.strip():
        r.add("structure_length", "fail", "missing title")
    elif wc < profile.min_words:
        r.add("structure_length", "fail",
              f"too short: {wc} words (minimum {profile.min_words}, "
              f"target {lo}-{hi})")
    elif not (lo <= wc <= hi):
        r.add("structure_length", "warn",
              f"{wc} words (target {lo}-{hi}); {h2} H2 sections")
    elif h2 < 3:
        r.add("structure_length", "warn", f"only {h2} H2 sections")
    else:
        r.add("structure_length", "pass", f"{wc} words, {h2} H2 sections")

    # Reports its PASS as well as its warn. Emitting only on failure made this
    # the one check in the harness that vanished when it was fine, so the
    # panel could not distinguish "checked, good" from "never ran" — and the
    # "Passing" filter never listed it at all.
    #
    # The threshold stays at 160 (changing it would newly warn on articles
    # written under the old rule); the advice names the same number rather
    # than the 155 it used to suggest, which left a five-character band where
    # the message contradicted the check.
    _meta_len = len(draft.meta_description)
    if _meta_len > 160:
        r.add("meta_description", "warn",
              f"{_meta_len} chars — keep it to 160 or fewer, or search "
              "engines truncate it mid-sentence")
    elif _meta_len:
        r.add("meta_description", "pass", f"{_meta_len} chars, within 160")
    else:
        r.add("meta_description", "warn", "no meta description")

    # 2. citation integrity -------------------------------------------------
    # Reads combined markers too: "[S4, S5]" cites two sources, and matching a
    # lone [S1] only reported a fully cited draft as "post is unsourced" and
    # blocked it. _TAG_RE is the same shape the reader boundary strips.
    markers = {tag for group in re.findall(_TAG_RE, body)
               for tag in re.findall(r"S\d+", group)}
    ref_tags = {ref.tag for ref in draft.references}
    bundle_urls = {s.url for s in bundle.sources}
    bad_refs = [ref.tag for ref in draft.references if ref.url not in bundle_urls]
    missing = markers - ref_tags
    if not markers:
        r.add("citation_integrity", "fail", "no inline citations — post is unsourced")
    elif missing:
        r.add("citation_integrity", "fail",
              f"citations with no matching reference: {sorted(missing)}")
    elif bad_refs:
        r.add("citation_integrity", "fail",
              f"references not traceable to bundle sources: {bad_refs}")
    else:
        unused = ref_tags - markers
        detail = f"{len(markers)} sources cited"
        if unused:
            detail += f"; unused refs: {sorted(unused)}"
        r.add("citation_integrity", "warn" if unused else "pass", detail)

    # 3. grounding (hallucinated statistics) --------------------------------
    allowed = set()
    for c in bundle.claims:
        allowed |= {_norm(n) for n in c.numbers}
    for s in bundle.sources:
        allowed |= {_norm(t) for t in _stat_tokens(s.text)}
    orphan = [t for t in _stat_tokens(body) if _norm(t) not in allowed]
    if orphan:
        r.add("grounding", "fail",
              f"statistics not found in any source (possible hallucination): {orphan}")
    else:
        r.add("grounding", "pass", "all statistics trace to source material")

    # 4. flagged-claim leakage ----------------------------------------------
    leaks = []
    for c in bundle.claims:
        if c.status != "flagged":
            continue
        for n in c.numbers:
            if _norm(n) in _norm(body):
                leaks.append(n)
    if leaks:
        r.add("flagged_leakage", "fail",
              f"flagged/vendor-only claim data leaked into draft: {leaks}")
    else:
        r.add("flagged_leakage", "pass", "no flagged-claim data present")

    # 5. originality (verbatim copying) -------------------------------------
    body_words = _words(body)
    copied = _copied_sentences(body, body_words,
                               [_words(s.text) for s in bundle.sources])
    if copied:
        shown = copied[:5]
        more = len(copied) - len(shown)
        detail = (f"{len(copied)} sentence(s) share a run of "
                  f"{VERBATIM_RUN_LIMIT}+ words with a source. Replace each of "
                  "these: " + " | ".join(f'"{s}"' for s in shown)
                  + (f" (+{more} more)" if more else ""))
        r.add("originality", "fail", detail)
    else:
        r.add("originality", "pass",
              f"no run of {VERBATIM_RUN_LIMIT}+ words shared with any source")

    # 6. course link + CTA --------------------------------------------------
    # Only when the topic is ours. Requiring a Red Education course link in an
    # article about cycling is not a lowered standard, it is the wrong
    # question — and it FAILED, so the whole draft was held for it. Decided by
    # the same signal that decides whether the WRITER is asked for one, so the
    # prompt and the check can never disagree about whether the article was
    # supposed to have it. Studio bundles are always ours; nothing changes.
    if bundle.is_our_subject():
        cfg = load_course_links()
        has_link = cfg["base_url"] in body
        has_cta = any(p.lower() in body.lower() for p in cfg["cta_phrases"])
        if not has_link:
            r.add("course_link_cta", "fail", "no inline Red Education course link")
        elif not has_cta:
            r.add("course_link_cta", "warn",
                  "course link present but no clear CTA phrase")
        else:
            r.add("course_link_cta", "pass", "course link + CTA present")

    # 7. SEO keyword coverage (only checked if keywords were requested) -----
    if keywords:
        body_lower = body.lower()
        missing = [kw for kw in keywords if kw.lower() not in body_lower]
        if missing:
            r.add("keyword_coverage", "fail",
                  f"missing required keyword(s): {', '.join(missing)}")
        else:
            r.add("keyword_coverage", "pass",
                  f"all {len(keywords)} required keyword(s) present")

    # 8. SERP competitiveness (only when a snapshot was supplied) ------------
    # WARN-only, never FAIL: this measures the outside world, not the draft's
    # own integrity, and a flaky competitor fetch must never block publishing.
    if serp is not None and serp.pages:
        median = serp.median_word_count
        n = len(serp.pages)
        if median and wc < 0.7 * median:
            r.add("serp_competitiveness", "warn",
                  f"draft may be thin for this SERP: {wc} words vs "
                  f"{median}-word median across the top {n} ranking page(s) "
                  f"for \"{serp.keyword}\"")
        else:
            r.add("serp_competitiveness", "pass",
                  f"{wc} words vs {median}-word median across the top {n} "
                  f"ranking page(s)")

    # 9. direct answer (AI-overview extraction pattern) ----------------------
    # First non-empty block before the first H2 should read as a standalone
    # 40-60 word direct answer. Generous band (25-90) — the point is catching
    # throat-clearing one-liners and rambling multi-paragraph openings, not
    # punishing a 65-word answer.
    pre_h2 = body.split("\n## ")[0]
    blocks = [b.strip() for b in pre_h2.split("\n\n")
              if b.strip() and not b.strip().startswith("#")]
    opening_wc = len(blocks[0].split()) if blocks else 0
    if not blocks:
        r.add("direct_answer", "warn", "no opening paragraph before the first H2")
    elif not (25 <= opening_wc <= 90):
        r.add("direct_answer", "warn",
              f"opening paragraph is {opening_wc} words — aim for a standalone "
              "40-60 word direct answer an AI overview could quote")
    else:
        r.add("direct_answer", "pass",
              f"{opening_wc}-word direct-answer opening")

    # 10. entity coverage (semantic SEO from the Vendor KB) ------------------
    if entities:
        body_lower = body.lower()
        mentioned = [e for e in entities if e.lower() in body_lower]
        want = min(3, len(entities))
        if len(mentioned) < want:
            r.add("entity_coverage", "warn",
                  f"only {len(mentioned)} of {len(entities)} related vendor "
                  f"entities mentioned (aim for {want}+): consider weaving in "
                  "specific product/certification/course names")
        else:
            r.add("entity_coverage", "pass",
                  f"{len(mentioned)} of {len(entities)} related entities mentioned")

    # 11b. internal link SANITY -----------------------------------------------
    # Catches the failure mode where the writer, short of real URLs, aims many
    # different anchors at the site root or one vendor page: an anchor reading
    # "65476V Administering Avaya Infinity" whose href was the front page. The
    # links all resolve, so citation_integrity and REACS's internal_linking
    # (which counts DISTINCT targets) both passed while 10 of 14 links were
    # wrong. WARN, not FAIL: a link the editor would swap is a quality problem,
    # not a grounding violation.
    md_links = re.findall(r"\[([^\]]+)\]\(([^)\s]+)\)", body)
    html_links = [(txt, u) for u, txt in
                  re.findall(r"<a\s[^>]*href=[\"']([^\"']+)[\"'][^>]*>([^<]*)</a>", body)]
    internal = [(txt, u) for txt, u in md_links + html_links
                if re.match(r"^(/|https?://(www\.)?rededucation\.com)", u, re.I)]
    if len(internal) > 4:
        r.add("internal_link_density", "warn",
              f"{len(internal)} internal links — keep a focused article to "
              "2-4 earned links unless the brief explicitly requires more")
    elif internal:
        r.add("internal_link_density", "pass",
              f"{len(internal)} internal link(s), within the focused 2-4 range")
    problems = []
    # Absolute homepage AND bare relative root — "](/)" is the same defect and
    # slipped straight through an absolute-only pattern.
    roots = [t2 for t2, u in internal
             if re.fullmatch(r"(https?://(www\.)?rededucation\.com)?/?(#.*)?", u, re.I)]
    if roots:
        problems.append(f"{len(roots)} link(s) point at the site root instead of a "
                        f"real page (e.g. \"{roots[0][:40]}\")")
    by_url = {}
    for txt, u in internal:
        by_url.setdefault(u.rstrip("/").lower(), set()).add(txt.strip().lower())
    reused = {u: a for u, a in by_url.items() if len(a) > 1}
    if reused:
        u0, anchors = next(iter(reused.items()))
        problems.append(f"{len(reused)} URL(s) reused for different anchor texts "
                        f"({len(anchors)} anchors share {u0})")
    # Anchor text naming a specific course whose code is absent from the target
    # URL: "[Avaya 71202V Integrating Avaya Aura](/prepare-for-avaya-
    # certification-success/)" survived the root/reuse rules above because it is
    # neither — but it still sends the reader somewhere the anchor did not
    # promise. A course code is a hard, checkable identity: if the anchor names
    # one, the URL for that course contains it.
    # Same identity check, two ways of naming a course: a numeric code
    # ("65850V", "EDU-210") OR a KB entity's own distinctive name with no code
    # at all ("Panorama", "Cortex XSOAR"). The code-only version missed the
    # second kind outright — an anchor reading "Panorama: Centralized Network
    # Security Administration" was silently pointed at an EDU-210 page, and
    # "Cortex XSOAR 6.8: Automation" at a Cortex XSIAM page, because neither
    # anchor contains a digit for the old regex to find. _entity_alias is the
    # SAME logic reacs.py's entity_coverage uses to recognise a KB name inside
    # prose (acronym-in-parens, code, or head-before-colon) — reused here
    # rather than re-invented, so "what identifies a course" has one
    # definition, not two that can silently disagree.
    from .reacs import _entity_alias

    # An anchor that IS the target page's own title is a correct link by
    # definition — skip the identity check entirely for those. Every false
    # positive this check produced was that shape: a real blog post whose real
    # title happens to name a course code ("EDU-210 to EDU-330: Top Palo Alto
    # Firewall Courses Ranked" -> that post's own URL), flagged because the
    # code isn't in the slug. 3 of 122 correct links measured before this.
    known_titles = {}
    if isinstance(allowed_link_urls, dict):
        known_titles = {_norm_link(k): (v or "").strip().lower()
                        for k, v in allowed_link_urls.items()}

    mismatched = []
    for txt, u in internal:
        if known_titles.get(_norm_link(u)) == txt.strip().lower():
            continue
        identifier = None
        for e in (entities or []):
            alias = _entity_alias(e) or e
            if len(alias) >= 4 and alias.lower() in txt.lower():
                identifier = alias
                break
        if identifier is None:
            codes = re.findall(r"\b(?:[a-z]{2,4}[-\s]?\d{3,5}[a-z]?|\d{4,5}[a-z])\b",
                               txt, re.I)
            identifier = codes[0] if codes else None
        if identifier is None:
            continue
        # Strip everything but letters/digits on BOTH sides — not just hyphens
        # and spaces. A real KB course can be named "Administering BIG-IP &
        # Configuring BIG-IP Local Traffic Manager"; squashing only "-\s" left
        # the "&" in the identifier, which can never appear in a URL slug, so
        # a CORRECTLY linked course with punctuation in its name was always
        # flagged as wrong. Both sides now reduce to bare alphanumerics.
        slug = re.sub(r"[^a-z0-9]", "", u.lower())
        if re.sub(r"[^a-z0-9]", "", identifier.lower()) not in slug:
            mismatched.append((txt.strip(), identifier))
    if mismatched:
        txt0, id0 = mismatched[0]
        problems.append(f"{len(mismatched)} link(s) whose anchor names a specific "
                        f"course but whose URL is for something else "
                        f"(e.g. \"{txt0[:44]}\" -> no \"{id0}\" in the target)")

    # Links whose subject the article never actually discusses --------------
    # A firewall-troubleshooting article carried links to Cortex XSIAM, Cortex
    # XDR and Panorama. Every one passed internal_link_targets, because that
    # check asks whether a link points where its anchor says — a question
    # about correctness, not relevance. Measured on that article: XSIAM
    # appeared 4 times and XDR twice, NOT ONCE outside a link anchor, while
    # EDU-330 appeared 5 times in the prose and "packet" 19 times.
    #
    # A link whose subject is never named in the surrounding prose was
    # inserted, not earned. Reuses _entity_alias — the same definition of
    # "what identifies a course" the mismatch check above uses — so the two
    # cannot disagree about which entity an anchor names.
    prose_only = re.sub(r"\[[^\]]*\]\([^)\s]*\)", " ", body).lower()
    orphan_links = []
    for txt, _u in internal:
        for e in (entities or []):
            alias = _entity_alias(e) or e
            if len(alias) >= 4 and alias.lower() in txt.lower():
                if alias.lower() not in prose_only:
                    orphan_links.append((txt.strip(), alias))
                break
    if orphan_links:
        txt0, alias0 = orphan_links[0]
        r.add("link_relevance", "warn",
              f"{len(orphan_links)} internal link(s) to something the article "
              f"never discusses: \"{txt0[:44]}\" links to {alias0}, which is "
              "not mentioned anywhere outside that link. A link the prose has "
              "not earned reads as an advert. Either write about it, or drop "
              "the link.")
    elif internal:
        r.add("link_relevance", "pass",
              "every internal link points at something the article discusses")

    # A link to another vendor is acceptable in a real comparison, where that
    # vendor is discussed in prose. It is not acceptable as an unexplained
    # recommendation inserted into a single-vendor article.
    cross_vendor = []
    for txt, url in internal:
        for vendor_name in kb.canonical_names:
            if vendor_name.lower() == (kb.vendor_name or "").lower():
                continue
            if ((kb.vendor_name or "").lower() == "idira"
                    and vendor_name.lower() == "palo alto networks"):
                continue
            slug = re.sub(r"[^a-z0-9]+", "-", vendor_name.lower()).strip("-")
            named_anchor = re.search(rf"\b{re.escape(vendor_name)}\b", txt, re.I)
            named_url = bool(slug and re.search(
                rf"(?:/|-){re.escape(slug)}(?:/|-)", url.lower()))
            if (named_anchor or named_url) and not re.search(
                    rf"\b{re.escape(vendor_name.lower())}\b", prose_only):
                cross_vendor.append((txt.strip(), vendor_name))
                break
    if cross_vendor:
        txt0, vendor0 = cross_vendor[0]
        r.add("cross_vendor_links", "fail",
              f"{len(cross_vendor)} internal link(s) introduce another vendor "
              f"that the article never discusses (e.g. \"{txt0[:48]}\" names "
              f"{vendor0}). Remove it, or make the comparison substantive in "
              "the prose rather than using another vendor as a generic CTA.")
    elif internal and kb.canonical_names:
        r.add("cross_vendor_links", "pass",
              "no unexplained links to another vendor")

    # The previous check only protects LINK recommendations. Article 11 was
    # about F5 yet devoted an H2, a table row and an FAQ to Check Point TEM;
    # because that competitor was discussed in prose, cross_vendor_links
    # considered it explained. Scope is a separate question: a single-vendor
    # brief cannot quietly become a comparison during synthesis.
    scope_problems = _other_vendor_scope_problems(draft, kb, outline)
    if scope_problems:
        vendor0, count0 = scope_problems[0]
        r.add("vendor_scope", "fail",
              f"{len(scope_problems)} other vendor(s) receive substantive "
              f"coverage without being commissioned in the title or brief "
              f"({vendor0}: {count0} mention(s)). Remove that material, or "
              "make the comparison explicit in the brief before writing.")
    elif kb.vendor_name and kb.canonical_names:
        r.add("vendor_scope", "pass",
              "no uncommissioned competitor section or repeated recommendation")

    # Targets that are not KNOWN-REAL pages. An HTTP check cannot substitute
    # for this: rededucation.com serves 200 for ANY slug under /course/, so a
    # fabricated "/course/f5-configuring-big-ip-local-traffic-manager/" looks
    # alive to curl while being a soft-404. Only membership of the scraped
    # PublishedPost set (plus the configured vendor/generic pages) proves a URL
    # exists, which is why the caller passes that set in.
    unknown: list[tuple[str, str]] = []
    if allowed_link_urls:
        allowed = {_norm_link(u) for u in allowed_link_urls}
        unknown = [(txt, u) for txt, u in internal if _norm_link(u) not in allowed]
        if unknown:
            t0, u0 = unknown[0]
            problems.append(
                f"{len(unknown)} link(s) point at a URL that is not a known page "
                f"(e.g. \"{t0[:34]}\" -> {u0}) — invented, even if it returns 200")

    if internal:
        if problems:
            # Name the near miss where there is one, so this and the
            # internal_links finding below stop reading as two problems.
            #
            # BOTH sides have to be real findings, or the hint invents one.
            # Only a target this check just called invented, matched against a
            # link internal_links will actually report missing. Comparing
            # every body URL to every requested one instead — and over by_url,
            # whose keys have had their trailing slash stripped — told the
            # editor a correctly written link was "mistyped" and promised to
            # clear an internal_links finding that was never raised. Article 5
            # got that on a prisma-access URL it had linked exactly right.
            missing_by_key = {_slug_key(u): u for u in (internal_links or [])
                              if u not in body}
            hints, seen = [], set()
            for _txt, url in unknown:
                near = missing_by_key.get(_slug_key(url))
                # _norm_link, not !=: a bare trailing-slash difference is the
                # same page, and internal_links already explains that case
                # from its own side.
                if not near or _norm_link(near) == _norm_link(url):
                    continue
                if url in seen:
                    continue
                seen.add(url)
                hints.append(
                    f'"{url}" looks like a mistyped "{near}", which the '
                    "brief asked for — the two differ only in "
                    "punctuation, so fixing the URL clears the "
                    "internal_links finding as well")
            r.add("internal_link_targets", "warn",
                  "; ".join(problems + hints))
        else:
            r.add("internal_link_targets", "pass",
                  f"{len(by_url)} distinct internal target(s), none generic")

    # 11. requested internal links present -----------------------------------
    if internal_links:
        missing = [u for u in internal_links if u not in body]
        if missing:
            body_keys = {_slug_key(u): u for u in
                         set(re.findall(r"\]\((https?://[^)\s]+)\)", body))}
            detail = (f"{len(missing)} requested internal link(s) missing from "
                      f"the body: {missing[:3]}")
            near = [(m, body_keys[_slug_key(m)]) for m in missing
                    if _slug_key(m) in body_keys
                    and body_keys[_slug_key(m)] != m]
            if near:
                # The link IS there, spelled wrong. Saying "missing" alone sent
                # the editor hunting for something already in front of them.
                detail += ("; but the body has " + ", ".join(
                    f'"{got}" — almost the requested "{want}", differing only '
                    "in punctuation. Correct the URL rather than adding a "
                    "second link" for want, got in near))
            r.add("internal_links", "warn", detail)
        else:
            r.add("internal_links", "pass",
                  f"all {len(internal_links)} requested internal link(s) present")

    # 12. the article must actually be about its own title -------------------
    # Nothing here previously compared the body to the SUBJECT the title
    # promises. An article headlined "How Check Point Acquisitions Are
    # Reshaping Security Automation Workflows" shipped as a publish candidate
    # containing not one word about an acquisition — every other check passed,
    # because each asks whether the prose is sound, sourced and on-keyword,
    # and none asks whether it is on-TOPIC. Grounding and citation_integrity
    # cannot catch it either: generic automation prose is perfectly well
    # sourced, just not about the thing promised.
    #
    # WARN, not FAIL: the title's wording is an editorial choice and a term
    # can be legitimately expressed as a synonym, so this flags for a human
    # rather than blocking. A term is "covered" if it appears anywhere in the
    # body, stem-tolerantly ("acquisition" covers "acquisitions").
    subject_terms = distinctive_terms(draft.title, exclude=_norm_words(keywords or []))
    if subject_terms:
        body_lower = body.lower()
        uncovered = [t for t in subject_terms
                     if t not in body_lower and t.rstrip("s") not in body_lower]
        if uncovered:
            r.add("title_subject_coverage", "warn",
                  "the title promises subject matter the article never "
                  f"mentions: {', '.join(repr(t) for t in uncovered)} — either "
                  "cover it or retitle the article")
        else:
            r.add("title_subject_coverage", "pass",
                  f"all {len(subject_terms)} subject term(s) from the title "
                  "appear in the body")

    # 13. writing mechanics --------------------------------------------------
    # Casing of certification acronyms and release tokens kept reaching
    # published drafts: "ccte r82 certification" where the Vendor KB plainly
    # holds "CCTE". Nothing checked it, because apply_acronym_case() was only
    # ever applied to scraped titles and link anchors, never to the article
    # body.
    #
    # parse_article_response() now repairs these deterministically at the
    # single point every write path funnels through, so in normal operation
    # this check passes. It stays as a FAIL rather than a WARN precisely
    # because of that: the fixer runs first, so anything still here is a class
    # of error the fixer does not know about, or a bug in it. Either way it
    # should stop the article, not be filed as a reviewer suggestion.
    #
    # Only mechanical, decidable errors — casing against a known list,
    # repeated words, space before punctuation. NOT general grammar or
    # spelling: that needs a language model, and this harness is deterministic
    # by design so its verdict never depends on a model's mood or on quota.
    mech = writing_mechanics_issues(body, acronyms, kb.vendor_terms)
    if mech:
        r.add("writing_mechanics", "fail",
              f"{len(mech)} mechanical writing error(s): "
              + "; ".join(mech[:4])
              + (f" (+{len(mech) - 4} more)" if len(mech) > 4 else ""))
    else:
        r.add("writing_mechanics", "pass",
              "no acronym-casing, repeated-word or spacing errors")
    # 14. Rank Math title readability ----------------------------------------
    # Rank Math scores the SEO TITLE, not the H1, and runs power word and
    # sentiment word as SEPARATE checks. The brief prompt used to say "one is
    # enough", so every article shipped with exactly the 2 title errors seen
    # in Rank Math. Checked here because Rank Math only reports it after the
    # post reaches WordPress — by which point the article is written, QA'd and
    # pushed.
    #
    # Advisory: this is title polish an editor can fix in seconds, and it has
    # no bearing on whether the article is true or sourced. Same reasoning as
    # meta_description. The prompt now asks for all four, so a WARN here means
    # the model dropped one, not that the rule is unstated.
    if seo_title:
        title_issues = title_readability_issues(seo_title, (keywords or [""])[0])
        # NAME THE STRING IT JUDGED. This check reads meta_title, which is a
        # separate field from the article heading and usually shorter — a
        # heading can carry a power word the SEO title does not. Reporting
        # only the verdict meant a reviewer looked at an H1 containing
        # "Complete" and "Valuable", read "no sentiment word", and reasonably
        # concluded the check was broken. It took a database query to settle.
        # The titles are capped at 60 characters, so quoting one costs little.
        n = len(title_issues)
        if title_issues:
            r.add("title_readability", "warn",
                  f'SEO title "{seo_title}" fails {n} Rank Math '
                  f"check{'' if n == 1 else 's'}: " + "; ".join(title_issues)
                  + ". This is the meta title, not the article heading.")
        else:
            r.add("title_readability", "pass",
                  f'SEO title "{seo_title}" passes all four Rank Math checks')

    # 15. V1 — unverified vendor version strings -----------------------------
    # BLOCKING as of 25 Aug 2026. The standard rates V1 critical, and a `fail`
    # already stops publication regardless of score, which is what
    # QA_CRITICAL_MAX = 0 means — no new gate logic was needed.
    #
    # Promoted once Check Point was fully backfilled from the vendor's own
    # pages (12 of 12 exam codes accounted for) and the duplicate exam codes
    # were split. The two-week warn-mode soak the pack asks for was waived by
    # decision; if false positives show up, they will show up as blocked
    # publishes rather than as warnings someone reads later.
    #
    # THE ESCAPE HATCH MATTERS MORE NOW THAT THIS BLOCKS: a version the
    # knowledge base does not hold still passes if a verbatim quote in the
    # article's own research contains it. So genuinely new releases — a Check
    # Point R83 that ships next month — do not block a properly sourced
    # article. They block an unsourced one, which is the point.
    #
    # The root cause this exists for: the model reasons "Check Point ships
    # R82, therefore the CCME tests R82". It does not. A version is verified
    # only against the certifications THIS article is about, or by a verbatim
    # quote in the research it was written from.
    #
    # THREE CASES, and keeping them apart is the whole design. An earlier
    # version collapsed the last two and silently passed 11 of Check Point's
    # 14 certifications:
    #   (a) the article names a certification we HAVE versions for
    #       -> verify against exactly those. The real check.
    #   (b) it names a certification we have NO versions for
    #       -> we cannot verify. Say so. Passing here reproduces the exact
    #          vendor-level inference the rule exists to refuse: "CCSA tests
    #          R82" would clear simply because CCSE does.
    #   (c) it names no certification at all
    #       -> this is a product-release statement ("Check Point ships R82"),
    #          not a certification claim. Check it against the vendor's
    #          recorded releases and do not manufacture a finding: failing
    #          product prose would make the rule noise, and noise is how a
    #          rule gets switched off.
    tokens = extract_version_tokens(body, kb.vendor_name)
    known_codes = {c.upper() for c in
                   (kb.all_cert_codes or list(kb.cert_versions))}
    named = _subject_certifications(draft.title, body,
                                    {c: [] for c in known_codes})
    covered = [c for c in named if kb.cert_versions.get(c)]
    uncovered = [c for c in named if not kb.cert_versions.get(c)]

    if tokens and (kb.cert_versions or uncovered):
        quotes = [c.text.upper() for c in bundle.claims]

        def quoted(tok: str) -> bool:
            return any(tok in q for q in quotes)

        if covered:                                              # case (a)
            allowed = {v.upper() for c in covered
                       for v in kb.cert_versions.get(c, [])}
            unverified = {t: n for t, n in tokens.items()
                          if t not in allowed and not quoted(t)}
            if unverified:
                r.add("version_strings", "fail",
                      "unverified vendor version(s): "
                      + ", ".join(f"{t} (x{n})" for t, n in sorted(unverified.items()))
                      + f" — not recorded for {', '.join(sorted(covered))}, and "
                      "not backed by a verbatim source quote. Confirm against "
                      "the Vendor Knowledge Base or drop the version from the "
                      "sentence.")
            else:
                r.add("version_strings", "pass",
                      f"{len(tokens)} version token(s) verified against "
                      + ", ".join(sorted(covered)))
        elif uncovered:                                          # case (b)
            unquoted = {t: n for t, n in tokens.items() if not quoted(t)}
            if unquoted:
                r.add("version_strings", "fail",
                      "cannot verify version(s) "
                      + ", ".join(f"{t} (x{n})" for t, n in sorted(unquoted.items()))
                      + f": no versions are recorded for "
                      + ", ".join(sorted(uncovered))
                      + ". A certification does NOT automatically track its "
                      "vendor's latest release, so this cannot be cleared by "
                      "the vendor's other exams. Add the version to the Vendor "
                      "Knowledge Base with a source, quote a primary source "
                      "verbatim, or write the sentence without a version.")
            else:
                r.add("version_strings", "pass",
                      "version token(s) backed by a verbatim source quote")
        else:                                                    # case (c)
            allowed = {v.upper() for vs in kb.cert_versions.values() for v in vs}
            unverified = {t: n for t, n in tokens.items()
                          if t not in allowed and not quoted(t)}
            if unverified:
                r.add("version_strings", "fail",
                      "unverified vendor release(s): "
                      + ", ".join(f"{t} (x{n})" for t, n in sorted(unverified.items()))
                      + f" — not a release recorded for any {kb.vendor_name} "
                      "certification, and not backed by a verbatim source "
                      "quote. No certification was named, so this was read as "
                      "a product statement.")
            else:
                r.add("version_strings", "pass",
                      f"{len(tokens)} version token(s) match a recorded "
                      f"{kb.vendor_name} release; no certification claimed")

    # 16. V4 — heading promises a quantity, section delivers none ------------
    # Advisory by design: the fix is editorial (add the number, or retitle the
    # heading), and a section can legitimately explain that a figure is not
    # published. Proved by a live draft whose "Exam Domains and Technical
    # Weighting" section contained no percentage at all.
    gaps = []
    sections = re.split(r"^##\s+(.+)$", body, flags=re.M)
    for i in range(1, len(sections) - 1, 2):
        heading, content = sections[i].strip(), sections[i + 1]
        hit = next((w for w in PROMISE_WORDS if w in heading.lower()), None)
        if hit and not PROMISE_WORDS[hit].search(_RELEASE_TOKEN_RE.sub(" ", content)):
            gaps.append((heading, hit))
    if gaps:
        r.add("promise_gaps", "warn",
              f"{len(gaps)} heading(s) promise a figure the section never gives: "
              + "; ".join(f'"{h}" (promises "{w}")' for h, w in gaps[:3])
              + ". Add the actual figure, or retitle the heading to what the "
                "section really delivers. A release number is not a figure.")
    else:
        r.add("promise_gaps", "pass",
              "every heading promising a figure delivers one")

    # 16b. table columns that promise a figure and give none -----------------
    # The same rule as V4, applied where it is most misleading. A table renders
    # as reference data — clean rules, aligned columns, an authoritative look —
    # whether or not any of its cells carry a checkable fact. This one shipped
    # under the heading "Panorama Certification and Exam Logistics":
    #
    #   | Certification/Course | Focus Area           | Logistics              |
    #   | EDU-210              | Firewall Essentials  | Single-device config   |
    #   | Panorama Training    | Multi-device Orch.   | Enterprise-wide policy |
    #   | Certification Exam   | Device Groups        | Proctored evaluation   |
    #
    # No exam code, no duration, no question count, no fee — nothing a reader
    # could book against. Every other check passed it, because a table with
    # rows is a table with rows.
    table_gaps = []
    for tbl in _tables(body):
        header, rows = tbl[0], tbl[1:]
        for i, head in enumerate(header):
            word = next((w for w in PROMISE_WORDS if w in head.lower()), None)
            if not word:
                continue
            column = [row[i] for row in rows if i < len(row) and row[i]]
            if not column:
                continue
            joined = _RELEASE_TOKEN_RE.sub(" ", " ".join(column))
            if not PROMISE_WORDS[word].search(joined):
                table_gaps.append((head, word, column[:2]))
    if table_gaps:
        head, word, sample = table_gaps[0]
        r.add("table_substance", "warn",
              f"{len(table_gaps)} table column(s) promise a figure and give "
              f"none: \"{head}\" contains "
              + ", ".join(f'"{c}"' for c in sample)
              + ". A table reads as reference data whether or not it holds "
                "any — fill the column with the actual figures, or drop it and "
                "retitle the table to what it really shows.")
    elif _tables(body):
        r.add("table_substance", "pass",
              "table columns promising a figure deliver one")

    # 17. V9 — exact-match keyword repetition ceiling ------------------------
    # Advisory. Counts the PRIMARY keyword only, matching the standard.
    # Note the pipeline used to instruct the writer to do the opposite —
    # synthesis/prompt.py asked for "roughly 10-14 times" — so this check and
    # that instruction have to stay in step or the writer is being graded
    # against a target it was told to miss.
    if keywords and keywords[0]:
        primary = keywords[0]
        hits = len(re.findall(re.escape(primary), body, re.I))
        if hits > KEYWORD_REPEAT_MAX:
            r.add("keyword_repetition", "warn",
                  f'"{primary}" appears {hits} times as an exact match — the '
                  f"ceiling is {KEYWORD_REPEAT_MAX}. Replace the surplus with "
                  "natural variations and pronouns; keep the opening, one H2 "
                  "and the close.")
        else:
            r.add("keyword_repetition", "pass",
                  f'"{primary}" appears {hits} time(s), within the '
                  f"{KEYWORD_REPEAT_MAX} ceiling")

    # 18. V2 — certification code presence -----------------------------------
    # Only runs for certification-intent articles that have a REGISTRATION
    # code on record. The acronym is not accepted: it is already in the title
    # of every certification article, so checking it would pass by
    # construction, while "156-836" is the string a candidate searches and
    # books against — and the one the CCME article never printed.
    if kb.is_certification:
        missing = missing_exam_codes(body, kb, covered + uncovered) \
            if kb.exam_codes else []
        if missing:
            r.add("exam_code_presence", "warn",
                  "certification article never names the exam code: "
                  + "; ".join(missing)
                  + ". It is the most citable entity on the page.")
        elif not kb.exam_codes:
            # FAIL-OPEN, CLOSED. This used to skip silently whenever the KB
            # held no registration code, which meant the LESS the knowledge
            # base knew about a certification, the cleaner the article scored
            # — the Prisma Access guide printed no exam code, no question
            # count, no duration and no cost, and this check said nothing at
            # all because there was nothing on record to compare against.
            # Absence of data is not evidence of correctness; say so.
            r.add("exam_code_presence", "warn",
                  "no registration code is on record for any "
                  f"{kb.vendor_name or 'vendor'} certification, so this "
                  "article's exam mechanics could not be checked at all. "
                  "A targeted fix cannot clear this: with no code on record "
                  "there is nothing for it to print. Add the code to the "
                  "Vendor Knowledge Base from the vendor's own exam page, or "
                  "confirm by hand that the article names it.")
        else:
            r.add("exam_code_presence", "pass", "exam code present")

    # 28. V11 — vendor grounding ---------------------------------------------
    # An article about a vendor's certification that never read a page from
    # that vendor cannot have verified the credential it describes, and the
    # failure is silent in every other check: structure, citations, keyword
    # coverage and readability are all satisfiable from anybody's prose.
    #
    # The article that proved it was headlined "The Complete Prisma Access SSE
    # Certification Guide 2026" and was written from exactly two sources — a
    # Fortinet page comparing Fortinet's firewall to Palo Alto's, and a NIST
    # zero-trust PDF. It scored REACS 100 and QA 94 while inventing the
    # credential's name: there is no "Prisma Access SSE certification", the
    # credential is the Palo Alto Networks Certified Security Service Edge
    # Engineer and "Prisma Access SSE: Configuration and Deployment" is the
    # course. Both facts are on the vendor's own site, on the page that ranks
    # first for the article's own keyword.
    #
    # FAILS. The standard rates a fabricated credential critical, and on an
    # authorised training partner's blog it is a partnership problem, not a
    # quality nit. Empty vendor_domains switches the rule off, per the
    # QaKnowledge contract — a vendor we hold no domains for cannot be graded.
    if kb.is_certification and kb.vendor_domains:
        cited = {(c.source_domain or "").lower() for c in bundle.claims}
        cited |= {(ref.get("domain") or "").lower()
                  for ref in _ref_dicts(draft.references)}
        own = [d for d in cited
               if any(d == v or d.endswith("." + v) for v in kb.vendor_domains)]
        if not own:
            r.add("vendor_grounding", "fail",
                  f"certification guide with no source from "
                  f"{kb.vendor_name or 'the vendor'}'s own site "
                  f"({', '.join(sorted(kb.vendor_domains))}) — "
                  + (f"written from {', '.join(sorted(d for d in cited if d))} "
                     "instead" if any(cited) else "no usable sources at all")
                  + ". The credential's name, level, exam code and domain "
                  "weightings can only be confirmed against the vendor, so "
                  "nothing on this page about the exam itself has been "
                  "verified. A targeted fix cannot clear this: it can only "
                  "cite sources the article already has. Use Rewrite whole "
                  "article to research again, and if the vendor's domain is "
                  "not on the allowlist yet, approve it in Allowlist gaps "
                  "first.")
        else:
            r.add("vendor_grounding", "pass",
                  f"grounded in {len(own)} source(s) from "
                  + ", ".join(sorted(own)))

    # 28a. catalogue identity and high-risk editorial facts ----------------
    # Keep each FAQ entry as its own paragraph. Scope checks must not let an
    # explicit product name in one answer accidentally legitimise a role or
    # workflow claim in the next answer.
    faq_text = "\n\n".join(
        str(item.get("answer") or item.get("a") or "") + " "
        + str(item.get("question") or item.get("q") or "")
        for item in (draft.faq or []) if isinstance(item, dict))
    published_text = "\n".join(filter(None, [draft.title, seo_title,
                                                draft.meta_description,
                                                body, faq_text]))
    # Checked on published_text, not the body alone: the FAQ answers go
    # through the same reader boundary and carry the same tags.
    as_prose = _citation_as_prose(published_text)
    if as_prose:
        r.add("citation_as_prose", "fail",
              f"{len(as_prose)} citation tag(s) used as words rather than "
              "pointers — tags are stripped before publishing, so name the "
              "source and let the tag trail the sentence (\"according to "
              "Pearson VUE [S1]\"): " + "; ".join(as_prose[:3]))
    else:
        r.add("citation_as_prose", "pass",
              "every citation tag is a pointer, not a word in the sentence")

    mislabels = _course_credential_mislabels(published_text, kb)
    if mislabels:
        course, alias = mislabels[0]
        r.add("course_credential_identity", "fail",
              f'{len(mislabels)} training course(s) are presented as a '
              f'certification, credential or exam ("{alias}" is the course '
              f'"{course}"). Name the separate credential exactly if one is '
              "supported; otherwise call this a course throughout, including "
              "the title, FAQ and SEO fields.")
    elif kb.course_names:
        r.add("course_credential_identity", "pass",
              f"all {len(kb.course_names)} known course identities remain courses")

    fact_problems = _editorial_fact_problems(body)
    if fact_problems:
        r.add("volatile_facts", "fail",
              f"{len(fact_problems)} high-risk claim(s) are stale or lack exact "
              "same-sentence support: " + "; ".join(fact_problems[:3])
              + ". Remove the claim or cite a current source for the exact "
              "course, relationship, delivery method and date.")
    else:
        r.add("volatile_facts", "pass",
              "prerequisite, rename, logistics and promotion claims are current and sourced")

    severe = _severe_defect_problems(published_text, draft.references, bundle, kb)
    if severe:
        r.add("severe_defect_claims", "fail",
              f"{len(severe)} severe software defect claim(s) lack exact "
              "first-party support: " + "; ".join(severe[:3])
              + ". Use the subject vendor's advisory or release note for the "
                "exact version/ID and impact, or remove the claim.")
    elif kb.vendor_name and kb.vendor_domains:
        r.add("severe_defect_claims", "pass",
              "no unsupported absolute software defect claims")

    scope = _product_scope_problems(published_text, kb.vendor_name)
    if kb.vendor_name == "Palo Alto Networks":
        scope += panorama_scope_problems(published_text, _ref_dicts(draft.references))
    if scope:
        r.add("product_scope", "fail",
              f"{len(scope)} product/management scope error(s): "
              + "; ".join(scope[:3]))
    elif kb.vendor_name == "F5":
        r.add("product_scope", "pass",
              "F5 management roles and Guided Configuration are scoped correctly")
    elif kb.vendor_name == "Palo Alto Networks":
        r.add("product_scope", "pass", "no known Panorama management-scope errors")

    technical_text = "\n\n".join([body] + [
        str(item.get("answer") or item.get("a") or "")
        for item in (draft.faq or []) if isinstance(item, dict)])
    evidence, qualifications = technical_claim_findings(
        technical_text, _ref_dicts(draft.references), bundle, kb)
    r.add("technical_claim_evidence", "fail" if evidence else "pass",
          "; ".join(evidence[:4]) if evidence else
          "no missing technical evidence detected by citation/topic/figure checks")
    r.add("technical_claim_scope", "warn" if qualifications else "pass",
          "; ".join(qualifications[:4]) if qualifications else
          "no unqualified capacity, overwrite, universal recommendation or index-source patterns detected")

    # 28b. entity type ------------------------------------------------------
    # "EDU-330 Firewall Troubleshooting is a technical framework for isolating
    # network and application-layer anomalies." EDU-330 is a three-day
    # instructor-led course — Palo Alto's own page says so, and that page was
    # [S5] in the article's own reference list. The article cited it and
    # called it something else.
    #
    # This is the fourth article to describe one of the vendor's SKUs as the
    # wrong kind of thing: a "Prisma Access SSE certification" that is a
    # course, a "Panorama certification" that is a product, EDU-330 as a
    # "framework". It lands in the opening sentence every time, because
    # direct_answer REQUIRES a self-contained definition there — which is also
    # the block an AI assistant extracts and quotes.
    #
    # FAILS. On an authorised training partner's site, publishing the wrong
    # entity type for the vendor's own SKU is a partnership problem.
    subject = None
    typed_terms = list(kb.course_names) + list(kb.certification_names)
    for name in typed_terms + list(kb.vendor_terms or []):
        alias = _entity_alias(name) or name
        if len(alias) >= 4 and re.search(rf"\b{re.escape(alias)}\b",
                                         draft.title or "", re.I):
            subject = alias
            break
    definitions = _defining_sentences(body, subject) if subject else []
    if definitions:
        flagged = []
        for d in definitions:
            m = _WRONG_ENTITY_TYPE_RE.search(d)
            if m and not _RIGHT_ENTITY_TYPE_RE.search(d):
                flagged.append((d, m.group(0).lower()))
        if flagged:
            types = ", ".join(sorted({t for _d, t in flagged}))
            r.add("entity_type", "fail",
                  f'{len(flagged)} of {len(definitions)} sentence(s) defining '
                  f'"{subject}" call it the wrong kind of thing ({types}): '
                  + "; ".join(f'"{d[:96]}"' for d, _t in flagged[:2])
                  + ". The Vendor Knowledge Base has it as a course or "
                    "certification, not a way of working. FIX EVERY ONE - a "
                    "definition left behind further down the body is quoted "
                    "just as readily as the opening. Say what it actually is, "
                    "in the vendor's own words.")
        else:
            r.add("entity_type", "pass",
                  f'all {len(definitions)} definition(s) of "{subject}" name '
                  "the kind of thing it is")

    # 29. heading length -----------------------------------------------------
    # A heading is a label. This one was a 500-character paragraph:
    #
    #   ## Key architectural benefits of Panorama provide the core
    #   functionality needed to manage high-volume security environments where
    #   manual device-by-device configuration is no longer feasible. By using
    #   device groups and templates, administrators can ...
    #
    # The model wrote the heading and its paragraph on ONE line, and markdown
    # runs a heading to the end of the line, so the whole thing became the
    # heading. On the live site it renders as a wall of giant capitals, and it
    # shipped as a publish candidate at QA 97 / REACS 100 because nothing here
    # looked at how long a heading was — structure_length counts H2s, and a
    # 500-character H2 still counts as one.
    #
    # FAILS. Measured across the estate before choosing: 115 headings, median
    # 42 characters, 90th percentile 57, and exactly one above 80 — this one.
    # A 100-character bar cannot catch anything anyone wrote on purpose.
    long_headings = [(lvl, text) for lvl, text in
                     re.findall(r"^(#{1,6})\s*(.+)$", body, re.M)
                     if len(text.strip()) > MAX_HEADING_CHARS]
    if long_headings:
        lvl, text = long_headings[0]
        detail = (f"{len(long_headings)} heading(s) longer than "
                  f"{MAX_HEADING_CHARS} characters — a heading is a label, not "
                  f"a sentence. \"{lvl} {text[:70]}...\" is {len(text)} "
                  "characters, which means the paragraph after it was written "
                  "on the same line and markdown swallowed it into the "
                  "heading. On the site it renders as a wall of capitals.")
        # Name the heading the brief actually asked for. The model expanded an
        # outline item into a sentence, so the outline item IS the fix, and
        # saying so turns this from a judgement into a substitution.
        match = _outline_for_heading(text, outline)
        if match:
            detail += (f" The brief's outline item for this section is "
                       f"\"{match}\" — make that the heading, and move the "
                       "rest into a paragraph beneath it.")
        else:
            detail += (" Cut it to a short label and move the prose into a "
                       "paragraph beneath it.")
        r.add("heading_length", "fail", detail)
    elif re.search(r"^#{1,6}\s*\S", body, re.M):
        r.add("heading_length", "pass",
              f"all headings within {MAX_HEADING_CHARS} characters")

    # 19. V5 — brand capitalisation ------------------------------------------
    # Title and meta description are called out separately because that is
    # what a buyer and the vendor see in a SERP snippet; "check point" there
    # on an authorised partner's result is a different problem from the same
    # typo mid-paragraph.
    if kb.canonical_names:
        casing = brand_casing(
            {"title": draft.title, "meta_title": seo_title or "",
             "meta_description": draft.meta_description,
             "body": _strip_code_and_links(body)},
            kb.canonical_names)
        if casing:
            high = [d for f, d in casing if f in ("title", "meta_title",
                                                  "meta_description")]
            r.add("brand_casing", "warn",
                  f"{len(casing)} vendor name(s) mis-cased: "
                  + "; ".join(f"{f}: {d}" for f, d in casing[:4])
                  + (f" — {len(high)} of these are in the title or meta, "
                     "which is what the buyer and the vendor see" if high else ""))
        else:
            r.add("brand_casing", "pass", "vendor names correctly cased")

    # 20. V6 — Australian English --------------------------------------------
    # Vendor product names are exempted from the knowledge base, without
    # which this fires on SmartCenter and Authorization Server and gets
    # switched off — the failure mode the standard names explicitly.
    spell = us_spellings(_strip_code_and_links(body),
                         kb.vendor_terms + kb.canonical_names)
    if spell:
        r.add("locale_spelling", "warn",
              f"{len(spell)} US spelling(s): "
              + "; ".join(d for _, d in spell[:5])
              + (f" (+{len(spell) - 5} more)" if len(spell) > 5 else ""))
    else:
        r.add("locale_spelling", "pass", "no US spellings found")

    # 21. V7a — attribution source mismatch ----------------------------------
    if kb.entity_domains:
        mism = attribution_mismatches(body, _ref_dicts(draft.references),
                                      kb.entity_domains)
        if mism:
            r.add("attribution_match", "warn",
                  f"{len(mism)} claim(s) credited to one entity but cited to "
                  "another: " + "; ".join(d for _, d in mism[:3]))
        else:
            r.add("attribution_match", "pass",
                  "every attributed claim cites the entity it credits")

    # 21b. unnamed in-house attribution --------------------------------------
    # "Red Education instructors regularly see that..." — third person, so not
    # the first-person breach REACS catches, and not an unsourced external
    # claim either. It reads as authority while resting on nobody, and every
    # instance is generic enough to sit on any vendor's article unchanged.
    #
    # Structural rather than a phrase list, deliberately: one article carried
    # three instances using three different verbs — "regularly see", "note
    # that", "frequently observe". Banning a phrase produces a fourth variant.
    # A sentence naming a real person passes, because the fix is to attribute
    # the observation or drop it, not to avoid the words.
    unnamed = unnamed_attributions(body, kb.known_people)
    if unnamed:
        r.add("unnamed_attribution", "warn",
              f"{len(unnamed)} claim(s) credited to unnamed in-house staff: "
              + "; ".join(f'"{d}…"' for _c, d in unnamed[:3])
              + ". Name the instructor who says it — the byline roster is "
                "right there — or state the point without the attribution. "
                "As written it borrows authority from nobody in particular.")
    else:
        r.add("unnamed_attribution", "pass",
              "no observations credited to unnamed in-house staff")

    # 22. V7b — vague attribution --------------------------------------------
    vague = vague_attributions(body)
    if vague:
        r.add("vague_attribution", "warn",
              f"{len(vague)} unsourced appeal(s) to authority: "
              + "; ".join(d for _, d in vague[:4])
              + ". Either cite it or cut it.")
    else:
        r.add("vague_attribution", "pass", "no unsourced appeals to authority")

    # 23. V7c — named private individual — THE ONE THAT BLOCKS ---------------
    # FAIL, not WARN, and the only new rule shipping blocking. It needs no
    # knowledge-base work, and the harm is not a quality nit: the live case
    # named a private individual, asserted their employment history and
    # current role, and linked their professional profile, for no reader
    # benefit. Narrow by construction (a name AND a career claim in the same
    # sentence) because a blocking rule's false positives cost more than its
    # misses.
    people = named_private_individuals(body, kb.known_people, kb.vendor_terms)
    if people:
        r.add("named_individual", "fail",
              f"{len(people)} private individual(s) named with career claims: "
              + "; ".join(d for _, d in people[:3])
              + ". Remove the name and the claim, or confirm they are Red "
                "Education staff or an on-the-record public figure.")
    else:
        r.add("named_individual", "pass",
              "no private individuals named with career claims")

    # 24. V8 — interested-party sourcing -------------------------------------
    if kb.interested_domains:
        ip = interested_party_only(body, _ref_dicts(draft.references),
                                   kb.interested_domains)
        if ip:
            r.add("interested_party", "warn",
                  f"{len(ip)} claim(s) sourced only to an interested party: "
                  + "; ".join(d for _, d in ip[:3]))
        else:
            r.add("interested_party", "pass",
                  "no claim rests solely on a competitor or vendor-marketing source")

    # 25. V10 — cross-article contradiction -----------------------------------
    # Scoped to version claims per certification, the one claim type held
    # structured enough to compare. Finds nothing until a second article
    # exists on the same certification, which is correct rather than a stub.
    if kb.corpus_versions:
        clash = corpus_contradictions(tokens, covered + uncovered,
                                      kb.corpus_versions)
        if clash:
            r.add("corpus_consistency", "warn",
                  f"{len(clash)} claim(s) contradict another article on the "
                  "estate: " + "; ".join(d for _, d in clash[:2]))
        else:
            r.add("corpus_consistency", "pass",
                  "version claims agree with the rest of the estate")

    return r
