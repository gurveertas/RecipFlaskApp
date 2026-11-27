# Web front end

A browser UI so anyone on the internal network can run the pipeline without
the command line. A thin Flask layer over the existing modules — it calls the
same `retrieve → verify → generate → validate` functions the CLIs use, so it
can never drift from `run.py` / `generate.py`.

```
webapp/
  app.py            Flask app factory; loads .env itself (_load_dotenv), then
                    registers the blueprints below
  service.py        the single call into the pipeline for the quick-draft UI
                    (demo + live paths)
  pipeline.py       shared orchestration for every governed stage: topics,
                    briefs, writing, targeted fixes, section rewrites, and the
                    WordPress push. Both UIs call this, so they can't diverge
  studio.py         Article Studio — the 6-step governed flow (/studio)
  admin.py          Vendor Centre + the usage meter (/admin/vendors)
  topics.py         Topic Engine board (/admin/topics)
  articles.py       the older per-article admin pages (/admin/articles)
  render.py         markdown -> SAFE html (nh3). Every route that renders
                    model output must go through this
  templates/        index.html (quick draft), studio/ (the 6 steps),
                    admin/, guide.html, shared partials
  static/css/       the design system (tokens + base + layout + components)
                    plus preview.css — the live site's article formatting
```

## Live progress

Generating a live draft can take 30–90s (retrieval, then up to 3 write+QA
attempts), so the UI shows real step-by-step progress rather than a blind
spinner: a progress bar plus a checklist (Search sources → Verify & corroborate
→ Write draft → Run QA), each updating the moment that stage actually happens —
including "regenerating…" if a draft gets blocked by QA and is retried.

This is wired through `POST /api/jobs` (starts generation in a background
thread, returns a `job_id`) and `GET /api/jobs/<job_id>/stream`, a
Server-Sent-Events endpoint the browser subscribes to with `EventSource`. The
plain blocking `POST /api/generate` still exists for scripts/curl.

**Important deployment constraint:** job state lives in an in-memory dict in
`app.py`, scoped to one Python process. That's why the run commands below use
`--worker-class gthread --workers 1 --threads N` (one process, many threads)
instead of multiple worker *processes* — with multiple processes, the
`/stream` request could land on a different process than the one running the
job and 404. If you ever need multi-process/multi-machine scaling, move the
job/queue state into something shared (Redis, etc.) first.

## Styling — a small design system (reusable, expandable)

CSS lives in `static/css/`, split so you can extend the UI by composing existing
classes instead of writing one-off styles. Load order matters (later files read
the tokens): `tokens → base → layout → components`.

| File | What's in it | When you touch it |
| --- | --- | --- |
| `tokens.css` | All design tokens as CSS variables: brand colour, spacing scale, radii, type scale, shadows, and the **light/dark** semantic palettes. | Rebrand or retheme — change values here and the whole app follows. Nothing else. |
| `base.css` | Reset + bare element defaults (body, headings, links, focus rings, scrollbars). | Rarely. |
| `layout.css` | Page structure: `.app-header`, `.app-main`, the responsive `.app-grid`, and layout utilities (`.stack`, `.cluster`, `.flex-between`). | New page regions / arrangement. |
| `components.css` | Reusable components, each its own BEM-ish block: `.card`, `.btn` (`--primary/--ghost/--block/--sm`), `.field`/`.input`, `.segmented`, `.badge` (`--pass/warn/fail/info`), `.banner`, `.verdict`, `.checklist`/`.check`, `.article`, `.reflist`, `.spinner`, `.toolbar`. | Add a new UI element — append a new titled block; reuse the tokens. |

Conventions: components are `.block`, `.block__element`, `.block--modifier`;
colours/spacing/type always come from tokens (never hard-coded); every colour has
a light and dark value so theming is automatic. Theme is chosen by the OS by
default and can be overridden by the header toggle (persisted via `localStorage`,
applied as `data-theme` on `<html>`).

To add a component: add a titled block in `components.css` using existing tokens,
then use the class in `index.html`. No build step — the browser loads the files
directly.

## Access model

**No authentication.** Deploy it only on the internal network / VPN — anyone
who can reach the host can use it. If it ever needs to be exposed more widely,
put it behind your reverse proxy's auth (or add SSO) first.

## Run it (internal VM)

From the repo root, using the project virtualenv:

```bash
# one-time
python3.14 -m venv venv
venv/bin/pip install -r requirements.txt

# run (production server, reachable on the LAN)
venv/bin/gunicorn "webapp:create_app()" -b 0.0.0.0:8000 --timeout 300 \
  --worker-class gthread --workers 1 --threads 8
```

Then open `http://<vm-ip>:8000/` from any machine on the network.

For local development with auto-reload:

```bash
venv/bin/flask --app webapp run --debug   # http://127.0.0.1:5000
```

### Deploying to a server

Don't hand-roll the unit — use the tested files in [`deploy/`](../deploy/) and
follow [`deploy/DEPLOY.md`](../deploy/DEPLOY.md):

| File | Purpose |
|---|---|
| `deploy/gunicorn.conf.py` | Pins `workers = 1` (see above) and the 300s timeout. |
| `deploy/blog-generator.service` | systemd unit, with `APP_DB_PATH` set and the secrets kept in a 0600 `.env`. |
| `deploy/nginx-blog-generator.conf` | Reverse proxy, with the long read timeouts the SSE progress stream needs. Mounted as a path on the existing site, not a standalone server block. |
| `deploy/update.sh` | Pull, reinstall, restart, verify. Snapshots the database first. |

**The one thing not to get wrong:** set `APP_DB_PATH` to a directory *outside*
the deployed code. The database defaults to `config/app.db`, inside the source
tree — deploy over that and every vendor, topic, article and the cached
published-post index is destroyed.

Set it **in `.env`, not only in the unit.** The two scheduled jobs
(`scrape_published_posts.py`, `sync_wp_state.py`) run from cron and inherit
nothing from systemd, so a unit-only value leaves them writing to a different,
empty database. `config/db.py` reads `.env` itself, so setting it there covers
gunicorn, cron and one-off scripts alike.

The app has no login. Access control is entirely the network's job, so bind it
to an internal interface and keep it there.

```bash
sudo systemctl enable --now blog-generator
```

## Demo vs Live

- **Demo mode** (default) needs no keys. It generates from the bundled sample
  sources in `fixtures/`, so the *topic is fixed to the sample*; the region is
  still adjustable. Use it to let the team try the flow.
- **Live mode** generates a real draft on any topic. It needs
  `SERPER_API_KEY` (search) and `ANTHROPIC_API_KEY` (Claude) in the server
  environment. The UI hides/disables the Live toggle until a key is present.

Every draft — demo or live — is run through the 6-check reliability QA harness
before it's shown, and the verdict (publish candidate / blocked) is displayed
at the top of the result.
