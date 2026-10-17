"""Shared HTML -> markdown conversion.

Both the discovery agent's `fetch_page` tool (`nimbus.mcp_server`) and the
per-page LLM extractor (`nimbus.extractor`) need to turn raw listing-page
HTML into compact markdown before handing it to a model: raw listing-page
HTML runs to tens of thousands of tokens, the markdown is a fraction of
that. Keeping the conversion in one place means the boilerplate-tag list
only has to be tuned once, in code both callers share, rather than twice
and in sync by convention alone -- the same pattern `fetch/sitemap.py`
already uses for sitemap parsing shared between `mcp_server` and
`crawler`.

Pure, no I/O.
"""

from __future__ import annotations

from bs4 import BeautifulSoup
from markdownify import markdownify

# Tags whose content carries no listing information and should be dropped
# entirely (not merely unwrapped) before markdown conversion.
STRIP_TAGS = ("script", "style", "nav", "footer")


def html_to_markdown(html: str) -> str:
    """Strip boilerplate tags (content included, not just the wrapper) and
    convert what remains to markdown."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(STRIP_TAGS):
        tag.decompose()
    return markdownify(str(soup)).strip()
