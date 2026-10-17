"""The `nimbus` CLI. `demo --offline` is the one path this file tests most
carefully: it is the presentation path (pre-flight ruling R7), and must make
no network call and no agent call at all, succeed even if `discover_site`
would raise, and produce identical output on every run.

Every other command (`onboard`, `crawl`, `heal`) does touch the live network
and, for `onboard`/`heal`, the live discovery agent -- this file exercises
only their early, no-network error paths (a missing recipe, nothing
onboarded yet), never their real bodies, so running this suite never spends
real API credits.
"""

from __future__ import annotations

from pathlib import Path

from conftest import make_recipe
from typer.testing import CliRunner

from nimbus.cli import app
from nimbus.models import Listing, PropertyType, Tenure
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
