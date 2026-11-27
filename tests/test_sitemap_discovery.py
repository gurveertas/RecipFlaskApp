"""The coverage scrape must follow the site's sitemap index, and fail loudly.

Hard-coded Yoast-style sitemap names 404'd after the site moved to WordPress
core sitemaps. A 404 there is a themed HTML page with no <url> blocks, so the
run reported "found 0 URLs", exited 0, and still stamped coverage_scraped_at —
nightly, for weeks, while the vendor step claimed the index was current.
"""
import subprocess

import pytest

import scrape_published_posts as scraper

INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex><sitemap><loc>https://www.rededucation.com/wp-sitemap-posts-post-1.xml</loc></sitemap>
<sitemap><loc>https://www.rededucation.com/wp-sitemap-posts-post-2.xml</loc></sitemap>
<sitemap><loc>https://www.rededucation.com/wp-sitemap-posts-page-1.xml</loc></sitemap>
<sitemap><loc>https://www.rededucation.com/wp-sitemap-posts-case-studies-1.xml</loc></sitemap>
<sitemap><loc>https://www.rededucation.com/wp-sitemap-posts-courses-1.xml</loc></sitemap>
<sitemap><loc>https://www.rededucation.com/wp-sitemap-posts-course-explained-1.xml</loc></sitemap>
<sitemap><loc>https://www.rededucation.com/wp-sitemap-posts-coursesvender-1.xml</loc></sitemap>
<sitemap><loc>https://www.rededucation.com/wp-sitemap-posts-product-3.xml</loc></sitemap>
<sitemap><loc>https://www.rededucation.com/wp-sitemap-posts-team-1.xml</loc></sitemap>
<sitemap><loc>https://www.rededucation.com/wp-sitemap-posts-awards-1.xml</loc></sitemap>
<sitemap><loc>https://www.rededucation.com/wp-sitemap-taxonomies-vendors-1.xml</loc></sitemap>
<sitemap><loc>https://www.rededucation.com/wp-sitemap-taxonomies-category-1.xml</loc></sitemap>
<sitemap><loc>https://www.rededucation.com/wp-sitemap-users-1.xml</loc></sitemap></sitemapindex>"""


class TestDiscovery:
    def _discover(self, monkeypatch, index=INDEX):
        monkeypatch.setattr(scraper, "_fetch", lambda url, **kw: index)
        return scraper._discover_sitemaps()

    def test_every_content_post_type_is_claimed(self, monkeypatch):
        found = dict(self._discover(monkeypatch))
        assert found["https://www.rededucation.com/wp-sitemap-posts-post-1.xml"] == "post"
        assert found["https://www.rededucation.com/wp-sitemap-posts-page-1.xml"] == "page"
        assert found["https://www.rededucation.com/wp-sitemap-posts-case-studies-1.xml"] == "case_study"

    def test_all_four_course_sources_map_to_course(self, monkeypatch):
        found = dict(self._discover(monkeypatch))
        courses = [u for u, k in found.items() if k == "course"]
        assert len(courses) == 4
        assert any("posts-courses-" in u for u in courses)
        assert any("course-explained" in u for u in courses)
        assert any("coursesvender" in u for u in courses)
        assert any("posts-product-" in u for u in courses)

    def test_a_numbered_file_we_never_named_is_still_followed(self, monkeypatch):
        # product-3 without product-1/2 present: the point of reading the
        # index is that a new or renumbered file needs no code change.
        found = dict(self._discover(monkeypatch))
        assert "https://www.rededucation.com/wp-sitemap-posts-product-3.xml" in found
        assert "https://www.rededucation.com/wp-sitemap-posts-post-2.xml" in found

    def test_listing_pages_are_left_out(self, monkeypatch):
        found = dict(self._discover(monkeypatch))
        for unwanted in ("team", "awards", "taxonomies-category", "users"):
            assert not any(unwanted in u for u in found), unwanted

    def test_vendors_taxonomy_keeps_its_old_page_kind(self, monkeypatch):
        found = dict(self._discover(monkeypatch))
        assert found["https://www.rededucation.com/wp-sitemap-taxonomies-vendors-1.xml"] == "page"

    def test_every_kind_is_one_the_priority_table_knows(self, monkeypatch):
        # An unknown kind would raise a KeyError deep inside _list_urls.
        for _url, kind in self._discover(monkeypatch):
            assert kind in scraper.KIND_PRIORITY


