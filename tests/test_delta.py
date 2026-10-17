"""The nightly delta: new / price-changed / withdrawn / unchanged,
compared by `source_url` within a domain. No test here touches the
network or calls an LLM.
"""

from __future__ import annotations

import sqlite3

from conftest import make_listing

from nimbus import storage
from nimbus.delta import (
    MASS_WITHDRAWAL_FETCH_RATIO,
    apply_delta,
    should_allow_mass_withdrawal,
)
from nimbus.models import CrawlOutcome, ExtractionResult, RejectionReason
from nimbus.storage import init_db

DOMAIN = "ashworthholme.co.uk"

CHANGED_URL = "https://www.ashworthholme.co.uk/property-details/1/-/sale/changed"
SAME_URL = "https://www.ashworthholme.co.uk/property-details/2/-/sale/same"
GONE_URL = "https://www.ashworthholme.co.uk/property-details/3/-/sale/gone"
NEW_URL = "https://www.ashworthholme.co.uk/property-details/4/-/sale/brand-new"


def history_rows(conn: sqlite3.Connection, source_url: str) -> list[tuple[int | None, int | None]]:
    rows = conn.execute(
        "SELECT old_price_gbp, new_price_gbp FROM listing_history WHERE source_url = ? ORDER BY id",
        (source_url,),
    ).fetchall()
    return [(row["old_price_gbp"], row["new_price_gbp"]) for row in rows]


def _seed_previous_run(conn: sqlite3.Connection) -> None:
    """Populate storage with "last night's" state via apply_delta itself —
    every one of these is `new` on this first call, which is exactly how
    a real first crawl of a domain behaves."""
    previous_listings = [
        make_listing(source_url=CHANGED_URL, price_gbp=450_000, content_hash="h1"),
        make_listing(source_url=SAME_URL, price_gbp=300_000, content_hash="h2"),
        make_listing(source_url=GONE_URL, price_gbp=200_000, content_hash="h3"),
    ]
    apply_delta(conn, DOMAIN, previous_listings)


def test_apply_delta_classifies_new_changed_withdrawn_unchanged(tmp_path):
    conn = init_db(tmp_path / "nimbus.db")
    _seed_previous_run(conn)

    changed = make_listing(source_url=CHANGED_URL, price_gbp=425_000, content_hash="h1b")
    same = make_listing(source_url=SAME_URL, price_gbp=300_000, content_hash="h2")
    brand_new = make_listing(source_url=NEW_URL, price_gbp=500_000, content_hash="h4")

    report = apply_delta(conn, DOMAIN, [changed, same, brand_new])

    assert report.new == [brand_new.source_url]
    assert report.price_changed == [(changed.source_url, 450_000, 425_000)]
    assert report.withdrawn == [GONE_URL]
    assert report.unchanged == [same.source_url]


def test_apply_delta_writes_price_history_row(tmp_path):
    conn = init_db(tmp_path / "nimbus.db")
    _seed_previous_run(conn)

    changed = make_listing(source_url=CHANGED_URL, price_gbp=425_000, content_hash="h1b")
    apply_delta(conn, DOMAIN, [changed])

    assert history_rows(conn, changed.source_url) == [(450_000, 425_000)]


def test_apply_delta_is_idempotent(tmp_path):
    conn = init_db(tmp_path / "nimbus.db")
    _seed_previous_run(conn)

    listings = [
        make_listing(source_url=CHANGED_URL, price_gbp=425_000, content_hash="h1b"),
        make_listing(source_url=SAME_URL, price_gbp=300_000, content_hash="h2"),
        make_listing(source_url=NEW_URL, price_gbp=500_000, content_hash="h4"),
    ]

    r1 = apply_delta(conn, DOMAIN, listings)
    r2 = apply_delta(conn, DOMAIN, listings)

    assert r1.new == [NEW_URL]
    assert r2.new == []
    assert set(r2.unchanged) == {listing.source_url for listing in listings}
    assert r2.price_changed == []
    # GONE_URL dropped out on the first call and is reported withdrawn
    # exactly once. The second call does not re-report it: the first one
    # stamped `withdrawn_at`, which is what stops an orchestrator being
    # told the same listing vanished again every night for a year.
    assert r1.withdrawn == [GONE_URL]
    assert r2.withdrawn == []
    # A second application with the same prices must not duplicate the
    # price_changed history row the first application already wrote.
    assert history_rows(conn, CHANGED_URL) == [(450_000, 425_000)]


