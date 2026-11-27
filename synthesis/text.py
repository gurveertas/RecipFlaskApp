"""Plain string utilities shared across synthesis modules — no LLM/DB concerns."""
from __future__ import annotations

import re


def slugify(text: str, limit: int = 70) -> str:
    """URL slug, truncated at a WORD boundary.

    A plain [:70] cut every slug mid-word — real examples that reached
    WordPress as live URLs: "...-and-confi" (configuring), "...-2026-objec"
    (objectives), "...-security-automa" (automation). Besides reading as
    broken, it splits the keyword phrase, which is one of the things Rank
    Math scores a page on.
    """
    s = re.sub(r"[^a-z0-9\s-]", "", text.lower())
    s = re.sub(r"\s+", "-", s).strip("-")
    if len(s) <= limit:
        return s
    cut = s[:limit]
    if "-" in cut:
        cut = cut[:cut.rindex("-")]
    return cut.strip("-")


# Rank Math flags a URL over ~75 characters. "https://www.rededucation.com/"
# plus the trailing slash is 30 of those, leaving 45 for the slug — a real
# article measured 97 characters and was flagged.
SLUG_MAX = 45


def article_slug(title: str, keyword: str = "") -> str:
    """The published URL slug: the focus keyword first, padded from the title.

    Keyword-first because two separate Rank Math checks read this — "keyword
    in URL" and "URL is not too long" — and a title-derived slug satisfied
    neither: it ran to 67 characters (a 97-character URL) and split the
    keyword across a truncation boundary.

    Falls back to the title when there is no keyword, and never exceeds
    SLUG_MAX or cuts mid-word."""
    base = slugify(keyword or title, limit=SLUG_MAX)
    if not base:
        return slugify(title, limit=SLUG_MAX)
    # Pad with title words the keyword doesn't already carry, while they fit.
    have = set(base.split("-"))
    for word in slugify(title, limit=200).split("-"):
        # Skip filler: padding with it produced slugs ending "…-the",
        # "…-how-to", "…-what-do" — length spent on nothing.
        if word in have or word in TITLE_FILLER:
            continue
        candidate = f"{base}-{word}"
        if len(candidate) > SLUG_MAX:
            break
        base, _ = candidate, have.add(word)
    return base


def today_block() -> str:
    """One line telling the model the CURRENT date — injected into every
    prompt builder (topics, brief, article, ad-hoc, section rewrite).

    Without it, models date-stamp titles/prose with the latest year in their
    training data (e.g. "…Career in 2024" generated in 2026) because nothing
    downstream checks years: the scoring formula, retrieval, and QA are all
    year-blind for generated text. Telling the model the date is the fix at
    the source."""
    from datetime import date

    t = date.today()
    return (f"Today's date is {t.day} {t.strftime('%B %Y')}. If a title, "
            f"keyword, or sentence mentions a year as current, use {t.year} — "
            "never an earlier year (your training data may lag behind today).")


def refresh_last_updated(content: str, today=None) -> str:
    """Set the visible freshness line whenever the article body changes."""
    from datetime import date

    if not content:
        return content
    current = today or date.today()
    stamp = f"Last updated: {current.day} {current.strftime('%B %Y')}"
    pattern = re.compile(r"(?im)^\s*[*_]*last updated\s*:[^\n]*$")
    if pattern.search(content):
        return pattern.sub(stamp, content, count=1)
    return content.rstrip() + "\n\n" + stamp


# Structural and marketing words that carry no subject meaning. Shared so the
# retrieval query builder and the QA title-coverage check agree on what counts
# as a title's actual SUBJECT — if they diverged, one could search for a term
# the other doesn't require, or require one it never searched for.
# Years are deliberately absent: "2026" is real subject matter in a guide.
TITLE_FILLER = frozenset({
    "how", "why", "what", "when", "which", "who", "whose", "are", "is", "was",
    "the", "a", "an", "and", "or", "but", "for", "to", "in", "of", "on", "with",
    "your", "you", "our", "their", "its", "it", "that", "this", "from", "into",
    "complete", "ultimate", "essential", "definitive", "comprehensive", "full",
    "guide", "overview", "introduction", "roadmap", "playbook", "handbook",
    "mastering", "master", "unlocking", "unlock", "navigating", "navigate",
    "reshaping", "reshape", "transforming", "transform", "simplifying",
    "understanding", "exploring", "leveraging", "harnessing", "demystifying",
    "top", "best", "key", "everything", "need", "know", "about",
})


