"""Shared test fixtures and factories.

No test in this suite may touch the network. Fixtures under
tests/fixtures/ stand in for anything that would otherwise require a live
request; the one exception (capturing the robots.txt fixtures themselves)
happened once, at authoring time, outside the test run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from nimbus.models import Listing, PropertyType, SiteRecipe, Tenure

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def make_listing(**overrides) -> Listing:
    """A valid Listing with sensible defaults, overridable per-test."""
    fields = {
        "source_domain": "ashworthholme.co.uk",
        "source_url": "https://www.ashworthholme.co.uk/property/123",
        "external_id": "123",
        "address": "1 Example Street, Manchester",
        "postcode": "M20 2RN",
        "price_gbp": 450_000,
        "price_qualifier": None,
        "bedrooms": 3,
        "bathrooms": 1,
        "property_type": PropertyType.TERRACED,
        "tenure": Tenure.FREEHOLD,
        "floor_area_sqft": 900.0,
        "description": "A lovely terraced house.",
        "agent_name": "Ashworth Holme",
        "listed_date": None,
        "content_hash": "deadbeef",
    }
    fields.update(overrides)
    return Listing(**fields)


def make_recipe(**overrides) -> SiteRecipe:
    """A valid SiteRecipe with sensible defaults, overridable per-test."""
    fields = {
        "domain": "ashworthholme.co.uk",
        "version": 1,
        "discovered_at": datetime(2026, 1, 1, tzinfo=UTC),
        "sitemap_urls": ["https://www.ashworthholme.co.uk/sitemap.xml"],
        "listing_index_urls": [],
        "listing_url_patterns": ["/property/*"],
        "pagination": None,
        "field_hints": [],
        "render_required": False,
        "confidence": 0.9,
        "notes": "Flat sitemap, direct listing URLs.",
    }
    fields.update(overrides)
    return SiteRecipe(**fields)


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR
