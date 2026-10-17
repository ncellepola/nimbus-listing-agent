"""The deterministic, recipe-driven crawler.

This is the hot path: it runs every night, across every onboarded site, and
contains no LLM anywhere in it (`test_crawler_module_contains_no_llm_client`
greps this file for the two LLM client package names and fails the build if
either appears). By the time this module runs, an LLM has already spent its
one-time cost producing a `SiteRecipe` (Task 3); everything here is a plain
loop over `Fetcher.get()` driven entirely by that recipe's fields.

Per ADR-0003, this module calls `nimbus.fetch.fetcher.Fetcher` directly. It
does not go through `nimbus.mcp_server` -- MCP exists to let an LLM client
choose, turn by turn, which tool to call next; a nightly batch crawl has no
such judgement left to exercise, so routing it through a stdio MCP round
trip per page would add protocol overhead for a decision the recipe already
made at onboarding time.

Three functions:

- `select_listing_urls` is pure: given a flat list of candidate URLs and a
  `SiteRecipe`, it keeps only the ones matching `recipe.listing_url_patterns`,
  de-duplicating while preserving order.
- `collect_candidate_urls` walks the recipe's declared sources -- sitemaps
  first (expanding one level of sitemap index, via the sitemap parsing
  `nimbus.fetch.sitemap` shares with `mcp_server.get_sitemap`), falling
  back to `listing_index_urls` walked through `recipe.pagination` when a
  site has no sitemap at all (the jaksandco.co.uk case). It returns the
  unfiltered superset of URLs found; `select_listing_urls` narrows that
  down to actual listing pages.
- `crawl_site` ties the two together and fetches each selected listing URL,
  honouring a caller-supplied `crawl_delay` between *every* fetch the run
  makes -- candidate collection included, via a shared `Pacer` -- and
  skipping (not failing the run on) any URL robots.txt disallows or any
  `FetchError`. It
  returns a `CrawlOutcome`, not a bare list: the fetched `CrawlRecord`s plus
  candidate/selected/fetched/skipped counts an orchestration layer can
  write straight into `storage.crawl_runs` without re-crawling to recover
  them.

Crawl delay is deliberately not read off the recipe: it is live robots.txt
policy that can change week to week, so the caller resolves it fresh via
`await fetcher.robots_for(domain)` and passes it in explicitly (pre-flight
ruling R5). Freezing it into the recipe would let a site's crawl-delay
tightening go unnoticed for as long as the recipe goes unrefreshed.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import logging
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from nimbus.fetch.fetcher import Fetcher, FetchError, RobotsDisallowed
from nimbus.fetch.sitemap import is_sitemap_index, locs
from nimbus.models import CrawlOutcome, CrawlRecord, PaginationRule, SiteRecipe

logger = logging.getLogger(__name__)


class Pacer:
    """Enforces a crawl delay between consecutive fetches, across a whole
    run rather than within one phase of it.

    `crawl_site` used to sleep only between listing-page fetches, while
    `collect_candidate_urls` fanned out across child sitemaps and
    paginated index pages at full speed -- so every run opened with an
    unpaced burst against exactly the sites most likely to have declared a
    `Crawl-delay` in the first place. One `Pacer` threaded through both
    phases also closes the gap at the boundary between them.

    No sleep before the first fetch: a delay is a gap *between* requests.
    """

    def __init__(self, delay: float, sleep: Callable[[float], Awaitable[None]]) -> None:
        self._delay = delay
        self._sleep = sleep
        self._started = False

    async def wait(self) -> None:
        if self._started and self._delay > 0:
            await self._sleep(self._delay)
        self._started = True


_NO_DELAY = Pacer(0.0, asyncio.sleep)


def _extract_links(html: str, base_url: str) -> list[str]:
    """Every absolute http(s) link on a listing-index page, resolved
    against `base_url`. Non-http(s) links (mailto:, tel:, javascript:) and
    bare in-page anchors are dropped."""
    soup = BeautifulSoup(html, "html.parser")
    out: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith("#"):
            continue
        absolute = urljoin(base_url, href)
        if urlsplit(absolute).scheme in ("http", "https"):
            out.append(absolute)
    return out


def _paginated_url(url: str, pagination: PaginationRule | None, page: int) -> str:
    """The URL for `page` (1-indexed) of a paginated listing index, per
    `pagination`. Page 1 is always the recipe's own `listing_index_urls`
    entry, unchanged."""
    if pagination is None or pagination.kind == "none" or page <= 1:
        return url

    parts = urlsplit(url)
    if pagination.kind == "query_param":
        param = pagination.param or "page"
        query = dict(parse_qsl(parts.query))
        query[param] = str(page)
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
        )

    # kind == "path": insert a /page/N/ segment ahead of the query string,
    # e.g. https://…/find-a-property/?department=residential-sales ->
    # https://…/find-a-property/page/2/?department=residential-sales
    # (jaksandco.co.uk's own convention).
    path = parts.path if parts.path.endswith("/") else f"{parts.path}/"
    path = f"{path}page/{page}/"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


async def _collect_from_sitemaps(recipe: SiteRecipe, fetcher: Fetcher, pacer: Pacer) -> list[str]:
    urls: list[str] = []
    for root_url in recipe.sitemap_urls:
        await pacer.wait()
        try:
            page = await fetcher.get(root_url)
        except RobotsDisallowed:
            logger.warning("robots disallows sitemap %s; skipping", root_url)
            continue
        except FetchError:
            logger.warning("failed to fetch sitemap %s; skipping", root_url, exc_info=True)
            continue

        try:
            root = ET.fromstring(page.body)
        except ET.ParseError:
            logger.warning("sitemap %s is not valid XML; skipping", root_url)
            continue

        if is_sitemap_index(root):
            # Expand one level, same convention as mcp_server.get_sitemap:
            # fetch each child sitemap and take its <loc> entries, but do
            # not recurse into further indexes.
            for child_loc in locs(root):
                await pacer.wait()
                try:
                    child_page = await fetcher.get(child_loc)
                except RobotsDisallowed:
                    logger.warning("robots disallows child sitemap %s; skipping", child_loc)
                    continue
                except FetchError:
                    logger.warning(
                        "failed to fetch child sitemap %s; skipping", child_loc, exc_info=True
                    )
                    continue
                try:
                    child_root = ET.fromstring(child_page.body)
                except ET.ParseError:
                    logger.warning("child sitemap %s is not valid XML; skipping", child_loc)
                    continue
                urls.extend(locs(child_root))
        else:
            urls.extend(locs(root))

    return urls


async def _collect_from_listing_index(
    recipe: SiteRecipe, fetcher: Fetcher, pacer: Pacer
) -> list[str]:
    max_pages = recipe.pagination.max_pages if recipe.pagination is not None else 1
    urls: list[str] = []
    for index_url in recipe.listing_index_urls:
        for page in range(1, max_pages + 1):
            page_url = _paginated_url(index_url, recipe.pagination, page)
            await pacer.wait()
            try:
                fetched = await fetcher.get(page_url)
            except RobotsDisallowed:
                logger.warning("robots disallows listing index %s; skipping", page_url)
                break
            except FetchError:
                logger.warning(
                    "failed to fetch listing index %s; skipping", page_url, exc_info=True
                )
                break

            links = _extract_links(fetched.body, page_url)
            if page > 1 and not links:
                # No links at all on a page beyond the first almost always
                # means pagination has run out (a jaksandco.co.uk quirk:
                # its own page 2 reports results but renders no cards).
                break
            urls.extend(links)

    return urls


async def collect_candidate_urls(
    recipe: SiteRecipe, *, fetcher: Fetcher, pacer: Pacer | None = None
) -> list[str]:
    """Every URL reachable from the recipe's declared sources, unfiltered.

    Sitemaps are tried first, when the recipe has any. A site with no
    sitemap at all (`sitemap_urls` empty, or every sitemap fetch failing)
    falls back to walking `listing_index_urls` through `recipe.pagination`
    and scraping each index page's own links.

    Not yet narrowed to listing pages -- use `select_listing_urls` for
    that, against `recipe.listing_url_patterns`.

    `pacer` applies the site's declared crawl delay between these fetches
    too. It defaults to no delay so this function stays usable on its own,
    but `crawl_site` always passes the same `Pacer` it uses for listing
    pages: a run that politely spaces its 195 listing fetches and then
    hammers a sitemap index has not been polite.
    """
    pacer = pacer if pacer is not None else _NO_DELAY
    if recipe.sitemap_urls:
        urls = await _collect_from_sitemaps(recipe, fetcher, pacer)
        if urls:
            return urls

    return await _collect_from_listing_index(recipe, fetcher, pacer)


def _matches_any_pattern(url: str, patterns: list[str]) -> bool:
    if not patterns:
        return False
    path = urlsplit(url).path
    return any(
        fnmatch.fnmatch(url, pattern) or fnmatch.fnmatch(path, pattern) for pattern in patterns
    )


def select_listing_urls(urls: list[str], recipe: SiteRecipe) -> list[str]:
    """Pure: narrow `urls` down to the ones matching any of
    `recipe.listing_url_patterns`, de-duplicating while preserving the
    order they first appeared in. No I/O, no recipe mutation."""
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        if url in seen:
            continue
        if _matches_any_pattern(url, recipe.listing_url_patterns):
            seen.add(url)
            out.append(url)
    return out


async def crawl_site(
    recipe: SiteRecipe,
    *,
    fetcher: Fetcher,
    crawl_delay: float,
    limit: int | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> CrawlOutcome:
    """Fetch every listing page a recipe implies, with no LLM anywhere in
    the loop. `crawl_delay` is resolved by the caller from live robots.txt
    (`await fetcher.robots_for(domain)`) rather than frozen into the
    recipe, per pre-flight ruling R5, and is awaited via `sleep` between
    fetches (not before the first one). A URL robots.txt disallows, or any
    `FetchError` (timeout, too-many-redirects, a flaky listing page -- real
    sites have these; bentleyhurst.co.uk's own recipe notes two sample
    URLs that "errored on fetch"), is logged and skipped. One bad URL must
    never abort an entire site's nightly crawl; any exception other than
    those two (a bug in this module, a corrupted cache entry, ...) still
    propagates, since silently swallowing *that* would hide a real defect
    behind the exact same log line as routine site flakiness.

    Returns a `CrawlOutcome`, not a bare list of records: the counts an
    orchestration layer needs to write a truthful `storage.crawl_runs` row
    only exist here, inside this loop, and are gone once a skipped URL has
    been discarded -- a caller cannot recover them by inspecting `records`
    alone, and must not have to re-crawl just to find out how many URLs
    failed.

    Also logs one `ERROR` summary line distinguishing two failure shapes
    that must not be confused with each other or with a quiet night: a
    recipe whose `listing_url_patterns` suddenly match none of the
    candidates found (recipe drift -- the thing Task 6's health
    monitor/self-heal exist to catch) versus every selected URL failing to
    fetch or being disallowed (usually transient). See the comment above
    the logging calls below for the exact conditions.
    """
    pacer = Pacer(crawl_delay, sleep)
    candidates = await collect_candidate_urls(recipe, fetcher=fetcher, pacer=pacer)
    # `selected_urls` is what the recipe implied; `listing_urls` is what
    # this run actually walks. They differ only under an explicit --limit,
    # and both numbers reach the CrawlOutcome: reporting the truncated
    # count as `selected` is what let `--limit 5` on a 195-listing site
    # look like a complete crawl to the mass-withdrawal guard.
    selected_urls = select_listing_urls(candidates, recipe)
    limit_applied = limit is not None and limit < len(selected_urls)
    listing_urls = selected_urls[:limit] if limit is not None else selected_urls

    records: list[CrawlRecord] = []
    skipped_robots = 0
    skipped_errors = 0
    for url in listing_urls:
        await pacer.wait()

        was_cached = fetcher.cache.get(url) is not None
        try:
            page = await fetcher.get(url)
        except RobotsDisallowed:
            logger.warning("robots disallows listing page %s; skipping", url)
            skipped_robots += 1
            continue
        except FetchError:
            logger.warning("failed to fetch listing page %s; skipping", url, exc_info=True)
            skipped_errors += 1
            continue

        content_hash = hashlib.sha256(page.body.encode("utf-8")).hexdigest()
        records.append(
            CrawlRecord(
                url=page.url,
                html=page.body,
                from_cache=was_cached,
                fetched_at=page.fetched_at,
                content_hash=content_hash,
            )
        )

    # One summary line per run, at a severity that actually distinguishes
    # "this site genuinely has nothing new tonight" from the two distinct
    # failure shapes below -- across hundreds of sites, none of these three
    # must look identical in the logs with only per-URL WARNINGs to tell
    # them apart.
    if candidates and not selected_urls:
        # Candidates existed but listing_url_patterns matched none of
        # them: this is recipe drift, not a quiet night. A site redesign
        # that changes its URL structure produces exactly this shape, and
        # it is precisely what Task 6's health monitor/self-heal exist to
        # notice -- it needs a recipe repair, not a retry.
        logger.error(
            "crawl_site(%s): listing_url_patterns matched none of %d candidate URL(s); "
            "the recipe's patterns may be stale (a site redesign is the usual cause)",
            recipe.domain,
            len(candidates),
        )
    elif listing_urls and not records:
        # Patterns matched, but every matched URL failed to fetch or was
        # disallowed -- usually transient (a network blip, a temporary
        # block), unlike the pattern-mismatch case above.
        logger.error(
            "crawl_site(%s): all %d selected listing URL(s) failed to fetch or were "
            "disallowed (skipped_robots=%d, skipped_errors=%d)",
            recipe.domain,
            len(listing_urls),
            skipped_robots,
            skipped_errors,
        )
    else:
        logger.info(
            "crawl_site(%s): %d candidate(s), %d selected, %d attempted, %d fetched "
            "(limit_applied=%s, skipped_robots=%d, skipped_errors=%d)",
            recipe.domain,
            len(candidates),
            len(selected_urls),
            len(listing_urls),
            len(records),
            limit_applied,
            skipped_robots,
            skipped_errors,
        )

    return CrawlOutcome(
        domain=recipe.domain,
        records=records,
        candidates=len(candidates),
        selected=len(selected_urls),
        attempted=len(listing_urls),
        fetched=len(records),
        skipped_robots=skipped_robots,
        skipped_errors=skipped_errors,
        limit_applied=limit_applied,
    )
