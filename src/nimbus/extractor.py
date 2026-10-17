"""The per-page LLM extractor, its deterministic price/sale guards, and the
extraction audit trail.

Architecture (ADR-0002/0003 continued): Task 3's discovery agent and Task
4's crawler never touch an LLM. This module is where the one LLM call per
page happens — a single `anthropic` Messages API request per `CrawlRecord`,
not an agent loop (`claude_agent_sdk`), because turning crawled HTML into a
`Listing` is one transformation, not an open-ended task that needs tool
choice turn by turn.

The one call is not trusted on its own. Two deterministic layers sit around
it, both exercised directly against the real `ashworthholme.co.uk` and
`bentleyhurst.co.uk` fixtures (not just synthetic payloads), because those
are where a naive implementation breaks:

1. `parse_price` / `is_residential_sale` are pure, no-LLM functions that run
   on the model's own output. The model is asked to copy the literal
   status-and-price text it saw (`price_raw`) rather than compute a number,
   so these two functions — not the model — decide the pounds figure, the
   qualifier (`poa`, `rental`, `range`, ...), and whether the page is even a
   residential sale at all.
2. `extract_listing` cross-checks the model's reported price against the
   page content actually reachable from the recipe's own `field_hints`
   price location (or a status-bearing `h1`/`h2`/`h3`), never against the
   page as a whole. `ashworthholme.co.uk`'s real
   fixtures carry 48-49 unrelated pound amounts from a price-filter
   dropdown (£350 to £10,000,000); a bare substring-in-page check passes
   all of them, including the wrong ones, so the search is scoped to where
   the recipe says the real price actually lives.

A page that offers no such location at all (`bentleyhurst.co.uk`'s
fixtures are client-rendered Gatsby shells with zero headings and zero
pound signs in the fetched HTML) is rejected before the model is even
called: there is nothing to corroborate against, so no amount of model
confidence can be trusted, and paying for a call that can never be
validated is pointless.

On confidence: nothing here asks the model how confident it is. A model
asked that question says 0.9 regardless of whether it is right (see the
module docstring's design note duplicated in the task report). Instead,
`field_confidence` distinguishes "passed an actual check" (1.0), "present
but only the model's own say-so, never independently checked" (0.5), and
"absent, or rejected by a check" (0.0); `overall_confidence` is a ceiling of
1.0 with a deduction applied only for a failed *checkable* guard (price
corroboration), not a plain average across many unverifiable optional
fields — averaging in a dozen 0.5s for fields nothing here verifies would
drag even a perfectly corroborated extraction below any sane review
threshold, which is exactly the "decorative number" failure mode to avoid.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from pydantic import ValidationError

from nimbus import storage
from nimbus.htmltext import html_to_markdown
from nimbus.models import (
    CrawlRecord,
    ExtractionResult,
    Listing,
    PropertyType,
    RejectionReason,
    SiteRecipe,
    Tenure,
)


class ExtractionError(Exception):
    """Raised when `client.messages.create` itself fails for `record.url` —
    a timeout, a rate limit, an authentication failure, any transport- or
    API-level problem the `anthropic` client raises.

    Mirrors `nimbus.fetch.fetcher.FetchError`'s shape and purpose: `Fetcher.get()`
    wraps whatever a backend raises so callers catch one Nimbus-level type
    regardless of backend; `extract_listing` does the same here so a
    caller (Task 7's orchestrator) catches `ExtractionError` without
    importing anything `anthropic`-specific. It still propagates — this
    boundary changes the type, it does not swallow the failure.
    """

    def __init__(self, url: str, cause: BaseException) -> None:
        self.url = url
        self.cause = cause
        super().__init__(f"failed to extract {url}: {cause}")


EXTRACT_PROMPT_VERSION = "v1"

# The model actually called for extraction. Separate from the `model`
# string callers pass into `audit_row`/`record_extraction` — those record
# whatever model the caller says ran, which lets a future re-extraction
# under a different model id still produce a truthful audit row.
EXTRACTION_MODEL = "claude-sonnet-5"

_PROMPT_PATH = Path(__file__).parent / "prompts" / "extract.md"

_TOOL_NAME = "emit_listing"

# Where the true price lives when the recipe has no field_hint selector for
# it, or the selector matches nothing on this particular page: status text
# (e.g. "For Sale £650,000", "To Let £1,000 PCM") is overwhelmingly carried
# in a top-level heading, never deep in body copy.
_HEADING_TAGS = ("h1", "h2", "h3")

# Elements that are *inputs* (a price-filter dropdown's <select>/<option>,
# a search <form>, a text/number <input>, a <datalist>) rather than
# anything the page asserts about this listing. ashworthholme.co.uk's own
# price-filter <select> even has id="main-minprice-sales", which would
# otherwise satisfy _PRICE_WIDGET_SELECTOR below directly -- stripping
# these subtrees first means no later rule in _corroboration_text can ever
# see a decoy value a user merely *could have chosen*, no matter how
# broadly that rule matches.
_FORM_CONTROL_TAGS = ("select", "option", "form", "input", "datalist")

# Widens the corroboration region to elements that *name themselves* as a
# price widget, for a site like jaksandco.co.uk whose recipe has no
# field_hint selector for price and whose price sits in a plain
# `<div class="price">` rather than any heading at all.
_PRICE_WIDGET_SELECTOR = "[class*='price'], [id*='price']"

# A real price widget is a short UI snippet (jaksandco.co.uk's own is under
# 40 characters). This caps _PRICE_WIDGET_SELECTOR against a WordPress-style
# body/container class such as "price_qualifier-guide-price" -- that
# literally contains "price" too, but it decorates the whole page's <body>
# or a top-level wrapper, and matching it would widen the corroboration
# region back to nearly the entire page: exactly what this function exists
# to avoid: corroboration must stay scoped to where the price lives.
_MAX_PRICE_WIDGET_CHARS = 300

# Cap on how much markdown page text is sent to the model. The six real
# fixtures convert to 2.8-18.7KB of markdown; this is a safety margin, not a
# limit expected to bite in practice.
_MAX_PAGE_TEXT_CHARS = 20_000

_QUALIFIER_KEYWORDS: list[tuple[str, str]] = [
    ("offers in excess of", "offers_in_excess_of"),
    ("offers over", "offers_over"),
    ("oiro", "oiro"),
    ("guide price", "guide_price"),
]

_RENTAL_MARKERS = ("pcm", "per calendar month", "per month", "/pm", "pw")
_LETTING_STATUS_MARKERS = ("to let", "let agreed", "to rent")
_SOLD_MARKERS = ("sold stc", "sold subject to contract", "sold")

# Markers the *pre-model* filter acts on. A strict subset of the three
# tuples above, by design: this filter is an optimisation, never a new
# policy. It may only skip a model call that `is_residential_sale` would
# certainly have rejected afterwards, so it carries the unambiguous
# multi-word status phrases and deliberately omits `_SOLD_MARKERS`' bare
# "sold" (a "Recently Sold Nearby" nav heading must not cost a live
# listing) and `_RENTAL_MARKERS`' two-character "pw"/"/pm", which are too
# short to match safely against a whole corroboration region.
_UNAMBIGUOUS_NON_SALE_MARKERS = (
    "sold stc",
    "sold subject to contract",
    "to let",
    "let agreed",
    "to rent",
    "pcm",
    "per calendar month",
)

_SCORED_FIELDS = (
    "address",
    "postcode",
    "price_gbp",
    "bedrooms",
    "bathrooms",
    "property_type",
    "tenure",
    "floor_area_sqft",
    "description",
    "agent_name",
    "listed_date",
    "external_id",
)


# --------------------------------------------------------------------------
# Pure, no-LLM guards. These run on the model's output, never on its own
# say-so (Step 3 of the brief): the model copies literal page text, these
# functions decide what it means.
# --------------------------------------------------------------------------


_MONEY_RE = re.compile(r"£\s?([0-9][0-9,]*(?:\.[0-9]+)?)\s*(million|m|k)?\b", re.IGNORECASE)


def _money_matches(text: str) -> list[tuple[str, str]]:
    return [(num, suffix or "") for num, suffix in _MONEY_RE.findall(text)]


def _to_pounds(num: str, suffix: str) -> int:
    value = float(num.replace(",", ""))
    suffix = suffix.lower()
    if suffix in ("m", "million"):
        value *= 1_000_000
    elif suffix == "k":
        value *= 1_000
    return int(round(value))


def parse_price(raw: str) -> tuple[int | None, str | None]:
    """Parse a raw price string into `(pounds, qualifier)`.

    Pure and deterministic — no LLM involved. This is the guard the model's
    `price_raw` text is run through, not something the model computes
    itself. `pounds` is `None` for `poa` and `rental` (there is no sale
    price to report); for a range, the lower bound is returned with
    qualifier `"range"`.
    """
    text = raw.strip()
    lowered = text.lower()

    if "price on application" in lowered or re.search(r"\bpoa\b", lowered):
        return None, "poa"

    if re.search(r"\bpcm\b", lowered) or "per calendar month" in lowered:
        return None, "rental"

    matches = _money_matches(text)
    if len(matches) >= 2:
        pounds = _to_pounds(*matches[0])
        return pounds, "range"

    qualifier = None
    for phrase, slug in _QUALIFIER_KEYWORDS:
        if phrase in lowered:
            qualifier = slug
            break

    if not matches:
        return None, qualifier

    pounds = _to_pounds(*matches[0])
    return pounds, qualifier


def is_residential_sale(raw_text: str, listing: Listing) -> bool:
    """Whether `raw_text` (the model's literal status/price text) describes
    an open residential **sale** — never a rental, a sold/let listing, or a
    listing already rejected by `parse_price`. Pure, no LLM.

    `listing.price_qualifier` (already produced by `parse_price`) is
    checked alongside `raw_text` itself, because some disqualifying signals
    (an explicit "Sold STC"/"To Let" status with no visible price at all)
    never reach `parse_price` as a price string in the first place.
    """
    lowered = raw_text.lower()

    if listing.price_qualifier == "rental":
        return False

    if any(marker in lowered for marker in _RENTAL_MARKERS):
        return False
    if any(marker in lowered for marker in _LETTING_STATUS_MARKERS):
        return False
    if any(marker in lowered for marker in _SOLD_MARKERS):
        return False

    return True


def non_sale_marker(status_text: str) -> str | None:
    """The first unambiguous non-sale status marker in `status_text`, or
    `None`. Pure, no LLM, and cheap enough to run before the model call.

    Measured on this repo's own cached pages: 12 of ashworthholme.co.uk's
    18 listing pages (11 Sold STC, 1 To Let) carry one of these markers in
    the status heading the corroboration region already contains, with no
    false positive on any of the 6 live For Sale pages. That is two thirds
    of that site's extraction spend, previously paid to have the model
    tell us something the heading already said.
    """
    lowered = status_text.lower()
    for marker in _UNAMBIGUOUS_NON_SALE_MARKERS:
        if marker in lowered:
            return marker
    return None


# --------------------------------------------------------------------------
# HTML -> page text / corroboration region. BeautifulSoup decodes HTML
# entities while parsing (so `&pound;` becomes `£` in every `.get_text()`
# call below) without any separate unescaping step.
# --------------------------------------------------------------------------


def _page_markdown(html: str) -> str:
    return html_to_markdown(html)[:_MAX_PAGE_TEXT_CHARS]


def _corroboration_text(soup: BeautifulSoup, recipe: SiteRecipe) -> str:
    """Text from the recipe's own price field-hint location, every
    top-level heading, and any element that names itself as a price
    widget by class/id. Never the whole page — the
    whole page is exactly where ashworthholme.co.uk's 48-49 unrelated price
    values live.

    Works against a copy of `soup` with every form-control subtree
    (`select`/`option`/`form`/`input`/`datalist`) removed first, so a
    decoy value that exists only inside a price-filter dropdown's
    `<option>` is never visible to *any* of the three rules below, however
    broadly one of them might otherwise match -- not just the heading scan
    jaksandco.co.uk needs none of this for, but the class/id widening rule
    that site does need, which would otherwise be exactly as likely to
    re-admit ashworthholme.co.uk's dropdown as the thing it was added to
    find.
    """
    cleaned = BeautifulSoup(str(soup), "html.parser")
    for tag in cleaned(_FORM_CONTROL_TAGS):
        tag.decompose()

    parts: list[str] = []

    price_hint = next((h for h in recipe.field_hints if h.field == "price"), None)
    if price_hint is not None and price_hint.css_selector:
        try:
            matched = cleaned.select(price_hint.css_selector)
        except Exception:
            matched = []
        parts.extend(el.get_text(" ", strip=True) for el in matched)

    parts.extend(el.get_text(" ", strip=True) for el in cleaned.find_all(_HEADING_TAGS))

    # jaksandco.co.uk has no field_hint selector for price and no
    # price-bearing heading at all -- its price lives only in a
    # `<div class="price">`. The length cap excludes a WordPress-style
    # body/container class that also contains the substring "price"
    # (e.g. "price_qualifier-guide-price") but wraps the whole page.
    for element in cleaned.select(_PRICE_WIDGET_SELECTOR):
        text = element.get_text(" ", strip=True)
        if text and len(text) <= _MAX_PRICE_WIDGET_CHARS:
            parts.append(text)

    return "\n".join(part for part in parts if part)


def _field_hints_block(recipe: SiteRecipe) -> str:
    if not recipe.field_hints:
        return "(no field hints recorded for this site)"
    lines = []
    for hint in recipe.field_hints:
        selector = hint.css_selector or "(no selector)"
        notes = hint.notes or ""
        lines.append(f"- {hint.field}: {selector} — {notes}")
    return "\n".join(lines)


def _render_extract_prompt(record: CrawlRecord, recipe: SiteRecipe, page_text: str) -> str:
    template = _PROMPT_PATH.read_text()
    return (
        template.replace("__DOMAIN__", recipe.domain)
        .replace("__URL__", record.url)
        .replace("__FIELD_HINTS__", _field_hints_block(recipe))
        .replace("__PAGE_TEXT__", page_text)
    )


def _listing_tool() -> dict[str, Any]:
    return {
        "name": _TOOL_NAME,
        "description": (
            "Emit the residential sale listing data extracted from the page. Call this "
            "exactly once, even for a page that turns out not to be a residential sale "
            "listing (give your best-effort fields and an honest price_raw instead)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "address": {"type": ["string", "null"]},
                "postcode": {"type": ["string", "null"]},
                "price_raw": {
                    "type": "string",
                    "description": (
                        "The exact status-and-price text as it appears on the page, "
                        "verbatim, e.g. 'For Sale £650,000', 'POA', '£1,000 PCM', "
                        "'Sold STC'. Never a number you computed yourself."
                    ),
                },
                "bedrooms": {"type": ["integer", "null"]},
                "bathrooms": {"type": ["integer", "null"]},
                "property_type": {
                    "type": ["string", "null"],
                    "enum": [member.value for member in PropertyType] + [None],
                },
                "tenure": {
                    "type": ["string", "null"],
                    "enum": [member.value for member in Tenure] + [None],
                },
                "floor_area_sqft": {"type": ["number", "null"]},
                "description": {"type": ["string", "null"]},
                "agent_name": {"type": ["string", "null"]},
                "listed_date": {"type": ["string", "null"]},
                "external_id": {"type": ["string", "null"]},
            },
            "required": ["address", "price_raw"],
        },
    }


def _field_confidence(listing: Listing) -> dict[str, float]:
    """Each field's confidence, derived from what was actually checked —
    never from anything the model asserted about its own certainty.
    `price_gbp` is 1.0 whenever it is non-null: a price that failed
    corroboration is already rewritten to `None` in `extract_listing`
    *before* this runs, so a non-null price here has, by construction,
    already passed that check (there is no remaining branch where a
    non-null, uncorroborated price reaches this function — unlike an
    earlier version of this function, which carried one anyway). Every
    other field has no independent check in this task, so a present value
    scores 0.5 (the model's own unverified claim) rather than 1.0; absent
    is 0.0."""
    scores: dict[str, float] = {}
    for field in _SCORED_FIELDS:
        value = getattr(listing, field)
        if value is None:
            scores[field] = 0.0
        elif field == "price_gbp":
            scores[field] = 1.0
        else:
            scores[field] = 0.5
    return scores


def _empty_result(issue: str, reason: RejectionReason) -> ExtractionResult:
    return ExtractionResult(
        listing=None,
        field_confidence={},
        overall_confidence=0.0,
        needs_review=True,
        issues=[issue],
        rejection_reason=reason,
    )


def unchanged_since_last_run(conn: sqlite3.Connection, record: CrawlRecord) -> Listing | None:
    """The stored `Listing` for `record.url` when this run fetched
    byte-identical HTML, or `None` when there is nothing stored or the
    page has changed.

    This is what makes "no LLM call for a page that did not change" true
    rather than merely intended. `content_hash` was computed by the
    crawler, written to both `listings` and `extraction_audit`, and
    documented in `models.CrawlRecord` as the skip that avoids
    re-extraction -- and never read, so the nightly run paid for one model
    call per listing page per night whether or not a single byte had
    moved.

    The caller must still pass the returned `Listing` through to
    `apply_delta`: "we did not need to re-read it" is not "it is gone".
    """
    stored = storage.get_listing(conn, record.url)
    if stored is not None and stored.content_hash == record.content_hash:
        return stored
    return None


async def extract_listing(record: CrawlRecord, recipe: SiteRecipe, *, client) -> ExtractionResult:
    """Extract one `Listing` from `record` using `recipe` as a guide, via a
    single call through `client` (an `anthropic.AsyncAnthropic`-shaped
    object exposing `await client.messages.create(...)`).

    Never raises on bad model output and never returns a `Listing` that
    failed schema validation, failed the residential-sale check, or
    reported a price that could not be corroborated against the page
    (that field is dropped, not trusted) — see the module docstring for the
    guard layers: corroboration is scoped to where the recipe says the
    price lives, and a page with nothing to corroborate against is
    rejected before any call is paid for.
    """
    soup = BeautifulSoup(record.html, "html.parser")
    corroboration_text = _corroboration_text(soup, recipe)

    if not corroboration_text.strip():
        # Ruling R27: nothing on this page (no field-hint match, no
        # heading at all) to corroborate anything against. Reject before
        # spending a model call that could never be validated either way —
        # bentleyhurst.co.uk's real fixtures are exactly this: a
        # client-rendered Gatsby shell with zero headings and zero pound
        # signs in the HTML the crawler actually fetched.
        return _empty_result(
            "page has no heading or price-hint content to corroborate against; it is "
            "likely a client-rendered page with no listing data in the fetched HTML "
            "(or the recipe's field hints no longer match this page)",
            RejectionReason.NO_CORROBORATION_REGION,
        )

    # Deterministic pre-filter, before the one paid call. The status
    # heading is already in `corroboration_text`; if it says Sold STC or
    # To Let, `is_residential_sale` is going to reject this page in a
    # moment anyway, so there is nothing to buy by asking the model first.
    marker = non_sale_marker(corroboration_text)
    if marker is not None:
        return _empty_result(
            f"page is not an open residential sale listing: its status text carries "
            f"{marker!r}, so it was rejected before any model call was made",
            RejectionReason.NOT_RESIDENTIAL_SALE,
        )

    page_text = _page_markdown(record.html)
    prompt = _render_extract_prompt(record, recipe, page_text)

    try:
        response = await client.messages.create(
            model=EXTRACTION_MODEL,
            max_tokens=1024,
            tools=[_listing_tool()],
            tool_choice={"type": "tool", "name": _TOOL_NAME},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        # Wrapped into a Nimbus-level type, the same boundary Fetcher.get()
        # draws around backend-specific transport errors with FetchError
        # (fetch/fetcher.py): a caller catches ExtractionError without
        # importing anything `anthropic`-specific. Still propagates, by
        # design — this is a type change, not a swallow.
        raise ExtractionError(record.url, exc) from exc

    tool_blocks = [
        block for block in response.content if getattr(block, "type", None) == "tool_use"
    ]
    if not tool_blocks:
        return _empty_result(
            "model did not return structured tool output", RejectionReason.NO_TOOL_OUTPUT
        )

    payload = dict(tool_blocks[0].input)
    price_raw = str(payload.pop("price_raw", "") or "")
    price_gbp, price_qualifier = parse_price(price_raw)

    try:
        listing = Listing(
            source_domain=recipe.domain,
            source_url=record.url,
            external_id=payload.get("external_id"),
            address=payload.get("address"),
            postcode=payload.get("postcode"),
            price_gbp=price_gbp,
            price_qualifier=price_qualifier,
            bedrooms=payload.get("bedrooms"),
            bathrooms=payload.get("bathrooms"),
            property_type=payload.get("property_type") or PropertyType.OTHER,
            tenure=payload.get("tenure") or Tenure.UNKNOWN,
            floor_area_sqft=payload.get("floor_area_sqft"),
            description=payload.get("description"),
            agent_name=payload.get("agent_name"),
            listed_date=payload.get("listed_date"),
            content_hash=record.content_hash,
        )
    except ValidationError as exc:
        return _empty_result(
            f"model output failed Listing schema validation: {exc}",
            RejectionReason.SCHEMA_INVALID,
        )

    if not is_residential_sale(price_raw, listing):
        return _empty_result(
            f"page is not an open residential sale listing (price/status text {price_raw!r} "
            "carries a rental, letting, or sold marker)",
            RejectionReason.NOT_RESIDENTIAL_SALE,
        )

    issues: list[str] = []
    price_corroborated = True
    if price_gbp is not None:
        formatted = f"{price_gbp:,}"
        normalised_region = corroboration_text.replace(" ", " ")
        if formatted not in normalised_region:
            price_corroborated = False
            issues.append(
                f"model-reported price £{formatted} is not corroborated by the recipe's "
                "field-hint location or status heading; the price has been dropped rather "
                "than trusted (it may have come from unrelated page content, e.g. a "
                "price-filter dropdown)"
            )
            listing = listing.model_copy(update={"price_gbp": None, "price_qualifier": None})

    field_confidence = _field_confidence(listing)
    overall_confidence = 1.0 if price_corroborated else 0.6

    return ExtractionResult(
        listing=listing,
        field_confidence=field_confidence,
        overall_confidence=overall_confidence,
        needs_review=bool(issues),
        issues=issues,
    )


# --------------------------------------------------------------------------
# Audit trail: this module owns persistence of
# every extraction attempt, successful or not. Task 7 calls only
# `record_extraction`.
# --------------------------------------------------------------------------


def audit_row(
    record: CrawlRecord,
    recipe: SiteRecipe,
    result: ExtractionResult,
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
) -> dict[str, Any]:
    """The `extraction_audit` row for one extraction attempt. Always
    records `schema_valid` (did we end up with a validated `Listing`?) and
    `needs_review`, independent of whether the caller's `model`/token/cost
    bookkeeping is accurate — Task 6's health monitor reads both."""
    return {
        "url": record.url,
        "model": model,
        "prompt_version": EXTRACT_PROMPT_VERSION,
        "recipe_version": recipe.version,
        "html_hash": record.content_hash,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_usd": cost_usd,
        "schema_valid": result.listing is not None,
        "needs_review": result.needs_review,
        # Which rejection path was taken, or NULL when a Listing was
        # produced. `nimbus.heal.site_health` reads this to drop
        # deliberate exclusions from its denominator -- without it, a
        # site whose stock is mostly Sold STC reads as a collapsed recipe.
        "rejection_reason": (
            result.rejection_reason.value if result.rejection_reason is not None else None
        ),
    }


def record_extraction(
    conn: sqlite3.Connection,
    record: CrawlRecord,
    recipe: SiteRecipe,
    result: ExtractionResult,
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: float,
) -> None:
    """Compose `audit_row` and persist it via `storage.insert_audit`. The
    only way an extraction attempt reaches the `extraction_audit` table:
    callers use this and never `storage.insert_audit` directly, so every
    attempt is recorded with the same columns in the same shape."""
    row = audit_row(
        record,
        recipe,
        result,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
    )
    storage.insert_audit(conn, **row)
