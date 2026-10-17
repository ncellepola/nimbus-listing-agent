"""The `nimbus` command-line interface.

Five commands wrap the pipeline Tasks 1-6 already built:

- `onboard <domain>`   -- run the real discovery agent once and save a SiteRecipe.
- `crawl <domain>`     -- run the deterministic nightly crawl + extraction + delta.
- `heal <domain>`      -- re-run discovery against a domain whose recipe has gone stale.
- `report`             -- show what storage knows: recipes, health, stored listings.
- `demo [--offline]`   -- the presentation path, implemented in `nimbus.demo`.

`onboard`/`crawl`/`heal` are real: they build a real `Fetcher`/`anthropic` client from
`Settings` and touch the network and (for `onboard`/`heal`) the live discovery agent.
`tests/test_cli.py` exercises their early, no-network error paths (a missing recipe,
a missing API key -- `onboard`/`heal` fail the same way `discover_site` itself does,
before any network call) and, for `crawl`, the mass-withdrawal decision against a
mocked `CrawlOutcome` -- never their live bodies, precisely because doing that would
spend real API credits every time `pytest` runs.

`demo` is different on purpose, and big enough that its implementation lives in its
own module, `nimbus.demo`, rather than here: see that module's docstring and
`docs/adr/0004-cache-first-fetch-for-reproducible-demos.md`.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import typer

from nimbus import console, storage
from nimbus.config import Settings
from nimbus.crawler import crawl_site
from nimbus.delta import apply_delta, should_allow_mass_withdrawal
from nimbus.discovery import DiscoveryError, discover_site
from nimbus.extractor import (
    ExtractionError,
    extract_listing,
    record_extraction,
    unchanged_since_last_run,
)
from nimbus.fetch.backends import HttpxBackend
from nimbus.fetch.cache import HtmlCache
from nimbus.fetch.fetcher import Fetcher
from nimbus.heal import heal_site, site_health
from nimbus.models import ExtractionResult, Listing

app = typer.Typer(
    name="nimbus",
    help=(
        "Onboard estate-agent sites, crawl them nightly, heal stale recipes, "
        "and report what's known. Run `nimbus demo --offline` first."
    ),
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _main(
    ctx: typer.Context,
    db: Path = typer.Option(
        None,
        "--db",
        help="SQLite database path (default: Settings().db_path). Ignored by `demo`, "
        "which always runs against its own throwaway database so its output stays "
        "reproducible regardless of what else has been onboarded or crawled.",
    ),
) -> None:
    ctx.obj = {"db": db if db is not None else Settings().db_path}


def _conn(ctx: typer.Context) -> sqlite3.Connection:
    return storage.init_db(ctx.obj["db"])


def _settings_for(ctx: typer.Context) -> Settings:
    return Settings(db_path=ctx.obj["db"])


# --------------------------------------------------------------------------
# onboard / crawl / heal / report: the real commands. Each builds a real
# Settings/Fetcher/(for crawl) anthropic client and touches the network;
# the test suite only exercises their early, no-network error paths.
# --------------------------------------------------------------------------


@app.command()
def onboard(ctx: typer.Context, domain: str) -> None:
    """Learn a previously-unseen site once via the live discovery agent and
    save the result as a new, versioned SiteRecipe."""
    settings = _settings_for(ctx)
    conn = _conn(ctx)

    typer.echo(
        f"Onboarding {domain} via the discovery agent (this calls the live Anthropic API)..."
    )
    try:
        recipe = asyncio.run(discover_site(domain, settings=settings))
    except DiscoveryError as exc:
        typer.echo(f"Discovery failed: {exc}")
        raise typer.Exit(code=1) from exc

    version = storage.save_recipe(conn, recipe)
    typer.echo(
        f"Saved {domain} recipe v{version}: confidence={recipe.confidence:.2f}, "
        f"render_required={recipe.render_required}, "
        f"{len(recipe.sitemap_urls)} sitemap(s), {len(recipe.listing_url_patterns)} URL pattern(s)."
    )


@app.command()
def crawl(
    ctx: typer.Context,
    domain: str,
    limit: int | None = typer.Option(
        None, "--limit", help="Cap the number of listing pages fetched."
    ),
) -> None:
    """Run the deterministic nightly crawl for `domain` against its saved
    SiteRecipe, extract each page, and apply the delta against storage.
    Contains no agent call; it does call the live `anthropic` extraction
    API once per fetched page, if an API key is configured."""
    settings = _settings_for(ctx)
    conn = _conn(ctx)

    recipe = storage.load_recipe(conn, domain)
    if recipe is None:
        typer.echo(f"No recipe saved for {domain!r}. Run `nimbus onboard {domain}` first.")
        raise typer.Exit(code=1)

    cache = HtmlCache(root=settings.cache_dir)
    fetcher = Fetcher(settings, cache, HttpxBackend(user_agent=settings.user_agent))

    # Crawl delay is resolved from *live* robots.txt, not the recipe: a
    # recipe can be weeks stale while robots.txt is current policy (see
    # nimbus.crawler's module docstring and pre-flight ruling R5).
    policy = asyncio.run(fetcher.robots_for(domain))
    outcome = asyncio.run(
        crawl_site(recipe, fetcher=fetcher, crawl_delay=policy.crawl_delay, limit=limit)
    )
    typer.echo(
        f"{domain}: {outcome.candidates} candidate(s), {outcome.selected} selected, "
        f"{outcome.attempted} attempted, {outcome.fetched} fetched "
        f"(skipped_robots={outcome.skipped_robots}, "
        f"skipped_errors={outcome.skipped_errors}, crawl_delay={policy.crawl_delay}s)"
    )
    if outcome.limit_applied:
        typer.echo(
            f"      --limit {limit} truncated {outcome.selected} selected URL(s) to "
            f"{outcome.attempted}: this run is a partial view of the site, so it cannot "
            "authorise withdrawing anything it did not look at."
        )

    api_key = settings.anthropic_api_key.get_secret_value()
    if not api_key:
        # No new listings were extracted this run, which is not the same
        # thing as "nothing is live any more" -- stop here, before
        # apply_delta, rather than let an empty listings set caused by
        # "extraction never ran" be read as a real withdrawal signal.
        typer.echo("No ANTHROPIC_API_KEY configured; stopping before extraction.")
        return

    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=api_key)
    listings: list[Listing] = []
    results: list[ExtractionResult] = []
    skipped_unchanged = 0
    for record in outcome.records:
        # The one LLM call on this path, skipped entirely when the page's
        # bytes are identical to what was extracted from them last time.
        # The stored Listing still goes into `listings` and `results`:
        # last night's extraction is this page's current extraction, and
        # dropping it here would turn "unchanged" into "withdrawn".
        unchanged = unchanged_since_last_run(conn, record)
        if unchanged is not None:
            skipped_unchanged += 1
            listings.append(unchanged)
            results.append(ExtractionResult(listing=unchanged, overall_confidence=1.0))
            continue
        try:
            result = asyncio.run(extract_listing(record, recipe, client=client))
        except ExtractionError as exc:
            typer.echo(f"  extraction failed for {record.url}: {exc}")
            continue
        results.append(result)
        # NOTE: extract_listing does not surface the raw response's token
        # usage, so real cost accounting is not wired up here -- see
        # README's "what is not built" section. The audit row is still
        # written with schema_valid/needs_review, which is what
        # site_health actually reads.
        record_extraction(
            conn,
            record,
            recipe,
            result,
            model="claude-sonnet-5",
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
        )
        if result.listing is not None:
            listings.append(result.listing)

    if skipped_unchanged:
        typer.echo(
            f"  {skipped_unchanged} unchanged page(s) skipped extraction entirely "
            "(content_hash identical to the stored listing), at no model cost"
        )

    # should_allow_mass_withdrawal, not a bare `outcome.fetched > 0`: a
    # crawl that selected 1075 URLs and fetched exactly one of them must
    # not be trusted to authorise withdrawing the other 1074 previously
    # stored listings -- and neither must one that fetched all 195 and
    # then extracted nothing from any of them, which is what a site
    # redesign looks like. Both signals go in.
    report = apply_delta(
        conn,
        domain,
        listings,
        allow_mass_withdrawal=should_allow_mass_withdrawal(outcome, results),
    )
    console.print_delta(f"{domain} delta", report)


@app.command()
def heal(ctx: typer.Context, domain: str) -> None:
    """Re-run the live discovery agent against `domain` and save the result
    as a new recipe version, printing a diff against whatever was stored
    before. This is the genuine self-heal path -- it calls the real agent,
    unlike `demo --offline`'s stubbed stand-in for it."""
    settings = _settings_for(ctx)
    conn = _conn(ctx)

    health = site_health(conn, domain)
    if health.sample_size:
        console.print_health(f"{domain} health before heal", health)

    typer.echo(f"Healing {domain} via the discovery agent (this calls the live Anthropic API)...")
    try:
        recipe, diff = asyncio.run(heal_site(conn, domain, settings=settings))
    except DiscoveryError as exc:
        typer.echo(f"Heal failed: {exc}")
        raise typer.Exit(code=1) from exc

    typer.echo(f"{domain} recipe repaired: now v{recipe.version}")
    for line in diff:
        typer.echo(f"  - {line}")


