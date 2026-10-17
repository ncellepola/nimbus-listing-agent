"""The nightly delta: new / price-changed / withdrawn / unchanged.

A nightly crawl on its own is just a dump of whatever is live right now.
What turns it into intelligence is comparing it against what storage
already knew for the domain, by `source_url` (never by address text or
any other fuzzy key — the same URL is the one thing a site keeps stable
across a redesign, a price change, or a relist). `apply_delta` is this
comparison: it classifies every incoming `Listing` against the previous
state, persists the new state (`nimbus.storage.upsert_listing`), writes
a `listing_history` row for every price change, and returns a
`DeltaReport` an orchestration layer (Task 7) can act on directly without
re-querying storage to reconstruct what happened.

`apply_delta` is idempotent: calling it twice with the same `listings`
list produces the same stored state and reports nothing as `new` or
`price_changed` the second time, because the first call already brought
storage in line with that list.

A destructive-by-default footgun this module refuses to have: a crawl
that failed outright (every fetch errored, a redirect loop, a block) or
partially failed still produces a `CrawlOutcome` -- it is just empty or
thin. Passing that straight to `apply_delta` must not read as "this
domain's entire inventory sold overnight." Mass withdrawal is therefore
guarded in code (`allow_mass_withdrawal`), not merely documented as a
precondition the caller has to remember -- the same reasoning ADR-0002's
robots gate lives in `fetch_page` rather than a prompt: policy that
matters belongs where it cannot be skipped.
"""

from __future__ import annotations

import logging
import sqlite3

from nimbus import storage
from nimbus.models import (
    DELIBERATE_EXCLUSIONS,
    CrawlOutcome,
    DeltaReport,
    ExtractionResult,
    Listing,
)

logger = logging.getLogger(__name__)

# "More than half" is deliberate, not "all gone": it catches a partially
# failed crawl too, not just the fully empty one. A site does not usually
# sell off a majority of its stock in a single night.
_MASS_WITHDRAWAL_FRACTION = 0.5

# How much of a crawl's own *selected* URL set must actually have been
# fetched before its outcome is trusted to authorise a mass withdrawal.
# Deliberately not "outcome.fetched > 0": a crawl that selected 1075 URLs
# and fetched exactly one of them satisfies "fetched > 0" trivially, and
# that one success is not evidence the other 1074 are gone -- it is
# evidence the crawl mostly failed, which is precisely the shape
# _MASS_WITHDRAWAL_FRACTION above exists to catch. Callers at the
# orchestration layer (nimbus.cli, nimbus.demo) must derive
# `apply_delta`'s `allow_mass_withdrawal` from `should_allow_mass_withdrawal`,
# not from any bare truthiness check on `outcome.fetched`.
MASS_WITHDRAWAL_FETCH_RATIO = 0.9

# How much of what the run could *judge* must actually have yielded a
# Listing. Fetching is not the signal that drives withdrawal -- extraction
# is: `apply_delta` compares stored URLs against the `listings` it is
# handed, and a site redesign leaves every URL resolving perfectly while
# breaking every extraction. That run scores a fetch ratio of exactly 1.0
# and would, on the fetch ratio alone, be authorised to withdraw the whole
# domain. It is the one scenario the heal subsystem exists for, and the
# guard did not fire in it.
#
# 0.5 rather than the fetch path's 0.9 because the two failure modes are
# shaped differently. A fetch either worked or did not; extraction has a
# legitimate tail -- a land or commercial page with no parseable heading,
# a listing whose markup is genuinely unusual -- that does not mean the
# recipe is broken. What this must catch is the collapse towards zero, not
# a few percent of noise. Deliberate exclusions (Sold STC, To Let) leave
# the denominator entirely, for the same reason `site_health` drops them:
# they are correct outcomes, and on this repo's own ashworthholme.co.uk
# fixtures they are two thirds of every page crawled.
MASS_WITHDRAWAL_EXTRACTION_RATIO = 0.5


def should_allow_mass_withdrawal(
    outcome: CrawlOutcome,
    results: list[ExtractionResult],
    *,
    min_fetch_ratio: float = MASS_WITHDRAWAL_FETCH_RATIO,
    min_extraction_ratio: float = MASS_WITHDRAWAL_EXTRACTION_RATIO,
) -> bool:
    """Whether this run is trustworthy enough to authorise withdrawing
    more than half of a domain's stored listings.

    `results` is every `ExtractionResult` the run produced, in any order,
    and is required rather than optional on purpose: the caller cannot
    forget to supply the signal that actually drives withdrawal.

    Four ways to fail, each a distinct real shape:

    - an explicit `--limit` was applied. A limited run deliberately looks
      at a slice of the site; the URLs it never opened are not absent,
      they are unexamined. Disqualifying in its own right, because a
      `--limit` equal to the selected set would otherwise clear the ratio.
    - nothing was selected at all (`outcome.selected == 0`): no candidate
      matched the recipe's patterns, which is recipe drift, not a quiet
      night.
    - too little of what the recipe implied was actually fetched. A crawl
      that selected 1075 URLs and fetched one of them is evidence the
      crawl failed, not that 1074 listings sold overnight.
    - too little of what *was* fetched survived extraction. This is the
      redesign case: URLs intact, markup changed, every extraction
      failing. See `MASS_WITHDRAWAL_EXTRACTION_RATIO` above.
    """
    if outcome.limit_applied:
        return False
    if outcome.selected == 0:
        return False
    if outcome.fetched < outcome.selected * min_fetch_ratio:
        return False

    if outcome.fetched > 0 and not results:
        # Pages were fetched but extraction produced nothing at all --
        # every call raised, or the step never ran. An empty `listings`
        # list caused by "extraction did not happen" must never be read
        # as "nothing is live any more".
        return False

    judgeable = [r for r in results if r.rejection_reason not in DELIBERATE_EXCLUSIONS]
    if not judgeable:
        # Everything this run saw was a correct non-sale exclusion. That
        # says the recipe is fine, but it is no evidence about the stored
        # listings either, so it does not authorise removing them.
        return False

    extracted = sum(1 for r in judgeable if r.listing is not None)
    return extracted >= len(judgeable) * min_extraction_ratio


