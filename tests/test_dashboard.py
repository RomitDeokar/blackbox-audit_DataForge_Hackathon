"""Tests for reporting statistics and rendering.

Driven entirely by hand-written fixture JSONL, so nothing here depends on the
harness having run.
"""

from __future__ import annotations

import io
import json

import pytest
from click.testing import CliRunner
from rich.console import Console

from chaos_harness.trial_log import TrialRecord, write_trials
from reporting.dashboard import (
    banner,
    cli,
    evidence_table,
    percentile,
    render_summary,
    summarize,
    summarize_records,
    watch,
)


def _rec(trial_id: int, target: str, offset_ms: float, committed: bool, latency: float | None):
    """Build a record whose mismatch flag is derived, never hand-set.

    Ground truth in these fixtures: a booking should exist iff offset >= 0.
    """
    expected = offset_ms >= 0
    return TrialRecord(
        trial_id=trial_id,
        target=target,
        offset_ms=offset_ms,
        booking_id=trial_id,
        db_status_after="COMMITTED" if committed else "ROLLED_BACK",
        mismatch=committed is not expected,
        cancel_latency_ms=latency,
        expected_committed=expected,
        actually_committed=committed,
        gating_word="confirmed",
    )


@pytest.fixture
def fenced_log(tmp_path):
    """A clean fenced run: state always matches what was heard."""
    path = tmp_path / "trials_fenced.jsonl"
    records = [
        _rec(1, "fenced", -100, False, 1.0),
        _rec(2, "fenced", -50, False, 2.0),
        _rec(3, "fenced", 0, True, 3.0),
        _rec(4, "fenced", 50, True, 4.0),
        _rec(5, "fenced", 100, True, 100.0),
    ]
    write_trials(path, records)
    return path


@pytest.fixture
def naive_log(tmp_path):
    """A naive run: committed regardless, so the negative offsets mismatch."""
    path = tmp_path / "trials_naive.jsonl"
    records = [
        _rec(1, "naive", -100, True, 1.0),
        _rec(2, "naive", -50, True, 1.0),
        _rec(3, "naive", 0, True, 1.0),
        _rec(4, "naive", 50, True, 1.0),
        _rec(5, "naive", 100, True, 1.0),
    ]
    write_trials(path, records)
    return path


# --- percentiles ----------------------------------------------------------


def test_percentile_known_values():
    values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert percentile(values, 0) == 1
    assert percentile(values, 50) == pytest.approx(5.5)
    assert percentile(values, 95) == pytest.approx(9.55)
    assert percentile(values, 100) == 10


def test_percentile_interpolates_linearly():
    # rank = (3-1) * 0.5 = 1.0 -> exactly the middle element
    assert percentile([10, 20, 40], 50) == pytest.approx(20)
    # rank = 2 * 0.25 = 0.5 -> halfway between 10 and 20
    assert percentile([10, 20, 40], 25) == pytest.approx(15)


def test_percentile_single_and_empty():
    assert percentile([42], 95) == 42
    assert percentile([], 50) is None


def test_percentile_ignores_none_and_nan():
    assert percentile([1.0, None, float("nan"), 3.0], 50) == pytest.approx(2.0)


def test_percentile_is_order_independent():
    assert percentile([9, 1, 5, 3, 7], 50) == percentile([1, 3, 5, 7, 9], 50)


def test_percentile_rejects_out_of_range_q():
    with pytest.raises(ValueError):
        percentile([1, 2], 101)
    with pytest.raises(ValueError):
        percentile([1, 2], -1)


# --- summarize ------------------------------------------------------------


def test_summarize_computes_totals_and_percentiles(fenced_log):
    summary = summarize([fenced_log])
    fenced = summary["targets"]["fenced"]

    assert fenced["trials"] == 5
    assert fenced["mismatches"] == 0
    assert fenced["committed"] == 3
    assert fenced["passed"] is True
    # latencies: 1, 2, 3, 4, 100
    assert fenced["cancel_latency_ms"]["p50"] == pytest.approx(3.0)
    assert fenced["cancel_latency_ms"]["p95"] == pytest.approx(80.8)
    assert fenced["cancel_latency_ms"]["min"] == 1.0
    assert fenced["cancel_latency_ms"]["max"] == 100.0
    assert fenced["cancel_latency_ms"]["samples"] == 5


def test_summarize_counts_naive_mismatches(naive_log):
    naive = summarize([naive_log])["targets"]["naive"]

    assert naive["trials"] == 5
    assert naive["mismatches"] == 2
    assert naive["mismatch_rate"] == pytest.approx(0.4)
    assert naive["mismatch_offsets_ms"] == [-100.0, -50.0]
    assert naive["passed"] is False


def test_summarize_two_logs_keeps_targets_separate(fenced_log, naive_log):
    summary = summarize([naive_log, fenced_log])

    assert set(summary["targets"]) == {"fenced", "naive"}
    assert summary["totals"]["trials"] == 10
    assert summary["totals"]["mismatches"] == 2
    # The verdict tracks the fenced variant only: naive is the control group.
    assert summary["verdict"] == "PASS"


def test_verdict_fails_when_the_fenced_target_mismatches(tmp_path):
    path = tmp_path / "trials_fenced.jsonl"
    write_trials(path, [_rec(1, "fenced", -100, True, 1.0)])

    summary = summarize([path])
    assert summary["targets"]["fenced"]["mismatches"] == 1
    assert summary["verdict"] == "FAIL"


def test_verdict_when_no_fenced_data(naive_log):
    assert summarize([naive_log])["verdict"] == "NO_FENCED_DATA"