def distinctive_terms(title: str, exclude: set[str] | None = None) -> list[str]:
    """The words in `title` that carry its actual subject, in order, minus
    filler, anything under 3 characters, and anything in `exclude`.

    This is what separates "How Check Point Acquisitions Are Reshaping
    Automated Security Workflows" into the thing the article is ABOUT
    (acquisitions, automated, workflows) from the headline scaffolding."""
    exclude = exclude or set()
    out: list[str] = []
    for tok in re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]*", title):
        low = tok.lower().strip("'-")
        if len(low) < 3 or low in TITLE_FILLER or low in exclude or low in out:
            continue
        out.append(low)
    return out


# Words that describe an article's FORM rather than its subject. These are
# still sent to retrieval (searching for "exam objectives" genuinely helps
# find the right pages) but sources are never REQUIRED to contain them: a
# perfectly good certification guide is built from sources about the
# certification, and no source anywhere says the word "objectives".
#
# Measured, not guessed: requiring every title term to appear in a source
# would have blocked 3 of 4 real published articles on 'worth', 'objectives',
# 'plan' and 'domains'. This list is what separates those from a genuine
# thematic claim like "acquisitions", which a source MUST attest because the
# article asserts it about the real world.
STRUCTURE_WORDS = frozenset({
    "objectives", "objective", "study", "plan", "exam", "exams", "domains",
    "syllabus", "curriculum", "blueprint", "outline", "prerequisites",
    "requirements", "worth", "salary", "salaries", "career", "careers",
    "path", "paths", "roles", "role", "skills", "tips", "steps", "checklist",
    "comparison", "compare", "review", "benefits", "cost", "costs", "pricing",
    "faq", "faqs", "questions", "answers", "deep", "dive", "strategies",
    "strategy", "practitioner", "practitioners", "beginner", "beginners",
    "advanced", "intermediate", "training", "certification", "certified",
    "course", "courses", "guide", "guides",
})


def thematic_terms(title: str, exclude: set[str] | None = None) -> list[str]:
    """distinctive_terms() minus the article-structure vocabulary — i.e. the
    claims a title makes about the WORLD, which retrieved sources have to
    actually support.

    "How Check Point Acquisitions Are Reshaping Automated Security Workflows"
    -> ["acquisitions", "automated", "workflows"]
    "The Complete PCNSE Certification Guide 2026: Objectives, Study Plan and
    Exam Domains" -> [] (every term describes the article's own shape)."""
    return [t for t in distinctive_terms(title, exclude)
            if t not in STRUCTURE_WORDS
            and not t.isdigit()
            # Anything containing a digit is an IDENTIFIER — a course code
            # (61132V, EDU-210), a version (R81) or a year — not a claim
            # about the world that a source must attest. They also drift
            # between revisions: a legitimate Avaya topic on 61132V was
            # blocked because every retrieved page still said 61131V, the
            # previous revision of the same course. The subject itself stays
            # protected by the non-numeric terms, and by the keyword.
            and not any(ch.isdigit() for ch in t)]


# --------------------------------------------------------------------------- #
# Writing mechanics
# --------------------------------------------------------------------------- #

# Segments whose contents must never be rewritten: fenced code, inline code,
# the target half of a markdown link, and bare URLs. Casing a word inside a
# URL changes where the link points; casing one inside code changes what the
# code says. Ordered longest-construct-first so a fence wins over an inline
# span that happens to sit inside it.
_PROTECTED = re.compile(
    r"```.*?```"                 # fenced code block
    r"|`[^`\n]*`"                # inline code
    r"|\]\([^)]*\)"              # markdown link target
    r"|https?://\S+"             # bare URL
    # Markdown table DELIMITER row: "| :--- | ---: | :---: |". This is table
    # SYNTAX, not prose, and the colon in an alignment marker is not
    # punctuation. Without it _SPACE_BEFORE_PUNCT matches the space before
    # every ":" and writing_mechanics FAILS — which BLOCKS, while REACS's
    # mandatory comparison_table pillar simultaneously REQUIRES a table. Two
    # gates in direct conflict, decided by whether the model happened to put a
    # space after the pipe: "| :--- |" failed, "|:--- |" passed. Observed on
    # two live articles that differed only in that space.
    r"|^[ \t]*\|[ \t:\-|]+\|[ \t]*$",
    re.DOTALL | re.MULTILINE)

