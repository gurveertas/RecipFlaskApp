# Blog Generator — Red Education content pipeline

Research → verify → write → QA → preview → publish. Takes a vendor and a topic
and produces a fully-sourced, quality-gated article, then pushes it to
rededucation.com **as a WordPress draft**. It never publishes live: going live
stays a human action in WordPress.

The foundation is a **verified source bundle** — clean articles from
allowlisted domains only, every statistic and quote tagged by origin,
corroborated across independent outlets, and flagged where it isn't
trustworthy. Everything downstream is built so the writer can only use facts
that bundle actually contains.

**Two ways in:**

- **Blog Generator** (`/`) — a one-shot quick draft on any topic. Nothing is
  saved. Good for experiments.
- **Article Studio** (`/studio`) — the governed 6-step flow. Everything is
  saved, scored and quality-gated. This is the main path.

## Run the web app

```bash
gunicorn -w 1 -k gthread --threads 8 -b 127.0.0.1:8000 "webapp:create_app()"
```

Credentials come from `.env` in the repo root, which the app loads itself
(`webapp/app.py::_load_dotenv`) — nothing needs exporting first. See
[Configuration](#configuration).

## Why the sourcing matters

Google now discounts mass-produced, thinly-sourced content. The single biggest
SEO lever is *demonstrable, sourced expertise*. The research stage supplies it —
and it also guards against the two failure modes visible in the existing blog
archive: **stale facts** (e.g. citing the retired PCNSE as current) and
**single-sourced or vendor-marketing claims** presented as established fact.

## Install

```bash
pip install -r requirements.txt
```

## Run the offline demo (no API keys needed)

The fastest way to see what the module does. It runs verification over bundled
sample sources in `fixtures/sample_sources.json`:

```bash
python run.py --offline
```

You'll see sources kept/dropped, every extracted claim with a status
(VERIFIED / UNVERIFIED / FLAGGED), corroboration links, and reviewer warnings.

## Run live

Live retrieval needs a search API key (Serper by default):

```bash
export SERPER_API_KEY=your_key_here
python run.py --topic "Palo Alto NGFW Engineer certification 2026" --region Australia
```

Export the machine-readable bundle for the next stage:

```bash
python run.py --offline --json bundle.json
```

Optional richer claim extraction via Anthropic (falls back to heuristics if the
key is absent or anything fails):

```bash
export ANTHROPIC_API_KEY=your_key_here
python run.py --offline --use-llm
```

## The pipeline

1. **Retrieve** (`retrieval/`) — search is constrained to allowlisted domains
   only; each hit is fetched and reduced to clean main-article text. Anything
   off the allowlist is refused at ingestion.
2. **Dedupe** — same-domain and near-duplicate-title sources are collapsed so
   corroboration counts *independent* outlets.
3. **Recency** — stale sources are dropped for time-sensitive topics; evergreen
   topics are exempt.
4. **Currency** — catches superseded facts (retired certs) and stale-year
   framing in titles.
5. **Claim extraction** (`verification/claims.py`) — pulls statistics, quotes,
   and dated facts, each tagged with its source URL and tier.
6. **Corroboration** — a stat is `VERIFIED` only when the same figure appears in
   ≥2 independent domains; otherwise `UNVERIFIED`.
7. **Vendor rule** — a security/threat claim supported *only* by a vendor source
   is `FLAGGED` for independent review. (Vendor sources remain authoritative for
   their own product facts.)

Output is a `VerifiedBundle` (see `retrieval/models.py`) — the sole input the
synthesis stage will consume.

## Configuration

Config now lives in a real database (`config/app.db`, SQLite via SQLAlchemy —
see `config/db.py`), not flat files. The three YAML files below still exist and
are the *versioned seed* for it — they populate the database once, on first
run, and are otherwise historical (edit the database directly, or via the
Vendor Centre admin UI at `/admin/vendors`, not the YAML, after that first run):

- **`config/sources.yaml`** — the tiered trusted-source allowlist seed.
- **`config/settings.yaml`** — recency thresholds, evergreen keywords, and
  corroboration strictness seed.

Retired-certification currency warnings (e.g. "PCNSE retired → NGFW Engineer")
are no longer a YAML list — they're `Certification` records in the **Vendor
Knowledge Base** (`/admin/vendors`), entered per vendor alongside its products
and courses. Mark a certification retired there and it becomes a QA tripwire
immediately, no restart needed.

## Stage 2: Synthesis + reliability QA

Turns a `VerifiedBundle` into a draft **and** validates it. The generator is only
half the reliability story; the QA harness is the other half.

Offline demo (no keys — builds a bundle from fixtures, mock-generates, validates):

```bash
python generate.py --offline
```

Watch the QA harness catch a deliberately broken draft (hallucinated stat,
leaked vendor-only claim, verbatim copy):

```bash
python generate.py --offline --fault-demo
```

Live generation (needs `SERPER_API_KEY` for retrieval + `ANTHROPIC_API_KEY`):

```bash
python generate.py --topic "Palo Alto NGFW Engineer certification 2026" --region India
```

### Generation rules (enforced in the prompt AND re-checked by the harness)

- Use only facts present in the verified bundle — never invent a statistic.
- Cite every factual claim inline with its `[S#]` tag.
- Verified claims stated directly; single-sourced claims attributed in-text;
  flagged/vendor-only claims excluded entirely.
- Original wording — no verbatim runs from any source.

### Reliability QA harness (`synthesis/validate.py`, no API key needed)

Every draft runs **16 checks**. Any FAIL blocks it from being a publish
candidate (the CLI exits non-zero, so it can gate an automated step); a WARN
informs the reviewer without blocking.

**Integrity tier — these can FAIL:**

1. **structure_length** — headings, word count, meta length, title
2. **citation_integrity** — every `[S#]` resolves; references are real bundle
   sources; the post is actually sourced
3. **grounding** — no statistic appears that isn't in the source material
   (hallucination guard)
4. **flagged_leakage** — no flagged/vendor-only figure or CVE leaks in
5. **originality** — no long verbatim run copied from a source
6. **course_link_cta** — inline course link + an approved CTA phrase, verbatim
7. **keyword_coverage** — the target keyword appears verbatim in the body
8. **writing_mechanics** — certification acronyms and release tokens spelled the
   way the Vendor KB spells them (`ccte r82` → `CCTE R82`), no repeated words,
   no space before punctuation. `parse_article_response()` repairs these
   deterministically on every write path before QA sees them, so a failure here
   means a class of error the fixer doesn't know about — which is why it FAILs
   rather than warns. Code spans, link targets and URLs are never rewritten.
   Deliberately **not** general grammar or spelling: that needs a language
   model, and this harness stays deterministic so its verdict never depends on
   a model's mood or on quota.

**Advisory tier — WARN only:**

9. **meta_description** — length
10. **serp_competitiveness** — draft length vs pages actually ranking
11. **direct_answer** — opening works as a standalone 40–60 word answer
12. **entity_coverage** — the vendor's real products/certs/courses are named
13. **internal_links** — links the brief requested are present
14. **internal_link_targets** — no site-root links, no reused targets, no
    anchor naming a course whose URL is for something else
15. **title_readability** — the four Rank Math title checks, run locally on the
    SEO title: focus keyword near the start, a power word, a sentiment word, a
    number. Rank Math scores power word and sentiment as *separate* checks, so
    a title carrying only one always fails the other — which is what the brief
    prompt used to ask for. Checked here because Rank Math only reports it once
    the post reaches WordPress, long after the article is written and pushed.
16. **title_subject_coverage** — the subject the *title* promises actually
    appears in the body. Nothing else catches an article that is well sourced
    and on-keyword but simply not about the thing it is headlined as.

**QA score (0–100)** — integrity tier 60 points, advisory 40, a WARN taking
half credit, calculated over the checks that actually *ran* (several are
conditional). It **grades**; it does not gate — `qa_passed` is still "nothing
failed". A blocked draft never shows a green score.

Course links and CTA phrases live in `config/course_links.yaml` seed (Vendor
Knowledge Base course/certification data lives in the database instead — see
below).

## Article Studio (`/studio`) — the 6 steps

The governed path. Everything is saved and quality-gated.

1. **Vendor** — cards show what is already **live on rededucation.com** for
   that vendor (from a scrape of the real site, dated so a stale count is
   visible) separately from work started here, split into mutually exclusive
   stages: in brief / needs fixes / ready / awaiting publish / live.
2. **Topic** — the LLM *proposes* ideas; a deterministic formula *decides*
   (see Topic Engine below). An optional **theme** is a hard constraint: it is
   stored on the topic and flows into the research query and the brief, so a
   themed batch stays on theme. A **long focus keyword** (>40 chars or 5
   words) is flagged here — it cannot fit a 60-character SEO title, so Rank
   Math will score the page badly however good the article is.
3. **Brief** — the editorial contract: outline, key points, FAQ, CTA intent,
   and real internal links to weave in. Editable before writing.
4. **Article** — written with up to 3 attempts; failed checks feed targeted
   fixes into each retry. Both gates are shown with scores, and **Fix
   selected** repairs only the checks you tick.
5. **Preview** — the article in rededucation.com's own formatting. Type sizes,
   colours, table styling and the FAQ accordion were **measured from live
   published posts**, not approximated. Desktop/Tablet/Mobile widths match the
   live column.
6. **Publish** — creates a **WordPress draft**. See below.

## Tests (`tests/`)

```bash
venv/bin/python -m pytest tests/ -q
```

109 regression tests over the gates: `test_reacs_pillars.py` for the REACS
pillars, `test_qa_rules.py` for the QA risk rules V1&ndash;V10, and
`test_brief_buyer_questions.py` for the Brief's buyer-question coverage. No network, no
API key, and a throwaway seeded database — `tests/conftest.py` points
`APP_DB_PATH` at a temp file *before* importing `config.db`, which resolves its
engine at import time, so the suite can never read or damage `config/app.db`.

Every test is named after the real published defect or shipped bug that proved
it, which is the standard's own instruction: nobody should remove one without
understanding what it protects. Two of these rules have already blocked live
articles for the wrong reason, and both regressions are pinned here.

## REACS — the AI Content Standard

A **second, independent gate** alongside QA, answering a different question:
QA asks "is this draft internally sound?"; REACS asks "does it meet the
AI-search-visibility content standard?" 12 pillars, 6 mandatory + 6 advisory.
Score = mandatory × 60% + advisory × 40%. **Any single mandatory failure
blocks REACS regardless of score**; passing needs zero mandatory failures
*and* ≥90. Publishing requires QA **and** REACS.

The instructor-byline pillar was removed from the gate by decision — articles
publish under the shared WordPress account for now — so the gate scores six
mandatory pillars rather than the standard's eight. That difference is settled,
not a backlog.

## WordPress Publisher (`publish/`)

Pushes the article to rededucation.com as a **draft**.

- `publish/payload.py` is pure (markdown → HTML, JSON-LD, payload assembly);
  `publish/client.py` holds every network call and credential read, so the
  blast radius of "what can talk to the live site" is one module.
- **Draft-only is enforced twice**: `status` is hard-coded in the payload
  builder, and `create_draft_post` re-checks before sending.
- The whole application can make exactly three WordPress calls: create a post,
  set Rank Math meta on the id just created, and a read-only status check.
  **No update or delete of existing content exists anywhere.**
- Sends title, slug, body, excerpt, author and category. **Featured images are
  attached by hand**; tags are not sent.
- The body carries JSON-LD (Article + FAQPage), an FAQ accordion, and the
  site's inline table styling so generated tables match hand-written posts.
  `[S#]` citation markers are stripped at the reader boundary — they exist for
  QA, not for readers.
- After a push the UI links to the **wp-admin edit screen**, not the public
  permalink: a draft has no public URL and returns the site's 404.

### Knowing what happened next

This tool creates drafts; a human publishes them. Two scheduled jobs close that
loop, both read-only against the live site.

- **`sync_wp_state.py`** asks WordPress for the status of every article we
  pushed but don't yet know to be live, and stamps `Article.published_at` when
  it reports `publish`. `published_at` is never set by us directly — the
  asymmetry is deliberate: `pushed_at` is ours, `published_at` is theirs. The
  vendor card then reads **live** rather than **awaiting publish**. Anything
  other than a definite answer (a 429, a timeout) leaves the row untouched.
- **`scrape_published_posts.py`** refreshes the site-coverage index and stamps
  `AppConfig.coverage_scraped_at`. The "live on rededucation.com" counts come
  from this scrape, not from live queries, so Article Studio's Vendor step
  prints how long ago it last ran — an undated count that is six weeks stale
  looks exactly like a fresh one.

Both are wired to cron on the server; see [`deploy/DEPLOY.md`](deploy/DEPLOY.md).
Because they run outside systemd they do **not** inherit the unit's
environment, which is why `APP_DB_PATH` belongs in `.env` — `config/db.py`
reads it from there so every entry point resolves the same database.

## Vendor Knowledge Base (`/admin/vendors`)

A nested-CRUD admin UI: vendor list → vendor detail → products, certifications
(exam code, current/retired status, retirement year + replacement), and
courses (each tied to a specific certification — enforced at the database
level, a course can't exist without one). Retired certifications feed the QA
Engine's currency-warning tripwire directly; vendors also carry a P1–P5
priority field the Topic Engine (below) uses to weight scoring.

## Topic Engine (`/admin/topics`)

Generates AI-proposed blog topic ideas for a vendor and scores them with Red
Education's deterministic weighting formula (ported faithfully from the
original build — the model estimates raw dimensions only; it never decides
create/park/reject). Pick a vendor (must have a Vendor Centre priority set)
+ idea count + optional theme; the model proposes titles/keywords and
estimates commercial/SEO/AI-visibility/difficulty (0–100 each); Python code
in `synthesis/topics.py` combines those with the vendor's priority-derived
alignment score into an opportunity score, bucketed Create (≥65) / Parked
(45–64) / Rejected (<45). Duplicate keywords per vendor are rejected at the
database level (case-insensitive). Approve/Park/Reject/Delete on the board
overrides the formula's bucket by hand.

## Layout

```
config/          SQLite + SQLAlchemy models/seeding (db.py), YAML seed files, loader
retrieval/       models, domain-constrained search, fetch/clean, orchestrator
verification/    dedupe, recency, currency, claim extraction, verify orchestrator
synthesis/       style spec, prompt builder, LLM dispatch (llm.py), generator
                 (+ offline mock), QA harness, Topic Engine scoring (topics.py)
publish/         WordPress Publisher: payload.py (pure) + client.py (the only
                 code that talks to WordPress)
webapp/          Flask app: Blog Generator UI, Article Studio (studio.py),
                 Vendor Centre (admin.py), Topic Engine (topics.py),
                 render.py (markdown -> safe HTML), pipeline.py (shared
                 orchestration for every stage)
fixtures/        sample sources for the offline demos
run.py           stage 1 CLI: retrieval + verification report
generate.py      stage 2 CLI: generate a draft + reliability QA report
scrape_published_posts.py   ingest rededucation.com's own sitemap, powering
                 content-gap and internal-link suggestions, the Vendor step's
                 coverage counts, and their "last checked" stamp (nightly cron)
sync_wp_state.py record which pushed drafts a human has since published
                 (hourly cron, read-only against WordPress)
```

## Environment (`.env`)

The app reads `.env` from the repo root itself, so nothing needs exporting.
`chmod 600` it — it holds live credentials.

| Variable | Purpose |
|---|---|
| `SERPER_API_KEY` | live search for retrieval + SERP difficulty |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | the writing model |
| `ANTHROPIC_API_KEY` | optional richer claim extraction |
| `SECRET_KEY` | Flask session signing |
| `SERPER_CREDITS_START` | baseline for the usage meter; update after a top-up |
| `WORDPRESS_URL` | site **root** — the client appends `/wp-json` itself |
| `WORDPRESS_USERNAME` | a WP login |
| `WORDPRESS_APP_PASSWORD` | an **Application Password**, not a login password |
| `WP_AUTHOR_ID` | numeric WP user every post is authored by |
| `WP_AUTHOR_LABEL` | that user's public display name, for the UI only |
| `RETRIEVAL_RENDER` | set `0` to disable the headless-browser fallback |
| `CHROME_BIN` | path to Chrome/Chromium if it isn't in a standard location |
| `GEMINI_DAILY_LIMIT` | daily-call cap shown in the usage meter (default 500) |
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | optional OpenAI-compatible provider. When both URL and key are set they take precedence over Gemini — see `synthesis/llm.py` |
| `APP_DB_PATH` | where the SQLite database lives. Defaults to `config/app.db`. **When deploying, set it to a path outside the code tree, and set it here in `.env` rather than only in the systemd unit** — cron jobs and one-off scripts don't inherit the unit's environment, so they would otherwise fall back to the default and quietly use a different, empty database. `config/db.py` reads this file directly so every entry point agrees. See [`deploy/DEPLOY.md`](deploy/DEPLOY.md) |

The pushing account needs **`edit_others_posts`** to publish as another user,
and **`unfiltered_html`** or WordPress strips the FAQ accordion's `<style>`
block. An Author-role token doesn't error — it silently reassigns the post to
itself.

## This project's 8-stage pipeline

1. **Foundation & Vendor Centre** ✅ — the database (`config/db.py`)
2. **Vendor Knowledge Base** ✅ — products/certifications/courses admin UI (`/admin/vendors`)
3. **Research Engine** ✅ — `retrieval/` + `verification/`
4. **Topic Engine** ✅ — AI-proposed, deterministically-scored ideas (`/admin/topics`)
5. **Brief + Writer** ✅ — reviewable brief then article (`synthesis/brief.py`),
   surfaced as steps 3-4 of Article Studio.
6. **QA Engine** ✅ — `synthesis/validate.py` (16 checks), plus the separate
   REACS gate (`synthesis/reacs.py`) and a Preview step before publishing.
7. **WordPress Publisher** ✅ — `publish/`, live and pushing drafts.
8. **Analytics + Refresh** — built, then **removed** at the user's request
   ("of no use for now"). Don't re-add it speculatively; the reference
   implementation still exists if it's ever wanted.