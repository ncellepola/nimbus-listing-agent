"""The one LLM call per page, its deterministic guards, and the audit
trail it writes. `StubClient` below stands in for `anthropic.AsyncAnthropic`
-- no test in this file touches the network or a real model.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import make_listing, make_recipe

from nimbus.extractor import (
    EXTRACT_PROMPT_VERSION,
    ExtractionError,
    audit_row,
    extract_listing,
    record_extraction,
)
from nimbus.models import CrawlRecord, ExtractionResult, PropertyType, SiteRecipe, Tenure
from nimbus.storage import init_db

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _record_from_fixture(domain: str, filename: str, url: str) -> CrawlRecord:
    html = (FIXTURES_DIR / "pages" / domain / filename).read_text(encoding="utf-8", errors="ignore")
    return CrawlRecord(
        url=url,
        html=html,
        from_cache=False,
        fetched_at=datetime.now(UTC),
        content_hash=hashlib.sha256(html.encode("utf-8")).hexdigest(),
    )


def _recipe_from_fixture(domain: str) -> SiteRecipe:
    data = json.loads((FIXTURES_DIR / "recipes" / f"{domain}.json").read_text())
    return SiteRecipe(**data)


def _simple_record(
    html: str = "<html><h1>1 Test St</h1><h2>For Sale £450,000</h2></html>",
) -> CrawlRecord:
    return CrawlRecord(
        url="https://www.example.co.uk/property/1",
        html=html,
        from_cache=False,
        fetched_at=datetime.now(UTC),
        content_hash=hashlib.sha256(html.encode("utf-8")).hexdigest(),
    )


def _simple_recipe() -> SiteRecipe:
    return make_recipe(
        domain="example.co.uk",
        field_hints=[
            {"field": "price", "css_selector": "h2", "notes": "status + price"},
            {"field": "address", "css_selector": "h1", "notes": None},
        ],
    )


VALID_PAYLOAD = {
    "address": "1 Test St, Manchester",
    "postcode": "M1 1AA",
    "price_raw": "For Sale £450,000",
    "bedrooms": 3,
    "bathrooms": 1,
    "property_type": PropertyType.TERRACED.value,
    "tenure": Tenure.FREEHOLD.value,
    "floor_area_sqft": 900.0,
    "description": "A lovely house.",
    "agent_name": "Test Agents",
    "listed_date": None,
    "external_id": "123",
}


class _FakeTextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeToolUseBlock:
    type = "tool_use"

    def __init__(self, input: dict, name: str = "emit_listing", id: str = "toolu_1") -> None:
        self.input = input
        self.name = name
        self.id = id


class _FakeUsage:
    def __init__(self, input_tokens: int = 100, output_tokens: int = 50) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeMessage:
    def __init__(self, content: list, stop_reason: str = "tool_use") -> None:
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _FakeUsage()


class _FakeMessagesAPI:
    def __init__(self, outer: StubClient) -> None:
        self._outer = outer

    async def create(self, **kwargs):
        return self._outer._response()


class StubClient:
    """A stand-in for `anthropic.AsyncAnthropic`: `await client.messages.create(...)`
    returns a canned response instead of calling the network."""

    def __init__(
        self,
        *,
        returns: str | None = None,
        payload: dict | None = None,
        price: int | None = None,
    ) -> None:
        self.returns = returns
        self.payload = dict(payload) if payload is not None else dict(VALID_PAYLOAD)
        if price is not None:
            self.payload["price_raw"] = f"For Sale £{price:,}"
        self.messages = _FakeMessagesAPI(self)

    def _response(self) -> _FakeMessage:
        if self.returns is not None:
            return _FakeMessage(content=[_FakeTextBlock(self.returns)], stop_reason="end_turn")
        return _FakeMessage(content=[_FakeToolUseBlock(input=dict(self.payload))])


async def test_extract_listing_rejects_malformed_model_output():
    record = _simple_record()
    recipe = _simple_recipe()
    result = await extract_listing(record, recipe, client=StubClient(returns="not json"))
    assert result.listing is None
    assert result.needs_review is True
    assert result.issues


async def test_extract_listing_rejects_price_not_corroborated_by_field_hint():
    # The real ashworthholme fixtures carry 48 distinct pound values from a
    # price-filter dropdown, £350 to £10,000,000, against a true price of
    # £650,000. A mere "does this value appear in the page" check passes a
    # wrong answer, so the price must be corroborated at the recipe's field
    # hint location (its own H2), not merely be present somewhere on the page.
    recipe = _recipe_from_fixture("ashworthholme.co.uk")
    record = _record_from_fixture(
        "ashworthholme.co.uk",
        "rossett-ave-altrincham.html",
        "https://www.ashworthholme.co.uk/property-details/1/-/sale/rossett-ave",
    )
    result = await extract_listing(record, recipe, client=StubClient(price=10_000_000))
    assert result.needs_review is True
    assert any("not corroborated" in issue for issue in result.issues)
    # The rest of the listing survives; only the unverifiable price is dropped.
    assert result.listing is not None
    assert result.listing.price_gbp is None


@pytest.mark.parametrize(
    "domain,filename,true_price",
    [
        # ashworthholme.co.uk: the true price is in the field_hint's own
        # selector (h2), buried among the page's 48-49 price-filter-dropdown
        # decoys everywhere else.
        ("ashworthholme.co.uk", "rossett-ave-altrincham.html", 650_000),
        ("ashworthholme.co.uk", "sherbrooke-close.html", 850_000),
        # jaksandco.co.uk: the recipe's price field_hint has no
        # css_selector at all, and the price is in neither h1 nor any
        # other heading (jaksandco's own first h2 is "What's nearby?") --
        # only the price/id-widening rule in _corroboration_text finds it.
        ("jaksandco.co.uk", "grenfell-road-didsbury.html", 475_000),
        ("jaksandco.co.uk", "kingsway-manchester.html", 350_000),
    ],
)
async def test_extract_listing_corroborates_the_real_price_on_real_fixtures(
    domain, filename, true_price
):
    recipe = _recipe_from_fixture(domain)
    record = _record_from_fixture(domain, filename, f"https://www.{domain}/property/{filename}")
    result = await extract_listing(record, recipe, client=StubClient(price=true_price))
    assert result.needs_review is False
    assert result.listing is not None
    assert result.listing.price_gbp == true_price


async def test_extract_listing_still_rejects_the_ashworthholme_decoy_after_widening():
    # Regression guard for the fix above: widening the corroboration region
    # by class/id (needed for jaksandco.co.uk) must not re-admit
    # ashworthholme.co.uk's price-filter <select> -- its own id
    # ("main-minprice-sales") contains the substring "price" and would
    # satisfy the widened selector directly if form controls weren't
    # stripped first.
    recipe = _recipe_from_fixture("ashworthholme.co.uk")
    record = _record_from_fixture(
        "ashworthholme.co.uk",
        "sherbrooke-close.html",
        "https://www.ashworthholme.co.uk/property-details/1/-/sale/sherbrooke-close",
    )
    result = await extract_listing(record, recipe, client=StubClient(price=10_000_000))
    assert result.needs_review is True
    assert any("not corroborated" in issue for issue in result.issues)
    assert result.listing is not None
    assert result.listing.price_gbp is None


async def test_extract_listing_wraps_client_failures_in_extraction_error():
    class _ExplodingMessagesAPI:
        async def create(self, **kwargs):
            raise RuntimeError("connection reset")

    class _ExplodingClient:
        def __init__(self) -> None:
            self.messages = _ExplodingMessagesAPI()

    record = _simple_record()
    recipe = _simple_recipe()
    with pytest.raises(ExtractionError) as excinfo:
        await extract_listing(record, recipe, client=_ExplodingClient())
    assert excinfo.value.url == record.url
    assert isinstance(excinfo.value.cause, RuntimeError)


async def test_extract_listing_on_a_page_with_no_extractable_fields():
    # bentleyhurst fixtures are client-rendered Gatsby shells with no price,
    # address or description in the HTML at all (ruling R27).
    recipe = _recipe_from_fixture("bentleyhurst.co.uk")
    record = _record_from_fixture(
        "bentleyhurst.co.uk",
        "icon-building.html",
        "https://www.bentleyhurst.co.uk/property-for-sale/icon-building",
    )
    result = await extract_listing(record, recipe, client=StubClient())
    assert result.listing is None
    assert result.needs_review is True
    assert result.issues


async def test_extract_listing_happy_path_sets_confidence_and_hash():
    record = _simple_record()
    recipe = _simple_recipe()
    result = await extract_listing(record, recipe, client=StubClient(price=450_000))
    assert result.listing is not None
    assert result.listing.price_gbp == 450_000
    assert result.listing.content_hash == record.content_hash
    assert result.overall_confidence >= 0.8
    assert result.needs_review is False


async def test_extract_listing_rejects_a_rental_page():
    record = _simple_record(html="<html><h1>1 Test St</h1><h2>To Let £1,000 PCM</h2></html>")
    recipe = _simple_recipe()
    result = await extract_listing(
        record,
        recipe,
        client=StubClient(payload={**VALID_PAYLOAD, "price_raw": "To Let £1,000 PCM"}),
    )
    assert result.listing is None
    assert result.needs_review is True


async def test_extract_listing_rejects_model_output_that_fails_schema_validation():
    record = _simple_record()
    recipe = _simple_recipe()
    bad_payload = {**VALID_PAYLOAD, "property_type": "spaceship", "tenure": "spaceship"}
    result = await extract_listing(record, recipe, client=StubClient(payload=bad_payload))
    assert result.listing is None
    assert result.needs_review is True
    assert result.issues


def test_audit_row_records_model_and_prompt_and_recipe_versions():
    record = _simple_record()
    recipe = _simple_recipe()
    listing = make_listing(content_hash=record.content_hash)
    result = ExtractionResult(
        listing=listing,
        field_confidence={"price_gbp": 1.0},
        overall_confidence=0.9,
        needs_review=False,
        issues=[],
    )
    row = audit_row(
        record,
        recipe,
        result,
        model="claude-sonnet-5",
        input_tokens=1,
        output_tokens=1,
        cost_usd=0.0,
    )
    assert row["prompt_version"] == EXTRACT_PROMPT_VERSION
    assert row["recipe_version"] == recipe.version
    assert row["html_hash"] == record.content_hash
    assert row["schema_valid"] is True
    assert row["needs_review"] is False


def test_audit_row_flags_an_unreviewed_rejected_extraction():
    record = _simple_record()
    recipe = _simple_recipe()
    result = ExtractionResult(
        listing=None, overall_confidence=0.0, needs_review=True, issues=["no corroboration"]
    )
    row = audit_row(
        record,
        recipe,
        result,
        model="claude-sonnet-5",
        input_tokens=1,
        output_tokens=1,
        cost_usd=0.0,
    )
    assert row["schema_valid"] is False
    assert row["needs_review"] is True


def test_record_extraction_persists_a_readable_audit_row(tmp_path):
    conn = init_db(tmp_path / "nimbus.db")
    record = _simple_record()
    recipe = _simple_recipe()
    listing = make_listing(content_hash=record.content_hash)
    result = ExtractionResult(listing=listing, overall_confidence=0.9, needs_review=False)

    record_extraction(
        conn,
        record,
        recipe,
        result,
        model="claude-sonnet-5",
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.001,
    )

    count = conn.execute(
        "SELECT COUNT(*) FROM extraction_audit WHERE url = ?", (record.url,)
    ).fetchone()[0]
    assert count == 1


@pytest.mark.parametrize(
    "domain,filename",
    [
        ("ashworthholme.co.uk", "rossett-ave-altrincham.html"),
        ("ashworthholme.co.uk", "sherbrooke-close.html"),
        ("bentleyhurst.co.uk", "icon-building.html"),
        ("bentleyhurst.co.uk", "parkers-apartments.html"),
        ("jaksandco.co.uk", "grenfell-road-didsbury.html"),
        ("jaksandco.co.uk", "kingsway-manchester.html"),
    ],
)
async def test_extract_listing_never_crashes_on_any_real_fixture(domain, filename):
    """R27, generalised: whatever the model returns, extraction on a real
    page must come back as an `ExtractionResult`, never raise, and never
    hand back a `Listing` whose `content_hash` doesn't match the record it
    came from."""
    recipe = _recipe_from_fixture(domain)
    record = _record_from_fixture(domain, filename, f"https://www.{domain}/property/{filename}")
    result = await extract_listing(record, recipe, client=StubClient())
    assert isinstance(result, ExtractionResult)
    if result.listing is not None:
        assert result.listing.content_hash == record.content_hash
    else:
        assert result.needs_review is True
        assert result.issues
