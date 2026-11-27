"""The Foundation + Vendor Knowledge Base + Topic Engine database: SQLAlchemy
models + seeding.

This is the ONLY file that talks to the database. config/__init__.py queries
these models and reconstructs the exact dict/list shapes the rest of the
pipeline already expects — see that file for the compatibility contract.

Three different data lifecycles live here:
  - Foundation tables (Vendor, AllowlistDomain, EvergreenKeyword,
    SecurityKeyword, CtaPhrase, AppConfig) are seed-ONCE from the three YAML
    files (kept in the repo as versioned seed data, not deleted). Editing the
    YAML after first run has no effect unless you explicitly call
    reseed_from_yaml(force=True) — deliberately, so restarting the app can't
    silently clobber Vendor Knowledge Base edits made directly in the DB.
  - Vendor Knowledge Base tables (Product, Certification, Course) have NO YAML
    counterpart and are NEVER part of seed_if_empty()/reseed_from_yaml() —
    they're populated entirely through the admin UI (webapp/admin.py).
  - Topic Engine (Topic) likewise has no YAML counterpart — populated only via
    the Topic Engine admin UI (webapp/topics.py), which calls an LLM to
    propose ideas and Python code (synthesis/topics.py) to score them.
  Don't add a YAML file for any of these just because that's the pattern for
  the Foundation tables above.

Enum columns store the Python member NAME (e.g. "VENDOR"), not its value
("vendor") — SQLAlchemy's default Enum() behavior. Always go through the enum
in Python; don't raw-string-compare a status/category column.
"""
from __future__ import annotations

import enum
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml
from sqlalchemy import (JSON, Enum, ForeignKey, Index, String, UniqueConstraint, create_engine,
                        event, func, select)
from sqlalchemy.orm import (DeclarativeBase, Mapped, Session, mapped_column,
                            relationship, sessionmaker)

_CONFIG_DIR = Path(__file__).parent


def _app_db_path_from_dotenv() -> str:
    """APP_DB_PATH out of <repo>/.env, without importing the web app.

    Resolved HERE rather than left to each caller because the database path
    has to be identical for every entry point — gunicorn, a cron job, a
    one-off script. It was not: APP_DB_PATH lived only in the systemd unit,
    so the service used data/app.db while any shell run silently fell back to
    config/app.db and operated on a different, empty database. A scrape run
    from cron would have written 619 rows nobody ever saw.

    Deliberately does not call webapp.app._load_dotenv: config/ must not
    depend on webapp/, and this needs to happen at import time, before any
    caller gets a chance to load anything.
    """
    env_path = _CONFIG_DIR.parent / ".env"
    if not env_path.exists():
        return ""
    try:
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("APP_DB_PATH="):
                return line.partition("=")[2].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _resolve_db_path() -> Path:
    """Where the SQLite file lives.

    Defaults to config/app.db — the historical location, so local development
    and every existing script keep working with no configuration at all.

    APP_DB_PATH overrides it, from the real environment or from <repo>/.env,
    and MUST point outside the deployed source tree on a server. A deploy
    replaces the code directory; with the database inside it, every vendor,
    topic, article and the cached published-post index would be destroyed on
    each release.
    """
    override = (os.getenv("APP_DB_PATH", "").strip()
                or _app_db_path_from_dotenv())
    if not override:
        return _CONFIG_DIR / "app.db"
    path = Path(override).expanduser()
    # Create the parent so a fresh server doesn't need a manual mkdir before
    # first boot; SQLite itself will not create missing directories.
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


_DB_PATH = _resolve_db_path()

_engine = create_engine(f"sqlite:///{_DB_PATH}")
_SessionLocal = sessionmaker(bind=_engine)


