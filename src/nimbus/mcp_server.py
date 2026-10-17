"""The discovery agent's only route to the web.

Three tools, and nothing else, are exposed over stdio MCP: `get_robots`,
`get_sitemap`, `fetch_page`. This is deliberate — the agent that learns an
unseen site gets no generic HTTP fetch and no shell. Crawling policy is
enforced in code the agent must call, not requested of it in a prompt.

`fetch_page` is the enforcement point: it delegates every fetch to
`Fetcher.get()`, which always resolves the site's robots policy and checks
it before the cache-miss path or the network backend is ever reached. A
disallowed URL comes back here as a structured `{"error": "robots_disallowed"}`
result, never as raw HTML, and the backend is never touched.

`build_tools(fetcher)` returns the three callables bound to a fetcher, so
tests can exercise them directly without going through stdio.
`build_server(fetcher)` registers them on a `FastMCP` instance; `main()`
wires up real settings/cache/backend and runs that server over stdio.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Callable
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from markdownify import markdownify
from mcp.server.fastmcp import FastMCP

from nimbus.config import Settings
from nimbus.fetch.backends import CrawlbaseBackend, HttpxBackend
from nimbus.fetch.cache import HtmlCache
from nimbus.fetch.fetcher import Fetcher, RobotsDisallowed

# Tags whose content carries no listing information and should be dropped
# entirely (not merely unwrapped) before markdown conversion.
_STRIP_TAGS = ("script", "style", "nav", "footer")


def _html_to_markdown(html: str) -> str:
    """Strip boilerplate tags (content included, not just the wrapper) and
    convert what remains to markdown. Raw listing-page HTML runs to tens of
    thousands of tokens; the markdown is a fraction of that."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_STRIP_TAGS):
        tag.decompose()
    return markdownify(str(soup)).strip()


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _locs(root: ET.Element) -> list[str]:
    """`<loc>` text of each direct child element, regardless of whether the
    root is a `<urlset>` (children are `<url>`) or a `<sitemapindex>`
    (children are `<sitemap>`) — both wrap a `<loc>`."""
    out = []
    for entry in root:
        for child in entry:
            if _strip_ns(child.tag) == "loc" and child.text:
                out.append(child.text.strip())
    return out


def _make_get_robots(fetcher: Fetcher) -> Callable:
    async def get_robots(domain: str) -> dict:
        policy = await fetcher.robots_for(domain)
        robots_url = f"https://{domain}/robots.txt"
        cached = fetcher.cache.get(robots_url)
        return {
            "domain": domain,
            "text": cached.body if cached is not None else "",
            "crawl_delay": policy.crawl_delay,
            "sitemaps": policy.sitemaps,
            "ai_signals": policy.ai_signals.model_dump(mode="json"),
            "fetchable": policy.can_fetch(f"https://{domain}/"),
        }

    return get_robots


def _make_get_sitemap(fetcher: Fetcher) -> Callable:
    async def get_sitemap(domain: str, *, max_urls: int = 2000) -> dict:
        policy = await fetcher.robots_for(domain)
        roots = policy.sitemaps or [f"https://{domain}/sitemap.xml"]

        urls: list[str] = []
        for root_url in roots:
            try:
                page = await fetcher.get(root_url)
            except RobotsDisallowed:
                continue
            try:
                root = ET.fromstring(page.body)
            except ET.ParseError:
                continue

            if _strip_ns(root.tag) == "sitemapindex":
                # Expand one level: fetch each child sitemap and take its
                # <loc> entries, but do not recurse into further indexes.
                for child_loc in _locs(root):
                    if len(urls) >= max_urls:
                        break
                    try:
                        child_page = await fetcher.get(child_loc)
                    except RobotsDisallowed:
                        continue
                    try:
                        child_root = ET.fromstring(child_page.body)
                    except ET.ParseError:
                        continue
                    urls.extend(_locs(child_root))
            else:
                urls.extend(_locs(root))

            if len(urls) >= max_urls:
                break

        urls = urls[:max_urls]
        return {
            "sitemap_urls": urls,
            "url_count": len(urls),
            "sample_urls": urls[:10],
        }

    return get_sitemap


def _make_fetch_page(fetcher: Fetcher) -> Callable:
    async def fetch_page(url: str, as_markdown: bool = True, max_chars: int = 40000) -> dict:
        domain = urlsplit(url).netloc
        # Checked before fetcher.get() is even called, so a disallowed URL
        # can never reach the cache-miss path or the backend from here.
        was_cached = fetcher.cache.get(url) is not None
        try:
            page = await fetcher.get(url)
        except RobotsDisallowed:
            return {"error": "robots_disallowed", "url": url, "domain": domain}

        content = _html_to_markdown(page.body) if as_markdown else page.body
        truncated = len(content) > max_chars
        if truncated:
            content = content[:max_chars]

        return {
            "url": url,
            "status": page.status,
            "content": content,
            "truncated": truncated,
            "from_cache": was_cached,
        }

    return fetch_page


def build_tools(fetcher: Fetcher) -> dict[str, Callable]:
    """The three tool callables, bound to `fetcher`. Tests exercise these
    directly, without going through stdio."""
    return {
        "get_robots": _make_get_robots(fetcher),
        "get_sitemap": _make_get_sitemap(fetcher),
        "fetch_page": _make_fetch_page(fetcher),
    }


def build_server(fetcher: Fetcher) -> FastMCP:
    server = FastMCP("nimbus-fetch")
    for name, fn in build_tools(fetcher).items():
        server.add_tool(fn, name=name)
    return server


def main() -> None:
    settings = Settings()
    cache = HtmlCache(root=settings.cache_dir)
    if settings.fetch_backend == "crawlbase":
        token = settings.crawlbase_token.get_secret_value() if settings.crawlbase_token else ""
        backend = CrawlbaseBackend(token=token, user_agent=settings.user_agent)
    else:
        backend = HttpxBackend(user_agent=settings.user_agent)
    fetcher = Fetcher(settings, cache, backend)
    build_server(fetcher).run()


if __name__ == "__main__":
    main()