# Version tokens this domain writes in caps and models routinely lower-case:
# Check Point releases (R82, R81.20). Not in the Vendor KB, which holds exam
# codes and certification acronyms rather than software releases, so they are
# matched by shape instead. Deliberately narrow — a bare "r" plus two digits
# in this corpus is a release, and nothing else.
_VERSION_TOKEN = re.compile(r"\bR(\d{2}(?:\.\d{1,2})?)\b", re.IGNORECASE)

# The same token with the letter split off — "R 82". Found in the live Vendor
# KB ("Check Point Certified Troubleshooting Expert (CCTE) R 82") while the
# same row's exam_code read "CCTE R82", so it reached article anchor text from
# the data rather than from the model. Normalised here too, because scraped KB
# names flow into prose and the next import can reintroduce it.
_SPLIT_VERSION = re.compile(r"\bR (\d{2}(?:\.\d{1,2})?)\b")

# " ,"  " ."  — a space before closing punctuation.
_SPACE_BEFORE_PUNCT = re.compile(r"[ \t]+([,.;:!?])")
# "the the", "to to" — the same word twice. Case-insensitive, but only for
# words that are plausibly duplicated by a model; a legitimate "had had" is
# vanishingly rare in this register.
_DOUBLED_WORD = re.compile(r"\b(\w+)(\s+)\1\b", re.IGNORECASE)


def _split_protected(text: str):
    """Yield (segment, is_protected) so callers rewrite only prose."""
    pos = 0
    for m in _PROTECTED.finditer(text or ""):
        if m.start() > pos:
            yield text[pos:m.start()], False
        yield m.group(0), True
        pos = m.end()
    if pos < len(text or ""):
        yield text[pos:], False


# Ordinary English words that a catalogue entry can donate by accident. "BIG"
# arrived from "Configuring BIG IP Access Policy Manager" — someone typed
# BIG-IP without the hyphen — and everything in this set is uppercased
# wherever it appears as a whole word, so that one row would have published
# "a BIG improvement".
#
# The asymmetry is what decides the contents: uppercasing a common word is
# visibly broken prose, while missing a rare acronym is a small miss. When in
# doubt, it goes in here.
_NEVER_AN_ACRONYM = {
    "BIG", "NEW", "ALL", "ONE", "TWO", "END", "TOP", "KEY", "LOW", "HIGH",
    "FULL", "CORE", "EDGE", "NEXT", "OPEN", "REAL", "LIVE", "FAST", "WEB",
    "AND", "THE", "FOR", "WITH", "YOUR", "OUR", "ITS", "IT", "AS", "OR",
    "IS", "TO", "IN", "ON", "AT", "BY", "OF", "AN", "A", "PRO", "MAX",
    "SET", "GET", "RUN", "USE", "ADD", "NEW", "PLUS", "BASE", "MAIN",
}


