# Nimbus Listing Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a PoC that onboards an unseen estate-agent website from a bare domain, collects its residential listings daily with deterministic code, and repairs itself when the site changes.

**Architecture:** A Claude Agent SDK agent explores a site through a local stdio MCP server and emits a versioned, schema-validated `SiteRecipe`. Deterministic code executes that recipe nightly with no LLM on the hot path; a per-page LLM extractor turns HTML into validated `Listing` rows with an audit trail. A health monitor watches the schema-valid rate and sends the agent back in to repair the recipe when a site changes.

**Tech Stack:** Python 3.12, uv, pydantic v2, pydantic-settings, claude-agent-sdk, anthropic, mcp (FastMCP), httpx, typer, SQLite (stdlib `sqlite3`), pytest, pytest-asyncio, ruff, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-10-17-nimbus-listing-agent-design.md`

## Global Constraints

- Python `>=3.12`; project managed with `uv`; dependencies declared in `pyproject.toml`.
- Package root `src/nimbus/`; tests in `tests/`; ruff line length 100.
- Repo `github.com/ncellepola/nimbus-listing-agent`, public. One issue → one branch → one PR per task, squash merge, conventional commit messages (`feat:`, `test:`, `docs:`, `chore:`).
- CI (`.github/workflows/ci.yml`) runs `ruff check`, `ruff format --check`, `pytest` on every PR and must be green before merge.
- **No test may touch the network.** Tests use fixtures under `tests/fixtures/`. Live-site tests are marked `@pytest.mark.live` and deselected in CI via `-m "not live"`.
- Secrets come only from `.env` via `pydantic-settings`. `.env` is gitignored; `.env.example` is committed.
- Crawl identity is the single user agent string `NimbusBot/0.1 (+https://github.com/ncellepola/nimbus-listing-agent)`.
- Target sites: `ashworthholme.co.uk`, `jaksandco.co.uk`, `bentleyhurst.co.uk`. Reserve: `henstockps.co.uk`. `normie.co.uk` is a fixture only, never crawled.
- All money is integer GBP pounds (no pence, no floats). All timestamps are timezone-aware UTC.

## Review Focus

Input classes the spec implies but does not call out, each pinned to a test in the owning task:

1. **A site with no `sitemap.xml`.** Discovery must fall back to a listing index page rather than returning an empty recipe. — Task 3.
2. **A missing or 404 `robots.txt`.** Must default to allow-all while still applying the default crawl delay, not crash and not block the crawl. — Task 1.
3. **Prices that are not plain numbers.** `POA`, `Offers in excess of £450,000`, `Guide Price £1.25m`, `£450,000 - £475,000`, `£1,250 pcm`. Must parse or abstain, never silently produce a wrong integer. — Task 5.
4. **Pages that are not residential sales listings.** Rentals and `Sold STC` pages match listing URL patterns and must be excluded from the sales dataset. — Task 5.
5. **An LLM response that is malformed or hallucinates fields.** Schema validation must reject it, mark it for review, and write no listing row. — Tasks 3 and 5.

---

## File Structure

| Path | Responsibility |
|---|---|
| `src/nimbus/config.py` | `Settings` — env-driven configuration |
| `src/nimbus/models.py` | All pydantic models shared across components |
| `src/nimbus/storage.py` | SQLite schema, recipe CRUD, audit insert |
| `src/nimbus/fetch/robots.py` | robots.txt parsing incl. AI directives |
| `src/nimbus/fetch/cache.py` | content-addressed disk cache |
| `src/nimbus/fetch/backends.py` | `httpx` and Crawlbase fetch backends |
| `src/nimbus/fetch/fetcher.py` | robots-gated, cache-first fetch facade |
| `src/nimbus/mcp_server.py` | local stdio MCP server, 3 tools |
| `src/nimbus/discovery.py` | agent that emits `SiteRecipe` |
| `src/nimbus/crawler.py` | deterministic recipe execution |
| `src/nimbus/extractor.py` | HTML → `Listing` via LLM, plus pure parsers |
| `src/nimbus/heal.py` | site health and recipe repair |
| `src/nimbus/delta.py` | nightly new/changed/withdrawn diff |
| `src/nimbus/cli.py` | typer CLI and demo entry point |

---

### Task 1: Foundation, models, storage and the robots-gated fetch layer

Branch `feat/fetch-robots-cache`, PR #1.

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `.env.example`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml`, `README.md` (stub), `docs/adr/0001-record-architecture-decisions.md`, `docs/adr/0002-agent-learns-code-executes.md`
- Create: `src/nimbus/__init__.py`, `src/nimbus/config.py`, `src/nimbus/models.py`, `src/nimbus/storage.py`, `src/nimbus/fetch/__init__.py`, `src/nimbus/fetch/robots.py`, `src/nimbus/fetch/cache.py`, `src/nimbus/fetch/backends.py`, `src/nimbus/fetch/fetcher.py`
- Create: `tests/conftest.py`, `tests/fixtures/robots/normie.txt`, `tests/fixtures/robots/henstock.txt`, `tests/test_models.py`, `tests/test_robots.py`, `tests/test_cache.py`, `tests/test_storage.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `models.PropertyType(StrEnum)`: `DETACHED, SEMI_DETACHED, TERRACED, FLAT, BUNGALOW, LAND, OTHER`
  - `models.Tenure(StrEnum)`: `FREEHOLD, LEASEHOLD, SHARE_OF_FREEHOLD, UNKNOWN`
  - `models.Listing` fields: `source_domain: str`, `source_url: str`, `external_id: str | None`, `address: str`, `postcode: str | None`, `price_gbp: int | None`, `price_qualifier: str | None`, `bedrooms: int | None`, `bathrooms: int | None`, `property_type: PropertyType`, `tenure: Tenure`, `floor_area_sqft: float | None`, `description: str | None`, `agent_name: str | None`, `listed_date: date | None`, `content_hash: str`
  - `models.FieldHint`: `field: str`, `css_selector: str | None`, `notes: str | None`
  - `models.PaginationRule`: `kind: Literal["query_param","path","none"]`, `param: str | None`, `max_pages: int`
  - `models.SiteRecipe`: `domain: str`, `version: int`, `discovered_at: datetime`, `sitemap_urls: list[str]`, `listing_index_urls: list[str]`, `listing_url_patterns: list[str]`, `pagination: PaginationRule | None`, `field_hints: list[FieldHint]`, `render_required: bool`, `confidence: float`, `notes: str`
  - `models.ExtractionResult`: `listing: Listing | None`, `field_confidence: dict[str, float]`, `overall_confidence: float`, `needs_review: bool`, `issues: list[str]`
  - `models.AiSignals`: `content_signal: dict[str, str]`, `named_ai_agents: set[str]`, `ai_training_allowed: bool`
  - `config.Settings` (pydantic-settings, `.env`): `anthropic_api_key: SecretStr`, `cache_dir: Path`, `db_path: Path`, `user_agent: str`, `request_timeout: float = 20.0`, `default_crawl_delay: float = 1.0`, `fetch_backend: Literal["httpx","crawlbase"] = "httpx"`, `crawlbase_token: SecretStr | None`
  - `fetch.robots.RobotsPolicy.from_text(text: str, *, agent: str) -> RobotsPolicy`, `RobotsPolicy.allow_all(agent: str) -> RobotsPolicy`, `.can_fetch(url: str) -> bool`, `.crawl_delay: float`, `.sitemaps: list[str]`, `.ai_signals: AiSignals`
  - `fetch.cache.CachedPage`: `url, status, content_type, body, fetched_at`
  - `fetch.cache.HtmlCache(root: Path)`, `.key(url: str) -> str`, `.get(url: str) -> CachedPage | None`, `.put(url, status, content_type, body) -> CachedPage`
  - `fetch.backends.FetchBackend` protocol: `async def fetch(self, url: str, *, timeout: float) -> RawResponse`; `HttpxBackend`, `CrawlbaseBackend`
  - `fetch.fetcher.Fetcher(settings, cache, backend)`, `async .robots_for(domain: str) -> RobotsPolicy`, `async .get(url: str, *, allow_network: bool = True) -> CachedPage`, raises `RobotsDisallowed(url)`
  - `storage.init_db(path: Path) -> sqlite3.Connection`, `save_recipe(conn, recipe) -> int` (returns new version), `load_recipe(conn, domain) -> SiteRecipe | None`, `load_recipe_version(conn, domain, version) -> SiteRecipe | None`, `insert_audit(conn, **fields) -> None`

Tables created by `init_db`: `sites`, `recipes`, `listings`, `listing_history`, `extraction_audit`, `crawl_runs`.

`extraction_audit` must include `schema_valid INTEGER NOT NULL` and `needs_review INTEGER NOT NULL` alongside `model`, `prompt_version`, `recipe_version`, `html_hash`, `input_tokens`, `output_tokens`, `cost_usd`, `created_at`. Task 6 computes site health from those two columns, so they are not optional (pre-flight ruling R3).

- [ ] **Step 1: Scaffold the project and CI**

Create `pyproject.toml` (name `nimbus-listing-agent`, `requires-python = ">=3.12"`, `[tool.ruff] line-length = 100`, pytest markers `live`), `.gitignore` (include `.env`, `.cache/`, `*.db`), `.env.example`, `.pre-commit-config.yaml` (ruff + ruff-format), and `.github/workflows/ci.yml` running `uv sync`, `ruff check`, `ruff format --check`, `pytest -m "not live"` on Python 3.12.

- [ ] **Step 2: Write ADR-0001 and ADR-0002**

ADR-0001 records that ADRs are used. ADR-0002 records the central decision — *the agent learns a site once and emits a recipe; deterministic code executes it nightly* — with the cost and determinism reasoning from the spec.

- [ ] **Step 3: Capture robots fixtures**

Save the live `robots.txt` of `normie.co.uk` to `tests/fixtures/robots/normie.txt` and `henstockps.co.uk` to `tests/fixtures/robots/henstock.txt`. These are the only network reads in this task and they happen once, at authoring time.

- [ ] **Step 4: Write the failing tests**

```python
def test_robots_parses_crawl_delay_and_sitemaps():
    p = RobotsPolicy.from_text(HENSTOCK, agent="NimbusBot")
    assert p.crawl_delay == 2.0
    assert p.can_fetch("https://www.henstockps.co.uk/property/123") is True
    assert p.can_fetch("https://www.henstockps.co.uk/property/bedrooms/3") is False

def test_robots_detects_ai_directives():
    p = RobotsPolicy.from_text(NORMIE, agent="NimbusBot")
    assert p.ai_signals.ai_training_allowed is False
    assert p.ai_signals.content_signal["ai-train"] == "no"
    assert "ClaudeBot" in p.ai_signals.named_ai_agents

def test_robots_missing_file_defaults_to_allow_with_default_delay():
    p = RobotsPolicy.allow_all(agent="NimbusBot")
    assert p.can_fetch("https://example.co.uk/anything") is True
    assert p.crawl_delay == 1.0

def test_cache_key_is_stable_and_roundtrips():
    ...  # same URL -> same key; put then get returns identical body

async def test_fetcher_raises_on_disallowed_url():
    with pytest.raises(RobotsDisallowed):
        await fetcher.get("https://www.henstockps.co.uk/property/bedrooms/3")

async def test_fetcher_serves_cache_without_network():
    # backend that raises if called; cache pre-populated; get() succeeds

def test_listing_normalises_postcode_and_rejects_negative_price():
    assert Listing(..., postcode="m20 2rn").postcode == "M20 2RN"
    with pytest.raises(ValidationError):
        Listing(..., price_gbp=-1)

def test_init_db_creates_all_tables_and_recipe_versions_increment():
    assert save_recipe(conn, recipe) == 1
    assert save_recipe(conn, recipe) == 2
    assert load_recipe(conn, "ashworthholme.co.uk").version == 2
```

- [ ] **Step 5: Run tests to verify they fail**

Run: `uv run pytest -m "not live" -v`
Expected: FAIL — modules not found.

- [ ] **Step 6: Implement `models.py`, `config.py`, `fetch/`, `storage.py`**

Signatures are fixed in the Interfaces block above. Two points the signatures do not determine: `HtmlCache.key` is the SHA-256 of the normalised absolute URL, and `RobotsPolicy` must parse `Content-Signal:` directives (comma-separated `key=value` pairs) and record every `User-Agent` line that names a known AI crawler, in addition to standard `User-agent`/`Disallow`/`Allow`/`Crawl-delay`/`Sitemap` handling.

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest -m "not live" -v` → PASS. Then `uv run ruff check . && uv run ruff format --check .` → clean.

- [ ] **Step 8: Open PR #1 and merge on green CI**

```bash
git add -A
git commit -m "feat: robots-gated cache-first fetch layer with AI directive parsing"
gh pr create --title "feat: fetch layer, models, storage" --body "Closes #1"
```

---

### Task 2: Local stdio MCP server

Branch `feat/mcp-fetch-server`, PR #2.

**Files:**
- Create: `src/nimbus/mcp_server.py`, `docs/adr/0005-build-the-mcp-server-buy-the-anti-bot-infrastructure.md`
- Create: `tests/test_mcp_server.py`, `tests/fixtures/sitemaps/index.xml`, `tests/fixtures/sitemaps/property.xml`
- Modify: `pyproject.toml` (add `mcp`, `markdownify`; add `[project.scripts] nimbus-mcp = "nimbus.mcp_server:main"`)

**Interfaces:**
- Consumes: `Fetcher`, `RobotsPolicy`, `HtmlCache`, `Settings` from Task 1.
- Produces:
  - `async def get_robots(domain: str) -> dict` → `{"domain", "text", "crawl_delay", "sitemaps", "ai_signals", "fetchable"}`
  - `async def get_sitemap(domain: str, *, max_urls: int = 2000) -> dict` → `{"sitemap_urls", "url_count", "sample_urls"}`, recursively expanding `<sitemapindex>` one level
  - `async def fetch_page(url: str, as_markdown: bool = True, max_chars: int = 40000) -> dict` → `{"url", "status", "content", "truncated", "from_cache"}`, or `{"error": "robots_disallowed", ...}`
  - `def build_tools(fetcher: Fetcher) -> dict[str, Callable]` — returns the three tool callables bound to `fetcher`; tests exercise these directly without stdio (pre-flight ruling R1)
  - `def build_server(fetcher: Fetcher) -> FastMCP` — registers what `build_tools` returns
  - `def main() -> None` — runs the server over stdio

- [ ] **Step 1: Write the failing tests**

```python
async def test_fetch_page_refuses_disallowed_url_without_calling_backend():
    out = await fetch_page("https://www.henstockps.co.uk/property/bedrooms/3")
    assert out["error"] == "robots_disallowed"
    assert backend.call_count == 0

async def test_fetch_page_served_from_cache_reports_from_cache():
    out = await fetch_page(CACHED_URL)
    assert out["from_cache"] is True

async def test_get_sitemap_expands_sitemap_index():
    out = await get_sitemap("jaksandco.co.uk")
    assert out["url_count"] > 0
    assert any("/property" in u for u in out["sample_urls"])

async def test_fetch_page_markdown_is_smaller_than_html():
    md = await fetch_page(CACHED_URL, as_markdown=True)
    html = await fetch_page(CACHED_URL, as_markdown=False)
    assert len(md["content"]) < len(html["content"]) / 2

async def test_fetch_page_truncates_at_max_chars():
    out = await fetch_page(CACHED_URL, max_chars=500)
    assert out["truncated"] is True and len(out["content"]) <= 500
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mcp_server.py -v` → FAIL.

- [ ] **Step 3: Implement `mcp_server.py`**

Use `mcp.server.fastmcp.FastMCP`. Register the three tools with the signatures above. The robots check must happen inside `fetch_page` before the backend is consulted — this is the enforcement point and the reason the tool exists. Markdown conversion via `markdownify`, stripping `script`, `style`, `nav`, `footer`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -m "not live" -v` → PASS.

- [ ] **Step 5: Verify the server starts over stdio**

Run: `uv run nimbus-mcp` and confirm it blocks awaiting stdio rather than exiting. Kill it.

- [ ] **Step 6: Write ADR-0005**

Record why the MCP server is built here rather than taking Crawlbase or Firecrawl: the demo path must be offline and deterministic, and compliance must be enforceable inside the tool. Record the counterpart decision that JS rendering, proxy rotation and anti-bot evasion are commodity problems to buy, and that `CrawlbaseBackend` exists as the configured seam for that.

- [ ] **Step 7: Commit, PR #2, merge on green**

```bash
git commit -m "feat: local stdio MCP server with robots enforcement at the tool boundary"
```

---

### Task 3: Discovery agent emitting a versioned SiteRecipe

Branch `feat/discovery-agent`, PR #3.

**Files:**
- Create: `src/nimbus/discovery.py`, `src/nimbus/prompts/discovery.md`
- Create: `tests/test_discovery.py`, `tests/fixtures/recipes/valid.json`, `tests/fixtures/recipes/malformed.json`
- Modify: `pyproject.toml` (add `claude-agent-sdk`), `docs/adr/0003-mcp-for-exploration-direct-calls-for-the-hot-path.md`

**Interfaces:**
- Consumes: MCP tools from Task 2, `SiteRecipe` and `save_recipe` from Task 1.
- Produces:
  - `DISCOVERY_PROMPT_VERSION: str` (e.g. `"v1"`)
  - `def parse_recipe_payload(text: str, domain: str) -> SiteRecipe` — extracts the final JSON object from agent output and validates it; raises `RecipeParseError` on malformed or schema-invalid input
  - `def validate_recipe(recipe: SiteRecipe, sample_urls: list[str]) -> list[str]` — returns human-readable problems, empty when sound
  - `async def discover_site(domain: str, *, settings: Settings, max_turns: int = 20) -> SiteRecipe`

- [ ] **Step 1: Write ADR-0003**

Record the decision that MCP carries the exploratory agent while the nightly crawler calls the fetch layer directly, with the reasoning that MCP is a protocol for exposing tools to an LLM client and a batch crawler is not one.

- [ ] **Step 2: Write the failing tests**

```python
def test_parse_recipe_payload_accepts_json_in_prose():
    r = parse_recipe_payload("Here is the recipe:\n```json\n{...}\n```", "ashworthholme.co.uk")
    assert r.domain == "ashworthholme.co.uk"
    assert r.listing_url_patterns

def test_parse_recipe_payload_rejects_malformed_json():
    with pytest.raises(RecipeParseError):
        parse_recipe_payload(MALFORMED, "ashworthholme.co.uk")

def test_parse_recipe_payload_rejects_schema_violation():
    # confidence = 1.7 -> out of range
    with pytest.raises(RecipeParseError):
        parse_recipe_payload(BAD_CONFIDENCE, "ashworthholme.co.uk")

def test_validate_recipe_flags_pattern_matching_no_sample_urls():
    problems = validate_recipe(recipe_with_bogus_pattern, SAMPLE_URLS)
    assert any("matches none" in p for p in problems)

def test_validate_recipe_requires_a_source_of_urls():
    # Review Focus 1: no sitemap AND no listing index is not a usable recipe
    r = SiteRecipe(..., sitemap_urls=[], listing_index_urls=[])
    assert any("no sitemap" in p.lower() for p in validate_recipe(r, []))

@pytest.mark.live
async def test_discover_site_against_ashworthholme():
    r = await discover_site("ashworthholme.co.uk", settings=settings)
    assert r.confidence > 0.5 and r.listing_url_patterns
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_discovery.py -m "not live" -v` → FAIL.

- [ ] **Step 4: Write the discovery prompt**

`src/nimbus/prompts/discovery.md` instructs the agent to read robots first, prefer the sitemap, fall back to finding a listing index page when there is no sitemap, open two or three sample pages to confirm they are residential sales listings, and finish with a single JSON object matching the `SiteRecipe` schema and nothing else.

- [ ] **Step 5: Implement `discovery.py`**

`discover_site` runs `claude_agent_sdk.query` with `ClaudeAgentOptions` configured with the Task 2 MCP server and `allowed_tools` restricted to the three tools. Collect the final assistant text, call `parse_recipe_payload`, then `validate_recipe`; on failure, retry once with the problems fed back, then raise.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest -m "not live" -v` → PASS.

- [ ] **Step 7: Run the agent against all three sites for real and commit the recipes**

Run: `uv run python -m nimbus.discovery ashworthholme.co.uk --out tests/fixtures/recipes/ashworthholme.co.uk.json` (and the other two). Add a `__main__` block to `discovery.py` for this — the CLI does not exist until Task 7, so Task 3 must not depend on it (pre-flight ruling R6). Committing the recipes means later tasks and CI never need the agent.

- [ ] **Step 8: Commit, PR #3, merge on green**

```bash
git commit -m "feat: site discovery agent emitting schema-validated SiteRecipe"
```

---

### Task 4: Deterministic recipe-driven crawler

Branch `feat/recipe-crawler`, PR #4.

**Files:**
- Create: `src/nimbus/crawler.py`
- Create: `tests/test_crawler.py`
- Modify: `src/nimbus/models.py` (add `CrawlRecord`)

**Interfaces:**
- Consumes: `SiteRecipe`, `Fetcher`, `CachedPage` from Tasks 1 and 3.
- Produces:
  - `models.CrawlRecord`: `url: str`, `html: str`, `from_cache: bool`, `fetched_at: datetime`, `content_hash: str`
  - `def select_listing_urls(urls: list[str], recipe: SiteRecipe) -> list[str]` — pure; filters by `listing_url_patterns`, de-duplicates, preserves order
  - `async def collect_candidate_urls(recipe: SiteRecipe, *, fetcher: Fetcher) -> list[str]` — sitemaps first, listing index + pagination as fallback
  - `models.CrawlOutcome`: `domain: str`, `records: list[CrawlRecord]`, `candidates: int`, `selected: int`, `fetched: int`, `skipped_robots: int`, `skipped_errors: int`
  - `async def crawl_site(recipe: SiteRecipe, *, fetcher: Fetcher, crawl_delay: float, limit: int | None = None, sleep=asyncio.sleep) -> CrawlOutcome`

Returns counts, not a bare list (ruling R29). `crawl_site` collects candidates internally, so a caller that only receives records cannot populate `storage.crawl_runs` truthfully without re-running collection and re-fetching every sitemap. Also produces one summary log line per run, at ERROR when candidates existed but zero records came back.

Crawl delay is passed in explicitly, resolved by the caller from `await fetcher.robots_for(domain)`. It is live site policy and must not be frozen into a recipe that may be weeks stale (pre-flight ruling R5).

- [ ] **Step 1: Write the failing tests**

```python
def test_select_listing_urls_filters_and_dedupes():
    out = select_listing_urls([LISTING, ABOUT, LISTING], recipe)
    assert out == [LISTING]

async def test_crawl_site_honours_crawl_delay():
    delays = []
    await crawl_site(recipe, fetcher=f, crawl_delay=2.0, sleep=lambda d: delays.append(d))
    assert all(d >= 2.0 for d in delays)

async def test_crawl_site_respects_limit():
    assert len(await crawl_site(recipe, fetcher=f, crawl_delay=0.0, limit=3)) == 3

async def test_crawl_site_skips_robots_disallowed_without_failing_the_run():
    records = await crawl_site(recipe_including_disallowed_url, fetcher=f, crawl_delay=0.0)
    assert DISALLOWED not in [r.url for r in records]

def test_crawler_module_imports_no_llm_client():
    src = Path("src/nimbus/crawler.py").read_text()
    assert "anthropic" not in src and "claude_agent_sdk" not in src
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_crawler.py -v` → FAIL.

- [ ] **Step 3: Implement `crawler.py`**

No LLM import — the last test enforces the architectural boundary. `crawl_site` awaits `sleep(recipe crawl delay)` between fetches and logs but does not raise on `RobotsDisallowed`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -m "not live" -v` → PASS.

- [ ] **Step 5: Populate the HTML cache for all three sites**

Run the crawler live once with `--limit 15` per site so `tests/fixtures/pages/` and the cache hold real HTML. Commit a trimmed set of pages as fixtures.

- [ ] **Step 6: Commit, PR #4, merge on green**

```bash
git commit -m "feat: deterministic recipe-driven crawler with no LLM on the hot path"
```

---

### Task 5: LLM extractor, price parsing and audit trail

Branch `feat/llm-extractor`, PR #5.

**Files:**
- Create: `src/nimbus/extractor.py`, `src/nimbus/prompts/extract.md`
- Create: `tests/test_extractor.py`, `tests/test_price_parsing.py`
- Modify: `pyproject.toml` (add `anthropic`)

**Interfaces:**
- Consumes: `CrawlRecord`, `SiteRecipe`, `Listing`, `ExtractionResult`, `insert_audit`.
- Produces:
  - `EXTRACT_PROMPT_VERSION: str`
  - `def parse_price(raw: str) -> tuple[int | None, str | None]` — returns `(pounds, qualifier)`
  - `def is_residential_sale(raw_text: str, listing: Listing) -> bool`
  - `async def extract_listing(record: CrawlRecord, recipe: SiteRecipe, *, client) -> ExtractionResult`
  - `def audit_row(record, recipe, result, *, model: str, input_tokens: int, output_tokens: int, cost_usd: float) -> dict` — includes `schema_valid` and `needs_review` (pre-flight ruling R3)
  - `def record_extraction(conn, record, recipe, result, *, model: str, input_tokens: int, output_tokens: int, cost_usd: float) -> None` — composes `audit_row` and calls `storage.insert_audit`; this task owns audit persistence, Task 7 calls only this (pre-flight ruling R4)

- [ ] **Step 1: Write the failing tests**

```python
@pytest.mark.parametrize("raw,pounds,qual", [
    ("£450,000", 450_000, None),
    ("Offers in excess of £450,000", 450_000, "offers_in_excess_of"),
    ("Guide Price £1.25m", 1_250_000, "guide_price"),
    ("£450,000 - £475,000", 450_000, "range"),
    ("POA", None, "poa"),
    ("Price on application", None, "poa"),
    ("£1,250 pcm", None, "rental"),
])
def test_parse_price(raw, pounds, qual):
    assert parse_price(raw) == (pounds, qual)

def test_is_residential_sale_rejects_rental_and_sold_stc():
    assert is_residential_sale("£1,250 pcm available now", listing) is False
    assert is_residential_sale("Sold STC", listing) is False
    assert is_residential_sale("£450,000 Freehold", listing) is True

async def test_extract_listing_rejects_malformed_model_output():
    result = await extract_listing(record, recipe, client=StubClient(returns="not json"))
    assert result.listing is None and result.needs_review is True
    assert result.issues

async def test_extract_listing_rejects_price_not_corroborated_by_field_hint():
    # The real ashworthholme fixtures carry 48 distinct pound values from a
    # price-filter dropdown, £350 to £10,000,000, against a true price of
    # £650,000. A mere "does this value appear in the page" check passes a
    # wrong answer, so the price must be corroborated at the recipe's field
    # hint location (or the status-bearing heading), not merely be present.
    result = await extract_listing(record, recipe, client=StubClient(price=10_000_000))
    assert result.needs_review is True
    assert any("not corroborated" in i for i in result.issues)

async def test_extract_listing_on_a_page_with_no_extractable_fields():
    # bentleyhurst fixtures are client-rendered Gatsby shells with no price,
    # address or description in the HTML at all (ruling R27).
    result = await extract_listing(empty_shell_record, recipe, client=client)
    assert result.listing is None
    assert result.needs_review is True and result.issues

async def test_extract_listing_happy_path_sets_confidence_and_hash():
    result = await extract_listing(record, recipe, client=StubClient(valid_payload))
    assert result.listing.price_gbp == 450_000
    assert result.overall_confidence >= 0.8 and result.needs_review is False

def test_audit_row_records_model_and_prompt_and_recipe_versions():
    row = audit_row(record, recipe, result, model="claude-sonnet-5", input_tokens=1, output_tokens=1, cost_usd=0.0)
    assert row["prompt_version"] == EXTRACT_PROMPT_VERSION
    assert row["recipe_version"] == recipe.version
    assert row["html_hash"] == record.content_hash
    assert row["schema_valid"] is True and row["needs_review"] is False

def test_record_extraction_persists_a_readable_audit_row():
    record_extraction(conn, record, recipe, result, model="claude-sonnet-5",
                      input_tokens=10, output_tokens=5, cost_usd=0.001)
    assert audit_count(conn, record.url) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_extractor.py tests/test_price_parsing.py -v` → FAIL.

- [ ] **Step 3: Implement `parse_price` and `is_residential_sale`**

Pure functions, no LLM. `parse_price` recognises the qualifier vocabulary pinned by the parametrised test and returns `None` pounds for `poa` and `rental`. These run *after* the model, as a deterministic guard over its output.

- [ ] **Step 4: Implement `extract_listing`**

One `anthropic` message per page with the `Listing` schema supplied as a tool so the model returns structured output. Validate with pydantic; cross-check the returned price string against the page text and flag `needs_review` when absent. Never return a `Listing` that failed validation.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest -m "not live" -v` → PASS.

- [ ] **Step 6: Commit, PR #5, merge on green**

```bash
git commit -m "feat: schema-validated LLM extraction with deterministic price guards and audit trail"
```

---

### Task 6: Health monitoring, recipe self-heal and nightly delta

Branch `feat/self-heal-and-delta`, PR #6.

**Files:**
- Create: `src/nimbus/heal.py`, `src/nimbus/delta.py`
- Create: `tests/test_heal.py`, `tests/test_delta.py`, `tests/fixtures/pages/mutated/`
- Modify: `src/nimbus/models.py` (add `SiteHealth`, `DeltaReport`), `src/nimbus/storage.py` (listing upsert and history)

**Interfaces:**
- Consumes: `discover_site`, `extract_listing`, `storage`, `Listing`.
- Produces:
  - `models.SiteHealth`: `domain: str`, `window: int`, `schema_valid_rate: float`, `needs_review_rate: float`, `sample_size: int`
  - `models.DeltaReport`: `new: list[str]`, `price_changed: list[tuple[str, int, int]]`, `withdrawn: list[str]`, `unchanged: list[str]`
  - `def site_health(conn, domain: str, window: int = 50) -> SiteHealth`
  - `def needs_heal(health: SiteHealth, *, threshold: float = 0.8) -> bool`
  - `async def heal_site(conn, domain: str, *, settings) -> tuple[SiteRecipe, list[str]]` — re-discovers, returns the new recipe and a human-readable diff against the previous version
  - `def apply_delta(conn, domain: str, listings: list[Listing]) -> DeltaReport`

- [ ] **Step 1: Create the mutated page fixtures**

Copy three cached pages from one site into `tests/fixtures/pages/mutated/` and change the markup the recipe depends on — rename the price container class, move the address into a different element. This is the simulated site redesign.

- [ ] **Step 2: Write the failing tests**

```python
def test_site_health_computes_rates_over_window():
    h = site_health(conn, "ashworthholme.co.uk", window=10)
    assert h.sample_size == 10 and 0.0 <= h.schema_valid_rate <= 1.0

def test_needs_heal_triggers_below_threshold():
    assert needs_heal(SiteHealth(..., schema_valid_rate=0.4)) is True
    assert needs_heal(SiteHealth(..., schema_valid_rate=0.95)) is False

def test_apply_delta_classifies_new_changed_withdrawn_unchanged():
    report = apply_delta(conn, DOMAIN, [changed, same, brand_new])
    assert report.new == [brand_new.source_url]
    assert report.price_changed == [(changed.source_url, 450_000, 425_000)]
    assert report.withdrawn == [GONE_URL]
    assert report.unchanged == [same.source_url]

def test_apply_delta_writes_price_history_row():
    apply_delta(conn, DOMAIN, [changed])
    assert history_rows(conn, changed.source_url) == [(450_000, 425_000)]

def test_apply_delta_is_idempotent():
    r1 = apply_delta(conn, DOMAIN, LISTINGS)
    r2 = apply_delta(conn, DOMAIN, LISTINGS)
    assert r2.new == [] and set(r2.unchanged) == set(l.source_url for l in LISTINGS)

async def test_heal_site_bumps_version_and_reports_diff():
    recipe, diff = await heal_site(conn, DOMAIN, settings=settings)
    assert recipe.version == previous.version + 1 and diff
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_heal.py tests/test_delta.py -v` → FAIL.

- [ ] **Step 4: Implement `delta.py` then `heal.py`**

Delta compares by `source_url` within a domain; a listing seen previously and absent from this run is withdrawn. Price changes write a `listing_history` row. `heal_site` calls `discover_site` again, diffs field hints and URL patterns against the stored recipe, and saves as a new version rather than overwriting.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest -m "not live" -v` → PASS.

- [ ] **Step 6: Commit, PR #6, merge on green**

```bash
git commit -m "feat: site health monitoring, recipe self-heal and nightly delta"
```

---

### Task 7: CLI, reproducible demo and documentation

Branch `feat/cli-and-demo`, PR #7.

**Files:**
- Create: `src/nimbus/cli.py`, `scripts/demo.sh`, `docs/architecture.md`, `docs/pricing-model.md`, `docs/adr/0004-cache-first-fetch-for-reproducible-demos.md`
- Create: `tests/test_cli.py`
- Modify: `README.md`, `pyproject.toml` (add `typer`; `[project.scripts] nimbus = "nimbus.cli:app"`)

**Interfaces:**
- Consumes: everything above.
- Produces: a typer app with a global `--db PATH` option defaulting to `Settings.db_path` (pre-flight ruling R8), and commands `onboard <domain>`, `crawl <domain> [--limit N]`, `heal <domain>`, `report [--domain D]`, `demo [--offline]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_cli_exposes_all_commands():
    out = runner.invoke(app, ["--help"]).output
    for cmd in ("onboard", "crawl", "heal", "report", "demo"):
        assert cmd in out

def test_report_renders_seeded_database_without_network():
    result = runner.invoke(app, ["report", "--db", str(seeded_db)])
    assert result.exit_code == 0
    assert "ashworthholme.co.uk" in result.output

def test_demo_runs_offline_from_fixtures():
    result = runner.invoke(app, ["demo", "--offline"])
    assert result.exit_code == 0
    assert "recipe repaired" in result.output.lower()

def test_demo_offline_makes_no_agent_call():
    # discover_site patched to raise; --offline must still succeed
    result = runner.invoke(app, ["demo", "--offline"])
    assert result.exit_code == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -v` → FAIL.

- [ ] **Step 3: Implement `cli.py`**

Typer app. `demo --offline` runs the full narrative from fixtures with **no network and no agent call at all**: show a recipe, crawl from cache, extract, report the delta, swap in the mutated pages, show the health rate collapse, then drive the real diff-and-version-bump code path with `discover_site` stubbed by a committed post-heal recipe fixture (`tests/fixtures/recipes/<domain>.healed.json`), and show the recovered extraction. The live `heal` command runs the genuine agent. The spec names a network-dependent demo as an anti-goal, so the offline path must not call the agent (pre-flight ruling R7).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -m "not live" -v` → PASS.

- [ ] **Step 5: Write the documentation**

`README.md`: what it is, the architecture in one diagram, quickstart, the demo command, and an honest "what is not built" section. `docs/architecture.md`: the component walkthrough and the production path. `docs/pricing-model.md`: the written answer to part 2 of the brief — training dataset construction from Land Registry Price Paid joined to collected listings, feature selection, comparable selection, gradient boosting on log price with conformal prediction intervals, the abstain rule for thin evidence, and time-ordered splits to avoid leakage.

- [ ] **Step 6: Verify the whole demo end to end with the network off**

Run: `uv run nimbus demo --offline` twice and confirm identical output both times.

- [ ] **Step 7: Commit, PR #7, merge on green**

```bash
git commit -m "feat: CLI, offline reproducible demo and architecture documentation"
```

---

## Timing

| Task | Budget |
|---|---|
| 1 — foundation, models, storage, fetch | 1:30 |
| 2 — MCP server | 1:00 |
| 3 — discovery agent | 1:30 |
| 4 — crawler | 0:45 |
| 5 — extractor | 1:15 |
| 6 — heal and delta | 1:15 |
| 7 — CLI, demo, docs | 0:45 |
| **Total** | **8:00** |

The 4–5 page solution document, the architecture diagram and the presentation are produced after this plan completes and are not costed here.
