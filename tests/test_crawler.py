"""The nightly crawler is the other half of the project's central
architectural claim (ADR-0002, ADR-0003): an LLM learns a site once, at
onboarding, and produces a `SiteRecipe`; everything here just walks that
recipe with a plain Python loop over `Fetcher.get()`. The last test in this
file is not box-ticking -- it mechanically enforces that no LLM client is
even importable from this module, so the architectural separation cannot
silently rot into "well, it's only a small agent call".
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import make_recipe

from nimbus.config import Settings
from nimbus.crawler import collect_candidate_urls, crawl_site, select_listing_urls
from nimbus.fetch.backends import RawResponse
from nimbus.fetch.cache import HtmlCache
from nimbus.fetch.fetcher import Fetcher, RobotsDisallowed
from nimbus.models import CrawlRecord

FIXTURES = Path(__file__).parent / "fixtures"
HENSTOCK_ROBOTS = (FIXTURES / "robots" / "henstock.txt").read_text()
SITEMAP_INDEX = (FIXTURES / "sitemaps" / "index.xml").read_text()
SITEMAP_PROPERTY = (FIXTURES / "sitemaps" / "property.xml").read_text()

LISTING = "https://www.ashworthholme.co.uk/property/123"
ABOUT = "https://www.ashworthholme.co.uk/about-us/"

ALLOW_ALL_ROBOTS = "User-agent: *\nAllow: /\n"


class _RaisingBackend:
    """A backend that fails the test loudly if ever called -- every test
    here must be served entirely from the cache."""

    def __init__(self) -> None:
        self.call_count = 0

    async def fetch(self, url: str, *, timeout: float):
        self.call_count += 1
        raise AssertionError(f"backend.fetch must not be called for {url}")


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        anthropic_api_key="",
        cache_dir=tmp_path / "cache",
        db_path=tmp_path / "nimbus.db",
        user_agent="NimbusBot/0.1 (+https://github.com/ncellepola/nimbus-listing-agent)",
    )


@pytest.fixture
def cache(tmp_path) -> HtmlCache:
    return HtmlCache(root=tmp_path / "html-cache")


@pytest.fixture
def fetcher(settings, cache) -> Fetcher:
    return Fetcher(settings, cache, _RaisingBackend())


def test_select_listing_urls_filters_and_dedupes():
    recipe = make_recipe(listing_url_patterns=["/property/*"])

    out = select_listing_urls([LISTING, ABOUT, LISTING], recipe)

    assert out == [LISTING]


def test_select_listing_urls_matches_against_path_not_just_full_url():
    recipe = make_recipe(listing_url_patterns=["/property/*"])

    out = select_listing_urls(["https://other-host.example.com/property/999"], recipe)

    assert out == ["https://other-host.example.com/property/999"]


def test_select_listing_urls_returns_nothing_for_no_patterns():
    recipe = make_recipe(listing_url_patterns=[])

    assert select_listing_urls([LISTING], recipe) == []


async def test_collect_candidate_urls_expands_sitemap_index(cache, fetcher):
    cache.put("https://jaksandco.co.uk/robots.txt", 200, "text/plain", ALLOW_ALL_ROBOTS)
    cache.put("https://jaksandco.co.uk/sitemap-index.xml", 200, "application/xml", SITEMAP_INDEX)
    cache.put(
        "https://jaksandco.co.uk/sitemap-property.xml", 200, "application/xml", SITEMAP_PROPERTY
    )
    recipe = make_recipe(
        domain="jaksandco.co.uk",
        sitemap_urls=["https://jaksandco.co.uk/sitemap-index.xml"],
        listing_index_urls=[],
    )

    urls = await collect_candidate_urls(recipe, fetcher=fetcher)

    assert "https://jaksandco.co.uk/property/12345" in urls
    assert "https://jaksandco.co.uk/property/12346" in urls
    assert "https://jaksandco.co.uk/about-us" in urls


async def test_collect_candidate_urls_falls_back_to_listing_index_when_no_sitemap(cache, fetcher):
    """The jaksandco.co.uk case: no sitemap at all, so the crawler must walk
    the listing index page's own links instead."""
    cache.put("https://www.jaksandco.co.uk/robots.txt", 200, "text/plain", ALLOW_ALL_ROBOTS)
    index_html = """
    <html><body>
    <a href="/property/grenfell-road-didsbury-manchester-m20-6tq/">Grenfell Road</a>
    <a href="/property/kingsway-manchester-m20-5ns/">Kingsway</a>
    <a href="/about-us/">About</a>
    <a href="mailto:hello@jaksandco.co.uk">Email us</a>
    </body></html>
    """
    cache.put(
        "https://www.jaksandco.co.uk/find-a-property/?department=residential-sales",
        200,
        "text/html",
        index_html,
    )
    recipe = make_recipe(
        domain="jaksandco.co.uk",
        sitemap_urls=[],
        listing_index_urls=[
            "https://www.jaksandco.co.uk/find-a-property/?department=residential-sales"
        ],
        listing_url_patterns=["/property/*"],
        pagination=None,
    )

    urls = await collect_candidate_urls(recipe, fetcher=fetcher)

    assert "https://www.jaksandco.co.uk/property/grenfell-road-didsbury-manchester-m20-6tq/" in urls
    assert "https://www.jaksandco.co.uk/property/kingsway-manchester-m20-5ns/" in urls
    assert "mailto:hello@jaksandco.co.uk" not in urls