def fix_writing_mechanics(text: str, acronyms: set[str] | None = None,
                          vendor_terms: list[str] | None = None) -> str:
    """Correct the mechanical errors we can fix deterministically.

    Casing of known acronyms is the one that keeps reaching published drafts:
    the model writes "ccte r82 certification" and nothing downstream fixed it,
    because apply_acronym_case() was only ever applied to scraped titles and
    link anchors, never to the article body.

    Fixed rather than merely flagged, because the correct spelling is already
    known — the Vendor KB holds it. Failing QA and hoping the next attempt
    complies costs a retry and, as course_link_cta showed, often does not
    work.

    Australian spelling is corrected here for the same reason, and it is the
    reason this function grew a `vendor_terms` argument. locale_spelling is
    one word to swap — "organizations" for "organisations" — and it survived
    three consecutive targeted fixes on one article, three LLM calls spent on
    a string replacement. The exemptions come from qa_rules.us_spelling_spans,
    the SAME function the check reports from, so the fixer cannot correct
    something the checker would have allowed: without that shared list this
    would rewrite SmartCenter as SmartCentre.

    Never touches code, link targets or URLs.
    """
    if not text:
        return text

    out = []
    for seg, protected in _split_protected(text):
        if protected:
            out.append(seg)
            continue
        if acronyms:
            # The stoplist is applied HERE as well as at harvest time. This is
            # the function that can corrupt prose, so it does not rely on its
            # caller having curated the set it was handed.
            seg = re.sub(
                r"[A-Za-z][A-Za-z0-9-]*",
                lambda m: (m.group(0).upper()
                           if m.group(0).upper() in acronyms
                           and m.group(0).upper() not in _NEVER_AN_ACRONYM
                           else m.group(0)),
                seg)
        seg = _VERSION_TOKEN.sub(lambda m: "R" + m.group(1), seg)
        seg = _SPLIT_VERSION.sub(lambda m: "R" + m.group(1), seg)
        seg = _SPACE_BEFORE_PUNCT.sub(r"\1", seg)
        seg = _DOUBLED_WORD.sub(lambda m: m.group(1), seg)
        seg = _to_au_spelling(seg, vendor_terms)
        seg = _fix_entity_casing(seg, vendor_terms)
        out.append(seg)
    return "".join(out)


def _entity_phrases(vendor_terms: list[str] | None) -> list[str]:
    """Product phrases from KB entity names, canonically cased.

    The head before the colon is the product; what follows is the course.
    "Cortex XSIAM: Security Operations, Integration, and Automation" gives
    "Cortex XSIAM", which is the form that belongs in prose.

    TWO WORDS MINIMUM, deliberately. A single-word entity like "Panorama" is
    an ordinary noun as often as a product, and case-correcting one mid
    sentence is a worse error than leaving it alone. A two-word phrase is
    distinctive enough to be safe."""
    phrases = []
    for term in vendor_terms or []:
        head = (term or "").split(":")[0]
        head = re.sub(r"\s*\([^)]*\)\s*$", "", head).strip(" -–—,")
        if 2 <= len(head.split()) <= 6 and any(c.isalpha() for c in head):
            phrases.append(head)
    # Longest first, so "Cortex XSIAM" is corrected before a shorter phrase
    # that overlaps it can claim part of the same span.
    return sorted(set(phrases), key=len, reverse=True)


def _brand_tokens(vendor_terms: list[str] | None) -> list[str]:
    """Single KB words that are brands because of their INTERNAL capital.

    _entity_phrases above needs two words, deliberately: "Panorama" alone is an
    ordinary noun as often as a product, and case-correcting one mid sentence
    is worse than leaving it. That rule has a hole. "FortiManager" is one word,
    carries no ALL-CAPS run for known_acronyms to find, and so reached a
    published draft as "Fortimanager" nine times, in the title and the meta
    description too, with brand_casing and writing_mechanics both passing.

    An interior capital is the distinguishing mark and it is safe where a
    leading capital is not: no ordinary English word has one. "Panorama" stays
    out, and so does the KB's own miscased "Fortios" row — which is the point,
    since harvesting that would teach the fixer to write the error.

    Words are taken from anywhere in an entity name, so "FortiManager
    Administrator 7.6" yields the brand without the course around it.
    """
    tokens = {}
    for term in vendor_terms or []:
        for word in re.findall(r"[A-Za-z][A-Za-z0-9]*", term or ""):
            if len(word) > 2 and any(c.isupper() for c in word[1:]) and not word.isupper():
                tokens.setdefault(word.lower(), word)
    return [tokens[k] for k in sorted(tokens, key=len, reverse=True)]


