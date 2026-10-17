"""Content-addressed disk cache for fetched pages.

Cache-first: a URL already on disk is never re-fetched, so the demo (and
the test suite) runs offline and byte-identically on every run.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


@dataclass
class CachedPage:
    url: str
    status: int
    content_type: str
    body: str
    fetched_at: datetime


def _normalise_url(url: str) -> str:
    """Lowercase scheme and host, drop any fragment, leave path/query as-is."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


class HtmlCache:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def key(self, url: str) -> str:
        normalised = _normalise_url(url)
        return hashlib.sha256(normalised.encode("utf-8")).hexdigest()

    def _path_for(self, url: str) -> Path:
        return self.root / f"{self.key(url)}.json"

    def get(self, url: str, *, max_age: float | None = None) -> CachedPage | None:
        """The cached page for `url`, or `None` if there is none.

        `max_age` is a staleness bound in seconds: an entry fetched longer
        ago than that is reported as a miss, so the caller re-fetches.
        `None` -- the default -- means entries never expire, which is what
        the offline demo and the test suite depend on (ADR-0004: the demo
        must be byte-identical on every run, and that is only true if a
        seeded entry is still served tomorrow). The nightly crawl passes
        `Settings.cache_max_age` instead: without a bound, a delta run
        would compare yesterday's bytes against themselves and could never
        detect a change.
        """
        path = self._path_for(url)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if max_age is not None:
            fetched_at = datetime.fromisoformat(data["fetched_at"])
            if (datetime.now(UTC) - fetched_at).total_seconds() > max_age:
                return None
        return CachedPage(
            url=data["url"],
            status=data["status"],
            content_type=data["content_type"],
            body=data["body"],
            fetched_at=datetime.fromisoformat(data["fetched_at"]),
        )

    def put(self, url: str, status: int, content_type: str, body: str) -> CachedPage:
        page = CachedPage(
            url=url,
            status=status,
            content_type=content_type,
            body=body,
            fetched_at=datetime.now(UTC),
        )
        path = self._path_for(url)
        path.write_text(
            json.dumps(
                {
                    "url": page.url,
                    "status": page.status,
                    "content_type": page.content_type,
                    "body": page.body,
                    "fetched_at": page.fetched_at.isoformat(),
                }
            ),
            encoding="utf-8",
        )
        return page
