"""Fetcher is the load-bearing architectural test of this repo: compliance
with robots.txt is enforced in code, before any network backend is ever
consulted — not requested of an LLM in a prompt. Keep these tests findable
by filename.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nimbus.config import Settings
from nimbus.fetch.backends import RawResponse
from nimbus.fetch.cache import HtmlCache
from nimbus.fetch.fetcher import Fetcher, FetchError, FetchStatusError, RobotsDisallowed

FIXTURES = Path(__file__).parent / "fixtures" / "robots"
HENSTOCK = (FIXTURES / "henstock.txt").read_text()


class _RaisingBackend:
    """A backend that fails the test loudly if ever called."""

    def __init__(self):
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


async def test_fetcher_raises_on_disallowed_url(tmp_path, settings):
    cache = HtmlCache(root=tmp_path)
    cache.put("https://www.henstockps.co.uk/robots.txt", 200, "text/plain", HENSTOCK)
    backend = _RaisingBackend()
    fetcher = Fetcher(settings, cache, backend)

    with pytest.raises(RobotsDisallowed):
        await fetcher.get("https://www.henstockps.co.uk/property/bedrooms/3")

    assert backend.call_count == 0


async def test_fetcher_serves_cache_without_network(tmp_path, settings):
    cache = HtmlCache(root=tmp_path)
    cache.put("https://www.henstockps.co.uk/robots.txt", 200, "text/plain", HENSTOCK)
    cache.put(
        "https://www.henstockps.co.uk/property/123",
        200,
        "text/html",
        "<html>listing</html>",
    )
    backend = _RaisingBackend()
    fetcher = Fetcher(settings, cache, backend)

    page = await fetcher.get("https://www.henstockps.co.uk/property/123")

    assert page.body == "<html>listing</html>"
    assert backend.call_count == 0


async def test_fetcher_fetches_via_backend_on_cache_miss_and_caches_result(tmp_path, settings):
    cache = HtmlCache(root=tmp_path)
    cache.put(
        "https://www.example.co.uk/robots.txt", 200, "text/plain", "User-agent: *\nAllow: /\n"
    )

    class _StubBackend:
        def __init__(self):
            self.calls = []

        async def fetch(self, url: str, *, timeout: float) -> RawResponse:
            self.calls.append(url)
            return RawResponse(status=200, content_type="text/html", body="<html>new</html>")

    backend = _StubBackend()
    fetcher = Fetcher(settings, cache, backend)

    page = await fetcher.get("https://www.example.co.uk/property/1")

    assert page.body == "<html>new</html>"
    assert backend.calls == ["https://www.example.co.uk/property/1"]

    # Second call is served from cache, not the backend.
    page2 = await fetcher.get("https://www.example.co.uk/property/1")
    assert page2.body == "<html>new</html>"
    assert backend.calls == ["https://www.example.co.uk/property/1"]


async def test_fetcher_wraps_backend_failure_as_fetch_error(tmp_path, settings):
    """A transport-level failure (timeout, too-many-redirects, a backend
    proxy error, ...) must come out of `Fetcher.get()` as `FetchError`
    regardless of which exception the configured backend itself raised --
    that's what lets a caller like the nightly crawler catch one thing
    without caring whether httpx or Crawlbase is underneath."""
    cache = HtmlCache(root=tmp_path)
    cache.put(
        "https://www.example.co.uk/robots.txt", 200, "text/plain", "User-agent: *\nAllow: /\n"
    )

    class _ExplodingBackend:
        async def fetch(self, url: str, *, timeout: float) -> RawResponse:
            raise RuntimeError("connection reset by peer")

    fetcher = Fetcher(settings, cache, _ExplodingBackend())

    with pytest.raises(FetchError) as exc_info:
        await fetcher.get("https://www.example.co.uk/property/1")

    assert exc_info.value.url == "https://www.example.co.uk/property/1"
    assert isinstance(exc_info.value.cause, RuntimeError)


async def test_fetcher_robots_for_applies_configured_default_when_block_omits_delay(tmp_path):
    """A robots.txt that exists and matches a block with no Crawl-delay must
    yield Settings.default_crawl_delay, not RobotsPolicy's own literal 1.0 —
    a configured default that silently fails to apply here is a politeness
    bug against exactly the small, fragile sites most likely to publish a
    robots.txt without a delay."""
    settings = Settings(
        anthropic_api_key="",
        cache_dir=tmp_path / "cache",
        db_path=tmp_path / "nimbus.db",
        user_agent="NimbusBot/0.1 (+https://github.com/ncellepola/nimbus-listing-agent)",
        default_crawl_delay=3.5,
    )
    cache = HtmlCache(root=tmp_path)
    cache.put(
        "https://www.quietagent.co.uk/robots.txt",
        200,
        "text/plain",
        "User-agent: *\nDisallow: /private/\n",
    )
    fetcher = Fetcher(settings, cache, _RaisingBackend())

    policy = await fetcher.robots_for("www.quietagent.co.uk")

    assert policy.crawl_delay == 3.5


# --------------------------------------------------------------------------
# robots.txt fallback branches. RFC 9309 §2.3.1.3/§2.3.1.4 draw a line this
# code previously did not: a robots.txt that is *unavailable* (404/410 — the
# site has none) permits everything, while one that is *unreachable* (a
# transport error, a 5xx) requires assuming a complete disallow. Treating
# the two alike meant one transient 503 permanently flipped a domain to
# allow-all, because the 503 body was cached and the cache never expires.
# --------------------------------------------------------------------------


class _StatusBackend:
    """A backend that answers every request with one fixed status/body."""

    def __init__(self, status: int, body: str = "") -> None:
        self.status = status
        self.body = body
        self.calls: list[str] = []

    async def fetch(self, url: str, *, timeout: float) -> RawResponse:
        self.calls.append(url)
        return RawResponse(status=self.status, content_type="text/plain", body=self.body)


async def test_robots_for_denies_everything_when_the_fetch_raises(tmp_path, settings, caplog):
    class _ExplodingBackend:
        async def fetch(self, url: str, *, timeout: float) -> RawResponse:
            raise RuntimeError("connection reset by peer")

    fetcher = Fetcher(settings, HtmlCache(root=tmp_path), _ExplodingBackend())

    with caplog.at_level("WARNING"):
        policy = await fetcher.robots_for("www.example.co.uk")

    assert policy.can_fetch("https://www.example.co.uk/anything") is False
    assert any(
        "www.example.co.uk" in record.message and record.levelname == "WARNING"
        for record in caplog.records
    )


async def test_robots_for_denies_everything_on_a_server_error(tmp_path, settings, caplog):
    backend = _StatusBackend(503, "<html>temporarily unavailable</html>")
    fetcher = Fetcher(settings, HtmlCache(root=tmp_path), backend)

    with caplog.at_level("WARNING"):
        policy = await fetcher.robots_for("www.example.co.uk")

    assert policy.can_fetch("https://www.example.co.uk/property/1") is False
    assert any(
        "503" in record.message and record.levelname == "WARNING" for record in caplog.records
    )


async def test_a_503_robots_response_is_never_cached(tmp_path, settings):
    """The defect this guards: `cache.put` stored whatever the backend
    returned, including a 503, and the cache never expires -- so a single
    transient failure flipped a domain to allow-all permanently, with no
    retry possible. A non-200 robots response must leave the cache empty so
    the very next run asks again."""
    cache = HtmlCache(root=tmp_path)
    backend = _StatusBackend(503)
    fetcher = Fetcher(settings, cache, backend)

    await fetcher.robots_for("www.example.co.uk")

    assert cache.get("https://www.example.co.uk/robots.txt") is None

    # ... and the next call really does re-ask the backend rather than
    # reading a poisoned cache entry.
    await fetcher.robots_for("www.example.co.uk")
    assert len(backend.calls) == 2


async def test_robots_for_denies_everything_on_a_cdn_403(tmp_path, settings):
    """A 403 is RFC 9309's "unavailable" class, but in practice it is a CDN
    actively blocking us rather than a site declaring it has no robots.txt.
    Reading an active block as blanket permission is the wrong default."""
    fetcher = Fetcher(settings, HtmlCache(root=tmp_path), _StatusBackend(403))

    policy = await fetcher.robots_for("www.example.co.uk")

    assert policy.can_fetch("https://www.example.co.uk/property/1") is False


async def test_robots_for_allows_all_on_404(tmp_path, settings, caplog):
    cache = HtmlCache(root=tmp_path)
    fetcher = Fetcher(settings, cache, _StatusBackend(404, "not found"))

    with caplog.at_level("WARNING"):
        policy = await fetcher.robots_for("www.example.co.uk")

    assert policy.can_fetch("https://www.example.co.uk/property/1") is True
    assert policy.crawl_delay == settings.default_crawl_delay
    assert cache.get("https://www.example.co.uk/robots.txt") is None
    assert any("404" in record.message for record in caplog.records)


async def test_robots_for_allows_all_on_an_empty_200_body(tmp_path, settings, caplog):
    fetcher = Fetcher(settings, HtmlCache(root=tmp_path), _StatusBackend(200, "   \n  "))

    with caplog.at_level("WARNING"):
        policy = await fetcher.robots_for("www.example.co.uk")

    assert policy.can_fetch("https://www.example.co.uk/property/1") is True
    assert any("empty" in record.message for record in caplog.records)


async def test_robots_for_parses_a_real_200_response(tmp_path, settings):
    fetcher = Fetcher(settings, HtmlCache(root=tmp_path), _StatusBackend(200, HENSTOCK))

    policy = await fetcher.robots_for("www.henstockps.co.uk")

    assert policy.can_fetch("https://www.henstockps.co.uk/property/bedrooms/3") is False
    assert policy.can_fetch("https://www.henstockps.co.uk/property/123") is True


# --------------------------------------------------------------------------
# The same status blindness on the listing path: a 404 or 500 body was
# cached permanently and handed to the extractor as if it were page content.
# --------------------------------------------------------------------------


async def test_get_raises_on_a_non_2xx_response_and_caches_nothing(tmp_path, settings):
    cache = HtmlCache(root=tmp_path)
    cache.put(
        "https://www.example.co.uk/robots.txt", 200, "text/plain", "User-agent: *\nAllow: /\n"
    )

    class _NotFoundBackend:
        async def fetch(self, url: str, *, timeout: float) -> RawResponse:
            return RawResponse(status=404, content_type="text/html", body="<html>gone</html>")

    fetcher = Fetcher(settings, cache, _NotFoundBackend())

    with pytest.raises(FetchStatusError) as exc_info:
        await fetcher.get("https://www.example.co.uk/property/1")

    assert exc_info.value.status == 404
    assert cache.get("https://www.example.co.uk/property/1") is None
    # A caller that only knows about FetchError still catches it, so one
    # dead listing page skips rather than aborting a whole site's crawl.
    assert isinstance(exc_info.value, FetchError)


async def test_get_rejects_a_stale_non_2xx_cache_entry(tmp_path, settings):
    """Entries written before non-2xx caching was removed must not be
    served to the extractor as page content either."""
    cache = HtmlCache(root=tmp_path)
    cache.put(
        "https://www.example.co.uk/robots.txt", 200, "text/plain", "User-agent: *\nAllow: /\n"
    )
    cache.put("https://www.example.co.uk/property/1", 500, "text/html", "<html>oops</html>")
    fetcher = Fetcher(settings, cache, _RaisingBackend())

    with pytest.raises(FetchStatusError):
        await fetcher.get("https://www.example.co.uk/property/1")


async def test_get_refetches_when_the_cached_page_is_older_than_settings_max_age(tmp_path):
    """The nightly delta can only fire if the nightly crawl actually
    re-reads the site. Without a staleness bound, `Fetcher.get()` served
    yesterday's cache entry forever and every page was `unchanged`."""
    settings = Settings(
        anthropic_api_key="",
        cache_dir=tmp_path / "cache",
        db_path=tmp_path / "nimbus.db",
        user_agent="NimbusBot/0.1 (+https://github.com/ncellepola/nimbus-listing-agent)",
        cache_max_age=0.0,  # everything already on disk is stale
    )
    cache = HtmlCache(root=tmp_path)
    cache.put(
        "https://www.example.co.uk/robots.txt", 200, "text/plain", "User-agent: *\nAllow: /\n"
    )
    cache.put("https://www.example.co.uk/property/1", 200, "text/html", "<html>yesterday</html>")

    backend = _StatusBackend(200, "<html>today</html>")
    backend.content_type = "text/html"
    fetcher = Fetcher(settings, cache, backend)

    page = await fetcher.get("https://www.example.co.uk/property/1")

    assert page.body == "<html>today</html>"
