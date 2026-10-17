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
from nimbus.fetch.fetcher import Fetcher, FetchError, RobotsDisallowed

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
