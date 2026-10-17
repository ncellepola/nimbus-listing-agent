# Nimbus Listing Agent — Design Spec

**Date:** 2026-10-17
**Status:** Approved (chat), pending full written solution document
**Scope:** Proof of concept for the *agentic AI & data architecture* half of the Nimbus assignment. The pricing model is answered in prose only (`docs/pricing-model.md`), not built.

## Intent

Nimbus needs residential listings collected daily from *many* estate-agent websites. The hard part is not fetching pages — it is that every estate agent has a different site, built by a different web shop, and those sites change without warning. Hand-writing a scraper per site does not scale past a few dozen and rots continuously.

This PoC demonstrates the architectural answer: **an LLM agent learns a site once and emits a reusable recipe; deterministic code executes that recipe every night; a health monitor detects drift and sends the agent back in to repair the recipe.**

That split is the thesis. Agentic AI is used where the work is rare, messy and open-ended (understanding an unseen website). Conventional code is used where the work is high-volume and well-defined (fetching known URLs on a schedule). Running an LLM against every page of every site every night would be both expensive and non-deterministic.

## Goals

- Onboard an estate-agent site from nothing but a bare domain, with zero per-site code.
- Collect listings daily and classify each as new / price-changed / withdrawn / unchanged.
- Detect when extraction quality degrades and repair the site recipe automatically.
- Respect site crawling policy as an enforced property of the system, not a prompt instruction.
- Produce a full audit trail: which model, which prompt version, which recipe version, what it cost.
- Demonstrate the above live, offline, deterministically.

## Non-goals

- Production scale. Three sites, tens of listings.
- The pricing model as code.
- Postgres, queues, orchestrators, dashboards, authentication, deployment.
- JavaScript-rendered sites on the demo path (the backend seam exists; the demo does not depend on it).

## Anti-goals

Things that would make this a failure even if it ran:

- A demo that needs the network and dies in the room.
- An LLM call on the per-listing hot path presented as a scalable design.
- Robots compliance implemented as a sentence in a system prompt.
- Per-site hardcoded CSS selectors with the agent bolted on decoratively.

## Constraints

- 8 hours of build time.
- Python 3.12, `uv`, pydantic, SQLite, pytest, ruff, GitHub Actions.
- Public repo `github.com/ncellepola/nimbus-listing-agent`, visible SDLC trail: one issue → one branch → one PR per feature, conventional commits, CI green on every PR, ADRs in `docs/adr/`.
- Secrets from `.env` only; never committed.
- Tests must not hit the network. Cached HTML fixtures only.

## Architecture

```
                     ┌─────────────────────────────┐
  bare domain  ──▶   │  discovery agent            │  Claude Agent SDK
                     │  (explores, reasons)        │  + local MCP tools
                     └──────────────┬──────────────┘
                                    │ emits
                                    ▼
                           ┌─────────────────┐
                           │  SiteRecipe     │  versioned, persisted
                           │  (JSON, schema- │
                           │   validated)    │
                           └────────┬────────┘
                                    │ consumed nightly by
                                    ▼
  ┌──────────────┐      ┌───────────────────────┐      ┌──────────────────┐
  │ fetch layer  │ ◀──  │  deterministic crawler│ ──▶  │  LLM extractor   │
  │ robots gate  │      │  (zero LLM)           │      │  → Listing       │
  │ disk cache   │      └───────────────────────┘      │  + confidence    │
  └──────────────┘                                     └────────┬─────────┘
                                                                │
                           ┌────────────────────────────────────┤
                           ▼                                    ▼
                    ┌─────────────┐                     ┌───────────────┐
                    │ delta       │                     │ health monitor│
                    │ new/changed │                     │ drift detect  │
                    │ /withdrawn  │                     └───────┬───────┘
                    └─────────────┘                             │ below threshold
                                                                ▼
                                                        re-run discovery,
                                                        bump recipe version
```

### Components

**Fetch layer** (`nimbus.fetch`). Robots policy, disk cache, pluggable backend. Cache-first: a URL already on disk is never re-fetched, so the demo runs offline and byte-identically. Backend is `httpx` by default, Crawlbase optional for JS rendering and anti-bot. Robots parsing covers the modern case: `Crawl-delay`, wildcard paths, and AI-specific directives (`Content-Signal`, named agents such as `ClaudeBot`/`GPTBot`).

**MCP server** (`nimbus.mcp_server`). Local stdio MCP exposing `get_robots`, `get_sitemap`, `fetch_page`. This is the agent's only route to the web. The robots gate lives *inside* `fetch_page`, so a disallowed URL is refused by the tool — the model cannot talk its way past it. Pages are returned as markdown by default, which costs far fewer input tokens than raw HTML.

**Discovery agent** (`nimbus.discovery`). Claude Agent SDK, MCP tools allow-listed. Given a domain, it reads robots, walks the sitemap (or finds a listing index when there is no sitemap), opens sample pages, works out which URLs are property listings and where the fields live, and returns a `SiteRecipe`. Output is schema-validated; invalid output is retried once, then surfaced rather than persisted.

**Crawler** (`nimbus.crawler`). Deterministic, no LLM, no MCP. Reads the recipe, enumerates listing URLs, fetches with the crawl delay honoured. This is the part that runs every night across every site, so it must be cheap and predictable.

**Extractor** (`nimbus.extractor`). One Claude call per page via the plain Anthropic SDK (a single transformation needs no agent loop), returning a schema-validated `Listing` with per-field confidence. Deterministic pre-parsing handles price qualifiers and filters rentals and sold-STC pages before they reach the sales dataset. Every call writes an audit row.

**Health and heal** (`nimbus.heal`). Per-site schema-valid rate over a rolling window. Below threshold means the site changed; discovery re-runs, the recipe diff is recorded, the version bumps. Demonstrated by mutating a cached page and watching the system notice and recover.

**Delta** (`nimbus.delta`). Nightly diff producing new / price-changed / withdrawn / unchanged, with price history retained.

**Storage.** SQLite: `sites`, `recipes` (versioned), `listings`, `listing_history`, `extraction_audit`, `crawl_runs`.

## Key decisions (ADR-worthy)

1. **Agent learns, code executes.** LLM cost is per-site-onboarding, not per-listing. Recorded as ADR-0002.
2. **MCP for exploration, direct calls for the hot path.** MCP is a protocol for exposing tools to an LLM client; a nightly batch crawler is not an LLM client. Recorded as ADR-0003.
3. **Compliance at the tool boundary.** Policy enforced in code the agent calls, not in the prompt.
4. **Cache-first fetch.** Reproducible demo, polite crawling, cheap re-runs.
5. **Build the MCP server rather than buy one** for the demo path, keeping Crawlbase as a configured backend for the production path where anti-bot and JS rendering are commodity problems worth buying.

## Sites

`ashworthholme.co.uk` (flat sitemap, direct listing URLs), `jaksandco.co.uk` (WordPress sitemap index, separate property sitemap), `bentleyhurst.co.uk` (split sitemap index). Three genuinely different structures. `henstockps.co.uk` held in reserve as a `Crawl-delay: 2` test. `normie.co.uk` deliberately excluded: its robots carry `Content-Signal: ai-train=no` and name AI agents explicitly — it becomes a test fixture for the AI-directive parser rather than a crawl target.

## Left to the builder

Prompt wording, retry and backoff specifics, CLI output formatting, log format, exact confidence formula, and the choice of markdown converter.
