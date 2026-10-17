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
"""

from __future__ import annotations

import sqlite3

from nimbus import storage
from nimbus.models import DeltaReport, Listing


def apply_delta(conn: sqlite3.Connection, domain: str, listings: list[Listing]) -> DeltaReport:
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

    report.withdrawn = [
        source_url for source_url in previously_stored if source_url not in seen_this_run
    ]

    return report
