"""Shared orchestration between the classic admin pages (webapp/topics.py,
webapp/articles.py) and Article Studio (webapp/studio.py) — the actual
LLM-calling, governance-gated, DB-writing logic both need identically.
Extracted so the two UIs can't silently drift apart on governance behavior
(the duplicate-keyword filter, the "verified facts must be non-empty" gate,
etc.) — a real risk once the same rules are implemented twice.

Framework-agnostic: takes a SQLAlchemy session + plain values, returns plain
result objects, and raises on failure. Two kinds of failure, deliberately
distinguished so callers can reproduce each old route's exact message
formatting:
  - PipelineError: a deliberate, user-facing governance/validation message
    (e.g. "Only topics approved as 'create' can receive a brief.") — callers
    show `str(exc)` as-is, no exception-type prefix.
  - anything else (network/LLM/parse errors, including synthesis/llm.py's own
    plain RuntimeError("No LLM key set...")) — callers pass it through
    `safe_error()` below, which logs the full traceback server-side and returns
    only a generic, non-sensitive string to reflect into the page/URL (the raw
    text can embed provider/HTTP response fragments).
Callers must catch PipelineError BEFORE the generic Exception handler.
"""
from __future__ import annotations

import difflib
import logging
import re
import threading
from dataclasses import asdict, dataclass, replace

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)

from config.db import (Article, ArticleStatus, Certification, CertificationStatus,
                       Instructor, InstructorVendor, Product, Topic, TopicPriority, Vendor)
from synthesis.brief import (ArticlePackage, BriefFaq, ContentBrief, apply_patch,
                             build_article_prompt, build_section_rewrite_prompt,
                             count_words, generate_brief,
                             generate_passing_article, parse_article_response,
                             parse_patch_response, parse_section_response,
                             regenerate_title, replace_section, split_blocks)
from synthesis.llm import complete_with_configured_provider
from synthesis.gaps import internal_link_menu, known_acronyms
from synthesis.prompt import clamp_claim, usable_claims
from synthesis.reacs import (ReacsCheck, ReacsReport, compute_reacs,
                             pick_instructor, reacs_feedback_block)
from synthesis.text import (article_slug, fix_writing_mechanics,
                            refresh_last_updated, slugify)
from synthesis.topics import (TOPIC_SYSTEM_PROMPT, build_topic_prompt,
                              near_duplicate_of, parse_topic_response,
                              score_topic)

LIVE_SOURCE_LIMIT = 8

_brief_creation_locks: dict[int, threading.Lock] = {}
_brief_creation_locks_guard = threading.Lock()


class PipelineError(Exception):
    """A deliberate governance/validation failure with a user-facing message."""


def _require_commercial_vendor(vendor: Vendor, action: str) -> None:
    """Stop commercial workflow actions for source-only/former vendors."""
    if vendor.priority is None:
        raise PipelineError(
            f"{vendor.name} is research-only in the Vendor Centre, so it "
            f"cannot {action}. Set a commercial priority only if Red Education "
            "is currently authorised to market that vendor's training.")


def safe_error(exc: Exception) -> str:
    """Log the full exception server-side; return a string safe to reflect
    into a page/URL.

    PipelineError messages are curated and callers show them verbatim — this
    is only for the generic `except Exception` path, whose raw text can embed
    provider/HTTP response fragments (URLs, response bodies) we don't want
    echoed back into the browser or the redirect query string. The exception
    class name is not sensitive and is kept as a debugging breadcrumb; the
    full traceback goes to the server log for the operator."""
    logger.exception("Unhandled error during a pipeline/generation operation")
    return (f"{type(exc).__name__} — the operation failed. "
            "Check the server logs for details.")


def _retrieval_query(topic_title: str, keyword: str,
                    theme: str | None = None) -> str:
    """The keyword, plus any distinctive SUBJECT words the title carries that
    the keyword doesn't.

    Keyword-only search was losing the point of themed topics outright. The
    topic "How Check Point Acquisitions Are Reshaping Automated Security
    Workflows" had keyword "check point security automation", so retrieval
    never searched for acquisitions at all — it returned a SANS log-analysis
    paper, a Pearson VUE exam page and a Glassdoor company profile, and the
    finished article contained not one word about an acquisition. The writer
    was not ignoring the brief; nothing it was given mentioned the subject.

    Keeping the keyword as the spine preserves the reason it was preferred
    over the title in the first place (headline phrasing like "Simplifying
    Distributed Security: Why Panorama is Essential" pulls loose, off-topic
    hits). Only the words the keyword is MISSING are added, capped at 4 so a
    long headline can't dilute the query it is augmenting.

    Uses the SAME distinctive_terms() the QA title-coverage check uses, so
    what we search for and what we later require the article to cover can
    never drift apart."""
    from synthesis.text import distinctive_terms

    if not keyword:
        return topic_title
    kw_tokens = set(re.findall(r"[a-z0-9]+", keyword.lower()))
    # The THEME goes first among the additions. It is the thing the operator
    # explicitly asked for, so if anything gets cut by the 4-term cap it
    # should not be this: an "acquisitions" batch whose keyword omitted the
    # word searched for no acquisitions and wrote about none.
    extra = distinctive_terms(theme or "", exclude=kw_tokens)
    for w in distinctive_terms(topic_title, exclude=kw_tokens):
        if w not in extra:
            extra.append(w)
    return f"{keyword} {' '.join(extra[:4])}".strip() if extra else keyword


def _assert_sources_cover_subject(bundle, topic_title: str, keyword: str) -> None:
    """Refuse to proceed when retrieval found NOTHING about what the topic is
    actually about.

    This is the gate that was missing when "How Check Point Acquisitions Are
    Reshaping Security Automation Workflows" was written from a SANS
    log-analysis paper, a Pearson VUE exam page and a Glassdoor company
    profile — not one of which mentions an acquisition. Every downstream
    check passed, because each asks whether the prose is sound, sourced and
    on-keyword, and generic automation prose is all three. The article was
    confidently, fluently about the wrong thing.

    Failing HERE is the point: it happens before an LLM call is spent, and it
    is the only check that cannot be satisfied by padding. A body-level check
    can always be cleared by adding one sentence containing the word (measured
    — 120 sentences of generic prose plus "Check Point has made acquisitions."
    passes title_subject_coverage), which is exactly the shortcut a targeted
    fix would find. Sources either discuss the subject or they don't.

    Only THEMATIC terms are required (see synthesis/text.thematic_terms):
    requiring every title word would have blocked 3 of 4 real articles on
    'worth', 'objectives', 'plan' and 'domains' — words describing the
    article's shape, which no source ever says. Certification guides yield no
    thematic terms at all, so this never fires on them."""
    from synthesis.text import thematic_terms

    # No sources at all is a DIFFERENT failure from sources that miss the
    # subject, and conflating them misdirects the fix. The old wording said
    # "None of the 0 page(s) retrieved mention ..." and told the user to
    # narrow the topic, when nothing had been retrieved to narrow against.
    if not bundle.sources:
        raise PipelineError(
            "Retrieval found no usable sources for this topic at all — not "
            "even off-topic ones. The search is restricted to this vendor's "
            "allowlisted domains, so a very specific topic can match nothing. "
            "Try a broader topic or keyword, or add a domain that covers it "
            "to the allowlist.")

    terms = thematic_terms(topic_title, set(re.findall(r"[a-z0-9]+", (keyword or "").lower())))
    if not terms:
        return
    corpus = " ".join(f"{s.title or ''} {s.text or ''}" for s in bundle.sources).lower()
    uncovered = [t for t in terms
                 if t not in corpus and t.rstrip("s") not in corpus]
    # Only block on a TOTAL miss. One thematic term unsupported while others
    # are well covered is a thin article, not a wrong one, and blocking it
    # would make the gate the thing that stops legitimate work — the failure
    # mode this codebase keeps re-learning with unsatisfiable checks.
    if len(uncovered) < len(terms):
        return
    raise PipelineError(
        "Couldn't source this topic. None of the "
        f"{len(bundle.sources)} page(s) retrieved mention "
        + ", ".join(f'"{t}"' for t in uncovered)
        + " — the subject this topic is built on. Writing now would produce a "
          "confident article about something else, which is exactly what "
          "happened before this check existed. Narrow the topic to what the "
          "sources can support, reword its keyword to name the subject, or "
          "add a domain that covers it to the allowlist.")


def _live_bundle(topic_title: str, region: str | None = None,
                 search_query: str | None = None,
                 subject_of: str | None = None,
                 vendor_name: str | None = None):
    """Live retrieve()+verify(), called fresh at EACH stage transition (brief,
    then again at write) rather than reusing a stale snapshot.

    `search_query` (the topic's KEYWORD when the caller has it) is used for
    retrieval — the marketing-headline `topic_title` makes a poor search query
    (e.g. "Simplifying Distributed Security: Why Panorama is Essential for
    Scaling" pulls loosely-related off-topic hits, starving the writer of
    grounded material), whereas the keyword ("palo alto panorama management")
    is the vendor-named canonical phrase. `topic_title` stays the bundle's
    subject (used downstream as the article topic + in currency/recency
    checks), so only search relevance changes, not what the article is about."""
    from retrieval import retrieve
    from verification import verify

    query = search_query or topic_title
    sources, dropped = retrieve(query, limit=LIVE_SOURCE_LIMIT,
                                vendor_name=vendor_name)
    # An augmented query can be TOO narrow. _retrieval_query appends up to 4
    # subject terms, and on a niche topic that stacks 9 terms behind a site:
    # restriction and matches nothing — measured: "avaya ip office
    # certification 77202v platform configuration support domains" returned 0
    # results while the bare keyword returned a real page. Falling back to the
    # keyword keeps the subject-augmentation win without turning it into a
    # dead end. Costs one extra search only when the first found nothing.
    bundle = verify(topic_title, region, sources, dropped_in=dropped,
                    vendor_name=vendor_name)
    # Fall back on the VERIFIED bundle, not the raw hit list. Checking the raw
    # list missed the common case: the augmented query returns one or two
    # pages that verification then drops (stale, or deduped to one per
    # domain), leaving an empty bundle while the fallback never fires. Two
    # Avaya topics failed exactly this way — retrieval "worked", verification
    # emptied it, and the keyword query that would have succeeded was never
    # tried.
    if not bundle.sources and subject_of and subject_of != query:
        logger.info("bundle empty after verification; retrying on the keyword alone")
        sources, dropped = retrieve(subject_of, limit=LIVE_SOURCE_LIMIT,
                                    vendor_name=vendor_name)
        bundle = verify(topic_title, region, sources, dropped_in=dropped,
                        vendor_name=vendor_name)
    # `subject_of` is the topic's KEYWORD; passing it opts this bundle into
    # the subject gate below. Both live callers pass it — it is a parameter
    # rather than a hard-coded call so _bundle_from_references (rebuilding a
    # written article's OWN sources for a section rewrite or targeted fix)
    # can't be blocked by it. Gating a repair on the same miss would leave a
    # bad article with no way to fix it.
    if subject_of is not None:
        _assert_sources_cover_subject(bundle, topic_title, subject_of)
    return bundle


def _serp_snapshot(keyword: str):
    """Live competitor snapshot for the QA harness's serp_competitiveness
    check. Best-effort by contract: any failure (no key, network, empty SERP)
    returns None and the check simply doesn't run — external SERP state must
    never break a write."""
    try:
        from retrieval.serp import competitor_snapshot
        return competitor_snapshot(keyword)
    except Exception:
        logger.exception("competitor snapshot failed; continuing without it")
        return None


def _measure_difficulty(ideas: list) -> None:
    """#1 real-keyword-data (Serper proxy route): replace each idea's
    LLM-guessed `difficulty` with a measured score from the keyword's live
    top-10 (authority-domain density + exact-title matches), recording both
    numbers in the rationale for transparency. In-place, best-effort: any
    keyword whose SERP can't be read keeps the model's estimate — topic
    generation must never fail because of this enrichment. Parallelised
    because a batch is up to 25 keywords = 25 Serper queries."""
    from concurrent.futures import ThreadPoolExecutor

    try:
        from retrieval.serp import serp_difficulty
        with ThreadPoolExecutor(max_workers=8) as ex:
            measured = list(ex.map(
                lambda i: serp_difficulty(i.keyword), ideas))
    except Exception:
        logger.exception("SERP difficulty measurement failed; keeping LLM estimates")
        return
    for idea, result in zip(ideas, measured):
        if result is None:
            continue
        score, detail = result
        idea.rationale = (f"{idea.rationale} · difficulty {score} measured "
                          f"from live SERP ({detail}); model estimated "
                          f"{idea.difficulty}")[:500]
        idea.difficulty = score


