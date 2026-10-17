"""Tests for the discovery agent's pure, LLM-free guard rails.

`parse_recipe_payload` and `validate_recipe` are pure functions precisely so
a plausible-looking but wrong `SiteRecipe` can be caught in a fast, offline
test — before it is ever persisted and poisons a nightly crawl. No test here
touches the network or calls an LLM, except `test_discover_site_against_*`,
which is marked `live` and excluded by default (`pytest -m "not live"`).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import make_recipe

import nimbus.discovery as discovery
from nimbus.config import Settings
from nimbus.discovery import (
    DiscoveryError,
    RecipeParseError,
    discover_site,
    parse_recipe_payload,
    validate_recipe,
)

FIXTURES = Path(__file__).parent / "fixtures" / "recipes"
VALID = (FIXTURES / "valid.json").read_text()
MALFORMED = (FIXTURES / "malformed.json").read_text()

SAMPLE_URLS = [
    "https://www.ashworthholme.co.uk/property-details/34612724/-/sale/alcester-road",
    "https://www.ashworthholme.co.uk/property-details/34612800/-/sale/mill-lane",
]


def test_parse_recipe_payload_accepts_json_in_prose():
    text = f"Here is the recipe:\n```json\n{VALID}\n```\nLet me know if you need anything else."

    r = parse_recipe_payload(text, "ashworthholme.co.uk")

    assert r.domain == "ashworthholme.co.uk"
    assert r.listing_url_patterns


def test_parse_recipe_payload_rejects_malformed_json():
    with pytest.raises(RecipeParseError):
        parse_recipe_payload(MALFORMED, "ashworthholme.co.uk")


def test_parse_recipe_payload_rejects_schema_violation():
    payload = json.loads(VALID)
    payload["confidence"] = 1.7  # out of the [0.0, 1.0] range -> schema violation
    bad_confidence = json.dumps(payload)

    with pytest.raises(RecipeParseError):
        parse_recipe_payload(bad_confidence, "ashworthholme.co.uk")


def test_validate_recipe_flags_pattern_matching_no_sample_urls():
    recipe_with_bogus_pattern = make_recipe(
        listing_url_patterns=["/this-pattern-matches-nothing/*"],
    )

    problems = validate_recipe(recipe_with_bogus_pattern, SAMPLE_URLS)

    assert any("matches none" in p for p in problems)


def test_validate_recipe_passes_a_sound_recipe():
    recipe = make_recipe(listing_url_patterns=["/property-details/*"])

    problems = validate_recipe(recipe, SAMPLE_URLS)

    assert problems == []


def test_validate_recipe_requires_a_source_of_urls():
    # Review Focus 1: no sitemap AND no listing index is not a usable recipe.
    r = make_recipe(sitemap_urls=[], listing_index_urls=[])

    problems = validate_recipe(r, [])

    assert any("no sitemap" in p.lower() for p in problems)


def test_validate_recipe_flags_no_listing_url_patterns():
    r = make_recipe(listing_url_patterns=[])

    problems = validate_recipe(r, [])

    assert any("listing_url_patterns" in p for p in problems)


async def test_discover_site_requires_api_key():
    settings = Settings(anthropic_api_key="", cache_dir=Path("x"), db_path=Path("y"))

    with pytest.raises(DiscoveryError, match="ANTHROPIC_API_KEY"):
        await discover_site("ashworthholme.co.uk", settings=settings)


def test_mcp_server_options_anchors_cwd_to_repo_root(monkeypatch, tmp_path):
    # cwd=None (the default) just inherits the caller's cwd, which is a
    # no-op, not a fix, for a caller invoked from outside the repo root:
    # the MCP stdio subprocess's own Settings() would then fail to find
    # .env. Changing into a throwaway directory before building the
    # options proves the resolved cwd is anchored to the repo root (derived
    # from this module's own file location), not inherited from here.
    monkeypatch.chdir(tmp_path)

    options = discovery._mcp_server_options(
        Settings(anthropic_api_key="sk-ant-test-not-real"), 20, "sk-ant-test-not-real"
    )

    assert options.cwd == str(discovery._REPO_ROOT)
    assert options.cwd != str(tmp_path)
    assert (Path(options.cwd) / "pyproject.toml").is_file()


def _settings_with_key(tmp_path) -> Settings:
    return Settings(
        anthropic_api_key="sk-ant-test-not-real",
        cache_dir=tmp_path / "cache",
        db_path=tmp_path / "nimbus.db",
    )


def _unsound_recipe_json() -> str:
    """Parses fine and is schema-valid, but validate_recipe rejects it: no
    sitemap and no listing index URLs at all (Review Focus 1)."""
    payload = json.loads(VALID)
    payload["sitemap_urls"] = []
    payload["listing_index_urls"] = []
    return json.dumps(payload)


def _fake_run_agent(outputs: list[str]):
    """A stand-in for `_run_agent` that returns the next canned output on
    each call, so `discover_site`'s retry logic can be exercised without
    ever starting a real agent query or touching the network."""

    async def fake(prompt: str, options) -> str:
        return outputs.pop(0)

    return fake


async def test_discover_site_retries_once_after_parse_failure_then_succeeds(monkeypatch, tmp_path):
    settings = _settings_with_key(tmp_path)
    monkeypatch.setattr(discovery, "_run_agent", _fake_run_agent([MALFORMED, VALID]))

    r = await discover_site("ashworthholme.co.uk", settings=settings)

    assert r.domain == "ashworthholme.co.uk"
    assert r.listing_url_patterns


async def test_discover_site_raises_after_two_validation_failures(monkeypatch, tmp_path):
    settings = _settings_with_key(tmp_path)
    unsound = _unsound_recipe_json()
    monkeypatch.setattr(discovery, "_run_agent", _fake_run_agent([unsound, unsound]))

    with pytest.raises(DiscoveryError, match="validation twice"):
        await discover_site("ashworthholme.co.uk", settings=settings)


async def test_discover_site_raises_after_two_parse_failures(monkeypatch, tmp_path):
    settings = _settings_with_key(tmp_path)
    monkeypatch.setattr(discovery, "_run_agent", _fake_run_agent([MALFORMED, MALFORMED]))

    with pytest.raises(DiscoveryError, match="failed twice"):
        await discover_site("ashworthholme.co.uk", settings=settings)


@pytest.mark.live
async def test_discover_site_against_ashworthholme():
    settings = Settings()
    r = await discover_site("ashworthholme.co.uk", settings=settings)

    assert r.confidence > 0.5 and r.listing_url_patterns
