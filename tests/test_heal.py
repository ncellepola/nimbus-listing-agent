"""Health monitoring and recipe self-heal.

No test here touches the network or calls an LLM: `discover_site` is
always monkeypatched (the live path is `nimbus.discovery`'s own job to
test, under `@pytest.mark.live`), and every extraction below runs against
`StubClient`, exactly as `tests/test_extractor.py` does.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import make_recipe

import nimbus.heal as heal
from nimbus.extractor import extract_listing, record_extraction
from nimbus.heal import DEFAULT_HEAL_THRESHOLD, heal_site, needs_heal, site_health
from nimbus.models import CrawlRecord, FieldHint, RejectionReason, SiteHealth, SiteRecipe
from nimbus.storage import init_db, insert_audit, save_recipe

FIXTURES_DIR = Path(__file__).parent / "fixtures"
DOMAIN = "ashworthholme.co.uk"


def _audit_url(n: int) -> str:
    return f"https://www.{DOMAIN}/property-details/{n}/-/sale/example"


def test_site_health_computes_rates_over_window(tmp_path):
    conn = init_db(tmp_path / "nimbus.db")
    for n in range(10):
        insert_audit(
            conn,
            url=_audit_url(n),
            model="claude-sonnet-5",
            prompt_version="v1",
            recipe_version=1,
            html_hash=f"hash{n}",
            input_tokens=10,
            output_tokens=5,
            cost_usd=0.001,
            schema_valid=n % 3 != 0,
            needs_review=n % 2 == 0,
        )

    h = site_health(conn, DOMAIN, window=10)

    assert h.sample_size == 10
    assert 0.0 <= h.schema_valid_rate <= 1.0
    assert 0.0 <= h.needs_review_rate <= 1.0


def test_site_health_only_counts_the_given_domain(tmp_path):
    conn = init_db(tmp_path / "nimbus.db")
    insert_audit(
        conn,
        url="https://www.jaksandco.co.uk/property/1",
        model="m",
        prompt_version="v1",
        recipe_version=1,
        html_hash="h",
        input_tokens=1,
        output_tokens=1,
        cost_usd=0.0,
        schema_valid=False,
        needs_review=True,
    )

    h = site_health(conn, DOMAIN, window=10)

    assert h.sample_size == 0
    assert h.schema_valid_rate == 0.0


def test_site_health_window_caps_sample_to_the_most_recent_rows(tmp_path):
    conn = init_db(tmp_path / "nimbus.db")
    for n in range(5):
        insert_audit(
            conn,
            url=_audit_url(n),
            model="m",
            prompt_version="v1",
            recipe_version=1,
            html_hash="h",
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.0,
            schema_valid=False,
            needs_review=True,
        )
    for n in range(5, 8):
        insert_audit(
            conn,
            url=_audit_url(n),
            model="m",
            prompt_version="v1",
            recipe_version=1,
            html_hash="h",
            input_tokens=1,
            output_tokens=1,
            cost_usd=0.0,
            schema_valid=True,
            needs_review=False,
        )

    h = site_health(conn, DOMAIN, window=3)

    # Only the 3 most recently inserted rows (the healthy ones) are in the
    # sample; the 5 broken rows before them are outside the window.
    assert h.sample_size == 3
    assert h.schema_valid_rate == 1.0


def test_needs_heal_triggers_below_threshold():
    healthy = SiteHealth(
        domain=DOMAIN, window=50, schema_valid_rate=0.95, needs_review_rate=0.1, sample_size=50
    )
    unhealthy = SiteHealth(
        domain=DOMAIN, window=50, schema_valid_rate=0.4, needs_review_rate=0.1, sample_size=50
    )

    assert needs_heal(unhealthy) is True
    assert needs_heal(healthy) is False
    assert needs_heal(unhealthy, threshold=DEFAULT_HEAL_THRESHOLD) is True


def test_needs_heal_withholds_judgement_with_no_sample():
    empty = SiteHealth(
        domain=DOMAIN, window=50, schema_valid_rate=0.0, needs_review_rate=0.0, sample_size=0
    )

    assert needs_heal(empty) is False


def _settings_with_key(tmp_path):
    from nimbus.config import Settings

    return Settings(
        anthropic_api_key="sk-ant-test-not-real",
        cache_dir=tmp_path / "cache",
        db_path=tmp_path / "nimbus.db",
    )


def _redesigned_recipe(previous: SiteRecipe) -> SiteRecipe:
    """Stands in for what a real `discover_site` run would come back with
    after the site's redesign: the price and address are no longer inside
    any heading, so the healed recipe's own field_hints now point at the
    new container classes directly."""
    return previous.model_copy(
        update={
            "version": 1,
            "discovered_at": datetime.now(UTC),
            "field_hints": [
                FieldHint(field="address", css_selector=".listing-heading", notes="post-redesign"),
                FieldHint(field="price", css_selector=".listing-status", notes="post-redesign"),
            ],
            "notes": "re-discovered after a site redesign moved price/address out of h1/h2",
        }
    )


async def test_heal_site_bumps_version_and_reports_diff(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "nimbus.db")
    previous = make_recipe(domain=DOMAIN)
    save_recipe(conn, previous)  # v1

    healed_payload = _redesigned_recipe(previous)

    async def fake_discover_site(domain, *, settings, max_turns=20):
        assert domain == DOMAIN
        return healed_payload

    monkeypatch.setattr(heal, "discover_site", fake_discover_site)

    recipe, diff = await heal_site(conn, DOMAIN, settings=_settings_with_key(tmp_path))

    assert recipe.version == previous.version + 1
    assert diff
    assert any("field_hints" in line for line in diff)


async def test_heal_site_reports_no_previous_recipe_on_first_discovery(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "nimbus.db")
    first_recipe = make_recipe(domain=DOMAIN)

    async def fake_discover_site(domain, *, settings, max_turns=20):
        return first_recipe

    monkeypatch.setattr(heal, "discover_site", fake_discover_site)

    recipe, diff = await heal_site(conn, DOMAIN, settings=_settings_with_key(tmp_path))

    assert recipe.version == 1
    assert any("no previous recipe" in line for line in diff)


# --------------------------------------------------------------------------
# End-to-end: health measurably collapses against a simulated site
# redesign (tests/fixtures/pages/mutated/) and recovers once heal_site
# re-points the recipe's field_hints at the redesigned markup. Every
# extraction below runs against StubClient -- the only live-network path
# anywhere in this module is the one DiscoveryError/live test would be,
# and there is none here.
# --------------------------------------------------------------------------


class _FakeTextBlock:
    type = "text"


class _FakeToolUseBlock:
    type = "tool_use"

    def __init__(self, input: dict) -> None:
        self.input = input
        self.name = "emit_listing"
        self.id = "toolu_1"


class _FakeUsage:
    input_tokens = 100
    output_tokens = 50


class _FakeMessage:
    def __init__(self, content: list) -> None:
        self.content = content
        self.usage = _FakeUsage()


class _FakeMessagesAPI:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def create(self, **kwargs):
        return _FakeMessage(content=[_FakeToolUseBlock(input=dict(self._payload))])


class StubClient:
    """Returns a canned `emit_listing` tool call. Never touches the
    network; see tests/test_extractor.py for the pattern this mirrors."""

    def __init__(self, *, address: str, price: int) -> None:
        payload = {
            "address": address,
            "postcode": None,
            "price_raw": f"For Sale £{price:,}",
            "bedrooms": None,
            "bathrooms": None,
            "property_type": None,
            "tenure": None,
            "floor_area_sqft": None,
            "description": None,
            "agent_name": None,
            "listed_date": None,
            "external_id": None,
        }
        self.messages = _FakeMessagesAPI(payload)


def _record_from(path: Path, url: str) -> CrawlRecord:
    html = path.read_text(encoding="utf-8", errors="ignore")
    return CrawlRecord(
        url=url,
        html=html,
        from_cache=False,
        fetched_at=datetime.now(UTC),
        content_hash=hashlib.sha256(html.encode("utf-8")).hexdigest(),
    )


def _real_recipe() -> SiteRecipe:
    import json

    data = json.loads((FIXTURES_DIR / "recipes" / f"{DOMAIN}.json").read_text())
    return SiteRecipe(**data)


BASELINE_PAGES = [
    ("ashworthholme.co.uk/sherbrooke-close.html", "sherbrooke-close", 850_000),
    ("ashworthholme.co.uk/rossett-ave-altrincham.html", "rossett-ave", 650_000),
]

MUTATED_PAGES = [
    ("mutated/sherbrooke-close.html", "sherbrooke-close", 850_000),
    ("mutated/rossett-ave-altrincham.html", "rossett-ave", 650_000),
    ("mutated/flat-6-1.html", "flat-6-1", 850_000),
]


async def _extract_and_record(
    conn, recipe: SiteRecipe, relpath: str, slug: str, price: int
) -> None:
    record = _record_from(
        FIXTURES_DIR / "pages" / relpath,
        f"https://www.{DOMAIN}/property-details/{slug}",
    )
    result = await extract_listing(
        record, recipe, client=StubClient(address=f"{slug}, Sale", price=price)
    )
    record_extraction(
        conn,
        record,
        recipe,
        result,
        model="claude-sonnet-5",
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.001,
    )


async def test_health_collapses_on_mutated_pages_and_recovers_after_heal(tmp_path, monkeypatch):
    conn = init_db(tmp_path / "nimbus.db")
    old_recipe = _real_recipe()
    save_recipe(conn, old_recipe)  # v1

    # Baseline: the real, unmutated fixtures extract cleanly under the
    # original recipe (the same two files tests/test_extractor.py proves
    # corroborate correctly).
    for relpath, slug, price in BASELINE_PAGES:
        await _extract_and_record(conn, old_recipe, relpath, slug, price)

    health_before = site_health(conn, DOMAIN, window=len(BASELINE_PAGES))
    assert health_before.schema_valid_rate == 1.0
    assert needs_heal(health_before) is False

    # Simulated site redesign: the price/address moved out of h1/h2 into
    # renamed containers the old recipe's field_hints (css_selector "h1"/
    # "h2") no longer reach. The old recipe's own corroboration region is
    # now empty for every one of these pages, so extraction is rejected
    # before a model is even called -- schema_valid=False.
    for relpath, slug, price in MUTATED_PAGES:
        await _extract_and_record(conn, old_recipe, relpath, slug, price)

    health_after_redesign = site_health(conn, DOMAIN, window=len(MUTATED_PAGES))
    assert health_after_redesign.schema_valid_rate == 0.0
    assert needs_heal(health_after_redesign) is True

    # Self-heal: discover_site is monkeypatched (no live agent, no
    # network) to return what a real re-discovery of the redesigned site
    # would produce -- field_hints pointing at the new containers.
    healed_payload = _redesigned_recipe(old_recipe)

    async def fake_discover_site(domain, *, settings, max_turns=20):
        return healed_payload

    monkeypatch.setattr(heal, "discover_site", fake_discover_site)

    new_recipe, diff = await heal_site(conn, DOMAIN, settings=_settings_with_key(tmp_path))
    assert new_recipe.version == old_recipe.version + 1
    assert diff

    # Re-extracting the same mutated pages under the healed recipe
    # recovers: the new field_hints reach the redesigned containers
    # directly, by class, regardless of tag name.
    for relpath, slug, price in MUTATED_PAGES:
        await _extract_and_record(conn, new_recipe, relpath, slug, price)

    health_after_heal = site_health(conn, DOMAIN, window=len(MUTATED_PAGES))
    assert health_after_heal.schema_valid_rate == 1.0
    assert needs_heal(health_after_heal) is False


@pytest.mark.live
async def test_heal_site_against_the_real_agent(tmp_path):
    from nimbus.config import Settings

    conn = init_db(tmp_path / "nimbus.db")
    settings = Settings()
    recipe, diff = await heal_site(conn, DOMAIN, settings=settings)

    assert recipe.version == 1
    assert diff


# --------------------------------------------------------------------------
# A correctly-rejected non-sale page is not drift. `extract_listing`
# deliberately routes Sold-STC and To-Let pages through the model so
# `is_residential_sale` can reject them; recording that as
# `schema_valid=False` made a *correct* recipe on the demo's own site read
# as a collapsed one, which with a scheduler would call the discovery
# agent nightly, forever, inverting the whole cost argument.
# --------------------------------------------------------------------------

# The real ashworthholme.co.uk mix, measured over all 18 pages this repo
# has cached for that domain: 6 "For Sale", 11 "Sold Subject to Contract",
# 1 "To Let". Before the fix, that produced schema_valid_rate=0.333 and
# needs_heal=True against a recipe that was working perfectly.
_ASHWORTH_FOR_SALE = 6
_ASHWORTH_SOLD_STC = 11
_ASHWORTH_TO_LET = 1


def _record_non_sale(conn, n: int) -> None:
    insert_audit(
        conn,
        url=_audit_url(n),
        model="claude-sonnet-5",
        prompt_version="v1",
        recipe_version=1,
        html_hash=f"hash{n}",
        input_tokens=0,
        output_tokens=0,
        cost_usd=0.0,
        schema_valid=False,
        needs_review=True,
        rejection_reason=RejectionReason.NOT_RESIDENTIAL_SALE.value,
    )


def _record_sale(conn, n: int) -> None:
    insert_audit(
        conn,
        url=_audit_url(n),
        model="claude-sonnet-5",
        prompt_version="v1",
        recipe_version=1,
        html_hash=f"hash{n}",
        input_tokens=0,
        output_tokens=0,
        cost_usd=0.0,
        schema_valid=True,
        needs_review=False,
        rejection_reason=None,
    )


def test_deliberate_non_sale_exclusions_do_not_read_as_drift(tmp_path):
    """The exact mix of this repo's own 18 cached ashworthholme.co.uk
    pages, against a correct recipe: every page was handled correctly, so
    the site must not be reported as needing heal."""
    conn = init_db(tmp_path / "nimbus.db")
    n = 0
    for _ in range(_ASHWORTH_FOR_SALE):
        _record_sale(conn, n)
        n += 1
    for _ in range(_ASHWORTH_SOLD_STC + _ASHWORTH_TO_LET):
        _record_non_sale(conn, n)
        n += 1

    health = site_health(conn, DOMAIN, window=50)

    assert health.deliberate_exclusions == _ASHWORTH_SOLD_STC + _ASHWORTH_TO_LET
    assert health.sample_size == _ASHWORTH_FOR_SALE
    assert health.schema_valid_rate == 1.0
    assert needs_heal(health) is False


def test_a_real_extraction_failure_still_reads_as_drift(tmp_path):
    """The other half of the same change: excluding deliberate exclusions
    must not blunt the signal the heal subsystem exists for. A page that
    failed for any reason other than "this is not a sale" still counts."""
    conn = init_db(tmp_path / "nimbus.db")
    for n in range(10):
        insert_audit(
            conn,
            url=_audit_url(n),
            model="claude-sonnet-5",
            prompt_version="v1",
            recipe_version=1,
            html_hash=f"hash{n}",
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            schema_valid=False,
            needs_review=True,
            rejection_reason=RejectionReason.NO_CORROBORATION_REGION.value,
        )

    health = site_health(conn, DOMAIN, window=50)

    assert health.deliberate_exclusions == 0
    assert health.sample_size == 10
    assert health.schema_valid_rate == 0.0
    assert needs_heal(health) is True


def test_a_site_of_nothing_but_sold_stock_is_not_reported_as_needing_heal(tmp_path):
    """Every page correctly excluded leaves no evidence either way --
    the same reasoning `sample_size == 0` already uses. Reporting
    needs_heal here would send the agent back in against a site whose
    recipe is working perfectly and which simply has no stock on."""
    conn = init_db(tmp_path / "nimbus.db")
    for n in range(8):
        _record_non_sale(conn, n)

    health = site_health(conn, DOMAIN, window=50)

    assert health.sample_size == 0
    assert health.deliberate_exclusions == 8
    assert needs_heal(health) is False