def _fix_entity_casing(text: str, vendor_terms: list[str] | None) -> str:
    """Give KB product names the casing the KB spells them with.

    The article opened "The cortex xsiam vs siem migration is". XSIAM and SIEM
    are acronyms and handled above; "cortex" is a product name, and it was
    lower case for a structural reason — the target keyword is stored in lower
    case and both REACS's direct_answer and QA's keyword_coverage require it
    VERBATIM near the start, so the writer pastes in exactly what it was
    given."""
    for phrase in _entity_phrases(vendor_terms):
        pattern = r"\b" + r"\s+".join(re.escape(w) for w in phrase.split()) + r"\b"
        text = re.sub(pattern,
                      lambda m, p=phrase: p if m.group(0) != p else m.group(0),
                      text, flags=re.I)
    # Then the single-word brands the phrase rule cannot reach (see
    # _brand_tokens). After the phrases, so a phrase already corrected as a
    # whole is not taken apart word by word.
    for token in _brand_tokens(vendor_terms):
        text = re.sub(r"\b" + re.escape(token) + r"\b",
                      lambda m, t=token: t if m.group(0) != t else m.group(0),
                      text, flags=re.I)
    return text


def _to_au_spelling(text: str, vendor_terms: list[str] | None) -> str:
    """Apply the AU spelling corrections qa_rules would report on this text.

    Right to left, so an earlier replacement never shifts a later span."""
    from .qa_rules import us_spelling_spans          # local: avoids a cycle

    spans = us_spelling_spans(text, list(vendor_terms or []))
    for start, end, replacement in reversed(spans):
        text = text[:start] + replacement + text[end:]
    return text


def writing_mechanics_issues(text: str,
                             acronyms: set[str] | None = None,
                             vendor_terms: list[str] | None = None) -> list[str]:
    """Mechanical errors still present, as short human-readable strings.

    The QA counterpart to fix_writing_mechanics: anything this returns is
    something the fixer could not or did not repair, which means either a new
    class of error or a bug in the fixer. Prose only — code and URLs are
    skipped, same as the fixer.
    """
    issues: list[str] = []
    seen: set[str] = set()

    def note(msg: str) -> None:
        if msg not in seen:
            seen.add(msg)
            issues.append(msg)

    for seg, protected in _split_protected(text or ""):
        if protected:
            continue
        if acronyms:
            for m in re.finditer(r"[A-Za-z][A-Za-z0-9-]*", seg):
                word = m.group(0)
                if (word.upper() in acronyms and word != word.upper()
                        and word.upper() not in _NEVER_AN_ACRONYM):
                    note(f'"{word}" should be "{word.upper()}"')
        for m in _VERSION_TOKEN.finditer(seg):
            if not m.group(0).startswith("R"):
                note(f'"{m.group(0)}" should be "R{m.group(1)}"')
        for m in _SPLIT_VERSION.finditer(seg):
            note(f'"{m.group(0)}" should be "R{m.group(1)}" (no space)')
        for m in _SPACE_BEFORE_PUNCT.finditer(seg):
            note(f'space before "{m.group(1)}"')
        for m in _DOUBLED_WORD.finditer(seg):
            note(f'repeated word "{m.group(1)}"')
        # Product names in the wrong case. Symmetry with the fixer above: if
        # _fix_entity_casing would change it, this must be able to say so, or
        # a residual it could not reach goes unreported.
        for phrase in _entity_phrases(vendor_terms):
            pattern = (r"\b" + r"\s+".join(re.escape(w) for w in phrase.split())
                       + r"\b")
            for m in re.finditer(pattern, seg, re.I):
                if m.group(0) != phrase:
                    note(f'"{m.group(0)}" should be "{phrase}"')
        for token in _brand_tokens(vendor_terms):
            for m in re.finditer(r"\b" + re.escape(token) + r"\b", seg, re.I):
                if m.group(0) != token:
                    note(f'"{m.group(0)}" should be "{token}"')
    return issues


