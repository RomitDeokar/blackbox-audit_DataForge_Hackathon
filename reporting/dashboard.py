"""Reporting: live trial table, summary statistics, pass/fail banner.

    python -m reporting.dashboard watch results/trials_fenced.jsonl
    python -m reporting.dashboard summarize results/trials_naive.jsonl results/trials_fenced.jsonl
    python -m reporting.dashboard evidence          # markdown table for RIME_EVIDENCE.md

The only contract with the harness is the JSONL schema in
``chaos_harness.trial_log`` (trial_id, target, offset_ms, booking_id,
db_status_after, mismatch, cancel_latency_ms, timestamp). This module never
writes to that schema and never reaches into the harness' internals.
"""

from __future__ import annotations

import json
import math
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Sequence

import click
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from chaos_harness.trial_log import TrialRecord, iter_trials, write_json_atomic

__all__ = [
    "banner",
    "percentile",
    "render_summary",
    "summarize",
    "summarize_records",
    "watch",
]

MAX_LIVE_ROWS = 18


# --------------------------------------------------------------------------
# statistics
# --------------------------------------------------------------------------


def percentile(values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile, ``q`` in [0, 100].

    ``numpy`` is deliberately not a dependency -- a 606-row list does not
    justify it, and the interpolation rule is worth stating explicitly in a
    project whose headline numbers are percentiles.
    """
    if not values:
        return None
    if not 0.0 <= q <= 100.0:
        raise ValueError(f"percentile must be within [0, 100], got {q}")

    ordered = sorted(float(v) for v in values if v is not None and not math.isnan(float(v)))
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]

    rank = (len(ordered) - 1) * (q / 100.0)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[int(rank)]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def summarize_records(records: Iterable[TrialRecord]) -> dict[str, Any]:
    """Aggregate one target's trials into the reported statistics."""
    records = list(records)
    latencies = [r.cancel_latency_ms for r in records if r.cancel_latency_ms is not None]
    mismatched = [r for r in records if r.mismatch]

    statuses = Counter(r.db_status_after for r in records)
    outcomes = Counter(r.fence_outcome for r in records if r.fence_outcome)
    errors = [r for r in records if r.error]

    offsets = [r.offset_ms for r in records]
    committed = sum(1 for r in records if r.actually_committed)

    return {
        "target": records[0].target if records else None,
        "trials": len(records),
        "mismatches": len(mismatched),
        "mismatch_rate": (len(mismatched) / len(records)) if records else None,
        "passed": bool(records) and not mismatched,
        "committed": committed,
        "cancel_latency_ms": {
            "p50": percentile(latencies, 50),
            "p95": percentile(latencies, 95),
            "p99": percentile(latencies, 99),
            "min": min(latencies) if latencies else None,
            "max": max(latencies) if latencies else None,
            "mean": (sum(latencies) / len(latencies)) if latencies else None,
            "samples": len(latencies),
        },
        "offset_range_ms": (
            {"min": min(offsets), "max": max(offsets)} if offsets else {"min": None, "max": None}
        ),
        "db_status_counts": dict(sorted(statuses.items())),
        "fence_outcome_counts": dict(sorted(outcomes.items())),
        "errors": len(errors),
        "mismatch_offsets_ms": sorted({r.offset_ms for r in mismatched}),
        "modes": sorted({r.mode for r in records}),
    }


def summarize(paths: Sequence[str | Path]) -> dict[str, Any]:
    """Summarise one or more trial logs, keyed by target name.

    Rows are grouped by their ``target`` field rather than by filename, so a
    single combined log or an oddly named file still reports correctly.
    """
    if not paths:
        raise ValueError("summarize() needs at least one trial log path")

    by_target: dict[str, list[TrialRecord]] = {}
    files: list[dict[str, Any]] = []

    for path in paths:
        p = Path(path)
        records = list(iter_trials(p))
        files.append({"path": str(p), "exists": p.exists(), "trials": len(records)})
        for record in records:
            by_target.setdefault(record.target, []).append(record)

    targets = {name: summarize_records(rows) for name, rows in sorted(by_target.items())}

    total_trials = sum(t["trials"] for t in targets.values())
    total_mismatches = sum(t["mismatches"] for t in targets.values())
    fenced = targets.get("fenced")

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": files,
        "targets": targets,
        "totals": {"trials": total_trials, "mismatches": total_mismatches},
        # The acceptance criterion is specifically about the fenced variant:
        # the naive variant is *expected* to mismatch.
        "verdict": (
            "PASS"
            if fenced and fenced["trials"] > 0 and fenced["mismatches"] == 0
            else "FAIL"
            if fenced
            else "NO_FENCED_DATA"
        ),
    }


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def _fmt(value: float | None, digits: int = 3, suffix: str = "") -> str:
    if value is None:
        return "-"
    return f"{value:.{digits}f}{suffix}"