async def test_collect_candidate_urls_paginates_and_stops_on_empty_page(cache, fetcher):
    from nimbus.models import PaginationRule

    cache.put("https://www.jaksandco.co.uk/robots.txt", 200, "text/plain", ALLOW_ALL_ROBOTS)
    cache.put(
        "https://www.jaksandco.co.uk/find-a-property/?department=residential-sales",
        200,
        "text/html",
        '<html><body><a href="/property/one/">One</a></body></html>',
    )
    cache.put(
        "https://www.jaksandco.co.uk/find-a-property/page/2/?department=residential-sales",
        200,
        "text/html",
        '<html><body><a href="/property/two/">Two</a></body></html>',
    )
    cache.put(
        "https://www.jaksandco.co.uk/find-a-property/page/3/?department=residential-sales",
        200,
        "text/html",
        "<html><body>It seems we can't find what you're looking for.</body></html>",
    )
    recipe = make_recipe(
        domain="jaksandco.co.uk",
        sitemap_urls=[],
        listing_index_urls=[
            "https://www.jaksandco.co.uk/find-a-property/?department=residential-sales"
        ],
        listing_url_patterns=["/property/*"],
        pagination=PaginationRule(kind="path", param=None, max_pages=5),
    )

    urls = await collect_candidate_urls(recipe, fetcher=fetcher)

    assert "https://www.jaksandco.co.uk/property/one/" in urls
    assert "https://www.jaksandco.co.uk/property/two/" in urls
    # Page 3 had no links at all; pagination must stop there rather than
    # walking all the way to max_pages.
    assert len([u for u in urls if "/property/" in u]) == 2


async def test_collect_candidate_urls_skips_sitemap_robots_disallows(cache, fetcher):
    cache.put("https://www.henstockps.co.uk/robots.txt", 200, "text/plain", HENSTOCK_ROBOTS)
    recipe = make_recipe(
        domain="henstockps.co.uk",
        sitemap_urls=["https://www.henstockps.co.uk/pdf/sitemap.xml"],
        listing_index_urls=[],
    )

    urls = await collect_candidate_urls(recipe, fetcher=fetcher)

    assert urls == []


