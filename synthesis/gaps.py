"""Content-gap and internal-link suggestions, matched against
rededucation.com's own published posts (config.db.PublishedPost, populated by
scrape_published_posts.py).

THE CORE PRINCIPLE applies here too, same as synthesis/topics.py's scoring
formula: relevance is decided by transparent Python logic — never by an LLM
guessing which posts are "similar". Cheap, deterministic, and good enough for
a suggestion surface; nothing here blocks or gates anything downstream.

Matching is IDF-WEIGHTED token overlap, not plain overlap. Plain overlap
weighted every word equally, so two generic words outranked the one word that
actually mattered: a Nutanix NMCAA topic was offered F5 pages, because "F5
Networks certification guide 2026" shares {2026, certification} (score 0.22)
while "Nutanix recognizes Red Education..." shares only {nutanix} (0.11).
"certification" appears across much of the corpus and "nutanix" in a handful,
so rarity is exactly the signal that was missing. The writer then dutifully
linked those F5 pages from a Nutanix article.

Posts are matched on title AND url slug. Half the scraped titles were
Cloudflare's "Just a moment..." interstitial, which carries no signal at all,
while the slug beside it ("preparing-for-f5-certifications") carries the real
keywords. Reading both means a broken title degrades a match instead of
erasing the post from consideration entirely.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import urlparse

from config.db import PublishedPost, Vendor
from .reacs import _entity_alias
from .text import _NEVER_AN_ACRONYM

_EPOCH = datetime(1970, 1, 1)

_STOPWORDS = {
    "a", "an", "the", "and", "or", "for", "to", "of", "in", "on", "with", "is",
    "are", "your", "you", "how", "what", "why", "which", "vs", "guide", "this",
    "that", "from", "will", "can", "do", "does", "it", "be", "at", "as",
    # 2-letter fillers, now that tokens of length 2 are kept (see _tokens)
    "we", "my", "us", "by", "if", "no", "so", "up", "re", "our", "its",
}

# What the site returns instead of the article when a bot check fires, or when
# the page is gone. These carry no signal — treat them as absent and fall back
# to the slug. scrape_published_posts.py now refuses to store them at all.
_UNUSABLE_TITLE_MARKERS = (
    "just a moment", "attention required", "access denied", "forbidden",
    "page not found", "not found", "error 40", "cloudflare", "security check",
    "checking your browser", "enable javascript", "are you a robot",
)


def title_is_unusable(title: str) -> bool:
    """A title carrying no usable signal. Deliberately NOT length-based beyond
    a couple of characters — "Cisco", "F5" and "EPI" are real vendor-page
    titles, and an earlier `len < 8` rule flagged 23 perfectly good rows as
    broken. A bare URL counts as unusable: that is what title_from_url falls
    back to when a slug yields nothing."""
    t = (title or "").strip().lower()
    return (not t) or len(t) < 3 or t.startswith(("http://", "https://")) \
        or any(m in t for m in _UNUSABLE_TITLE_MARKERS)


def slug_words(url: str) -> str:
    """The URL's final path segment as plain words
    ("/preparing-for-f5-certs/" -> "preparing for f5 certs") — the one piece
    of real signal that survives a bot-challenge fetch."""
    path = urlparse(url or "").path.strip("/")
    slug = path.rsplit("/", 1)[-1] if path else ""
    slug = re.sub(r"\.(html?|php|aspx)$", "", slug, flags=re.I)
    return re.sub(r"[-_]+", " ", slug).strip()


def title_from_url(url: str, acronyms: set[str] | None = None) -> str:
    """A readable display title derived from the slug, for rows whose scraped
    title is unusable. Acronyms stay upper-case: `.title()` renders "ccsa" as
    "Ccsa", and these labels are shown to the editor."""
    words = slug_words(url).split()
    if not words:
        return url
    known = acronyms or set()
    out: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        # A hyphenated code ("EDU-210", "ADV-WAF", "BIG-IP") arrives from the
        # slug as two words, so the joined form has to be tried before the
        # single one or it can never match.
        pair = f"{w}-{words[i + 1]}".upper() if i + 1 < len(words) else ""
        if pair and pair in known:
            out.append(pair)
            i += 2
            continue
        if w.upper() in known:                                # CCSE, CCME, PCNSE
            out.append(w.upper())
        elif re.fullmatch(r"[a-z]{1,5}\d+|\d{4}", w):        # f5, r81, 2026
            out.append(w.upper())
        elif (len(w) <= 5 and not re.search(r"[aeiou]", w)
              and w not in _VOWELLESS_WORDS):
            # Consonant-only short tokens (SD, CP, TS). A fallback for codes
            # the Vendor KB doesn't know — NOT the main rule: every real
            # certification acronym contains a vowel, which is exactly why
            # this alone produced "Ccse".
            out.append(w.upper())
        else:
            out.append(w.capitalize())
        i += 1
    return " ".join(out)


# English words with no a/e/i/o/u, which the consonant fallback below would
# otherwise shout. Small by nature — this is the whole realistic set for
# URL slugs. "…Certification Launched BY Check Point" is the bug they cause.
_VOWELLESS_WORDS = {"by", "my", "why", "try", "dry", "fly", "gym", "myth",
                    "thy", "shy", "sky", "spy", "cry", "fry", "ply", "wry"}

_ACRONYM_TOKEN_RE = re.compile(r"[A-Z0-9][A-Z0-9\-]{1,11}$")


def known_acronyms(session) -> set[str]:
    """Uppercase acronyms and exam codes from the Vendor Knowledge Base.

    Exists because guessing which slug words are acronyms cannot work. The
    previous rule uppercased a short token only if it had NO vowel — and every
    certification acronym here has one: CCSA, CCSE, CCTE, CCME, CCTA, CCES,
    PCNSE, NMCAA. So anchors published as "Check Point Certified Security
    Expert Ccse". The code comment even claimed to handle "ccsa-style codes",
    which it never did.

    The KB already holds the correct spelling, so read it instead of
    inferring: parenthesised acronyms in a certification or course name, the
    exam_code column, and any certification whose whole name IS an acronym
    ("PCNSE"). Multi-word parentheticals ("Advanced Configuration") are
    skipped — they are descriptions, not acronyms."""
    from config.db import Certification, Course

    out: set[str] = set()

    def add(token: str | None) -> None:
        """Only tokens the KB ITSELF spells in caps.

        Casing in the source is the signal that separates an acronym from an
        ordinary word: the KB stores "PCNSE" and "EDU-210" in caps but
        "Management" and "iRules" as written. Accepting any short name
        produced "Privileged Access MANAGEMENT" and "The Unpolished Gem Of
        IRULES" — a spelling error introduced while fixing a spelling
        error."""
        tok = (token or "").strip()
        if tok != tok.upper():
            return
        if tok in _NEVER_AN_ACRONYM:
            return
        if 2 <= len(tok) <= 12 and _ACRONYM_TOKEN_RE.fullmatch(tok):
            out.add(tok)

    for rows, has_code in ((session.query(Certification).all(), True),
                           (session.query(Course).all(), False)):
        for row in rows:
            name = row.name or ""
            for inner in re.findall(r"\(([^)]+)\)", name):
                add(inner)
            add(name)                       # a name that IS an acronym
            # ALL-CAPS words INSIDE a mixed-case name. "Cortex XSIAM:
            # Security Operations" holds XSIAM, and nothing was reading it —
            # the article opened "The cortex xsiam vs siem migration is", in
            # lower case, because none of XSIAM, XSOAR, XDR or SSE was ever
            # in this set. They are only ever spelled in caps in the KB, which
            # is the same signal add() already trusts.
            #
            # Guarded on the name being MIXED case. An all-caps name would
            # otherwise donate every word it contains, and we would publish
            # "the ESSENTIALS of" — the exact class of error add()'s own
            # docstring warns about.
            if name and name != name.upper():
                for word in re.findall(r"\b[A-Z][A-Z0-9-]{1,11}\b", name):
                    add(word)
            if has_code:
                add(getattr(row, "exam_code", None))
    return out | INDUSTRY_ACRONYMS


# Acronyms every security article uses that no vendor's catalogue defines.
# SIEM is the one that prompted this: it appeared as "siem" in an opening
# sentence, and it cannot come from the Vendor KB because it is not any
# vendor's product.
#
# DELIBERATELY CONSERVATIVE. Every entry here is uppercased wherever it
# appears as a whole word in prose, so an entry that is also an ordinary
# English word would corrupt text — "IT" would rewrite the pronoun "it", and
# "AS", "OR" and "IS" are worse. Nothing goes in this set unless it is never
# a word in its own right. Vendor-specific acronyms do NOT belong here; they
# come from the KB above, where a human already curated the spelling.
INDUSTRY_ACRONYMS: set[str] = {
    "SIEM", "SOAR", "SASE", "SSE", "XDR", "EDR", "NDR", "MDR", "TDIR",
    "NGFW", "WAF", "ZTNA", "CASB", "DLP", "MFA", "SSO", "IAM", "PAM",
    "SOC", "VPN", "DNS", "DHCP", "TLS", "SSL", "SD-WAN", "MTTR", "MTTD",
}


def _tokens(text: str) -> set[str]:
    """Meaningful tokens, keeping 2-character ones.

    The old `len(w) > 2` floor silently deleted "f5" from BOTH queries and post
    titles, so F5 — a vendor with 39 pages in the corpus — could not be matched
    at all and its internal-link menu came back empty. The same floor lost
    "ip", "ai", "os", "ha" and "vm", every one of which is a real term in this
    domain. Two-letter fillers are handled by _STOPWORDS instead, which is
    where that decision belongs; and IDF already discounts anything common."""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if w not in _STOPWORDS and len(w) >= 2}


def _post_tokens(post: PublishedPost) -> set[str]:
    """Match on title AND slug — see the module docstring for why the slug
    alone is often all the signal there is."""
    title = "" if title_is_unusable(post.title) else post.title
    return _tokens(f"{title} {slug_words(post.url)}")


def apply_acronym_case(title: str, acronyms: set[str] | None) -> str:
    """Spell known acronyms in caps inside ANY display title.

    Not limited to slug-derived titles: the site's own course pages carry
    titles like "Check Point Security Administration Ccsa", and that string
    becomes the anchor text a writer copies into an article. We control the
    anchor, and the Vendor KB knows the correct spelling, so use it.

    Whole words only, and only words the KB lists — a title that already
    reads "Check Point CCSE vs Palo Alto NGFW Engineer 2026" is untouched."""
    if not acronyms:
        return title
    return re.sub(r"[A-Za-z][A-Za-z0-9]*",
                  lambda m: m.group(0).upper() if m.group(0).upper() in acronyms
                  else m.group(0),
                  title or "")


@dataclass
class RelatedPost:
    post: PublishedPost
    score: float   # share of the query's IDF WEIGHT this post covers
    # Vendor-KB acronyms, so a title rebuilt from a slug spells CCSE, not
    # "Ccse". Optional: callers without a session still get the old behaviour.
    acronyms: set[str] | None = None

    @property
    def display_title(self) -> str:
        """What the editor should see — never the bot-challenge placeholder."""
        title = (title_from_url(self.post.url, self.acronyms)
                 if title_is_unusable(self.post.title) else self.post.title)
        return apply_acronym_case(title, self.acronyms)


def _idf(corpus: list[set[str]]) -> dict[str, float]:
    """Inverse document frequency per token across the published corpus. A
    word in most posts ("certification", "training") is worth little; one in a
    handful ("nutanix", "nmcaa") is worth a lot. Smoothed, so a token present
    in every single post still scores above zero rather than vanishing."""
    n = len(corpus) or 1
    df: dict[str, int] = {}
    for toks in corpus:
        for t in toks:
            df[t] = df.get(t, 0) + 1
    return {t: math.log(1 + n / (1 + c)) for t, c in df.items()}


# Everything worth linking TO from an article. Courses first in spirit: both
# gates ask for course links (REACS internal_linking, QA course_link_cta).
LINKABLE_KINDS = ("post", "course", "page", "case_study")
# Evidence that an ARTICLE already covers a topic. Posts ONLY — a course page
# existing says nothing about whether we have written about it, and counting
# one would tell the editor to skip a topic worth writing.
ARTICLE_KINDS = ("post",)


def related_posts(session, query_text: str, limit: int = 5,
                  min_score: float = 0.2,
                  kinds: tuple[str, ...] = LINKABLE_KINDS) -> list[RelatedPost]:
    """Rank published pages by IDF-weighted token overlap with `query_text` (a
    topic title, topic keyword, or brief keyword). Returns at most `limit`
    rows scoring >= min_score, best first — used for the Brief step's
    internal-link suggestions, where a loose match is still a useful
    candidate.

    `kinds` scopes which content types are eligible. IDF is computed over the
    SCOPED corpus, not the whole table, so "certification" being ubiquitous
    among courses doesn't distort scores for a posts-only query.

    Score = matched IDF weight / total query IDF weight, so it stays a 0-1
    "how much of what matters did this post cover" figure that is comparable
    across queries."""
    query_tokens = _tokens(query_text)
    if not query_tokens:
        return []

    # Harvested ONCE per call, not per row — display_title needs it to spell
    # CCSE rather than "Ccse" when a title is rebuilt from a slug.
    acronyms = known_acronyms(session)

    q = session.query(PublishedPost)
    if kinds:
        q = q.filter(PublishedPost.kind.in_(kinds))
    posts = q.all()
    if not posts:
        return []
    corpus = [_post_tokens(p) for p in posts]
    idf = _idf(corpus)
    # A query token in NO post is maximally rare, so weight it like one — not
    # zero, which would let a query match on its generic words alone.
    default_w = math.log(1 + len(corpus))
    total_w = sum(idf.get(t, default_w) for t in query_tokens) or 1.0

    results = []
    for post, post_tokens in zip(posts, corpus):
        overlap = query_tokens & post_tokens
        if not overlap:
            continue
        score = sum(idf.get(t, default_w) for t in overlap) / total_w
        if score >= min_score:
            results.append(RelatedPost(post=post, score=round(score, 3),
                                       acronyms=acronyms))
    # NOTE: no slice here — _diverse below needs the full ranked list to have
    # replacements available when it drops a near-duplicate.
    # Tie-break on recency: between two equally relevant posts, the newer one
    # is the better thing to link.
    results.sort(key=lambda r: (r.score, r.post.published_at or _EPOCH),
                 reverse=True)
    return _diverse(results, limit)


def _diverse(ranked: list[RelatedPost], limit: int,
             overlap_cap: float = 0.5) -> list[RelatedPost]:
    """Take the top `limit`, skipping candidates that say the same thing as one
    already taken.

    The same course exists under several URLs across sitemaps
    (/product/check-point-security-administration-ccsa-ccsa_ind/,
    /product/check-point-ccsa-r82-a1/, /checkpoint/check-point-certified-
    security-administrator-ccsa/), and each scores identically. Without this,
    all five slots went to variants of ONE course and the editor saw no
    genuine choice. A candidate is dropped when `overlap_cap` or more of its
    tokens are already covered by a chosen one — near-duplicates go, a
    genuinely different page on the same subject stays.

    0.5 chosen by measurement, not feel: across CCSA/PCNSE/F5/Avaya/Nutanix
    queries it still fills all five slots (so it never starves the list) while
    giving the best course/post mix — 0.8 handed three of five slots to
    variants of one course."""
    chosen: list[RelatedPost] = []
    taken: list[set[str]] = []
    for cand in ranked:
        toks = _post_tokens(cand.post)
        if toks and any(len(toks & seen) / len(toks) >= overlap_cap for seen in taken):
            continue
        chosen.append(cand)
        taken.append(toks)
        if len(chosen) >= limit:
            break
    return chosen


def best_match(session, query_text: str, min_score: float = 0.5,
               kinds: tuple[str, ...] = ARTICLE_KINDS) -> RelatedPost | None:
    """The single closest published post, or None. A HIGHER bar than
    related_posts uses: this drives the Topic step's "we already have this"
    content-gap tag, where a false positive tells the editor to skip a topic
    worth writing.

    Raised from 0.34 to 0.5 alongside the move to IDF weighting. Under plain
    overlap 0.34 was reachable on generic words alone — the exact failure this
    module's docstring describes — whereas an IDF score of 0.5 means the post
    covers half the query's actual distinguishing weight."""
    matches = related_posts(session, query_text, limit=1, min_score=min_score,
                            kinds=kinds)
    return matches[0] if matches else None