# --------------------------------------------------------------------------- #
# Rank Math "Title Readability"
# --------------------------------------------------------------------------- #
# Rank Math scores the SEO TITLE (meta_title), not the article H1, and runs
# four independent title checks. Two of them — power word and sentiment word —
# are SEPARATE, so a title carrying only one of the two always fails the other.
# The brief prompt used to say "one is enough", which guaranteed 2 errors on
# every article.
#
# These are curated subsets of Rank Math's own lists, not copies of them: the
# authoritative lists live in the plugin and run over a thousand entries. Words
# here were chosen to be unambiguous members of their category AND to suit an
# authorised training partner's register — Rank Math's power list also holds
# "Jaw-Dropping" and "Sinful", which are not going in a certification guide.
# Anything Rank Math accepts that is missing here shows as a WARN we did not
# need; the fix is to add the word, not to loosen the check.
POWER_WORDS = frozenset({
    "complete", "comprehensive", "definitive", "essential", "ultimate",
    "proven", "practical", "effective", "expert", "authoritative",
    "strategic", "critical", "crucial", "key", "vital", "important",
    "professional", "reliable", "trusted", "leading", "advanced",
    "quick", "fast", "instant", "step-by-step", "actionable",
    "official", "certified", "real", "genuine", "exclusive",
})

# Rank Math accepts EITHER polarity for the sentiment check, so both are here.
SENTIMENT_WORDS = frozenset({
    # positive
    "best", "great", "good", "easy", "simple", "smart", "powerful", "strong",
    "valuable", "useful", "worth", "worthy", "worthwhile", "successful",
    "safe", "clear", "confident", "rewarding", "top", "better", "easier",
    "simpler", "stronger",
    # negative — equally valid, and often the more honest framing for a
    # difficulty or cost angle ("Is the CCTE R82 Exam Too Hard?")
    "hard", "difficult", "worst", "risky", "costly", "expensive", "painful",
    "confusing", "common", "avoid", "mistakes", "pitfalls", "wrong",
    "harder", "tough", "tricky", "demanding", "steep",
})

_TOKEN = re.compile(r"[A-Za-z][A-Za-z-]*")


def title_readability_issues(seo_title: str, keyword: str = "") -> list[str]:
    """Rank Math's four Title Readability checks, run locally.

    Deterministic, and cheap enough to run on every draft — Rank Math itself
    only reports these after the post reaches WordPress, which is far too late
    to be useful: by then the article is written, QA'd and pushed.
    """
    title = (seo_title or "").strip()
    if not title:
        return ["no SEO title"]

    issues: list[str] = []
    words = {w.lower() for w in _TOKEN.findall(title)}

    kw = (keyword or "").strip().lower()
    if kw:
        # "at the beginning" is Rank Math's wording; it accepts the phrase
        # starting within roughly the first half of the title.
        pos = title.lower().find(kw)
        if pos < 0:
            issues.append(f'focus keyword "{keyword}" missing from the SEO title')
        elif pos > len(title) // 2:
            issues.append(f'focus keyword "{keyword}" is not near the beginning')

    if not (words & POWER_WORDS):
        issues.append("no power word (e.g. Complete, Essential, Proven)")
    if not (words & SENTIMENT_WORDS):
        issues.append("no positive/negative sentiment word (e.g. Best, Easy, Hard)")
    if not any(ch.isdigit() for ch in title):
        issues.append("no number (a year or a count)")
    return issues


# Function words and punctuation a title must never end on once truncated.
# Cutting at a word boundary alone still leaves "... Study Plan and a".
_DANGLING = {"a", "an", "the", "and", "or", "of", "for", "to", "in", "on",
             "with", "your", "its", "is", "are", "that", "how", "why", "what"}


def clamp_title(text: str, limit: int = 120) -> str:
    """Trim a title to `limit` characters without cutting a word or leaving a
    dangling connective.

    A plain [:120] produced "…Study Plan and a Genuinely Worthy Ex" — a title
    severed mid-word, shown in the header and carried into the article. It
    surfaced only once titles got longer, which the Rank Math work made
    routine by requiring a power word, a sentiment word and a number.

    Same failure the slug had, and the same fix: back off to a boundary, then
    keep backing off while the last word is one that cannot end a title.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text

    cut = text[:limit]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]

    words = cut.split()
    while words:
        last = words[-1].strip(",:;—-").lower()
        if last in _DANGLING or not last:
            words.pop()
            continue
        break
    return " ".join(words).rstrip(" ,:;—-")
