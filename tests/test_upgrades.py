"""Tests for the hardening pass: sentence unification, orphan audit, chart, verify.

Everything runs in replay mode offline: no external API calls.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from chaos_harness.cli import cli
from chaos_harness.driver import SweepConfig, run_sweep
from chaos_harness.trial_log import TrialRecord
from reporting.chart import mismatch_by_offset, render_mismatch_chart
from shared import booking_store as bs
from shared.constants import (
    CONFIRMATION_PHRASE,
    TEST_BOOKING_TIME,
    TEST_PARTY_SIZE,
    confirmation_sentence,
)
from shared.rime_timestamps import DEFAULT_TIMELINE_FIXTURE, load_timeline

# --- sentence unification -------------------------------------------------


def test_confirmation_phrase_is_the_template_rendering():
    """The docstring contract: both agent variants speak the SAME sentence."""
    assert confirmation_sentence(TEST_PARTY_SIZE, TEST_BOOKING_TIME) == CONFIRMATION_PHRASE


def test_fixture_sentence_is_canonical_and_words_stay_in_sync():
    raw = json.loads(DEFAULT_TIMELINE_FIXTURE.read_text(encoding="utf-8"))
    assert raw["_sentence"] == confirmation_sentence(TEST_PARTY_SIZE, TEST_BOOKING_TIME)
    words = raw["word_timestamps"]["words"]
    assert words[words.index("table") + 1] == "of"
    tl = load_timeline()
    assert tl.gating_time("confirmed") == pytest.approx(1.276)


# --- chart -----------------------------------------------------------------


def _record(target: str, offset: float, mismatch: bool) -> TrialRecord:
    return TrialRecord(
        trial_id=1,
        target=target,
        offset_ms=offset,
        booking_id=1,
        db_status_after="COMMITTED" if not mismatch else "ROLLED_BACK",
        mismatch=mismatch,
        cancel_latency_ms=1.0,
    )


def test_chart_math_groups_by_target_and_offset():
    data = mismatch_by_offset(
        [
            _record("naive", -10.0, True),
            _record("naive", -10.0, True),
            _record("fenced", -10.0, False),
            _record("fenced", 10.0, False),
        ]
    )
    assert data["naive"][-10.0] == 1.0
    assert data["fenced"][-10.0] == 0.0
    assert list(data["fenced"]) == [-10.0, 10.0]


def test_chart_writes_svg(tmp_path):
    records = [_record("naive", o, True) for o in (-500.0, 0.0, 500.0)]
    records += [_record("fenced", o, False) for o in (-500.0, 0.0, 500.0)]
    out = tmp_path / "chart.svg"
    render_mismatch_chart(records, out)
    svg = out.read_text(encoding="utf-8")
    # The file keeps its XML declaration, so check containment rather than prefix.
    assert "<svg" in svg
    assert "naive" in svg and "fenced" in svg
    assert "polyline" in svg


# --- orphan audit + verify -------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    db = tmp_path / "booking.db"
    monkeypatch.setenv("DB_PATH", str(db))
    bs.set_db_path(str(db))
    bs.init_db()
    yield
    bs.set_db_path(None)


def test_orphan_audit_counting():
    lo = bs.create_pending_booking(4, "7:00 PM")
    hi = bs.create_pending_booking(4, "8:00 PM")
    bs.commit_booking(lo)
    assert bs.count_bookings(status=bs.STATUS_PENDING_AUDIO, id_lo=lo, id_hi=hi) == 1


def test_fenced_sweep_leaves_no_orphans(tmp_path):
    config = SweepConfig(
        target="fenced",
        offsets_ms=[-10.0, 0.0, 10.0],
        runs_per_offset=2,
        results_dir=tmp_path,
    )
    result = run_sweep(config)
    assert result.passed
    assert result.orphans == 0


def test_verify_quick_smoke(tmp_path):
    runner = CliRunner()
    res = runner.invoke(cli, ["verify", "--quick", "--results-dir", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert (tmp_path / "summary.json").exists()
    svg = tmp_path / "mismatch_by_offset.svg"
    assert svg.exists() and svg.stat().st_size > 500