def _known_link_urls(session, vendor_name: str = "") -> dict[str, str]:
    """{url: title} for every internal page an article is allowed to link: the
    scraped PublishedPost rows plus the configured vendor page and the verified
    generic pages.

    Passed to validate_draft so a fabricated URL is caught by identity rather
    than by HTTP status — the site answers 200 for any /course/<slug>, so a
    made-up course URL looks alive to a fetch.

    Carries TITLES, not just URLs, because the anchor/target mismatch check
    needs them: an anchor that IS the target page's own title is a correct
    link by definition, and every false positive that check produced was of
    exactly that shape (a blog post whose real title happens to name a course
    code, e.g. "EDU-210 to EDU-330: Top Palo Alto Firewall Courses Ranked")."""
    from config import load_course_links
    from config.db import PublishedPost

    urls = {p.url: (p.title or "") for p in
            session.query(PublishedPost.url, PublishedPost.title).all()}
    cfg = load_course_links()
    base = cfg["base_url"]
    urls[base + "/certifications/"] = "Red Education certification training"
    urls[base + "/training-services/"] = "Red Education training services"
    for v, path in cfg["vendors"].items():
        urls.setdefault(base + path, v)
    return urls


def _vendor_cert_versions(session, vendor_id: int) -> dict[str, list[str]]:
    """{exam code: [version labels]} for QA rule V1, from the SOURCED
    certification_versions table — never from Certification.exam_code, which
    a scrape contaminated with unsourced release tokens.

    Keyed on the exam code rather than the vendor precisely so the check can
    scope to the certification an article is actually about: Check Point
    ships R82, but CCME is R81.X while CCSE moved to R82. Returns {} for a
    vendor with nothing recorded, which switches the check off for that
    vendor rather than guessing."""
    from config.db import CertificationVersion

    rows = session.execute(
        select(Certification.exam_code, CertificationVersion.version_label)
        .join(CertificationVersion,
              CertificationVersion.certification_id == Certification.id)
        .where(Certification.vendor_id == vendor_id)).all()
    out: dict[str, list[str]] = {}
    for code, label in rows:
        if code:
            out.setdefault(code.upper(), []).append(label)
    return out


# Research units and threat-intel brands an article names in prose but whose
# domain is their parent vendor's. Without these, "according to Unit 42" is an
# unrecognised entity and V7a stays silent on the exact defect it exists for.
_ENTITY_DOMAIN_ALIASES = {
    "Unit 42": ["paloaltonetworks.com"],
    "Talos": ["cisco.com", "talosintelligence.com"],
    "Cisco Talos": ["cisco.com", "talosintelligence.com"],
    "Check Point Research": ["checkpoint.com"],
    "F5 Labs": ["f5.com"],
}


def _qa_knowledge(session, article: Article):
    """Assemble everything the QA risk rules need that is not in the draft.

    One builder rather than eight call-site lookups, so every path into
    validate_draft gets the same context and no rule silently runs blind on
    one route and not another. Every field can legitimately come back empty —
    that switches its rule off rather than making it guess, which is the
    right behaviour while the knowledge base is still partial."""
    from config.db import (AllowlistDomain, CertificationVersion, ContentType,
                           DomainCategory, Instructor)
    from synthesis.qa_rules import QaKnowledge
    from synthesis.validate import extract_version_tokens, _subject_certifications

    vendor_id = article.vendor_id
    versions = _vendor_cert_versions(session, vendor_id)

    # V2 wants the REGISTRATION code ("156-836"), which lives on the version
    # row, never the acronym in Certification.exam_code.
    codes: dict[str, list[str]] = {}
    for code, exam in session.execute(
            select(Certification.exam_code, CertificationVersion.exam_code)
            .join(CertificationVersion,
                  CertificationVersion.certification_id == Certification.id)
            .where(Certification.vendor_id == vendor_id)).all():
        if code and exam:
            codes.setdefault(code.upper(), []).append(exam)

    vendors = {v.id: v.name for v in session.scalars(select(Vendor)).all()}

    entity_domains: dict[str, list[str]] = dict(_ENTITY_DOMAIN_ALIASES)
    interested: set[str] = set()
    for d in session.scalars(select(AllowlistDomain).where(
            AllowlistDomain.category == DomainCategory.VENDOR)).all():
        name = vendors.get(d.vendor_id)
        if name:
            entity_domains.setdefault(name, []).append(d.domain)
        # A vendor domain belonging to someone OTHER than this article's
        # vendor is a competitor selling against the thing we are writing
        # about — a lead requiring primary-source confirmation, not a
        # citation.
        if d.vendor_id and d.vendor_id != vendor_id:
            # IDIRA is part of Palo Alto Networks, not a competitor.
            if not (vendor_id == 12 and d.vendor_id == 2):
                interested.add(d.domain)

    # V10: what the rest of the estate already claims about these
    # certifications. Only other articles for the same vendor can contradict
    # this one on a version.
    corpus: dict[str, dict[str, str]] = {}
    for other in session.scalars(select(Article).where(
            Article.vendor_id == vendor_id, Article.id != article.id,
            Article.content != "")).all():
        toks = extract_version_tokens(other.content, article.vendor.name)
        if not toks:
            continue
        for cert in _subject_certifications(other.title, other.content, versions):
            for tok in toks:
                corpus.setdefault(cert, {}).setdefault(tok, other.title)

    topic = article.topic
    certification_names, course_names = _vendor_cert_context(session, vendor_id)
    return QaKnowledge(
        vendor_name=article.vendor.name,
        cert_versions=versions,
        exam_codes=codes,
        # Credentials only: a course row's "exam_code" is a COURSE code
        # (SDWSCS, SPCOR), and letting those count as certification acronyms
        # makes V1 believe an article names a certification when it names a
        # class.
        all_cert_codes=[c.exam_code.upper() for c in session.scalars(
            select(Certification).where(
                Certification.vendor_id == vendor_id,
                Certification.is_credential.is_(True))).all() if c.exam_code],
        is_certification=bool(
            topic is not None
            and topic.content_type == ContentType.CERTIFICATION_GUIDE),
        canonical_names=sorted(vendors.values(), key=len, reverse=True),
        vendor_terms=_vendor_entities(session, vendor_id),
        certification_names=certification_names,
        course_names=course_names,
        entity_domains=entity_domains,
        interested_domains=interested,
        vendor_domains={d.domain.lower() for d in session.scalars(
            select(AllowlistDomain).where(
                AllowlistDomain.category == DomainCategory.VENDOR,
                AllowlistDomain.vendor_id.in_(
                    [vendor_id, 2] if vendor_id == 12 else [vendor_id])
                )).all()},
        known_people={i.name for i in session.scalars(select(Instructor)).all()},
        corpus_versions=corpus,
    )


def _vendor_entities(session, vendor_id: int) -> list[str]:
    """Related-entity list for semantic coverage: the vendor's current
    product, certification, and course names from the Vendor KB — real data,
    not model guesses. Fed to the writer prompt as "reference at least 3
    where they fit" and graded by validate_draft's WARN-only entity_coverage
    check. Capped and de-duplicated; order is products -> certs -> courses so
    a cap trims the longest tail (courses) first."""
    products = session.scalars(
        select(Product).where(Product.vendor_id == vendor_id)).all()
    certs, courses = _vendor_cert_context(session, vendor_id)
    seen, out = set(), []
    for name in [p.name for p in products] + certs + courses:
        key = name.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(name.strip())
    return out[:25]


def _vendor_cert_context(session, vendor_id: int) -> tuple[list[str], list[str]]:
    """(credential names, course names) for the Topic Engine's prompt.

    Only rows that are actually CREDENTIALS go in the first list. 52 of the
    KB's 121 certification rows are training courses the site scrape filed
    there (see Certification.is_credential), and handing those to the model as
    "this vendor's certifications" is how a certification guide comes to be
    written about a course. Their names still reach the prompt — in the second
    list, which is what they are."""
    certs = session.scalars(
        select(Certification).where(
            Certification.vendor_id == vendor_id,
            Certification.status == CertificationStatus.CURRENT,
        )).all()
    courses = [c.name for cert in certs for c in cert.courses]
    return [c.name for c in certs if c.is_credential], courses


def _insert_topics_defensively(session, rows: list[Topic]) -> list[Topic]:
    """add_all()+commit() on the happy path. If a stray collision slips past
    the app-level filter above, a plain bulk commit would roll back the WHOLE
    batch — fall back to one SAVEPOINT per row so only the true collision is
    dropped, not every good row alongside it."""
    try:
        session.add_all(rows)
        session.commit()
        return rows
    except IntegrityError:
        session.rollback()
        inserted = []
        for row in rows:
            try:
                with session.begin_nested():
                    session.add(row)
            except IntegrityError:
                continue
            else:
                inserted.append(row)
        session.commit()
        return inserted


@dataclass
class TopicGenResult:
    inserted: list[Topic]
    created: int
    parked: int
    retired: int
    skipped: int
    # Ideas dropped for repeating something this vendor already has. Surfaced
    # so "I asked for 10 and got 7" reads as the filter working rather than as
    # the generator underdelivering.
    repeats: int = 0


def generate_topics_for_vendor(session, vendor: Vendor, count: int,
                               theme: str | None) -> TopicGenResult:
    _require_commercial_vendor(vendor, "generate commercial topic ideas")
    active_certs = session.scalars(
        select(Certification).where(
            Certification.vendor_id == vendor.id,
            Certification.status == CertificationStatus.CURRENT,
            # Course rows filed as certifications are not credentials to
            # propose certification guides about — see _vendor_cert_context.
            Certification.is_credential.is_(True),
        )).all()
    courses = [c.name for cert in session.scalars(
        select(Certification).where(
            Certification.vendor_id == vendor.id,
            Certification.status == CertificationStatus.CURRENT)).all()
        for c in cert.courses]
    existing_topics = session.scalars(select(Topic).where(Topic.vendor_id == vendor.id)).all()
    existing_keywords = {t.keyword.lower() for t in existing_topics}

    prompt = build_topic_prompt(
        vendor_name=vendor.name, count=count, theme=theme,
        certifications=[c.name for c in active_certs], courses=courses,
        existing_keywords=sorted(existing_keywords),
        existing_titles=[t.title for t in existing_topics],
    )
    # Not wrapped here — any exception from the LLM call propagates untouched,
    # so callers' generic Exception handler shows it with a type prefix,
    # exactly as the original routes did for this one call.
    raw, model_used = complete_with_configured_provider(TOPIC_SYSTEM_PROMPT, prompt,
                                                        purpose="topics")

    ok, result = parse_topic_response(raw)
    if not ok:
        raise PipelineError(f"Topic generation failed: {result}")
    ideas = result

    # Duplicate filtering. Exact keyword matching was the whole check, and it
    # is far too weak to keep successive batches fresh: "ccsa certification
    # guide" and "ccsa exam guide 2026" are different strings and the same
    # article, so on an unchanged knowledge base the tenth run looked like the
    # first. Compared against existing TITLES as well as keywords — the same
    # idea is usually re-packaged with a new keyword, not a new subject.
    seen_texts = ([t.keyword for t in existing_topics]
                  + [t.title for t in existing_topics])
    fresh_ideas, repeats = [], []
    for i in ideas:
        if i.keyword in existing_keywords:
            repeats.append(i.keyword)
            continue
        dup = near_duplicate_of(f"{i.keyword} {i.title}", seen_texts)
        if dup:
            repeats.append(i.keyword)
            continue
        # Also guard WITHIN the batch, or one run can return three phrasings
        # of a single idea and none of them counts as existing yet.
        if near_duplicate_of(f"{i.keyword} {i.title}",
                             [f"{f.keyword} {f.title}" for f in fresh_ideas]):
            repeats.append(i.keyword)
            continue
        fresh_ideas.append(i)

    if not fresh_ideas:
        raise PipelineError(
            f"All {len(ideas)} generated ideas repeat topics this vendor "
            "already has. The obvious angles for this vendor look used up — "
            "try a theme to steer the batch somewhere new, or retire topics "
            "you no longer want.")

    # Replace guessed difficulty with measured SERP difficulty where possible
    # — BEFORE scoring, so the create/park/retire decision uses real data.
    _measure_difficulty(fresh_ideas)

    rows = []
    for idea in fresh_ideas:
        scored = score_topic(idea, vendor.priority)
        rows.append(Topic(
            vendor_id=vendor.id, title=scored.title, keyword=scored.keyword,
            search_intent=scored.search_intent, content_type=scored.content_type,
            commercial_score=scored.commercial_score, seo_score=scored.seo_score,
            ai_score=scored.ai_score, difficulty=scored.difficulty,
            vendor_alignment=scored.vendor_alignment,
            opportunity_score=scored.opportunity_score,
            priority=TopicPriority(scored.priority), rationale=scored.rationale,
            model=model_used, theme=theme,
        ))

    inserted = _insert_topics_defensively(session, rows)
    # Read attributes while the session is still open — SQLAlchemy expires
    # every instrumented attribute after commit() (expire_on_commit=True by
    # default).
    created = sum(1 for t in inserted if t.priority == TopicPriority.CREATE)
    parked = sum(1 for t in inserted if t.priority == TopicPriority.PARKED)
    retired = sum(1 for t in inserted if t.priority == TopicPriority.RETIRE)
    skipped = len(rows) - len(inserted)
    return TopicGenResult(inserted=inserted, created=created, parked=parked,
                          retired=retired, skipped=skipped,
                          repeats=len(repeats))