class TestFetchFailsLoudly:
    def _curl(self, monkeypatch, returncode, body=b""):
        def fake_run(*_a, **_kw):
            return subprocess.CompletedProcess([], returncode, stdout=body, stderr=b"")
        monkeypatch.setattr(scraper.subprocess, "run", fake_run)

    def test_a_required_fetch_raises_on_an_http_error(self, monkeypatch):
        # curl -f exits 22 on 404. The old code returned the 404 PAGE, which
        # parsed as a sitemap with no URLs.
        self._curl(monkeypatch, 22)
        with pytest.raises(RuntimeError):
            scraper._fetch("https://example.com/gone.xml", required=True)

    def test_a_page_fetch_stays_tolerant(self, monkeypatch):
        # Title fetches meet Cloudflare routinely; the caller falls back to
        # the slug, so an empty string is the right answer, not an exception.
        self._curl(monkeypatch, 22)
        assert scraper._fetch("https://example.com/blocked") == ""

    def test_a_good_fetch_returns_the_body(self, monkeypatch):
        self._curl(monkeypatch, 0, b"<loc>ok</loc>")
        assert scraper._fetch("https://example.com/x", required=True) == "<loc>ok</loc>"


class TestPruning:
    """A row the site no longer lists must not stay a link candidate.

    The upsert never deleted, so after the sitemap migration 94 rows were
    stranded and two of them 404 — pages the internal-link menu could still
    have offered an article.
    """

    KEPT = "https://www.rededucation.com/kept-by-the-sitemap/"
    GONE = "https://www.rededucation.com/dropped-from-the-sitemap/"

    @pytest.fixture
    def rows(self):
        """Two rows to prune between, and a net under the whole table.

        main() prunes everything absent from the sitemap set, and this test
        hands it a set of one. Any row another test owns would go with it, so
        snapshot the table first and put back whatever the run removed. The
        suite passes without this only because the seeded database ships no
        published posts — which is luck, not a guarantee.
        """
        from config.db import PublishedPost, get_session

        with get_session() as s:
            before = [(r.url, r.title, r.kind, r.published_at)
                      for r in s.query(PublishedPost).all()]
            for url in (self.KEPT, self.GONE):
                s.add(PublishedPost(url=url, title="t", kind="post"))
            s.commit()
        yield
        with get_session() as s:
            for url in (self.KEPT, self.GONE):
                row = s.query(PublishedPost).filter_by(url=url).first()
                if row is not None:
                    s.delete(row)
            s.commit()
        with get_session() as s:
            have = {r.url for r in s.query(PublishedPost).all()}
            for url, title, kind, published_at in before:
                if url not in have:
                    s.add(PublishedPost(url=url, title=title, kind=kind,
                                        published_at=published_at))
            s.commit()

    def _run(self, monkeypatch, argv):
        monkeypatch.setattr(scraper, "_load_acronyms", lambda: None)
        monkeypatch.setattr(scraper, "_list_urls",
                            lambda only: [(self.KEPT, None, "post", "Kept")])
        monkeypatch.setattr("sys.argv", ["scrape_published_posts.py"] + argv)
        scraper.main()

    def _urls(self):
        from config.db import PublishedPost, get_session

        with get_session() as s:
            return {r.url for r in s.query(PublishedPost).all()}

    def test_a_full_scrape_drops_what_the_sitemaps_no_longer_list(
            self, monkeypatch, rows):
        self._run(monkeypatch, ["--titles", "slug"])
        urls = self._urls()
        assert self.GONE not in urls
        assert self.KEPT in urls

    def test_a_filtered_scrape_never_prunes(self, monkeypatch, rows):
        # --kinds post carries no course URLs; pruning against that set would
        # delete every course in the table.
        self._run(monkeypatch, ["--titles", "slug", "--kinds", "post"])
        assert self.GONE in self._urls()

    def test_no_prune_keeps_everything(self, monkeypatch, rows):
        self._run(monkeypatch, ["--titles", "slug", "--no-prune"])
        assert self.GONE in self._urls()


class TestAnEmptyRunChangesNothing:
    def test_it_exits_non_zero_without_stamping(self, monkeypatch):
        from config.db import AppConfig, get_session

        with get_session() as s:
            cfg = s.get(AppConfig, 1)
            before = cfg.coverage_scraped_at if cfg else None

        monkeypatch.setattr(scraper, "_list_urls", lambda only: [])
        monkeypatch.setattr(scraper, "_load_acronyms", lambda: None)
        monkeypatch.setattr("sys.argv", ["scrape_published_posts.py"])

        with pytest.raises(SystemExit):
            scraper.main()

        with get_session() as s:
            cfg = s.get(AppConfig, 1)
            assert (cfg.coverage_scraped_at if cfg else None) == before
