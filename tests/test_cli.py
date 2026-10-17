"""The `nimbus` CLI. `demo --offline` is the one path this file tests most
carefully: it is the presentation path, and must make
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

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from conftest import make_recipe
from typer.testing import CliRunner

from nimbus.cli import app
from nimbus.fetch.backends import CrawlbaseBackend
from nimbus.models import (
    CrawlOutcome,
    CrawlRecord,
    ExtractionResult,
    Listing,
    PropertyType,
    RejectionReason,
    Tenure,
)
from nimbus.storage import init_db, save_recipe, upsert_listing

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
        attempted=1075,
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
    # Asserted on the report, not on `list_listings`: a suppressed and an
    # applied withdrawal both leave every row in place, so "every stored
    # URL is still there" passes just as happily with the guard removed.
    # Only the report distinguishes the two outcomes.
    assert "withdrawn=0" in result.output
    assert "withdrawal suppressed" in result.output.lower()


def test_crawl_does_not_withdraw_when_a_full_crawl_extracts_nothing(tmp_path, monkeypatch):
    """The redesign shape, end to end, and the composition two of this
    repo's own passing tests left open: every URL still resolves so the
    crawl fetches 195/195 and scores a perfect fetch ratio, then every
    extraction fails against markup the recipe no longer matches. The
    fetch-completeness guard sees a flawless run; `listings` is empty; and
    without the extraction signal the whole domain gets withdrawn -- in
    precisely the scenario the heal subsystem exists for."""
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

    records = [
        CrawlRecord(
            url=url,
            html="<html><div>redesigned, unparseable by this recipe</div></html>",
            from_cache=False,
            fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
            content_hash=f"new{n}",
        )
        for n, url in enumerate(stored_urls)
    ]
    full_crawl = CrawlOutcome(
        domain=DOMAIN,
        records=records,
        candidates=10,
        selected=10,
        attempted=10,
        fetched=10,
        skipped_robots=0,
        skipped_errors=0,
    )

    async def fake_robots_for(self, domain):
        return SimpleNamespace(crawl_delay=0.0)

    async def fake_crawl_site(recipe, *, fetcher, crawl_delay, limit=None):
        return full_crawl

    async def fake_extract_listing(record, recipe, *, client):
        return ExtractionResult(
            listing=None,
            overall_confidence=0.0,
            needs_review=True,
            issues=["page has no heading or price-hint content to corroborate against"],
            rejection_reason=RejectionReason.NO_CORROBORATION_REGION,
        )

    monkeypatch.setattr("nimbus.fetch.fetcher.Fetcher.robots_for", fake_robots_for)
    monkeypatch.setattr("nimbus.cli.crawl_site", fake_crawl_site)
    monkeypatch.setattr("nimbus.cli.extract_listing", fake_extract_listing)

    result = runner.invoke(app, ["--db", str(db), "crawl", DOMAIN])

    assert result.exit_code == 0, result.output
    # The crawl itself looked perfect -- that is the whole point.
    assert "10 selected, 10 attempted, 10 fetched" in result.output
    assert "withdrawn=0" in result.output
    assert "withdrawal suppressed" in result.output.lower()


def test_crawl_skips_extraction_for_a_page_whose_bytes_have_not_changed(tmp_path, monkeypatch):
    """`content_hash` was computed, stored on both `listings` and
    `extraction_audit`, and documented as the thing that avoids
    re-extraction -- and never consulted, so the nightly run paid for one
    model call per listing page per night regardless. This is the test
    for the claim the README makes."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))

    db = tmp_path / "nimbus.db"
    conn = init_db(db)
    save_recipe(conn, make_recipe(domain=DOMAIN))

    url = f"https://www.{DOMAIN}/property-details/1"
    html = "<html><h1>1 Example Street, Sale</h1><h2>For Sale £450,000</h2></html>"
    content_hash = hashlib.sha256(html.encode("utf-8")).hexdigest()
    upsert_listing(
        conn,
        Listing(
            source_domain=DOMAIN,
            source_url=url,
            address="1 Example Street, Sale",
            price_gbp=450_000,
            property_type=PropertyType.DETACHED,
            tenure=Tenure.FREEHOLD,
            content_hash=content_hash,
        ),
    )

    outcome = CrawlOutcome(
        domain=DOMAIN,
        records=[
            CrawlRecord(
                url=url,
                html=html,
                from_cache=False,
                fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
                content_hash=content_hash,
            )
        ],
        candidates=1,
        selected=1,
        attempted=1,
        fetched=1,
        skipped_robots=0,
        skipped_errors=0,
    )

    async def fake_robots_for(self, domain):
        return SimpleNamespace(crawl_delay=0.0)

    async def fake_crawl_site(recipe, *, fetcher, crawl_delay, limit=None):
        return outcome

    async def must_not_be_called(record, recipe, *, client):
        raise AssertionError("an unchanged page must not be re-extracted")

    monkeypatch.setattr("nimbus.fetch.fetcher.Fetcher.robots_for", fake_robots_for)
    monkeypatch.setattr("nimbus.cli.crawl_site", fake_crawl_site)
    monkeypatch.setattr("nimbus.cli.extract_listing", must_not_be_called)

    result = runner.invoke(app, ["--db", str(db), "crawl", DOMAIN])

    assert result.exit_code == 0, result.output
    assert "1 unchanged" in result.output
    # Skipped, not dropped: the listing must still reach apply_delta, or
    # "we did not re-read it" would be recorded as "it is gone".
    assert "unchanged=1" in result.output
    assert "withdrawn=0" in result.output


