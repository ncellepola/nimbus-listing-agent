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
from nimbus.models import CrawlOutcome
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
    # GONE_URL dropped out of every run from the first apply_delta call
    # onward, so it is reported withdrawn both times, not just once.
    assert r1.withdrawn == [GONE_URL]
    assert r2.withdrawn == [GONE_URL]
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
    assert should_allow_mass_withdrawal(limited) is False


def test_should_allow_mass_withdrawal_refuses_a_limit_that_covered_everything():
    """Carrying the untruncated count fixes the ratio, but a limit equal to
    (or above) the selected set still clears it. An ad-hoc, operator-driven
    partial run is never the thing that should wipe a dataset, however much
    of the site it happened to cover, so an applied limit is disqualifying
    in its own right."""
    assert (
        should_allow_mass_withdrawal(
            _outcome(selected=195, fetched=195, attempted=195, limit_applied=True)
        )
        is False
    )


def test_should_allow_mass_withdrawal_refuses_a_mostly_failed_crawl():
    """A crawl that selected 1075 URLs and fetched exactly 1 of them must
    not authorise a mass withdrawal just because `fetched > 0` -- that is
    the exact "failed crawl looks like an overnight sell-off" shape
    apply_delta's own guard exists to catch, re-opened at the integration
    layer if the caller derives the flag from "something worked" alone."""
    assert should_allow_mass_withdrawal(_outcome(selected=1075, fetched=1)) is False


def test_should_allow_mass_withdrawal_permits_a_complete_crawl():
    assert should_allow_mass_withdrawal(_outcome(selected=2, fetched=2)) is True


def test_should_allow_mass_withdrawal_permits_at_the_ratio_boundary():
    # 9/10 == the default 0.9 threshold exactly -- >=, not >.
    assert should_allow_mass_withdrawal(_outcome(selected=10, fetched=9)) is True


def test_should_allow_mass_withdrawal_refuses_just_under_the_boundary():
    assert should_allow_mass_withdrawal(_outcome(selected=10, fetched=8)) is False


def test_should_allow_mass_withdrawal_refuses_when_nothing_was_selected():
    # No candidates matched at all is itself a failure signal (recipe
    # drift), not evidence that it is safe to treat an empty run as a real
    # mass withdrawal.
    assert should_allow_mass_withdrawal(_outcome(selected=0, fetched=0)) is False


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
