"""Retrieval unions a scoped search with one unconstrained search.

The scoped search splits its domains into batches of 8 and takes each batch's
own top 10, so it never sees a single global ranking — a strong page loses to a
weaker one for being in a busier batch. The open channel adds the ranking
Google actually produced, for 1 credit against the scoped search's 4.

The load-bearing property, and what every test here is really about: BOTH
channels are filtered through the same allowlist, so the open channel cannot
widen what is ingested. Measured before it was built, an unconstrained search
for "CCME certification exam" returns Cellebrite pages about a different
credential that shares the acronym, plus four exam-dump farms.
"""
import os

import pytest

from config.db import (DomainSighting, DomainSightingStatus, get_session,
                       record_domain_sighting)
from retrieval.search import SearchHit


class FakeClient:
    """Records what it was asked, returns what it was told to."""

    def __init__(self, scoped, open_hits):
        self._scoped, self._open = scoped, open_hits
        self.open_calls = 0

    def search(self, query, allowed_domains, limit=12):
        return list(self._scoped)

    def search_open(self, query, num=20):
        self.open_calls += 1
        return list(self._open)


def hit(url, title="t"):
    return SearchHit(url=url, title=title, snippet="s")


@pytest.fixture(autouse=True)
def _clean():
    yield
    with get_session() as s:
        for r in s.query(DomainSighting).all():
            s.delete(r)
        s.commit()


def run(scoped, open_hits, monkeypatch, **env):
    """Drive retrieve() with fetching stubbed out, returning what it tried to
    fetch — the union, after allowlist filtering."""
    import retrieval
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    tried = []

    def fake_fetch(url, timeout=20, allow_any_domain=False):
        tried.append((url, allow_any_domain))
        return None                    # nothing is really downloaded

    monkeypatch.setattr("retrieval.fetch.fetch_source", fake_fetch)
    client = FakeClient(scoped, open_hits)
    retrieval.retrieve("check point ccme certification", client=client, limit=8)
    return [u for u, _f in tried], client


class TestTheUnion:
    def test_an_allowlisted_open_hit_is_added(self, monkeypatch):
        tried, _ = run([hit("https://blog.checkpoint.com/a")],
                       [hit("https://www.paloaltonetworks.com/b")], monkeypatch)
        assert "https://www.paloaltonetworks.com/b" in tried

    def test_a_non_allowlisted_open_hit_is_never_fetched(self, monkeypatch):
        # 591lab, certlibrary, testpassport — the sites we compete against.
        tried, _ = run([hit("https://blog.checkpoint.com/a")],
                       [hit("https://591lab.com/dumps")], monkeypatch)
        assert not any("591lab" in u for u in tried)

    def test_scoped_hits_come_first(self, monkeypatch):
        # The trusted set leads; the open channel only ever supplements.
        tried, _ = run([hit("https://blog.checkpoint.com/a")],
                       [hit("https://www.paloaltonetworks.com/b")], monkeypatch)
        assert tried[0] == "https://blog.checkpoint.com/a"

    def test_a_url_both_channels_return_is_fetched_once(self, monkeypatch):
        u = "https://blog.checkpoint.com/same"
        tried, _ = run([hit(u)], [hit(u)], monkeypatch)
        assert tried.count(u) == 1

    def test_it_can_be_switched_off(self, monkeypatch):
        tried, client = run([hit("https://blog.checkpoint.com/a")],
                            [hit("https://www.paloaltonetworks.com/b")],
                            monkeypatch, RETRIEVAL_OPEN_SEARCH="0")
        assert client.open_calls == 0
        assert tried == ["https://blog.checkpoint.com/a"]


class TestTheGapReport:
    def test_a_rejected_domain_is_recorded(self, monkeypatch):
        run([], [hit("https://www.gartner.com/mq", "Magic Quadrant")], monkeypatch)
        with get_session() as s:
            row = s.query(DomainSighting).filter_by(domain="www.gartner.com").one()
            assert row.hits == 1
            assert row.status is DomainSightingStatus.PENDING
            assert row.last_title == "Magic Quadrant"

    def test_an_allowlisted_domain_is_not_recorded(self, monkeypatch):
        # The report is a list of GAPS. A domain we can already cite is not one.
        run([], [hit("https://blog.checkpoint.com/a")], monkeypatch)
        with get_session() as s:
            assert s.query(DomainSighting).count() == 0

    def test_repeat_sightings_accumulate(self):
        record_domain_sighting("gartner.com", "u1", "first", "q1")
        record_domain_sighting("gartner.com", "u2", "second", "q2")
        with get_session() as s:
            row = s.query(DomainSighting).filter_by(domain="gartner.com").one()
            assert row.hits == 2
            assert row.last_title == "second"

    def test_recording_never_raises(self):
        # Best-effort by contract: bookkeeping must not break the retrieval it
        # is observing.
        record_domain_sighting("")
        record_domain_sighting(None)


