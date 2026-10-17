"""Pydantic models shared across every component.

These are the data contracts: the discovery agent emits a SiteRecipe, the
crawler produces pages, the extractor produces Listings and
ExtractionResults, storage persists all of them. Keep this module free of
I/O and free of any dependency on the fetch layer, the LLM clients, or
storage — it only describes shapes.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class PropertyType(StrEnum):
    DETACHED = "detached"
    SEMI_DETACHED = "semi_detached"
    TERRACED = "terraced"
    FLAT = "flat"
    BUNGALOW = "bungalow"
    LAND = "land"
    OTHER = "other"


class Tenure(StrEnum):
    FREEHOLD = "freehold"
    LEASEHOLD = "leasehold"
    SHARE_OF_FREEHOLD = "share_of_freehold"
    UNKNOWN = "unknown"


class Listing(BaseModel):
    source_domain: str
    source_url: str
    external_id: str | None = None
    address: str
    postcode: str | None = None
    price_gbp: int | None = Field(default=None, ge=0)
    price_qualifier: str | None = None
    bedrooms: int | None = Field(default=None, ge=0)
    bathrooms: int | None = Field(default=None, ge=0)
    property_type: PropertyType
    tenure: Tenure
    floor_area_sqft: float | None = Field(default=None, ge=0)
    description: str | None = None
    agent_name: str | None = None
    listed_date: date | None = None
    content_hash: str

    @field_validator("postcode")
    @classmethod
    def _normalise_postcode(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return " ".join(value.split()).upper()


class FieldHint(BaseModel):
    field: str
    css_selector: str | None = None
    notes: str | None = None


class PaginationRule(BaseModel):
    kind: Literal["query_param", "path", "none"]
    param: str | None = None
    max_pages: int = Field(ge=1)


class SiteRecipe(BaseModel):
    domain: str
    version: int = Field(ge=1)
    discovered_at: datetime
    sitemap_urls: list[str] = Field(default_factory=list)
    listing_index_urls: list[str] = Field(default_factory=list)
    listing_url_patterns: list[str] = Field(default_factory=list)
    # URLs of individual listing pages the discovery agent actually opened
    # and confirmed were residential sales listings -- the evidence behind
    # listing_url_patterns, and what validate_recipe checks the pattern
    # against (not sitemap_urls/listing_index_urls, which are index pages,
    # not listings).
    sample_listing_urls: list[str] = Field(default_factory=list)
    pagination: PaginationRule | None = None
    field_hints: list[FieldHint] = Field(default_factory=list)
    render_required: bool = False
    confidence: float = Field(ge=0.0, le=1.0)
    notes: str = ""


class CrawlRecord(BaseModel):
    """One page fetched by the deterministic crawler (Task 4), before any
    extraction happens.

    `content_hash` is what makes the nightly run cheap: `cli.crawl`
    compares it against the stored `Listing.content_hash` (see
    `nimbus.extractor.unchanged_since_last_run`) and skips extraction
    entirely for a page whose bytes have not moved, so the only model call
    on the nightly path is one per *changed* listing page."""

    url: str
    html: str
    from_cache: bool
    fetched_at: datetime
    content_hash: str


class CrawlOutcome(BaseModel):
    """The full result of one `crawl_site` run against one domain.

    `records` is what Task 5 extracts from. The counts exist so an
    orchestration layer can write a truthful `crawl_runs` row (Task 7)
    without re-deriving them by re-crawling: `crawl_site` already knows
    exactly how many candidate URLs it found, how many matched the
    recipe's listing_url_patterns, how many were actually fetched, and how
    many were skipped for each of the two tolerated reasons (robots.txt,
    or a transport-level FetchError) -- recovering that from `records`
    alone is impossible once the skipped URLs have been discarded.

    `selected` and `attempted` are deliberately separate, and the
    distinction is load-bearing rather than cosmetic. `selected` is how
    many URLs the *recipe* implied -- every candidate matching
    `listing_url_patterns`, before any `--limit`. `attempted` is how many
    of those this particular run actually walked. They differ only when an
    operator passed an explicit limit, and conflating them is what let
    `--limit 5` against a 195-listing site report "5 of 5 fetched" and so
    authorise withdrawing the other 190 (see
    `nimbus.delta.should_allow_mass_withdrawal`, which divides by
    `selected`, never `attempted`)."""

    domain: str
    records: list[CrawlRecord]
    candidates: int
    selected: int
    attempted: int
    fetched: int
    skipped_robots: int
    skipped_errors: int
    # True when an explicit `limit` truncated the selected set. A limited
    # run is by construction a partial view of the site, so it is never
    # evidence that the URLs it did not even look at have gone.
    limit_applied: bool = False


class RejectionReason(StrEnum):
    """Why `extract_listing` produced no `Listing`.

    The distinction that matters is between a *deliberate exclusion* and a
    *failure*. `NOT_RESIDENTIAL_SALE` means the pipeline worked exactly as
    designed and decided this page is a Sold-STC or To-Let listing we do
    not collect; the other three mean something went wrong, and are the
    signal Task 6's health monitor exists to watch. Conflating them made a
    *correct* recipe on this project's own demo site report
    schema_valid_rate=0.333 and needs_heal=True, because 12 of
    ashworthholme.co.uk's 18 cached pages are correctly-rejected non-sales.
    """

    NOT_RESIDENTIAL_SALE = "not_residential_sale"
    NO_CORROBORATION_REGION = "no_corroboration_region"
    NO_TOOL_OUTPUT = "no_tool_output"
    SCHEMA_INVALID = "schema_invalid"


# Rejections that are correct outcomes rather than evidence of drift.
# `nimbus.heal.site_health` drops these from its denominator entirely:
# they say nothing about whether the recipe still matches the site.
DELIBERATE_EXCLUSIONS = frozenset({RejectionReason.NOT_RESIDENTIAL_SALE})


class ExtractionResult(BaseModel):
    listing: Listing | None = None
    field_confidence: dict[str, float] = Field(default_factory=dict)
    overall_confidence: float = Field(ge=0.0, le=1.0)
    needs_review: bool = False
    issues: list[str] = Field(default_factory=list)
    # None whenever `listing` is set. Otherwise names which of the four
    # rejection paths was taken, so the audit row -- and through it
    # site_health -- can tell a deliberate exclusion from a failure.
    rejection_reason: RejectionReason | None = None


class AiSignals(BaseModel):
    content_signal: dict[str, str] = Field(default_factory=dict)
    named_ai_agents: set[str] = Field(default_factory=set)
    ai_training_allowed: bool = True


class SiteHealth(BaseModel):
    """A domain's extraction health over its most recent `window` audit
    rows (Task 6). Deliberately built from `extraction_audit.schema_valid`
    / `needs_review` — never from `ExtractionResult.overall_confidence`,
    which only ever takes the values 1.0 or 0.6 and carries no information
    beyond `needs_review` (Task 5's own finding)."""

    domain: str
    window: int
    schema_valid_rate: float = Field(ge=0.0, le=1.0)
    needs_review_rate: float = Field(ge=0.0, le=1.0)
    sample_size: int = Field(ge=0)
    # Audit rows that were dropped from the sample above because they
    # record a deliberate exclusion (a Sold-STC or To-Let page the
    # extractor correctly refused) rather than a failure. Reported, not
    # hidden: an operator should be able to see that a site's health is
    # being judged on 6 pages out of 18 and why.
    deliberate_exclusions: int = Field(default=0, ge=0)


class DeltaReport(BaseModel):
    """The result of comparing one night's crawl against what storage
    already knew for a domain (Task 6), by `source_url`. `price_changed`
    entries are `(source_url, old_price_gbp, new_price_gbp)`."""

    new: list[str] = Field(default_factory=list)
    price_changed: list[tuple[str, int, int]] = Field(default_factory=list)
    withdrawn: list[str] = Field(default_factory=list)
    unchanged: list[str] = Field(default_factory=list)
    # True when apply_delta refused to report a mass withdrawal (more than
    # half of a domain's stored listings vanishing in one run -- almost
    # always a failed or partial crawl, not an overnight sell-off) and left
    # those listings untouched instead. See nimbus.delta.apply_delta.
    withdrawal_suppressed: bool = False
