"""Pluggable fetch backends.

`httpx` is the default, offline-friendly backend. `CrawlbaseBackend` is the
configured seam for a paid anti-bot / JS-rendering backend on sites where
that is needed — a commodity problem worth buying rather than building for
this proof of concept.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import httpx


@dataclass
class RawResponse:
    status: int
    content_type: str
    body: str


@runtime_checkable
class FetchBackend(Protocol):
    async def fetch(self, url: str, *, timeout: float) -> RawResponse: ...


class HttpxBackend:
    """Default backend: a plain HTTP GET via httpx, identifying as our UA."""

    def __init__(self, user_agent: str) -> None:
        self.user_agent = user_agent

    async def fetch(self, url: str, *, timeout: float) -> RawResponse:
        async with httpx.AsyncClient(
            headers={"User-Agent": self.user_agent}, follow_redirects=True
        ) as client:
            response = await client.get(url, timeout=timeout)
        content_type = response.headers.get("content-type", "").split(";")[0].strip()
        return RawResponse(
            status=response.status_code, content_type=content_type, body=response.text
        )


class CrawlbaseBackend:
    """Backend that proxies requests through Crawlbase, for JS-rendered or
    anti-bot-protected sites. Not exercised by the offline test suite or the
    offline demo path; configured via `fetch_backend = "crawlbase"` and
    `crawlbase_token`."""

    BASE_URL = "https://api.crawlbase.com/"

    def __init__(self, token: str, user_agent: str) -> None:
        self.token = token
        self.user_agent = user_agent

    async def fetch(self, url: str, *, timeout: float) -> RawResponse:
        async with httpx.AsyncClient(headers={"User-Agent": self.user_agent}) as client:
            response = await client.get(
                self.BASE_URL,
                params={"token": self.token, "url": url},
                timeout=timeout,
            )
        content_type = response.headers.get("content-type", "").split(";")[0].strip()
        return RawResponse(
            status=response.status_code, content_type=content_type, body=response.text
        )
