# Architecture

This document walks through what each component does, how data flows
between them, and what changes to actually run this in production rather
than as a proof of concept. See `docs/adr/` for the decisions behind each
choice, written at the time they were made.

## The central claim

An LLM is expensive and non-deterministic; a site's markup is stable for
long stretches between redesigns. So the LLM only ever does two jobs, both
bounded and auditable, and nothing else in the hot path touches a model at
all:

1. **Learn a site's structure once** (discovery), producing a small,
   versioned, schema-validated data artifact — a `SiteRecipe` — not a
   decision made fresh on every run.
2. **Turn one fetched page into structured fields** (extraction), one call
   per page, with its own deterministic guards around it and an audit row
   for every attempt.

Everything else — crawling, URL selection, pagination, delta computation,
health monitoring — is plain, deterministic, testable Python with zero LLM
dependency. `tests/test_crawler.py::test_crawler_module_contains_no_llm_client`
greps the crawler's own source for the two LLM client package names and
fails the build if either appears, so this separation cannot quietly rot.

```
 ┌──────────────┐   once per site    ┌───────────────┐
 │ discovery.py │ ───────────────────▶│  SiteRecipe   │ (versioned, in storage)
 │ (LLM agent,  │   via mcp_server.py │  (sitemap/    │
 │  claude_agent│   (3 tools, robots- │  index URLs,  │
 │  _sdk query) │   gated fetch)      │  URL patterns,│
 └──────────────┘                     │  field hints, │
        ▲                             │  confidence)  │
        │ re-run when health          └───────┬───────┘
        │ collapses (heal.py)                  │ read nightly
        │                                      ▼
 ┌──────┴───────┐                      ┌───────────────┐
 │  heal.py     │                      │  crawler.py   │  no LLM, no MCP
 │ site_health  │◀─────────────────────│  crawl_site   │  (ADR-0003)
 │ needs_heal   │  extraction_audit    │  (plain loop  │
 │ heal_site    │  rows (schema_valid, │  over Fetcher)│
 └──────────────┘  needs_review)       └───────┬───────┘
                                                │ CrawlRecord per page
                                                ▼
                                        ┌───────────────┐
                                        │ extractor.py  │  one LLM call/page
                                        │ extract_listing│ + deterministic
                                        │ record_extraction│ price/sale guards
                                        └───────┬───────┘
                                                │ Listing (or None)
                                                ▼
                                        ┌───────────────┐
                                        │  delta.py     │  new/changed/
                                        │ apply_delta   │  withdrawn/unchanged
                                        └───────────────┘
```

## Component walkthrough

### `fetch/` — the one place every HTTP request goes through

`Fetcher.get(url)` (`fetch/fetcher.py`) always resolves the domain's
robots.txt policy (`robots_for`) and checks `policy.can_fetch(url)` *before*
touching the on-disk cache miss path or the configured backend. A
disallowed URL raises `RobotsDisallowed` without ever reaching
`FetchBackend.fetch`; `tests/test_mcp_server.py::test_fetch_page_refuses_disallowed_url_without_calling_backend`
proves this with a backend that fails the test if it is ever called at all.
This is the project's core architectural claim: compliance is enforced in
code the agent must call, not requested of it in a prompt (ADR-0002,
ADR-0005).

`fetch/cache.py`'s `HtmlCache` is a flat, content-addressed (SHA-256 of the
normalised URL) JSON-per-page disk cache. It exists for three reasons at
once: avoiding redundant fetches during development, making the test suite
fast and fully offline, and — the use ADR-0004 documents — making
`nimbus demo --offline` reproducible by pre-seeding it with real fixture
pages instead of fetching them.

`fetch/backends.py` defines `FetchBackend` as a `Protocol` with two
implementations: `HttpxBackend` (a plain GET, the only one the offline test
suite or demo ever exercises) and `CrawlbaseBackend` (a seam for a paid
anti-bot/JS-rendering proxy, configured but unused by default). This is a
deliberate build-vs-buy split (ADR-0005): robots compliance is this
project's differentiator and is built; JS rendering and anti-bot evasion
are commodity problems and are bought.

`fetch/robots.py` parses robots.txt including `Crawl-delay` and the modern
`Content-Signal`/named-AI-agent conventions, resolved per user-agent.