@event.listens_for(_engine, "connect")
def _enable_foreign_keys(dbapi_connection, _record):
    """SQLite disables FK constraint enforcement per-connection by default.
    Without this, Course.certification_id (NOT NULL FK) would only be
    validated by the ORM, not the database itself — a raw/bypassing insert
    could still violate it. Fires once per new pooled connection; the pragma
    is per-connection state that persists for that connection's lifetime."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


class Base(DeclarativeBase):
    pass


class DomainCategory(str, enum.Enum):
    """The buckets sources.yaml splits domains into. A closed set — the enum
    column stops a typo from creating an unroutable category.

    JOBS was split out of DATA: salary/vacancy aggregators are legitimate
    sources for a "what does this certification pay?" article and actively
    harmful for any other kind, because a job board has a page for every
    string on earth. A Nutanix NMCAA certification guide cited
    "39 preschool teacher jobs in Cadillac, MI" — Glassdoor matched the
    acronym against the Northwest Michigan Community Action Agency."""
    VENDOR = "vendor"
    AUTHORITY = "authority"
    PRESS = "press"
    DATA = "data"
    JOBS = "jobs"


class Vendor(Base):
    __tablename__ = "vendors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    # Nullable, and MOST rows will be null: only 5 of the ~24 vendors here
    # have a course_links.yaml entry today. See config/__init__.py's
    # load_course_links() for why that must stay filtered on reconstruction.
    course_path: Mapped[str | None] = mapped_column(String, nullable=True)
    # Vendor Knowledge Base: commercial priority (1=P1 highest ... 5=P5), for
    # the not-yet-built Topic Engine to weight topic scoring by. Null = not
    # yet prioritized (or not a real "sold" vendor, just an allowlist source).
    # Assigning real priorities is a business call for marketing to make via
    # the admin UI, not something to invent here.
    priority: Mapped[int | None] = mapped_column(nullable=True)
    # Ordering WITHIN a priority tier, lowest first; null sorts after every
    # ranked vendor and falls back to alphabetical. Display only: topic
    # scoring reads `priority` (the tier), never this, so promoting a vendor
    # inside its tier can't quietly change how its topics score — see
    # synthesis/topics.py's vendor_alignment_from_priority.
    display_rank: Mapped[int | None] = mapped_column(nullable=True)
    # The WordPress category id this vendor's articles are filed under. A
    # deterministic lookup, NOT an AI decision: 23 of 24 vendors already have
    # an exact category on the live site, the article always knows its vendor,
    # and a model could only add cost, latency and the chance of inventing a
    # term that doesn't exist. Null = no category on the site yet (Ivanti),
    # which the Publish step reports rather than guessing at.
    wp_category_id: Mapped[int | None] = mapped_column(nullable=True)

    domains: Mapped[list["AllowlistDomain"]] = relationship(back_populates="vendor")
    products: Mapped[list["Product"]] = relationship(back_populates="vendor")
    certifications: Mapped[list["Certification"]] = relationship(back_populates="vendor")


class AllowlistDomain(Base):
    __tablename__ = "allowlist_domains"

    id: Mapped[int] = mapped_column(primary_key=True)
    domain: Mapped[str] = mapped_column(String, unique=True)
    tier: Mapped[int]
    category: Mapped[DomainCategory] = mapped_column(Enum(DomainCategory))
    vendor_id: Mapped[int | None] = mapped_column(ForeignKey("vendors.id"), nullable=True)
    # Explicit ordering, set at seed time. NOT decorative: this order feeds
    # Allowlist.domains_for_topic() -> SerperClient.search()'s site: query
    # batching (retrieval/search.py) -> which domains survive the result-count
    # cutoff. SQLite does not guarantee row order without ORDER BY, so every
    # query against this table orders by `position` explicitly.
    position: Mapped[int]

    vendor: Mapped["Vendor | None"] = relationship(back_populates="domains")


class ProductStatus(str, enum.Enum):
    CURRENT = "current"
    LEGACY = "legacy"
    END_OF_LIFE = "end_of_life"


class Product(Base):
    """Vendor Knowledge Base: a vendor's product line, with lifecycle status.
    No YAML seed — populated only via the admin UI (webapp/admin.py)."""
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"), nullable=False)
    name: Mapped[str] = mapped_column(String)
    status: Mapped[ProductStatus] = mapped_column(Enum(ProductStatus))

    vendor: Mapped["Vendor"] = relationship(back_populates="products")


class CertificationStatus(str, enum.Enum):
    CURRENT = "current"
    RETIRED = "retired"


class Certification(Base):
    """Vendor Knowledge Base: a vendor's certification, current or retired.

    Retired certifications are the QA Engine's currency-warning tripwire (see
    config/__init__.py's load_retired_certifications() and
    verification/currency.py) — this replaces the old freeform CurrencyFlag
    table entirely (folded in via a one-time migration, not YAML-seeded).
    """
    __tablename__ = "certifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"), nullable=False)
    name: Mapped[str] = mapped_column(String)
    exam_code: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[CertificationStatus] = mapped_column(Enum(CertificationStatus))
    # Is this row an actual CREDENTIAL, or a training course the site scrape
    # filed here? 52 of 121 were the latter: Red Education's own course pages
    # (rededucation.com/coursesvender/...) mirrored into this table, with the
    # COURSE code in exam_code — "SDWSCS", "SPCOR", "PFAA-400-BVP". The Topic
    # Engine reads this table as "the vendor's certifications" and was
    # proposing certification guides about courses, which is the same fusion
    # of course and credential that put "Prisma Access SSE certification" —
    # a credential that does not exist — into a published draft.
    #
    # False does NOT mean the row is wrong or should be deleted: the Course
    # rows hang off it, and the name is a real course name. It means "do not
    # treat this as a credential". Marketing flips it back on /admin/vendors
    # when a row is given a real certification identity.
    is_credential: Mapped[bool] = mapped_column(default=True)
    retired_year: Mapped[int | None] = mapped_column(nullable=True)
    replaced_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("certifications.id"), nullable=True)
    # If set, used VERBATIM as the currency warning (preserves hand-written
    # wording exactly). If absent, load_retired_certifications() auto-builds
    # one from name/vendor/retired_year/replaced_by.
    notes: Mapped[str | None] = mapped_column(String, nullable=True)

    vendor: Mapped["Vendor"] = relationship(back_populates="certifications")
    # remote_side disambiguates the self-join's direction (Certification ->
    # Certification) — SQLAlchemy can't infer parent/child on a self-referential
    # relationship without it and raises a mapper-configuration error at import
    # time otherwise.
    replaced_by: Mapped["Certification | None"] = relationship(
        remote_side="Certification.id")
    courses: Mapped[list["Course"]] = relationship(back_populates="certification")


class Course(Base):
    """Vendor Knowledge Base: a training course tied to a specific
    certification. certification_id is NOT NULL — "a course cannot be created
    without a certification" is enforced at the database level (see the
    PRAGMA foreign_keys=ON connect-event above)."""
    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(primary_key=True)
    certification_id: Mapped[int] = mapped_column(
        ForeignKey("certifications.id"), nullable=False)
    name: Mapped[str] = mapped_column(String)
    duration: Mapped[str | None] = mapped_column(String, nullable=True)
    url: Mapped[str] = mapped_column(String)

    certification: Mapped["Certification"] = relationship(back_populates="courses")


class CertificationVersion(Base):
    """QA rule V1: the software/certification release a given certification
    actually tests, with the source that establishes it.

    KEYED ON certification_id, NEVER ON VENDOR, and that is the whole point.
    Check Point ships R82, but the CCME exam is R81.X while CCSE moved to
    R82 — so "does this vendor have an R82?" is the wrong question and
    answering it is exactly how an article ends up asserting that the CCME
    tests R82. Verification scopes to the certifications the ARTICLE is
    about (see synthesis/validate.py's version check).

    Deliberately NOT derived from Certification.exam_code. That column was
    populated by a scrape which glued release tokens onto codes ("CCSE R82",
    "CCTE R82"), with no source and no verification date — using it as the
    verification key would rubber-stamp the very token the rule exists to
    question. _migrate_clean_exam_code_release_tokens() strips those, and
    everything here arrives with a source_url instead.

    source_url is NOT NULL by design: a version with no provenance is a
    guess, and a guess is what V1 is trying to catch.

    is_current carries parallel versions during a transition — Check Point
    CCTE runs R81.20 and R82 concurrently until the older exam retires — so
    an article citing either is correct and neither should be flagged.
    """
    __tablename__ = "certification_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    certification_id: Mapped[int] = mapped_column(
        ForeignKey("certifications.id"), nullable=False)
    version_label: Mapped[str] = mapped_column(String)          # "R81.X", "R82"
    exam_code: Mapped[str | None] = mapped_column(String, nullable=True)
    is_current: Mapped[bool] = mapped_column(default=True)
    source_url: Mapped[str] = mapped_column(String, nullable=False)
    # HOW the row was established, in words. Not in the reference schema, and
    # worth the extra column: it is the difference between "a human read this
    # off the vendor's courseware PDF" and "this came out of a scrape", which
    # is precisely the question anyone will ask before promoting V1 from warn
    # to block.
    source_note: Mapped[str | None] = mapped_column(String, nullable=True)
    verified_at: Mapped[datetime] = mapped_column(server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    certification: Mapped["Certification"] = relationship()


Index("ix_certification_versions_cert_current",
      CertificationVersion.certification_id, CertificationVersion.is_current)


class SourceSnapshot(Base):
    """The extracted text of a source, exactly as it read when an article was
    written from it. Keyed on URL and shared across articles.

    Two reasons this exists, and the second matters more than the first.

    SPEED AND RELIABILITY: _bundle_from_references() used to re-fetch every
    cited URL on every section rewrite and every targeted fix — measured at
    4.1s for two sources, and up to eight are allowed. Worse, a single fetch
    failure raised and blocked the whole operation, and those failures are
    TRANSIENT: the isaca.org PDF that blocked a re-score returned 74,857
    characters when retried minutes later. Re-scoring an article should not
    depend on someone else's CDN.

    CORRECTNESS, which is the real argument: QA's grounding and originality
    checks ask whether the article's claims trace to the material it was
    WRITTEN FROM. Re-fetching asks whether they trace to whatever that page
    says today. Those are different questions, and only the first one is the
    one QA means. An article correctly grounded in an August page must not
    start failing in October because the vendor edited it.

    So snapshots are deliberately NEVER refreshed. This is an evidentiary
    record, not a cache in the usual sense — `fetched_at` is when the
    evidence was taken, and staleness is the point rather than a defect.
    """
    __tablename__ = "source_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(String, unique=True, index=True)
    domain: Mapped[str] = mapped_column(String, default="")
    title: Mapped[str] = mapped_column(String, default="")
    text: Mapped[str] = mapped_column(String, default="")
    published: Mapped[str | None] = mapped_column(String, nullable=True)  # ISO date
    tier: Mapped[int] = mapped_column(default=2)
    is_vendor: Mapped[bool] = mapped_column(default=False)
    vendor: Mapped[str | None] = mapped_column(String, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SearchIntent(str, enum.Enum):
    INFORMATIONAL = "informational"
    COMMERCIAL = "commercial"
    TRANSACTIONAL = "transactional"
    NAVIGATIONAL = "navigational"


class ContentType(str, enum.Enum):
    TECHNOLOGY_EDUCATION = "technology_education"
    VENDOR_NEWS = "vendor_news"
    SALARY_GUIDE = "salary_guide"
    CERTIFICATION_GUIDE = "certification_guide"
    CAREER_ARTICLE = "career_article"
    # Added for slice 3.9's buyer-question coverage. A comparison has a
    # distinct set of questions a buyer actually has — commercial model,
    # migration cost, roadmap position — and the article that proved the
    # slice (XSOAR vs XSIAM) answered none of them. Without an intent to key
    # on, the Brief cannot require them.
    #
    # Safe to add: SQLite Enum columns in this file are plain VARCHAR with no
    # CHECK constraint, so a new member is a pure Python-side change and
    # needs no migration — the same reasoning recorded on TopicPriority.
    COMPARISON = "comparison"


class TopicPriority(str, enum.Enum):
    CREATE = "create"
    PARKED = "parked"
    RETIRE = "retire"
    # The ported original has 4 more values (refresh/merge/redirect/escalate)
    # belonging to the not-yet-built Analytics+Refresh stage. SQLite Enum
    # columns are plain VARCHAR with no CHECK constraint (confirmed), so
    # adding those members later is a pure Python-side change, no migration.


class Topic(Base):
    """Topic Engine: an AI-proposed, deterministically-scored blog topic idea.

    THE CORE PRINCIPLE (ported from the original): the model estimates raw
    dimensions only (commercial_score/seo_score/ai_score/difficulty); it never
    decides create/park/reject — synthesis/topics.py's scoring formula does,
    in versioned Python code, not a prompt. `priority` starts at whatever the
    formula computes and is human-overridable via the admin UI (an "Approve"
    click just sets it to CREATE).
    """
    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(primary_key=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"), nullable=False)
    title: Mapped[str] = mapped_column(String)
    # COLLATE NOCASE: DB-level case-insensitive uniqueness (with vendor_id
    # below), not just "the parser always lowercases first" — matches the
    # original's `unique index ... (vendor_id, lower(keyword))` as an
    # unconditional guarantee rather than one that depends on every future
    # code path remembering to lowercase before insert.
    keyword: Mapped[str] = mapped_column(String(collation="NOCASE"))
    search_intent: Mapped[SearchIntent] = mapped_column(Enum(SearchIntent))
    content_type: Mapped[ContentType] = mapped_column(Enum(ContentType))
    commercial_score: Mapped[int]
    seo_score: Mapped[int]
    ai_score: Mapped[int]
    difficulty: Mapped[int]
    vendor_alignment: Mapped[int]
    opportunity_score: Mapped[int]
    priority: Mapped[TopicPriority] = mapped_column(Enum(TopicPriority))
    rationale: Mapped[str] = mapped_column(String)
    model: Mapped[str] = mapped_column(String)   # which provider/model produced this idea
    # The theme typed at generation time, kept so later stages can stay on it.
    # Without this the theme died at the topic step: the brief, the retrieval
    # query and the writer never knew a batch was themed, so an "acquisitions"
    # run could produce an article mentioning no acquisition.
    theme: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (UniqueConstraint("vendor_id", "keyword"),)

    vendor: Mapped["Vendor"] = relationship()


class Instructor(Base):
    """REACS Engine (Slice 9): a real, named Red Education instructor who can
    be credited as an article's byline (EEAT — "Technically reviewed by
    ___"). Seeded once from real roster data in _seed() below (see that
    function's comment) — closer in spirit to CtaPhrase's Foundation seed
    data than to Product/Certification/Course's admin-managed rows, since
    this is a small, fixed real-world roster, not day-to-day marketing data.
    No admin CRUD UI exists for this table (matching the reference
    implementation, which never built one either) — add one only if a real
    need for editing the roster through the UI comes up."""
    __tablename__ = "instructors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True)
    credentials: Mapped[str] = mapped_column(String, default="")
    # The WordPress user this instructor would publish as. WP's REST API sets
    # post authorship by NUMERIC USER ID and will not accept a display name,
    # so crediting an instructor as the post author needs a real WP account
    # for them.
    #
    # DECIDED 2026-08-25: those accounts are NOT being created. Every row here
    # stays null and every article publishes under the shared account in
    # WP_AUTHOR_ID, which is how the live site already publishes. This is a
    # settled decision, not an unfinished migration — do not read null as a
    # gap waiting to be filled.
    #
    # The column and the precedence branch in wp_author_for() are kept because
    # they cost nothing and the null-means-shared-account path is exactly what
    # makes today's behaviour work. The instructor is still credited in the
    # BODY, by REACS's "Technically reviewed by ___" line, which is a separate
    # thing entirely from who owns the WordPress post.
    wp_user_id: Mapped[int | None] = mapped_column(nullable=True)
    bio: Mapped[str] = mapped_column(String, default="")
    active: Mapped[bool] = mapped_column(default=True)


class InstructorVendor(Base):
    """Plain many-to-many: which vendors an instructor is credentialed to
    write about. Read by synthesis/reacs.py's pick_instructor() to prefer a
    vendor-matched byline over a random one."""
    __tablename__ = "instructor_vendors"

    instructor_id: Mapped[int] = mapped_column(ForeignKey("instructors.id"), primary_key=True)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"), primary_key=True)


class ArticleStatus(str, enum.Enum):
    BRIEF = "brief"
    DRAFT = "draft"
    # The ported original has more lifecycle values (qa/review/scheduled/
    # published/refresh) belonging to not-yet-built stages 6-8's persistence
    # needs. This app's Stage 6 QA already runs inline (see qa_passed/
    # qa_checks below) rather than as a separate status transition.


class Article(Base):
    """Content Brief + Writer: an approved Topic turned into a structured
    brief, then (optionally) into a full written article.

    topic_id/vendor_id are NOT NULL — every Article in this pipeline is
    created FROM an approved Topic (itself always vendor-attached), mirroring
    the Course.certification_id precedent above ("can't exist without its
    parent," enforced at the database level). vendor_id is denormalized off
    the topic at creation time for convenient querying. No ondelete is set on
    either FK (plain RESTRICT under PRAGMA foreign_keys=ON, consistent with
    every other FK in this file) — CASCADE would silently destroy a brief/
    article representing real LLM spend if its source Topic were deleted;
    SET NULL isn't valid against a NOT NULL column. Deleting a Topic that
    already has an Article must be refused with a friendly error, not crash
    — see webapp/topics.py's delete() route.

    brief/faq/qa_checks are SQLAlchemy JSON columns (plain TEXT + json.dumps/
    loads on SQLite) — always REASSIGN the whole dict/list
    (`article.qa_checks = [...]`), never mutate one in place (`.append(...)`)
    or the change won't be tracked as dirty and silently won't be saved.
    """
    __tablename__ = "articles"
    __table_args__ = (
        Index("uq_articles_topic_id", "topic_id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id"), nullable=False)
    vendor_id: Mapped[int] = mapped_column(ForeignKey("vendors.id"), nullable=False)
    title: Mapped[str] = mapped_column(String)
    slug: Mapped[str] = mapped_column(String, unique=True)
    keyword: Mapped[str] = mapped_column(String)
    brief: Mapped[dict] = mapped_column(JSON)              # the ContentBrief, whole
    content: Mapped[str] = mapped_column(String, default="")
    meta_title: Mapped[str] = mapped_column(String, default="")
    meta_description: Mapped[str] = mapped_column(String, default="")
    excerpt: Mapped[str] = mapped_column(String, default="")
    cta: Mapped[str] = mapped_column(String, default="")
    faq: Mapped[list] = mapped_column(JSON, default=list)   # [{"question":..,"answer":..}]
    # [{"tag":"S1","url":..,"title":..,"domain":..}, ...] — the [S#] -> real
    # source URL mapping, so the written content's inline [S#] markers can be
    # rendered as real links (synthesis/models.py's linkify_citations())
    # instead of dead bracket text. Populated once, at write time.
    references: Mapped[list] = mapped_column(JSON, default=list)
    word_count: Mapped[int] = mapped_column(default=0)
    model: Mapped[str] = mapped_column(String, default="")
    status: Mapped[ArticleStatus] = mapped_column(Enum(ArticleStatus))
    qa_passed: Mapped[bool | None] = mapped_column(nullable=True)
    qa_checks: Mapped[list | None] = mapped_column(JSON, nullable=True)  # [{name,status,detail}]
    # Reliability-QA quality score, 0-100, same -1 = "never computed" sentinel
    # as reacs_score below. Stored rather than computed on read so it is a
    # record of what the article scored WHEN IT WAS WRITTEN: qa_checks holds
    # exactly the checks that ran, so recomputing gives the same answer today,
    # but the moment the tiers or weights are retuned every historical article
    # would silently re-grade. reacs_score is stored for the same reason.
    # It grades quality only — qa_passed remains the gate, still "nothing
    # failed", and is deliberately not a function of this number.
    qa_score: Mapped[int] = mapped_column(default=-1)

    @property
    def qa_score_band(self) -> str:
        """"none" | "fail" | "warn" | "pass" for the stored QA score, via the
        same single band function a live QAReport uses — so a score shown in
        the review panel can't be coloured differently from the same score in
        the engine that produced it."""
        from synthesis.models import qa_score_band as _band
        return _band(self.qa_score, self.qa_passed is False)
    # REACS Engine (Slice 9): a second, separate quality gate alongside QA
    # above — never merged with qa_passed/qa_checks (see synthesis/reacs.py's
    # module docstring for why). reacs_score's -1 sentinel (never a valid
    # score 0-100) means "REACS never computed", mirroring the reference
    # schema's own CHECK constraint intent (SQLite Enum columns in this file
    # already have no CHECK constraint either, so this is documentation, not
    # enforcement). reacs_passed mirrors qa_passed's bool-for-gating role.
    reacs_report: Mapped[list | None] = mapped_column(JSON, nullable=True)  # [{pillar,tier,passed,message}]
    reacs_score: Mapped[int] = mapped_column(default=-1)
    reacs_passed: Mapped[bool | None] = mapped_column(nullable=True)
    # Stage 7 (WordPress Publisher). wp_post_id is the DRAFT's id in
    # WordPress — this tool never creates anything else. published_at stays
    # null until a human publishes it there; nothing here sets it, and that
    # asymmetry is the point: pushed_at is ours, published_at is theirs.
    wp_post_id: Mapped[int | None] = mapped_column(nullable=True)
    wp_link: Mapped[str | None] = mapped_column(String, nullable=True)
    pushed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # Advisory prose review (synthesis/prose.py) — run on demand from a button,
    # never automatically. Stored so the findings survive a page reload; there
    # is deliberately no prose_passed flag, because nothing is allowed to gate
    # on a model's opinion of the writing.
    # {check name: attempts} for checks that were TICKED for a targeted fix
    # and were still failing when the pass finished. Not a defect ledger and
    # not history — just enough to warn before the same AI call is spent
    # twice. A check that clears is removed; a full rewrite clears the lot,
    # because past attempts say nothing about text that no longer exists.
    unresolved_fixes: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    prose_review: Mapped[list | None] = mapped_column(JSON, nullable=True)
    prose_reviewed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # The instructor byline assigned at write time (webapp/pipeline.py),
    # picked once and never re-rolled on rewrite — see synthesis/reacs.py's
    # pick_instructor().
    byline_instructor_id: Mapped[int | None] = mapped_column(
        ForeignKey("instructors.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())

    topic: Mapped["Topic"] = relationship()
    vendor: Mapped["Vendor"] = relationship()
    byline_instructor: Mapped["Instructor | None"] = relationship()


class PublishedPost(Base):
    """A real, already-published post on rededucation.com — scraped from the
    site's own post-sitemap, not generated by this app. Powers Article
    Studio's Topic-step content-gap lane and Brief-step internal-link
    suggestions (synthesis/gaps.py does the matching; nothing here is
    vendor-tagged at write time — relevance is computed at query time via
    keyword overlap, so improving the matcher later never needs a migration).
    Refreshed by re-running scripts/scrape_published_posts.py; safe to
    re-run — upserts on `url`.
    """
    __tablename__ = "published_posts"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String)
    url: Mapped[str] = mapped_column(String, unique=True)
    published_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # Which sitemap this came from: "post" | "course" | "page" | "case_study".
    # A plain string, not an Enum, because the set is defined by the site's
    # sitemap index rather than by this app, and a new content type should not
    # need a schema change to be ingested.
    #
    # It exists to keep two consumers apart (synthesis/gaps.py): the Topic
    # step's content-gap tag must only ever consider "post" — a COURSE page
    # existing is not evidence that an ARTICLE has been written, and counting
    # it would tell the editor to skip topics worth writing — while the Brief
    # step's internal-link suggestions want posts AND courses AND pages,
    # since a course page is usually the single most useful internal link an
    # article can carry.
    kind: Mapped[str] = mapped_column(String, default="post", server_default="post")


class EvergreenKeyword(Base):
    __tablename__ = "evergreen_keywords"

    id: Mapped[int] = mapped_column(primary_key=True)
    phrase: Mapped[str] = mapped_column(String)


class SecurityKeyword(Base):
    __tablename__ = "security_keywords"

    id: Mapped[int] = mapped_column(primary_key=True)
    keyword: Mapped[str] = mapped_column(String)


class CtaPhrase(Base):
    __tablename__ = "cta_phrases"

    id: Mapped[int] = mapped_column(primary_key=True)
    phrase: Mapped[str] = mapped_column(String)
    # Order shows up verbatim in the LLM prompt (synthesis/prompt.py joins
    # these with "\n") — kept deterministic so prompts don't vary run to run.
    position: Mapped[int]


class ApiUsage(Base):
    """One row per external paid API call — the app's own usage meter.

    Written from the three chokepoints every paid call funnels through
    (synthesis/llm.py's provider call, retrieval/search.py's Serper search,
    retrieval/serp.py's plain SERP search), via record_api_usage() below.
    This is a LOCAL tally: an accurate meter of THIS app's consumption and a
    per-feature attribution the provider dashboards can't give, but the
    dashboards stay authoritative for remaining balance/quota (neither
    Serper nor Gemini exposes a balance API).

    created_at is a PYTHON-side local datetime (not server_default UTC) on
    purpose: the /admin/usage page groups by local calendar day, and this is
    a single-machine tool — SQLite's CURRENT_TIMESTAMP would be UTC and
    split days at 05:30 IST.
    """
    __tablename__ = "api_usage"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String)   # "serper" | "gemini" | "llm" | "anthropic"
    purpose: Mapped[str] = mapped_column(String)    # e.g. "topics", "article", "retrieval", "serp_difficulty"
    model: Mapped[str] = mapped_column(String, default="")
    credits: Mapped[int] = mapped_column(default=0)          # serper credits
    prompt_tokens: Mapped[int] = mapped_column(default=0)    # LLM usage, provider-reported
    completion_tokens: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(default=datetime.now, index=True)


class DomainSightingStatus(str, enum.Enum):
    PENDING = "pending"       # seen, awaiting a human decision
    IGNORED = "ignored"       # decided against; stop showing it


class DomainSighting(Base):
    """A domain the open web keeps returning for our topics that is NOT on the
    allowlist.

    Retrieval unions a scoped site:-filtered search with one unconstrained
    search, then filters BOTH through the allowlist, so nothing new can be
    ingested. What this table records is what the second channel had to throw
    away — which is the only honest way to find out that gartner.com ranks for
    our topics and nobody ever added it.

    A queue for a human, never an input to retrieval. Measured on real
    queries, the same channel also surfaces 591lab, certlibrary and
    testpassport, and a Cellebrite page for a Check Point exam code — so
    frequency here is evidence worth looking at, and nothing more than that.
    """
    __tablename__ = "domain_sightings"

    id: Mapped[int] = mapped_column(primary_key=True)
    domain: Mapped[str] = mapped_column(String, unique=True, index=True)
    hits: Mapped[int] = mapped_column(default=1)
    last_url: Mapped[str] = mapped_column(String, default="")
    last_title: Mapped[str] = mapped_column(String, default="")
    last_query: Mapped[str] = mapped_column(String, default="")
    status: Mapped[DomainSightingStatus] = mapped_column(
        Enum(DomainSightingStatus), default=DomainSightingStatus.PENDING)
    first_seen: Mapped[datetime] = mapped_column(server_default=func.now())
    last_seen: Mapped[datetime] = mapped_column(server_default=func.now())


def record_domain_sighting(domain: str, url: str = "", title: str = "",
                           query: str = "") -> None:
    """Best-effort: a bookkeeping failure must never break the retrieval it
    was observing — same contract as record_api_usage below."""
    domain = (domain or "").strip().lower()
    if not domain:
        return
    try:
        with get_session() as s:
            row = s.scalar(select(DomainSighting).where(
                DomainSighting.domain == domain))
            if row is None:
                s.add(DomainSighting(domain=domain, hits=1, last_url=url or "",
                                     last_title=title or "",
                                     last_query=query or ""))
            else:
                row.hits += 1
                row.last_url = url or row.last_url
                row.last_title = title or row.last_title
                row.last_query = query or row.last_query
                row.last_seen = datetime.now(timezone.utc)
            s.commit()
    except Exception:
        pass


def record_api_usage(provider: str, purpose: str, model: str = "",
                     credits: int = 0, prompt_tokens: int = 0,
                     completion_tokens: int = 0) -> None:
    """Best-effort usage recording — a metering failure must NEVER break the
    call it was metering, so everything is swallowed (the call already
    succeeded and cost real money by the time this runs)."""
    try:
        with get_session() as s:
            s.add(ApiUsage(provider=provider, purpose=purpose, model=model,
                           credits=credits, prompt_tokens=prompt_tokens,
                           completion_tokens=completion_tokens))
            s.commit()
    except Exception:
        pass


class AppConfig(Base):
    """Singleton row (id=1): the handful of unrelated scalars that used to be
    top-level/nested keys in settings.yaml + course_links.yaml."""
    __tablename__ = "app_config"

    id: Mapped[int] = mapped_column(primary_key=True)
    max_age_days: Mapped[int]
    min_independent_domains: Mapped[int]
    stale_year_lookback: Mapped[int]
    course_base_url: Mapped[str] = mapped_column(String)
    # When scrape_published_posts.py last completed. The "live on
    # rededucation.com" counts on the vendor step are only as current as this,
    # and they were previously undated — a number nobody could tell was six
    # weeks old. Null means the scrape has not run since this column existed.
    coverage_scraped_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # Serper has no balance API, so "credits left" is derived: a baseline
    # minus everything this app has tracked. It drifts whenever credits are
    # spent outside this server — a script run from a laptop hits the same
    # account and lands in a different database — and it drifted by 97, which
    # is 52% of what it was reporting.
    #
    # These two make reconciling a form field instead of an .env edit and a
    # restart, and they record WHEN it was last done so a stale estimate can
    # look stale. Null means fall back to SERPER_CREDITS_START.
    serper_balance: Mapped[int | None] = mapped_column(nullable=True)
    serper_balance_at: Mapped[datetime | None] = mapped_column(nullable=True)
    serper_balance_tracked: Mapped[int | None] = mapped_column(nullable=True)


def get_session() -> Session:
    return _SessionLocal()


# --------------------------------------------------------------------------- #
# Seeding
# --------------------------------------------------------------------------- #
def _load_yaml(name: str) -> dict:
    with open(_CONFIG_DIR / name) as f:
        return yaml.safe_load(f)


def _seed(session: Session) -> None:
    sources = _load_yaml("sources.yaml")
    settings = _load_yaml("settings.yaml")
    course_links = _load_yaml("course_links.yaml")

    # Vendors: union of sources.yaml's tier1_vendor names and course_links.yaml's
    # vendor keys (today the latter is a subset of the former, but don't assume
    # that ordering — same vendor name string is the join key in both files).
    vendor_course_paths = course_links.get("vendors", {})
    vendor_names = list(dict.fromkeys(  # de-dup, keep first-seen order
        [e["vendor"] for e in sources.get("tier1_vendor", [])] +
        list(vendor_course_paths.keys())
    ))
    vendor_rows: dict[str, Vendor] = {}
    for name in vendor_names:
        v = Vendor(name=name, course_path=vendor_course_paths.get(name))
        session.add(v)
        vendor_rows[name] = v
    session.flush()  # assign ids so AllowlistDomain.vendor_id can reference them

    # Allowlist domains: same categories as sources.yaml's lists, in file
    # order (position = global running index, matching how Allowlist._ingest
    # appends into one dict across those lists in this exact sequence today).
    position = 0
    category_map = [
        ("tier1_vendor", DomainCategory.VENDOR, 1, True),
        ("tier1_authority", DomainCategory.AUTHORITY, 1, False),
        ("tier2_press", DomainCategory.PRESS, 2, False),
        ("tier2_data", DomainCategory.DATA, 2, False),
        ("tier2_jobs", DomainCategory.JOBS, 2, False),
    ]
    for key, category, tier, is_vendor in category_map:
        for e in sources.get(key, []):
            vendor_id = vendor_rows[e["vendor"]].id if is_vendor else None
            session.add(AllowlistDomain(
                domain=e["domain"].lower().strip(),
                tier=tier,
                category=category,
                vendor_id=vendor_id,
                position=position,
            ))
            position += 1

    for phrase in settings.get("recency", {}).get("evergreen_keywords", []):
        session.add(EvergreenKeyword(phrase=phrase))

    for kw in settings.get("corroboration", {}).get("security_keywords", []):
        session.add(SecurityKeyword(keyword=kw))

    for i, phrase in enumerate(course_links.get("cta_phrases", [])):
        session.add(CtaPhrase(phrase=phrase, position=i))

    session.add(AppConfig(
        id=1,
        max_age_days=settings["recency"]["max_age_days"],
        min_independent_domains=settings["corroboration"]["min_independent_domains"],
        stale_year_lookback=settings.get("stale_year_lookback", 2),
        course_base_url=course_links["base_url"],
    ))

    session.commit()


# Real Red Education instructor roster (REACS Engine, Slice 9) — a small,
# fixed real-world list, seeded once like CtaPhrase's Foundation data, not
# admin-managed like Product/Certification/Course. Vendor names here must
# match Vendor.name exactly; a name not found among existing vendors is
# silently skipped (mirrors the reference seed's own comment: don't invent a
# vendor mapping for one that isn't onboarded yet).
# CORRECTED FROM RED EDUCATION. The v1.3 documents are briefs, not roster
# sources: Travis Loke teaches AWS and Palo Alto Networks. On 7 Sep 2026 Red
# Education explicitly confirmed that every F5 article must be assigned to
# Rodolfo Nutzmann, so F5 is intentionally part of his coverage below.
#
# The code pack's own instruction applies here — "seed instructor_vendors
# from certification records, not from existing article bylines" — and this
# correction came from Red Education directly rather than from the document
# or from a byline.
#
# Vendors without a mapped instructor still fall back to the active roster,
# rotated. F5 does not: Rodolfo is its sole eligible instructor.
_INSTRUCTOR_ROSTER = [
    ("Jerry Solomon", "Certified Instructor — Palo Alto Networks, Cisco, Nutanix",
     ["Palo Alto Networks", "Cisco", "Nutanix"]),
    ("Travis Loke", "Certified Instructor — AWS, Palo Alto Networks",
     ["AWS", "Palo Alto Networks"]),
    ("Dan Schultz", "Certified Instructor — Palo Alto Networks, Check Point",
     ["Palo Alto Networks", "Check Point"]),
    ("Rodolfo Nutzmann", "Certified Instructor — F5, Arista Networks, Extreme Networks",
     ["F5", "Arista Networks", "Extreme Networks"]),
]


def _seed_instructors_if_empty(session: Session) -> None:
    """Independent of _seed()'s Vendor-empty gate above: _seed() only ever
    fires on a brand-new database, but this table needs to reach an already-
    seeded, already-in-use database too (this app's first real users predate
    REACS). Checked and seeded on its own, every startup, no-op once
    populated."""
    if session.scalar(select(Instructor).limit(1)) is not None:
        return
    vendor_by_name = {v.name: v for v in session.query(Vendor).all()}
    for name, credentials, vendor_names in _INSTRUCTOR_ROSTER:
        instructor = Instructor(name=name, credentials=credentials)
        session.add(instructor)
        session.flush()  # assign id for InstructorVendor rows below
        for vname in vendor_names:
            vendor = vendor_by_name.get(vname)
            if vendor is not None:
                session.add(InstructorVendor(instructor_id=instructor.id, vendor_id=vendor.id))
    session.commit()


def _migrate_reconcile_instructor_roster() -> None:
    """Bring an already-seeded database's instructor coverage back in line
    with _INSTRUCTOR_ROSTER above.

    Needed because _seed_instructors_if_empty() only fires on an EMPTY table,
    so a correction to the roster would otherwise never reach a database that
    was seeded before it — including the deployed one. Two of the four entries
    were wrong until 25 Aug 2026.

    Safe to reconcile rather than merge: there is deliberately no admin CRUD
    for instructor_vendors (see the Instructor docstring), so nothing in this
    table is a hand edit that could be clobbered. Idempotent, and a no-op once
    the data matches.

    Only touches instructors named in the roster. An instructor added by hand
    later is left entirely alone."""
    with _engine.begin() as conn:
        tables = {row[0] for row in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"instructors", "instructor_vendors", "vendors"} <= tables:
            return
        vendor_id = {name: vid for vid, name in conn.exec_driver_sql(
            "SELECT id, name FROM vendors")}
        for name, credentials, vendor_names in _INSTRUCTOR_ROSTER:
            row = conn.exec_driver_sql(
                "SELECT id, credentials FROM instructors WHERE name = ?",
                (name,)).fetchone()
            if row is None:
                continue
            iid, current_credentials = row
            wanted = {vendor_id[v] for v in vendor_names if v in vendor_id}
            have = {r[0] for r in conn.exec_driver_sql(
                "SELECT vendor_id FROM instructor_vendors WHERE instructor_id = ?",
                (iid,))}
            for vid in have - wanted:
                conn.exec_driver_sql(
                    "DELETE FROM instructor_vendors WHERE instructor_id = ? "
                    "AND vendor_id = ?", (iid, vid))
            for vid in wanted - have:
                conn.exec_driver_sql(
                    "INSERT INTO instructor_vendors (instructor_id, vendor_id) "
                    "VALUES (?, ?)", (iid, vid))
            # The credential line is RENDERED in the byline, so leaving it
            # stale would print vendors the instructor does not teach — the
            # exact M7(c) defect the standard calls a live misrepresentation.
            if current_credentials != credentials:
                conn.exec_driver_sql(
                    "UPDATE instructors SET credentials = ? WHERE id = ?",
                    (credentials, iid))

        # Red Education's rule is stronger than ordinary eligibility: F5
        # articles always use Rodolfo. Remove any stale/manual F5 eligibility
        # from other instructors and repair existing article bylines too, so
        # an old draft cannot keep the fallback assignment it received before
        # this mapping existed. Idempotent and enforced on every startup.
        f5_id = vendor_id.get("F5")
        rodolfo = conn.exec_driver_sql(
            "SELECT id FROM instructors WHERE name = ?",
            ("Rodolfo Nutzmann",)).fetchone()
        if f5_id is not None and rodolfo is not None:
            rodolfo_id = rodolfo[0]
            conn.exec_driver_sql(
                "DELETE FROM instructor_vendors "
                "WHERE vendor_id = ? AND instructor_id != ?",
                (f5_id, rodolfo_id))
            conn.exec_driver_sql(
                "INSERT OR IGNORE INTO instructor_vendors "
                "(instructor_id, vendor_id) VALUES (?, ?)",
                (rodolfo_id, f5_id))
            conn.exec_driver_sql(
                "UPDATE articles SET byline_instructor_id = ? "
                "WHERE vendor_id = ? AND "
                "(byline_instructor_id IS NULL OR byline_instructor_id != ?)",
                (rodolfo_id, f5_id, rodolfo_id))


def _migrate_add_columns() -> None:
    """Lightweight, additive-only migration: ALTER TABLE ADD COLUMN for any
    column a model declares that an already-existing SQLite table is missing.

    This app has no Alembic-style migration framework — Base.metadata.
    create_all() only creates entirely NEW tables; a column added to an
    EXISTING table's model never reaches a database that predates it without
    this. Additive-only by construction (only ever ADDs a column, never
    drops/renames/alters one), so it's always safe to run unconditionally on
    every startup."""
    additions = {
        "app_config": [
            ("coverage_scraped_at", "DATETIME"),
            ("serper_balance", "INTEGER"),
            ("serper_balance_at", "DATETIME"),
            ("serper_balance_tracked", "INTEGER"),
        ],
        "published_posts": [
            ("kind", "VARCHAR NOT NULL DEFAULT 'post'"),
        ],
        "topics": [
            ("theme", "VARCHAR"),
        ],
        "vendors": [
            ("display_rank", "INTEGER"),
            ("wp_category_id", "INTEGER"),
        ],
        "instructors": [
            ("wp_user_id", "INTEGER"),
        ],
        "certifications": [
            ("is_credential", "BOOLEAN NOT NULL DEFAULT 1"),
        ],
        "articles": [
            ("qa_score", "INTEGER NOT NULL DEFAULT -1"),
            ("wp_post_id", "INTEGER"),
            ("wp_link", "VARCHAR"),
            ("pushed_at", "DATETIME"),
            ("published_at", "DATETIME"),
            ("prose_review", "JSON"),
            ("unresolved_fixes", "JSON"),
            ("prose_reviewed_at", "DATETIME"),
            ("reacs_report", "JSON"),
            ("reacs_score", "INTEGER NOT NULL DEFAULT -1"),
            ("reacs_passed", "BOOLEAN"),
            ("byline_instructor_id", "INTEGER REFERENCES instructors(id)"),
        ],
    }
    with _engine.begin() as conn:
        for table, columns in additions.items():
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            for name, decl in columns:
                if name not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _migrate_unique_article_topics() -> None:
    """Make the Topic -> Article relationship one-to-one.

    The Studio checks for an existing article before doing the long brief
    generation call, but two overlapping requests can both pass that check
    before either commits. The unique index is the final authority across
    threads and processes. It deliberately fails startup if historical
    duplicates exist instead of silently deleting generated editorial work.
    """
    with _engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_articles_topic_id "
            "ON articles(topic_id)")


def _migrate_drop_analytics() -> None:
    """Drop the Stage 8 analytics table — the feature was removed 2026-08-05
    at the user's request ("of no use for now").

    Same shape as _migrate_drop_cluster_rule below and safe for the same
    reasons: idempotent, a no-op once applied, and it runs unconditionally on
    startup so a database created before the removal converges. The table was
    empty when it went (verified), so nothing was lost. If analytics comes
    back, restore the model and delete this — do NOT leave both."""
    with _engine.begin() as conn:
        tables = {row[0] for row in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "analytics_snapshots" in tables:
            conn.exec_driver_sql("DROP TABLE analytics_snapshots")


def _migrate_drop_cluster_rule() -> None:
    """Removes the Cluster Rule (Slice 10) schema — site_links, and the
    pillar_id columns that referenced it — per the decision to drop pillar/
    cluster functionality while keeping the rest of REACS (scoring, byline)
    intact. Idempotent (checks existence first), symmetric with
    _migrate_add_columns above: safe to run unconditionally on every
    startup, and a no-op forever once applied. Column drops before the table
    drop, since SQLite ALTER TABLE DROP COLUMN on a column that still
    REFERENCES a table works fine, but dropping site_links out from under an
    existing FK reference is the wrong order to attempt first."""
    with _engine.begin() as conn:
        for table in ("topics", "articles"):
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if "pillar_id" in existing:
                conn.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN pillar_id")
        tables = {row[0] for row in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "site_links" in tables:
            conn.exec_driver_sql("DROP TABLE site_links")


def _migrate_clean_exam_code_release_tokens() -> None:
    """Strip release tokens that a scrape glued onto exam codes: "CCSE R82"
    -> "CCSE", "CCTE R81" -> "CCTE".

    An exam code identifies the exam; the release it targets is a separate,
    dated, sourced fact and now lives in certification_versions. Leaving the
    two fused had three costs: known_acronyms() taught the casing fixer that
    "CCSE R82" was one acronym, article titles inherited the version from the
    KB (which is how a draft ended up titled "The Complete CCTE R82
    Certification Guide"), and QA rule V1 would have accepted an unsourced
    scrape as proof that a version was real.

    Idempotent and additive in spirit: it only ever removes a trailing
    release token, never touches a code without one, and is a no-op once
    applied. Certification.name is deliberately left alone — those are
    marketing display strings, not identifiers."""
    with _engine.begin() as conn:
        tables = {row[0] for row in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "certifications" not in tables:
            return
        rows = conn.exec_driver_sql(
            "SELECT id, exam_code FROM certifications "
            "WHERE exam_code IS NOT NULL").fetchall()
        for cid, code in rows:
            cleaned = re.sub(r"\s+R\s?8\d(?:\.\d{1,2}|\.X)?$", "", code or "",
                             flags=re.I).strip()
            if cleaned and cleaned != code:
                conn.exec_driver_sql(
                    "UPDATE certifications SET exam_code = ? WHERE id = ?",
                    (cleaned, cid))


def _migrate_split_duplicate_exam_codes() -> None:
    """Stop a RETIRED certification sharing an exam code with the CURRENT one.

    QA rule V1 keys on exam_code, so two certifications sharing a code land in
    the same bucket and their version rows merge — which means an article
    claiming a retired release is current would verify against it. That defeats
    the per-certification guarantee the whole rule is built on.

    Check Point had two: CCTE and CCTA each existed as a current row and a
    retired R81 predecessor. In truth these are ONE certification at two
    versions, which is what certification_versions now records; the duplicate
    rows date from before that table existed, when the only way to model a
    version was another certification.

    NON-DESTRUCTIVE by choice. The retired rows are kept and only their
    exam_code is cleared, because:
      - load_retired_certifications() matches on `name`, not exam_code, so the
        currency-warning tripwire keeps working untouched;
      - known_acronyms() still gets CCTE and CCTA from the current rows;
      - deleting a historical certification would destroy a record that may
        well be real, to fix a keying problem that clearing one column solves.

    Deliberately narrow: it only touches a code shared by exactly one CURRENT
    row and one or more RETIRED ones, which is decidable. Codes shared by two
    CURRENT certifications (F5's "LTM", Extreme Networks' "Management" and
    "Advanced Configuration") are genuinely ambiguous and are LEFT ALONE — they
    gate nothing today, since V1 is Check Point-only.
    """
    with _engine.begin() as conn:
        tables = {row[0] for row in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "certifications" not in tables:
            return
        rows = conn.exec_driver_sql(
            "SELECT id, vendor_id, upper(exam_code), status FROM certifications "
            "WHERE exam_code IS NOT NULL AND exam_code != ''").fetchall()
        groups: dict[tuple, list] = {}
        for cid, vendor_id, code, status in rows:
            groups.setdefault((vendor_id, code), []).append((cid, status))
        for (_vendor_id, _code), members in groups.items():
            if len(members) < 2:
                continue
            current = [m for m in members if m[1] == "CURRENT"]
            retired = [m for m in members if m[1] == "RETIRED"]
            if len(current) == 1 and retired:
                for cid, _ in retired:
                    conn.exec_driver_sql(
                        "UPDATE certifications SET exam_code = NULL WHERE id = ?",
                        (cid,))


def _migrate_flag_course_shaped_certifications() -> None:
    """Mark the rows in `certifications` that are really training courses.

    The Vendor KB was seeded from a scrape of Red Education's own site, and
    Course.certification_id is NOT NULL, so every scraped course had to be
    given a parent certification row. For 52 of 121 rows that parent IS the
    course: the certification and its only course carry the identical name,
    and the "exam_code" is a course code (SDWSCS, SPCOR, PFAA-400-BVP), not an
    exam registration code. Cisco, Ping Identity, Extreme Networks and
    Fortinet were 100% course rows.

    That fed the Topic Engine a list of "certifications" made of course names,
    which is how a certification guide comes to be written about a course —
    the defect that shipped as "Prisma Access SSE certification", a credential
    that does not exist.

    Two conditions, both required, so a real credential can never be demoted:
    the certification's own course carries the same name, AND the row has no
    recorded CertificationVersion. Measured before writing: 0 of the 52 have a
    version, and all 69 properly-recorded credentials keep is_credential.

    Nothing is deleted or renamed. Guessing the real credential name from a
    course name is exactly the fabrication this whole change exists to stop —
    that needs the vendor's own page, and it is Marketing's call."""
    from sqlalchemy import select as _select

    with get_session() as session:
        versioned = {v.certification_id for v in
                     session.scalars(_select(CertificationVersion)).all()}
        vendor_names = {v.id: v.name for v in session.scalars(
            _select(Vendor)).all()}

        def identity(name: str, vendor_name: str) -> str:
            """Compare catalogue labels after harmless vendor branding.

            Red Education course titles commonly start with the vendor while
            their synthetic parent row does not ("Nutanix X" versus "X").
            They are still the same course-shaped record.
            """
            value = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
            vendor = re.sub(r"[^a-z0-9]+", " ", vendor_name.lower()).strip()
            if vendor and value.startswith(vendor + " "):
                value = value[len(vendor):].strip()
            return value

        def credential_shaped(name: str) -> bool:
            """Names that identify credentials even when a course shares it.

            EPI's "Certified Data Centre Professional", Netskope One
            Professional and Check Point Threat Prevention Specialist all
            have a same-named catalogue course and no CertificationVersion.
            Demoting those would repeat the identity error in reverse.
            """
            return bool(re.search(
                r"^(?:certified\b|.+\bcertificate\b)|"
                r"\b(?:professional|administrator|specialist|expert|"
                r"consultant|auditor)\s*(?:\([^)]*\))?$",
                (name or "").strip(), re.I))

        changed = 0
        for cert in session.scalars(_select(Certification)).all():
            vendor_name = vendor_names.get(cert.vendor_id, "")
            course_shaped = any(
                identity(c.name or "", vendor_name)
                == identity(cert.name or "", vendor_name)
                for c in cert.courses)
            should_be = not (
                course_shaped
                and cert.id not in versioned
                and not credential_shaped(cert.name))
            # DERIVED, and only ever turns a row OFF. Flipping the flag by
            # hand does not stick, because the next startup recomputes it from
            # the same two facts — that is deliberate. The way to make a row a
            # credential is to give it a credential's identity: a real name
            # from the vendor's own page, and a recorded CertificationVersion.
            # Then it is no longer course-shaped and this leaves it alone.
            if cert.is_credential and not should_be:
                cert.is_credential = False
                changed += 1
        if changed:
            session.commit()


def _migrate_add_vendor_apex_domains() -> None:
    """Give Check Point the apex domain every other vendor already has.

    Its vendor allowlist held only research.checkpoint.com and
    blog.checkpoint.com — two research/marketing blogs. Not checkpoint.com,
    and not support.checkpoint.com, which is where the certification pages and
    the SK articles live (sk163417 is what settles which release each Check
    Point exam is at). So a scoped search for a Check Point certification
    could reach the company's blog and nothing else, and the CCTE and CCME
    guides were both written without one page from Check Point.

    Idempotent, and deliberately narrow: only a vendor with NO apex domain
    gets one, derived from a subdomain it already has, and only when the
    apex's own brand label matches the vendor's name. That last condition is
    load-bearing — without it AWS, whose only domain is aws.amazon.com, is
    handed amazon.com, which is a retail store and not the vendor at all.
    It adds nothing to the allowlist that was not already trusted at the
    subdomain level."""
    from sqlalchemy import select as _select

    with get_session() as session:
        rows = session.scalars(_select(AllowlistDomain).where(
            AllowlistDomain.category == DomainCategory.VENDOR)).all()
        by_vendor: dict[int, list[AllowlistDomain]] = {}
        for d in rows:
            if d.vendor_id:
                by_vendor.setdefault(d.vendor_id, []).append(d)

        known = {d.domain.lower() for d in rows}
        vendor_names = {v.id: v.name for v in session.scalars(
            _select(Vendor)).all()}
        added = 0
        for vendor_id, domains in by_vendor.items():
            apexes = {".".join(d.domain.lower().split(".")[-2:])
                      for d in domains}
            if len(apexes) != 1:
                continue          # ambiguous (e.g. Avaya's two brands) — leave it
            apex = apexes.pop()
            if apex in known:
                continue          # already has it
            # aws.amazon.com -> "amazon" != "aws": a different company's site.
            label = apex.split(".")[0].lower()
            vendor_key = "".join(ch for ch in (vendor_names.get(vendor_id) or "")
                                 if ch.isalnum()).lower()
            if not vendor_key or label != vendor_key:
                continue
            # Inherit tier and position from the vendor's existing entries,
            # so the apex is trusted exactly as much as its own subdomains
            # already are and sorts alongside them.
            sibling = min(domains, key=lambda d: d.position)
            session.add(AllowlistDomain(
                domain=apex, category=DomainCategory.VENDOR,
                vendor_id=vendor_id, tier=sibling.tier,
                position=sibling.position))
            known.add(apex)
            added += 1
        # Undo what an earlier, unguarded version of this migration added:
        # amazon.com for AWS. Only ever removes a row this migration could
        # have created — an apex whose label does not match its vendor.
        for d in rows:
            label = d.domain.lower().split(".")[0]
            key = "".join(ch for ch in (vendor_names.get(d.vendor_id) or "")
                          if ch.isalnum()).lower()
            if (d.domain.lower().count(".") == 1 and key and label != key
                    and any(o.domain.lower().endswith("." + d.domain.lower())
                            for o in rows)):
                session.delete(d)
                added += 1

        if added:
            session.commit()


def _migrate_clean_certification_names_and_codes() -> None:
    """Two data-hygiene fixes on the certification catalogue.

    (1) STRIP RELEASE TOKENS FROM *CURRENT* CERTIFICATION NAMES.
    "Deployment Administrator (CPDA) R82" becomes "Deployment Administrator
    (CPDA)". A release is a dated, sourced fact and it lives in
    certification_versions; carrying it in the display name means it reaches
    article TITLES, which is how a draft ended up titled "The Complete CCTE
    R82 Certification Guide".

    RETIRED rows keep their version, deliberately. Their name IS the pattern
    load_retired_certifications() hands to the currency check, and the version
    is the whole point of the tripwire: "Certified Troubleshooting Expert
    (CCTE) R81" must match an article citing R81 and must NOT match one about
    the current R82 exam. Strip it and the retired warning fires on every CCTE
    article. Safe to treat the two differently because _vendor_cert_context()
    only ever hands CURRENT certifications to the writer, so a retired name
    never reaches a title.

    (2) CLEAR exam_code WHERE IT IS NOT AN EXAM CODE.
    Some rows carry a fragment of the course name in that column — Extreme
    Networks has two courses coded "Management" and two coded "Advanced
    Configuration". Those are not codes, they are the tail of a title, and
    they collide: QA rule V1 keys on exam_code, so two certifications sharing
    one land in the same version bucket. A real code is a single uppercase
    token (CCSA, LTM, EDU-210); anything multi-word or mixed-case is not.

    F5's "LTM" is the one genuine collision, shared by a dedicated 3-day LTM
    course and a 5-day combined course. It is kept on the dedicated one and
    cleared from the combination, which is named for being a combination
    rather than for LTM. Keeping it somewhere matters: known_acronyms() reads
    this column, and clearing LTM everywhere would stop the casing fixer
    correcting "ltm" in prose.

    Idempotent, and a no-op once applied."""
    with _engine.begin() as conn:
        tables = {row[0] for row in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "certifications" not in tables:
            return

        # (1) release tokens out of CURRENT names
        for cid, name in conn.exec_driver_sql(
                "SELECT id, name FROM certifications WHERE status = 'CURRENT'"
                ).fetchall():
            cleaned = re.sub(r"\s+R\s?8\d(?:\.\d{1,2}|\.X)?$", "", name or "").strip()
            if cleaned and cleaned != name:
                conn.exec_driver_sql(
                    "UPDATE certifications SET name = ? WHERE id = ?", (cleaned, cid))

        # (2) anything that is not a single uppercase token is not a code
        for cid, code in conn.exec_driver_sql(
                "SELECT id, exam_code FROM certifications "
                "WHERE exam_code IS NOT NULL AND exam_code != ''").fetchall():
            token = (code or "").strip()
            if " " in token or token != token.upper():
                conn.exec_driver_sql(
                    "UPDATE certifications SET exam_code = NULL WHERE id = ?", (cid,))

        # (2b) F5's LTM: keep it on the dedicated course, clear the combination.
        rows = conn.exec_driver_sql(
            "SELECT c.id, c.name FROM certifications c JOIN vendors v "
            "ON v.id = c.vendor_id WHERE v.name = 'F5' AND c.exam_code = 'LTM'"
            ).fetchall()
        if len(rows) > 1:
            keep = min(rows, key=lambda r: len(r[1] or ""))
            for cid, _name in rows:
                if cid != keep[0]:
                    conn.exec_driver_sql(
                        "UPDATE certifications SET exam_code = NULL WHERE id = ?", (cid,))


def _seed_certification_versions_if_empty(session: Session) -> None:
    """Seed the versions REACS v1.3 states outright, with provenance.

    ONLY Check Point, and only what the standard document itself asserts —
    nothing here is inferred from the Vendor KB, from article text, or from a
    model. The standard names CCME as R81.X / 156-836, CCSE as having moved
    to R82, and CCTE as running R81.20 and R82 concurrently. Everything else
    on the estate is deliberately absent: an unseeded vendor produces no
    findings at all (see the version check's pattern lookup), which is the
    honest behaviour while V1 ships in warn mode.

    BEFORE PROMOTING V1 TO BLOCKING: a human needs to confirm these against
    Check Point's courseware PDF and the Pearson VUE page, and backfill the
    remaining vendors. source_note records that they have not been yet."""
    if session.scalar(select(CertificationVersion).limit(1)) is not None:
        return
    SK = "https://support.checkpoint.com/results/sk/sk163417"
    PV = "https://www.pearsonvue.com/us/en/checkpoint.html"
    _sk = ("Read from Check Point's own certification matrix, sk163417, on "
           "25 Aug 2026.")
    _pv = ("Read from Check Point's exam listing on Pearson VUE, the vendor's "
           "registration platform, on 25 Aug 2026.")
    _ret = _pv + " Pearson VUE names this exam as retiring on 30 Sep 2026."
    CPDA_URL = ("https://www.checkpoint.com/resources/items/"
                "course-overview-deployment-administrator-cpda")
    _atc = ("Course release, not an exam version — CPDA has no exam. Confirmed "
            "by Red Education as an authorised Check Point ATC delivering the "
            "course, 25 Aug 2026, against the vendor course page cited here; "
            "that page returns an empty HTTP 202 challenge to automated "
            "requests and could not be read directly.")

    # (exam code, version label, registration code, is_current, source, note)
    #
    # SOURCED FROM THE VENDOR, 25 Aug 2026. Everything here was read off
    # Check Point's own pages; nothing is inferred from the standard document,
    # from the Vendor KB scrape, or from article text.
    #
    # THE CORRECTION THAT MATTERS: CCME is R81, not "R81.X". The REACS v1.3
    # standard document asserts R81.X and CASE 1's whole remediation rests on
    # it, but Check Point publishes "Check Point Certified Maestro Expert -
    # R81". V1 matches version tokens exactly, so with R81.X recorded an
    # article stating the vendor's OWN published version was flagged as
    # unverified while the string Check Point does not publish passed. The
    # gate had it precisely backwards on the row the standard leans on hardest.
    #
    # is_current is True for the R81.20 exams: they run concurrently with R82
    # until 30 Sep 2026, which is the parallel-version transition is_current
    # exists to carry.
    #
    # TWO CHECK POINT CODES ARE DELIBERATELY ABSENT, and both are answers
    # rather than gaps. Recorded here because the schema has nowhere to store
    # a negative finding — a row needs a version_label — so without this note
    # someone re-investigates them in three months.
    #
    #   CTPS  Threat Prevention Specialist, exam 156-590. Check Point lists it
    #         on sk163417 with NO version at all.
    # CPDA is the interesting case and IS recorded below, for a reason worth
    # understanding before anyone "tidies" it away. It is not on sk163417 in
    # any form — zero mentions of "CPDA" or "Deployment Administrator" on
    # Check Point's certification matrix — and Check Point's own URL for it
    # reads .../course-overview-deployment-administrator-cpda. It is a COURSE,
    # not an accreditation, and there is no exam behind it.
    #
    # It still gets a row because the question this table answers is "what
    # release does this thing target", and for a course that has a real
    # answer. Without the row, an article correctly stating the CPDA course
    # covers R82 was flagged as unverifiable — a false positive on true prose,
    # which is how a warn-mode rule earns a reputation for noise before it is
    # ever promoted.
    #
    # Side effect of both: registration codes live on the version row, so a
    # certification with a code but no version has nowhere to put it. CTPS's
    # 156-590 is therefore invisible to QA rule V2.
    wanted = [
        # Core
        ("CCSA", "R82",    "156-215.82",    True,  SK, _sk),
        ("CCSE", "R82",    "156-315.82",    True,  SK, _sk),
        ("CCSE", "R81.20", "156-315.81.20", True,  PV, _pv),
        # Infinity Specialist Accreditations
        ("CCME", "R81",    "156-836",       True,  SK, _sk),
        ("CCAS", "R81.20", "156-521",       True,  SK, _sk),
        ("CCCS", "R81.20", "156-561",       True,  SK, _sk),
        ("CCES", "R81.20", "156-536",       True,  SK, _sk),
        ("CCMS", "R81",    "156-541",       True,  SK, _sk + " " + _ret),
        ("CCVS", "R81",    "156-551",       True,  SK, _sk),
        # Course, not an exam — hence no registration code. Confirmed by Red
        # Education, an authorised Check Point ATC that delivers it; Check
        # Point's own course page could not be machine-read (www.checkpoint.com
        # answers automated requests with an empty HTTP 202 challenge, unlike
        # support.checkpoint.com), so the confirmation is the ATC's, against
        # the vendor page cited here.
        ("CPDA", "R82",    None,            True,  CPDA_URL, _atc),
        ("CCTA", "R82",    "156-583",       True,  SK, _sk),
        ("CCTA", "R81.20", "156-582",       True,  PV, _pv),
        ("CCTE", "R82",    "156-588",       True,  SK, _sk),
        ("CCTE", "R81.20", "156-587",       True,  SK, _sk + " " + _ret),
    ]
    vendor = session.scalar(select(Vendor).where(Vendor.name == "Check Point"))
    if vendor is None:
        return
    certs = session.scalars(select(Certification).where(
        Certification.vendor_id == vendor.id)).all()
    for code, label, exam, current, source, note in wanted:
        # Match the CURRENT certification for this code where there is one,
        # so a retired duplicate row does not take the seed.
        match = next((c for c in certs
                      if (c.exam_code or "").upper() == code
                      and c.status == CertificationStatus.CURRENT), None)
        if match is None:
            match = next((c for c in certs if (c.exam_code or "").upper() == code), None)
        if match is None:
            continue
        session.add(CertificationVersion(
            certification_id=match.id, version_label=label, exam_code=exam,
            is_current=current, source_url=source, source_note=note))
    session.commit()


def seed_if_empty() -> None:
    Base.metadata.create_all(_engine)
    _migrate_add_columns()
    _migrate_unique_article_topics()
    _migrate_drop_cluster_rule()
    _migrate_drop_analytics()
    _migrate_clean_exam_code_release_tokens()
    _migrate_split_duplicate_exam_codes()
    _migrate_reconcile_instructor_roster()
    _migrate_clean_certification_names_and_codes()
    _migrate_add_vendor_apex_domains()
    _migrate_flag_course_shaped_certifications()
    with get_session() as session:
        if session.scalar(select(Vendor).limit(1)) is None:
            _seed(session)
        _seed_instructors_if_empty(session)
        _seed_certification_versions_if_empty(session)


def reseed_from_yaml(force: bool = False) -> None:
    """Manual escape hatch: wipe and re-import the Foundation tables from the
    YAML seed files. No-op unless force=True (this is destructive to any edits
    made directly in the DB since the last seed). Does NOT touch Product/
    Certification/Course — those have no YAML counterpart, see module docstring."""
    if not force:
        raise ValueError("reseed_from_yaml is destructive — call with force=True")
    Base.metadata.create_all(_engine)
    with get_session() as session:
        for model in (AllowlistDomain, EvergreenKeyword,
                      SecurityKeyword, CtaPhrase, AppConfig, Vendor):
            session.query(model).delete()
        session.commit()
        _seed(session)