def test_apply_delta_scopes_withdrawn_to_the_given_domain(tmp_path):
    conn = init_db(tmp_path / "nimbus.db")
    other_domain_url = "https://www.jaksandco.co.uk/property/other"
    apply_delta(
        conn,
        "jaksandco.co.uk",
        [make_listing(source_domain="jaksandco.co.uk", source_url=other_domain_url, price_gbp=1)],
    )

    report = apply_delta(conn, DOMAIN, [])

    assert report.withdrawn == []


def _seed_five_listings(conn: sqlite3.Connection) -> list[str]:
    urls = [f"https://www.ashworthholme.co.uk/property-details/{n}" for n in range(5)]
    listings = [
        make_listing(source_url=url, price_gbp=100_000 + n, content_hash=f"hash{n}")
        for n, url in enumerate(urls)
    ]
    apply_delta(conn, DOMAIN, listings)
    return urls


def test_apply_delta_suppresses_mass_withdrawal_by_default(tmp_path, caplog):
    conn = init_db(tmp_path / "nimbus.db")
    urls = _seed_five_listings(conn)

    with caplog.at_level("ERROR"):
        report = apply_delta(conn, DOMAIN, [])

    assert report.withdrawn == []
    assert report.withdrawal_suppressed is True
    # Nothing was wiped: every previously stored listing is still there.
    stored_urls = {listing.source_url for listing in storage.list_listings(conn, DOMAIN)}
    assert stored_urls == set(urls)
    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert any(DOMAIN in r.message and "5" in r.message for r in error_records)


def test_apply_delta_marks_a_minority_withdrawn_normally(tmp_path, caplog):
    conn = init_db(tmp_path / "nimbus.db")
    urls = _seed_five_listings(conn)
    remaining = urls[1:]  # drop just one of five -- 20%, not a mass withdrawal

    listings = [
        make_listing(source_url=url, price_gbp=100_000, content_hash="h") for url in remaining
    ]

    with caplog.at_level("ERROR"):
        report = apply_delta(conn, DOMAIN, listings)

    assert report.withdrawn == [urls[0]]
    assert report.withdrawal_suppressed is False
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


def test_apply_delta_allow_mass_withdrawal_overrides_the_guard(tmp_path):
    conn = init_db(tmp_path / "nimbus.db")
    urls = _seed_five_listings(conn)

    report = apply_delta(conn, DOMAIN, [], allow_mass_withdrawal=True)

    assert set(report.withdrawn) == set(urls)
    assert report.withdrawal_suppressed is False


def _outcome(
    *, selected: int, fetched: int, attempted: int | None = None, limit_applied: bool = False
) -> CrawlOutcome:
    attempted = selected if attempted is None else attempted
    return CrawlOutcome(
        domain=DOMAIN,
        records=[],
        candidates=selected,
        selected=selected,
        attempted=attempted,
        fetched=fetched,
        skipped_robots=0,
        skipped_errors=attempted - fetched,
        limit_applied=limit_applied,
    )


def test_should_allow_mass_withdrawal_refuses_a_limited_run():
    """Measured against this repo's own fixtures: `--limit 5` on a site
    whose recipe implies 195 listing URLs used to report `selected=5,
    fetched=5` -- a ratio of exactly 1.0 -- and so authorised withdrawing
    the other 190. The CLI advertises `--limit` and the architecture doc
    tells operators to use it, so this was reachable by following the
    documentation."""
    limited = _outcome(selected=195, fetched=5, attempted=5, limit_applied=True)

    # The truncated view -- what the outcome used to report -- clears the
    # ratio trivially, which is exactly why the ratio alone is not enough.
    assert limited.fetched >= limited.attempted * MASS_WITHDRAWAL_FETCH_RATIO
    assert should_allow_mass_withdrawal(limited, _extracted(5)) is False


def test_a_limit_that_did_not_truncate_anything_is_not_disqualifying():
    """`crawl_site` sets `limit_applied` only when the limit actually bit,
    so `--limit 50` against a one-listing site is the complete run it
    looks like. The guard must not treat the mere presence of a `--limit`
    flag as disqualifying -- an earlier version of this test asserted on
    a `limit_applied=True, attempted == selected` outcome that
    `crawl_site` cannot produce."""
    assert (
        should_allow_mass_withdrawal(
            _outcome(selected=1, fetched=1, attempted=1, limit_applied=False), _extracted(1)
        )
        is True
    )


def test_should_allow_mass_withdrawal_refuses_a_mostly_failed_crawl():
    """A crawl that selected 1075 URLs and fetched exactly 1 of them must
    not authorise a mass withdrawal just because `fetched > 0` -- that is
    the exact "failed crawl looks like an overnight sell-off" shape
    apply_delta's own guard exists to catch, re-opened at the integration
    layer if the caller derives the flag from "something worked" alone."""
    assert should_allow_mass_withdrawal(_outcome(selected=1075, fetched=1), _extracted(1)) is False


