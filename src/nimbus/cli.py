"""The `nimbus` command-line interface.

Five commands wrap the pipeline Tasks 1-6 already built:

- `onboard <domain>`   -- run the real discovery agent once and save a SiteRecipe.
- `crawl <domain>`     -- run the deterministic nightly crawl + extraction + delta.
- `heal <domain>`      -- re-run discovery against a domain whose recipe has gone stale.
- `report`             -- show what storage knows: recipes, health, stored listings.
- `demo [--offline]`   -- the presentation path. See below.

`onboard`/`crawl`/`heal` are real: they build a real `Fetcher`/`anthropic` client from
`Settings` and touch the network and (for `onboard`/`heal`) the live discovery agent.
Nothing in this module's test suite exercises those bodies beyond their early,
no-network error paths (a missing recipe, a missing API key) -- precisely because
doing otherwise would spend real API credits every time `pytest` runs.

`demo` is different on purpose. `--offline` is this project's answer to "a demo that
dies in the room" (the spec's own named anti-goal, pre-flight ruling R7): it makes
*no* network call and *no* agent call at all, runs the same five architectural beats
every other command is built from, and produces byte-identical output on every run.
It does this by loading a recipe straight from a committed fixture file instead of
calling `discover_site`, pre-seeding `HtmlCache` with real captured pages instead of
fetching them, and running extraction against a canned stub client instead of the
real `anthropic` API -- never by skipping the real `crawl_site`/`extract_listing`/
`apply_delta`/`heal_site` code. See `docs/adr/0004-cache-first-fetch-for-reproducible-demos.md`.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

import typer

import nimbus.heal as heal_module
from nimbus import storage
from nimbus.config import Settings
from nimbus.crawler import crawl_site
from nimbus.delta import apply_delta
from nimbus.discovery import DiscoveryError, discover_site
from nimbus.extractor import ExtractionError, extract_listing, record_extraction
from nimbus.fetch.backends import HttpxBackend
from nimbus.fetch.cache import HtmlCache
from nimbus.fetch.fetcher import Fetcher
from nimbus.heal import heal_site, needs_heal, site_health
from nimbus.models import CrawlOutcome, DeltaReport, SiteHealth, SiteRecipe

# src/nimbus/cli.py -> nimbus -> src -> repo root, same convention
# nimbus.discovery._REPO_ROOT uses -- computed from this file's own location,
# not the process cwd, so `nimbus demo --offline` finds the committed fixtures
# regardless of where it is invoked from.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURES_DIR = _REPO_ROOT / "tests" / "fixtures"

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


def _banner(text: str) -> None:
    rule = "=" * max(60, len(text) + 4)
    typer.echo(rule)
    typer.echo(text)
    typer.echo(rule)


def _format_price(price_gbp: int | None) -> str:
    return f"£{price_gbp:,}" if price_gbp is not None else "—"


def _print_delta(label: str, report: DeltaReport) -> None:
    typer.echo(
        f"{label}: new={len(report.new)} price_changed={len(report.price_changed)} "
        f"withdrawn={len(report.withdrawn)} unchanged={len(report.unchanged)}"
        + (" (withdrawal suppressed)" if report.withdrawal_suppressed else "")
    )


def _print_health(label: str, health: SiteHealth) -> None:
    typer.echo(
        f"{label}: schema_valid_rate={health.schema_valid_rate:.0%} "
        f"needs_review_rate={health.needs_review_rate:.0%} (sample={health.sample_size}) "
        f"-> needs_heal={needs_heal(health)}"
    )


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
        f"{outcome.fetched} fetched (skipped_robots={outcome.skipped_robots}, "
        f"skipped_errors={outcome.skipped_errors}, crawl_delay={policy.crawl_delay}s)"
    )

    api_key = settings.anthropic_api_key.get_secret_value()
    if not api_key:
        typer.echo("No ANTHROPIC_API_KEY configured; stopping before extraction.")
        return

    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=api_key)
    listings = []
    for record in outcome.records:
        try:
            result = asyncio.run(extract_listing(record, recipe, client=client))
        except ExtractionError as exc:
            typer.echo(f"  extraction failed for {record.url}: {exc}")
            continue
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

    report = apply_delta(conn, domain, listings, allow_mass_withdrawal=outcome.fetched > 0)
    _print_delta(f"{domain} delta", report)


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
        _print_health(f"{domain} health before heal", health)

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
            _print_health("health", health)

        listings = storage.list_listings(conn, d)
        typer.echo(f"listings stored: {len(listings)}")
        for listing in listings:
            price = _format_price(listing.price_gbp)
            typer.echo(f"  {price:>12}  {listing.address}  ({listing.source_url})")


def _all_domains(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT domain FROM sites ORDER BY domain").fetchall()
    return [row["domain"] for row in rows]


# --------------------------------------------------------------------------
# demo: the presentation path. See the module docstring. Everything below
# this point is read-only against committed fixtures in `--offline` mode;
# nothing here mutates anything outside its own throwaway temp directory.
# --------------------------------------------------------------------------

_DOMAIN = "ashworthholme.co.uk"
_RECIPE_FIXTURE = _FIXTURES_DIR / "recipes" / f"{_DOMAIN}.json"
_HEALED_RECIPE_FIXTURE = _FIXTURES_DIR / "recipes" / f"{_DOMAIN}.healed.json"
_BASELINE_PAGES_DIR = _FIXTURES_DIR / "pages" / _DOMAIN
_MUTATED_PAGES_DIR = _FIXTURES_DIR / "pages" / "mutated"

_SHERBROOKE_URL = (
    "https://www.ashworthholme.co.uk/property-details/34887120/-/sale/sherbrooke-close"
)
_ROSSETT_URL = "https://www.ashworthholme.co.uk/property-details/34782880/-/altrincham/rossett-ave"
_SITEMAP_URL = "https://www.ashworthholme.co.uk/sitemap.xml"
_ROBOTS_URL = "https://www.ashworthholme.co.uk/robots.txt"

# (listing URL, fixture filename shared by the baseline and mutated page
# directories, the address the page's heading actually carries, the price
# its status line actually carries). Used both to seed the cache and to
# hand the stub extraction client the literal text each page corroborates
# against -- the same two real ashworthholme.co.uk pages Task 6's own
# health-collapse-and-recover test exercises.
_DemoPage = tuple[str, str, str, int]
_PAGES: list[_DemoPage] = [
    (_SHERBROOKE_URL, "sherbrooke-close.html", "Sherbrooke Close, Sale", 850_000),
    (_ROSSETT_URL, "rossett-ave-altrincham.html", "Rossett Ave, Timperley, Altrincham", 650_000),
]


class _NoNetworkBackend:
    """A `FetchBackend` that fails loudly if ever called. `demo --offline`
    pre-seeds `HtmlCache` with every URL it needs, so `Fetcher.get()` should
    never reach this; if it ever does, that is a bug in the demo, not a
    tolerable fallback -- hence raising rather than returning anything."""

    async def fetch(self, url: str, *, timeout: float):
        raise AssertionError(
            f"nimbus demo --offline must never touch the network, but tried to fetch {url}"
        )


async def _no_sleep(_delay: float) -> None:
    """`crawl_site`'s `sleep` hook, replaced for the demo only so the
    narrative doesn't sit idle for real crawl-delay seconds between pages
    -- `test_crawl_site_honours_crawl_delay` is what proves the real delay
    logic itself; this demo is not re-proving it."""
    return None


class _StubToolUseBlock:
    type = "tool_use"

    def __init__(self, input: dict[str, Any]) -> None:
        self.input = input
        self.name = "emit_listing"
        self.id = "toolu_demo"


class _StubMessage:
    def __init__(self, content: list[Any]) -> None:
        self.content = content


class _StubMessagesAPI:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def create(self, **kwargs: Any) -> _StubMessage:
        return _StubMessage(content=[_StubToolUseBlock(input=dict(self._payload))])


class _StubExtractionClient:
    """Stands in for `anthropic.AsyncAnthropic` in `demo --offline`: a
    canned `emit_listing` tool call, matching the literal address/price
    text the real fixture page carries. Never touches the network --
    exactly the stand-in `tests/test_heal.py::StubClient` already uses for
    the same two real pages."""

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
        self.messages = _StubMessagesAPI(payload)


def _sitemap_xml(urls: list[str]) -> str:
    locs = "\n".join(f"  <url><loc>{url}</loc></url>" for url in urls)
    return (
        '<?xml version="1.0"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{locs}\n"
        "</urlset>"
    )


def _page_for_url(url: str) -> _DemoPage:
    for page in _PAGES:
        if page[0] == url:
            return page
    raise AssertionError(f"demo crawled an unexpected URL: {url}")


async def _extract_and_record(
    conn: sqlite3.Connection, recipe: SiteRecipe, outcome: CrawlOutcome
) -> list:
    """Extract every record in `outcome` against the stub client, record
    each attempt's audit row, and return the successfully extracted
    `Listing`s. Mirrors `tests/test_heal.py::_extract_and_record`."""
    listings = []
    for record in outcome.records:
        _, _, address, price = _page_for_url(record.url)
        result = await extract_listing(
            record, recipe, client=_StubExtractionClient(address=address, price=price)
        )
        record_extraction(
            conn,
            record,
            recipe,
            result,
            model="claude-sonnet-5 (stubbed for --offline)",
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
        )
        slug = record.url.rstrip("/").rsplit("/", 1)[-1]
        if result.listing is not None:
            extracted_price = _format_price(result.listing.price_gbp)
            typer.echo(f"      {slug}: extracted {result.listing.address} — {extracted_price}")
            listings.append(result.listing)
        else:
            reason = result.issues[0] if result.issues else "no reason given"
            # The first clause only -- extract_listing's own issue text is
            # written to be a full sentence for the audit trail, not a
            # one-line terminal summary; truncating here, not there, keeps
            # the audit row's text intact.
            headline = reason.split(";")[0].split(" (")[0]
            typer.echo(f"      {slug}: extraction REJECTED -- {headline}")
    return listings


async def _run_offline_demo() -> None:
    _banner("NIMBUS OFFLINE DEMO -- no network call, no agent call, fully reproducible")

    with tempfile.TemporaryDirectory(prefix="nimbus-demo-") as tmp:
        tmp_path = Path(tmp)
        conn = storage.init_db(tmp_path / "nimbus.db")
        settings = Settings(
            anthropic_api_key="",
            cache_dir=tmp_path / "cache",
            db_path=tmp_path / "nimbus.db",
        )
        cache = HtmlCache(root=settings.cache_dir)
        fetcher = Fetcher(settings, cache, _NoNetworkBackend())

        # Seed every URL the crawl will touch before touching it -- the
        # cache-first guarantee ADR-0004 is about, made concrete.
        cache.put(_ROBOTS_URL, 200, "text/plain", "User-agent: *\nAllow: /\n")
        cache.put(_SITEMAP_URL, 200, "application/xml", _sitemap_xml([p[0] for p in _PAGES]))
        for url, filename, _address, _price in _PAGES:
            cache.put(url, 200, "text/html", (_BASELINE_PAGES_DIR / filename).read_text())

        typer.echo("\n[1/5] Onboarding ashworthholme.co.uk")
        typer.echo("      This is a recipe the discovery agent actually captured against the live")
        typer.echo("      site -- not synthesised for this demo.")
        recipe = SiteRecipe(**json.loads(_RECIPE_FIXTURE.read_text()))
        version = storage.save_recipe(conn, recipe)
        typer.echo(
            f"      saved recipe v{version}: confidence={recipe.confidence:.2f}, "
            f"render_required={recipe.render_required}, "
            f"pattern={recipe.listing_url_patterns[0]!r}"
        )

        typer.echo("\n[2/5] Crawling from the local cache and extracting each page")
        policy = await fetcher.robots_for(recipe.domain)
        outcome = await crawl_site(
            recipe, fetcher=fetcher, crawl_delay=policy.crawl_delay, sleep=_no_sleep
        )
        typer.echo(
            f"      {outcome.candidates} candidate(s), {outcome.selected} selected, "
            f"{outcome.fetched} fetched (crawl_delay={policy.crawl_delay}s, from robots.txt)"
        )
        listings = await _extract_and_record(conn, recipe, outcome)
        first_delta = apply_delta(
            conn, recipe.domain, listings, allow_mass_withdrawal=outcome.fetched > 0
        )
        _print_delta("      delta", first_delta)

        typer.echo("\n[3/5] Simulating a site redesign: swapping in the mutated page markup")
        typer.echo('      <h1>/<h2> -> <div class="listing-heading">/<div class="listing-status">')
        for url, filename, _address, _price in _PAGES:
            cache.put(url, 200, "text/html", (_MUTATED_PAGES_DIR / filename).read_text())
        mutated_outcome = await crawl_site(
            recipe, fetcher=fetcher, crawl_delay=policy.crawl_delay, sleep=_no_sleep
        )
        await _extract_and_record(conn, recipe, mutated_outcome)

        typer.echo("\n[4/5] Checking extraction health")
        health_before = site_health(conn, recipe.domain, window=len(_PAGES))
        _print_health("      health immediately after the redesign", health_before)
        if needs_heal(health_before):
            typer.echo("      HEALTH COLLAPSED -- field_hints no longer match this site's markup")

        typer.echo("\n[5/5] Repairing: re-discovering the site and bumping the recipe version")
        healed_payload = SiteRecipe(**json.loads(_HEALED_RECIPE_FIXTURE.read_text()))

        async def _stub_discover_site(domain: str, *, settings: Settings, max_turns: int = 20):
            return healed_payload

        # discover_site is the one real network+agent call anywhere in this
        # pipeline. Swapping it out at nimbus.heal's own call site -- never
        # calling it, not even once -- is what makes --offline's "no agent
        # call at all" true, while still running heal_site's real
        # load-previous/save-append-only/diff logic (pre-flight ruling R7).
        original_discover_site = heal_module.discover_site
        heal_module.discover_site = _stub_discover_site
        try:
            new_recipe, diff = await heal_site(conn, recipe.domain, settings=settings)
        finally:
            heal_module.discover_site = original_discover_site

        typer.echo(f"      recipe repaired: v{recipe.version} -> v{new_recipe.version}")
        for line in diff:
            typer.echo(f"        - {line}")

        recovered_outcome = await crawl_site(
            new_recipe, fetcher=fetcher, crawl_delay=policy.crawl_delay, sleep=_no_sleep
        )
        recovered_listings = await _extract_and_record(conn, new_recipe, recovered_outcome)

        health_after = site_health(conn, recipe.domain, window=len(_PAGES))
        _print_health("      health after the repair", health_after)

        recovered_delta = apply_delta(
            conn,
            recipe.domain,
            recovered_listings,
            allow_mass_withdrawal=recovered_outcome.fetched > 0,
        )
        _print_delta("      post-heal delta", recovered_delta)

    _banner("DEMO COMPLETE")


async def _run_live_demo(settings: Settings) -> None:
    """The non-`--offline` path: the same onboard/crawl/extract narrative,
    for real, against the real `ashworthholme.co.uk`. Calls the live
    discovery agent and the live Anthropic extraction API; costs real
    money and depends on the site answering the same way twice, which is
    exactly why `--offline` exists for the room. The simulated-redesign /
    self-heal beats are not reproduced here -- there is no live redesign to
    show on demand -- see `demo --offline` for the full narrative."""
    _banner(f"NIMBUS LIVE DEMO against {_DOMAIN} -- this calls the real network and the real agent")

    with tempfile.TemporaryDirectory(prefix="nimbus-live-demo-") as tmp:
        tmp_path = Path(tmp)
        conn = storage.init_db(tmp_path / "nimbus.db")
        live_settings = settings.model_copy(update={"cache_dir": tmp_path / "cache"})

        typer.echo(f"\n[1/3] Discovering {_DOMAIN} via the live agent...")
        try:
            recipe = await discover_site(_DOMAIN, settings=live_settings)
        except DiscoveryError as exc:
            typer.echo(f"Discovery failed: {exc}")
            raise typer.Exit(code=1) from exc
        version = storage.save_recipe(conn, recipe)
        typer.echo(f"      saved v{version}, confidence={recipe.confidence:.2f}")

        cache = HtmlCache(root=live_settings.cache_dir)
        fetcher = Fetcher(live_settings, cache, HttpxBackend(user_agent=live_settings.user_agent))
        policy = await fetcher.robots_for(_DOMAIN)
        typer.echo(f"\n[2/3] Crawling live (crawl_delay={policy.crawl_delay}s, from robots.txt)...")
        outcome = await crawl_site(recipe, fetcher=fetcher, crawl_delay=policy.crawl_delay, limit=3)
        typer.echo(f"      fetched {outcome.fetched}/{outcome.selected} selected page(s)")

        api_key = live_settings.anthropic_api_key.get_secret_value()
        if not api_key:
            typer.echo("No ANTHROPIC_API_KEY configured; stopping before extraction.")
            return

        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=api_key)
        listings = []
        typer.echo("\n[3/3] Extracting...")
        for record in outcome.records:
            try:
                result = await extract_listing(record, recipe, client=client)
            except ExtractionError as exc:
                typer.echo(f"      extraction failed for {record.url}: {exc}")
                continue
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

        delta_report = apply_delta(
            conn, _DOMAIN, listings, allow_mass_withdrawal=outcome.fetched > 0
        )
        _print_delta("delta", delta_report)

    typer.echo(
        "\nLive demo complete. The simulated-redesign/self-heal narrative is only in "
        "`nimbus demo --offline`."
    )


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
    path is built around."""
    if offline:
        asyncio.run(_run_offline_demo())
    else:
        asyncio.run(_run_live_demo(_settings_for(ctx)))


if __name__ == "__main__":
    app()