def _distinctive_vendor_tokens(session, vendor_name: str) -> set[str]:
    """The tokens that actually IDENTIFY this vendor, not merely appear in its
    name.

    The vendor-relevance gate below accepts a page if it shares any vendor
    token — and "networks" is in Palo Alto Networks, Extreme Networks AND
    Arista Networks, so Palo Alto pages qualified as "about" Arista. Measured:
    6 of the 8 entries in Arista's approved-link menu were Palo Alto or Check
    Point pages, which is precisely the failure this menu exists to prevent
    (the writer links what it is offered).

    Which words are generic is derived from the vendor table itself rather
    than hardcoded: any token appearing in more than one vendor's name cannot
    distinguish between them. Falls back to the full token set if that would
    leave nothing — a vendor named only with shared words still needs to match
    something."""
    names = [v.name for v in session.query(Vendor.name).all()]
    shared: dict[str, int] = {}
    for n in names:
        for tok in _tokens(n):
            shared[tok] = shared.get(tok, 0) + 1
    tokens = _tokens(vendor_name)
    distinctive = {tok for tok in tokens if shared.get(tok, 0) <= 1}
    return distinctive or tokens


# A dated announcement stops being a useful internal link once the date has
# passed. An article linked readers to "a free webinar ... EDU-270" whose
# target was a rededucation.com page from October 2024 announcing a course as
# forthcoming — the course has since been retired and the webinar long since
# happened.
_EVENT_PAGE_RE = re.compile(r"\b(?:webinar|live\s*session|register\s*now|"
                            r"upcoming|join\s+us)\b", re.I)