def _assign_byline(session, article: Article, vendor_id: int) -> None:
    """Pick an instructor byline ONCE per article — called at write time, a
    no-op on every subsequent rewrite (byline_instructor_id already set).

    Only instructors certified for this vendor are eligible, and where several
    are, the least-used one wins so bylines rotate rather than clustering on
    whoever random.choice() favoured (see synthesis/reacs.py's
    pick_instructor).

    A VENDOR WITH NO CERTIFIED INSTRUCTOR falls back to the whole active
    roster, rotated the same way — a decision taken 2026-08-25 so that every
    article carries a byline rather than publishing unattributed. Nine vendors
    with certifications have no certified instructor, so without the fallback
    their articles would all ship anonymously.

    This is a deliberate divergence from v1.3's M7, which requires the byline
    instructor to be certified for the article's vendor. It is coherent only
    because M7 is already off the gate. See synthesis/reacs.py's
    pick_instructor for what it does and does not cost."""
    if article.byline_instructor_id is not None:
        return
    matched_ids = {iv.instructor_id for iv in
                  session.query(InstructorVendor).filter_by(vendor_id=vendor_id).all()}
    active = session.query(Instructor).filter_by(active=True).all()
    eligible = [i for i in active if i.id in matched_ids] or active
    if not eligible:
        return
    # Bylines already carried FOR THIS VENDOR, so rotation spreads a vendor's
    # articles across the people who can review them. A global count would let
    # one busy vendor starve another of its own rotation.
    counts = dict(session.execute(
        select(Article.byline_instructor_id, func.count(Article.id))
        .where(Article.vendor_id == vendor_id,
               Article.byline_instructor_id.isnot(None))
        .group_by(Article.byline_instructor_id)).all())
    usage = {i.name: counts.get(i.id, 0) for i in eligible}
    name = pick_instructor([i.name for i in eligible],
                           [i.name for i in active], usage=usage)
    if name is None:
        return
    article.byline_instructor_id = next(i.id for i in eligible if i.name == name)


@dataclass
class WpAuthor:
    """Who a draft will be published as, and whether that is achievable."""
    name: str | None            # display name, or None for the shared account
    wp_user_id: int | None      # the WordPress user id the REST call needs
    ready: bool                 # False -> WordPress would pick for us
    source: str = ""            # "instructor" | "shared account" | ""
    reason: str = ""


def wp_author_for(article: Article) -> WpAuthor:
    """The WordPress user id this article will be published under.

    WordPress requires an author on EVERY post and sets it by numeric user id
    — there is no "no author" option, and a display name is rejected. So the
    only real choice is which account, and the default is the shared Red
    Education user in WP_AUTHOR_ID (currently 4), matching how the live site
    already publishes (its posts show a single generic account, not a person).

    An instructor with their own `wp_user_id` would take precedence, but
    DECIDED 2026-08-25: per-person WordPress accounts are not being created.
    Every instructor's wp_user_id stays null and every article therefore
    resolves to the shared account. The branch below is kept because it costs
    nothing and documents the alternative should that ever change; it is not
    a pending migration.

    Note this is NOT the same thing as the "Technically reviewed by ___" line
    in the article body: that is REACS's instructor byline, rendered by us
    inside the content, and it appears regardless of who owns the WP post.

    This is the single place the rule lives, so the pusher never reinvents
    it. The configured id is taken on trust — a wrong one makes WordPress
    reject the create call."""
    import os

    i = article.byline_instructor
    if i is not None and i.wp_user_id is not None:
        return WpAuthor(i.name, i.wp_user_id, True, "instructor")

    configured = os.getenv("WP_AUTHOR_ID", "").strip()
    if configured.isdigit():
        # WP_AUTHOR_LABEL is that user's PUBLIC display name, recorded so the
        # Publish step can show the byline readers will actually see without
        # calling WordPress on every page render. Verified once against
        # /wp-json/wp/v2/users/<id>; re-check it if the id or the display name
        # changes, since nothing here can detect that drift.
        return WpAuthor(os.getenv("WP_AUTHOR_LABEL", "").strip() or None,
                        int(configured), True, "shared account")
    return WpAuthor(
        i.name if i else None, None, False, "",
        "WP_AUTHOR_ID is not set, so WordPress would attribute the draft to "
        "whichever account owns the API credential.")


@dataclass
class WpCategory:
    """The WordPress category a draft will be filed under."""
    name: str | None
    wp_category_id: int | None
    ready: bool
    reason: str = ""


def wp_category_for(article: Article) -> WpCategory:
    """The article's WordPress category — its vendor's, by lookup.

    Deliberately NOT an AI call. 23 of the 24 vendors have an exact category
    on the live site and every article already knows its vendor, so this has
    exactly one right answer; a model could only add cost, latency and the
    possibility of inventing a term that isn't in the taxonomy. WordPress
    takes category IDs, not names, so an invented one fails the push anyway.

    Null means the vendor has no category on the site yet (Ivanti today) —
    reported so someone creates it, rather than silently filing the post
    under Uncategorized."""
    v = article.vendor
    if v is None:
        return WpCategory(None, None, False, "Article has no vendor.")
    if v.wp_category_id is None:
        return WpCategory(None, None, False,
                          f"{v.name} has no WordPress category on the site "
                          "yet — create one and record its id, or the post "
                          "lands in Uncategorized.")
    return WpCategory(v.name, v.wp_category_id, True)


def wp_edit_link(article: Article) -> str:
    """The wp-admin edit URL for this article's draft.

    NOT `article.wp_link`. WordPress returns the public permalink (?p=<id>)
    even for a draft, and a draft has no public URL — opening it while logged
    out returns the site's 404 page, which reads as "the push failed" when
    the push was fine. The edit screen is the only place a draft can actually
    be seen, so that is what the UI should offer while status is draft.

    wp_link is still stored: it becomes the real address the moment someone
    publishes the post in WordPress."""
    import os

    base = os.getenv("WORDPRESS_URL", "").strip().rstrip("/")
    if not (base and article.wp_post_id):
        return article.wp_link or ""
    return f"{base}/wp-admin/post.php?post={article.wp_post_id}&action=edit"


def wp_payload_for(article: Article) -> dict:
    """The exact JSON body this article would send to WordPress.

    Separate from push_to_wordpress so the Publish step can SHOW it without
    a network call or credentials — a dry run is the only way to review what
    goes to a live site before it goes there."""
    from publish import build_post_payload

    cat = wp_category_for(article)
    author = wp_author_for(article)
    i = article.byline_instructor
    return build_post_payload(
        title=article.title, slug=article.slug,
        content_markdown=article.content or "",
        excerpt=article.excerpt or "",
        meta_title=article.meta_title or "",
        meta_description=article.meta_description or "",
        keyword=article.keyword or "",
        faq=article.faq or [],
        category_ids=[cat.wp_category_id] if cat.ready else [],
        author_id=author.wp_user_id,
        byline=(i.name, i.credentials) if i else None)


def _remote_post_state(post_id: int) -> str:
    """The WordPress post's status, or "missing"/"unknown".

    Best-effort: a network hiccup must not be read as "deleted", or a push
    guard would open up at exactly the wrong moment, so anything other than a
    definite answer returns "unknown" and is treated as still-present."""
    from publish.client import WpError, _call
    try:
        return (_call(f"/wp/v2/posts/{int(post_id)}?context=edit") or {}).get(
            "status", "unknown")
    except WpError as exc:
        if "404" in str(exc):
            return "missing"
        logger.warning("Could not check WP post %s: %s", post_id, exc)
        return "unknown"


def sync_published_state(session, limit: int = 50) -> dict:
    """Ask WordPress which of our pushed drafts a human has since published,
    and record that as Article.published_at.

    Closes the one blind spot in Stage 7: this tool creates drafts and then
    never hears what happened to them, so a draft published the same
    afternoon and one ignored for a month looked identical here — both just
    "pushed". published_at is still THEIRS, not ours: we only observe it, and
    only ever from WordPress's own `status` field.

    Only articles already pushed and not yet known-published are checked, so
    the work shrinks to zero once everything is reconciled. `limit` caps one
    run — each article is a separate REST call, and this is meant to be a
    cheap scheduled job, not a full reconciliation sweep.

    Returns counts; never raises. A WordPress outage must leave the database
    exactly as it was rather than marking anything wrongly.
    """
    from datetime import datetime, timezone

    pending = (session.query(Article)
               .filter(Article.wp_post_id.isnot(None),
                       Article.published_at.is_(None))
               .order_by(Article.pushed_at)
               .limit(limit).all())

    result = {"checked": 0, "published": 0, "still_draft": 0, "missing": 0,
              "unknown": 0}
    for article in pending:
        state = _remote_post_state(article.wp_post_id)
        result["checked"] += 1
        if state == "publish":
            # WordPress does return a real publish date, but its timezone
            # handling varies by site config; the moment we OBSERVED it is
            # unambiguous and enough for "is this live yet".
            article.published_at = datetime.now(timezone.utc)
            result["published"] += 1
        elif state in ("draft", "pending", "future", "private"):
            result["still_draft"] += 1
        elif state == "missing":
            # Deliberately not cleared here. push_to_wordpress() already
            # handles a deleted post, with the user in front of it; silently
            # unlinking during a background job would let a later push create
            # a duplicate with nobody having seen why.
            result["missing"] += 1
        else:
            result["unknown"] += 1

    if result["published"]:
        session.commit()
    return result


def push_to_wordpress(session, article: Article) -> str:
    """Create the WordPress DRAFT for this article. Returns its edit link.

    Gated on the same two checks the rest of the pipeline uses for
    publication readiness, because a push is the one irreversible-ish step
    here: it puts our text into their CMS, where a person then has to notice
    and delete it if it was wrong."""
    from datetime import datetime, timezone
    from publish import create_draft_post, set_rankmath_meta

    _require_commercial_vendor(article.vendor, "push articles to WordPress")
    if article.status != ArticleStatus.DRAFT:
        raise PipelineError("Only a written draft can be pushed.")
    # Stored verdicts describe the rules that existed when the draft was
    # written. Re-run the current rules at the boundary that matters so an
    # older green badge cannot bypass a newly-added factual control.
    qa_rechecked, _changes = rescore_article(session, article)
    if not qa_rechecked:
        raise PipelineError(
            "Current QA could not be re-run from this article's saved "
            "sources. Refresh its research or rewrite it before pushing.")
    if not (article.qa_passed and article.reacs_passed):
        raise PipelineError(
            "Not a publish candidate — QA and REACS must both pass before "
            "anything reaches WordPress.")
    if article.wp_post_id:
        # Verify the draft is still THERE before refusing. A human deleting
        # the draft in WordPress is normal — it is the whole point of pushing
        # a draft rather than publishing — but our stored link outlives it,
        # and without this check the guard blocks every future push for an
        # article whose draft no longer exists. Observed live: draft 182567
        # was binned and the article became permanently unpushable.
        state = _remote_post_state(article.wp_post_id)
        if state in ("draft", "publish", "pending", "future", "private"):
            raise PipelineError(
                f"Already pushed as WordPress post {article.wp_post_id} "
                f"(status: {state}). Edit it there — pushing again would "
                "create a duplicate rather than update it.")
        logger.info("WP post %s is %s; clearing the stale link and re-pushing",
                    article.wp_post_id, state)
        article.wp_post_id = None
        article.wp_link = None
        article.pushed_at = None
        session.commit()

    payload = wp_payload_for(article)
    try:
        created = create_draft_post(payload)
    except Exception as exc:                      # WpError, mostly
        raise PipelineError(str(exc)) from exc

    # Rank Math's fields can't ride along in the create call (see
    # publish.set_rankmath_meta). Best-effort and AFTER the post exists: a
    # missing focus keyword is a warning, not a reason to lose the draft.
    try:
        set_rankmath_meta(created.get("id"),
                          keyword=article.keyword or "",
                          title=article.meta_title or "",
                          description=article.meta_description or "")
    except Exception:
        logger.exception("Rank Math meta not set for post %s", created.get("id"))

    article.wp_post_id = created.get("id")
    article.wp_link = (created.get("link")
                       or (created.get("guid") or {}).get("rendered", ""))
    article.pushed_at = datetime.now(timezone.utc)
    session.commit()
    return wp_edit_link(article)


def _store_qa(article: Article, report) -> None:
    """Persist a QAReport onto the article's qa_* fields.

    A helper for the same reason _store_reacs is one: this is written from
    three places (write_article, fix_issues, rewrite_section) and they must
    never disagree. qa_score was added later, and three hand-edited copies of
    the same three lines is exactly how one of them ends up not storing it."""
    article.qa_passed = report.passed
    article.qa_checks = [{"name": c.name, "status": c.status, "detail": c.detail}
                         for c in report.checks]
    article.qa_score = report.score


def _store_reacs(article: Article, report) -> None:
    """Persist an already-computed ReacsReport onto the article's fields —
    a SEPARATE gate from QA (see synthesis/reacs.py's module docstring).
    Split out from _run_reacs below so write_article() can store the report
    generate_passing_article() already computed inside its own retry loop,
    instead of paying for a redundant recomputation with identical inputs."""
    article.reacs_report = [
        {"pillar": c.pillar, "tier": c.tier, "passed": c.passed, "message": c.message}
        for c in report.checks
    ]
    article.reacs_score = report.score
    article.reacs_passed = report.passed