It also **fails closed**, which the first version of this did not and
which matters more than the parsing does. RFC 9309 separates a robots.txt
that is *unavailable* (§2.3.1.3 — a 404: the site publishes none, so
everything is permitted) from one that is *unreachable* (§2.3.1.4 — a
transport error or a 5xx: the crawler "MUST assume complete disallow").
`robots_for` originally returned allow-all for every one of those cases,
cached whatever the backend returned including a 503 body, and never
expired it — so a single transient failure flipped a domain to allow-all
permanently, with no log and no way to notice. Now the branches are
split, each fallback logs a WARNING naming the domain and the branch, and
no non-2xx response is ever cached, on this path or the listing path.

### `mcp_server.py` — the discovery agent's only route to the web

Three tools, over local stdio MCP, and nothing else: `get_robots`,
`get_sitemap`, `fetch_page`. No generic HTTP fetch, no shell. `fetch_page`
delegates straight to `Fetcher.get()`; a disallowed URL comes back as
`{"error": "robots_disallowed"}`, never as raw HTML. `get_sitemap` expands
one level of sitemap index and returns a *stratified* sample (not just the
first N entries) because a real sitemap grouped by category — 396 rentals
then 679 sales, in one case — makes a naive head-sample useless.

### `discovery.py` — the agent that learns a site once

`discover_site(domain, settings=...)` drives a `claude_agent_sdk.query`
against the three MCP tools above, with `tools=[]` on the agent's own
built-in tool set (disabling Bash/Read/WebFetch/etc. entirely) so the MCP
tools are its *only* way to reach the web — not merely its suggested way.
The agent's final JSON answer is parsed (`parse_recipe_payload`) and
structurally validated (`validate_recipe`) — checking things a pydantic
schema cannot express, chiefly that `listing_url_patterns` actually matches
the `sample_listing_urls` the agent *says* it opened and confirmed --
self-reported and unverified, which is its own known weakness (see "What
we'd fix next"). One retry, with
the validation problems fed back to the agent, is attempted before raising
`DiscoveryError`.

Real discovery runs against three live sites surfaced genuinely
heterogeneous problems, which is the point of doing this once per site
rather than assuming a universal page shape:

- `ashworthholme.co.uk`'s listing URLs put the town of **Sale** (Cheshire)
  in the same URL segment a naive heuristic would read as the deal type
  ("for sale"), and lettings/commercial listings share the exact same URL
  pattern as sales — the recipe's `notes` field records that separation has
  to come from page content (a status heading), not the URL.
- `jaksandco.co.uk` has no sitemap at all. The agent fell back to its
  listing index and found a `?department=residential-sales` filter that
  excludes the lettings results the unfiltered index mixes in.
- `bentleyhurst.co.uk` is a client-rendered Gatsby site: the agent set
  `render_required: true` and `confidence: 0.3`, and stated plainly in its
  own notes that it could not confirm price, address, or bedroom count from
  the fetched HTML — a recipe honestly reporting its own limits rather than
  guessing.

A known limitation, found by running this for real rather than assumed in
advance: `get_sitemap` can hand the agent on the order of 200KB of JSON for
a large site, and in practice the agent reported reading only a fraction of
it before answering. See "What we'd fix next," below.

### `crawler.py` — the nightly hot path, with zero LLM/agent dependency

`crawl_site(recipe, fetcher=..., crawl_delay=...)` is a plain loop:
`collect_candidate_urls` walks the recipe's sitemap (falling back to its
listing index + pagination rule when there is no sitemap), `select_listing_urls`
narrows that down by `listing_url_patterns`, and each selected URL is
fetched through the same `Fetcher.get()` the agent uses, skipping (not
aborting the run for) a single `RobotsDisallowed` or `FetchError`. It
returns a `CrawlOutcome` carrying the counts an orchestrator needs for a
truthful log line and `crawl_runs` row — candidates / selected (before any
`--limit`) / attempted / fetched / skipped-by-reason — because those
counts are gone once a skipped URL is discarded and must not require
re-crawling to recover. `cli.crawl` writes the `crawl_runs` row from them.

Every fetch the run makes, candidate collection included, is spaced by
the same `Pacer`. Pacing only the listing loop (as this originally did)
meant each run opened with an unpaced burst across a sitemap index's
children, against the sites most likely to have declared a `Crawl-delay`.