def test_should_allow_mass_withdrawal_permits_a_complete_crawl():
    assert should_allow_mass_withdrawal(_outcome(selected=2, fetched=2), _extracted(2)) is True


def test_should_allow_mass_withdrawal_permits_at_the_ratio_boundary():
    # 9/10 == the default 0.9 threshold exactly -- >=, not >.
    assert should_allow_mass_withdrawal(_outcome(selected=10, fetched=9), _extracted(9)) is True


def test_should_allow_mass_withdrawal_refuses_just_under_the_boundary():
    assert should_allow_mass_withdrawal(_outcome(selected=10, fetched=8), _extracted(8)) is False


def test_should_allow_mass_withdrawal_refuses_when_nothing_was_selected():
    # No candidates matched at all is itself a failure signal (recipe
    # drift), not evidence that it is safe to treat an empty run as a real
    # mass withdrawal.
    assert should_allow_mass_withdrawal(_outcome(selected=0, fetched=0), []) is False


def test_apply_delta_logs_when_mass_withdrawal_override_actually_proceeds(tmp_path, caplog):
    """An operator-approved mass withdrawal must still leave an audit
    trace -- a system whose selling point includes an audit trail cannot
    let the one most consequential write (wiping most of a domain's
    inventory) happen silently just because the caller opted in."""
    conn = init_db(tmp_path / "nimbus.db")
    _seed_five_listings(conn)

    with caplog.at_level("WARNING"):
        report = apply_delta(conn, DOMAIN, [], allow_mass_withdrawal=True)

    assert len(report.withdrawn) == 5
    assert report.withdrawal_suppressed is False
    relevant = [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]
    assert any(DOMAIN in r.message and "5" in r.message for r in relevant)


# --------------------------------------------------------------------------
# The guard measured fetch completeness, but withdrawal is driven by
# *extraction*. A site redesign leaves URLs intact: the crawler fetches
# 195/195, scores a ratio of 1.0, and is authorised -- then every
# extraction fails, `listings == []`, and the entire domain is withdrawn.
# The guard did not fire in the one scenario the heal subsystem exists for.
# --------------------------------------------------------------------------


def _extracted(n: int) -> list[ExtractionResult]:
    return [
        ExtractionResult(listing=make_listing(source_url=f"{SAME_URL}/{i}"), overall_confidence=1.0)
        for i in range(n)
    ]


def _failed(n: int, reason: RejectionReason) -> list[ExtractionResult]:
    return [
        ExtractionResult(
            listing=None, overall_confidence=0.0, needs_review=True, rejection_reason=reason
        )
        for _ in range(n)
    ]


def test_a_complete_crawl_with_zero_extractions_cannot_authorise_withdrawal():
    """The redesign shape, composed from two of this repo's own passing
    behaviours: every URL still resolves, so the fetch ratio is a perfect
    1.0, and every extraction then fails against markup the recipe no
    longer matches."""
    outcome = _outcome(selected=195, fetched=195)

    assert outcome.fetched >= outcome.selected * MASS_WITHDRAWAL_FETCH_RATIO
    assert (
        should_allow_mass_withdrawal(outcome, _failed(195, RejectionReason.NO_CORROBORATION_REGION))
        is False
    )


def test_a_complete_crawl_whose_extractions_worked_still_authorises_withdrawal():
    outcome = _outcome(selected=10, fetched=10)

    assert should_allow_mass_withdrawal(outcome, _extracted(10)) is True


def test_deliberate_non_sale_exclusions_do_not_count_against_the_extraction_yield():
    """A site whose stock is mostly Sold STC is not a broken site. Those
    pages are correctly excluded, so they leave the denominator entirely
    -- the same reasoning site_health uses, for the same reason."""
    outcome = _outcome(selected=18, fetched=18)
    results = _extracted(6) + _failed(12, RejectionReason.NOT_RESIDENTIAL_SALE)

    assert should_allow_mass_withdrawal(outcome, results) is True


def test_a_crawl_whose_extraction_never_ran_cannot_authorise_withdrawal():
    """Pages were fetched but no extraction result came back at all (every
    call raised, or the step was skipped). An empty `listings` list from
    "extraction never happened" must never read as "nothing is live"."""
    assert should_allow_mass_withdrawal(_outcome(selected=10, fetched=10), []) is False


