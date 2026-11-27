"""Fetch a URL and extract clean main-article text + publication date.

Uses trafilatura, which is robust at stripping nav/ads/boilerplate and pulling
metadata. Everything here is defensive: a source that fails to fetch or parse
is skipped (and reported as dropped) rather than crashing the run.
"""
from __future__ import annotations

import os
import select
import shutil
import signal
import subprocess
import tempfile
import time
from datetime import date
from urllib.parse import urlparse
from typing import Optional

import requests

try:
    import trafilatura
except ImportError:  # pragma: no cover
    trafilatura = None

try:
    import dateparser
except ImportError:  # pragma: no cover
    dateparser = None

try:
    import pypdf
except ImportError:  # pragma: no cover
    pypdf = None

from .models import OPEN_WEB_TIER, Source
from config import load_allowlist

_UA = ("Mozilla/5.0 (compatible; RedEd-BlogGen/1.0; +content-research)")


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower()


# Where Chrome usually lives. CHROME_BIN overrides; if none is found the
# render fallback simply never runs and behaviour is exactly as before.
_CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome", "chromium", "chromium-browser",
)


def _chrome_binary() -> Optional[str]:
    explicit = os.getenv("CHROME_BIN", "").strip()
    if explicit:
        return explicit if os.path.exists(explicit) or shutil.which(explicit) else None
    for cand in _CHROME_CANDIDATES:
        if os.path.exists(cand):
            return cand
        found = shutil.which(cand)
        if found:
            return found
    return None


