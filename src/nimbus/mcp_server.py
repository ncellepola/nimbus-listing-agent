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
`build_server(fetcher)` registers them on an `MCPServer` instance; `main()`
wires up real settings/cache/backend and runs that server over stdio.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from collections.abc import Callable
from urllib.parse import urlsplit

from mcp.server.mcpserver import MCPServer

from nimbus.config import Settings
from nimbus.fetch.backends import build_backend
from nimbus.fetch.cache import HtmlCache
from nimbus.fetch.fetcher import Fetcher, FetchError, RobotsDisallowed
from nimbus.fetch.sitemap import is_sitemap_index, locs
from nimbus.htmltext import html_to_markdown

# Size of get_sitemap's stratified sample_urls. 25, not 10: a sample this
# small still needs to be spread across a sitemap of thousands of URLs
# (see _stratified_sample) with enough URLs surviving to cover more than
# one property-type/branch/date grouping.
_SAMPLE_SIZE = 25

logger = logging.getLogger(__name__)


def _stratified_sample(urls: list[str], size: int) -> list[str]:
    """`size` URLs spread evenly across the whole list, not just its head.

    A sitemap grouped by property type, branch, or listing date puts every
    URL of one category before any URL of another (396 `property-to-rent`
    URLs then 679 `property-for-sale` URLs is a real example that broke
    discovery against a live site: `urls[:10]` was ten rentals and no sale
    listing anywhere in the sample). Taking every `len(urls) / size`'th
    index instead means a sample always has a chance of covering every
    category, however the sitemap is ordered. Deterministic: the same
    `urls` list always yields the same sample."""
    if len(urls) <= size:
        return list(urls)

    stride = len(urls) / size
    seen_indices: set[int] = set()
    sample: list[str] = []
    for i in range(size):
        index = int(i * stride)
        if index not in seen_indices:
            seen_indices.add(index)
            sample.append(urls[index])
    return sample


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
            except FetchError:
                # Includes FetchStatusError. `roots` falls back to a
                # *guessed* https://{domain}/sitemap.xml when robots.txt
                # declares none, so a 404 here is the ordinary "this site
                # has no sitemap" case (jaksandco.co.uk, in this repo's
                # own fixtures) and must leave the agent free to fall
                # back to a listing index -- not end its tool call.
                logger.warning("get_sitemap: could not fetch %s; skipping", root_url)
                continue
            try:
                root = ET.fromstring(page.body)
            except ET.ParseError:
                continue

            if is_sitemap_index(root):
                # Expand one level: fetch each child sitemap and take its
                # <loc> entries, but do not recurse into further indexes.
                for child_loc in locs(root):
                    if len(urls) >= max_urls:
                        break
                    try:
                        child_page = await fetcher.get(child_loc)
                    except RobotsDisallowed:
                        continue
                    except FetchError:
                        logger.warning(
                            "get_sitemap: could not fetch child sitemap %s; skipping", child_loc
                        )
                        continue
                    try:
                        child_root = ET.fromstring(child_page.body)
                    except ET.ParseError:
                        continue
                    urls.extend(locs(child_root))
            else:
                urls.extend(locs(root))

            if len(urls) >= max_urls:
                break

        urls = urls[:max_urls]
        return {
            # The discovered *page* URLs. Named `urls`, not `sitemap_urls`,
            # so it cannot be confused with SiteRecipe.sitemap_urls, where
            # that name means the sitemap *file* locations, not the pages
            # listed inside them.
            "urls": urls,
            "url_count": len(urls),
            "sample_urls": _stratified_sample(urls, _SAMPLE_SIZE),
            # The sitemap file(s) actually fetched to produce `urls`.
            "sitemap_roots": roots,
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
        except FetchError as exc:
            # A dead or erroring URL comes back as data the agent can
            # reason about and route around, exactly as a disallowed one
            # does. Raising would end the whole tool call over one bad
            # page -- and `Fetcher.get()` raises on any non-2xx now, so
            # a single 404 in a sitemap would have done it.
            status = getattr(exc, "status", None)
            logger.warning("fetch_page: %s", exc)
            return {
                "error": "http_error" if status is not None else "fetch_failed",
                "url": url,
                "domain": domain,
                "status": status,
                "detail": str(exc),
            }

        content = html_to_markdown(page.body) if as_markdown else page.body
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


def build_server(fetcher: Fetcher) -> MCPServer:
    server = MCPServer("nimbus-fetch")
    for name, fn in build_tools(fetcher).items():
        server.add_tool(fn, name=name)
    return server


def main() -> None:
    settings = Settings()
    cache = HtmlCache(root=settings.cache_dir)
    fetcher = Fetcher(settings, cache, build_backend(settings))
    build_server(fetcher).run()


if __name__ == "__main__":
    main()