def apply_delta(
    conn: sqlite3.Connection,
    domain: str,
    listings: list[Listing],
    *,
    allow_mass_withdrawal: bool = False,
) -> DeltaReport:
    """Classify `listings` (this run's crawl output for `domain`) against
    what `storage` already has on file, persisting the new state as it
    goes.

    - A `source_url` with no prior stored row is `new`.
    - A `source_url` seen before whose stored `price_gbp` differs from
      this run's is `price_changed`; a `listing_history` row records the
      old and new price.
    - A `source_url` seen before with the same price is `unchanged`.
    - A previously stored `source_url` for `domain` that is absent from
      `listings` entirely is `withdrawn` — the listing used to be live and
      is not any more. Its row is stamped with `withdrawn_at` rather than
      deleted, so its history up to withdrawal is preserved, it is not
      re-reported as newly withdrawn every subsequent night, and it drops
      out of the denominator of the mass-withdrawal fraction (left in, a
      domain whose cumulative withdrawals passed half its rows would latch
      the guard permanently shut). A URL that reappears in a later run has
      the stamp cleared by `storage.upsert_listing`.

    If the listings that would be withdrawn are more than half of what
    was previously stored for `domain` (and at least one listing was
    stored), the withdrawal is refused unless `allow_mass_withdrawal` is
    `True`: `report.withdrawn` is empty, `report.withdrawal_suppressed` is
    `True`, those listings are left completely untouched, and an ERROR is
    logged naming `domain` and the counts. New/changed/unchanged
    classification for the listings actually passed in still happens
    normally either way — only the withdrawn side of a failed or partial
    crawl is destructive, and that is what this guards.

    Defaults closed: a caller that forgets `allow_mass_withdrawal`
    entirely gets the safe behaviour. A suppressed withdrawal is
    recoverable on the next good run; a wiped dataset is not.
    """
    previously_stored = {
        listing.source_url: listing for listing in storage.list_listings(conn, domain)
    }

    report = DeltaReport()
    seen_this_run: set[str] = set()

    for listing in listings:
        seen_this_run.add(listing.source_url)
        existing = previously_stored.get(listing.source_url)

        if existing is None:
            report.new.append(listing.source_url)
        elif existing.price_gbp != listing.price_gbp:
            storage.insert_price_history(
                conn, listing.source_url, existing.price_gbp, listing.price_gbp
            )
            # DeltaReport.price_changed is typed tuple[str, int, int] -- a
            # price appearing/disappearing (None on one side) still gets a
            # true listing_history row above, but has no int pair to
            # report here, so it is deliberately left out of all four
            # buckets rather than forced into one that would misdescribe it.
            if existing.price_gbp is not None and listing.price_gbp is not None:
                report.price_changed.append(
                    (listing.source_url, existing.price_gbp, listing.price_gbp)
                )
        else:
            report.unchanged.append(listing.source_url)

        storage.upsert_listing(conn, listing)

    proposed_withdrawn = [
        source_url for source_url in previously_stored if source_url not in seen_this_run
    ]

    stored_count = len(previously_stored)
    is_mass_withdrawal = (
        stored_count >= 1 and len(proposed_withdrawn) > stored_count * _MASS_WITHDRAWAL_FRACTION
    )

    if is_mass_withdrawal and not allow_mass_withdrawal:
        logger.error(
            "apply_delta(%s): refusing to withdraw %d/%d previously stored listing(s) in one "
            "run -- more than half, which almost always means a failed or partial crawl rather "
            "than a real sell-off; leaving them untouched. Pass allow_mass_withdrawal=True to "
            "override if this is genuinely expected.",
            domain,
            len(proposed_withdrawn),
            stored_count,
        )
        report.withdrawal_suppressed = True
    else:
        if is_mass_withdrawal:
            # The override was granted and a mass withdrawal is about to
            # actually happen -- the one write in this module consequential
            # enough that it must never go unlogged just because the caller
            # opted in. Separate from the ERROR branch above: that one is a
            # refusal, this one is a deliberate, approved action, logged so
            # it leaves the same audit trail an operator would expect from a
            # system whose selling point includes one.
            logger.warning(
                "apply_delta(%s): withdrawing %d/%d previously stored listing(s) in one run -- "
                "more than half, proceeding because allow_mass_withdrawal=True was passed "
                "explicitly. This should be a deliberate, reviewed decision, not a routine one.",
                domain,
                len(proposed_withdrawn),
                stored_count,
            )
        storage.mark_withdrawn(conn, proposed_withdrawn)
        report.withdrawn = proposed_withdrawn

    return report