def _kill_process_group(proc) -> None:
    """SIGKILL Chrome and everything it forked.

    proc.kill() reaches only the process we launched, and headless Chrome
    forks a zygote, a GPU process and a renderer. Those outlived every render,
    kept writing into the --user-data-dir about to be deleted, and accumulated
    on the box as orphans. One signal to the group reaches all of them.

    Best-effort by contract: this runs while cleaning up, and a failure to
    kill must not replace whatever we were already doing with an exception.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, ValueError):
        try:
            proc.kill()
        except OSError:
            pass


# How long to keep listening once the output ends in "</html>". Chrome writes
# the dump in pieces, and a piece can end on a literal "</html>" inside a
# script; the real end of the document is the one nothing follows.
_DUMP_SETTLE_SECONDS = 0.3


def _read_dom_dump(proc, timeout: float) -> bytes:
    """Chrome's --dump-dom output, returned as soon as the document is whole.

    Stops at whichever comes first: the output ends in </html> and nothing
    follows, Chrome closes stdout, or `timeout` seconds pass — in which case
    whatever was written so far is returned, exactly as the old fixed wait did.
    Never waits for EOF alone: Chrome's forked children hold the pipe open.
    """
    fd = proc.stdout.fileno()
    deadline = time.monotonic() + timeout
    buf = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            break
        chunk = os.read(fd, 1 << 16)
        if not chunk:
            break
        buf += chunk
        if (bytes(buf[-64:]).rstrip().endswith(b"</html>")
                and not select.select([fd], [], [], _DUMP_SETTLE_SECONDS)[0]):
            break
    return bytes(buf)


def _render_html(url: str, timeout: float = 15) -> Optional[str]:
    """The page's HTML AFTER JavaScript has run, via headless Chrome.

    Exists because the pages that matter most for some vendors are
    single-page apps that serve an empty shell to a plain GET. Measured:
    documentation.avaya.com returns 69KB of HTML from which trafilatura
    extracts NOTHING, and Credly badge pages (which carry the course codes
    like 61131V) return 50KB and extract nothing — no meta description to
    fall back on either. Those aren't thin pages, they're unrendered ones,
    and no amount of extra allowlisted domains fixes that.

    Only called when static extraction has already failed, so the common
    path never pays for it. Returns None on any failure — a slow or missing
    browser must degrade to the old behaviour, never break a run.

    Set RETRIEVAL_RENDER=0 to disable.
    """
    if os.getenv("RETRIEVAL_RENDER", "1").strip() == "0":
        return None
    binary = _chrome_binary()
    if not binary:
        return None
    # --headless=new, and NO --virtual-time-budget. With the budget flag these
    # SPAs produced 0 bytes however long they were given: virtual time never
    # settles while the page keeps issuing requests, so the DOM is never
    # dumped. Measured on a Credly badge page: 246KB containing the course
    # code at a 5s wait, identical at 8s and 12s.
    #
    # WHEN to stop took measuring too. Chrome dumps the DOM once the page has
    # loaded and then, on some pages, never exits, so this used to wait out a
    # flat 6s and keep whatever had been written. That cost the full 6s even
    # on pages dumped sooner, and returned NOTHING for pages that need longer:
    # docs.paloaltonetworks.com takes 6.5-7.2s on the server, so all three
    # sources of a Panorama article came back empty and it could never be
    # re-scored. Now the dump is read as it arrives and reading stops the
    # moment the document is complete; `timeout` is only the ceiling for a
    # page that never finishes.
    #
    # ignore_cleanup_errors: even with the process-group kill below there is a
    # window where a dying renderer writes one more file into the profile
    # while rmtree is walking it. That raced and took the whole request down
    # with "OSError: [Errno 39] Directory not empty: 'Default'" — a temp
    # directory we no longer care about must never be able to fail a write.
    with tempfile.TemporaryDirectory(prefix="reded-render-",
                                     ignore_cleanup_errors=True) as profile:
        cmd = [binary, "--headless=new", "--disable-gpu", "--no-sandbox",
               "--no-first-run", "--disable-dev-shm-usage",
               f"--user-data-dir={profile}", "--dump-dom", url]
        proc = None
        out = b""
        try:
            # start_new_session puts Chrome and every process it forks into
            # one process group, which is the only way to kill all of them.
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL,
                                    start_new_session=True)
            out = _read_dom_dump(proc, timeout)
        except OSError:
            return None
        finally:
            if proc is not None:
                if proc.poll() is None:
                    _kill_process_group(proc)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                if proc.stdout is not None:
                    proc.stdout.close()
    html = out.decode("utf-8", errors="replace")
    return html or None


def _is_public_http_url(url: str) -> bool:
    """Refuse anything that is not a plain public http(s) fetch.

    The allowlist was doing this job implicitly: every URL had to be one of 61
    known public domains, so nothing could aim the fetcher at the machine it
    runs on. Allowing arbitrary domains removes that guarantee, so the check
    has to become explicit — otherwise a search result could point at
    127.0.0.1 or a metadata endpoint and the server would dutifully retrieve
    it.

    Literal addresses and obvious local names only. A hostname that RESOLVES
    to a private address still gets through, which would need connection-level
    control to stop; this closes the direct case rather than pretending to be
    a complete SSRF defence.
    """
    import ipaddress

    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").strip().lower()
    if not host or host == "localhost" or host.endswith(".localhost"):
        return False
    if host.endswith(".internal") or host.endswith(".local"):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True                     # a normal domain name
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def fetch_source(url: str, timeout: int = 20,
                 allow_any_domain: bool = False) -> Optional[Source]:
    """Fetch + clean a single URL. Returns None if it can't be used.

    `allow_any_domain` lifts the allowlist requirement, for the Quick draft
    path only — the one that says on its own page that nothing is saved and
    that governed content belongs in Article Studio. Everything admitted that
    way is tagged OPEN_WEB_TIER so a reader of the claims can tell a vetted
    source from one Google happened to return."""
    if trafilatura is None:
        raise RuntimeError("trafilatura not installed. Run: pip install -r requirements.txt")

    allow = load_allowlist()
    meta = allow.match(_domain(url))
    if meta is None:
        if not allow_any_domain:
            return None  # not on the allowlist — refuse to ingest
        if not _is_public_http_url(url):
            return None  # never fetch the machine we are running on
        meta = {"tier": OPEN_WEB_TIER, "is_vendor": False, "vendor": None}

    try:
        resp = requests.get(url, headers={"User-Agent": _UA}, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException:
        return None

    # Many vendors' official spec sheets/support docs are raw PDFs, not HTML —
    # trafilatura only parses HTML, so these silently came back "too thin" and
    # got dropped, cutting real diversity for exactly the topics (niche
    # products, technical rules) where independent web coverage is already
    # thin. Detect by Content-Type (PDFs sometimes lack a .pdf URL extension)
    # and route to pypdf instead.
    content_type = resp.headers.get("content-type", "")
    if "application/pdf" in content_type or url.lower().endswith(".pdf"):
        extracted, pdf_title = _extract_pdf(resp.content)
        title = pdf_title or url
        published = None
    else:
        downloaded = resp.text
        extracted = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=True,
            favor_precision=True,
        )
        md = trafilatura.extract_metadata(downloaded)
        title = (md.title if md and md.title else "").strip() or url
        published = _parse_date(md.date if md else None)

    # Second attempt through a real browser for JS-rendered pages. Cheap in
    # aggregate: only pages that already failed reach this.
    if (not extracted or len(extracted) < 250) and "application/pdf" not in content_type:
        rendered = _render_html(url)
        if rendered:
            # NOT favor_precision here, unlike the static path. On a rendered
            # SPA the precision heuristic picked the COOKIE CONSENT DIALOG as
            # the main content — 1,560 chars of "these cookies are necessary"
            # with the actual badge text discarded. Default extraction returns
            # the real content ("61131V - Administering Avaya Aura® System
            # Manager, issued by Red Education...").
            extracted = trafilatura.extract(
                rendered, include_comments=False, include_tables=True)
            md = trafilatura.extract_metadata(rendered)
            if md and md.title:
                title = md.title.strip() or title
            published = published or _parse_date(md.date if md else None)

    if not extracted or len(extracted) < 250:
        return None  # genuinely too thin, even rendered

    return Source(
        url=url,
        domain=_domain(url),
        title=title,
        text=extracted.strip(),
        published=published,
        tier=meta["tier"],
        is_vendor=meta["is_vendor"],
        vendor=meta.get("vendor"),
    )


def _extract_pdf(content: bytes) -> tuple[Optional[str], Optional[str]]:
    """Plain text extraction from a PDF's pages, plus a best-effort title —
    the PDF's own metadata title if it has one, else the first non-empty
    line of extracted text (PDF cover pages almost always open with the
    document's real title). Returns (None, None) (never raises) on anything
    malformed/encrypted/unreadable — same "skip, don't crash" contract as
    the rest of this module."""
    if pypdf is None:
        return None, None
    import io
    try:
        reader = pypdf.PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            return None, None
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        meta_title = (reader.metadata.title or "").strip() if reader.metadata else ""
        first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        title = meta_title or first_line[:200] or None
        return text, title
    except Exception:
        return None, None


def _parse_date(raw) -> Optional[date]:
    if not raw or dateparser is None:
        return None
    dt = dateparser.parse(str(raw))
    return dt.date() if dt else None
