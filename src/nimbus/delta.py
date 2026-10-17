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
from nimbus.models import DeltaReport, Listing

logger = logging.getLogger(__name__)

# "More than half" is deliberate, not "all gone": it catches a partially
# failed crawl too, not just the fully empty one. A site does not usually
# sell off a majority of its stock in a single night.
_MASS_WITHDRAWAL_FRACTION = 0.5


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
      is not any more. Its stored row is left as-is (not deleted), so its
      history up to withdrawal is preserved.

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
        report.withdrawn = proposed_withdrawn

    return report