def test_crawl_re_extracts_a_page_whose_bytes_changed(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))

    db = tmp_path / "nimbus.db"
    conn = init_db(db)
    save_recipe(conn, make_recipe(domain=DOMAIN))

    url = f"https://www.{DOMAIN}/property-details/1"
    upsert_listing(
        conn,
        Listing(
            source_domain=DOMAIN,
            source_url=url,
            address="1 Example Street, Sale",
            price_gbp=450_000,
            property_type=PropertyType.DETACHED,
            tenure=Tenure.FREEHOLD,
            content_hash="yesterdays-hash",
        ),
    )

    outcome = CrawlOutcome(
        domain=DOMAIN,
        records=[
            CrawlRecord(
                url=url,
                html="<html>reduced</html>",
                from_cache=False,
                fetched_at=datetime(2026, 1, 1, tzinfo=UTC),
                content_hash="todays-different-hash",
            )
        ],
        candidates=1,
        selected=1,
        attempted=1,
        fetched=1,
        skipped_robots=0,
        skipped_errors=0,
    )
    calls: list[str] = []

    async def fake_robots_for(self, domain):
        return SimpleNamespace(crawl_delay=0.0)

    async def fake_crawl_site(recipe, *, fetcher, crawl_delay, limit=None):
        return outcome

    async def fake_extract_listing(record, recipe, *, client):
        calls.append(record.url)
        return ExtractionResult(
            listing=Listing(
                source_domain=DOMAIN,
                source_url=record.url,
                address="1 Example Street, Sale",
                price_gbp=425_000,
                property_type=PropertyType.DETACHED,
                tenure=Tenure.FREEHOLD,
                content_hash=record.content_hash,
            ),
            overall_confidence=1.0,
        )

    monkeypatch.setattr("nimbus.fetch.fetcher.Fetcher.robots_for", fake_robots_for)
    monkeypatch.setattr("nimbus.cli.crawl_site", fake_crawl_site)
    monkeypatch.setattr("nimbus.cli.extract_listing", fake_extract_listing)

    result = runner.invoke(app, ["--db", str(db), "crawl", DOMAIN])

    assert result.exit_code == 0, result.output
    assert calls == [url]
    assert "price_changed=1" in result.output