_EVENT_STALE_DAYS = 365


def _unlinkable(post, retired_terms: set[str], today: date | None = None) -> str | None:
    """Why this published page must not go on the approved-link menu, or None.

    Two reasons, both about pages that are REAL and still resolve — which is
    why nothing upstream catches them. A page for a retired course is a live
    page selling something we no longer run, and a webinar announcement from
    two years ago is a live page inviting a reader to an event that is over.

    Retirement is matched against the KB's RETIRED certifications, so it only
    knows what the knowledge base has been told. EDU-270, the course that
    prompted this, is not in the KB at all — the event rule is what catches
    that one, and the honest limit of the retirement rule is worth stating:
    it cannot exclude what nobody recorded."""
    title_url = f"{post.title or ''} {post.url or ''}".lower()
    for term in retired_terms:
        if term and term in title_url:
            return f"page for retired course/certification ({term})"
    if _EVENT_PAGE_RE.search(title_url):
        when = getattr(post, "published_at", None)
        if when is None:
            return "undated event announcement"
        age = ((today or date.today()) - when.date()
               if hasattr(when, "date") else (today or date.today()) - when).days
        if age > _EVENT_STALE_DAYS:
            return f"event announcement {age} days old"
    return None


def _retired_link_terms(session, vendor_name: str) -> set[str]:
    """Lower-cased names and codes of this vendor's RETIRED certifications."""
    from config.db import Certification, CertificationStatus, Vendor

    v = session.query(Vendor).filter(Vendor.name == vendor_name).first()
    if v is None:
        return set()
    out: set[str] = set()
    for c in session.query(Certification).filter(
            Certification.vendor_id == v.id,
            Certification.status == CertificationStatus.RETIRED).all():
        if c.exam_code and len(c.exam_code) >= 4:
            out.add(c.exam_code.lower())
        alias = _entity_alias(c.name) or c.name
        # >= 5, not 6: PCNSE and PCNSA are five characters, are exactly the
        # retired courses a Palo Alto article would reach for, and a 6-char
        # floor silently excluded both — the rule looked like it worked while
        # matching nothing at all for that vendor.
        if alias and len(alias) >= 5:
            out.add(alias.lower())
    return out


