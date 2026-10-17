"""The robots-gated, cache-first fetch facade.

This is the load-bearing architectural claim of the project: compliance is
enforced here, in code, before any backend is ever consulted — not asked
for in a prompt. `Fetcher.get()` always resolves the site's robots policy
and checks it before touching the cache miss path or the network backend.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from nimbus.config import Settings
from nimbus.fetch.backends import FetchBackend
from nimbus.fetch.cache import CachedPage, HtmlCache
from nimbus.fetch.robots import RobotsPolicy


class RobotsDisallowed(Exception):
    """Raised when robots.txt disallows fetching a URL for our agent."""

    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__(f"robots.txt disallows fetching {url}")


class Fetcher:
    def __init__(self, settings: Settings, cache: HtmlCache, backend: FetchBackend) -> None:
        self.settings = settings
        self.cache = cache
        self.backend = backend

    @property
    def _agent_name(self) -> str:
        return self.settings.user_agent.split("/")[0].strip()

    async def _fetch_raw(self, url: str) -> CachedPage:
        """Cache-first fetch with no robots check — used for robots.txt
        itself, which must always be reachable regardless of policy."""
        cached = self.cache.get(url)
        if cached is not None:
            return cached
        response = await self.backend.fetch(url, timeout=self.settings.request_timeout)
        return self.cache.put(url, response.status, response.content_type, response.body)

    async def robots_for(self, domain: str) -> RobotsPolicy:
        robots_url = f"https://{domain}/robots.txt"
        try:
            raw = await self._fetch_raw(robots_url)
        except Exception:
            policy = RobotsPolicy.allow_all(agent=self._agent_name)
            policy.crawl_delay = self.settings.default_crawl_delay
            return policy

        if raw.status != 200 or not raw.body.strip():
            policy = RobotsPolicy.allow_all(agent=self._agent_name)
            policy.crawl_delay = self.settings.default_crawl_delay
            return policy

        return RobotsPolicy.from_text(raw.body, agent=self._agent_name)

    async def get(self, url: str, *, allow_network: bool = True) -> CachedPage:
        domain = urlsplit(url).netloc
        policy = await self.robots_for(domain)
        if not policy.can_fetch(url):
            raise RobotsDisallowed(url)

        cached = self.cache.get(url)
        if cached is not None:
            return cached

        if not allow_network:
            raise RuntimeError(f"no cached page for {url} and network access is disabled")

        response = await self.backend.fetch(url, timeout=self.settings.request_timeout)
        return self.cache.put(url, response.status, response.content_type, response.body)