def banner(summary: dict[str, Any]) -> Panel:
    """Green PASS / red FAIL panel driven by the fenced target's mismatches."""
    verdict = summary.get("verdict")
    fenced = summary.get("targets", {}).get("fenced")

    if verdict == "PASS":
        body = Text.assemble(
            ("PASS", "bold white on green"),
            (
                f"  fenced agent: 0 state mismatches across {fenced['trials']} trials",
                "green",
            ),
        )
        return Panel(body, border_style="green", title="acceptance test")

    if verdict == "FAIL":
        body = Text.assemble(
            ("FAIL", "bold white on red"),
            (
                f"  fenced agent: {fenced['mismatches']} state mismatch(es) "
                f"across {fenced['trials']} trials",
                "red",
            ),
        )
        return Panel(body, border_style="red", title="acceptance test")

    return Panel(
        Text("no fenced-target trials found in the supplied logs", style="yellow"),
        border_style="yellow",
        title="acceptance test",
    )


def _summary_table(summary: dict[str, Any]) -> Table:
    table = Table(title="trial summary", title_style="bold", expand=False)
    table.add_column("target", style="bold")
    table.add_column("trials", justify="right")
    table.add_column("committed", justify="right")
    table.add_column("mismatches", justify="right")
    table.add_column("p50 (ms)", justify="right")
    table.add_column("p95 (ms)", justify="right")
    table.add_column("verdict")

    for name, stats in summary.get("targets", {}).items():
        mismatches = stats["mismatches"]
        # The naive variant is the control group: mismatches there are the
        # expected result, not a failure, so it is never rendered red.
        if name == "naive":
            verdict = Text(f"{mismatches} mismatch(es) — expected", style="yellow")
        elif mismatches == 0:
            verdict = Text("PASS", style="bold green")
        else:
            verdict = Text("FAIL", style="bold red")

        table.add_row(
            name,
            str(stats["trials"]),
            str(stats["committed"]),
            Text(str(mismatches), style="red" if mismatches and name != "naive" else None),
            _fmt(stats["cancel_latency_ms"]["p50"], 3),
            _fmt(stats["cancel_latency_ms"]["p95"], 3),
            verdict,
        )
    return table


def render_summary(summary: dict[str, Any], console: Console | None = None) -> None:
    """Print the banner plus the per-target statistics table."""
    console = console or Console()
    console.print()
    console.print(banner(summary))
    console.print(_summary_table(summary))

    for name, stats in summary.get("targets", {}).items():
        if stats["errors"]:
            console.print(f"[red]{name}: {stats['errors']} trial(s) errored[/]")
        if name == "fenced" and stats["mismatch_offsets_ms"]:
            console.print(
                f"[red]fenced mismatches at offsets (ms): "
                f"{stats['mismatch_offsets_ms']}[/]"
            )


def _live_table(records: Sequence[TrialRecord], path: Path) -> Group:
    total = len(records)
    mismatches = sum(1 for r in records if r.mismatch)

    table = Table(title=f"{path.name} — {total} trials", title_style="bold")
    table.add_column("trial", justify="right", style="dim")
    table.add_column("offset (ms)", justify="right")
    table.add_column("db status")
    table.add_column("mismatch")
    table.add_column("cancel latency (ms)", justify="right")

    for record in records[-MAX_LIVE_ROWS:]:
        table.add_row(
            str(record.trial_id),
            f"{record.offset_ms:+.0f}",
            record.db_status_after,
            Text("YES", style="bold red") if record.mismatch else Text("no", style="green"),
            _fmt(record.cancel_latency_ms, 3),
        )

    footer = Text.assemble(
        ("trials ", "dim"),
        (str(total), "bold"),
        ("   mismatches ", "dim"),
        (str(mismatches), "bold red" if mismatches else "bold green"),
    )
    return Group(table, footer)


