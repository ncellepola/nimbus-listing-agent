"""Pure, no-LLM price parsing and sale/rental classification.

These two functions are the deterministic guard that runs *after* the
model (Step 3 of the Task 5 brief): the model only ever copies the literal
status-and-price text it saw on the page, and `parse_price` /
`is_residential_sale` decide what that text actually means. No fixture, no
LLM client, no I/O.
"""

from __future__ import annotations

import pytest
from conftest import make_listing

from nimbus.extractor import is_residential_sale, parse_price


@pytest.mark.parametrize(
    "raw,pounds,qual",
    [
        ("£450,000", 450_000, None),
        ("Offers in excess of £450,000", 450_000, "offers_in_excess_of"),
        ("Guide Price £1.25m", 1_250_000, "guide_price"),
        ("£450,000 - £475,000", 450_000, "range"),
        ("POA", None, "poa"),
        ("Price on application", None, "poa"),
        ("£1,250 pcm", None, "rental"),
    ],
)
def test_parse_price(raw, pounds, qual):
    assert parse_price(raw) == (pounds, qual)


@pytest.mark.parametrize(
    "raw,pounds,qual",
    [
        # Extra qualifier vocabulary seen in the real jaksandco.co.uk recipe
        # notes ("Guide Price", "Offers Over", "OIRO"), beyond the set the
        # parametrised test above pins.
        ("Offers Over £500,000", 500_000, "offers_over"),
        ("OIRO £500,000", 500_000, "oiro"),
    ],
)
def test_parse_price_extra_qualifiers_seen_in_real_recipes(raw, pounds, qual):
    assert parse_price(raw) == (pounds, qual)


def test_is_residential_sale_rejects_rental_and_sold_stc():
    listing = make_listing()
    assert is_residential_sale("£1,250 pcm available now", listing) is False
    assert is_residential_sale("Sold STC", listing) is False
    assert is_residential_sale("£450,000 Freehold", listing) is True


def test_is_residential_sale_rejects_to_let_status_with_no_visible_price():
    # ashworthholme.co.uk's own recipe notes: a lettings page under the
    # same URL pattern reads "To Let" with a PCM price, but the status
    # text alone (no £ sign reaching parse_price at all) must still be
    # enough to reject it.
    listing = make_listing()
    assert is_residential_sale("To Let", listing) is False
    assert is_residential_sale("For Sale", listing) is True