def test_crawl_writes_a_crawl_runs_row(tmp_path, monkeypatch):
    """`crawl_runs` was created by the schema and never written to, while
    four places in the codebase justified `CrawlOutcome`'s counts by
    saying an orchestrator needs them to write a truthful row."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))

    db = tmp_path / "nimbus.db"
    conn = init_db(db)
    save_recipe(conn, make_recipe(domain=DOMAIN))

    outcome = CrawlOutcome(
        domain=DOMAIN,
        records=[],
        candidates=7,
        selected=4,
        attempted=4,
        fetched=3,
        skipped_robots=1,
        skipped_errors=0,
    )

    async def fake_robots_for(self, domain):
        return SimpleNamespace(crawl_delay=0.0)

    async def fake_crawl_site(recipe, *, fetcher, crawl_delay, limit=None):
        return outcome

    monkeypatch.setattr("nimbus.fetch.fetcher.Fetcher.robots_for", fake_robots_for)
    monkeypatch.setattr("nimbus.cli.crawl_site", fake_crawl_site)

    result = runner.invoke(app, ["--db", str(db), "crawl", DOMAIN])

    assert result.exit_code == 0, result.output
    rows = init_db(db).execute("SELECT * FROM crawl_runs").fetchall()
    assert len(rows) == 1
    assert rows[0]["domain"] == DOMAIN
    assert rows[0]["pages_fetched"] == 3
    assert rows[0]["finished_at"]


def test_crawl_uses_the_configured_fetch_backend(tmp_path, monkeypatch):
    """ADR-0005 and .env.example both present FETCH_BACKEND as the live
    switch for pointing a problem site at a paid service "without
    touching Fetcher" -- but only mcp_server.main() read it, so the crawl
    path hardcoded httpx and the setting silently did nothing."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("FETCH_BACKEND", "crawlbase")
    monkeypatch.setenv("CRAWLBASE_TOKEN", "tok-not-real")

    db = tmp_path / "nimbus.db"
    conn = init_db(db)
    save_recipe(conn, make_recipe(domain=DOMAIN))

    seen: list[object] = []

    async def fake_robots_for(self, domain):
        seen.append(self.backend)
        return SimpleNamespace(crawl_delay=0.0)

    async def fake_crawl_site(recipe, *, fetcher, crawl_delay, limit=None):
        return CrawlOutcome(
            domain=DOMAIN,
            records=[],
            candidates=0,
            selected=0,
            attempted=0,
            fetched=0,
            skipped_robots=0,
            skipped_errors=0,
        )

    monkeypatch.setattr("nimbus.fetch.fetcher.Fetcher.robots_for", fake_robots_for)
    monkeypatch.setattr("nimbus.cli.crawl_site", fake_crawl_site)

    result = runner.invoke(app, ["--db", str(db), "crawl", DOMAIN])

    assert result.exit_code == 0, result.output
    assert seen and isinstance(seen[0], CrawlbaseBackend)


def test_demo_offline_discloses_that_beat_2s_extraction_is_stubbed():
    """Beat 5 disclosed its stubbed re-discovery in the printed narration;
    beat 2 did not disclose its stubbed extraction client. The principle
    applies twice."""
    result = runner.invoke(app, ["demo", "--offline"])

    assert result.exit_code == 0, result.output
    out = result.output.lower()
    assert "the extraction model is stubbed" in out


def test_demo_offline_never_falls_back_to_a_default_robots_policy(caplog):
    """`robots_for` fails closed now, so an unseeded robots URL shows up
    as a WARNING instead of being silently answered with "allow
    everything". The demo must seed every robots URL it asks for -- both
    the bare registrable domain the crawl-delay lookup uses and the `www.`
    host the page fetches use."""
    with caplog.at_level("WARNING", logger="nimbus.fetch.fetcher"):
        result = runner.invoke(app, ["demo", "--offline"])

    assert result.exit_code == 0, result.output
    assert not [r for r in caplog.records if "robots.txt" in r.getMessage()]
