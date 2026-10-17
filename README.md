# Nimbus Listing Agent

Proof of concept for collecting residential property listings from estate-agent
websites: an LLM agent learns an unseen site once and emits a reusable
`SiteRecipe`; deterministic code executes that recipe nightly with no LLM on
the hot path; a health monitor sends the agent back in when the site changes.

See `docs/superpowers/specs/2026-10-17-nimbus-listing-agent-design.md` for the
full design spec and `docs/superpowers/plans/2026-10-17-nimbus-listing-agent.md`
for the implementation plan. `docs/adr/` records the architecture decisions as
they are made.

This README is a stub — full quickstart, architecture diagram and demo
instructions land in the final task.

## Development

```bash
uv sync --extra dev
uv run pytest -m "not live"
uv run ruff check .
uv run ruff format --check .
```

Copy `.env.example` to `.env` and fill in secrets before running anything that
calls the Anthropic API. Tests never touch the network or require a key.