def _reacs_seed_feedback(article: Article) -> tuple[str | None, str | None]:
    """(seed_content, seed_feedback) for a rewrite of an article that already
    passed QA but was blocked by REACS.

    Rewriting used to start every attempt from a blank page, so attempt 1
    threw away a draft that may have been one fix from passing and re-rolled
    a dozen structural requirements from scratch — pressing "Rewrite whole
    article" on an 85-scoring draft could hand back a 70. Seeding the loop
    with the stored content plus the stored REACS failures makes the first
    attempt a targeted repair of the article the user is actually looking at.

    Deliberately only seeds the QA-passed-but-REACS-blocked case: if QA
    failed, the draft has a grounding/citation/length problem and is not
    worth building on. Uses the STORED report rather than recomputing,
    because that report was produced against exactly this content, with the
    references that were live at the time."""
    if not article.content or not article.qa_passed:
        return None, None
    if article.reacs_passed or not article.reacs_report:
        return None, None
    checks = [ReacsCheck(pillar=c["pillar"], tier=c["tier"],
                         passed=c["passed"], message=c["message"])
              for c in article.reacs_report]
    report = ReacsReport(
        score=article.reacs_score,
        passed=False,
        mandatory_failed=sum(1 for c in checks
                             if c.tier == "mandatory" and not c.passed),
        checks=checks,
    )
    feedback = reacs_feedback_block(report)
    return (article.content, feedback) if feedback else (None, None)


def _clear_reacs(article: Article) -> None:
    """Reset REACS to the "never scored" sentinel. Needed when a write ends
    with no ReacsReport (QA failed on every attempt, so REACS was never run
    against the draft that got stored). Without this, a REWRITE of a
    previously-scored article leaves the OLD score and report sitting next
    to brand-new content — the panel showed a confident 65 with a checklist
    describing a draft the user was no longer looking at, including checks
    that the visible content actually passes."""
    article.reacs_report = None
    article.reacs_score = -1
    article.reacs_passed = None


def _run_reacs(session, article: Article, content: str, faq: list[dict]) -> None:
    """Compute REACS from scratch and store it. Used by rewrite_section(),
    which has no retry loop of its own and always needs a fresh score after
    splicing in a rewritten section — write_article() gets its ReacsReport
    already computed (see _store_reacs above) and never calls this."""
    from config import load_course_links
    report = compute_reacs(
        content=content, title=article.title, keyword=article.keyword,
        faq=faq, has_byline=article.byline_instructor_id is not None,
        entities=_vendor_entities(session, article.vendor_id),
        meta_title=article.meta_title or "",
        cta_phrases=load_course_links()["cta_phrases"],
    )
    _store_reacs(article, report)


def _title_needs_repair(title: str, keyword: str) -> bool:
    """True if the title is missing the keyword outright, OR carries it
    pasted in lowercase inside an otherwise Title Cased headline.

    The second case is cosmetic but conspicuous — "The Complete ncap
    certification 2026 Guide: Objectives, Study Plan and Exam Domains". It
    comes from briefs written before BRIEF_SYSTEM_PROMPT explained that the
    keyword check is case-INsensitive; told only to reproduce the phrase
    "verbatim", the model pasted it exactly as given, in lower case. REACS
    passes it (single_search_intent lowercases both sides), so nothing else
    will ever flag it. Detection is deliberately narrow: the keyword span
    must be entirely lowercase AND the rest of the title must show real
    capitalisation, so an all-lowercase house style would not trip it."""
    if not keyword or not title:
        return False
    idx = title.lower().find(keyword.lower())
    if idx < 0:
        return True                       # missing outright
    span = title[idx:idx + len(keyword)]
    if span != span.lower():
        return False                      # already capitalised somewhere
    rest = title[:idx] + title[idx + len(keyword):]
    return any(ch.isupper() for ch in rest)


def _repair_title(session, article: Article, vendor_name: str) -> bool:
    """No-op unless the title needs repair (see _title_needs_repair).
    Updates the title, the brief's copy of it, and the slug together — they
    must not drift apart — and commits them (see the comment at the commit
    below). Returns True if it changed anything."""
    kw = (article.keyword or "").strip()
    if not _title_needs_repair(article.title or "", kw):
        return False

    new_title = regenerate_title(article.title, kw, vendor_name)
    if not new_title:
        return False   # REACS will flag it, as it already does

    article.title = new_title
    if isinstance(article.brief, dict):
        # Reassign, don't mutate in place — SQLAlchemy doesn't track
        # in-place edits to a JSON column without a mutable type.
        article.brief = {**article.brief, "title": new_title}
    article.slug = _unique_slug(session, article_slug(new_title, article.keyword),
                                article.topic_id,
                                exclude_id=article.id)
    # Commit NOW, before write_article's long retrieval+LLM phase. Leaving
    # this pending would keep a SQLite write transaction open for the whole
    # ~90s write, and record_api_usage() (config/db.py) opens its OWN session
    # to insert — every one of those would fail with "database is locked" and
    # be silently swallowed, so a full write would vanish from /admin/usage.
    # Any mutation before the LLM phase has to be committed for that reason.
    session.commit()
    return True


def _unique_slug(session, base: str, fallback_suffix, exclude_id: int | None = None) -> str:
    """`exclude_id` skips one article when checking for a collision — needed
    when RE-slugging an existing article, which otherwise collides with its
    own current slug and takes the suffix for no reason (a title fixed only
    for capitalisation slugifies to the same string, so every repaired title
    picked up a "-<topic_id>" tail)."""
    q = session.query(Article).filter_by(slug=base)
    if exclude_id is not None:
        q = q.filter(Article.id != exclude_id)
    if not q.first():
        return base
    return f"{base}-{fallback_suffix}"


def _brief_creation_lock(topic_id: int) -> threading.Lock:
    with _brief_creation_locks_guard:
        return _brief_creation_locks.setdefault(topic_id, threading.Lock())


def create_brief_for_topic(session, topic: Topic) -> Article:
    """Return the topic's article, creating its brief exactly once.

    The per-topic lock avoids paying for the same long research/LLM operation
    twice in this app's single-worker threaded deployment. The database unique
    index remains the cross-process authority.
    """
    topic_id = topic.id
    with _brief_creation_lock(topic_id):
        # The caller loaded `topic` before waiting for this lock, which starts
        # a SQLite read transaction. End that old snapshot so a request that
        # waited behind the winner can see the Article it just committed.
        session.rollback()
        existing = session.query(Article).filter_by(topic_id=topic_id).first()
        if existing is not None:
            return existing
        return _create_brief_for_topic(session, topic)


def _create_brief_for_topic(session, topic: Topic) -> Article:
    """Generate and commit a new Article(status=BRIEF)."""
    topic_id = topic.id
    _require_commercial_vendor(topic.vendor, "create new article briefs")
    # Governance gate 1: only approved topics get a brief.
    if topic.priority != TopicPriority.CREATE:
        raise PipelineError(
            "Only topics approved as 'create' can receive a brief. "
            "Approve it in the Topic step first.")

    vendor = topic.vendor
    certifications, courses = _vendor_cert_context(session, vendor.id)

    # Not wrapped — propagates untouched, same as the original's own
    # try/except around just this call.
    bundle = _live_bundle(topic.title,
                          search_query=_retrieval_query(topic.title, topic.keyword,
                                                        topic.theme),
                          subject_of=topic.keyword,
                          vendor_name=topic.vendor.name if topic.vendor else None)

    # Governance gate 2: the Writer is never allowed to invent facts —
    # refuse if live research turned up nothing usable for this topic.
    # Same caps the writer prompt uses (synthesis/prompt.py) — an unbounded
    # list put a 470KB claims dump into the brief prompt when retrieval hit a
    # large PDF. Verified first: if the cap bites, keep the corroborated ones.
    verified_facts = [clamp_claim(c.text)
                      for c in usable_claims(bundle.claims, "verified")
                      + usable_claims(bundle.claims, "unverified")]
    if not verified_facts:
        raise PipelineError(
            f"No verified research exists for {vendor.name} on this topic. "
            "Check the Research Engine's source allowlist — the Writer is "
            "not allowed to invent facts.")

    ok, result = generate_brief(
        topic_title=topic.title, keyword=topic.keyword,
        content_type=topic.content_type.value, search_intent=topic.search_intent.value,
        vendor_name=vendor.name, certifications=certifications, courses=courses,
        verified_facts=verified_facts, theme=topic.theme,
    )
    if not ok:
        raise PipelineError(f"Brief generation failed: {result}")
    brief = result

    slug = _unique_slug(session, article_slug(brief.title, topic.keyword), topic.id)
    article = Article(
        topic_id=topic.id, vendor_id=vendor.id, title=brief.title, slug=slug,
        keyword=topic.keyword,
        brief={
            "title": brief.title, "audience": brief.audience,
            "keyword": brief.keyword, "format": brief.format,
            "outline": brief.outline, "key_points": brief.key_points,
            "internal_links": brief.internal_links,
            "faq": [{"question": f.question, "answer_hint": f.answer_hint}
                   for f in brief.faq],
            "cta": brief.cta, "image_brief": brief.image_brief,
        },
        status=ArticleStatus.BRIEF,
    )
    session.add(article)
    try:
        session.commit()
    except IntegrityError:
        # Another process may have committed this topic while this request was
        # generating. Reuse that winner; re-raise unrelated integrity errors.
        session.rollback()
        existing = session.query(Article).filter_by(topic_id=topic_id).first()
        if existing is not None:
            return existing
        raise
    return article


def write_article(session, article: Article) -> None:
    """Mutates `article` in place with the written content/meta/QA fields and
    commits. Raises PipelineError only for the "every attempt failed"
    best-effort-returned-None case; any other exception (retrieval/LLM/QA
    plumbing) propagates untouched."""
    vendor = article.vendor
    _require_commercial_vendor(vendor, "write or rewrite commercial articles")

    # REACS's single_search_intent pillar checks that the TITLE literally
    # contains the keyword — and the title is the one thing the Writer below
    # cannot change (ArticlePackage has no title field). So repair it here,
    # before writing, or pressing "Rewrite whole article" on an article with
    # a non-compliant title is a dead end: every attempt fails that mandatory
    # check identically. generate_brief() now prevents this for new briefs;
    # this is what fixes the ones created before that, without discarding a
    # curated brief.
    _repair_title(session, article, vendor.name)

    bundle = _live_bundle(article.topic.title,
        search_query=_retrieval_query(article.topic.title, article.keyword,
                                      article.topic.theme),
        subject_of=article.keyword, vendor_name=vendor.name)

    brief = ContentBrief(
        title=article.brief["title"], audience=article.brief["audience"],
        keyword=article.brief["keyword"], format=article.brief["format"],
        outline=article.brief["outline"], key_points=article.brief["key_points"],
        internal_links=article.brief["internal_links"],
        faq=[BriefFaq(**f) for f in article.brief["faq"]],
        cta=article.brief["cta"], image_brief=article.brief["image_brief"],
    )

    # One SERP snapshot per write (not per retry), for the WARN-only
    # serp_competitiveness QA check.
    serp = _serp_snapshot(article.keyword)
    entities = _vendor_entities(session, vendor.id)
    certification_names, course_names = _vendor_cert_context(session, vendor.id)

    # REACS Engine (Slice 9): assign the byline BEFORE writing (a no-op if
    # already assigned from a prior write) so has_byline is known when REACS
    # is scored below, and so a future WordPress push (Stage 7) has a stable
    # byline that was never re-rolled mid-article.
    _assign_byline(session, article, vendor.id)
    # Commit NOW, before the long write phase — the same trap _repair_title
    # above already hit. Assigning the byline dirties the session; the
    # internal_link_menu query below autoflushes it, opening a write
    # transaction that SQLite holds until commit. generate_passing_article
    # then runs for minutes with that lock held, and every record_api_usage()
    # call inside it fails on "database is locked" — silently, because
    # metering swallows all exceptions by design. Measured: an entire write's
    # LLM spend recorded zero rows.
    session.commit()

    seed_content, seed_feedback = _reacs_seed_feedback(article)
    # Real URLs for this vendor's own pages, so "link the courses you mention"
    # is followable. Without it the writer aimed every course anchor at the
    # site root or /<vendor>/ — see build_article_prompt's link_block comment.
    link_menu = internal_link_menu(session, vendor.name, article.keyword,
                                   entities=entities)
    result = generate_passing_article(brief, bundle, vendor.name, serp=serp,
                                      entities=entities,
                                      certifications=certification_names,
                                      courses=course_names,
                                      has_byline=article.byline_instructor_id is not None,
                                      seed_content=seed_content,
                                      seed_feedback=seed_feedback,
                                      link_menu=link_menu,
                                      allowed_link_urls=_known_link_urls(session, vendor.name),
                                      acronyms=known_acronyms(session),
                                      kb=_qa_knowledge(session, article),
                                      outline=(article.brief or {}).get("outline"))
    if result is None:
        raise PipelineError("Writing failed on every attempt.")
    package, draft, report, model_used, reacs_report = result

    # A full rewrite replaces the text, so previous failed attempts describe
    # prose that no longer exists. Carrying them forward would warn about a
    # problem the rewrite may well have solved.
    article.unresolved_fixes = None
    article.content = package.content
    article.meta_title = package.meta_title
    article.meta_description = package.meta_description
    article.excerpt = package.excerpt
    article.faq = package.faq
    article.cta = package.cta
    article.word_count = package.word_count
    article.model = model_used
    article.status = ArticleStatus.DRAFT
    _store_qa(article, report)
    # The [S#] -> real source URL mapping, so the rendered content's inline
    # markers can become real links (synthesis/models.py's linkify_citations)
    # instead of dead bracket text.
    article.references = [asdict(r) for r in draft.references]
    # Keep the text these citations were written from, so every later fix,
    # section rewrite and re-score is offline and deterministic.
    _snapshot_sources(session, bundle.sources)
    # REACS is a SEPARATE gate from QA above — see synthesis/reacs.py's
    # module docstring for why the two are never merged. Already computed
    # (and, if it failed, already retried against) inside
    # generate_passing_article's own loop; None only when QA itself never
    # passed in any attempt (REACS is never scored against a QA-rejected
    # draft), in which case we clear it to the "never scored" sentinel — the
    # UI shows that distinctly from a real failing score, and it must not
    # keep showing the PREVIOUS draft's report next to this new content.
    if reacs_report is not None:
        _store_reacs(article, reacs_report)
    else:
        _clear_reacs(article)
    session.commit()


