"""Environment-driven configuration.

Secrets come only from `.env` (see `.env.example`); `.env` is gitignored and
must never be committed or printed. The Anthropic API key is allowed to be
empty — nothing in the foundation layer calls an LLM, and later tasks that do
call one are expected to fail loudly and specifically when the key is blank,
not here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_USER_AGENT = "NimbusBot/0.1 (+https://github.com/ncellepola/nimbus-listing-agent)"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: SecretStr = SecretStr("")
    cache_dir: Path = Path(".cache")
    db_path: Path = Path("nimbus.db")
    user_agent: str = DEFAULT_USER_AGENT
    request_timeout: float = 20.0
    default_crawl_delay: float = 1.0
    # How old a cached page may be before `Fetcher` treats it as a miss and
    # re-fetches, in seconds. 12 hours: short enough that a nightly crawl
    # always sees today's bytes (without a bound, the nightly delta would
    # compare yesterday's cache entry against itself and could never fire),
    # long enough that a re-run an hour later costs the site nothing.
    # `None` disables expiry entirely, which is what `nimbus demo
    # --offline` sets so its seeded fixtures stay byte-identical forever
    # (ADR-0004).
    cache_max_age: float | None = 43_200.0
    fetch_backend: Literal["httpx", "crawlbase"] = "httpx"
    crawlbase_token: SecretStr | None = None
