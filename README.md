# Nimbus Listing Agent

A proof of concept, built for an interview exercise, answering the first
half of the brief: "how would you build a platform that continuously
collects residential listings from many estate-agent websites?" (The
second half — pricing assessment — is answered in writing, not in code; see
`docs/pricing-model.md`.)

The answer this repository argues for: an LLM agent learns a previously
unseen site **once**, producing a small, versioned, auditable data artifact
(a `SiteRecipe`); deterministic code executes that recipe every night with
**zero LLM calls on the hot path**; a health monitor watches extraction
quality and sends the agent back in only when a site's own markup has
changed enough to need it. Compliance (robots.txt) is enforced in code the
agent is physically unable to bypass, not requested of it in a prompt.

## Architecture, in one diagram

```
 discovery.py (LLM agent, once per site)
       │  via mcp_server.py: 3 tools, robots-gated fetch, nothing else
       ▼
   SiteRecipe  ──────────────────────────────────────▶  storage (versioned, append-only)
       │  read nightly, no LLM, no MCP (ADR-0003)
       ▼
   crawler.py ──CrawlRecord──▶ extractor.py ──Listing──▶ delta.py
   (crawl_site)   (1 LLM call/page    (extract_listing,     (apply_delta: new/
                   + deterministic     record_extraction)    price_changed/
                   price/sale guards)                         withdrawn/unchanged)
       │                            │
       └────────── extraction_audit rows ──────────────▶  heal.py
                                                            (site_health, needs_heal,
                                                             heal_site re-runs discovery
                                                             when extraction quality
                                                             collapses)
```

Full component-by-component walkthrough and the production path (what this
PoC does *not* yet do, in priority order): `docs/architecture.md`. Decisions
made along the way, with their context and trade-offs: `docs/adr/`.

## Quickstart

```bash
uv sync --extra dev
cp .env.example .env   # fill in a real ANTHROPIC_API_KEY before running onboard/crawl/heal
uv run pytest -m "not live" -q
uv run ruff check .
uv run ruff format --check .
```

Tests never touch the network or require a key — everything under
`tests/` runs against fixtures in `tests/fixtures/` (real HTML captured
from three live estate-agent sites, real recipes the discovery agent
genuinely produced against them, and a simulated redesign used to prove
the self-heal path). `@pytest.mark.live` tests call the real agent and the
real network and are excluded by default; run them deliberately with
`-m live` if you want to spend real API credits.

## The demo

```bash
uv run nimbus demo --offline
# or:
./scripts/demo.sh   # runs it twice and diffs the output to prove it's reproducible
```

This is the command built for a 15-20 minute presentation. It makes **no
network call and no agent call at all** (pre-flight ruling R7 — the spec
names "a demo that dies in the room" as the thing to avoid), and produces
identical output on every run, while still exercising the real
`crawl_site`/`extract_listing`/`apply_delta`/`heal_site` code — not a
separate demo-only implementation of any of them. See
`docs/adr/0004-cache-first-fetch-for-reproducible-demos.md` for exactly how.

It narrates five beats against `ashworthholme.co.uk`, using a recipe the
discovery agent genuinely captured against the live site:

1. **Onboarding** — show a real captured `SiteRecipe` (confidence, sitemap,
   URL pattern, field hints).
2. **Crawl + extract** — fetch from a pre-seeded cache, extract two real
   listing pages, show the delta against empty storage (both `new`).
3. **A simulated site redesign** — swap in mutated page markup where the
   address/price moved out of `<h1>`/`<h2>` into renamed `<div>`s the old
   recipe's selectors no longer reach.
4. **Health collapse** — `schema_valid_rate` drops to 0%; `needs_heal`
   flips to `True`.
5. **Repair** — re-discovery (stubbed with a committed fixture, not a live
   agent call), a version bump (v1 → v2), a readable diff of exactly which
   field hints changed, and extraction recovering to 100%.

Other commands (`onboard <domain>`, `crawl <domain>`, `heal <domain>`,
`report [--domain D]`) are the real thing — they build a real `Fetcher`/
`anthropic` client from `Settings` and do touch the network and (for
`onboard`/`heal`) the live discovery agent. Every command takes a global
`--db PATH` (default: `Settings().db_path`) so you can point any of them at
a specific SQLite file; `demo` ignores it and always uses its own
throwaway database, precisely so its output doesn't depend on what else
you've onboarded.

## What is not built

Honestly, in priority order of what would actually bite first in
production:

- **The pricing model.** Nothing in this codebase implements "is this
  listing priced below/around/above market" — that is a written design in
  `docs/pricing-model.md`, not code. This platform is what the pricing
  model would be trained on (via the Land Registry Price Paid join
  described there), not an implementation of it.
- **A scheduler.** `crawl`/`heal` are CLI commands invoked by hand; there
  is no cron/queue/worker running them nightly across a fleet of onboarded
  sites, and no alerting wired to the two distinct ERROR-log shapes
  `crawl_site` already distinguishes (recipe drift vs. transient fetch
  failure).
- **Real extraction cost accounting.** `extract_listing` does not surface
  the Anthropic response's token usage back to its caller, so `cli.py`
  records `0` for `input_tokens`/`output_tokens`/`cost_usd` rather than the
  real numbers. `schema_valid`/`needs_review` — what the health monitor
  actually reads — are unaffected.
- **JS-rendering actually wired up.** `CrawlbaseBackend` is a configured
  seam (`fetch_backend = "crawlbase"` in `Settings`) for sites like
  `bentleyhurst.co.uk` that need a headless browser; it has never been
  exercised against a real page in this repository.
- **`get_sitemap`'s response size.** Confirmed against a real site, not
  hypothetical: a large sitemap can hand the discovery agent on the order
  of 200KB of JSON, and in practice the agent read only a fraction of it
  before answering. Worth fixing before trusting discovery against a much
  larger site than the three this was built and tested against.
- **Multi-tenant configuration.** `Settings` is one global `.env`; a real
  fleet needs per-site crawl concurrency, UA strings, and backend choice,
  not one process-wide config.

## Repository layout

```
src/nimbus/
  config.py        Settings (pydantic-settings, reads .env)
  models.py        Listing, SiteRecipe, CrawlRecord/Outcome, ExtractionResult, SiteHealth, DeltaReport
  storage.py        SQLite schema, versioned recipe CRUD, listing/audit persistence
  fetch/            Robots-gated, cache-first Fetcher; HtmlCache; HttpxBackend/CrawlbaseBackend
  mcp_server.py     The discovery agent's only route to the web (3 tools, stdio MCP)
  discovery.py      The LLM agent that learns a site once -> SiteRecipe
  crawler.py        The deterministic nightly crawl -- zero LLM dependency
  extractor.py      One LLM call per page + deterministic price/sale guards + audit trail
  heal.py           site_health / needs_heal / heal_site
  delta.py          apply_delta: new / price_changed / withdrawn / unchanged
  cli.py            onboard / crawl / heal / report / demo
docs/
  architecture.md           component walkthrough + production path
  pricing-model.md          the written answer to the assignment's second half
  adr/                      decisions, written when made
tests/
  fixtures/                 real captured recipes, real captured pages, a simulated redesign
scripts/demo.sh             runs the offline demo twice and diffs the output
```

## Development

```bash
uv sync --extra dev
uv run pytest -m "not live" -q
uv run ruff check .
uv run ruff format --check .
```

Copy `.env.example` to `.env` and fill in secrets before running `onboard`,
`crawl`'s extraction step, or `heal` — anything that calls the Anthropic
API for real. `.env` is gitignored and must never be committed. Tests and
`demo --offline` never touch the network or require a key.