def _constraints_to_keep(reacs_report) -> str:
    """The mandatory pillars an edit must not break, stated BEFORE it writes.

    The writer is told what to fix and nothing about what to preserve, so it
    rewrites a block freely and blows a limit it cannot see. Measured twice:
    a fix to entity_type rewrote the opening and broke direct_answer_first
    (a 70-word ceiling and the keyword verbatim), and a fix to heading_length
    rewrote the SEO title and broke single_search_intent. Both are mandatory
    REACS pillars, so _blocking_regressions discarded every attempt and rolled
    the article back — three model calls each, no change, and the check still
    failing.

    Only the mandatory pillars, and only the ones currently PASSING. That set
    is exactly what causes a discard, it is at most six lines, and each
    pillar's own message already describes the satisfied state precisely
    ("Opening is a 44-word direct answer naming the keyword") — which is what
    the model needs before writing, not after failing."""
    lines = [f"- {c.pillar}: {c.message}"
             for c in getattr(reacs_report, "checks", [])
             if getattr(c, "tier", "") == "mandatory" and c.passed]
    if not lines:
        return ""
    return ("\n\nTHESE ALREADY PASS AND MUST STILL PASS AFTERWARDS. An edit "
            "that breaks one causes the WHOLE fix to be discarded and the "
            "article left exactly as it is, so read them before you rewrite "
            "anything:\n" + "\n".join(lines))


def _annotate_block_numbers(feedback: str, blocks: list[str]) -> str:
    """Tell the model WHERE the quoted findings are, in the patch's own terms.

    A check's message quotes the offending sentence; the patch format
    addresses numbered blocks. Nothing connected the two, so the model had to
    find the text itself — and when a finding covered two places it reliably
    fixed one. Measured on entity_type: the wrong definition sat in block 1
    and block 3, and three consecutive attempts edited block 1 alone. Because
    that left the check failing, the minimiser then judged the edit worthless
    collateral and dropped it, reverting the article to where it started.

    Cheap and general: any finding that quotes text gets the block numbers
    that text is in.

    Searches BLOCK -> feedback, not the other way round. Pulling the quoted
    spans out of the message by pairing quote characters is off by one the
    moment a message quotes anything else first: entity_type opens with the
    subject in quotes, so `"EDU-330" ... "the definition"` paired the closing
    quote of the subject with the opening quote of the definition and returned
    the words in between. Asking "is this block quoted in the message" has no
    such ambiguity."""
    flat = " ".join((feedback or "").split())
    lines = []
    for i, block in enumerate(blocks, 1):
        # Probes from THROUGHOUT the block, not just its opening. A quoted
        # finding often starts partway in: the block holding the second wrong
        # definition began "## What is EDU-330 Firewall Troubleshooting?", so
        # an opening-only probe never matched, that block was never named, and
        # the model went on editing the one block it had been pointed at.
        flat_block = " ".join(block.split())
        hit = next((flat_block[o:o + 40] for o in range(0, len(flat_block), 20)
                    if len(flat_block[o:o + 40]) >= 40
                    and flat_block[o:o + 40] in flat), None)
        if hit:
            lines.append(f"- block {i} contains \"{hit}...\"")
    if not lines:
        return feedback
    return (feedback + "\n\nWHERE THESE ARE, in the numbered blocks above. "
            "Every block listed here needs the edit — fixing one and leaving "
            "another is the most common way these findings survive a pass:\n"
            + "\n".join(lines))



def _course_credential_fix_feedback(article, kb) -> str:
    """List all course-as-credential references for targeted repair."""
    import re
    from synthesis.validate import _catalogue_aliases

    credential_aliases = {
        alias.casefold()
        for name in kb.certification_names
        for alias in _catalogue_aliases(name, kb.vendor_name)
    }

    fields = [
        ("article title", article.title or ""),
        ("SEO title", article.meta_title or ""),
        ("meta description", article.meta_description or ""),
        ("article body", article.content or ""),
    ]
    for index, faq in enumerate(article.faq or [], 1):
        fields.append((f"FAQ {index} question", faq.get("question", "")))
        fields.append((f"FAQ {index} answer", faq.get("answer", "")))

    findings = []
    seen = set()

    for course in kb.course_names:
        aliases = sorted(
            _catalogue_aliases(course, kb.vendor_name),
            key=len, reverse=True,
        )
        for alias in aliases:
            if alias.casefold() in credential_aliases:
                continue

            pattern = re.compile(
                rf"(?<!\w){re.escape(alias)}(?!\w)"
                r"(?:\s*\([^)]{2,20}\))?\s+"
                r"(?:(?:is|was|as)\s+)?(?:(?:a|an|the)\s+)?"
                r"(?:professional\s+|technical\s+)?"
                r"(?:certification|credential|exam)\b",
                re.I,
            )

            for field, value in fields:
                for match in pattern.finditer(value):
                    key = (field, match.start(), match.end())
                    if key in seen:
                        continue
                    seen.add(key)

                    context = " ".join(
                        value[max(0, match.start()-70):
                              min(len(value), match.end()+100)].split()
                    )
                    findings.append(
                        f"- {field}: {context!r} "
                        f"(training course: {course})"
                    )

    if not findings:
        return ""

    return (
        "COURSE/CREDENTIAL IDENTITY — FIX EVERY LOCATION BELOW. "
        "A training course is not automatically a certification or exam. "
        "Use the exact separately documented credential name only when "
        "the surrounding evidence supports that credential. "
        "Otherwise describe the item as a training course. "
        "Do not invent exam names or codes.\n"
        + "\n".join(findings)
    )

def _selected_feedback(article: Article, qa_names: list[str],
                       reacs_pillars: list[str]) -> str:
    """Retry feedback covering ONLY the issues the user ticked.

    Reuses QAReport.feedback_block() so each QA issue keeps its hard-won
    per-check advice (originality's "delete every listed sentence",
    structure_length's "add 2-3 more H2 sections", ...) rather than
    restating the bare detail line."""
    from synthesis.models import QAReport

    parts: list[str] = []
    if qa_names:
        rep = QAReport()
        for c in (article.qa_checks or []):
            if c["name"] in qa_names and c["status"] in ("fail", "warn"):
                rep.add(c["name"], c["status"], c.get("detail", ""))
        block = rep.feedback_block()
        if block:
            parts.append(block)
    if reacs_pillars:
        lines = [f"- [{c['tier'].upper()}] {c['pillar']}: {c['message']}"
                 for c in (article.reacs_report or [])
                 if c["pillar"] in reacs_pillars and not c["passed"]]
        if lines:
            parts.append("ALSO FIX THESE CONTENT-STANDARD GAPS:\n" + "\n".join(lines))
    return "\n\n".join(parts)


import os as _retry_config_os
_FIX_ATTEMPTS = max(1, min(3, int(_retry_config_os.getenv('BLOG_FIX_ATTEMPTS', '3'))))

# Checks no amount of rewriting can clear, so the targeted-fix UI must not
# offer them — ticking one would burn a full LLM call and change nothing:
#   instructor_byline    assigned by _assign_byline from the vendor's
#                        instructor list, never from article text
#   single_search_intent decided by the TITLE, which the Writer cannot set
#                        (ArticlePackage has no title field); _repair_title
#                        handles it at the top of write_article instead
#   serp_competitiveness WARN-by-design measurement of external SERP state
UNFIXABLE_BY_REWRITE = {"instructor_byline", "single_search_intent",
                        "serp_competitiveness"}


def unfixable_checks(session, article: Article) -> set[str]:
    """What THIS article's targeted fix cannot clear, however it is worded.

    The static set above, plus vendor_grounding when the article has no
    reference on the vendor's own domain. That check reads the article's
    SOURCES, not its text, and a fix rebuilds the bundle from the references
    the article already has (_bundle_from_references) — so with no vendor
    source among them, no wording satisfies it and ticking it spends a model
    call to change nothing.

    Deliberately NOT a blanket lock: when a vendor source IS cited and the
    check fails for another reason, or when one sits in the references
    uncited, a rewrite can still clear it. The lock mirrors the check's own
    preconditions — certification guides only, and only where the vendor has
    domains recorded, since an empty set switches the rule off entirely.
    """
    from config.db import AllowlistDomain, ContentType, DomainCategory

    locked = set(UNFIXABLE_BY_REWRITE)
    topic = article.topic
    if topic is None or topic.content_type != ContentType.CERTIFICATION_GUIDE:
        return locked
    domains = {d.domain.lower() for d in session.scalars(
        select(AllowlistDomain).where(
            AllowlistDomain.category == DomainCategory.VENDOR,
            AllowlistDomain.vendor_id.in_(
                [article.vendor_id, 2]
                if article.vendor_id == 12
                else [article.vendor_id]
            ))).all()}
    if not domains:
        return locked
    cited = {(ref.get("domain") or "").lower()
             for ref in (article.references or [])}
    if not any(c == v or c.endswith("." + v) for c in cited if c for v in domains):
        locked.add("vendor_grounding")

    # exam_code_presence warns in two different situations, and only one of
    # them is a wording problem. When the KB HOLDS a registration code the
    # article omits, the finding names the exact string ("CCME: 156-836") and
    # a fix can print it. When the KB holds none for this vendor at all,
    # missing_exam_codes() skips every certification and there is nothing a
    # rewrite could insert — the check is reporting a data gap, and it says so
    # itself: add the code to the Vendor Knowledge Base.
    from config.db import Certification, CertificationVersion

    has_code = session.scalars(
        select(Certification.id)
        .join(CertificationVersion,
              CertificationVersion.certification_id == Certification.id)
        .where(Certification.vendor_id == article.vendor_id,
               Certification.exam_code.isnot(None), Certification.exam_code != "",
               CertificationVersion.exam_code.isnot(None),
               CertificationVersion.exam_code != "")
        .limit(1)).first()
    if not has_code:
        locked.add("exam_code_presence")
    return locked


def _blocking_regressions(article: Article, report, new_reacs) -> list[tuple[str, str]]:
    """(name, detail) for checks that were passing before a targeted fix and
    now BLOCK — a QA fail, or a REACS mandatory pillar. WARN/advisory
    slippage is deliberately excluded: it doesn't stop publication, and
    treating it as fatal would discard almost every otherwise-good fix.

    The detail comes back too because it is the actionable half — QA's
    originality detail names the exact sentences that were reintroduced,
    which is what makes a retry able to undo the damage."""
    was_ok_qa = {c["name"] for c in (article.qa_checks or [])
                 if c["status"] == "pass"}
    was_ok_reacs = {c["pillar"] for c in (article.reacs_report or [])
                    if c["passed"]}
    out = [(c.name, c.detail) for c in report.checks
           if c.status == "fail" and c.name in was_ok_qa]
    out += [(c.pillar, c.message) for c in new_reacs.checks
            if not c.passed and c.tier == "mandatory" and c.pillar in was_ok_reacs]
    return out


def _soft_regressions(article: Article, report, new_reacs) -> list[tuple[str, str]]:
    """(name, detail) for checks that were passing and are now merely
    WARN/advisory-failing.

    These are what the editor actually complains about: tick one check, and
    an unrelated one goes amber. They were once tolerated silently, then
    merely reported. Now _revert_collateral() undoes the edits that caused
    them and, failing that, they are fed back for a retry exactly like a
    blocking regression — the detail is here because that feedback needs it.

    Still not fatal, though: an attempt carrying only soft damage is kept
    rather than discarded, because rejecting it would leave the editor with
    the article they already had and no fix at all."""
    was_ok_qa = {c["name"] for c in (article.qa_checks or [])
                 if c["status"] == "pass"}
    was_ok_reacs = {c["pillar"] for c in (article.reacs_report or [])
                    if c["passed"]}
    out = [(c.name, c.detail) for c in report.checks
           if c.status == "warn" and c.name in was_ok_qa]
    out += [(c.pillar, c.message) for c in new_reacs.checks
            if not c.passed and c.tier != "mandatory" and c.pillar in was_ok_reacs]
    return out


