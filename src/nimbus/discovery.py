"""The discovery agent: learns one previously-unseen site and emits a
versioned, schema-validated `SiteRecipe`.

This module is the centrepiece of the "agent learns a site once, code
executes the recipe nightly" architecture (ADR-0002). An LLM agent is
expensive and non-deterministic, so it only ever runs once per site
onboarding; everything downstream (Task 4's crawler) consumes the
`SiteRecipe` this module produces with zero further LLM involvement.

Two functions are deliberately pure, with no I/O and no agent dependency,
so they can be exercised in fast offline tests:

- `parse_recipe_payload` turns raw agent output text into a validated
  `SiteRecipe`, raising `RecipeParseError` on anything malformed or
  schema-invalid. A plausible-looking but wrong recipe must never reach
  `save_recipe` (Task 1) and poison every subsequent nightly crawl.
- `validate_recipe` runs extra, structural sanity checks a pydantic schema
  cannot express on its own — chiefly that the recipe actually has a source
  of listing URLs (a sitemap or a listing index) and that its
  `listing_url_patterns` match at least one real sample URL.

`discover_site` is the only part of this module that is not pure: it drives
`claude_agent_sdk.query` against the Task 2 MCP server (`get_robots`,
`get_sitemap`, `fetch_page`, and nothing else), parses and validates the
agent's final answer, retries once with the validation problems fed back on
failure, and raises on a second failure.
"""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import ValidationError

from nimbus.config import Settings
from nimbus.models import SiteRecipe

DISCOVERY_PROMPT_VERSION = "v1"

_PROMPT_PATH = Path(__file__).parent / "prompts" / "discovery.md"
_MCP_SERVER_NAME = "nimbus-fetch"
_MCP_TOOL_NAMES = ("get_robots", "get_sitemap", "fetch_page")

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)

_MISSING_API_KEY_MESSAGE = (
    "ANTHROPIC_API_KEY is not set. Discovery runs a live Claude Agent SDK query and "
    "cannot proceed without a key. Copy .env.example to .env and set a real "
    "ANTHROPIC_API_KEY before running discovery (never commit .env)."
)


class RecipeParseError(Exception):
    """Raised when agent output cannot be parsed into a valid `SiteRecipe`."""


class DiscoveryError(Exception):
    """Raised when `discover_site` cannot produce a sound recipe at all —
    a missing API key, an SDK/transport failure, or two consecutive
    failed attempts to produce a schema-valid, structurally sound recipe."""


def _extract_json_object(text: str) -> str:
    """The final fenced ```json block in `text`, or else the span from the
    first `{` to the last `}`. Agents routinely wrap their final answer in
    prose or a fenced code block; this tolerates both."""
    fences = _JSON_FENCE_RE.findall(text)
    if fences:
        return fences[-1]

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise RecipeParseError("no JSON object found in agent output")
    return text[start : end + 1]