@app.command()
def report(
    ctx: typer.Context,
    domain: str | None = typer.Option(None, "--domain", help="Show only this domain."),
) -> None:
    """Show every onboarded domain's recipe version, extraction health, and
    stored listings. Reads only from storage -- never touches the network."""
    conn = _conn(ctx)
    domains = [domain] if domain else _all_domains(conn)

    if not domains:
        typer.echo("No domains onboarded yet. Run `nimbus onboard <domain>` first.")
        return

    for d in domains:
        typer.echo(f"\n== {d} ==")
        recipe = storage.load_recipe(conn, d)
        if recipe is not None:
            typer.echo(
                f"recipe v{recipe.version}: confidence={recipe.confidence:.2f}, "
                f"render_required={recipe.render_required}"
            )
        else:
            typer.echo("no recipe saved")

        health = site_health(conn, d)
        if health.sample_size:
            console.print_health("health", health)

        listings = storage.list_listings(conn, d)
        typer.echo(f"listings stored: {len(listings)}")
        for listing in listings:
            price = console.format_price(listing.price_gbp)
            typer.echo(f"  {price:>12}  {listing.address}  ({listing.source_url})")


def _all_domains(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT domain FROM sites ORDER BY domain").fetchall()
    return [row["domain"] for row in rows]


@app.command()
def demo(
    ctx: typer.Context,
    offline: bool = typer.Option(
        False,
        "--offline",
        help="Run entirely from committed fixtures: no network call, no agent call, "
        "fully reproducible. Always uses its own throwaway database, ignoring --db.",
    ),
) -> None:
    """A narrated walkthrough: onboard a site, crawl it from cache, extract
    listings, simulate a site redesign, watch extraction health collapse,
    and watch the self-heal repair it. Run with `--offline` for the
    reproducible, no-network, no-agent-call version this project's demo
    path is built around. Implementation: `nimbus.demo`."""
    from nimbus.demo import run_live_demo, run_offline_demo

    if offline:
        asyncio.run(run_offline_demo())
    else:
        asyncio.run(run_live_demo(_settings_for(ctx)))


if __name__ == "__main__":
    app()