def watch(
    path: str | Path,
    *,
    interval: float = 0.25,
    max_seconds: float | None = None,
    console: Console | None = None,
) -> list[TrialRecord]:
    """Tail a trial log and render it live.

    Args:
        path: JSONL trial log (may not exist yet -- it will be picked up).
        interval: Poll interval in seconds.
        max_seconds: Stop after this long. ``None`` runs until interrupted;
            the test suite always passes a bound.
    """
    log_path = Path(path)
    console = console or Console()
    started = time.monotonic()
    records: list[TrialRecord] = []

    with Live(_live_table(records, log_path), console=console, refresh_per_second=8) as live:
        while True:
            fresh = list(iter_trials(log_path))
            if len(fresh) != len(records):
                records = fresh
                live.update(_live_table(records, log_path))

            if max_seconds is not None and time.monotonic() - started >= max_seconds:
                break
            try:
                time.sleep(interval)
            except KeyboardInterrupt:  # pragma: no cover - interactive only
                break

    return records


def evidence_table(summary: dict[str, Any]) -> str:
    """Markdown results table for ``RIME_EVIDENCE.md``."""
    lines = [
        "| Agent Variant | Trials | State Mismatches | Cancel Latency p50 | Cancel Latency p95 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name, stats in summary.get("targets", {}).items():
        p50 = stats["cancel_latency_ms"]["p50"]
        p95 = stats["cancel_latency_ms"]["p95"]
        lines.append(
            f"| {name} | {stats['trials']} | {stats['mismatches']} | "
            f"{_fmt(p50, 3, ' ms')} | {_fmt(p95, 3, ' ms')} |"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option("1.0.0", prog_name="blackbox-audit reporting")
def cli() -> None:
    """Render and summarise chaos-harness trial logs."""


@cli.command("watch")
@click.argument("path", type=click.Path(dir_okay=False))
@click.option("--interval", type=float, default=0.25, show_default=True)
@click.option(
    "--seconds",
    type=float,
    default=None,
    help="Stop after N seconds (default: run until Ctrl-C).",
)
def watch_cmd(path: str, interval: float, seconds: float | None) -> None:
    """Tail a trial log and render trials as they are appended."""
    watch(path, interval=interval, max_seconds=seconds)


@cli.command("summarize")
@click.argument("paths", nargs=-1, required=True, type=click.Path(dir_okay=False))
@click.option(
    "--out",
    type=click.Path(dir_okay=False),
    default=None,
    help="Where to write summary.json (default: alongside the first log).",
)
@click.option("--json", "as_json", is_flag=True, help="Print raw JSON instead of tables.")
def summarize_cmd(paths: tuple[str, ...], out: str | None, as_json: bool) -> None:
    """Summarise one or more trial logs and write results/summary.json."""
    summary = summarize(list(paths))

    if as_json:
        click.echo(json.dumps(summary, indent=2))
    else:
        render_summary(summary)

    out_path = Path(out) if out else Path(paths[0]).parent / "summary.json"
    write_json_atomic(out_path, summary)
    if not as_json:
        click.echo(f"summary written to {out_path}")

    if summary["verdict"] == "FAIL":
        raise SystemExit(1)


@cli.command("evidence")
@click.argument("paths", nargs=-1, type=click.Path(dir_okay=False))
def evidence_cmd(paths: tuple[str, ...]) -> None:
    """Print the markdown results table for RIME_EVIDENCE.md."""
    logs = list(paths) or ["results/trials_naive.jsonl", "results/trials_fenced.jsonl"]
    click.echo(evidence_table(summarize(logs)))


def main(argv: list[str] | None = None) -> int:
    try:
        cli.main(args=argv, standalone_mode=False)
    except SystemExit as exc:  # pragma: no cover - passthrough
        return int(exc.code or 0)
    except click.ClickException as exc:
        exc.show()
        return exc.exit_code
    except click.Abort:  # pragma: no cover - interactive only
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
