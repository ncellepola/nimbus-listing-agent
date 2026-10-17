from __future__ import annotations

from pathlib import Path

from nimbus.fetch.robots import RobotsPolicy

FIXTURES = Path(__file__).parent / "fixtures" / "robots"
HENSTOCK = (FIXTURES / "henstock.txt").read_text()
NORMIE = (FIXTURES / "normie.txt").read_text()


def test_robots_parses_crawl_delay_and_sitemaps():
    p = RobotsPolicy.from_text(HENSTOCK, agent="NimbusBot")
    assert p.crawl_delay == 2.0
    assert p.can_fetch("https://www.henstockps.co.uk/property/123") is True
    assert p.can_fetch("https://www.henstockps.co.uk/property/bedrooms/3") is False
    assert p.sitemaps == ["https://www.henstockps.co.uk/sitemap.xml"]


def test_robots_detects_ai_directives():
    p = RobotsPolicy.from_text(NORMIE, agent="NimbusBot")
    assert p.ai_signals.ai_training_allowed is False
    assert p.ai_signals.content_signal["ai-train"] == "no"
    assert "ClaudeBot" in p.ai_signals.named_ai_agents


def test_robots_missing_file_defaults_to_allow_with_default_delay():
    p = RobotsPolicy.allow_all(agent="NimbusBot")
    assert p.can_fetch("https://example.co.uk/anything") is True
    assert p.crawl_delay == 1.0


def test_robots_named_agent_block_overrides_wildcard():
    text = """\
User-agent: *
Disallow: /private/

User-agent: NimbusBot
Disallow: /secret/
Crawl-delay: 5
"""
    p = RobotsPolicy.from_text(text, agent="NimbusBot")
    # Named block applies instead of wildcard: /private/ is allowed for us,
    # /secret/ is not.
    assert p.can_fetch("https://example.co.uk/private/page") is True
    assert p.can_fetch("https://example.co.uk/secret/page") is False
    assert p.crawl_delay == 5.0


def test_robots_from_text_uses_injected_default_when_block_omits_crawl_delay():
    text = """\
User-agent: *
Disallow: /private/
"""
    p = RobotsPolicy.from_text(text, agent="NimbusBot", default_crawl_delay=7.25)
    assert p.crawl_delay == 7.25
