from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from nimbus.fetch.cache import HtmlCache


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


def test_cache_entry_older_than_max_age_reads_as_a_miss(tmp_path):
    """Without a staleness bound the nightly crawl re-reads yesterday's
    bytes, so the delta compares a page against itself and can never
    fire. `max_age=None` (the default) keeps the offline demo's seeded
    fixtures alive forever."""
    cache = HtmlCache(root=tmp_path)
    url = "https://www.example.co.uk/property/1"
    cache.put(url, 200, "text/html", "<html>yesterday</html>")

    path = next(tmp_path.glob("*.json"))
    data = json.loads(path.read_text())
    data["fetched_at"] = (datetime.now(UTC) - timedelta(hours=30)).isoformat()
    path.write_text(json.dumps(data))

    assert cache.get(url) is not None, "no bound means never expires"
    assert cache.get(url, max_age=43_200.0) is None
    assert cache.get(url, max_age=60 * 60 * 24 * 7) is not None
