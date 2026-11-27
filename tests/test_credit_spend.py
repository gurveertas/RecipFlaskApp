"""Serper credits are real money and the account ran down to 90.

Two lines dominated the last 245 credits: retrieval at 122 (50%) and
competitor_snapshot at 71 (29%). Both were spending on work already done.
"""
import pytest

from retrieval.search import SearchHit


class TestTheSerpSnapshotIsCached:
    """71 of 245 credits, for a check that is WARN-ONLY by design and can
    never block an article. The log shows one keyword fetched 13 times in an
    afternoon because every write, targeted fix and re-score refetched it."""

    @pytest.fixture(autouse=True)
    def _clear(self):
        import retrieval.serp as S
        S._snapshot_cache.clear()
        yield
        S._snapshot_cache.clear()

    def _stub(self, monkeypatch, result=None):
        import retrieval.serp as S
        calls = []
        monkeypatch.setattr(S, "_plain_search",
                            lambda q, num=10, purpose="": (calls.append(q), result)[1])
        return calls

    def test_the_same_keyword_is_fetched_once(self, monkeypatch):
        import retrieval.serp as S
        calls = self._stub(monkeypatch)
        for _ in range(5):
            S.competitor_snapshot("edu-330 firewall troubleshooting")
        assert len(calls) == 1

    def test_a_different_keyword_is_fetched_separately(self, monkeypatch):
        import retrieval.serp as S
        calls = self._stub(monkeypatch)
        S.competitor_snapshot("one keyword")
        S.competitor_snapshot("another keyword")
        assert len(calls) == 2

    def test_the_key_ignores_case_and_padding(self, monkeypatch):
        import retrieval.serp as S
        calls = self._stub(monkeypatch)
        S.competitor_snapshot("CCSE Certification")
        S.competitor_snapshot("  ccse certification ")
        assert len(calls) == 1

    def test_a_failure_is_retried_once_its_short_ttl_expires(self, monkeypatch):
        # A SERP outage must not blind the check for the rest of the day, and
        # must not make every QA run pay for another timeout either.
        import retrieval.serp as S
        calls = self._stub(monkeypatch)
        S.competitor_snapshot("k")
        S.competitor_snapshot("k")
        assert len(calls) == 1
        monkeypatch.setattr(S, "_SNAPSHOT_FAILURE_TTL", 0)
        S.competitor_snapshot("k")
        assert len(calls) == 2


class TestTheScopedSearchIsNotOversized:
    """domains_for_topic works the vendor out of the topic STRING, and a
    certification is named by its acronym. "CCSE certification" matched no
    vendor, fell through to all 54 vendor domains, and cost 7 credits in
    site:-batches instead of 4."""

    class _Client:
        def __init__(self, open_hits):
            self._open = open_hits
            self.scoped_domains = None

        def search(self, query, allowed_domains, limit=12):
            self.scoped_domains = list(allowed_domains)
            return []

        def search_open(self, query, num=20):
            return list(self._open)

    def _run(self, monkeypatch, open_urls, **kw):
        import retrieval
        monkeypatch.setattr("retrieval.fetch.fetch_source",
                            lambda url, timeout=20, allow_any_domain=False: None)
        c = self._Client([SearchHit(url=u, title="t", snippet="s") for u in open_urls])
        retrieval.retrieve("CCSE certification", client=c, limit=4, **kw)
        return c

    def test_one_ranking_vendor_narrows_the_search(self, monkeypatch):
        c = self._run(monkeypatch, ["https://www.checkpoint.com/a",
                                    "https://www.csoonline.com/b"])
        assert "cisco.com" not in c.scoped_domains
        assert any("checkpoint.com" in d for d in c.scoped_domains)

    def test_two_ranking_vendors_leave_it_wide(self, monkeypatch):
        # A comparison genuinely spans vendors; narrowing to whichever ranked
        # first would quietly drop the other.
        c = self._run(monkeypatch, ["https://www.checkpoint.com/a",
                                    "https://www.cisco.com/b"])
        assert any("checkpoint.com" in d for d in c.scoped_domains)
        assert any("cisco.com" in d for d in c.scoped_domains)

    def test_an_explicit_vendor_still_wins(self, monkeypatch):
        # Article Studio names the vendor authoritatively; nothing inferred
        # may override it.
        c = self._run(monkeypatch, ["https://www.cisco.com/b"],
                      vendor_name="Check Point")
        assert any("checkpoint.com" in d for d in c.scoped_domains)
        assert "cisco.com" not in c.scoped_domains


class TestTheSerpIsBoughtOncePerKeyword:
    """competitor_snapshot and serp_difficulty both ask for the top 10 of a
    keyword, for different reasons. An article's keyword was therefore bought
    twice: once when its topic was scored, again at QA."""

    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch):
        import retrieval.serp as S
        S._search_cache.clear()
        S._snapshot_cache.clear()
        monkeypatch.setenv("SERPER_API_KEY", "test-key")
        self.posts = []

        class _Resp:
            def raise_for_status(self): pass
            def json(self): return {"organic": [{"link": "https://x/", "title": "t"}]}

        import requests
        monkeypatch.setattr(requests, "post",
                            lambda *a, **k: (self.posts.append(1), _Resp())[1])
        yield
        S._search_cache.clear()
        S._snapshot_cache.clear()

    def test_two_purposes_share_one_paid_call(self):
        import retrieval.serp as S
        S._plain_search("edu-330", num=10, purpose="serp_difficulty")
        S._plain_search("edu-330", num=10, purpose="competitor_snapshot")
        assert len(self.posts) == 1

    def test_a_different_result_size_is_a_different_search(self):
        # A top-10 is not a top-20; serving one for the other would silently
        # truncate the union channel's results.
        import retrieval.serp as S
        S._plain_search("edu-330", num=10)
        S._plain_search("edu-330", num=20)
        assert len(self.posts) == 2

    def test_a_cache_hit_records_no_credit(self):
        # The meter must not bill for a call that never happened, or the
        # balance drifts the other way.
        import retrieval.serp as S
        from config.db import ApiUsage, get_session

        def tracked():
            with get_session() as s:
                return sum(r.credits for r in s.query(ApiUsage).all()
                           if r.provider == "serper")

        S._plain_search("uncached-probe-keyword", num=10, purpose="serp")
        after_first = tracked()
        S._plain_search("uncached-probe-keyword", num=10, purpose="serp")
        assert tracked() == after_first

    def test_a_missing_api_key_is_not_cached(self, monkeypatch):
        # Configuration, not a result: setting the key later must work
        # without waiting out a TTL.
        import retrieval.serp as S
        monkeypatch.delenv("SERPER_API_KEY", raising=False)
        assert S._plain_search("k") is None
        monkeypatch.setenv("SERPER_API_KEY", "test-key")
        assert S._plain_search("k") is not None
