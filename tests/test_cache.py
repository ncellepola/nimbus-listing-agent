from __future__ import annotations

from pathlib import Path

import pytest

from nimbus.config import Settings
from nimbus.fetch.backends import RawResponse
from nimbus.fetch.cache import HtmlCache
from nimbus.fetch.fetcher import Fetcher, RobotsDisallowed

FIXTURES = Path(__file__).parent / "fixtures" / "robots"
HENSTOCK = (FIXTURES / "henstock.txt").read_text()


def test_cache_key_is_stable_and_roundtrips(tmp_path):
    cache = HtmlCache(root=tmp_path)
    url = "https://www.example.co.uk/property/123"

    assert cache.key(url) == cache.key(url)
    assert cache.get(url) is None

    put_page = cache.put(url, 200, "text/html", "<html>hello</html>")
    got_page = cache.get(url)

    assert got_page is not None
    assert got_page.body == put_page.body == "<html>hello</html>"
    assert got_page.status == 200
    assert got_page.content_type == "text/html"
    assert got_page.url == url


def test_cache_key_differs_for_different_urls(tmp_path):
    cache = HtmlCache(root=tmp_path)
    assert cache.key("https://a.co.uk/1") != cache.key("https://a.co.uk/2")


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
