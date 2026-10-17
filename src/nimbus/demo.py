"""The `nimbus demo` harness: a narrated, two-mode walkthrough of the
pipeline Tasks 1-6 built, kept separate from `cli.py` because it is a
different responsibility from wiring a typer command to a function --
roughly three hundred lines of fixtures, stub classes, and two full
end-to-end runners live here, and `cli.py`'s own `demo` command is two
lines that call into this module.

`run_offline_demo()` is this project's answer to "a demo that dies in the
room" (the spec's own named anti-goal, pre-flight ruling R7): it makes *no*
network call and *no* agent call at all, runs the same five architectural
beats every other command is built from, and produces byte-identical output
on every run. It does this by loading a recipe straight from a committed
fixture file instead of calling `discover_site`, pre-seeding `HtmlCache`
with real captured pages instead of fetching them, and running extraction
against a canned stub client instead of the real `anthropic` API -- never
by skipping the real `crawl_site`/`extract_listing`/`apply_delta`/
`heal_site` code. See
`docs/adr/0004-cache-first-fetch-for-reproducible-demos.md`.

`run_live_demo()` is the real thing against the real `ashworthholme.co.uk`:
it calls the live discovery agent and the live Anthropic extraction API,
and does not attempt to reproduce the simulated-redesign/self-heal beats,
because there is no live redesign to show on demand.

A caveat that matters enough to say twice, once here and once in the
printed narration itself (not only in this docstring, the ADR, the README,
and `heal_site`'s own docstring -- none of which a live audience sees):
`run_offline_demo`'s step 5 does not prove the discovery agent can re-learn
a real redesign. It substitutes a committed fixture, built by a human
reading the mutated markup, for the one call that would do that. The
printed narration says so, in the room, not only in source comments.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

import typer

import nimbus.heal as heal_module
from nimbus import console, storage
from nimbus.config import Settings
from nimbus.crawler import crawl_site
from nimbus.delta import apply_delta, should_allow_mass_withdrawal
from nimbus.discovery import DiscoveryError, discover_site
from nimbus.extractor import ExtractionError, extract_listing, record_extraction
from nimbus.fetch.backends import HttpxBackend
from nimbus.fetch.cache import HtmlCache
from nimbus.fetch.fetcher import Fetcher
from nimbus.heal import heal_site, needs_heal, site_health
from nimbus.models import CrawlOutcome, ExtractionResult, Listing, SiteRecipe

# src/nimbus/demo.py -> nimbus -> src -> repo root, same convention
# nimbus.discovery._REPO_ROOT uses -- computed from this file's own
# location, not the process cwd, so `nimbus demo --offline` finds the
# committed fixtures regardless of where it is invoked from.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURES_DIR = _REPO_ROOT / "tests" / "fixtures"

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
) -> tuple[list[Listing], list[ExtractionResult]]:
    """Extract every record in `outcome` against the stub client, record
    each attempt's audit row, and return the successfully extracted
    `Listing`s alongside every `ExtractionResult`. Both are needed: the
    listings are what `apply_delta` compares, the results are what
    `should_allow_mass_withdrawal` reads to tell a redesign from a
    sell-off. Mirrors `tests/test_heal.py::_extract_and_record`."""
    listings: list[Listing] = []
    results: list[ExtractionResult] = []
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
        results.append(result)
        slug = record.url.rstrip("/").rsplit("/", 1)[-1]
        if result.listing is not None:
            extracted_price = console.format_price(result.listing.price_gbp)
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
    return listings, results


async def run_offline_demo() -> None:
    console.banner("NIMBUS OFFLINE DEMO -- no network call, no agent call, fully reproducible")

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
            f"{outcome.fetched} fetched (crawl_delay={policy.crawl_delay}s -- this site's "
            "robots.txt declares no Crawl-delay, so this is the configured default)"
        )
        listings, results = await _extract_and_record(conn, recipe, outcome)
        first_delta = apply_delta(
            conn,
            recipe.domain,
            listings,
            allow_mass_withdrawal=should_allow_mass_withdrawal(outcome, results),
        )
        console.print_delta("      delta", first_delta)

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
        console.print_health("      health immediately after the redesign", health_before)
        if needs_heal(health_before):
            typer.echo("      HEALTH COLLAPSED -- field_hints no longer match this site's markup")

        typer.echo("\n[5/5] Repairing: bumping the recipe version")
        # Said here, in the room, not only in this module's docstring, the
        # ADR, the README, and heal_site's own docstring: re-discovery
        # below is a committed fixture a human derived by reading the
        # mutated HTML, not the agent inferring the fix live. `nimbus heal`
        # is the command that actually calls the agent.
        typer.echo("      (offline mode: re-discovery is stubbed from a committed fixture --")
        typer.echo("       run `nimbus heal ashworthholme.co.uk` for the real agent)")
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
        recovered_listings, recovered_results = await _extract_and_record(
            conn, new_recipe, recovered_outcome
        )

        health_after = site_health(conn, recipe.domain, window=len(_PAGES))
        console.print_health("      health after the repair", health_after)

        recovered_delta = apply_delta(
            conn,
            recipe.domain,
            recovered_listings,
            allow_mass_withdrawal=should_allow_mass_withdrawal(
                recovered_outcome, recovered_results
            ),
        )
        console.print_delta("      post-heal delta", recovered_delta)

    console.banner("DEMO COMPLETE")


async def run_live_demo(settings: Settings) -> None:
    """The non-`--offline` path: the same onboard/crawl/extract narrative,
    for real, against the real `ashworthholme.co.uk`. Calls the live
    discovery agent and the live Anthropic extraction API; costs real
    money and depends on the site answering the same way twice, which is
    exactly why `--offline` exists for the room. The simulated-redesign /
    self-heal beats are not reproduced here -- there is no live redesign to
    show on demand -- see `run_offline_demo` for the full narrative."""
    console.banner(
        f"NIMBUS LIVE DEMO against {_DOMAIN} -- this calls the real network and the real agent"
    )

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
        typer.echo(
            f"\n[2/3] Crawling live (crawl_delay={policy.crawl_delay}s -- from robots.txt if it "
            "declares a Crawl-delay for us, otherwise the configured default)..."
        )
        outcome = await crawl_site(recipe, fetcher=fetcher, crawl_delay=policy.crawl_delay, limit=3)
        typer.echo(f"      fetched {outcome.fetched}/{outcome.selected} selected page(s)")

        api_key = live_settings.anthropic_api_key.get_secret_value()
        if not api_key:
            typer.echo("No ANTHROPIC_API_KEY configured; stopping before extraction.")
            return

        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=api_key)
        listings: list[Listing] = []
        results: list[ExtractionResult] = []
        typer.echo("\n[3/3] Extracting...")
        for record in outcome.records:
            try:
                result = await extract_listing(record, recipe, client=client)
            except ExtractionError as exc:
                typer.echo(f"      extraction failed for {record.url}: {exc}")
                continue
            results.append(result)
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
            conn,
            _DOMAIN,
            listings,
            allow_mass_withdrawal=should_allow_mass_withdrawal(outcome, results),
        )
        console.print_delta("delta", delta_report)

    typer.echo(
        "\nLive demo complete. The simulated-redesign/self-heal narrative is only in "
        "`nimbus demo --offline`."
    )
