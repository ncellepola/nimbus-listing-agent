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
    extraction happens. `content_hash` lets Task 5's delta detection decide
    whether a previously-seen page changed without re-extracting it."""

    url: str
    html: str
    from_cache: bool
    fetched_at: datetime
    content_hash: str


class CrawlOutcome(BaseModel):
    """The full result of one `crawl_site` run against one domain.

    `records` is what Task 5 extracts from. The five counts exist so an
    orchestration layer can write a truthful `crawl_runs` row (Task 7)
    without re-deriving them by re-crawling: `crawl_site` already knows
    exactly how many candidate URLs it found, how many matched the
    recipe's listing_url_patterns, how many were actually fetched, and how
    many were skipped for each of the two tolerated reasons (robots.txt,
    or a transport-level FetchError) -- recovering that from `records`
    alone is impossible once the skipped URLs have been discarded."""

    domain: str
    records: list[CrawlRecord]
    candidates: int
    selected: int
    fetched: int
    skipped_robots: int
    skipped_errors: int


class ExtractionResult(BaseModel):
    listing: Listing | None = None
    field_confidence: dict[str, float] = Field(default_factory=dict)
    overall_confidence: float = Field(ge=0.0, le=1.0)
    needs_review: bool = False
    issues: list[str] = Field(default_factory=list)


class AiSignals(BaseModel):
    content_signal: dict[str, str] = Field(default_factory=dict)
    named_ai_agents: set[str] = Field(default_factory=set)
    ai_training_allowed: bool = True
