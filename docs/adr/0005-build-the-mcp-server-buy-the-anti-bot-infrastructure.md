# ADR-0005: Build the MCP fetch server; buy the anti-bot infrastructure

## Status

Accepted

## Context

The discovery agent (Task 3) needs a way to look at an unseen site:
read its robots.txt, pull its sitemap, and fetch a sample of pages. Two
different problems hide inside "a way to look at a site":

1. **Compliance and control.** Which URLs the agent is allowed to visit,
   whether a page comes from cache or the network, and how much HTML the
   agent sees per call, must be enforced by something other than the
   agent's own judgement — an LLM asked nicely to "respect robots.txt" is
   not an enforcement mechanism, it is a suggestion the model can ignore,
   misread, or forget under context pressure. The demo path must also be
   offline and deterministic: tests and CI cannot depend on a live site
   answering the same way twice.
2. **Getting the bytes back at all.** Some sites render listings via
   client-side JS, sit behind anti-bot/CDN challenges, or rate-limit by
   IP. Solving that generally — headless browser rendering, proxy
   rotation, CAPTCHA/challenge handling, fingerprint rotation — is a deep,
   constantly-shifting problem that vendors (Crawlbase, Firecrawl, and
   others) already sell as a commodity service.

These two problems do not belong to the same decision. Problem 1 is the
project's core architectural claim — compliance enforced in code — and
delegating it to a third-party API would mean trusting that API's
policy, not ours, and losing the ability to unit-test the enforcement
offline. Problem 2 is infrastructure plumbing with no differentiating
value to this project; reimplementing JS rendering or proxy rotation
here would be effort spent on a problem already solved elsewhere, for a
proof of concept that needs to demonstrate the recipe-learning idea, not
a scraping platform.

## Decision

Build a small local stdio MCP server (`nimbus/mcp_server.py`) that is the
discovery agent's *only* route to the web: three tools —
`get_robots`, `get_sitemap`, `fetch_page` — and nothing else. No generic
HTTP fetch tool, no shell, is ever given to the agent. `fetch_page`
delegates to `Fetcher.get()` (Task 1), which resolves the site's robots
policy and checks it before the cache-miss path or any backend is
touched; a disallowed URL comes back as a structured
`{"error": "robots_disallowed"}` result, and the backend is provably
never called (`tests/test_mcp_server.py::test_fetch_page_refuses_disallowed_url_without_calling_backend`
is the single most important test in this repository for exactly this
reason). `fetch_page` also converts HTML to markdown by default,
stripping script/style/nav/footer content before conversion, because raw
listing-page HTML runs to tens of thousands of tokens and the markdown
is a fraction of that — the difference between a viable system and an
unaffordable one at 500 sites.

Buy, rather than build, the anti-bot/JS-rendering layer. `FetchBackend`
is a `Protocol` (Task 1) with two implementations: `HttpxBackend`, a
plain HTTP GET, is the default and the only backend the offline test
suite or demo ever exercises. `CrawlbaseBackend` is the configured seam
— `fetch_backend = "crawlbase"` plus `crawlbase_token` in `Settings` —
for sites that need JS rendering or anti-bot evasion. It is not
exercised by tests; it exists so that a real deployment can point
problem sites at a paid service without touching `Fetcher`, the MCP
tools, or anything upstream of the backend boundary.

That last claim was initially only half true: the `fetch_backend`
setting was read by `mcp_server.main()` alone, so the nightly crawl path
and the live demo both hardcoded `HttpxBackend` and setting
`FETCH_BACKEND=crawlbase` silently changed nothing for them. All three
entry points now build their backend through
`nimbus.fetch.backends.build_backend(settings)`, which is the one place
the switch is read.

## Consequences

- The enforcement point for robots compliance is a few lines of Python
  inside `fetch_page`/`Fetcher.get()`, fully covered by offline tests
  that assert the backend is never called for a disallowed URL. No
  prompt, and no third-party API's own policy, stands between the agent
  and that enforcement.
- The discovery agent cannot fetch anything the MCP server does not
  expose — there is no generic fetch tool to misuse or to be tricked
  into calling with an arbitrary URL and no robots check.
- JS-heavy or anti-bot-protected sites are out of scope for the httpx
  backend and for this proof of concept's demo path; they are deferred
  to `CrawlbaseBackend`, which trades a per-request cost for not having
  to build and maintain browser automation, proxy rotation, or challenge
  solving ourselves.
- Markdown conversion is lossy (tables, exact DOM structure, and some
  inline formatting do not survive); this is an accepted trade against
  token cost, and `fetch_page(as_markdown=False)` remains available when
  the agent needs the raw HTML.
