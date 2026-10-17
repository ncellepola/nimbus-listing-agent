# ADR-0004: Cache-first fetch makes the demo (and the test suite) reproducible

## Status

Accepted

## Context

Task 7's brief names a specific anti-goal: "a demo that dies in the room." A
15-20 minute interview presentation cannot depend on a live estate-agent
website answering the same way twice, on a network connection holding up, on
robots.txt not having changed since the recipe was captured, or on an LLM
agent call succeeding and costing money at exactly the moment someone is
watching.

At the same time, the whole point of the demo is to show *real* mechanics —
a real captured `SiteRecipe`, the real `crawl_site`/`extract_listing`/
`apply_delta`/`heal_site` code paths, a real simulated site redesign and a
real recipe-version bump — not a scripted animation that merely looks like
the system. Faking the narrative in a separate demo-only code path would
prove nothing about the system actually being presented.

`nimbus.fetch.cache.HtmlCache` (Task 1) already existed for a different
reason — avoiding redundant fetches during development and in the test
suite — but it turns out to be exactly the mechanism this problem needs:
`Fetcher.get()` (`src/nimbus/fetch/fetcher.py`) always checks the cache
before the network, keyed by a normalised URL. A cache entry put there by
anything — a prior live fetch, or a test/demo seeding it directly from a
fixture file — is indistinguishable to every piece of real pipeline code
downstream of `Fetcher.get()`.

One later correction, because the two uses pull in opposite directions: a
cache with no expiry is what the demo needs and is a defect in
production, where it would make every nightly crawl re-read yesterday's
bytes and the delta could never fire. `Settings.cache_max_age` bounds
entry age (12 hours by default) and the offline demo sets it to `None`.
The demo's reproducibility is therefore an explicit configuration choice
rather than a property of the cache itself.

## Decision

`nimbus demo --offline` (Task 7, `src/nimbus/demo.py`) pre-seeds a throwaway
`HtmlCache` with real, captured HTML fixtures (`tests/fixtures/pages/`) and a
synthetic robots.txt/sitemap built to match them, then runs the real
`crawl_site`, `extract_listing`, `apply_delta`, and `heal_site` against that
cache through a `Fetcher` whose backend raises `AssertionError` if ever
actually called — not a lenient stub that would silently succeed and hide a
bug, but a hard failure if the cache-first guarantee is ever violated.

Two further substitutions make the "no agent call at all" half of the claim
true as well:

- The *initial* recipe is loaded directly from a committed fixture
  (`tests/fixtures/recipes/ashworthholme.co.uk.json`) — a recipe the
  discovery agent genuinely produced against the live site at an earlier
  point, not synthesised for this demo — rather than calling `discover_site`.
- The *healed* recipe, after the simulated redesign, is loaded from a second
  committed fixture (`tests/fixtures/recipes/ashworthholme.co.uk.healed.json`)
  by temporarily substituting it for `nimbus.heal.discover_site` at its own
  call site, so `heal_site`'s real load-previous/save-append-only/diff logic
  still runs — only the one network-and-agent call inside it is swapped out.
- Extraction runs against a canned stub client with the same shape as
  `anthropic.AsyncAnthropic`, matching the pattern `tests/test_heal.py`
  already uses, rather than the real Anthropic API.

`nimbus demo` without `--offline`, and `onboard`/`crawl`/`heal`, are the real
thing: they build a real `Fetcher` with `HttpxBackend` and do touch the
network and (for `onboard`/`heal`) the live agent. `--offline` is additive,
not a replacement for those code paths — it is a second way of driving the
same functions.

## Consequences

- The demo is deterministic: `scripts/demo.sh` runs it twice and diffs the
  output to prove this live, and `tests/test_cli.py::test_demo_offline_is_byte_identical_across_runs`
  proves it in CI.
- `--offline` genuinely exercises `crawl_site`, `extract_listing`,
  `apply_delta`, and `heal_site` — the same functions the nightly production
  path runs — not a parallel "demo mode" implementation of any of them.
  Only the three I/O boundaries (HTML fetch, the discovery agent call, the
  extraction model call) are substituted.
- `tests/test_cli.py::test_demo_offline_makes_no_agent_call` monkeypatches
  `discover_site` to raise and still expects success — a regression that
  made the offline path call the real agent again would be caught
  immediately, not discovered live.
- This is also, quietly, why the rest of the test suite is offline and fast:
  every test file in `tests/` already seeds `HtmlCache` directly instead of
  touching the network, for the same reason this ADR gives the demo its own
  name. Task 7 did not invent cache-first fetch; it is the first place that
  depends on it for something a human is watching, which is why it earns an
  ADR of its own rather than staying an implementation detail of Task 1.
- The trade-off is the same one `tests/fixtures/pages/mutated/` already
  accepts: the "redesign" and the "repair" are both fixed in advance, not
  generated live. That is a deliberate choice for an interview demo, not a
  claim that the real discovery agent would necessarily re-learn any given
  real redesign unaided — see `nimbus.heal.heal_site`'s own docstring, which
  states this distinction for the same reason.
- That distinction is stated in this ADR, `heal_site`'s docstring, the
  README, and `demo.py`'s module docstring — none of which a live audience
  sees during a presentation. `run_offline_demo`'s own printed narration
  for step 5 therefore says so directly ("offline mode: re-discovery is
  stubbed from a committed fixture -- run `nimbus heal` for the real
  agent"), not only in the surrounding prose: a caveat that exists only in
  source and documentation is not a caveat the room gets.
