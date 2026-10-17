"""Health monitoring and recipe self-heal.

A recipe that worked last night can silently stop matching tonight — a
site redesign, not a bug in this codebase — and the failure looks like "a
quiet night" rather than an error: `crawl_site` still runs, still returns
a `CrawlOutcome`, it is just empty or full of pages the extractor can no
longer corroborate anything against. `site_health` is what turns that
silence into a number by reading the `extraction_audit` table's
`schema_valid` and `needs_review` columns — never
`ExtractionResult.overall_confidence`, which only ever takes the values
1.0 or 0.6 and carries no information `needs_review` doesn't already
carry (Task 5's own finding). `needs_heal` turns that number into a
decision, and `heal_site` acts on it by sending the discovery agent back
in to re-learn the site and saving the result as a new recipe version —
never overwriting the old one, so a previous version is always there to
diff against and to fall back to.
"""

from __future__ import annotations

import sqlite3
from urllib.parse import urlsplit

from nimbus import storage
from nimbus.config import Settings
from nimbus.discovery import discover_site
from nimbus.models import SiteHealth, SiteRecipe

DEFAULT_HEAL_THRESHOLD = 0.8


def _netloc_matches(url: str, domain: str) -> bool:
    """Whether `url`'s host is `domain`, ignoring a leading "www." —
    recipes and `Listing.source_domain` use the bare registrable domain
    (e.g. "ashworthholme.co.uk"), while fetched URLs carry whatever host
    the site itself serves from (e.g. "www.ashworthholme.co.uk")."""
    netloc = urlsplit(url).netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[len("www.") :]
    return netloc == domain.lower()


def site_health(conn: sqlite3.Connection, domain: str, window: int = 50) -> SiteHealth:
    """`domain`'s extraction health over its most recent `window`
    `extraction_audit` rows, newest first. `sample_size` is the number of
    matching rows actually found, which is `window` only once at least
    that many audit rows exist for `domain` — it is never padded or
    extrapolated."""
    rows = conn.execute(
        "SELECT url, schema_valid, needs_review FROM extraction_audit ORDER BY id DESC"
    ).fetchall()

    sample = [row for row in rows if row["url"] and _netloc_matches(row["url"], domain)][:window]
    sample_size = len(sample)

    if sample_size == 0:
        return SiteHealth(
            domain=domain,
            window=window,
            schema_valid_rate=0.0,
            needs_review_rate=0.0,
            sample_size=0,
        )

    schema_valid_rate = sum(1 for row in sample if row["schema_valid"]) / sample_size
    needs_review_rate = sum(1 for row in sample if row["needs_review"]) / sample_size

    return SiteHealth(
        domain=domain,
        window=window,
        schema_valid_rate=schema_valid_rate,
        needs_review_rate=needs_review_rate,
        sample_size=sample_size,
    )


def needs_heal(health: SiteHealth, *, threshold: float = DEFAULT_HEAL_THRESHOLD) -> bool:
    """Whether `health` is bad enough to send the discovery agent back in.

    Gated on `schema_valid_rate`, not `needs_review_rate`:
    `needs_review` fires on a single dropped, uncorroborated field (the
    extractor still produced a usable `Listing`), while a falling
    `schema_valid_rate` means extraction is failing outright — the
    stronger and rarer signal that a recipe has actually gone stale. A
    site with no audit history at all (`sample_size == 0`) has no
    evidence either way, so it is never reported as needing heal."""
    if health.sample_size == 0:
        return False
    return health.schema_valid_rate < threshold


def _field_hint_map(recipe: SiteRecipe) -> dict[str, str | None]:
    return {hint.field: hint.css_selector for hint in recipe.field_hints}


def _diff_recipes(previous: SiteRecipe | None, new: SiteRecipe) -> list[str]:
    """A human-readable diff between `previous` (the stored recipe before
    this heal) and `new` (what discovery just produced), for an operator
    or Task 7's demo to read without comparing JSON by hand."""
    if previous is None:
        return [f"no previous recipe for {new.domain}; this is the first discovered version"]

    diff: list[str] = []

    if previous.listing_url_patterns != new.listing_url_patterns:
        diff.append(
            f"listing_url_patterns: {previous.listing_url_patterns!r} -> "
            f"{new.listing_url_patterns!r}"
        )

    prev_hints = _field_hint_map(previous)
    new_hints = _field_hint_map(new)
    for field in sorted(set(prev_hints) | set(new_hints)):
        before, after = prev_hints.get(field), new_hints.get(field)
        if before != after:
            diff.append(f"field_hints[{field!r}].css_selector: {before!r} -> {after!r}")

    if previous.render_required != new.render_required:
        diff.append(f"render_required: {previous.render_required} -> {new.render_required}")

    if not diff:
        diff.append(
            f"no field_hint, listing_url_pattern, or render_required changes detected "
            f"between v{previous.version} and the newly discovered recipe"
        )

    return diff


async def heal_site(
    conn: sqlite3.Connection, domain: str, *, settings: Settings
) -> tuple[SiteRecipe, list[str]]:
    """Re-run discovery for `domain`, save the result as a new recipe
    version (never overwriting the previous one — `storage.save_recipe`
    is append-only), and return it alongside a human-readable diff against
    whatever recipe was stored before.

    Raises `nimbus.discovery.DiscoveryError` if discovery itself cannot
    produce a sound recipe — this function does not swallow that, since a
    failed heal must be visible, not mistaken for a successful one."""
    previous = storage.load_recipe(conn, domain)

    new_recipe = await discover_site(domain, settings=settings)

    new_version = storage.save_recipe(conn, new_recipe)
    healed_recipe = new_recipe.model_copy(update={"version": new_version})

    diff = _diff_recipes(previous, healed_recipe)

    return healed_recipe, diff
