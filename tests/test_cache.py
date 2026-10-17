from __future__ import annotations

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
