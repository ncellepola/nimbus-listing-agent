"""The three tools in `nimbus.mcp_server` are the discovery agent's only
route to the web (Task 3 gets these and nothing else). `fetch_page` is the
enforcement point for the project's central architectural claim: robots.txt
compliance is enforced in code the agent must call, never requested in a
prompt. The first test below is the single most important test in this
repository: it proves the robots gate runs *before* any backend is ever
touched, by using a backend that fails the test loudly if called at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nimbus.config import Settings
from nimbus.fetch.cache import HtmlCache
from nimbus.fetch.fetcher import Fetcher
from nimbus.mcp_server import build_tools

FIXTURES = Path(__file__).parent / "fixtures"
HENSTOCK_ROBOTS = (FIXTURES / "robots" / "henstock.txt").read_text()
SITEMAP_INDEX = (FIXTURES / "sitemaps" / "index.xml").read_text()
SITEMAP_PROPERTY = (FIXTURES / "sitemaps" / "property.xml").read_text()

CACHED_URL = "https://www.ashworthholme.co.uk/property/123"

_CACHED_HTML = """
<html>
<head>
<style>
body { color: red; font-family: Arial, sans-serif; margin: 0; padding: 0; }
.hero { display: none; }
.listing-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; }
.listing-card { border: 1px solid #ccc; padding: 1rem; border-radius: 4px; }
.navbar { background: #003366; color: white; padding: 0.5rem 1rem; }
.footer-links a { color: #ccc; text-decoration: none; margin-right: 1rem; }
</style>
<script>
window.dataLayer = window.dataLayer || [];
function gtag() { dataLayer.push(arguments); }
gtag('js', new Date());
gtag('config', 'UA-FAKE-00000-1');
var tracking = {
  fire: function () {
    return fetch('/collect', { method: 'POST', body: JSON.stringify({ event: 'pageview' }) });
  },
  retarget: function () {
    return { pixel: 'noise', vendor: 'adtech', session: Math.random().toString(36) };
  }
};
tracking.fire();
tracking.retarget();
</script>
</head>
<body>
<nav class="navbar">
<a href="/">Home</a><a href="/properties">Properties</a><a href="/sales">Sales</a>
<a href="/lettings">Lettings</a><a href="/valuations">Valuations</a><a href="/contact">Contact</a>
<a href="/about">About Us</a><a href="/careers">Careers</a><a href="/blog">Blog</a>
</nav>
<header><h1>Ashworth Holme Estate Agents</h1></header>
<main class="listing-grid">
<div class="listing-card">
<h2>3 Bedroom Terraced House, Manchester</h2>
<p class="listing-paragraph" data-test="desc1">A beautifully presented three bedroom
terraced house situated in a quiet residential street within easy reach of local
amenities, schools, and transport links. The property briefly comprises an entrance
hallway, a spacious lounge with bay window, a modern fitted kitchen diner, three
well-proportioned bedrooms, and a family bathroom. Outside there is a low-maintenance
rear garden and on-street parking to the front.</p>
<p class="listing-paragraph" data-test="desc2">The accommodation is arranged over two
floors and benefits from gas central heating and double glazing throughout. Viewing is
highly recommended to appreciate the space and location on offer. Early interest is
expected given the strong demand for family homes of this type in this sought-after
part of the city.</p>
<p class="listing-paragraph" data-test="desc3">Further details on the local area,
schools catchment, and transport links are available on request from our Manchester
office, which is open seven days a week for appointments and viewings. Please contact
the branch directly to arrange a convenient time to view this lovely family home
before it is withdrawn from the market.</p>
</div>
</main>
<footer class="footer-links">
<a href="/">Home</a><a href="/properties">Properties</a><a href="/sales">Sales</a>
<a href="/lettings">Lettings</a><a href="/valuations">Valuations</a><a href="/contact">Contact</a>
<a href="/privacy">Privacy Policy</a><a href="/terms">Terms of Use</a><a href="/cookies">Cookies</a>
<p>Ashworth Holme (c) 2026. All rights reserved. Registered in England and Wales.</p>
</footer>
</body>
</html>
"""


class _RaisingBackend:
    """A backend that fails the test loudly if ever called."""

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
def backend() -> _RaisingBackend:
    return _RaisingBackend()


@pytest.fixture
def cache(tmp_path, backend, settings) -> HtmlCache:
    html_cache = HtmlCache(root=tmp_path / "html-cache")

    # Henstock: robots.txt disallows */bedrooms/ for everyone.
    html_cache.put("https://www.henstockps.co.uk/robots.txt", 200, "text/plain", HENSTOCK_ROBOTS)

    # Ashworth Holme: allow-all robots.txt, one cached listing page.
    html_cache.put(
        "https://www.ashworthholme.co.uk/robots.txt",
        200,
        "text/plain",
        "User-agent: *\nAllow: /\n",
    )
    html_cache.put(CACHED_URL, 200, "text/html", _CACHED_HTML)

    # Jaks & Co: allow-all robots.txt pointing at a sitemap index that
    # expands, one level deep, into a urlset with property URLs.
    html_cache.put(
        "https://jaksandco.co.uk/robots.txt",
        200,
        "text/plain",
        "User-agent: *\nAllow: /\nSitemap: https://jaksandco.co.uk/sitemap-index.xml\n",
    )
    html_cache.put(
        "https://jaksandco.co.uk/sitemap-index.xml",
        200,
        "application/xml",
        SITEMAP_INDEX,
    )
    html_cache.put(
        "https://jaksandco.co.uk/sitemap-property.xml",
        200,
        "application/xml",
        SITEMAP_PROPERTY,
    )

    return html_cache


@pytest.fixture
def fetcher(settings, cache, backend) -> Fetcher:
    return Fetcher(settings, cache, backend)


@pytest.fixture
def tools(fetcher):
    return build_tools(fetcher)


@pytest.fixture
def fetch_page(tools):
    return tools["fetch_page"]


@pytest.fixture
def get_sitemap(tools):
    return tools["get_sitemap"]


@pytest.fixture
def get_robots(tools):
    return tools["get_robots"]


async def test_fetch_page_refuses_disallowed_url_without_calling_backend(fetch_page, backend):
    out = await fetch_page("https://www.henstockps.co.uk/property/bedrooms/3")

    assert out["error"] == "robots_disallowed"
    assert backend.call_count == 0


async def test_fetch_page_served_from_cache_reports_from_cache(fetch_page):
    out = await fetch_page(CACHED_URL)

    assert out["from_cache"] is True


async def test_get_sitemap_expands_sitemap_index(get_sitemap):
    out = await get_sitemap("jaksandco.co.uk")

    assert out["url_count"] > 0
    assert any("/property" in u for u in out["sample_urls"])


async def test_fetch_page_markdown_is_smaller_than_html(fetch_page):
    md = await fetch_page(CACHED_URL, as_markdown=True)
    html = await fetch_page(CACHED_URL, as_markdown=False)

    assert len(md["content"]) < len(html["content"]) / 2


async def test_fetch_page_truncates_at_max_chars(fetch_page):
    out = await fetch_page(CACHED_URL, max_chars=500)

    assert out["truncated"] is True
    assert len(out["content"]) <= 500


async def test_get_robots_reports_policy_fields(get_robots):
    out = await get_robots("www.henstockps.co.uk")

    assert out["domain"] == "www.henstockps.co.uk"
    assert "Disallow" in out["text"]
    assert out["crawl_delay"] == 2
    assert out["sitemaps"] == ["https://www.henstockps.co.uk/sitemap.xml"]
    assert out["fetchable"] is True
