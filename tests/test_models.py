from __future__ import annotations

import pytest
from conftest import make_listing
from pydantic import ValidationError


def test_listing_normalises_postcode_and_rejects_negative_price():
    assert make_listing(postcode="m20 2rn").postcode == "M20 2RN"
    with pytest.raises(ValidationError):
        make_listing(price_gbp=-1)


def test_listing_allows_missing_optional_fields():
    listing = make_listing(
        postcode=None,
        price_gbp=None,
        bedrooms=None,
        bathrooms=None,
        floor_area_sqft=None,
        description=None,
        agent_name=None,
        listed_date=None,
        external_id=None,
    )
    assert listing.postcode is None
    assert listing.price_gbp is None