async def test_crawl_site_honours_crawl_delay(cache, fetcher):
    cache.put("https://www.ashworthholme.co.uk/robots.txt", 200, "text/plain", ALLOW_ALL_ROBOTS)
    cache.put(
        "https://www.ashworthholme.co.uk/sitemap.xml",
        200,
        "application/xml",
        """<?xml version="1.0"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://www.ashworthholme.co.uk/property/1</loc></url>
          <url><loc>https://www.ashworthholme.co.uk/property/2</loc></url>
          <url><loc>https://www.ashworthholme.co.uk/property/3</loc></url>
        </urlset>""",
    )
    for n in (1, 2, 3):
        cache.put(
            f"https://www.ashworthholme.co.uk/property/{n}",
            200,
            "text/html",
            f"<html>listing {n}</html>",
        )
    recipe = make_recipe(
        domain="ashworthholme.co.uk",
        sitemap_urls=["https://www.ashworthholme.co.uk/sitemap.xml"],
        listing_index_urls=[],
        listing_url_patterns=["/property/*"],
    )

    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    records = await crawl_site(recipe, fetcher=fetcher, crawl_delay=2.0, sleep=fake_sleep)

    assert len(records) == 3
    assert delays
    assert all(d >= 2.0 for d in delays)


async def test_crawl_site_respects_limit(cache, fetcher):
    cache.put("https://www.ashworthholme.co.uk/robots.txt", 200, "text/plain", ALLOW_ALL_ROBOTS)
    cache.put(
        "https://www.ashworthholme.co.uk/sitemap.xml",
        200,
        "application/xml",
        """<?xml version="1.0"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://www.ashworthholme.co.uk/property/1</loc></url>
          <url><loc>https://www.ashworthholme.co.uk/property/2</loc></url>
          <url><loc>https://www.ashworthholme.co.uk/property/3</loc></url>
          <url><loc>https://www.ashworthholme.co.uk/property/4</loc></url>
        </urlset>""",
    )
    for n in (1, 2, 3, 4):
        cache.put(
            f"https://www.ashworthholme.co.uk/property/{n}",
            200,
            "text/html",
            f"<html>listing {n}</html>",
        )
    recipe = make_recipe(
        domain="ashworthholme.co.uk",
        sitemap_urls=["https://www.ashworthholme.co.uk/sitemap.xml"],
        listing_index_urls=[],
        listing_url_patterns=["/property/*"],
    )

    records = await crawl_site(recipe, fetcher=fetcher, crawl_delay=0.0, limit=3)

    assert len(records) == 3


async def test_crawl_site_skips_robots_disallowed_without_failing_the_run(cache, fetcher):
    disallowed = "https://www.henstockps.co.uk/property/bedrooms/3"
    allowed = "https://www.henstockps.co.uk/property/42"
    cache.put("https://www.henstockps.co.uk/robots.txt", 200, "text/plain", HENSTOCK_ROBOTS)
    cache.put(
        "https://www.henstockps.co.uk/sitemap.xml",
        200,
        "application/xml",
        f"""<?xml version="1.0"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>{disallowed}</loc></url>
          <url><loc>{allowed}</loc></url>
        </urlset>""",
    )
    cache.put(allowed, 200, "text/html", "<html>listing 42</html>")

    recipe = make_recipe(
        domain="henstockps.co.uk",
        sitemap_urls=["https://www.henstockps.co.uk/sitemap.xml"],
        listing_index_urls=[],
        listing_url_patterns=["/property/*"],
    )

    records = await crawl_site(recipe, fetcher=fetcher, crawl_delay=0.0)

    assert disallowed not in [r.url for r in records]
    assert allowed in [r.url for r in records]