class TestTheAdminPage:
    def _client(self):
        from webapp.app import create_app
        return create_app().test_client()

    def test_the_page_renders(self):
        assert self._client().get("/admin/domains").status_code == 200

    def test_ignoring_takes_it_off_the_queue(self):
        record_domain_sighting("noise.example", "u", "t", "q")
        with get_session() as s:
            sid = s.query(DomainSighting).filter_by(domain="noise.example").one().id
        r = self._client().post(f"/admin/domains/{sid}/ignore")
        assert r.status_code in (302, 303)
        with get_session() as s:
            assert s.get(DomainSighting, sid).status is DomainSightingStatus.IGNORED

    def test_approving_adds_it_to_the_allowlist(self):
        from config import load_allowlist
        from config.db import AllowlistDomain
        record_domain_sighting("analyst.example", "u", "t", "q")
        with get_session() as s:
            sid = s.query(DomainSighting).filter_by(domain="analyst.example").one().id
        self._client().post(f"/admin/domains/{sid}/approve",
                            data={"category": "press", "tier": "2"})
        with get_session() as s:
            added = s.query(AllowlistDomain).filter_by(
                domain="analyst.example").one_or_none()
            assert added is not None and added.tier == 2
            assert s.get(DomainSighting, sid) is None   # queue entry consumed
            s.delete(added)
            s.commit()
        load_allowlist.cache_clear()


class TestRenderCleanupCannotFailARequest:
    """A JS-render attempt took down the whole Brief step.

    Headless Chrome dumps the DOM and then does not exit, so the timeout is
    the normal path. subprocess.run's kill reaches only the process we
    launched; the zygote, GPU process and renderer it forked survived, kept
    writing into the --user-data-dir, and TemporaryDirectory's rmtree died
    with "OSError: [Errno 39] Directory not empty: 'Default'".

    The union channel did not create this — it made it reachable, by putting
    more pages in front of the fetcher, some of which need rendering.
    """

    def test_a_cleanup_failure_is_not_raised_at_the_caller(self, tmp_path,
                                                           monkeypatch):
        # The precise failure: something still writing while rmtree walks.
        import retrieval.fetch as F

        monkeypatch.setattr(F, "_chrome_binary", lambda: "/bin/true")

        real = F.tempfile.TemporaryDirectory

        def exploding(*a, **kw):
            # A TemporaryDirectory that always fails to clean up, unless the
            # caller asked for that to be ignored.
            assert kw.get("ignore_cleanup_errors") is True, (
                "render must not let temp cleanup fail the request")
            return real(*a, **kw)

        monkeypatch.setattr(F.tempfile, "TemporaryDirectory", exploding)
        F._render_html("https://example.com/x", timeout=1)

    def test_the_whole_process_group_is_signalled(self, monkeypatch):
        # proc.kill() is not enough: it leaves Chrome's children running.
        import retrieval.fetch as F
        killed = {}

        class FakeProc:
            pid = 4242
            def poll(self): return None
            def kill(self): killed["single"] = True

        monkeypatch.setattr(F.os, "getpgid", lambda pid: pid)
        monkeypatch.setattr(F.os, "killpg",
                            lambda pgid, sig: killed.setdefault("group", pgid))
        F._kill_process_group(FakeProc())
        assert killed.get("group") == 4242

    def test_killing_never_raises_when_the_process_is_already_gone(self,
                                                                  monkeypatch):
        import retrieval.fetch as F

        class Gone:
            pid = 1
            def kill(self): raise OSError("no such process")

        def boom(*a, **kw):
            raise ProcessLookupError()

        monkeypatch.setattr(F.os, "getpgid", boom)
        F._kill_process_group(Gone())      # must not raise
