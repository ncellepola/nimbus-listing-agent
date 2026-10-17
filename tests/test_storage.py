from __future__ import annotations

from datetime import UTC, datetime

from conftest import make_recipe

from nimbus.storage import init_db, insert_audit, load_recipe, load_recipe_version, save_recipe

EXPECTED_TABLES = {
    "sites",
    "recipes",
    "listings",
    "listing_history",
    "extraction_audit",
    "crawl_runs",
}


def test_init_db_creates_all_tables_and_recipe_versions_increment(tmp_path):
    conn = init_db(tmp_path / "nimbus.db")

    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    table_names = {row[0] for row in rows}
    assert EXPECTED_TABLES <= table_names

    recipe = make_recipe(domain="ashworthholme.co.uk")

    assert save_recipe(conn, recipe) == 1
    assert save_recipe(conn, recipe) == 2

    loaded = load_recipe(conn, "ashworthholme.co.uk")
    assert loaded is not None
    assert loaded.version == 2
    assert loaded.domain == "ashworthholme.co.uk"
    assert loaded.sitemap_urls == recipe.sitemap_urls


def test_load_recipe_version_fetches_a_specific_version(tmp_path):
    conn = init_db(tmp_path / "nimbus.db")
    recipe = make_recipe(domain="jaksandco.co.uk", notes="v1 notes")
    save_recipe(conn, recipe)
    recipe_v2 = make_recipe(domain="jaksandco.co.uk", notes="v2 notes")
    save_recipe(conn, recipe_v2)

    v1 = load_recipe_version(conn, "jaksandco.co.uk", 1)
    v2 = load_recipe_version(conn, "jaksandco.co.uk", 2)

    assert v1 is not None and v1.notes == "v1 notes"
    assert v2 is not None and v2.notes == "v2 notes"


def test_load_recipe_returns_none_for_unknown_domain(tmp_path):
    conn = init_db(tmp_path / "nimbus.db")
    assert load_recipe(conn, "unknown.co.uk") is None


def test_insert_audit_writes_a_readable_row(tmp_path):
    conn = init_db(tmp_path / "nimbus.db")
    insert_audit(
        conn,
        url="https://www.ashworthholme.co.uk/property/1",
        model="claude-sonnet-5",
        prompt_version="v1",
        recipe_version=1,
        html_hash="abc123",
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.001,
        schema_valid=True,
        needs_review=False,
        created_at=datetime.now(UTC).isoformat(),
    )
    row = conn.execute("SELECT schema_valid, needs_review FROM extraction_audit").fetchone()
    assert row is not None
    assert row[0] == 1
    assert row[1] == 0
