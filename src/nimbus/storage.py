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

from nimbus.models import FieldHint, Listing, PaginationRule, SiteRecipe

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
    sample_listing_urls TEXT NOT NULL,
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
        "sample_listing_urls": json.dumps(recipe.sample_listing_urls),
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
        sample_listing_urls=json.loads(row["sample_listing_urls"]),
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
            listing_url_patterns, sample_listing_urls, pagination, field_hints,
            render_required, confidence, notes
        ) VALUES (
            :domain, :version, :discovered_at, :sitemap_urls, :listing_index_urls,
            :listing_url_patterns, :sample_listing_urls, :pagination, :field_hints,
            :render_required, :confidence, :notes
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


# --------------------------------------------------------------------------
# Listing upsert and price history (Task 6). `nimbus.delta.apply_delta`
# composes these primitives into the nightly new/changed/withdrawn/
# unchanged classification; they are kept here, next to the schema they
# write, rather than duplicated in nimbus.delta.
# --------------------------------------------------------------------------


def _row_to_listing(row: sqlite3.Row) -> Listing:
    return Listing(
        source_domain=row["source_domain"],
        source_url=row["source_url"],
        external_id=row["external_id"],
        address=row["address"],
        postcode=row["postcode"],
        price_gbp=row["price_gbp"],
        price_qualifier=row["price_qualifier"],
        bedrooms=row["bedrooms"],
        bathrooms=row["bathrooms"],
        property_type=row["property_type"],
        tenure=row["tenure"],
        floor_area_sqft=row["floor_area_sqft"],
        description=row["description"],
        agent_name=row["agent_name"],
        listed_date=row["listed_date"],
        content_hash=row["content_hash"],
    )


def get_listing(conn: sqlite3.Connection, source_url: str) -> Listing | None:
    """The stored `Listing` for `source_url`, or `None` if it has never
    been seen before."""
    row = conn.execute(
        "SELECT * FROM listings WHERE source_url = ?",
        (source_url,),
    ).fetchone()
    if row is None:
        return None
    return _row_to_listing(row)


def list_listings(conn: sqlite3.Connection, domain: str) -> list[Listing]:
    """Every listing currently stored for `domain`, regardless of whether
    it appeared in the most recent crawl — this is the "what we already
    knew" side of a nightly delta."""
    rows = conn.execute(
        "SELECT * FROM listings WHERE source_domain = ? ORDER BY source_url",
        (domain,),
    ).fetchall()
    return [_row_to_listing(row) for row in rows]


def upsert_listing(conn: sqlite3.Connection, listing: Listing) -> None:
    """Insert `listing`, or update it in place if `source_url` already
    exists. `first_seen_at` is set once and never overwritten;
    `last_seen_at` always moves to now. Does not write `listing_history` —
    that is `insert_price_history`'s job, called separately when the
    caller has decided the price actually changed."""
    now = datetime.now(UTC).isoformat()
    existing = conn.execute(
        "SELECT first_seen_at FROM listings WHERE source_url = ?",
        (listing.source_url,),
    ).fetchone()
    first_seen_at = existing["first_seen_at"] if existing is not None else now

    conn.execute(
        """
        INSERT INTO listings (
            source_domain, source_url, external_id, address, postcode,
            price_gbp, price_qualifier, bedrooms, bathrooms, property_type,
            tenure, floor_area_sqft, description, agent_name, listed_date,
            content_hash, first_seen_at, last_seen_at
        ) VALUES (
            :source_domain, :source_url, :external_id, :address, :postcode,
            :price_gbp, :price_qualifier, :bedrooms, :bathrooms, :property_type,
            :tenure, :floor_area_sqft, :description, :agent_name, :listed_date,
            :content_hash, :first_seen_at, :last_seen_at
        )
        ON CONFLICT (source_url) DO UPDATE SET
            external_id = excluded.external_id,
            address = excluded.address,
            postcode = excluded.postcode,
            price_gbp = excluded.price_gbp,
            price_qualifier = excluded.price_qualifier,
            bedrooms = excluded.bedrooms,
            bathrooms = excluded.bathrooms,
            property_type = excluded.property_type,
            tenure = excluded.tenure,
            floor_area_sqft = excluded.floor_area_sqft,
            description = excluded.description,
            agent_name = excluded.agent_name,
            listed_date = excluded.listed_date,
            content_hash = excluded.content_hash,
            last_seen_at = excluded.last_seen_at
        """,
        {
            "source_domain": listing.source_domain,
            "source_url": listing.source_url,
            "external_id": listing.external_id,
            "address": listing.address,
            "postcode": listing.postcode,
            "price_gbp": listing.price_gbp,
            "price_qualifier": listing.price_qualifier,
            "bedrooms": listing.bedrooms,
            "bathrooms": listing.bathrooms,
            "property_type": listing.property_type,
            "tenure": listing.tenure,
            "floor_area_sqft": listing.floor_area_sqft,
            "description": listing.description,
            "agent_name": listing.agent_name,
            "listed_date": listing.listed_date.isoformat() if listing.listed_date else None,
            "content_hash": listing.content_hash,
            "first_seen_at": first_seen_at,
            "last_seen_at": now,
        },
    )
    conn.commit()


def insert_price_history(
    conn: sqlite3.Connection,
    source_url: str,
    old_price_gbp: int | None,
    new_price_gbp: int | None,
) -> None:
    """Record one price change for `source_url`. Called only when a caller
    (`nimbus.delta.apply_delta`) has already decided the price genuinely
    changed — this function does not compare, it only records."""
    conn.execute(
        "INSERT INTO listing_history (source_url, old_price_gbp, new_price_gbp, changed_at) "
        "VALUES (?, ?, ?, ?)",
        (source_url, old_price_gbp, new_price_gbp, datetime.now(UTC).isoformat()),
    )
    conn.commit()