def _unresolved(report, new_reacs, selected: set[str]) -> list[str]:
    """Which of the TICKED checks a candidate article still fails.

    Named rather than counted, because a check that CANNOT be fixed by a
    rewrite has to look different from one that merely needs another go —
    title_readability absorbed three consecutive attempts before anyone
    noticed the writer could not set the title at all."""
    return ([c.name for c in report.checks
             if c.name in selected and c.status in ("fail", "warn")]
            + [c.pillar for c in new_reacs.checks
               if c.pillar in selected and not c.passed])


@dataclass
class _Attempt:
    """One model attempt at a targeted fix, already scored against the article
    it is meant to improve."""
    package: object          # ArticlePackage
    model: str
    report: object           # QA ValidationReport
    reacs: object            # ReacsReport
    blocking: list[tuple[str, str]]
    soft: list[tuple[str, str]]
    unresolved: list[str]    # selected checks this attempt did NOT clear

    @property
    def collateral(self) -> int:
        """Checks that were passing before this attempt and are not now."""
        return len(self.blocking) + len(self.soft)

    @property
    def rank(self) -> tuple[int, int, int]:
        """Lower is better. Blocking damage first — it is the only kind that
        makes an attempt unusable — then damage of any kind, then how much of
        what the editor actually asked for is still outstanding."""
        return (len(self.blocking), self.collateral, len(self.unresolved))


def _is_improvement(base: _Attempt, best: _Attempt, trial: _Attempt) -> bool:
    """Is this candidate worth keeping?

    Only if it removes collateral damage AND leaves no more of what the editor
    ticked undone. The second half is measured against the model's own attempt
    rather than the best so far, so a run of accepted reverts cannot creep the
    fix backwards one check at a time."""
    return (len(trial.unresolved) <= len(base.unresolved)
            and trial.collateral < best.collateral)


# The non-body fields a patch may rewrite. Droppable exactly like an edit —
# see _drop_harmful_edits.
_PATCH_FIELDS = ("meta_title", "meta_description", "excerpt", "cta", "faq")


def _drop_harmful_edits(patch, attempt: _Attempt, evaluate, build) -> _Attempt:
    """Take back the individual changes that broke something.

    A patch already guarantees that blocks the model did not name are
    untouched, so what is left to go wrong is a change that WAS made and trips
    a check it had no business touching. Each one is dropped in turn and kept
    out only if that removes damage without leaving more of the request undone.

    EVERY change is a candidate, the non-body fields included. That is not
    symmetry for its own sake: asked to shorten one over-long heading, the
    model also volunteered a new meta_title, prepending "The " pushed it past
    60 characters, _clamp_str cut the final word off, and the target keyword
    stopped appearing verbatim — failing single_search_intent, a mandatory
    REACS pillar. Only edits were droppable, so the meta_title could not be
    taken back; all three attempts were discarded for a regression none of
    them needed, and the heading fix, correct every time, was discarded with
    them. Three calls spent, nothing changed, and nothing recorded either,
    because the discard path rolls back before the attempt counter is written.

    Free: one rescore is ~12ms measured, and a targeted fix is a handful of
    changes, not a whole article's worth."""
    base, best = attempt, attempt
    kept = list(patch.edits)
    dropped_fields: set[str] = set()

    candidates = ([("edit", e) for e in patch.edits]
                  + [("field", f) for f in _PATCH_FIELDS
                     if getattr(patch, f, None) is not None])

    for kind, item in candidates:
        if not best.collateral:
            break
        if kind == "edit":
            trial_edits = [e for e in kept if e is not item]
            trial_fields = dropped_fields
            if len(trial_edits) == len(kept):
                continue                  # already dropped
        else:
            if item in dropped_fields:
                continue
            trial_edits = kept
            trial_fields = dropped_fields | {item}
        trial = evaluate(build(trial_edits, trial_fields))
        if _is_improvement(base, best, trial):
            kept, dropped_fields, best = trial_edits, trial_fields, trial
    return best


def _rejoin(blocks: list[str], reverts: list[tuple[int, int, list[str]]]) -> str:
    """`blocks` with each revert applied. Highest index first, so applying one
    never shifts the slice another is aiming at."""
    out = list(blocks)
    for j1, j2, replacement in sorted(reverts, key=lambda r: -r[0]):
        out[j1:j2] = replacement
    return "\n\n".join(b for b in out if b.strip())


def _revert_collateral(original: str, attempt: _Attempt, evaluate) -> _Attempt:
    """The same idea as _drop_harmful_edits, for the whole-article fallback.

    There the model returns the entire article rather than a list of edits, so
    the edits have to be recovered by diffing it against the draft: every
    paragraph that changed is a candidate for reverting to the editor's own
    text. Less precise than a patch — a diff cannot tell an edit that was
    asked for from one that was not — which is exactly why the patch path
    exists and this one is only the fallback.
    """
    content = attempt.package.content
    if "```" in original or "```" in content:
        # Splitting on blank lines would cut a fenced block in half, and a
        # half-reverted fence corrupts everything after it.
        return attempt

    new = split_blocks(content)
    old = split_blocks(original)
    candidates: list[tuple[int, int, list[str]]] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, old, new, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace" and (i2 - i1) == (j2 - j1):
            # Paragraphs correspond 1:1 here, so offer each on its own — one
            # bad edit must not drag its innocent neighbours back with it.
            candidates += [(j1 + k, j1 + k + 1, [old[i1 + k]])
                           for k in range(i2 - i1)]
        else:
            candidates.append((j1, j2, old[i1:i2]))

    base, best, kept = attempt, attempt, []
    for cand in candidates:
        if not best.collateral:
            break
        trial = evaluate(replace(base.package,
                                 content=_rejoin(new, kept + [cand])))
        if _is_improvement(base, best, trial):
            kept.append(cand)
            best = trial
    return best


def _patched_package(article: Article, blocks: list[str], patch, edits,
                     drop_fields=(), base: dict | None = None) -> ArticlePackage:
    """The article as this patch would leave it.

    A field the patch left null keeps whatever the article has now — that is
    what null means, and it is the whole reason a fix aimed at the body cannot
    disturb the SEO title. A field named in `drop_fields` is treated the same
    way even though the patch DID set it: that is how _drop_harmful_edits
    takes back a field change that broke something.

    `base` overrides what "whatever the article has now" means. The targeted
    fix passes the pre-pass's corrected fields, because the pre-pass corrects
    the SEO title, excerpt, CTA and FAQ as well as the body, and only the body
    used to survive: a casing fix it made in the meta title was computed,
    scored, reported to the model as already clean — so the model was never
    asked for it — and then reverted here, leaving the check failing and the
    editor told it "did not clear last time"."""
    base = base or {}

    def field(name: str, fallback):
        value = getattr(patch, name, None)
        if value is None or name in drop_fields:
            return base.get(name, fallback)
        return value

    patched_content = apply_patch(blocks, edits)
    if edits:
        patched_content = refresh_last_updated(patched_content)
    return ArticlePackage(
        content=patched_content,
        meta_title=field("meta_title", article.meta_title or ""),
        meta_description=field("meta_description",
                               article.meta_description or ""),
        excerpt=field("excerpt", article.excerpt or ""),
        faq=field("faq", article.faq or []),
        cta=field("cta", article.cta or ""),
        word_count=0)