# --------------------------------------------------------------------------
# Withdrawal is persisted. It was reported and then forgotten, so the same
# listings were re-reported withdrawn every night, and -- worse -- they
# stayed in the denominator of the mass-withdrawal fraction, which meant a
# domain whose cumulative withdrawals passed half its rows latched the
# guard permanently shut.
# --------------------------------------------------------------------------


def test_a_withdrawal_is_reported_once_and_then_stops(tmp_path):
    conn = init_db(tmp_path / "nimbus.db")
    _seed_previous_run(conn)
    survivors = [
        make_listing(source_url=CHANGED_URL, price_gbp=450_000, content_hash="h1"),
        make_listing(source_url=SAME_URL, price_gbp=300_000, content_hash="h2"),
    ]

    first = apply_delta(conn, DOMAIN, survivors)
    second = apply_delta(conn, DOMAIN, survivors)

    assert first.withdrawn == [GONE_URL]
    assert second.withdrawn == []
    gone = storage.get_listing(conn, GONE_URL)
    assert gone is not None, "a withdrawn row is stamped, never deleted"
    assert gone.withdrawn_at is not None
    # And its price history survives the withdrawal.
    assert storage.list_listings(conn, DOMAIN, include_withdrawn=True)


def test_a_relisted_url_clears_its_withdrawal(tmp_path):
    conn = init_db(tmp_path / "nimbus.db")
    _seed_previous_run(conn)
    survivors = [
        make_listing(source_url=CHANGED_URL, price_gbp=450_000, content_hash="h1"),
        make_listing(source_url=SAME_URL, price_gbp=300_000, content_hash="h2"),
    ]
    apply_delta(conn, DOMAIN, survivors)
    assert storage.get_listing(conn, GONE_URL).withdrawn_at is not None

    relisted = make_listing(source_url=GONE_URL, price_gbp=195_000, content_hash="h3b")
    report = apply_delta(conn, DOMAIN, [*survivors, relisted])

    assert storage.get_listing(conn, GONE_URL).withdrawn_at is None
    assert GONE_URL not in report.withdrawn


def test_the_guard_does_not_latch_shut_once_half_a_domain_has_been_withdrawn(tmp_path):
    """Cumulative withdrawals used to stay in the denominator forever, so
    after enough of a domain's history had gone, every subsequent run
    looked like a mass withdrawal and nothing could ever be withdrawn
    again."""
    conn = init_db(tmp_path / "nimbus.db")
    urls = _seed_five_listings(conn)

    # Night one: three of five go, with the override granted.
    apply_delta(
        conn,
        DOMAIN,
        [make_listing(source_url=url, price_gbp=100_000, content_hash="h") for url in urls[3:]],
        allow_mass_withdrawal=True,
    )

    # Night two: one of the two survivors goes. That is 1 of 2 live
    # listings, not 4 of 5 stored ones -- a minority withdrawal, which
    # must go through without needing an override.
    report = apply_delta(
        conn,
        DOMAIN,
        [make_listing(source_url=urls[4], price_gbp=100_000, content_hash="h")],
    )

    assert report.withdrawal_suppressed is False
    assert report.withdrawn == [urls[3]]


# --------------------------------------------------------------------------
# B1: a dropped result must shrink the numerator, never the denominator.
# `cli.crawl` caught ExtractionError and `continue`d without appending, so
# an Anthropic 429/5xx storm or a revoked key made the guard's denominator
# collapse along with its numerator -- 99 of 100 calls raising left one
# success over a denominator of one, a perfect score, authorising the
# withdrawal of the whole domain. The same shape the fix was about, one
# layer up, and reachable by likelier means than a markup redesign.
# --------------------------------------------------------------------------


def test_results_dropped_by_the_caller_do_not_shrink_the_denominator():
    outcome = _outcome(selected=100, fetched=100)

    # What a caller that swallowed 99 ExtractionErrors hands over.
    assert should_allow_mass_withdrawal(outcome, _extracted(1)) is False


def test_extraction_errors_reported_as_results_also_refuse():
    """The other half: a caller that does report them correctly."""
    outcome = _outcome(selected=100, fetched=100)
    results = _extracted(1) + _failed(99, RejectionReason.EXTRACTION_ERROR)

    assert should_allow_mass_withdrawal(outcome, results) is False


def test_a_handful_of_extraction_errors_does_not_block_a_real_withdrawal():
    """The denominator is `fetched`, so the threshold stays reachable: 8
    of 10 pages extracting cleanly is still a trustworthy run."""
    outcome = _outcome(selected=10, fetched=10)

    assert should_allow_mass_withdrawal(outcome, _extracted(8)) is True
