"""REACS v1.3 QA risk rules — the detection half.

These are the rules the standard reports SEPARATELY from the REACS score:
"Factual accuracy is treated as a partnership risk, not a quality
preference." Structure and truth are orthogonal, so nothing here contributes
to the REACS pillar score; synthesis/validate.py turns each finding into a
QA check with its own status.

Why a separate module rather than more of validate.py: these are pure
functions over text plus knowledge-base context, with no Draft/QAReport
coupling, which makes them testable on their own and keeps validate.py
readable as a harness rather than a rulebook. V1 (version strings), V4
(promise gaps) and V9 (keyword repetition) landed in validate.py earlier and
deliberately stay there — they were written against the harness's own locals
and moving them now would be churn for its own sake.

Severity, and why almost everything WARNs:
  V7c  named private individual  -> FAIL. The one rule the standard says can
                                   ship blocking immediately: it needs no
                                   knowledge-base work, and the harm (a real
                                   person's employment history asserted on a
                                   commercial page) is not a quality nit.
  everything else               -> WARN, because each depends on knowledge-
                                   base coverage that is still partial.
                                   Promote as the data lands.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class QaKnowledge:
    """Everything the QA risk rules need to know that is not in the draft.

    One object rather than a dozen keyword arguments on validate_draft: the
    rules keep needing more context (domains, canonical names, the rest of
    the corpus) and a flat signature was already at eight parameters. Every
    field defaults to empty, and an empty field switches its rule OFF rather
    than making it guess — a rule with no data must produce no findings, not
    false ones."""

    vendor_name: str | None = None
    # {exam code: [sourced version labels]} — see config/db.py's
    # CertificationVersion. Powers V1 in validate.py.
    cert_versions: dict[str, list[str]] = field(default_factory=dict)
    # {exam code: [registration codes]} e.g. {"CCME": ["156-836"]}. V2.
    exam_codes: dict[str, list[str]] = field(default_factory=dict)
    # EVERY certification acronym this vendor has, whether or not versions are
    # recorded for it. V1 needs this to tell two very different situations
    # apart: an article about a certification we hold no versions for (cannot
    # verify — say so) versus an article naming no certification at all (a
    # product-release statement, which is not a certification claim).
    all_cert_codes: list[str] = field(default_factory=list)
    is_certification: bool = False
    # Brand names in their canonical casing. V5.
    canonical_names: list[str] = field(default_factory=list)
    # Vendor KB product/cert/course names, exempted from V6 spelling.
    vendor_terms: list[str] = field(default_factory=list)
    # Kept separate as an editorial contract. A course may prepare someone
    # for a credential, but it is never itself a certification or exam.
    certification_names: list[str] = field(default_factory=list)
    course_names: list[str] = field(default_factory=list)
    # {entity: [domains that entity actually controls]}. V7a.
    entity_domains: dict[str, list[str]] = field(default_factory=dict)
    # Domains belonging to a competitor or to vendor marketing. V8.
    interested_domains: set[str] = field(default_factory=set)
    # The domains THIS article's vendor actually publishes on. V11: a
    # certification guide written without reading one page of the vendor's own
    # site cannot have verified the credential it describes.
    vendor_domains: set[str] = field(default_factory=set)
    # People we are allowed to name: byline instructors, Red Education staff.
    known_people: set[str] = field(default_factory=set)
    # {version label: article title that already claims it} for this
    # certification, from the rest of the corpus. V10.
    corpus_versions: dict[str, dict[str, str]] = field(default_factory=dict)


Finding = tuple[str, str]          # (code, human-readable detail)


def _sentences(text: str) -> list[str]:
    """Sentences, and NEWLINES ARE BOUNDARIES.

    Splitting on punctuation alone glued a heading to the paragraph under it
    — "### Troubleshooting and Specialisations\nFor those focused on..." came
    through as one sentence, and every rule here then reasoned about a
    "sentence" that spanned two unrelated blocks. It produced a live blocking
    false positive (see named_private_individuals). A heading is not part of
    the sentence that follows it."""
    parts = re.split(r"(?<=[.!?])\s+|\n+", text or "")
    return [p.strip() for p in parts if p and p.strip()]


def _norm_code(s: str) -> str:
    """Exam codes match with or without separators: "156-836", "156 836" and
    "156836" are the same code to a reader and to a search engine."""
    return re.sub(r"[\s‐-―\-]", "", s or "").lower()


# --------------------------------------------------------------------------- #
# V2 — certification code presence
# --------------------------------------------------------------------------- #

def missing_exam_codes(body: str, kb: QaKnowledge,
                       subjects: list[str]) -> list[str]:
    """Certification codes this article should name and does not.

    Only the REGISTRATION code counts ("156-836"), never the acronym. The
    acronym is almost always in the title already, so checking it would make
    the rule pass by construction; the registration code is the string a
    candidate actually searches and books against, and it is the one the CCME
    article omitted entirely.

    A certification with no registration code on record is skipped, not
    failed — the pillar cannot apply to a fact the knowledge base has not
    been told. That is also why backfilling exam_codes is what makes this
    rule bite."""
    if not kb.is_certification:
        return []
    normalised = _norm_code(body)
    missing = []
    for cert in (subjects or kb.exam_codes):
        codes = [c for c in kb.exam_codes.get(cert, []) if c]
        if not codes:
            continue
        # ANY of the certification's codes satisfies the rule, not all of
        # them. A certification running two versions concurrently has two
        # registration codes — CCSE is 156-315.82 for R82 and 156-315.81.20
        # for R81.20 — and an article about one of them has no business
        # printing the other. Requiring every code made a correct article
        # fail for omitting an exam it was not about.
        if not any(_norm_code(c) in normalised for c in codes):
            missing.append(f"{cert}: {' or '.join(codes)}")
    return missing


# --------------------------------------------------------------------------- #
# V5 — brand capitalisation
# --------------------------------------------------------------------------- #

def brand_casing(fields: dict[str, str], canonical_names: list[str]) -> list[Finding]:
    """Vendor names cased wrongly, per field.

    The standard rates title and meta description high severity and the body
    a warning, because the first two are what the buyer and the vendor see in
    a SERP snippet — "check point" on an authorised partner's search result
    is a different kind of problem from the same typo mid-paragraph. The
    field name travels with the finding so the message can say so."""
    out: list[Finding] = []
    seen: set[tuple[str, str, str]] = set()
    for fieldname, value in fields.items():
        if not value:
            continue
        for canonical in canonical_names:
            if len(canonical) < 2:
                continue
            for m in re.finditer(r"\b" + re.escape(canonical) + r"\b", value, re.I):
                found = m.group(0)
                if found != canonical and (fieldname, canonical, found) not in seen:
                    seen.add((fieldname, canonical, found))
                    out.append((fieldname, f'"{found}" should be "{canonical}"'))
    return out


# --------------------------------------------------------------------------- #
# V6 — Australian English
# --------------------------------------------------------------------------- #

# Words that legitimately end -ize/-ise in Australian English. Without these
# the suffix rule flags "size" and "prize" and the whole check gets switched
# off as noise, which is exactly what the standard warns about.
AU_EXCEPTIONS: set[str] = {
    "size", "sizes", "sized", "sizing", "resize", "resizes", "resized",
    "resizing", "downsize", "downsized", "upsize", "capsize", "prize",
    "prizes", "prized", "seize", "seizes", "seized", "seizing", "maize",
    "assize", "wise", "rise", "raise", "praise", "advise", "revise",
    "supervise", "comprise", "surprise", "exercise", "franchise",
    "merchandise", "improvise", "devise", "disguise", "premise", "promise",
    "expertise", "enterprise", "otherwise", "likewise",
    # "program" is a computer program in AU English and stays.
    "program", "programs", "programmed", "programming",
}

# Explicit US -> AU pairs the suffix rules cannot derive. "enrolment" takes
# one L in Australian English and is the exact word the standard records as
# having slipped through a check that already flagged US spellings.
US_TO_AU: dict[str, str] = {
    "enrollment": "enrolment", "enrollments": "enrolments",
    "enroll": "enrol", "enrolls": "enrols", "enrolled": "enrolled",
    "color": "colour", "colors": "colours", "colored": "coloured",
    "favor": "favour", "favors": "favours", "favorite": "favourite",
    "behavior": "behaviour", "behaviors": "behaviours",
    "honor": "honour", "honors": "honours", "labor": "labour",
    "neighbor": "neighbour", "rumor": "rumour", "vigor": "vigour",
    "armor": "armour", "flavor": "flavour", "harbor": "harbour",
    "humor": "humour", "odor": "odour", "valor": "valour",
    "center": "centre", "centers": "centres", "centered": "centred",
    "meter": "metre", "meters": "metres", "fiber": "fibre",
    "fibers": "fibres", "theater": "theatre", "liter": "litre",
    "caliber": "calibre",
    "defense": "defence", "defenses": "defences",
    "offense": "offence", "offenses": "offences",
    "catalog": "catalogue", "catalogs": "catalogues",
    "dialog": "dialogue", "analog": "analogue",
    "traveled": "travelled", "traveling": "travelling",
    "canceled": "cancelled", "canceling": "cancelling",
    "labeled": "labelled", "labeling": "labelling",
    "modeled": "modelled", "modeling": "modelling",
    "fueled": "fuelled", "fueling": "fuelling",
    "signaling": "signalling", "signaled": "signalled",
    "gray": "grey", "aluminum": "aluminium", "sulfur": "sulphur",
}

# Exact vendor/UI labels that intentionally contain US spelling. These are
# not prose exceptions: changing the spelling changes the feature name shown
# in the vendor's product and configuration interfaces. Keep the phrase, not
# the bare word, so ordinary "defense" still receives the house-style fix.
OFFICIAL_US_SPELLING_TERMS = ("Bot Defense",)

# The -ational forms are here because they were missing: "organizational"
# ends in "ational", not "ation", so the old pattern matched none of
# organizational / organizationally / authorizational, and locale_spelling
# reported an article clean while its Key Facts block said "organizational
# security controls". Every other US spelling in that article was caught.
_IZE_RE = re.compile(
    r"\b[a-z]+(?:iz(?:e|es|ed|ing|ation(?:s|al|ally)?)|yz(?:e|es|ed|ing))\b",
    re.I)

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]*")


def _in_proper_noun_phrase(text: str, start: int, end: int) -> bool:
    """True when the match sits inside a Title Case run of three or more
    words — i.e. it is part of a proper noun, not a spelling mistake.

    The knowledge base holds certifications and courses, not architecture and
    feature names, so a vendor-term exemption list can never be complete.
    Live false positive that prompted this: "AI Data Center Security
    Architecture", a Check Point architecture name, flagged four times for
    "Center". The standard is explicit that this is the failure mode to avoid
    — "this generates noise and gets switched off" — so the shape of the
    phrase carries the exemption where the KB cannot.

    Requires the match itself to be capitalised and to have a capitalised
    neighbour on BOTH sides, which ordinary prose ("the enrollment centre
    opens") does not satisfy."""
    if not text[start:end][:1].isupper():
        return False
    before = _WORD_RE.findall(text[max(0, start - 60):start])
    after = _WORD_RE.findall(text[end:end + 60])
    prev_cap = bool(before) and before[-1][:1].isupper()
    next_cap = bool(after) and after[0][:1].isupper()
    return prev_cap and next_cap


def _au_form(word: str) -> str:
    w = word.lower()
    # LONGEST FIRST. "organizationally" ends with "ization" too, and a
    # shorter match returning first would leave the tail mangled.
    for us, au in (("izationally", "isationally"), ("izational", "isational"),
                   ("izations", "isations"), ("ization", "isation"),
                   ("izing", "ising"), ("ized", "ised"), ("izes", "ises"),
                   ("ize", "ise"), ("yzing", "ysing"), ("yzed", "ysed"),
                   ("yzes", "yses"), ("yze", "yse")):
        if w.endswith(us):
            return w[:-len(us)] + au
    return w


def _match_case(source: str, replacement: str) -> str:
    """`replacement` wearing `source`'s capitalisation.

    _au_form lowercases what it is given, which is harmless in a report and
    corrupting in a fixer: a sentence-initial "Organizations" must come back
    as "Organisations", not "organisations"."""
    if source.isupper() and len(source) > 1:
        return replacement.upper()
    if source[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def us_spelling_spans(text: str, vendor_terms: list[str]
                      ) -> list[tuple[int, int, str]]:
    """Every US spelling in `text` that is a real error: (start, end, AU form).

    THE single source of truth for this rule. us_spellings() reports from it
    and fix_writing_mechanics() corrects from it, so the two can never
    disagree about what counts as an error — a fixer with looser exemptions
    than its checker would "correct" a vendor product name, turning
    SmartCenter into SmartCentre, which is far worse than the spelling it set
    out to fix.

    Vendor terms are exempted by CONTAINMENT, not equality: the offending
    token inside "SmartCenter" is "Center" and inside "Authorization Server"
    is "Authorization", and neither is a spelling mistake — they are product
    names. Without this the rule generates noise on exactly the vendors we
    write about most, and a noisy check gets turned off."""
    text = text or ""
    exempt_terms = [t.lower() for t in vendor_terms if t]
    official_spans = [m.span() for term in OFFICIAL_US_SPELLING_TERMS
                      for m in re.finditer(re.escape(term), text, re.I)]

    def exempted(word: str, start: int) -> bool:
        low = word.lower()
        if low in AU_EXCEPTIONS:
            return True
        # Inside a longer CamelCase product token, e.g. SmartCenter.
        before = text[max(0, start - 1):start]
        if before.isalpha():
            return True
        if _in_proper_noun_phrase(text, start, start + len(word)):
            return True
        if any(start >= left and start + len(word) <= right
               for left, right in official_spans):
            return True
        return any(low in term for term in exempt_terms)

    spans: list[tuple[int, int, str]] = []
    for m in _IZE_RE.finditer(text):
        if not exempted(m.group(0), m.start()):
            spans.append((m.start(), m.end(),
                          _match_case(m.group(0), _au_form(m.group(0)))))
    for us, au in US_TO_AU.items():
        for m in re.finditer(r"\b" + us + r"\b", text, re.I):
            if not exempted(m.group(0), m.start()):
                spans.append((m.start(), m.end(),
                              _match_case(m.group(0), au)))
    # Left to right, and never two corrections over the same characters.
    spans.sort()
    out: list[tuple[int, int, str]] = []
    for span in spans:
        if not out or span[0] >= out[-1][1]:
            out.append(span)
    return out


def us_spellings(text: str, vendor_terms: list[str]) -> list[Finding]:
    """US spellings in prose, with vendor product names exempted. One finding
    per distinct word, so a term used ten times is one problem, not ten."""
    out: list[Finding] = []
    seen: set[str] = set()
    for start, end, au in us_spelling_spans(text, vendor_terms):
        word = (text or "")[start:end]
        if word.lower() in seen:
            continue
        seen.add(word.lower())
        out.append(("spelling", f'"{word}" should be "{au}"'))
    return out


# --------------------------------------------------------------------------- #
# V7a / V7b / V7c — attribution integrity
# --------------------------------------------------------------------------- #

# "According to Unit 42", "research from Mandiant", "Gartner reports".
# The lead-in is case-insensitive (a sentence starts "According to"), but the
# ENTITY capture must stay case-sensitive or it swallows ordinary lowercase
# words after the phrase and reports them as entities. Hence the scoped (?i:).
_ATTRIBUTION_RE = re.compile(
    r"(?i:according to|research (?:from|by)|a (?:report|study) (?:from|by)|"
    r"data from|findings from|as reported by)\s+"
    r"([A-Z][A-Za-z0-9&.\-]*(?:\s+[A-Z0-9][A-Za-z0-9&.\-]*){0,3})")

# A marker may cite SEVERAL sources: "[S4, S5]" and "[S2; S4]" are both in the
# corpus (webapp/render.py strips exactly these shapes before publishing). The
# old pattern matched a lone [S1] only, so a sentence cited in combined form
# read as uncited: vague_attribution reported "studies show with no citation"
# on a properly sourced sentence, while attribution_match and interested_party
# went silent on those sentences altogether.
#
# Presence and tag extraction are separate patterns because they answer
# different questions — "is this sentence cited at all" and "which sources".
_CITATION_RE = re.compile(r"\[\s*S\d+(?:\s*(?:,|;|and)\s*S\d+)*\s*\]", re.I)
_CITATION_TAG_RE = re.compile(r"S(\d+)", re.I)


def _cited_tags(sentence: str) -> list[str]:
    """Every source tag inside this sentence's citation markers."""
    return [f"S{n}" for m in _CITATION_RE.finditer(sentence)
            for n in _CITATION_TAG_RE.findall(m.group(0))]


def attribution_mismatches(body: str, references: list[dict],
                           entity_domains: dict[str, list[str]]) -> list[Finding]:
    """Claims attributed to a named entity whose citation points somewhere else.

    The live defect: a claim attributed to Unit 42 linked to a CSO Online
    listicle. Nothing caught it, because the schema stored a quote and a URL
    but never who the body said it came from — so there was nothing to
    compare. Here the body IS the record of attribution: the sentence names
    the entity and carries the [S#] tag, and the reference list maps that tag
    to a real domain.

    Only entities we hold domains for are judged. An unrecognised entity is
    not a finding — the standard treats an unknown entity as one, but with no
    entity registry that would flag every ordinary mention of a company we
    simply have not listed, which is how a rule earns its way into being
    ignored."""
    by_tag = {str(r.get("tag", "")).upper(): (r.get("domain") or "").lower()
              for r in (references or [])}
    out: list[Finding] = []
    for sentence in _sentences(body):
        tags = _cited_tags(sentence)
        if not tags:
            continue
        for m in _ATTRIBUTION_RE.finditer(sentence):
            entity = m.group(1).strip().rstrip(".,")
            allowed = None
            for known, domains in entity_domains.items():
                if known.lower() == entity.lower() or entity.lower().startswith(known.lower()):
                    allowed = [d.lower() for d in domains]
                    break
            if allowed is None:
                continue
            cited = [by_tag.get(t, "") for t in tags if by_tag.get(t)]
            if cited and not any(c.endswith(d) or d.endswith(c)
                                 for c in cited for d in allowed):
                out.append(("attribution",
                            f'"{entity}" is credited but the citation points to '
                            f'{", ".join(sorted(set(cited)))} — expected '
                            f'{", ".join(allowed)}'))
    return out


VAGUE_ATTRIBUTION_PHRASES = (
    "security professionals report", "experts agree", "studies show",
    "it is widely known", "many organisations find", "many organizations find",
    "research indicates", "industry experts say", "it is generally accepted",
)


def vague_attributions(body: str) -> list[Finding]:
    """Hand-waving attribution with no citation in the same sentence.

    A cited "studies show" is lazy writing but it is checkable, and QA is not
    a style guide. Uncited, it is an unsourced claim wearing the costume of
    one, which is what the rule is for: either cite it or cut it."""
    out: list[Finding] = []
    for sentence in _sentences(body):
        low = sentence.lower()
        for phrase in VAGUE_ATTRIBUTION_PHRASES:
            if phrase in low and not _CITATION_RE.search(sentence):
                out.append(("vague", f'"{phrase}" with no citation'))
                break
    return out


# An in-house group credited with an observation, and the verbs that do the
# crediting. Structural on PURPOSE, not a phrase list: one article carried
# "Red Education instructors regularly see", "Red Education instructors note
# that" and "Red Education instructors frequently observe" — three instances,
# three different verbs, already. Banning a phrase produces a fourth variant;
# the shape is what has to be caught.
_INHOUSE_GROUP_RE = re.compile(
    r"\b(?:Red Education|our|RedEd)\s+"
    r"(?:instructors?|trainers?|engineers?|experts?|consultants?|team)\b", re.I)
_REPORTING_VERB_RE = re.compile(
    r"\b(?:see|sees|seen|note|notes|noted|observe|observes|observed|report|"
    r"reports|reported|find|finds|found|say|says|said|tell|tells|told|hear|"
    r"hears|heard|encounter|encounters|encountered|notice|notices|noticed|"
    r"emphasise|emphasises|emphasize|emphasizes|recommend|recommends|"
    r"stress|stresses|warn|warns|advise|advises)\b", re.I)


def unnamed_attributions(body: str, known_people: set[str] | None = None
                         ) -> list[Finding]:
    """A claim credited to "our instructors" with nobody named.

    Third person, so it is not the first-person breach REACS already catches,
    and it is not an unsourced external claim either — it reads as authority
    while resting on nobody. Every instance is generic enough to sit on any
    vendor's article unchanged, which is the tell: it is template filler
    wearing the costume of experience.

    A sentence that NAMES someone passes. That is the point — the rule wants
    the observation attributed or dropped, not the words banned, because
    banning the words is what produced three verbs of the same sentence."""
    people = {p.lower() for p in (known_people or set()) if p}
    out: list[Finding] = []
    for sentence in _sentences(body):
        m = _INHOUSE_GROUP_RE.search(sentence)
        if not m:
            continue
        after = sentence[m.end():m.end() + 60]
        if not _REPORTING_VERB_RE.search(after):
            continue
        if any(name in sentence.lower() for name in people):
            continue                       # attributed to a real person
        out.append(("unnamed", m.group(0).strip() + " " +
                    " ".join(after.split()[:3])))
    return out


# Career and employment assertions. V7c fires only when a private name and
# one of these appear together — a name alone is usually a vendor founder,
# an author being cited, or a course instructor, and flagging every capital
# letter pair would make the rule unusable.
# NOTE "career at|spans" and NOT "career with": one of the approved CTA
# phrases is "Advance your career with Red Education", which appears in every
# article by design and matched the old pattern on every single one.
_CAREER_RE = re.compile(
    r"\b(?:joined|previously (?:worked|served|held)|serves as|currently (?:works|serves)|"
    r"his role|her role|their role|career (?:at|spans)|"
    r"worked at|spent \d+ years|appointed|promoted to|is the (?:CEO|CTO|CIO|CISO|"
    r"director|manager|head) of|linkedin)\b", re.I)

# A single literal space, never \s+: with \s+ the pattern happily spans a
# newline and pairs the last word of a heading with the first word of the
# paragraph below it ("Specialisations\nFor").
_PERSON_RE = re.compile(r"\b([A-Z][a-z]{2,})[ ]([A-Z][a-z]{2,})\b")

# No human name is built from these. Capitalised sentence-initial function
# words are how a heading's last word and a paragraph's first word get
# mistaken for a forename and surname.
_NAME_STOPWORDS = {
    "for", "the", "and", "with", "from", "into", "onto", "this", "that",
    "these", "those", "your", "our", "their", "his", "her", "its", "each",
    "every", "some", "any", "all", "both", "more", "most", "many", "much",
    "when", "where", "what", "which", "while", "after", "before", "during",
    "under", "over", "about", "across", "against", "between", "through",
    "beyond", "within", "without", "because", "since", "although", "though",
    "however", "therefore", "unlike", "like", "such", "than", "then", "also",
    "how", "why", "who", "whom", "whose", "not", "but", "yet", "still",
    "understanding", "choosing", "building", "getting", "working", "moving",
    "specialisations", "specialisation", "certifications", "certification",
    "troubleshooting", "administration", "management", "training", "course",
    "courses", "exam", "exams", "engineers", "engineer", "candidates",
}

# How close a name and a career claim must sit to be about each other. A long
# paragraph can easily contain both with no relationship between them.
_CAREER_PROXIMITY = 120

# Capitalised word pairs that are organisations, products or headings rather
# than people. Cheap guard against the obvious false positives.
_NOT_PEOPLE = {
    "red education", "check point", "palo alto", "cortex xsoar", "cortex xsiam",
    "unit 42", "big ip", "smart center", "security expert", "network security",
    "cloud security", "best practice", "best practices", "common mistakes",
    "next steps", "last updated", "exam domains", "study path", "key facts",
    "frequently asked", "getting started", "training partner", "united states",
    "new south", "south wales", "prisma access", "identity services",
}


def named_private_individuals(body: str, known_people: set[str],
                              vendor_terms: list[str]) -> list[Finding]:
    """People named alongside a career or employment claim who are not on the
    permitted list.

    Permitted: the byline instructor, Red Education staff, and public figures
    quoted from an on-the-record source. Everyone else comes out — the live
    case named a private individual, asserted their employment history and
    current role, and linked their professional profile, in service of
    nothing the reader asked for.

    Deliberately conjunctive: a name AND a career assertion within the same
    sentence. Name-only detection would flag every cited author and every
    vendor founder, and this is the one rule here that BLOCKS, so its false
    positive rate matters more than its recall."""
    permitted = {p.lower() for p in known_people}
    terms = [t.lower() for t in vendor_terms if t]
    out: list[Finding] = []
    seen: set[str] = set()
    for sentence in _sentences(body):
        careers = list(_CAREER_RE.finditer(sentence))
        if not careers:
            continue
        for m in _PERSON_RE.finditer(sentence):
            first, last = m.group(1), m.group(2)
            # Function words are not names. This is what stops a heading's
            # last word pairing with the next paragraph's first word.
            if (first.lower() in _NAME_STOPWORDS
                    or last.lower() in _NAME_STOPWORDS):
                continue
            name = f"{first} {last}"
            low = name.lower()
            if (low in permitted or low in _NOT_PEOPLE or low in seen
                    or any(low in t for t in terms)
                    or any(t in low for t in terms)):
                continue
            # The claim has to be ABOUT this name, not merely in the same
            # block of prose.
            if not any(abs(c.start() - m.start()) <= _CAREER_PROXIMITY
                       for c in careers):
                continue
            seen.add(low)
            out.append(("named_individual",
                        f'"{name}" is named alongside a career or employment '
                        "claim and is not the byline instructor or Red "
                        "Education staff"))
    return out


# --------------------------------------------------------------------------- #
# V8 — interested-party sourcing
# --------------------------------------------------------------------------- #

def interested_party_only(body: str, references: list[dict],
                          interested_domains: set[str]) -> list[Finding]:
    """Sentences whose ONLY support is a competitor or vendor-marketing page.

    The worked example: a claim about Palo Alto's XSOAR roadmap sourced only
    from D3 Security, who sell a competing SOAR product. That is a lead
    requiring primary-source confirmation, not a citation, and the difference
    matters commercially. A sentence that also cites a neutral or first-party
    source is fine — corroboration is the whole point, and two hits on the
    same syndicated article is not corroboration either, which is why this
    counts DISTINCT domains."""
    interested = {d.lower() for d in interested_domains}
    if not interested:
        return []
    by_tag = {str(r.get("tag", "")).upper(): (r.get("domain") or "").lower()
              for r in (references or [])}
    out: list[Finding] = []
    seen: set[str] = set()
    for sentence in _sentences(body):
        tags = _cited_tags(sentence)
        domains = {by_tag[t] for t in tags if by_tag.get(t)}
        if not domains:
            continue
        if all(any(d.endswith(i) or i.endswith(d) for i in interested)
               for d in domains):
            key = ",".join(sorted(domains))
            if key in seen:
                continue
            seen.add(key)
            out.append(("interested_party",
                        f"supported only by {key} — an interested party. "
                        "Confirm against a primary source before publication"))
    return out


# --------------------------------------------------------------------------- #
# V10 — cross-article contradiction
# --------------------------------------------------------------------------- #

def corpus_contradictions(tokens: dict[str, int], subjects: list[str],
                          corpus_versions: dict[str, dict[str, str]]) -> list[Finding]:
    """This article's version claims against what the rest of the estate says.

    "Two articles by the same author stating contradictory facts about the
    same event is worse than either being wrong alone." Scoped to version
    claims per certification, which is the one claim type we hold structured
    enough to compare — the standard logs the general case as v1.4 work.

    Fires only where another article has already made a DIFFERENT current-
    version claim about the same certification. With one article on the
    estate this finds nothing, which is correct rather than a stub: it starts
    working the moment there are two."""
    out: list[Finding] = []
    for cert in subjects:
        claimed = corpus_versions.get(cert) or {}
        if not claimed:
            continue
        others = {v: title for v, title in claimed.items() if v not in tokens}
        mine = [t for t in tokens if t not in claimed]
        if mine and others:
            out.append(("contradiction",
                        f"{cert}: this article says "
                        f"{', '.join(sorted(mine))} while "
                        + "; ".join(f'"{t}" says {v}' for v, t in
                                    sorted(others.items()))))
    return out
