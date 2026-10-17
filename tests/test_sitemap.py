"""`nimbus.fetch.sitemap` is the single place both the discovery agent's
`get_sitemap` tool (`nimbus.mcp_server`) and the nightly crawler
(`nimbus.crawler`) parse sitemap XML, so the two never silently diverge on
what a `<urlset>` vs. a `<sitemapindex>` means or how a `<loc>` is read.
Pure, no I/O -- these tests parse fixture XML directly."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from nimbus.fetch.sitemap import is_sitemap_index, locs, strip_ns

FIXTURES = Path(__file__).parent / "fixtures" / "sitemaps"
SITEMAP_INDEX = (FIXTURES / "index.xml").read_text()
SITEMAP_PROPERTY = (FIXTURES / "property.xml").read_text()


def test_strip_ns_removes_namespace_prefix():
    assert strip_ns("{http://www.sitemaps.org/schemas/sitemap/0.9}urlset") == "urlset"
    assert strip_ns("urlset") == "urlset"


def test_is_sitemap_index_distinguishes_urlset_from_sitemapindex():
    index_root = ET.fromstring(SITEMAP_INDEX)
    urlset_root = ET.fromstring(SITEMAP_PROPERTY)

    assert is_sitemap_index(index_root) is True
    assert is_sitemap_index(urlset_root) is False


def test_locs_reads_sitemap_entries_from_a_urlset():
    root = ET.fromstring(SITEMAP_PROPERTY)

    assert locs(root) == [
        "https://jaksandco.co.uk/property/12345",
        "https://jaksandco.co.uk/property/12346",
        "https://jaksandco.co.uk/about-us",
    ]


def test_locs_reads_sitemap_entries_from_a_sitemapindex():
    root = ET.fromstring(SITEMAP_INDEX)

    assert locs(root) == ["https://jaksandco.co.uk/sitemap-property.xml"]