def internal_link_menu(session, vendor_name: str, keyword: str = "",
                       limit: int = 12,
                       entities: list[str] | None = None) -> list[tuple[str, str]]:
    """(title, url) pairs of REAL internal pages for this vendor — the menu the
    writer is allowed to link to.

    Exists because the writer was told to name specific courses ("65476V
    Administering Avaya Infinity") while being given URLs for almost none of
    them, and told not to invent any. It resolved that by pointing ten
    different anchors at two URLs: the bare homepage and /avaya/. Anchor text
    said "65476V Administering Avaya Infinity"; the href was the front page.

    Only pages whose own text names the vendor are offered, so an Avaya
    article is never handed a Check Point course. Ordered by relevance to the
    keyword, courses included, so the single most useful link is first.

    `entities` (the SAME Vendor KB names passed to the writer's separate
    "cover these entities" instruction — see webapp/pipeline.py's
    _vendor_entities) each get a GUARANTEED lookup, appended after the
    keyword-ranked results. Without this, a real Panorama/Cortex-XDR/XSOAR
    course page could exist in the DB — 10, 14 and 5 matches respectively,
    confirmed live — and still never reach the menu, because the keyword-only
    ranking only reflects relevance to the ARTICLE's overall topic, not to
    every entity the writer is independently told to mention. The writer would
    then correctly follow "use only the menu" and correctly have no entry for
    Panorama on it — landing on the nearest-sounding wrong page, which reads
    as non-compliance but is actually a menu that was missing the one link
    that mattered."""
    vendor_tokens = _distinctive_vendor_tokens(session, vendor_name)
    if not vendor_tokens:
        return []
    acronyms = known_acronyms(session)
    ranked = related_posts(session, f"{vendor_name} {keyword}".strip(),
                           limit=limit * 4, min_score=0.05)
    # The entity pass scans EVERY linkable page, not just the keyword-ranked
    # pool. Restricting it to that pool was measurably lossy: 57 real entity
    # pages across 10 vendors existed in the table but could never reach a
    # menu, because relevance to the article's keyword and "is this the page
    # for course X" are different questions — a Prisma Browser course page is
    # not keyword-relevant to a PCNSE article, yet is exactly the right link
    # if the writer names Prisma Browser. Still ONE query and one token pass,
    # so it stays ~6ms rather than the 140ms a per-entity query cost.
    all_posts = session.query(PublishedPost).filter(
        PublishedPost.kind.in_(LINKABLE_KINDS)).all()
    tokens_by_url = {p.url: _post_tokens(p) for p in all_posts}
    posts_by_url = {p.url: p for p in all_posts}
    for r in ranked:                       # ranked rows carry display_title
        tokens_by_url.setdefault(r.post.url, _post_tokens(r.post))
        posts_by_url.setdefault(r.post.url, r.post)

    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    retired_terms = _retired_link_terms(session, vendor_name)

    def _take(post) -> bool:
        """Add a page if it's vendor-relevant, new, current, and there's room."""
        if len(out) >= limit:
            return False
        # A page can be real, resolve, and still be the wrong thing to link:
        # a retired course, or an event that has already happened.
        if _unlinkable(post, retired_terms):
            return False
        toks = tokens_by_url[post.url]
        # The page must actually be about this vendor — a shared generic word
        # ("certification") is not enough to put it on an approved-link menu.
        if not (vendor_tokens & toks):
            return False
        url = post.url.rstrip("/") + "/"
        if url in seen:
            return False
        seen.add(url)
        out.append((RelatedPost(post=post, score=0.0,
                                acronyms=acronyms).display_title, url))
        return True

    # ENTITIES FIRST, and matched against the pool already fetched above.
    # First because these are the pages the writer is separately instructed to
    # NAME, so they are the links it will actually reach for — letting the
    # broader keyword ranking fill every slot first is what left Panorama /
    # Cortex XDR / XSOAR off the menu entirely. Capped at half the menu so a
    # vendor with 25 KB entities can't crowd out the general vendor pages that
    # anchors like "<vendor> training" need.
    #
    # Matched locally rather than with a related_posts() call per entity: that
    # was 26 full-corpus queries + IDF rebuilds to build ONE menu (measured
    # 140ms vs 7ms), all re-reading the same rows this pool already holds.
    # Subset test (every alias token present in the page) is also a tighter
    # "this page IS that course" test than the fuzzy score threshold it
    # replaces.
    #
    # Reserve only 2 slots rather than half the menu. A `limit // 2` cap was
    # tried and silently re-broke the bug this pass exists to fix: with 25 KB
    # entities the first 6 alphabetical matches ate the allowance and Panorama
    # / XSOAR fell off the menu again. Measured across five vendors, only 3-8
    # entities ever resolve to a real page (never close to 25), so the
    # crowding-out risk that cap defended against does not occur — while
    # starving genuine matches demonstrably does.
    entity_slots = max(2, limit - 2)
    # Most specific entity FIRST. The KB stores both a bare and a
    # vendor-prefixed form of the same item ("EDU-210 Firewall Essentials..."
    # and "Palo Alto Networks EDU-210 Firewall Essentials..."), and in list
    # order the bare one went first and claimed the slot on a thinner match,
    # leaving the prefixed variant's better candidate unused. The longer name
    # discriminates better, so let it choose first.
    #
    # `claimed` then skips the twin outright: both forms describe ONE real
    # course, so letting each take a slot would spend two of the menu's
    # twelve on the same thing.
    ordered = sorted(entities or [], key=lambda e: len(_tokens(e)), reverse=True)
    claimed: set[frozenset] = set()
    for entity in ordered:
        if len(out) >= entity_slots:
            break
        alias = _entity_alias(entity) or entity
        if len(alias) < 4:
            continue
        # Subset test, not equality: the colon-form twins ("Cortex XSIAM:
        # Security Operations" vs "Palo Alto Networks Cortex XSIAM: ...")
        # alias to "Cortex XSIAM" and "Palo Alto Networks Cortex XSIAM", so an
        # exact-match dedup let both through and spent two menu slots on one
        # course. One alias containing the other means the same entity.
        alias_key = frozenset(_tokens(alias))
        if any(alias_key <= c or c <= alias_key for c in claimed):
            continue
        claimed.add(alias_key)
        alias_tokens = _tokens(alias)
        if not alias_tokens:
            continue
        # RANK the candidates — don't take whichever row the DB returned
        # first. The alias subset test is satisfied just as well by a page
        # that merely MENTIONS the course as by the course's own page, and
        # first-in-DB-order was picking the wrong one: for "EDU-210 Firewall
        # Essentials..." it offered the blog post "EDU-210 to EDU-330: Top
        # Palo Alto Firewall Courses Ranked" while all six real EDU-210
        # course pages stayed off the menu. The writer then had no correct
        # URL for an anchor naming that course, pointed it at the nearest
        # menu entry (EDU-330's page), and internal_link_targets warned about
        # a mismatch no rewrite could fix — the menu simply had no right
        # answer on it.
        #
        # Rank by how much of the FULL entity name the page covers, which is
        # what separates "this page IS the course" from "this page mentions
        # it". kind=="course" breaks ties toward the product page, and fewer
        # total tokens breaks the rest toward the more specific page.
        entity_tokens = _tokens(entity) or alias_tokens
        candidates = [p for p in all_posts
                      if alias_tokens <= tokens_by_url[p.url]]

        def _entity_rank(p, _et=entity_tokens):
            toks = tokens_by_url[p.url]
            return (len(_et & toks) / len(_et),
                    str(p.kind) == "course",
                    -len(toks))

        for post in sorted(candidates, key=_entity_rank, reverse=True):
            if _take(post):
                break

    for r in ranked:
        if len(out) >= limit:
            break
        _take(r.post)

    # REACS's internal_linking is MANDATORY and wants 2 DISTINCT targets. A
    # vendor with no pages of its own (Infoblox, Ivanti, Sophos have none)
    # would make that unsatisfiable — and an unsatisfiable mandatory check is
    # exactly the pressure that produced fabricated homepage links in the
    # first place. Top up with real, verified site-wide pages rather than
    # leaving the writer to invent something.
    for title, path in (("Red Education certification training", "/certifications/"),
                        ("Red Education training services", "/training-services/")):
        if len(out) >= 2:
            break
        full = "https://www.rededucation.com" + path
        if full not in seen:
            seen.add(full)
            out.append((title, full))
    return out


def published_counts_by_vendor(session,
                               kinds: tuple[str, ...] = ARTICLE_KINDS) -> dict[int, int]:
    """{vendor_id: how many PUBLISHED pages on the site are about that vendor}.

    Attribution reuses _distinctive_vendor_tokens — the same rule that decides
    which pages may appear on a vendor's internal-link menu — so a page counted
    here is a page the writer could actually link. "Networks" alone will not
    match, because it is shared across three vendor names.

    A page CAN count for two vendors ("Check Point CCSE vs Palo Alto NGFW"
    genuinely covers both) and that is deliberate, not double-counting to be
    corrected: the number answers "how much of this vendor exists on the site",
    not "how do posts divide up".

    One query and one token pass over the corpus, so it stays cheap enough to
    run on every page load of the vendor picker.
    """
    vendors = session.query(Vendor).all()
    vendor_tokens = {v.id: _distinctive_vendor_tokens(session, v.name) for v in vendors}
    counts = {v.id: 0 for v in vendors}
    for post in session.query(PublishedPost).filter(
            PublishedPost.kind.in_(kinds)).all():
        toks = _post_tokens(post)
        for vid, vtoks in vendor_tokens.items():
            if vtoks & toks:
                counts[vid] += 1
    return counts
