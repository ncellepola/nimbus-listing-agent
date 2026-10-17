"""SQLite schema, recipe CRUD and audit insert.

Recipes are versioned and append-only: `save_recipe` always inserts a new
row with the next version number for the domain rather than overwriting,
so a prior recipe version is never lost and `heal_site` (Task 6) can diff
against it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from nimbus.models import FieldHint, PaginationRule, SiteRecipe

SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    domain TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recipes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL,
    version INTEGER NOT NULL,
    discovered_at TEXT NOT NULL,
    sitemap_urls TEXT NOT NULL,
    listing_index_urls TEXT NOT NULL,
    listing_url_patterns TEXT NOT NULL,
    pagination TEXT,
    field_hints TEXT NOT NULL,
    render_required INTEGER NOT NULL,
    confidence REAL NOT NULL,
    notes TEXT NOT NULL,
    UNIQUE (domain, version)
);

CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_domain TEXT NOT NULL,
    source_url TEXT NOT NULL UNIQUE,
    external_id TEXT,
    address TEXT NOT NULL,
    postcode TEXT,
    price_gbp INTEGER,
    price_qualifier TEXT,
    bedrooms INTEGER,
    bathrooms INTEGER,
    property_type TEXT NOT NULL,
    tenure TEXT NOT NULL,
    floor_area_sqft REAL,
    description TEXT,
    agent_name TEXT,
    listed_date TEXT,
    content_hash TEXT NOT NULL,
    first_seen_at TEXT,
    last_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS listing_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url TEXT NOT NULL,
    old_price_gbp INTEGER,
    new_price_gbp INTEGER,
    changed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS extraction_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT,
    model TEXT,
    prompt_version TEXT,
    recipe_version INTEGER,
    html_hash TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL,
    schema_valid INTEGER NOT NULL,
    needs_review INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS crawl_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    pages_fetched INTEGER,
    listings_found INTEGER
);
"""


def init_db(path: Path) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _recipe_to_row(recipe: SiteRecipe, version: int) -> dict:
    return {
        "domain": recipe.domain,
        "version": version,
        "discovered_at": recipe.discovered_at.isoformat(),
        "sitemap_urls": json.dumps(recipe.sitemap_urls),
        "listing_index_urls": json.dumps(recipe.listing_index_urls),
        "listing_url_patterns": json.dumps(recipe.listing_url_patterns),
        "pagination": (
            json.dumps(recipe.pagination.model_dump()) if recipe.pagination is not None else None
        ),
        "field_hints": json.dumps([hint.model_dump() for hint in recipe.field_hints]),
        "render_required": int(recipe.render_required),
        "confidence": recipe.confidence,
        "notes": recipe.notes,
    }


def _row_to_recipe(row: sqlite3.Row) -> SiteRecipe:
    pagination_raw = row["pagination"]
    return SiteRecipe(
        domain=row["domain"],
        version=row["version"],
        discovered_at=datetime.fromisoformat(row["discovered_at"]),
        sitemap_urls=json.loads(row["sitemap_urls"]),
        listing_index_urls=json.loads(row["listing_index_urls"]),
        listing_url_patterns=json.loads(row["listing_url_patterns"]),
        pagination=PaginationRule(**json.loads(pagination_raw)) if pagination_raw else None,
        field_hints=[FieldHint(**h) for h in json.loads(row["field_hints"])],
        render_required=bool(row["render_required"]),
        confidence=row["confidence"],
        notes=row["notes"],
    )


def save_recipe(conn: sqlite3.Connection, recipe: SiteRecipe) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO sites (domain, created_at) VALUES (?, ?)",
        (recipe.domain, datetime.now(UTC).isoformat()),
    )
    current = conn.execute(
        "SELECT MAX(version) AS max_version FROM recipes WHERE domain = ?",
        (recipe.domain,),
    ).fetchone()
    next_version = (current["max_version"] or 0) + 1

    row = _recipe_to_row(recipe, next_version)
    conn.execute(
        """
        INSERT INTO recipes (
            domain, version, discovered_at, sitemap_urls, listing_index_urls,
            listing_url_patterns, pagination, field_hints, render_required,
            confidence, notes
        ) VALUES (
            :domain, :version, :discovered_at, :sitemap_urls, :listing_index_urls,
            :listing_url_patterns, :pagination, :field_hints, :render_required,
            :confidence, :notes
        )
        """,
        row,
    )
    conn.commit()
    return next_version


def load_recipe(conn: sqlite3.Connection, domain: str) -> SiteRecipe | None:
    row = conn.execute(
        "SELECT * FROM recipes WHERE domain = ? ORDER BY version DESC LIMIT 1",
        (domain,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_recipe(row)


def load_recipe_version(conn: sqlite3.Connection, domain: str, version: int) -> SiteRecipe | None:
    row = conn.execute(
        "SELECT * FROM recipes WHERE domain = ? AND version = ?",
        (domain, version),
    ).fetchone()
    if row is None:
        return None
    return _row_to_recipe(row)


def insert_audit(conn: sqlite3.Connection, **fields) -> None:
    fields.setdefault("created_at", datetime.now(UTC).isoformat())
    if "schema_valid" in fields:
        fields["schema_valid"] = int(bool(fields["schema_valid"]))
    if "needs_review" in fields:
        fields["needs_review"] = int(bool(fields["needs_review"]))

    columns = ", ".join(fields.keys())
    placeholders = ", ".join(f":{key}" for key in fields)
    conn.execute(
        f"INSERT INTO extraction_audit ({columns}) VALUES ({placeholders})",
        fields,
    )
    conn.commit()