def parse_recipe_payload(text: str, domain: str) -> SiteRecipe:
    """Extract the final JSON object from `text` and validate it as a
    `SiteRecipe` for `domain`. Raises `RecipeParseError` on malformed JSON
    or a schema violation — never returns a recipe that failed validation."""
    raw = _extract_json_object(text)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RecipeParseError(f"agent output is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise RecipeParseError("agent output JSON must be an object")

    # `domain` is the ground truth for this discovery run, not something the
    # agent should be trusted to invent; `version`/`discovered_at` are
    # storage's job (save_recipe recomputes the real version), so fill in
    # placeholders only when the agent omitted them.
    payload["domain"] = domain
    payload.setdefault("version", 1)
    payload.setdefault("discovered_at", datetime.now(UTC).isoformat())

    try:
        return SiteRecipe(**payload)
    except ValidationError as exc:
        raise RecipeParseError(f"agent output failed SiteRecipe schema validation: {exc}") from exc


def _matches_pattern(url: str, pattern: str) -> bool:
    path = urlsplit(url).path
    return fnmatch.fnmatch(url, pattern) or fnmatch.fnmatch(path, pattern)


def validate_recipe(recipe: SiteRecipe, sample_urls: list[str]) -> list[str]:
    """Structural checks a pydantic schema alone cannot express. Returns
    human-readable problems; an empty list means the recipe is sound."""
    problems: list[str] = []

    if not recipe.sitemap_urls and not recipe.listing_index_urls:
        problems.append(
            "recipe has no sitemap and no listing index URLs; there is no source of "
            "URLs for a crawler to discover listing pages from"
        )

    if not recipe.listing_url_patterns:
        problems.append(
            "recipe has no listing_url_patterns; the crawler cannot recognise which "
            "fetched URLs are listing pages"
        )

    if sample_urls:
        for pattern in recipe.listing_url_patterns:
            if not any(_matches_pattern(url, pattern) for url in sample_urls):
                problems.append(f"listing_url_pattern {pattern!r} matches none of the sample URLs")

    return problems


def _render_discovery_prompt(domain: str) -> str:
    template = _PROMPT_PATH.read_text()
    return template.replace("__DOMAIN__", domain)


def _render_retry_prompt(domain: str, problems: list[str]) -> str:
    base = _render_discovery_prompt(domain)
    feedback = "\n".join(f"- {p}" for p in problems)
    return (
        f"{base}\n\n"
        "---\n\n"
        "Your previous answer for this domain failed validation with these problems:\n"
        f"{feedback}\n\n"
        "Fix the recipe so every problem above is addressed, re-checking the site with "
        "your tools if needed, and reply with exactly one corrected JSON object."
    )


def _mcp_server_options(settings: Settings, max_turns: int):
    # Imported lazily so that importing nimbus.discovery (and running the
    # pure parse/validate tests) never requires claude_agent_sdk's CLI
    # subprocess machinery to be importable in a context that doesn't need it.
    from claude_agent_sdk import ClaudeAgentOptions

    return ClaudeAgentOptions(
        mcp_servers={
            _MCP_SERVER_NAME: {
                "type": "stdio",
                "command": sys.executable,
                "args": ["-m", "nimbus.mcp_server"],
            }
        },
        allowed_tools=[f"mcp__{_MCP_SERVER_NAME}__{name}" for name in _MCP_TOOL_NAMES],
        max_turns=max_turns,
    )


async def _run_agent(prompt: str, options) -> str:
    """Run one query turn and return the final assistant text: the result
    message's summary if the CLI provided one, else the text blocks of the
    last assistant message."""
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, query

    last_assistant_text = ""
    result: ResultMessage | None = None

    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            text = "".join(block.text for block in message.content if isinstance(block, TextBlock))
            if text:
                last_assistant_text = text
        elif isinstance(message, ResultMessage):
            result = message

    if result is not None and result.is_error:
        raise DiscoveryError(
            f"Claude Agent SDK query failed (subtype={result.subtype!r}): "
            f"{result.result or result.errors or 'no further detail'}"
        )

    if result is not None and result.result:
        return result.result
    return last_assistant_text


def _sample_urls_for(recipe: SiteRecipe) -> list[str]:
    return (recipe.sitemap_urls + recipe.listing_index_urls)[:50]


async def discover_site(domain: str, *, settings: Settings, max_turns: int = 20) -> SiteRecipe:
    """Explore `domain` through the Task 2 MCP tools and return a
    schema-validated, structurally sound `SiteRecipe`. Retries once with
    validation problems fed back to the agent; raises `DiscoveryError` if a
    sound recipe still cannot be produced, or if `settings` has no
    Anthropic API key configured."""
    api_key = settings.anthropic_api_key.get_secret_value()
    if not api_key:
        raise DiscoveryError(_MISSING_API_KEY_MESSAGE)

    options = _mcp_server_options(settings, max_turns)

    prompt = _render_discovery_prompt(domain)
    text = await _run_agent(prompt, options)
    try:
        recipe = parse_recipe_payload(text, domain)
        problems = validate_recipe(recipe, _sample_urls_for(recipe))
    except RecipeParseError as exc:
        recipe, problems = None, [str(exc)]

    if recipe is not None and not problems:
        return recipe

    retry_prompt = _render_retry_prompt(domain, problems)
    text = await _run_agent(retry_prompt, options)
    try:
        recipe = parse_recipe_payload(text, domain)
    except RecipeParseError as exc:
        raise DiscoveryError(
            f"discovery for {domain} failed twice; second attempt's output was unparsable: {exc}"
        ) from exc

    problems = validate_recipe(recipe, _sample_urls_for(recipe))
    if problems:
        raise DiscoveryError(
            f"discovery for {domain} failed validation twice; remaining problems: "
            f"{'; '.join(problems)}"
        )

    return recipe


def _main() -> None:
    """Standalone entrypoint: `python -m nimbus.discovery <domain> --out <path>`.
    Task 3 must not depend on the CLI, which does not exist until Task 7."""
    parser = argparse.ArgumentParser(
        description="Discover a SiteRecipe for a domain and write it as JSON."
    )
    parser.add_argument("domain")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--max-turns", type=int, default=20)
    args = parser.parse_args()

    settings = Settings()
    recipe = asyncio.run(discover_site(args.domain, settings=settings, max_turns=args.max_turns))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(recipe.model_dump_json(indent=2) + "\n")
    print(f"wrote {args.out} (confidence={recipe.confidence})")


if __name__ == "__main__":
    _main()
