"""Quick draft has to work for any topic. Article Studio must not change.

The allowlist is 61 IT-security and vendor-training domains. "Cycling tips"
searched all 54 vendor domains, matched nothing, and had every real cycling
result refused at the fetch gate — Live mode could only ever work for vendor
topics.

The governed path is the reason every test here checks BOTH directions: the
open web is opened for the ungoverned Quick draft page and for nothing else.
"""
import pytest

from retrieval.search import SearchHit


class FakeClient:
    def __init__(self, scoped, open_hits):
        self._scoped, self._open = scoped, open_hits
        self.scoped_calls = 0

    def search(self, query, allowed_domains, limit=12):
        self.scoped_calls += 1
        return list(self._scoped)

    def search_open(self, query, num=20):
        return list(self._open)


def hit(url, title="t"):
    return SearchHit(url=url, title=title, snippet="s")


ALLOWLISTED = "https://blog.checkpoint.com/a"
FOREIGN = "https://www.cyclingweekly.com/tips"


def run(monkeypatch, scoped, open_hits, **kw):
    """Drive retrieve(), returning (url, allow_any_domain) for each fetch."""
    import retrieval
    tried = []

    def fake_fetch(url, timeout=20, allow_any_domain=False):
        tried.append((url, allow_any_domain))
        return None

    monkeypatch.setattr("retrieval.fetch.fetch_source", fake_fetch)
    client = FakeClient(scoped, open_hits)
    retrieval.retrieve("cycling tips", client=client, limit=8, **kw)
    return tried, client


class TestTheGovernedPathIsUnchanged:
    def test_a_foreign_domain_is_never_fetched_by_default(self, monkeypatch):
        tried, _ = run(monkeypatch, [hit(ALLOWLISTED)], [hit(FOREIGN)])
        assert all("cyclingweekly" not in u for u, _f in tried)

    def test_allow_any_domain_is_never_set_by_default(self, monkeypatch):
        # The flag that lifts the allowlist must stay off for Studio.
        tried, _ = run(monkeypatch, [hit(ALLOWLISTED)], [])
        assert all(flag is False for _u, flag in tried)

    def test_the_scoped_search_still_runs_by_default(self, monkeypatch):
        _tried, client = run(monkeypatch, [hit(ALLOWLISTED)], [hit(FOREIGN)])
        assert client.scoped_calls == 1

    def test_studio_call_sites_do_not_opt_in(self):
        # The one guarantee the whole change rests on.
        import inspect
        from webapp import pipeline
        src = inspect.getsource(pipeline._live_bundle)
        assert "allow_open_web" not in src


class TestAForeignTopicReachesTheOpenWeb:
    def test_open_results_are_fetched_when_nothing_allowlisted_ranks(self, monkeypatch):
        tried, _ = run(monkeypatch, [], [hit(FOREIGN)], allow_open_web=True)
        assert ("https://www.cyclingweekly.com/tips", True) in tried

    def test_the_scoped_search_is_skipped_entirely(self, monkeypatch):
        # Searching 54 firewall vendors for cycling tips costs 4-7 credits to
        # return nothing.
        _tried, client = run(monkeypatch, [], [hit(FOREIGN)], allow_open_web=True)
        assert client.scoped_calls == 0


class TestAnItTopicStaysGovernedEvenOnQuickDraft:
    def test_one_allowlisted_result_keeps_the_allowlist(self, monkeypatch):
        # "CCSE certification" names no vendor in its text, so a string test
        # would send it to the open web where the exam dump farms are. What
        # settles it is whether anything of ours actually ranks.
        tried, client = run(monkeypatch, [hit(ALLOWLISTED)],
                            [hit(ALLOWLISTED), hit(FOREIGN)],
                            allow_open_web=True)
        assert client.scoped_calls == 1
        assert all("cyclingweekly" not in u for u, _f in tried)
        assert all(flag is False for _u, flag in tried)


class TestTheHostGuard:
    """The allowlist was implicitly stopping the fetcher aiming at this
    machine. Allowing arbitrary domains removes that, so it becomes explicit."""

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1:8000/x", "http://localhost/admin",
        "http://169.254.169.254/latest/meta-data/",   # cloud metadata
        "https://10.0.0.5/x", "https://192.168.1.1/", "http://[::1]/x",
        "file:///etc/passwd", "ftp://example.com/x",
        "https://service.internal/x",
    ])
    def test_non_public_targets_are_refused(self, url):
        from retrieval.fetch import _is_public_http_url
        assert _is_public_http_url(url) is False

    @pytest.mark.parametrize("url", [
        "https://www.cyclingweekly.com/tips", "http://example.com/a",
        "https://sub.domain.co.uk/path?q=1",
    ])
    def test_ordinary_public_urls_pass(self, url):
        from retrieval.fetch import _is_public_http_url
        assert _is_public_http_url(url) is True

    def test_fetch_refuses_a_private_host_even_when_opened_up(self, monkeypatch):
        from retrieval import fetch as F
        # No network call should happen at all; if one did, this would hang
        # rather than return None.
        monkeypatch.setattr(F.requests, "get",
                            lambda *a, **k: pytest.fail("fetched a private host"))
        assert F.fetch_source("http://127.0.0.1/x", allow_any_domain=True) is None


class TestQuickDraftOptsIn:
    def test_the_service_passes_the_flag(self):
        import inspect
        from webapp import service
        assert "allow_open_web=True" in inspect.getsource(service.run_generation)

    def test_open_web_sources_are_tagged_as_unvetted(self):
        # Tier 1 is authoritative, 2 established press, 3 "nobody vetted
        # this" — recorded on every Claim so downstream can tell.
        from retrieval.fetch import OPEN_WEB_TIER
        assert OPEN_WEB_TIER == 3
