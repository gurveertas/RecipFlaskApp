"""Article -> PDF, via the headless Chrome already on this box.

No new dependency. The alternatives (WeasyPrint, wkhtmltopdf) each pull a
native stack — cairo/pango or a patched Qt — onto a server where retrieval
already needs Chrome and has it installed, and where a broken native build
would take the whole app down rather than just this button.

The HTML handed to Chrome is SELF-CONTAINED: styles inline, no webfonts, no
/static references. A PDF that silently loses its layout because a font
request timed out is worse than one in a system serif, and Chrome renders it
from a file:// URL where those requests would be the only network traffic in
the whole operation.
"""
from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path

# Same binary discovery and the same process-group kill the retrieval renderer
# uses. Chrome forks a zygote, a GPU process and a renderer; killing only the
# process we launched leaves those writing into the profile directory we are
# about to delete, which raised "Directory not empty" and took a request down.
from retrieval.fetch import _chrome_binary, _kill_process_group


def html_to_pdf(html: str, timeout: int = 45) -> bytes | None:
    """Rendered PDF bytes, or None if Chrome is unavailable or fails.

    None rather than an exception: this is a convenience button, and a machine
    without Chrome should get a clear message rather than a 500."""
    binary = _chrome_binary()
    if not binary:
        return None

    with tempfile.TemporaryDirectory(prefix="reded-pdf-",
                                     ignore_cleanup_errors=True) as work:
        src = Path(work) / "article.html"
        out = Path(work) / "article.pdf"
        src.write_text(html, encoding="utf-8")
        cmd = [
            binary, "--headless=new", "--disable-gpu", "--no-sandbox",
            "--no-first-run", "--disable-dev-shm-usage",
            f"--user-data-dir={work}/profile",
            # Chrome's own header/footer stamps the file:// path and a date
            # across every page, which would put a temp directory in a
            # document meant to be sent to somebody.
            "--no-pdf-header-footer",
            f"--print-to-pdf={out}", src.as_uri(),
        ]
        proc = None
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL,
                                    start_new_session=True)
            # THE TIMEOUT IS NOT THE NORMAL PATH, AND NEITHER IS EXITING.
            # Measured: Chrome writes a complete PDF and then does not exit,
            # exactly as it behaves for --dump-dom. Waiting out the timeout
            # would make every export take the full `timeout` seconds, so
            # poll for the file instead and stop as soon as it stops growing.
            deadline = time.monotonic() + timeout
            last = -1
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break                       # exited by itself after all
                time.sleep(0.25)
                size = out.stat().st_size if out.exists() else 0
                if size and size == last:
                    break                       # written and settled
                last = size
        except OSError:
            return None
        finally:
            if proc is not None and proc.poll() is None:
                _kill_process_group(proc)

        if not out.exists() or out.stat().st_size == 0:
            return None
        data = out.read_bytes()
        # A truncated file is worse than none: it downloads, opens as a broken
        # document, and looks like the article is at fault.
        return data if data.startswith(b"%PDF-") else None
