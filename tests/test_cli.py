"""The `nimbus` CLI. `demo --offline` is the one path this file tests most
carefully: it is the presentation path (pre-flight ruling R7), and must make
no network call and no agent call at all, succeed even if `discover_site`
would raise, produce identical output on every run, and disclose in its own
printed narration (not only in source comments) that step 5's repair is a
stubbed fixture rather than a live re-discovery.

Every other command (`onboard`, `crawl`, `heal`) does touch the live network
and, for `onboard`/`heal`, the live discovery agent -- this file never runs
their real bodies, so running this suite never spends real API credits.
What it does cover, all without touching the network: `crawl`'s no-saved-
recipe error path; `onboard`/`heal`'s no-API-key error path (free to test --
`discover_site` itself raises `DiscoveryError` before any network call when
no key is configured); and, most importantly, `crawl`'s own
`allow_mass_withdrawal` decision against a mocked `CrawlOutcome` representing
a mostly-failed crawl, with the real `apply_delta` and the real
`should_allow_mass_withdrawal` both left unmocked.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from conftest import make_recipe
from typer.testing import CliRunner

from nimbus.cli import app
from nimbus.models import CrawlOutcome, Listing, PropertyType, Tenure
from nimbus.storage import init_db, list_listings, save_recipe, upsert_listing

runner = CliRunner()

DOMAIN = "ashworthholme.co.uk"


def _seed_db(path: Path) -> None:
    conn = init_db(path)
    save_recipe(conn, make_recipe(domain=DOMAIN))
    upsert_listing(
        conn,
        Listing(
            source_domain=DOMAIN,
            source_url="https://www.ashworthholme.co.uk/property-details/1/-/sale/example",
            address="1 Example Street, Sale",
            postcode="M33 1AA",
            price_gbp=450_000,
            bedrooms=3,
            bathrooms=1,
            property_type=PropertyType.DETACHED,
            tenure=Tenure.FREEHOLD,
            content_hash="deadbeef",
        ),
    )


def test_cli_exposes_all_commands():
    out = runner.invoke(app, ["--help"]).output
    for cmd in ("onboard", "crawl", "heal", "report", "demo"):
        assert cmd in out


def test_report_renders_seeded_database_without_network(tmp_path):
    db = tmp_path / "nimbus.db"
    _seed_db(db)

    result = runner.invoke(app, ["--db", str(db), "report"])

    assert result.exit_code == 0, result.output
    assert "ashworthholme.co.uk" in result.output
    assert "£450,000" in result.output


def test_report_filters_to_one_domain(tmp_path):
    db = tmp_path / "nimbus.db"
    _seed_db(db)
    conn = init_db(db)
    save_recipe(conn, make_recipe(domain="jaksandco.co.uk"))

    result = runner.invoke(app, ["--db", str(db), "report", "--domain", DOMAIN])

    assert result.exit_code == 0, result.output
    assert "ashworthholme.co.uk" in result.output
    assert "jaksandco.co.uk" not in result.output


def test_report_on_empty_database_says_so(tmp_path):
    db = tmp_path / "nimbus.db"
    init_db(db)

    result = runner.invoke(app, ["--db", str(db), "report"])

    assert result.exit_code == 0
    assert "no domains onboarded" in result.output.lower()


def test_crawl_without_a_saved_recipe_exits_cleanly_and_touches_nothing(tmp_path):
    db = tmp_path / "nimbus.db"
    init_db(db)

    result = runner.invoke(app, ["--db", str(db), "crawl", "unknown-example.co.uk"])

    assert result.exit_code == 1
    assert "onboard" in result.output.lower()


def test_demo_runs_offline_from_fixtures():
    result = runner.invoke(app, ["demo", "--offline"])

    assert result.exit_code == 0, result.output
    assert "recipe repaired" in result.output.lower()


def test_demo_offline_makes_no_agent_call(monkeypatch):
    calls: list[str] = []

    async def _boom(domain: str, *, settings, max_turns: int = 20):
        calls.append(domain)
        raise AssertionError("discover_site must never be called by `nimbus demo --offline`")

    monkeypatch.setattr("nimbus.discovery.discover_site", _boom)
    monkeypatch.setattr("nimbus.heal.discover_site", _boom)

    result = runner.invoke(app, ["demo", "--offline"])

    assert result.exit_code == 0, result.output
    assert calls == []


def test_demo_offline_is_byte_identical_across_runs():
    first = runner.invoke(app, ["demo", "--offline"])
    second = runner.invoke(app, ["demo", "--offline"])

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert first.output == second.output


def test_demo_offline_narrates_the_health_collapse_and_recovery():
    result = runner.invoke(app, ["demo", "--offline"])

    assert result.exit_code == 0, result.output
    out = result.output
    assert "schema_valid_rate=0%" in out
    assert "schema_valid_rate=100%" in out
    assert "v1 -> v2" in out


def test_demo_offline_discloses_that_step_5s_repair_is_stubbed():
    """Someone watching the presentation must not come away believing the
    agent just inferred the fix for the simulated redesign -- the stub is
    disclosed in source comments, the ADR, the README, and heal_site's own
    docstring, but none of those are visible during a live run, so the
    printed narration itself must say so."""
    result = runner.invoke(app, ["demo", "--offline"])

    assert result.exit_code == 0, result.output
    out = result.output.lower()
    assert "stubbed" in out
    assert "nimbus heal" in out


def test_onboard_without_api_key_fails_cleanly_without_network(tmp_path, monkeypatch):
    """discover_site itself raises DiscoveryError before any network call
    when no key is configured -- the free path, costing nothing to test."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    db = tmp_path / "nimbus.db"
    init_db(db)

    result = runner.invoke(app, ["--db", str(db), "onboard", "some-example.co.uk"])

    assert result.exit_code == 1
    assert "anthropic_api_key" in result.output.lower()