def test_summarize_groups_by_target_field_not_filename(tmp_path):
    """A combined log must still report per-target numbers correctly."""
    path = tmp_path / "everything.jsonl"
    write_trials(path, [_rec(1, "fenced", 0, True, 1.0), _rec(2, "naive", -10, True, 1.0)])

    summary = summarize([path])
    assert summary["targets"]["fenced"]["trials"] == 1
    assert summary["targets"]["naive"]["trials"] == 1


def test_summarize_missing_file_is_reported_not_fatal(tmp_path, fenced_log):
    summary = summarize([fenced_log, tmp_path / "absent.jsonl"])

    assert summary["targets"]["fenced"]["trials"] == 5
    missing = [f for f in summary["files"] if not f["exists"]]
    assert len(missing) == 1 and missing[0]["trials"] == 0


def test_summarize_requires_at_least_one_path():
    with pytest.raises(ValueError):
        summarize([])


def test_summarize_records_handles_empty_input():
    stats = summarize_records([])
    assert stats["trials"] == 0
    assert stats["passed"] is False
    assert stats["cancel_latency_ms"]["p50"] is None


def test_summarize_tolerates_missing_latencies(tmp_path):
    path = tmp_path / "trials_fenced.jsonl"
    write_trials(path, [_rec(1, "fenced", 0, True, None), _rec(2, "fenced", 0, True, 5.0)])

    latency = summarize([path])["targets"]["fenced"]["cancel_latency_ms"]
    assert latency["samples"] == 1
    assert latency["p50"] == 5.0


def test_summarize_counts_errors(tmp_path):
    path = tmp_path / "trials_fenced.jsonl"
    bad = _rec(1, "fenced", 0, True, 1.0)
    bad.error = "RuntimeError: boom"
    write_trials(path, [bad])

    assert summarize([path])["targets"]["fenced"]["errors"] == 1


def test_summary_json_is_serialisable(fenced_log, naive_log):
    summary = summarize([fenced_log, naive_log])
    assert json.loads(json.dumps(summary))["verdict"] == "PASS"


# --- rendering ------------------------------------------------------------


def _console() -> Console:
    """A Console that renders to an in-memory buffer (no leaked file handles)."""
    return Console(width=110, record=True, file=io.StringIO())


def _render(renderable) -> str:
    console = _console()
    console.print(renderable)
    return console.export_text()


def test_banner_is_green_pass_on_zero_fenced_mismatches(fenced_log):
    text = _render(banner(summarize([fenced_log])))
    assert "PASS" in text
    assert "0 state mismatches across 5 trials" in text


def test_banner_is_red_fail_with_the_count(tmp_path):
    path = tmp_path / "trials_fenced.jsonl"
    write_trials(path, [_rec(1, "fenced", -100, True, 1.0), _rec(2, "fenced", -50, True, 1.0)])

    text = _render(banner(summarize([path])))
    assert "FAIL" in text
    assert "2 state mismatch(es)" in text


def test_banner_when_no_fenced_data(naive_log):
    assert "no fenced-target trials" in _render(banner(summarize([naive_log])))


def test_render_summary_shows_both_targets(fenced_log, naive_log):
    console = _console()
    render_summary(summarize([fenced_log, naive_log]), console=console)
    text = console.export_text()

    assert "fenced" in text and "naive" in text
    assert "expected" in text  # naive mismatches are labelled, not flagged red


def test_evidence_table_is_markdown(fenced_log, naive_log):
    table = evidence_table(summarize([fenced_log, naive_log]))
    lines = table.splitlines()

    assert lines[0].startswith("| Agent Variant |")
    assert lines[1].startswith("| ---")
    assert len(lines) == 4
    assert "| fenced | 5 | 0 |" in table


# --- watch ----------------------------------------------------------------


def test_watch_reads_existing_rows_then_stops(fenced_log):
    records = watch(
        fenced_log,
        interval=0.01,
        max_seconds=0.05,
        console=_console(),
    )
    assert [r.trial_id for r in records] == [1, 2, 3, 4, 5]


def test_watch_on_a_missing_file_does_not_crash(tmp_path):
    records = watch(
        tmp_path / "not_yet.jsonl",
        interval=0.01,
        max_seconds=0.05,
        console=_console(),
    )
    assert records == []


# --- CLI ------------------------------------------------------------------


def test_cli_summarize_writes_summary_json(fenced_log, naive_log, tmp_path):
    result = CliRunner().invoke(cli, ["summarize", str(naive_log), str(fenced_log)])
    assert result.exit_code == 0, result.output
    assert "PASS" in result.output

    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["totals"]["trials"] == 10


def test_cli_summarize_json_mode(fenced_log, tmp_path):
    result = CliRunner().invoke(
        cli, ["summarize", str(fenced_log), "--json", "--out", str(tmp_path / "s.json")]
    )
    assert result.exit_code == 0
    assert json.loads(result.output)["verdict"] == "PASS"


def test_cli_summarize_exits_nonzero_on_fenced_failure(tmp_path):
    path = tmp_path / "trials_fenced.jsonl"
    write_trials(path, [_rec(1, "fenced", -100, True, 1.0)])

    result = CliRunner().invoke(cli, ["summarize", str(path)])
    assert result.exit_code == 1


def test_cli_evidence_prints_markdown(fenced_log, naive_log):
    result = CliRunner().invoke(cli, ["evidence", str(naive_log), str(fenced_log)])
    assert result.exit_code == 0
    assert "| Agent Variant |" in result.output


def test_cli_watch_with_a_time_bound(fenced_log):
    result = CliRunner().invoke(cli, ["watch", str(fenced_log), "--seconds", "0.05"])
    assert result.exit_code == 0