def fix_issues(session, article: Article, qa_names: list[str],
               reacs_pillars: list[str]) -> list[str]:
    """One targeted LLM pass that fixes ONLY the selected QA/REACS issues and
    leaves the rest of the article alone.

    Sits between the two existing repair tools: rewrite_section() targets one
    H2 but can't be aimed at a specific check, while write_article() fixes
    everything at once and re-researches from scratch. This aims directly at
    checks the user picked — useful because several checks are WARN-only or
    advisory and are often a deliberate editorial choice to keep.

    Grounded in the article's OWN stored references (_bundle_from_references),
    exactly like rewrite_section: no new retrieval, so the [S#] tags in the
    untouched prose stay valid. One call, no retry loop — QA/REACS re-run and
    report honestly, and the human decides whether to go again."""
    from config import load_course_links
    if article.status != ArticleStatus.DRAFT:
        raise PipelineError("Only a written draft can have issues fixed.")
    locked = unfixable_checks(session, article)
    dropped = [n for n in list(qa_names) + list(reacs_pillars) if n in locked]
    qa_names = [n for n in qa_names if n not in locked]
    reacs_pillars = [p for p in reacs_pillars if p not in locked]
    if not qa_names and not reacs_pillars:
        # Name the reason for THIS selection. "See the check's message" sent
        # the reader back to a message that describes the problem, not why the
        # button they just pressed declined to work on it.
        why = ("The byline, the title keyword and the SERP comparison are not "
               "set by the article text")
        if "vendor_grounding" in dropped:
            why = ("vendor_grounding needs a source from the vendor's own "
                   "site, and a fix can only cite sources this article "
                   "already has — use Rewrite whole article")
        raise PipelineError(
            f"Nothing selected that a rewrite can fix. {why} — see each "
            "check's own message for what does fix them.")


    bundle = _bundle_from_references(article, session)
    brief = ContentBrief(
        title=article.brief["title"], audience=article.brief["audience"],
        keyword=article.brief["keyword"], format=article.brief["format"],
        outline=article.brief["outline"], key_points=article.brief["key_points"],
        internal_links=article.brief["internal_links"],
        faq=[BriefFaq(**f) for f in article.brief["faq"]],
        cta=article.brief["cta"], image_brief=article.brief["image_brief"],
    )
    entities = _vendor_entities(session, article.vendor_id)
    # Same reason write_article() passes this: without a real menu, telling
    # the model "these links are wrong" gives it no way to know what the RIGHT
    # url is for a Cortex XDR / Panorama / etc. page, so a fix pass just
    # reproduces the same root/reused links every time. This was the actual
    # cause of internal_link_targets surviving repeated "Fix selected" clicks
    # on the same article.
    link_menu = internal_link_menu(session, article.vendor.name, article.keyword,
                                   entities=entities)
    allowed_link_urls = _known_link_urls(session, article.vendor.name)

    from synthesis.models import Draft, Reference
    from synthesis.validate import split_overlong_headings, validate_draft
    from synthesis.text import slugify

    # Scoring an attempt needs these, and every one of them is either a
    # network call or a DB read. Hoisted out of the loop: _serp_snapshot in
    # particular was spending a Serper credit per retry, and the minimiser
    # below rescores once per edit.
    serp = _serp_snapshot(article.keyword)
    acronyms = known_acronyms(session)
    kb = _qa_knowledge(session, article)
    cta_phrases = load_course_links()["cta_phrases"]
    selected = set(qa_names) | set(reacs_pillars)
    # THE BASELINE THE FIX WORKS FROM. Not article.content directly, because
    # the mechanical pre-pass below may correct it first and the prompt, the
    # block numbering and the patch must all see the same text.
    base_content = article.content
    # What a patch field left null falls back to. Replaced wholesale by the
    # pre-pass below when its corrections break nothing, so a deterministic
    # fix outside the body survives a patch that only touched the body.
    base_fields: dict = {}

    def evaluate(pkg: ArticlePackage) -> _Attempt:
        """Score one candidate article: what it broke, and what it left."""
        draft = Draft(
            topic=article.topic.title, region=None, title=article.title,
            slug=slugify(article.title), meta_description=pkg.meta_description,
            body_markdown=pkg.content, faq=pkg.faq,
            references=[Reference(**r) for r in article.references],
        )
        rep = validate_draft(
            draft, bundle, keywords=[article.keyword], serp=serp,
            entities=entities,
            internal_links=(article.brief.get("internal_links") or None),
            allowed_link_urls=allowed_link_urls, acronyms=acronyms,
            seo_title=pkg.meta_title, kb=kb,
            outline=(article.brief or {}).get("outline"))
        rc = compute_reacs(
            content=pkg.content, title=article.title, keyword=article.keyword,
            faq=pkg.faq,
            has_byline=article.byline_instructor_id is not None,
            entities=entities, meta_title=pkg.meta_title or "",
            cta_phrases=cta_phrases)
        return _Attempt(
            package=pkg, model=model_used, report=rep, reacs=rc,
            blocking=_blocking_regressions(article, rep, rc),
            soft=_soft_regressions(article, rep, rc),
            unresolved=_unresolved(rep, rc, selected))

    def run(patch_mode: bool, tries: int) -> tuple[_Attempt | None, str]:
        """Up to `tries` model attempts, returning the best usable one.

        A "fix one thing, leave the rest" tool that quietly breaks something
        else is worse than useless, and it DOES happen: this model reliably
        re-derives untouched FAQ answers from the source excerpts instead of
        copying them from the draft, reinstating verbatim phrasing an earlier
        pass had removed.

        Three layers, cheapest first. The patch format means a block the model
        did not name cannot change at all. What it DID name is then dropped
        edit by edit wherever an edit broke something, for free. And only what
        survives both — damage genuinely entangled with the fix — costs
        another call.
        """
        nonlocal model_used
        guard, best, why = "", None, ""
        blocks = split_blocks(base_content)
        for _attempt in range(tries):
            system_prompt, user_prompt, _tags = build_article_prompt(
                brief, bundle, article.vendor.name,
                feedback=(_annotate_block_numbers(feedback, blocks)
                          if patch_mode else feedback) + guard,
                entities=entities, previous_content=base_content,
                link_menu=link_menu, patch_mode=patch_mode,
                certifications=kb.certification_names,
                courses=kb.course_names,
                previous_fields={
                    "meta_title": article.meta_title,
                    "meta_description": article.meta_description,
                    "excerpt": article.excerpt,
                    "cta": article.cta,
                    "faq": article.faq,
                })
            raw, model_used = complete_with_configured_provider(
                system_prompt, user_prompt, purpose="targeted_fix")

            # Same acronym set the first write used, so a targeted fix cannot
            # reintroduce "ccte r82" after the original draft had it corrected.
            if patch_mode:
                ok, patch = parse_patch_response(raw, blocks, acronyms,
                                                 entities)
                if ok:
                    def build(edits, drop_fields=(), _p=patch):
                        return _patched_package(article, blocks, _p, edits,
                                                drop_fields, base=base_fields)
                    scored = evaluate(build(patch.edits))
                    if scored.collateral:
                        scored = _drop_harmful_edits(patch, scored, evaluate, build)
                    if patch.rejected:
                        logger.info("targeted fix: %d edit(s) discarded: %s",
                                    len(patch.rejected), "; ".join(patch.rejected))
                else:
                    why, scored = str(patch), None
            else:
                ok, result = parse_article_response(raw, acronyms, entities)
                if ok:
                    scored = evaluate(result)
                    if scored.collateral:
                        scored = _revert_collateral(base_content, scored,
                                                    evaluate)
                else:
                    why, scored = str(result), None

            if scored is None:
                continue
            if best is None or scored.rank < best.rank:
                best = scored
            # Stop only when the attempt broke nothing AND actually fixed
            # what was ticked. Breaking on collateral alone meant a pass that
            # damaged nothing and repaired nothing ended the loop after one
            # call — which is how "Fix selected" came to spend a call, change
            # a sentence, and leave the check failing with no second try.
            # UNFIXABLE_BY_REWRITE already keeps checks a rewrite cannot touch
            # out of `selected`, and unresolved_fixes warns before a third go,
            # so the extra attempts are spent only where they can help.
            if not best.collateral and not best.unresolved:
                break

            # The LATEST attempt's damage, not the best one's — "your last
            # attempt broke this" has to be true of the attempt just made, or
            # the model is being corrected for something it did not do.
            damage = scored.blocking + scored.soft
            guard = (
                "\n\nSTOP — YOUR LAST ATTEMPT BROKE PARTS OF THE ARTICLE THAT "
                "WERE ALREADY CORRECT. You changed text you were not asked to "
                "change, and that reintroduced these problems:\n"
                + "\n".join(f"- {name}: {detail}" for name, detail in damage)
                + "\nGo again. Fix ONLY the originally listed problems, and "
                  "touch nothing else.")
        return best, why

    model_used = ""

    # LAYER ZERO: the corrections that need no model at all.
    #
    # locale_spelling is one word to swap — "organizations" for
    # "organisations" — and it survived three consecutive targeted fixes on
    # one article. Three LLM calls spent on a string replacement, and it was
    # never going to work: the patch only carries blocks the model chose to
    # change, so a stray spelling in a paragraph it had no other reason to
    # touch is simply never in the response. fix_writing_mechanics already
    # owns everything we can correct deterministically, so apply it to the
    # WHOLE article first, before the prompt is even built.
    #
    # Adopted only if it breaks nothing, and it is scored by the same
    # evaluate() as any model attempt — a deterministic edit gets no more
    # trust than a generated one.
    def _corrected(text: str) -> str:
        return fix_writing_mechanics(text or "", acronyms, entities)

    # Headings first: a heading that swallowed its paragraph is repaired by
    # restoring the blank line the model left out, and where the brief's
    # outline names the heading that is a substitution, not a judgement. The
    # model was asked three times to make one such split and returned the same
    # 500-character heading with a verb tense changed.
    prepass = evaluate(ArticlePackage(
        content=refresh_last_updated(_corrected(split_overlong_headings(
            article.content, (article.brief or {}).get("outline")))),
        meta_title=_corrected(article.meta_title),
        meta_description=_corrected(article.meta_description),
        excerpt=_corrected(article.excerpt),
        faq=[{"question": _corrected(f.get("question", "")),
              "answer": _corrected(f.get("answer", ""))}
             for f in (article.faq or [])],
        cta=_corrected(article.cta), word_count=0))

    # Adopt the whole corrected package, not just the body. Gating on
    # `content != article.content` meant a pre-pass that fixed ONLY the SEO
    # title or an FAQ answer adopted nothing at all, and the fields it had
    # corrected were rebuilt from the article further down.
    if not prepass.collateral:
        base_content = prepass.package.content
        base_fields = {
            "meta_title": prepass.package.meta_title,
            "meta_description": prepass.package.meta_description,
            "excerpt": prepass.package.excerpt,
            "faq": prepass.package.faq,
            "cta": prepass.package.cta,
        }

    # FEEDBACK FROM A FRESH SCORE, not the stored report. _selected_feedback
    # reads article.qa_checks, which is whatever the last scoring wrote — so a
    # check improved since then hands the writer its OLD message. Measured:
    # entity_type learned to report every wrong definition, the stored report
    # still quoted only the first, and the block annotation therefore pointed
    # at one of the two blocks that needed the edit. Re-score fixed it, but
    # nobody should have to know that.
    # Score whatever the fix will actually START from. When the pre-pass broke
    # something its package is discarded, and storing ITS report anyway made
    # _selected_feedback read a check as passing that the real article still
    # fails — so the call rolled back with "Those issues are no longer failing"
    # while the article sat unchanged and still failing.
    scored_base = prepass if not prepass.collateral else evaluate(ArticlePackage(
        content=article.content, meta_title=article.meta_title or "",
        meta_description=article.meta_description or "",
        excerpt=article.excerpt or "", faq=list(article.faq or []),
        cta=article.cta or "", word_count=0))
    _store_qa(article, scored_base.report)
    _store_reacs(article, scored_base.reacs)
    feedback = _selected_feedback(article, qa_names, reacs_pillars)
    if feedback and "course_credential_identity" in qa_names:
        credential_feedback = _course_credential_fix_feedback(article, kb)
        if credential_feedback:
            feedback += "\n\n" + credential_feedback
    if feedback:
        feedback += _constraints_to_keep(prepass.reacs)
    if not feedback:
        session.rollback()
        raise PipelineError(
            "Those issues are no longer failing — re-check the panels below.")

    if not prepass.collateral and not prepass.unresolved:
        # Everything ticked is already clear. Spending a call now could only
        # make the article worse.
        best, parse_error = prepass, ""
    else:
        best, parse_error = run(patch_mode=True, tries=_FIX_ATTEMPTS)
    if best is None:
        # The patch schema produced nothing usable in any attempt. Rather than
        # leave the editor with a dead button, fall back to the older
        # whole-article path — less precise, still guarded.
        logger.warning("targeted fix: no usable patch (%s); falling back to "
                       "the whole-article path", parse_error)
        best, parse_error = run(patch_mode=False, tries=1)

    if best is None:
        session.rollback()
        raise PipelineError(f"Targeted fix failed: {parse_error}")
    if best.blocking:
        # Soft damage is survivable and gets kept; a blocking regression is
        # not — the editor would be handed an article that can no longer be
        # published in exchange for a warning cleared.
        session.rollback()
        raise PipelineError(
            "Discarded the fix — every attempt broke "
            + ", ".join(sorted({n for n, _d in best.blocking}))
            + ", which passed before. The article is unchanged. Try fixing "
            "fewer issues at once, or use Rewrite whole article.")

    result, report, new_reacs = best.package, best.report, best.reacs
    soft = [n for n, _d in best.soft]
    still_failing = best.unresolved

    article.content = result.content
    article.meta_title = result.meta_title
    article.meta_description = result.meta_description
    article.excerpt = result.excerpt
    article.faq = result.faq
    article.cta = result.cta
    # Recomputed here rather than taken from the model: on the patch path
    # nobody ever counted the assembled article, and on the fallback path
    # _revert_collateral may have put paragraphs back since it did.
    article.word_count = count_words(result.content)
    article.model = best.model
    _store_qa(article, report)
    _assign_byline(session, article, article.vendor_id)
    # Already computed above for the regression check — storing it directly
    # avoids a second identical pass over the same content.
    _store_reacs(article, new_reacs)
    session.commit()

    # Remember which ticked checks did not clear, so the panel can warn BEFORE
    # the same call is spent again. A check that cleared is dropped: the count
    # means "consecutive failed attempts on the text as it stands", not
    # lifetime history. title_readability absorbed three consecutive attempts
    # before anyone noticed it could not be fixed from the body at all.
    history = dict(article.unresolved_fixes or {})
    for name in selected:
        if name in still_failing:
            history[name] = int(history.get(name, 0)) + 1
        else:
            history.pop(name, None)
    article.unresolved_fixes = history or None
    session.commit()
    return soft, still_failing


def _snapshot_sources(session, sources) -> None:
    """Record these sources, so re-scoring never needs the network again.

    Idempotent: a URL already snapshotted is left exactly as it was, because
    the snapshot is the evidence the article was written from and refreshing
    it would silently change what grounding checks against (see
    config/db.py's SourceSnapshot)."""
    from config.db import SourceSnapshot

    for src in sources or []:
        if not src.url or not (src.text or "").strip():
            continue
        if session.scalar(select(SourceSnapshot).where(
                SourceSnapshot.url == src.url)) is not None:
            continue
        session.add(SourceSnapshot(
            url=src.url, domain=src.domain or "", title=src.title or "",
            text=src.text, tier=src.tier, is_vendor=bool(src.is_vendor),
            vendor=src.vendor,
            published=src.published.isoformat() if src.published else None))
    session.flush()


def _bundle_from_references(article: Article, session=None):
    """Rebuild the article's grounding bundle from its STORED references —
    re-fetching exactly the URLs it already cites, in tag order, so the
    rebuilt bundle's [S#] tags are identical to the ones in the body. This
    is what makes a section rewrite citation-safe: a fresh retrieval would
    assign different tags to different sources, corrupting citation_integrity
    for every UNtouched section. The URLs came from our own allowlisted
    retrieval at write time, so fetch_source()'s allowlist gate still holds."""
    from contextlib import nullcontext
    from datetime import date as _date

    from config.db import SourceSnapshot, get_session
    from retrieval.fetch import fetch_source
    from retrieval.models import Source
    from verification import rebuild_bundle

    refs = sorted(article.references or [],
                  key=lambda r: int(r["tag"].lstrip("S") or 0))
    if not refs:
        raise PipelineError(
            "This article has no stored references to ground a section "
            "rewrite — use the full Rewrite instead.")

    # USE THE CALLER'S SESSION. Opening a second one here deadlocks SQLite the
    # moment the caller has an uncommitted write — fix_issues() and
    # rewrite_section() both hold a session, and a nested connection trying to
    # commit a snapshot while the outer holds the write lock raises
    # OperationalError: database is locked. Reproduced, not theorised.
    #
    # The nullcontext branch keeps this usable from a one-off script that has
    # no session of its own.
    own = session is None
    with (get_session() if own else nullcontext(session)) as s:
        snaps = {sn.url: sn for sn in s.scalars(select(SourceSnapshot).where(
            SourceSnapshot.url.in_([r["url"] for r in refs]))).all()}

        sources, refetched = [], []
        for ref in refs:
            sn = snaps.get(ref["url"])
            if sn is not None:
                sources.append(Source(
                    url=sn.url, domain=sn.domain, title=sn.title, text=sn.text,
                    published=_date.fromisoformat(sn.published) if sn.published else None,
                    tier=sn.tier, is_vendor=sn.is_vendor, vendor=sn.vendor))
                continue
            # No snapshot: an article written before snapshots existed. Fetch
            # it once and keep it, so this article pays the network cost at
            # most once rather than on every rewrite.
            src = fetch_source(ref["url"])
            if src is None:
                raise PipelineError(
                    f"Couldn't re-fetch source [{ref['tag']}] ({ref['url']}) "
                    "— this article predates source snapshots and the page is "
                    "not responding. Fetch failures here are often transient, "
                    "so try again; a full Rewrite also fixes it permanently "
                    "by researching fresh sources and snapshotting them.")
            sources.append(src)
            refetched.append(src)

        if refetched:
            _snapshot_sources(s, refetched)
            # Only commit a session we own. Committing the caller's would
            # flush THEIR half-built work — fix_issues() deliberately holds a
            # draft it may still roll back if every attempt regresses.
            if own:
                s.commit()

    return rebuild_bundle(article.topic.title, None, sources)