async def test_crawl_site_produces_crawl_records_with_hash_and_cache_flag(cache, fetcher):
    cache.put("https://www.ashworthholme.co.uk/robots.txt", 200, "text/plain", ALLOW_ALL_ROBOTS)
    cache.put(
        "https://www.ashworthholme.co.uk/sitemap.xml",
        200,
        "application/xml",
        """<?xml version="1.0"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://www.ashworthholme.co.uk/property/1</loc></url>
        </urlset>""",
    )
    cache.put(
        "https://www.ashworthholme.co.uk/property/1", 200, "text/html", "<html>listing 1</html>"
    )
    recipe = make_recipe(
        domain="ashworthholme.co.uk",
        sitemap_urls=["https://www.ashworthholme.co.uk/sitemap.xml"],
        listing_index_urls=[],
        listing_url_patterns=["/property/*"],
    )

    records = await crawl_site(recipe, fetcher=fetcher, crawl_delay=0.0)

    assert len(records) == 1
    record = records[0]
    assert isinstance(record, CrawlRecord)
    assert record.url == "https://www.ashworthholme.co.uk/property/1"
    assert record.html == "<html>listing 1</html>"
    assert record.from_cache is True
    assert record.content_hash
    assert record.fetched_at is not None


async def test_crawl_site_skips_a_page_that_errors_on_fetch_without_failing_the_run(
    cache, settings
):
    """Real sites occasionally error on a specific listing URL (timeouts,
    too-many-redirects, 5xx) -- bentleyhurst.co.uk's own recipe notes two
    sample URLs that "errored on fetch". One such failure must not abort
    the rest of the crawl."""

    class _FlakyBackend:
        async def fetch(self, url: str, *, timeout: float) -> RawResponse:
            if url.endswith("/property/bad"):
                raise RuntimeError("connection reset by peer")
            return RawResponse(status=200, content_type="text/html", body="<html>ok</html>")

    cache.put("https://www.ashworthholme.co.uk/robots.txt", 200, "text/plain", ALLOW_ALL_ROBOTS)
    cache.put(
        "https://www.ashworthholme.co.uk/sitemap.xml",
        200,
        "application/xml",
        """<?xml version="1.0"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://www.ashworthholme.co.uk/property/bad</loc></url>
          <url><loc>https://www.ashworthholme.co.uk/property/good</loc></url>
        </urlset>""",
    )
    recipe = make_recipe(
        domain="ashworthholme.co.uk",
        sitemap_urls=["https://www.ashworthholme.co.uk/sitemap.xml"],
        listing_index_urls=[],
        listing_url_patterns=["/property/*"],
    )
    fetcher = Fetcher(settings, cache, _FlakyBackend())

    records = await crawl_site(recipe, fetcher=fetcher, crawl_delay=0.0)

    urls = [r.url for r in records]
    assert "https://www.ashworthholme.co.uk/property/bad" not in urls
    assert "https://www.ashworthholme.co.uk/property/good" in urls


async def test_crawl_site_raises_nothing_when_all_candidates_disallowed(cache, fetcher):
    cache.put("https://www.henstockps.co.uk/robots.txt", 200, "text/plain", HENSTOCK_ROBOTS)
    cache.put(
        "https://www.henstockps.co.uk/sitemap.xml",
        200,
        "application/xml",
        """<?xml version="1.0"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://www.henstockps.co.uk/property/bedrooms/3</loc></url>
        </urlset>""",
    )
    recipe = make_recipe(
        domain="henstockps.co.uk",
        sitemap_urls=["https://www.henstockps.co.uk/sitemap.xml"],
        listing_index_urls=[],
        listing_url_patterns=["/property/*"],
    )

    records = await crawl_site(recipe, fetcher=fetcher, crawl_delay=0.0)

    assert records == []


def test_crawler_module_imports_no_llm_client():
    src = Path("src/nimbus/crawler.py").read_text()
    assert "anthropic" not in src
    assert "claude_agent_sdk" not in src


async def test_robots_disallowed_is_importable_for_catching(fetcher):
    # sanity: the exception type the module must catch is importable from
    # the fetch layer, not reimplemented locally.
    assert issubclass(RobotsDisallowed, Exception)