def test_heal_without_api_key_fails_cleanly_without_network(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    db = tmp_path / "nimbus.db"
    _seed_db(db)

    result = runner.invoke(app, ["--db", str(db), "heal", DOMAIN])

    assert result.exit_code == 1
    assert "anthropic_api_key" in result.output.lower()


def test_crawl_never_derives_allow_mass_withdrawal_from_fetched_greater_than_zero_alone(
    tmp_path, monkeypatch
):
    """A crawl that selected 1075 URLs and fetched exactly 1 of them must
    not authorise withdrawing the other 1074 previously stored listings
    just because `outcome.fetched > 0` is trivially true -- the exact
    failed-crawl-looks-like-a-sell-off shape `nimbus.delta.apply_delta`'s
    own guard exists to catch, which a bare `fetched > 0` reopens at this
    integration layer. Mocks only the crawl outcome and the two I/O
    boundaries (robots resolution, the Anthropic client) `crawl` builds
    for real -- the actual `allow_mass_withdrawal` decision and the real
    `apply_delta` both run unmocked."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))

    db = tmp_path / "nimbus.db"
    conn = init_db(db)
    save_recipe(conn, make_recipe(domain=DOMAIN))
    stored_urls = [f"https://www.{DOMAIN}/property-details/{n}" for n in range(10)]
    for n, url in enumerate(stored_urls):
        upsert_listing(
            conn,
            Listing(
                source_domain=DOMAIN,
                source_url=url,
                address=f"{n} Example Street, Sale",
                price_gbp=100_000 + n,
                property_type=PropertyType.DETACHED,
                tenure=Tenure.FREEHOLD,
                content_hash=f"hash{n}",
            ),
        )

    mostly_failed_outcome = CrawlOutcome(
        domain=DOMAIN,
        records=[],
        candidates=1075,
        selected=1075,
        fetched=1,
        skipped_robots=0,
        skipped_errors=1074,
    )

    async def fake_robots_for(self, domain):
        return SimpleNamespace(crawl_delay=0.0)

    async def fake_crawl_site(recipe, *, fetcher, crawl_delay, limit=None):
        return mostly_failed_outcome

    monkeypatch.setattr("nimbus.fetch.fetcher.Fetcher.robots_for", fake_robots_for)
    monkeypatch.setattr("nimbus.cli.crawl_site", fake_crawl_site)

    result = runner.invoke(app, ["--db", str(db), "crawl", DOMAIN])

    assert result.exit_code == 0, result.output
    remaining = {listing.source_url for listing in list_listings(conn, DOMAIN)}
    assert remaining == set(stored_urls), "a mostly-failed crawl must not withdraw anything"
    assert "withdrawn=0" in result.output
    assert "withdrawal suppressed" in result.output.lower()
