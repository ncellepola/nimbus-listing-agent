# ADR-0003: MCP carries the exploratory agent; the nightly crawler calls the fetch layer directly

## Status

Accepted

## Context

Two different pieces of code need to reach the same `Fetcher` (Task 1): the
discovery agent (Task 3), which is an LLM deciding for itself which URLs to
look at next, and the nightly crawler (Task 4), which walks a fixed list of
URLs taken from a previously-saved `SiteRecipe`.

The Model Context Protocol exists to expose a bounded set of tools to an LLM
client so the model can choose when and how to call them, with the tool
boundary as the place policy (robots compliance, response size) is enforced
(ADR-0005). That is exactly the discovery agent's situation: it does not know
in advance what the site looks like, so it needs to decide, turn by turn,
whether to call `get_robots`, `get_sitemap`, or `fetch_page`, and with what
arguments.

The nightly crawler is not in that situation. By the time it runs, a
`SiteRecipe` already specifies the sitemap/index URLs, the listing URL
pattern, and the pagination rule. There is no judgement left to exercise —
"fetch these URLs, in this order, using this pagination rule" is a plain
loop over `Fetcher.get()`. Routing that loop through an MCP server and an
agent turn would add a stdio round-trip and (if a model were in the loop at
all) non-determinism and LLM cost to every single page of every crawl, for a
decision that is already fully determined by the recipe.

## Decision

`nimbus.mcp_server` (Task 2) is scoped entirely to the discovery agent: it is
the only thing in this codebase that runs as an MCP server, and it is only
ever attached to a `claude_agent_sdk` query. The nightly crawler (Task 4)
imports `nimbus.fetch.fetcher.Fetcher` and calls `.get()` directly, in a plain
Python loop driven by the fields on a loaded `SiteRecipe` — no MCP server, no
agent loop, no LLM call, in that path at all.

## Consequences

- The hot path (nightly crawl, hundreds of sites) has zero MCP/agent
  overhead: no subprocess, no protocol framing, no model round-trip per
  page. Its only dependency is the fetch layer, which is already covered by
  offline, deterministic tests (ADR-0005).
- The exploratory path (site onboarding, once per site) pays the cost of an
  agent loop and an MCP round-trip per tool call, which is acceptable
  because it happens once per site, not once per page per night.
- `Fetcher` has exactly one production entry point used two different ways
  — through MCP tool wrappers for the agent, and directly for the crawler —
  rather than two divergent implementations of "fetch a URL and respect
  robots.txt".
- If a future task ever needs an LLM decision inside the nightly crawl (it
  should not, per ADR-0002), that would be a new ADR, not a quiet reuse of
  `nimbus.mcp_server` for a purpose it was not scoped for.
