"""robots.txt parsing, including modern AI-crawler directives.

Covers the classic `User-agent` / `Disallow` / `Allow` / `Crawl-delay` /
`Sitemap` grammar plus two things most parsers miss:

- `Content-Signal: key=value, key=value` lines (see contentsignals.org),
  recorded per matched user-agent block, same as Allow/Disallow.
- Every `User-agent` line anywhere in the file that names a known AI
  crawler (ClaudeBot, GPTBot, ...), regardless of which block applies to
  *us* — a site can address an AI crawler by name without that crawler
  being the one asking.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from nimbus.models import AiSignals

DEFAULT_CRAWL_DELAY = 1.0

# Crawlers known to identify themselves for AI training / answer-engine use.
# Not exhaustive; covers the agents this project's robots fixtures name.
KNOWN_AI_AGENTS = {
    "gptbot",
    "oai-searchbot",
    "chatgpt-user",
    "claudebot",
    "claude-searchbot",
    "claude-user",
    "claude-web",
    "anthropic-ai",
    "perplexitybot",
    "perplexity-user",
    "google-extended",
    "google-cloudvertexbot",
    "applebot-extended",
    "amazonbot",
    "ccbot",
    "meta-externalagent",
    "meta-externalfetcher",
    "bytespider",
    "diffbot",
    "youbot",
    "cohere-ai",
}


@dataclass
class _Block:
    names: list[str] = field(default_factory=list)
    rules: list[tuple[bool, str]] = field(default_factory=list)  # (is_allow, pattern)
    crawl_delay: float | None = None
    content_signal: dict[str, str] = field(default_factory=dict)


def _pattern_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a robots.txt path pattern (`*` wildcard, optional `$` anchor)
    into a compiled, anchored-at-start regex."""
    out = ["^"]
    for i, ch in enumerate(pattern):
        if ch == "*":
            out.append(".*")
        elif ch == "$" and i == len(pattern) - 1:
            out.append("$")
        else:
            out.append(re.escape(ch))
    return re.compile("".join(out))


def _parse_blocks(text: str) -> tuple[list[_Block], list[str]]:
    """Parse the raw text into user-agent blocks plus the file-global
    sitemap list (Sitemap: applies to the whole file, not a block)."""
    blocks: list[_Block] = []
    sitemaps: list[str] = []
    current: _Block | None = None
    in_agent_run = False  # true while consecutive User-agent lines are seen

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            in_agent_run = False
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()

        if key == "user-agent":
            if current is None or not in_agent_run:
                current = _Block()
                blocks.append(current)
            current.names.append(value)
            in_agent_run = True
            continue

        in_agent_run = False

        if key == "sitemap":
            if value:
                sitemaps.append(value)
            continue

        if current is None:
            # Directive before any User-agent line: ignore, malformed input.
            continue

        if key == "disallow":
            if value:
                current.rules.append((False, value))
        elif key == "allow":
            if value:
                current.rules.append((True, value))
        elif key == "crawl-delay":
            try:
                current.crawl_delay = float(value)
            except ValueError:
                pass
        elif key == "content-signal":
            for pair in value.split(","):
                pair = pair.strip()
                if "=" in pair:
                    sig_key, _, sig_val = pair.partition("=")
                    current.content_signal[sig_key.strip().lower()] = sig_val.strip().lower()

    return blocks, sitemaps


def _collect_named_ai_agents(blocks: list[_Block]) -> set[str]:
    named: set[str] = set()
    for block in blocks:
        for name in block.names:
            if name.strip().lower() in KNOWN_AI_AGENTS:
                named.add(name.strip())
    return named


def _matching_blocks(blocks: list[_Block], agent: str) -> list[_Block]:
    agent_lower = agent.strip().lower()
    named_matches = [b for b in blocks if any(n.strip().lower() == agent_lower for n in b.names)]
    if named_matches:
        return named_matches
    return [b for b in blocks if any(n.strip() == "*" for n in b.names)]


class RobotsPolicy:
    """A parsed robots.txt, resolved for a single named agent."""

    def __init__(
        self,
        *,
        agent: str,
        rules: list[tuple[bool, str]],
        crawl_delay: float,
        sitemaps: list[str],
        ai_signals: AiSignals,
    ) -> None:
        self._agent = agent
        self._rules = rules
        self.crawl_delay = crawl_delay
        self.sitemaps = sitemaps
        self.ai_signals = ai_signals

    @classmethod
    def from_text(cls, text: str, *, agent: str) -> RobotsPolicy:
        blocks, sitemaps = _parse_blocks(text)
        matched = _matching_blocks(blocks, agent)

        rules: list[tuple[bool, str]] = []
        crawl_delay = DEFAULT_CRAWL_DELAY
        content_signal: dict[str, str] = {}
        for block in matched:
            rules.extend(block.rules)
            if block.crawl_delay is not None:
                crawl_delay = block.crawl_delay
            content_signal.update(block.content_signal)

        named_ai_agents = _collect_named_ai_agents(blocks)
        ai_training_allowed = content_signal.get("ai-train", "yes") != "no"

        return cls(
            agent=agent,
            rules=rules,
            crawl_delay=crawl_delay,
            sitemaps=sitemaps,
            ai_signals=AiSignals(
                content_signal=content_signal,
                named_ai_agents=named_ai_agents,
                ai_training_allowed=ai_training_allowed,
            ),
        )

    @classmethod
    def allow_all(cls, agent: str) -> RobotsPolicy:
        return cls(
            agent=agent,
            rules=[],
            crawl_delay=DEFAULT_CRAWL_DELAY,
            sitemaps=[],
            ai_signals=AiSignals(),
        )

    def can_fetch(self, url: str) -> bool:
        parsed = urlsplit(url)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        best_match: tuple[int, bool] | None = None  # (pattern length, is_allow)
        for is_allow, pattern in self._rules:
            if _pattern_to_regex(pattern).match(path):
                if best_match is None or len(pattern) > best_match[0]:
                    best_match = (len(pattern), is_allow)

        if best_match is None:
            return True
        return best_match[1]