def rewrite_section(session, article: Article, heading: str,
                    instruction: str | None = None) -> None:
    """Rewrite ONE H2 section of a written article in place, grounded in the
    same sources it already cites (see _bundle_from_references), then re-run
    the full QA harness on the spliced article and commit. One LLM call, no
    retry loop — QA reports honestly and the human decides whether to rewrite
    again; vastly cheaper than the full-article Rewrite for a targeted fix."""
    if article.status != ArticleStatus.DRAFT:
        raise PipelineError("Only a written draft can have sections rewritten.")

    heading = heading.strip()
    # Line-anchored, same matching rule replace_section() uses — a substring
    # check could false-positive on "## X" appearing mid-line and burn an
    # LLM call before the splice fails.
    if not any(ln.strip() == f"## {heading}"
               for ln in article.content.splitlines()):
        raise PipelineError(f'No section titled "{heading}" exists in this article.')

    bundle = _bundle_from_references(article, session)
    brief = ContentBrief(
        title=article.brief["title"], audience=article.brief["audience"],
        keyword=article.brief["keyword"], format=article.brief["format"],
        outline=article.brief["outline"], key_points=article.brief["key_points"],
        internal_links=article.brief["internal_links"],
        faq=[BriefFaq(**f) for f in article.brief["faq"]],
        cta=article.brief["cta"], image_brief=article.brief["image_brief"],
    )

    certification_names, course_names = _vendor_cert_context(
        session, article.vendor_id)
    system_prompt, user_prompt, _tags = build_section_rewrite_prompt(
        article.content, heading, brief, bundle, article.vendor.name,
        instruction, certifications=certification_names, courses=course_names)
    raw, model_used = complete_with_configured_provider(system_prompt, user_prompt,
                                                        purpose="section_rewrite")

    ok, result = parse_section_response(raw, heading)
    if not ok:
        raise PipelineError(f"Section rewrite failed: {result}")
    new_content = replace_section(article.content, heading, result)
    if new_content is None:
        raise PipelineError(f'No section titled "{heading}" exists in this article.')
    new_content = refresh_last_updated(new_content)

    # Re-run the FULL harness on the spliced article — a section edit can
    # break article-level invariants (length, CTA, keyword coverage), so
    # the QA verdict must be recomputed, not carried over.
    from synthesis.models import Draft, Reference
    from synthesis.validate import validate_draft
    from synthesis.text import slugify

    draft = Draft(
        topic=article.topic.title, region=None, title=article.title,
        slug=slugify(article.title), meta_description=article.meta_description,
        body_markdown=new_content, faq=article.faq,
        references=[Reference(**r) for r in article.references],
    )
    serp = _serp_snapshot(article.keyword)
    report = validate_draft(
        draft, bundle, keywords=[article.keyword], serp=serp,
        entities=_vendor_entities(session, article.vendor_id),
        internal_links=(article.brief.get("internal_links") or None),
        allowed_link_urls=_known_link_urls(session, article.vendor.name),
        acronyms=known_acronyms(session),
        seo_title=article.meta_title,
        kb=_qa_knowledge(session, article),
        outline=(article.brief or {}).get("outline"))

    article.content = new_content
    article.word_count = count_words(new_content)
    article.model = model_used
    _store_qa(article, report)
    # Backfill for an article written before REACS existed (byline_instructor_id
    # still None): a no-op otherwise. Without this, a legacy draft rewritten
    # ONLY via section-rewrite (the help text's own "use this first" guidance)
    # would never get a byline and REACS's instructor_byline mandatory pillar
    # would fail forever, since write_article — the only other place this is
    # assigned — never runs again for it.
    _assign_byline(session, article, article.vendor_id)
    # A section edit can also change REACS-relevant content (a table, the
    # "Last updated" line) — recompute alongside QA, same as write_article() does.
    _run_reacs(session, article, new_content, article.faq)
    session.commit()


def rescore_article(session, article: Article) -> tuple[bool, dict]:
    """Re-run both gates against the CURRENT body. Changes no content.

    Scores are stored at write time on purpose — they are a record of what an
    article scored when it was written, not a live reading. That is right for
    history and wrong the moment the gates themselves change: an article
    written under an older standard keeps showing the older verdict, and the
    only way to refresh it used to be a rewrite, which costs an LLM call and
    changes the text you were trying to re-measure.

    So this is the cheap door: no model, no retrieval beyond the article's own
    sources, nothing written but the two verdicts.

    REACS always re-runs — deterministic, needs only the text. QA needs the
    article's sources rebuilt, which can fail if one has moved, so it is
    best-effort and the caller reports when it did not happen rather than
    leaving a stale score looking freshly checked.

    Returns (qa_rechecked, {"qa": (before, after), "reacs": (before, after)}).
    """
    from synthesis.models import Draft, Reference
    from synthesis.validate import validate_draft

    was = {"qa": article.qa_score, "reacs": article.reacs_score}
    content = article.content or ""
    if not content.strip():
        raise PipelineError("This article has no body to score yet.")

    _run_reacs(session, article, content, article.faq or [])

    qa_rechecked = False
    try:
        draft = Draft(
            topic=article.topic.title, region=None, title=article.title,
            slug=article.slug, meta_description=article.meta_description,
            body_markdown=content, faq=article.faq or [],
            references=[Reference(**r) for r in (article.references or [])],
        )
        report = validate_draft(
            draft, _bundle_from_references(article, session),
            keywords=[article.keyword],
            entities=_vendor_entities(session, article.vendor_id),
            internal_links=(article.brief.get("internal_links") or None),
            allowed_link_urls=_known_link_urls(session, article.vendor.name),
            acronyms=known_acronyms(session),
            seo_title=article.meta_title,
            kb=_qa_knowledge(session, article),
            outline=(article.brief or {}).get("outline"))
        _store_qa(article, report)
        qa_rechecked = True
    except Exception:
        logger.exception("QA re-check failed for article %s", article.id)

    session.commit()
    return qa_rechecked, {"qa": (was["qa"], article.qa_score),
                          "reacs": (was["reacs"], article.reacs_score)}


def apply_prose_fixes(session, article: Article,
                      indexes: list[int]) -> tuple[int, int, bool]:
    """Apply chosen proofread suggestions to the body. No LLM call.

    Every stored issue carries a `quote` that review_prose() already verified
    appears verbatim in the body, so applying one is a plain replace of quote
    -> suggestion. That is the whole reason this can be instant and exact
    where "Rewrite one section" is a model call that may change prose nobody
    asked it to touch.

    Returns (applied, skipped, qa_rechecked).

    A suggestion is SKIPPED, never forced, when its quote is no longer in the
    body — the article was edited, or an earlier fix in this same batch
    rewrote the same span. Skipping is not failure: the finding was true when
    it was made and is simply stale, and silently pattern-matching something
    near it would corrupt text the editor did not agree to change.

    Only the FIRST occurrence of each quote is replaced. A quote appearing
    twice is two separate editorial decisions, and the second one still has
    its own finding if the reviewer raised it.

    Applied issues are dropped from prose_review so the list shrinks as it is
    worked through; anything skipped stays visible with its reason.

    Both gates are re-scored afterwards, because the body changed and a stale
    verdict next to edited text is worse than no verdict. REACS always
    re-runs — it is deterministic and needs nothing but the text. QA needs the
    article's sources rebuilt, which can fail if one has moved, so its
    re-check is best-effort and the caller reports when it did not happen
    rather than leaving a stale score looking current.
    """
    issues = list(article.prose_review or [])
    if not issues:
        raise PipelineError("There are no proofread suggestions to apply.")
    wanted = sorted({i for i in indexes if 0 <= i < len(issues)})
    if not wanted:
        raise PipelineError("Tick at least one suggestion to apply.")

    content = article.content or ""
    applied, kept = 0, []
    for pos, issue in enumerate(issues):
        quote = (issue.get("quote") or "")
        suggestion = (issue.get("suggestion") or "")
        if pos not in wanted:
            kept.append(issue)
            continue
        if not quote or not suggestion or quote not in content:
            stale = dict(issue)
            stale["stale"] = True
            kept.append(stale)
            continue
        content = content.replace(quote, suggestion, 1)
        applied += 1

    if not applied:
        raise PipelineError(
            "None of those suggestions could be applied — the text they quote "
            "is no longer in the article. Proofread again to refresh them.")

    content = refresh_last_updated(content)
    article.content = content
    article.word_count = count_words(content)
    article.prose_review = kept or None

    # Same re-score both gates get after any content change. The edit is
    # already made and is what the editor asked for, so a QA re-check that
    # cannot run must not lose it — rescore_article reports that instead.
    qa_rechecked, _scores = rescore_article(session, article)
    return applied, len(wanted) - applied, qa_rechecked


def apply_manual_edit(session, article: Article, *,
                      title: str | None = None, content: str | None = None,
                      meta_title: str | None = None,
                      meta_description: str | None = None,
                      faq: list[dict] | None = None) -> tuple[list[str], bool]:
    """Save an editor's own corrections to the article. No LLM call.

    The escape hatch for when the automated fixes cannot do it. Fix selected
    is one model call that may decline (and says so, after it has been
    charged), a section rewrite moves prose nobody asked it to touch, and some
    findings — a wrong figure, a clumsy sentence, an FAQ answer that misses
    the question — are faster to correct by hand than to describe to a model.

    Nothing is validated before saving: the text is stored exactly as typed
    and both gates are re-scored afterwards, so an edit that breaks something
    is VISIBLE immediately rather than refused. The person reading the finding
    decides what the article should say; this function only records it.

    Only fields the caller passes are considered, and only ones that actually
    differ are written — a form that changed one paragraph must not report an
    edit to five fields, and must not touch the freshness stamp of a body
    nobody edited.

    Returns (changed field names, qa_rechecked).
    """
    from synthesis.text import clamp_title

    if article.status != ArticleStatus.DRAFT:
        raise PipelineError("Only a written draft can be edited by hand.")

    changed: list[str] = []

    if content is not None:
        body = content.replace("\r\n", "\n").strip()
        if not body:
            raise PipelineError(
                "The article body cannot be empty. To start it again, use "
                "Rewrite whole article.")
        if body != (article.content or "").strip():
            # The same freshness handling every other body change gets: the
            # visible "Last updated" line is what REACS reads, and a hand edit
            # is as real an edit as a model's.
            article.content = refresh_last_updated(body)
            article.word_count = count_words(article.content)
            changed.append("body")

    if title is not None:
        clean = clamp_title(" ".join(title.split()), 120)
        if clean and clean != article.title:
            article.title = clean
            # The brief carries the title too and the Brief step writes both.
            # Letting them diverge would show two different titles for one
            # article on two pages of the same flow.
            if isinstance(article.brief, dict):
                brief = dict(article.brief)
                brief["title"] = clean
                article.brief = brief
            changed.append("title")

    for field, value in (("meta_title", meta_title),
                         ("meta_description", meta_description)):
        if value is None:
            continue
        clean = " ".join(value.split())
        if clean != (getattr(article, field) or ""):
            setattr(article, field, clean)
            changed.append(field.replace("_", " "))

    if faq is not None:
        pairs = [{"question": q, "answer": a}
                 for q, a in ((" ".join((f.get("question") or "").split()),
                               (f.get("answer") or "").replace("\r\n", "\n").strip())
                              for f in faq)
                 if q and a]
        # A pair needs both halves: REACS counts ANSWERS, so a question typed
        # without one would raise faq_depth's count while adding nothing a
        # reader can use.
        if pairs != list(article.faq or []):
            article.faq = pairs
            changed.append("FAQ")

    if not changed:
        return [], False

    session.commit()
    # Same re-score every other content change gets. The edit is already made
    # and is what the editor asked for, so a QA re-check that cannot run must
    # not lose it — rescore_article reports that instead.
    qa_rechecked, _scores = rescore_article(session, article)
    return changed, qa_rechecked


def run_prose_review(session, article: Article) -> int:
    """Run the advisory prose review for `article` and store the findings.

    Returns the number of issues found. Deliberately has no pass/fail concept
    and writes no gating flag: nothing in this pipeline is allowed to block on
    a model's opinion of the writing (see synthesis/prose.py). It is run from
    a button, never as part of a write, so its cost is always something the
    operator chose to spend.
    """
    from datetime import datetime, timezone

    from synthesis.prose import review_prose

    if not (article.content or "").strip():
        raise PipelineError("This article has no body to review yet.")

    issues, _model = review_prose(article.content,
                                  vendor_name=article.vendor.name if article.vendor else "")
    article.prose_review = [
        {"quote": i.quote, "problem": i.problem,
         "suggestion": i.suggestion, "kind": i.kind}
        for i in issues
    ]
    article.prose_reviewed_at = datetime.now(timezone.utc)
    session.commit()
    return len(issues)