`crawl_delay` is deliberately **not** read from the recipe. It is resolved
fresh, at the orchestration call site (`cli.py`'s `crawl` command and
`nimbus.demo`'s own crawl steps), via `await fetcher.robots_for(domain)` —
live robots.txt policy, which can tighten between recipe refreshes, whereas
the recipe itself can go weeks without being re-discovered.

### `extractor.py` — one LLM call per page, never trusted alone

`extract_listing(record, recipe, client=...)` makes exactly one
`anthropic` Messages API call per `CrawlRecord`, asking the model to copy
the literal status-and-price text it sees (`price_raw`) rather than compute
a number — `parse_price` and `is_residential_sale`, both pure and
deterministic, decide the actual pounds figure, the qualifier (`poa`,
`rental`, `range`, ...), and whether the page is even an open sale at all.

The model's reported price is then cross-checked against page text scoped
to the recipe's own `field_hints` price location, every top-level heading,
and any element that names itself a price widget by class/id — **never the
whole page**. `ashworthholme.co.uk`'s real fixtures carry 48 unrelated
pound amounts (£350 to £10,000,000) inside a price-filter `<select>`
against a true price of £650,000; a bare substring-in-page check passes
every one of them, including the wrong ones, which is why corroboration is
scoped this tightly and why form controls (`select`/`option`/`form`/
`input`/`datalist`) are stripped before any of the three corroboration
rules ever see the page.

A page offering *no* corroboration location at all is rejected before the
model is even called — `bentleyhurst.co.uk`'s real fixtures are
client-rendered shells with zero headings and zero pound signs in the
fetched HTML, so there is nothing to validate a model's answer against, and
paying for a call that can never be checked either way is pointless.

Every attempt — successful or not — is recorded via `record_extraction`
into the `extraction_audit` table: `schema_valid`, `needs_review`, the
recipe version, the page's content hash, and token/cost fields. This is
the table `heal.py`'s health monitor reads, and it is the project's audit
trail.

### `heal.py` — turning silence into a number, and a number into a repair

A recipe that extracted perfectly last night can silently stop matching
tonight — a redesign, not a bug — and `crawl_site` still "succeeds," just
against a recipe that no longer reaches anything real. `site_health` reads
the most recent `window` `extraction_audit` rows for a domain and computes
`schema_valid_rate`/`needs_review_rate`. It deliberately reads these two
raw audit columns, not `ExtractionResult.overall_confidence` — Task 5's own
finding is that `overall_confidence` only ever takes the values `1.0` or
`0.6` and carries no information `needs_review` doesn't already carry.

`needs_heal` gates on `schema_valid_rate`, not `needs_review_rate`:
`needs_review` fires on one dropped field with a `Listing` still produced;
a falling `schema_valid_rate` means extraction is failing outright, the
stronger and rarer signal of genuine recipe drift. `heal_site` re-runs
`discover_site` for the domain, saves the result as a new recipe version —
`storage.save_recipe` is append-only, so a prior version is never lost and
is always available to diff against or fall back to — and returns a
human-readable diff of what changed.

### `delta.py` — new / price-changed / withdrawn / unchanged

`apply_delta` compares one run's extracted listings against what storage
already has for a domain, by `source_url` (the one stable key across a
redesign, a price change, or a relist — never address text). The cost of
that key, which matters downstream rather than here: the same physical
property marketed by several agents enters as several rows, with
addresses written several different ways and asking prices that may
contradict each other, so anything that counts properties (the pricing
model's comparable sets and training data above all) must deduplicate to
a physical property first — see `docs/pricing-model.md` §1. It persists
the new state and writes a `listing_history` row for every real price
change. A guard refuses to report more than half of a domain's stored
listings as withdrawn in one run unless the caller explicitly passes
`allow_mass_withdrawal=True` — a failed or partially failed crawl must
never read as an overnight sell-off — and, when that override *is* passed
and a mass withdrawal does proceed, it is logged at `WARNING` so the one
most consequential write this module can make is never silent.

That guard is only as good as what a caller passes into it, which is why
`delta.py` also owns `should_allow_mass_withdrawal(outcome, results)`: every
orchestration call site derives `apply_delta`'s `allow_mass_withdrawal`
from it, never from a bare `outcome.fetched > 0`. `fetched > 0` is
satisfied by a single lucky page out of a thousand selected ones —
exactly the failed-crawl-looks-like-a-sell-off shape the guard above exists
to catch, reopened one layer up if the integration code derives the flag
from "something worked" instead of from how complete the crawl actually
was. `should_allow_mass_withdrawal` instead requires all four of:

- no explicit `--limit` was applied (a partial run by request is a slice
  of the site, not evidence about the pages it never opened),
- at least 90% of the crawl's own *untruncated* selected URL set was
  actually fetched (`MASS_WITHDRAWAL_FETCH_RATIO`),
- extraction produced results at all, and
- at least half of the results it could judge yielded a `Listing`
  (`MASS_WITHDRAWAL_EXTRACTION_RATIO`), with deliberate non-sale
  exclusions left out of that denominator.

The last of those is the one that is easy to miss and does the most work.
Withdrawal is driven by extraction, not by fetching: a site redesign
leaves every URL resolving perfectly, so the crawl scores a fetch ratio
of 1.0 while every extraction fails against markup the recipe no longer
matches — the exact scenario the heal subsystem exists for, and the one
the fetch ratio alone waves straight through.

### `cli.py` — the orchestration layer, and `demo.py` — the presentation path

`cli.py` is five thin commands: `onboard`, `crawl`, `heal`, `report`, and
`demo` (which just calls into `nimbus.demo`). `crawl` is where the
`crawl_delay = await fetcher.robots_for(domain)` call site and the
`should_allow_mass_withdrawal(outcome, results)` derivation both actually live —
Task 4-6 built the primitives; Task 7 is where they are wired together into
something that runs end to end.

`demo.py` is a separate module, not a larger `cli.py`, because it is a
different responsibility from wiring a command to a function: it is
roughly three hundred lines of fixtures, stub classes, and two full
end-to-end narrators (`run_offline_demo`, `run_live_demo`). `console.py` is
smaller still — just the printed-output helpers (`banner`, `print_delta`,
`print_health`, `format_price`) both `cli.py` and `demo.py` need, split out
to avoid a circular import between the two. See
`docs/adr/0004-cache-first-fetch-for-reproducible-demos.md` for how
`demo --offline` drives the same real code deterministically — and for the
honest limit on what that proves: `run_offline_demo`'s own printed output
says, in the room, that its repair step is a stubbed fixture, not the agent
inferring the fix live.

## The production path

This repo demonstrates the architecture; it is not the production system.
Getting from here to "continuously collects listings from hundreds of
estate-agent sites" needs, roughly in order of how soon each would bite:

1. **A scheduler**, not a CLI invoked by hand — `crawl` for every onboarded
   domain, nightly, with its own retry/backoff and alerting on the two
   `crawl_site` ERROR-log shapes (pattern-mismatch vs. all-fetches-failed)
   that currently only reach a log stream.
2. **Real token/cost accounting in `extractor.py`'s audit rows.** `extract_listing`
   does not currently surface the raw Anthropic response's token usage back
   to its caller, so `cli.py`'s `crawl` command records `0` for
   `input_tokens`/`output_tokens`/`cost_usd` rather than the real numbers.
   `schema_valid`/`needs_review` (what `site_health` actually reads) are
   unaffected, but real cost dashboards need this wired through.
3. **A headless-render backend wired up for `render_required` sites.**
   `CrawlbaseBackend` is the configured seam (ADR-0005); `bentleyhurst.co.uk`
   is a live example of a site that needs it — its recipe already says so.
4. **`get_sitemap`'s response size.** A ~208KB sitemap payload is a real
   measured problem, not a hypothetical one (see "What we'd fix next"
   below) — it should be paginated or summarised before the agent ever sees
   it, rather than truncated by however much of it the agent happens to
   read.
5. **A real queue/worker split** so one slow or stuck site's crawl cannot
   delay every other site's nightly run, and Crawlbase/proxy costs can be
   budgeted and rate-limited centrally rather than per-process.
6. **Multi-tenant/multi-region config** — `Settings` is currently a single
   global `.env`; a fleet of sites in different jurisdictions needs
   per-site configuration (different UA strings for different robots.txt
   blocks already matched by name, different crawl concurrency limits).
7. **The pricing model itself** — see `docs/pricing-model.md`. Nothing in
   this repository implements it; it is the answer to the assignment's
   second half, built on top of what this repository collects.

## What we'd fix next (found by actually running this)

- **`get_sitemap`'s payload size.** Confirmed against a real site: the tool
  can hand the agent on the order of 200KB of JSON, and in the
  `bentleyhurst.co.uk` run the agent itself reported reading only about a
  fifth of `properties.xml` before answering — meaning its recipe is built
  from a partial view of the sitemap by construction, not by a bug. This is
  the single most concrete thing worth fixing before trusting discovery
  against a much larger site: paginate or stratify-and-cap `get_sitemap`'s
  response server-side, the way `_stratified_sample` already caps
  `sample_urls`, rather than leaving the cap to whatever the agent happens
  to read.
- **Extraction cost accounting**, as above — wire the real Anthropic
  response usage through `extract_listing`'s return value instead of
  recording `0` tokens in `cli.py`.
- **A render-capable backend actually wired to a real site.**
  `CrawlbaseBackend` exists and is configured but has never been exercised
  against a real JS-rendered page in this repository.
