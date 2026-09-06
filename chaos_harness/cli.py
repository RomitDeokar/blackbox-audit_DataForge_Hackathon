"""Chaos harness CLI.

    # the free, full acceptance sweep (no API calls, ~1s)
    python -m chaos_harness.cli run --target fenced
    python -m chaos_harness.cli run --target naive

    # both variants plus the summary, in one shot
    python -m chaos_harness.cli acceptance

    # pipeline smoke test, no fixture and no store
    python -m chaos_harness.cli run --target dummy --trials 5

    # inspect the timing ground truth
    python -m chaos_harness.cli timeline
    python -m chaos_harness.cli heard --at 2.1
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import click

from shared.constants import (
    CONFIRMATION_GATING_WORD,
    SWEEP_OFFSET_MAX_MS,
    SWEEP_OFFSET_MIN_MS,
    SWEEP_OFFSET_STEP_MS,
    SWEEP_RUNS_PER_OFFSET,
    VARIANT_DUMMY,
    VARIANT_FENCED,
    VARIANT_NAIVE,
)
from shared.rime_timestamps import load_timeline

from .driver import SweepConfig, SweepResult, load_target_timeline, run_sweep
from .trial_log import trial_log_path

TARGET_CHOICES = [VARIANT_NAIVE, VARIANT_FENCED, VARIANT_DUMMY]


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _offsets(minimum: float, maximum: float, step: float) -> list[float]:
    if step <= 0:
        raise click.BadParameter("--step-ms must be positive")
    if maximum < minimum:
        raise click.BadParameter("--max-ms must be >= --min-ms")
    out: list[float] = []
    value = minimum
    # Integer counter rather than repeated addition: floats drift, and a drifted
    # ladder would put trials at offsets the report claims are exact.
    index = 0
    while value <= maximum + 1e-9:
        out.append(round(value, 6))
        index += 1
        value = minimum + index * step
    return out


def _print_result(result: SweepResult, *, quiet: bool) -> None:
    if quiet:
        return
    style = {"fg": "green", "bold": True} if result.passed else {"fg": "red", "bold": True}
    click.secho(result.summary_line(), **style)
    click.echo(f"  log: {result.log_path}")


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option("1.0.0", prog_name="blackbox-audit harness")
def cli() -> None:
    """Voice-agent chaos harness and state-integrity fuzzer."""


@cli.command()
@click.option(
    "--target",
    type=click.Choice(TARGET_CHOICES),
    required=True,
    help="Which backend policy to audit.",
)
@click.option("--trials", type=int, default=None, help="Cap the trial count (default: full sweep).")
@click.option(
    "--runs-per-offset",
    type=int,
    default=SWEEP_RUNS_PER_OFFSET,
    show_default=True,
    help="Repeats per offset.",
)
@click.option("--min-ms", type=float, default=float(SWEEP_OFFSET_MIN_MS), show_default=True)
@click.option("--max-ms", type=float, default=float(SWEEP_OFFSET_MAX_MS), show_default=True)
@click.option("--step-ms", type=float, default=float(SWEEP_OFFSET_STEP_MS), show_default=True)
@click.option(
    "--gating-word",
    default=CONFIRMATION_GATING_WORD,
    show_default=True,
    help="Word whose completed playback authorises the commit.",
)
@click.option("--fixture", type=click.Path(exists=True, dir_okay=False), default=None)
@click.option("--results-dir", type=click.Path(file_okay=False), default=None)
@click.option("--log-path", type=click.Path(dir_okay=False), default=None)
@click.option(
    "--mode",
    type=click.Choice(["replay", "live"]),
    default="replay",
    show_default=True,
    help="replay = offline and free; live = real LiveKit room and paid APIs.",
)
@click.option(
    "--room",
    default=None,
    help="LiveKit room name (live mode only; ignored in replay mode).",
)
@click.option(
    "--reset-db",
    is_flag=True,
    help="Wipe the booking store before every trial (slower; default keeps history).",
)
@click.option("--progress/--no-progress", default=True, help="Show a progress bar.")
@click.option("--quiet", is_flag=True, help="Suppress the result banner.")
@click.option("--verbose", is_flag=True, help="Debug logging to stderr.")
def run(
    target: str,
    trials: int | None,
    runs_per_offset: int,
    min_ms: float,
    max_ms: float,
    step_ms: float,
    gating_word: str,
    fixture: str | None,
    results_dir: str | None,
    log_path: str | None,
    mode: str,
    room: str | None,
    reset_db: bool,
    progress: bool,
    quiet: bool,
    verbose: bool,
) -> None:
    """Run a trial sweep against one target."""
    _configure_logging(verbose)

    if mode == "live":
        if not room:
            raise click.UsageError("--room is required in live mode")
        if trials is None:
            raise click.UsageError(
                "live mode requires an explicit --trials cap: every trial spends real "
                "provider quota. See the API budget rule in README.md."
            )
        # Fail fast instead of crashing deep inside run_sweep: live mode is a
        # documented-but-unimplemented path (see docs/ACCEPTANCE_TEST.md), so
        # refuse here, before anything is built.
        raise click.ClickException(
            "live mode is not implemented in this build. Run the offline "
            "acceptance sweep (default replay mode), or implement the live "
            "driver described in docs/ACCEPTANCE_TEST.md against a real room "
            "with an explicit --trials cap."
        )

    config = SweepConfig(
        target=target,
        trials=trials,
        offsets_ms=_offsets(min_ms, max_ms, step_ms),
        runs_per_offset=runs_per_offset,
        gating_word=gating_word,
        fixture=fixture,
        results_dir=results_dir,
        log_path=log_path,
        mode=mode,
        reset_db_between_trials=reset_db,
    )

    total = config.planned_trials()
    if total == 0:
        raise click.UsageError("nothing to run: the sweep resolved to 0 trials")

    if progress and not quiet:
        with click.progressbar(length=total, label=f"{target} sweep") as bar:
            result = run_sweep(config, on_trial=lambda _r: bar.update(1))
    else:
        result = run_sweep(config)

    _print_result(result, quiet=quiet)
    if not result.passed and target == VARIANT_FENCED:
        # A fenced mismatch is a real defect, so make it fail the shell.
        raise SystemExit(1)


@cli.command()
@click.option("--results-dir", type=click.Path(file_okay=False), default=None)
@click.option(
    "--runs-per-offset",
    type=int,
    default=SWEEP_RUNS_PER_OFFSET,
    show_default=True,
)
@click.option("--verbose", is_flag=True)
def acceptance(results_dir: str | None, runs_per_offset: int, verbose: bool) -> None:
    """Run the full acceptance sweep for BOTH variants, then summarise.

    This is the whole falsifiable claim in one command, and it costs nothing:
    offsets -500..+500ms in 10ms steps x 3 runs x 2 variants = 606 trials,
    replayed offline through the real decision code.
    """
    _configure_logging(verbose)

    results: list[SweepResult] = []
    for target in (VARIANT_NAIVE, VARIANT_FENCED):
        config = SweepConfig(
            target=target,
            runs_per_offset=runs_per_offset,
            results_dir=results_dir,
        )
        with click.progressbar(
            length=config.planned_trials(), label=f"{target:>6} sweep"
        ) as bar:
            results.append(run_sweep(config, on_trial=lambda _r: bar.update(1)))

    click.echo()
    for result in results:
        _print_result(result, quiet=False)

    from reporting.dashboard import render_summary, summarize

    summary = summarize([r.log_path for r in results])
    render_summary(summary)

    out_dir = Path(results_dir) if results_dir else trial_log_path(VARIANT_FENCED).parent
    summary_path = out_dir / "summary.json"
    from .trial_log import write_json_atomic

    write_json_atomic(summary_path, summary)
    click.echo(f"\nsummary written to {summary_path}")

    fenced = next((r for r in results if r.target == VARIANT_FENCED), None)
    if fenced is not None and not fenced.passed:
        raise SystemExit(1)


@cli.command()
@click.option("--target", type=click.Choice(TARGET_CHOICES), default=VARIANT_FENCED)
@click.option("--fixture", type=click.Path(exists=True, dir_okay=False), default=None)
@click.option("--gating-word", default=CONFIRMATION_GATING_WORD, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of a table.")
def timeline(target: str, fixture: str | None, gating_word: str, as_json: bool) -> None:
    """Print the word-level timeline the sweep is keyed to."""
    tl = load_target_timeline(target, fixture)

    if as_json:
        click.echo(json.dumps(tl.to_dict(), indent=2))
        return

    from rich.console import Console
    from rich.table import Table

    table = Table(title=f"confirmation timeline ({target})", title_style="bold")
    table.add_column("#", justify="right", style="dim")
    table.add_column("word")
    table.add_column("start (s)", justify="right")
    table.add_column("end (s)", justify="right")
    table.add_column("", style="bold yellow")

    needle = gating_word.strip().lower().strip(",.!?'\"")
    for i, word in enumerate(tl.words, start=1):
        marker = "<- gating word" if word.normalized == needle else ""
        table.add_row(str(i), word.text, f"{word.start:.3f}", f"{word.end:.3f}", marker)

    console = Console()
    console.print(table)
    try:
        console.print(
            f"gating word {gating_word!r} finishes at "
            f"[bold]{tl.gating_time(gating_word):.3f}s[/]; audio runs to "
            f"[bold]{tl.duration:.3f}s[/]"
        )
    except KeyError as exc:
        console.print(f"[red]{exc}[/]")
        raise SystemExit(1) from None


@cli.command()
@click.option("--at", "at_s", type=float, required=True, help="Interruption time in seconds.")
@click.option("--fixture", type=click.Path(exists=True, dir_okay=False), default=None)
@click.option("--gating-word", default=CONFIRMATION_GATING_WORD, show_default=True)
def heard(at_s: float, fixture: str | None, gating_word: str) -> None:
    """Answer: at interruption time X, what had the caller actually heard?"""
    tl = load_timeline(fixture) if fixture else load_timeline()
    text = tl.heard_text_by(at_s)
    gating_end = tl.gating_time(gating_word)
    reached = at_s + 1e-9 >= gating_end

    click.echo(f"Interrupt at {at_s:.3f}s")
    click.echo(f"User heard: {text!r}")
    click.echo(f"Gating word {gating_word!r} ends at {gating_end:.3f}s")
    click.secho(
        f"Booking should exist: {reached}",
        fg="green" if reached else "yellow",
        bold=True,
    )


@cli.command()
@click.option("--results-dir", type=click.Path(file_okay=False), default=None)
@click.option(
    "--runs-per-offset",
    type=int,
    default=SWEEP_RUNS_PER_OFFSET,
    show_default=True,
)
@click.option(
    "--quick",
    is_flag=True,
    help="Small ladder (-50..+50 ms, 25 ms steps) for CI and quick demos.",
)
@click.option("--verbose", is_flag=True)
def verify(results_dir: str | None, runs_per_offset: int, quick: bool, verbose: bool) -> None:
    """Run the judge-ready audit: sweeps, summary, orphan check, evidence chart.

    Runs both agent variants, checks that no PENDING_AUDIO row survives a fenced
    sweep, writes ``summary.json`` and ``mismatch_by_offset.svg`` into the
    results directory, and prints the markdown evidence table. Exit code 1 if
    the fenced variant mismatches or leaks any row. Offline and free.
    """
    _configure_logging(verbose)

    if quick:
        offsets = _offsets(-50.0, 50.0, 25.0)
    else:
        offsets = _offsets(SWEEP_OFFSET_MIN_MS, SWEEP_OFFSET_MAX_MS, SWEEP_OFFSET_STEP_MS)

    results: list[SweepResult] = []
    for target in (VARIANT_NAIVE, VARIANT_FENCED):
        config = SweepConfig(
            target=target,
            offsets_ms=offsets,
            runs_per_offset=runs_per_offset,
            results_dir=results_dir,
        )
        with click.progressbar(
            length=config.planned_trials(), label=f"{target:>6} sweep"
        ) as bar:
            results.append(run_sweep(config, on_trial=lambda _r: bar.update(1)))

    click.echo()
    for result in results:
        _print_result(result, quiet=False)

    from reporting.dashboard import evidence_table, render_summary, summarize

    summary = summarize([r.log_path for r in results])
    render_summary(summary)

    out_dir = Path(results_dir) if results_dir else trial_log_path(VARIANT_FENCED).parent
    from .trial_log import write_json_atomic

    summary_path = out_dir / "summary.json"
    write_json_atomic(summary_path, summary)
    click.echo(f"summary written to {summary_path}")

    # Orphan audit: a fenced sweep must never leave an unresolved PENDING row.

    fenced = next(r for r in results if r.target == VARIANT_FENCED)
    orphans = fenced.orphans
    click.secho(
        f"orphan PENDING_AUDIO rows in fenced sweep: {orphans}",
        fg="green" if orphans == 0 else "red",
        bold=True,
    )

    # One-chart evidence for the pitch deck / demo screen.
    from reporting.chart import render_mismatch_chart

    all_records = [rec for r in results for rec in r.records]
    chart_path = out_dir / "mismatch_by_offset.svg"
    render_mismatch_chart(all_records, chart_path)
    click.echo(f"chart written to {chart_path}")

    click.echo("\nmarkdown evidence table:")
    click.echo(evidence_table(summary))

    if not fenced.passed or orphans:
        raise SystemExit(1)


def main(argv: list[str] | None = None) -> int:
    """Entry point that returns an exit code instead of raising SystemExit."""
    try:
        cli.main(args=argv, standalone_mode=False)
    except SystemExit as exc:  # pragma: no cover - passthrough
        return int(exc.code or 0)
    except click.ClickException as exc:
        exc.show()
        return exc.exit_code
    except click.Abort:  # pragma: no cover - interactive only
        click.echo("aborted", err=True)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
