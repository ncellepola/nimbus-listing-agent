"""The robots-gated, cache-first fetch facade.

This is the load-bearing architectural claim of the project: compliance is
enforced here, in code, before any backend is ever consulted — not asked
for in a prompt. `Fetcher.get()` always resolves the site's robots policy
and checks it before touching the cache miss path or the network backend.

Two rules make that claim hold rather than merely sound true:

1. **A robots.txt we could not read is not permission.** RFC 9309 draws a
   line between *unavailable* (§2.3.1.3 — 404/410: the site has no
   robots.txt, so everything is permitted) and *unreachable* (§2.3.1.4 — a
   transport error or a 5xx: the crawler "MUST assume complete disallow").
   `robots_for` splits those branches and logs a WARNING naming the domain
   and the branch taken on every fallback, so a site silently flipping to
   allow-all is impossible rather than invisible.
2. **A non-2xx response is never cached.** The disk cache has no expiry
   beyond `max_age`, so caching a 503 robots.txt would turn one transient
   failure into a permanent, unnoticeable allow-all; caching a 404 listing
   page would hand an error document to the extractor as if it were
   content. Neither is stored, so the next run asks again.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from urllib.parse import urlsplit

from nimbus.config import Settings
from nimbus.fetch.backends import FetchBackend
from nimbus.fetch.cache import CachedPage, HtmlCache
from nimbus.fetch.robots import RobotsPolicy

logger = logging.getLogger(__name__)

# Statuses that mean "we were actively prevented from reading robots.txt",
# as opposed to "this site does not publish one". RFC 9309 files the whole
# 4xx range under *unavailable* (allow all), but 401/403/429 in practice
# mean a CDN or rate limiter is blocking us right now -- reading an active
# block as blanket permission is the wrong default, and the cost of being
# wrong in the other direction is only a skipped night.
_ROBOTS_UNREACHABLE_STATUSES = frozenset({401, 403, 429})


def _is_ok(status: int) -> bool:
    return 200 <= status < 300


class RobotsDisallowed(Exception):
    """Raised when robots.txt disallows fetching a URL for our agent."""

    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__(f"robots.txt disallows fetching {url}")


class FetchError(Exception):
    """Raised when the configured backend fails to fetch `url` for a
    transport-level reason -- a timeout, a connection error, too many
    redirects, a backend-specific proxy failure, and so on.

    `Fetcher.get()` raises this uniformly regardless of which `FetchBackend`
    is configured (`HttpxBackend` raises `httpx.HTTPError` subclasses;
    `CrawlbaseBackend` would raise whatever its own HTTP client raises for
    the same underlying problem). A caller that wants to tolerate a single
    bad URL without caring which backend is in use -- the nightly crawler,
    which must not let one flaky listing page abort an entire site's run --
    catches `FetchError` and nothing more backend-specific than that.
    """

    def __init__(self, url: str, cause: BaseException) -> None:
        self.url = url
        self.cause = cause
        super().__init__(f"failed to fetch {url}: {cause}")


class FetchStatusError(FetchError):
    """Raised when the backend answered, but with a non-2xx HTTP status.

    A subclass of `FetchError` on purpose: to the nightly crawler a 404 or
    a 500 on one listing page is the same kind of event as a timeout on it
    -- skip this URL, count it, carry on with the rest of the site -- so
    every existing `except FetchError` handler already does the right
    thing. Callers that care about the distinction read `.status`.
    """

    def __init__(self, url: str, status: int) -> None:
        self.status = status
        super().__init__(url, RuntimeError(f"HTTP {status}"))


class Fetcher:
    def __init__(self, settings: Settings, cache: HtmlCache, backend: FetchBackend) -> None:
        self.settings = settings
        self.cache = cache
        self.backend = backend

    @property
    def _agent_name(self) -> str:
        return self.settings.user_agent.split("/")[0].strip()

    def _allow_all_policy(self) -> RobotsPolicy:
        policy = RobotsPolicy.allow_all(agent=self._agent_name)
        policy.crawl_delay = self.settings.default_crawl_delay
        return policy

    def _deny_all_policy(self) -> RobotsPolicy:
        policy = RobotsPolicy.deny_all(agent=self._agent_name)
        policy.crawl_delay = self.settings.default_crawl_delay
        return policy

    async def _fetch_raw(self, url: str) -> CachedPage:
        """Cache-first fetch with no robots check — used for robots.txt
        itself, which must always be reachable regardless of policy.

        Returns the backend's response whatever its status, but only writes
        a 2xx one to the cache: `robots_for` needs to see the status to
        decide which fallback branch it is in, and a cached 503 would make
        that decision once and for all time."""
        cached = self.cache.get(url, max_age=self.settings.cache_max_age)
        if cached is not None:
            return cached
        response = await self.backend.fetch(url, timeout=self.settings.request_timeout)
        if not _is_ok(response.status):
            logger.warning(
                "not caching %s: backend returned HTTP %d (a non-2xx response is never "
                "stored, so the next run re-asks rather than reading a poisoned entry)",
                url,
                response.status,
            )
            return CachedPage(
                url=url,
                status=response.status,
                content_type=response.content_type,
                body=response.body,
                fetched_at=datetime.now(UTC),
            )
        return self.cache.put(url, response.status, response.content_type, response.body)

    async def robots_for(self, domain: str) -> RobotsPolicy:
        """`domain`'s robots policy, resolved fresh from the live file.

        Four distinct outcomes, each logged at WARNING when it is a
        fallback rather than a real parse:

        - transport error, or 5xx/401/403/429 -- **deny everything**
          (RFC 9309 §2.3.1.4's "complete disallow"; a site we cannot ask
          has not said yes),
        - any other non-200 (404/410: the site publishes no robots.txt) --
          allow all (§2.3.1.3),
        - 200 with an empty body -- allow all (an empty file is a valid
          robots.txt that restricts nothing),
        - 200 with content -- parse it.
        """
        robots_url = f"https://{domain}/robots.txt"
        try:
            raw = await self._fetch_raw(robots_url)
        except Exception:
            logger.warning(
                "robots.txt for %s is unreachable (transport error); assuming complete "
                "disallow for this domain until it can be read again (RFC 9309 2.3.1.4)",
                domain,
                exc_info=True,
            )
            return self._deny_all_policy()

        if raw.status >= 500 or raw.status in _ROBOTS_UNREACHABLE_STATUSES:
            logger.warning(
                "robots.txt for %s returned HTTP %d; assuming complete disallow for this "
                "domain until it can be read again (RFC 9309 2.3.1.4) -- the response was "
                "not cached, so the next run will re-ask",
                domain,
                raw.status,
            )
            return self._deny_all_policy()

        if raw.status != 200:
            logger.warning(
                "robots.txt for %s returned HTTP %d; treating the site as publishing no "
                "robots.txt at all and allowing everything (RFC 9309 2.3.1.3)",
                domain,
                raw.status,
            )
            return self._allow_all_policy()

        if not raw.body.strip():
            logger.warning(
                "robots.txt for %s is empty; an empty file restricts nothing, so allowing "
                "everything at the configured default crawl delay",
                domain,
            )
            return self._allow_all_policy()

        return RobotsPolicy.from_text(
            raw.body,
            agent=self._agent_name,
            default_crawl_delay=self.settings.default_crawl_delay,
        )

    async def get(self, url: str, *, allow_network: bool = True) -> CachedPage:
        """Fetch `url`, robots-gated and cache-first.

        Cache entries older than `Settings.cache_max_age` are treated as
        misses and re-fetched. `cache_max_age = None` disables that, which
        is what `nimbus demo --offline` configures so its seeded fixtures
        never expire (ADR-0004).

        Raises `RobotsDisallowed` before any network call if policy says
        so, `FetchStatusError` (a `FetchError`) for a non-2xx response, and
        `FetchError` for a transport failure. A non-2xx body is never
        cached and never returned -- it must not reach the extractor as if
        it were page content.
        """
        domain = urlsplit(url).netloc
        policy = await self.robots_for(domain)
        if not policy.can_fetch(url):
            raise RobotsDisallowed(url)

        cached = self.cache.get(url, max_age=self.settings.cache_max_age)
        if cached is not None:
            if not _is_ok(cached.status):
                # Written before non-2xx caching was removed. Treated as a
                # fetch failure rather than content, same as a fresh one.
                raise FetchStatusError(url, cached.status)
            return cached

        if not allow_network:
            raise RuntimeError(f"no cached page for {url} and network access is disabled")

        try:
            response = await self.backend.fetch(url, timeout=self.settings.request_timeout)
        except Exception as exc:
            raise FetchError(url, exc) from exc

        if not _is_ok(response.status):
            logger.warning("not caching %s: backend returned HTTP %d", url, response.status)
            raise FetchStatusError(url, response.status)

        return self.cache.put(url, response.status, response.content_type, response.body)
