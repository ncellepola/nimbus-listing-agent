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

from nimbus.heal import needs_heal
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
    typer.echo(
        f"{label}: schema_valid_rate={health.schema_valid_rate:.0%} "
        f"needs_review_rate={health.needs_review_rate:.0%} (sample={health.sample_size}) "
        f"-> needs_heal={needs_heal(health)}"
    )
