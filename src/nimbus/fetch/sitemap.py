"""Shared sitemap XML parsing.

Both the discovery agent's `get_sitemap` tool (`nimbus.mcp_server`) and the
nightly crawler (`nimbus.crawler`) need to pull `<loc>` entries out of a
fetched sitemap, and both need to tell a `<urlset>` apart from a
`<sitemapindex>` so they know whether to expand one level before reading
locs. Keeping this in one place means a future sitemap convention (an
`<image:image>` extension, `<lastmod>`-based filtering, ...) only has to be
taught once, in code both callers share, rather than twice and in sync by
convention alone.

Pure, no I/O: callers fetch the XML themselves (via `Fetcher.get()`) and
hand the parsed `ET.Element` in here.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET


def strip_ns(tag: str) -> str:
    """An XML tag's local name, with any `{namespace}` prefix removed."""
    return tag.rsplit("}", 1)[-1]


def is_sitemap_index(root: ET.Element) -> bool:
    """True when `root` is a `<sitemapindex>` (a list of further sitemaps
    to fetch) rather than a `<urlset>` (a list of actual page URLs)."""
    return strip_ns(root.tag) == "sitemapindex"


def locs(root: ET.Element) -> list[str]:
    """`<loc>` text of each direct child element of a parsed sitemap,
    whether `root` is a `<urlset>` (children are `<url>`) or a
    `<sitemapindex>` (children are `<sitemap>`) -- both wrap a `<loc>`."""
    out: list[str] = []
    for entry in root:
        for child in entry:
            if strip_ns(child.tag) == "loc" and child.text:
                out.append(child.text.strip())
    return out
