"""Headless render stops when the DOM dump is complete, not after a fixed wait.

A flat 6-second kill returned NOTHING for docs.paloaltonetworks.com, which
takes 6.5-7.2s to dump on the server: all three sources of a Panorama article
came back empty and the article could never be re-scored. Each test drives
_render_html against a fake "Chrome" that behaves the way the real one does.
"""
import inspect
import os
import sys
import textwrap
import time

import retrieval.fetch as F


def _fake_chrome(tmp_path, monkeypatch, body: str):
    script = tmp_path / "chrome"
    script.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    script.chmod(0o755)
    monkeypatch.setattr(F, "_chrome_binary", lambda: str(script))
    monkeypatch.setenv("RETRIEVAL_RENDER", "1")


def _render(timeout):
    started = time.monotonic()
    html = F._render_html("https://example.com/page", timeout=timeout)
    return html, time.monotonic() - started


class TestRenderStopsWhenTheDumpIsComplete:

    def test_a_page_that_hangs_after_dumping_returns_promptly(self, tmp_path,
                                                             monkeypatch):
        # Real Chrome's usual shape: dump the DOM, then never exit.
        _fake_chrome(tmp_path, monkeypatch, """
            import sys, time
            time.sleep(0.5)
            sys.stdout.write("<html><body><p>Panorama capacity</p></body></html>\\n")
            sys.stdout.flush()
            time.sleep(60)
        """)
        html, took = _render(timeout=20)
        assert "Panorama capacity" in html
        assert html.rstrip().endswith("</html>")
        assert took < 5, f"waited {took:.1f}s for a document that was already whole"

    def test_the_default_ceiling_clears_the_slowest_measured_vendor_page(self):
        # 7.2s was the slowest docs.paloaltonetworks.com dump on the server.
        default = inspect.signature(F._render_html).parameters["timeout"].default
        assert default >= 10

    def test_a_closing_tag_inside_a_script_does_not_end_the_read(self, tmp_path,
                                                                monkeypatch):
        _fake_chrome(tmp_path, monkeypatch, """
            import sys, time
            sys.stdout.write("<html><body><script>var t = '</html>")
            sys.stdout.flush()
            time.sleep(0.05)
            sys.stdout.write("';</script><p>the real content</p></body></html>\\n")
            sys.stdout.flush()
            time.sleep(60)
        """)
        html, _ = _render(timeout=20)
        assert "the real content" in html

    def test_a_page_that_never_finishes_stops_at_the_ceiling(self, tmp_path,
                                                            monkeypatch):
        _fake_chrome(tmp_path, monkeypatch, """
            import sys, time
            sys.stdout.write("<html><body><p>partial")
            sys.stdout.flush()
            time.sleep(60)
        """)
        html, took = _render(timeout=1.5)
        assert html.startswith("<html>")
        assert took < 4

    def test_chrome_and_its_children_are_gone_afterwards(self, tmp_path,
                                                        monkeypatch):
        # The child inherits stdout, as Chrome's renderer does, so EOF never
        # arrives while it lives — the read must not depend on it.
        pids = tmp_path / "pids"
        _fake_chrome(tmp_path, monkeypatch, f"""
            import os, subprocess, sys, time
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            open({str(pids)!r}, "w").write(f"{{os.getpid()}} {{child.pid}}")
            sys.stdout.write("<html><body>done</body></html>\\n")
            sys.stdout.flush()
            time.sleep(60)
        """)
        html, took = _render(timeout=20)
        assert "done" in html and took < 5
        alive = {int(p) for p in pids.read_text().split()}
        deadline = time.monotonic() + 3
        while alive and time.monotonic() < deadline:
            for pid in list(alive):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    alive.discard(pid)
            time.sleep(0.05)
        assert not alive, f"left running: {alive}"
