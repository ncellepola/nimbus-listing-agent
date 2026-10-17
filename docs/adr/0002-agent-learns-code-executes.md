# ADR-0002: The agent learns a site once; deterministic code executes the recipe nightly

## Status

Accepted

## Context

Nimbus needs residential listings collected daily from many estate-agent
websites. Every site is built by a different web shop, with different markup,
different sitemap conventions (or none), and different pagination. Sites also
change their markup over time without warning.

Two shapes were available:

1. Call an LLM against every page of every site on every run.
2. Use an LLM once per site to understand its structure, capture that
   understanding as data, and run conventional code against that data
   thereafter.

Running an LLM on every page every night scales badly on cost (linear in
pages × sites × nights) and on determinism (model output varies run to run,
which makes the crawl's own behaviour — which URLs it visits, in what order —
non-reproducible and hard to test or audit).

## Decision

An LLM discovery agent explores a previously-unseen site once: it reads
robots.txt, finds the sitemap or a listing index, samples a few pages, and
emits a versioned, schema-validated `SiteRecipe` (sitemap/index URLs, listing
URL patterns, pagination rule, field hints, a confidence score). That recipe
is persisted. From then on, a deterministic crawler reads the recipe and
fetches known URLs on a schedule — no LLM call, no MCP, no agent loop. A
separate per-page LLM extractor turns fetched HTML into structured `Listing`
rows (one call per page, not an agent loop, since it is a single
transformation), with its own audit trail. A health monitor watches the
extractor's schema-valid rate; when it drops, the discovery agent re-runs and
the recipe version bumps.

LLM cost is thus proportional to *site onboarding events* and *extraction
calls*, not to *every page on every crawl*, and the nightly crawl's URL
selection is fully deterministic and testable without a live model.

## Consequences

- The nightly hot path (`crawler.py`) has zero LLM/agent dependency, so it can
  run in CI, offline, byte-for-byte reproducibly, against cached fixtures.
- A `SiteRecipe` is a versioned, auditable artifact — reviewers can see
  exactly what the agent decided, and roll back to a prior version.
- A site redesign degrades the recipe rather than the whole system: the
  health monitor detects the degradation and the agent repairs just that
  site's recipe, not the extractor or the crawler.
- The approach trades a small amount of extraction accuracy (a recipe can go
  stale between re-discoveries) for a large, predictable reduction in LLM
  calls and a crawler that behaves the same way every time it runs.
