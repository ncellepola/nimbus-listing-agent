"""Tiny terminal-output helpers shared by `cli.py` and `demo.py`.

No business logic lives here -- only how a `DeltaReport`/`SiteHealth`/price
gets printed. Kept as its own module (rather than defined once in `cli.py`
and imported by `demo.py`, or vice versa) purely to avoid a circular import:
`cli.py`'s `demo` command needs `nimbus.demo`'s `run_offline_demo`/
`run_live_demo`, and `nimbus.demo` needs these formatting helpers, so
neither of those two modules can be where the helpers live.
"""

from __future__ import annotations

import typer

from nimbus.heal import needs_attention, needs_heal
from nimbus.models import DeltaReport, SiteHealth


def banner(text: str) -> None:
    rule = "=" * max(60, len(text) + 4)
    typer.echo(rule)
    typer.echo(text)
    typer.echo(rule)


def format_price(price_gbp: int | None) -> str:
    return f"£{price_gbp:,}" if price_gbp is not None else "—"


def print_delta(label: str, report: DeltaReport) -> None:
    typer.echo(
        f"{label}: new={len(report.new)} price_changed={len(report.price_changed)} "
        f"withdrawn={len(report.withdrawn)} unchanged={len(report.unchanged)}"
        + (" (withdrawal suppressed)" if report.withdrawal_suppressed else "")
    )


def print_health(label: str, health: SiteHealth) -> None:
    # Deliberate exclusions are shown only when there are any, and shown
    # rather than hidden: an operator reading "sample=6" against a site
    # with 18 crawled pages should be able to see where the other 12 went
    # (correctly-rejected Sold-STC / To-Let pages) without opening the
    # audit table.
    excluded = (
        f", {health.deliberate_exclusions} non-sale page(s) excluded"
        if health.deliberate_exclusions
        else ""
    )
    typer.echo(
        f"{label}: schema_valid_rate={health.schema_valid_rate:.0%} "
        f"needs_review_rate={health.needs_review_rate:.0%} "
        f"(sample={health.sample_size}{excluded}) "
        f"-> needs_heal={needs_heal(health)}"
    )
    if needs_attention(health):
        typer.echo(
            f"{label}: ATTENTION -- every one of this domain's "
            f"{health.deliberate_exclusions} audit row(s) was a non-sale exclusion, so "
            "there is nothing left to judge extraction health on. Either its stock has "
            "genuinely all gone, or the non-sale filter is rejecting live listings."
        )
