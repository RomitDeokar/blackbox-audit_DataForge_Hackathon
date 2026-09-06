"""End-to-end tests for the chaos harness: sweep engine, targets and CLI.

Everything runs in ``replay`` mode, so the whole file makes zero external API
calls. A guard test asserts that explicitly.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from chaos_harness.cli import cli
from chaos_harness.driver import SweepConfig, load_target_timeline, run_sweep
from chaos_harness.injector import plan_barge_in
from chaos_harness.targets import get_target
from chaos_harness.trial_log import REQUIRED_FIELDS, read_trials
from shared import booking_store as bs


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    db = tmp_path / "booking.db"
    monkeypatch.setenv("DB_PATH", str(db))
    bs.set_db_path(str(db))
    bs.init_db()
    yield
    bs.set_db_path(None)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# --- Part 5's definition of done -----------------------------------------


def test_dummy_five_trials_produces_five_valid_jsonl_lines(runner, tmp_path):
    result = runner.invoke(
        cli,
        [
            "run",
            "--target",
            "dummy",
            "--trials",
            "5",
            "--results-dir",
            str(tmp_path),
            "--no-progress",
        ],
    )
    assert result.exit_code == 0, result.output

    log = tmp_path / "trials_dummy.jsonl"
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 5

    for lineno, line in enumerate(lines, start=1):
        payload = json.loads(line)
        for field in REQUIRED_FIELDS:
            assert field in payload, f"line {lineno} missing {field}"
        assert payload["target"] == "dummy"
        assert isinstance(payload["mismatch"], bool)
        assert isinstance(payload["trial_id"], int)

    assert [json.loads(line)["trial_id"] for line in lines] == [1, 2, 3, 4, 5]


def test_dummy_target_needs_no_fixture_file():
    """The harness self-test must not depend on the Rime fixture existing."""
    timeline = load_target_timeline("dummy")
    assert len(timeline) > 0
    assert timeline.gating_time("confirmed") > 0


# --- the falsifiable claim ------------------------------------------------


def test_fenced_sweep_has_zero_mismatches(tmp_path):
    result = run_sweep(SweepConfig(target="fenced", results_dir=tmp_path))

    assert result.trials == 303
    assert result.mismatches == 0
    assert result.passed is True


def test_naive_sweep_shows_mismatches(tmp_path):
    """The control group must actually fail, or the comparison proves nothing."""
    result = run_sweep(SweepConfig(target="naive", results_dir=tmp_path))

    assert result.trials == 303
    assert result.mismatches > 0
    assert result.passed is False


def test_naive_mismatches_are_all_phantom_bookings(tmp_path):
    """Every naive failure is a booking the caller never heard confirmed."""
    result = run_sweep(SweepConfig(target="naive", results_dir=tmp_path))

    for record in result.records:
        if record.mismatch:
            assert record.actually_committed is True
            assert record.expected_committed is False
            assert record.db_status_after == bs.STATUS_COMMITTED


def test_fenced_rolls_back_exactly_when_the_gating_word_was_not_heard(tmp_path):
    result = run_sweep(SweepConfig(target="fenced", results_dir=tmp_path))

    for record in result.records:
        expected = bs.STATUS_COMMITTED if record.expected_committed else bs.STATUS_ROLLED_BACK
        assert record.db_status_after == expected, f"offset {record.offset_ms}"
        # Nothing may be left dangling in PENDING_AUDIO.
        assert record.db_status_after != bs.STATUS_PENDING_AUDIO


def test_fenced_heard_text_never_includes_the_gating_word_when_rolled_back(tmp_path):
    result = run_sweep(SweepConfig(target="fenced", results_dir=tmp_path))

    for record in result.records:
        if record.db_status_after == bs.STATUS_ROLLED_BACK:
            assert "confirmed" not in record.heard_text.lower()


# --- sweep mechanics ------------------------------------------------------


def test_trial_count_is_offsets_times_runs(tmp_path):
    config = SweepConfig(
        target="dummy",
        offsets_ms=[-100.0, 0.0, 100.0],
        runs_per_offset=4,
        results_dir=tmp_path,
    )
    assert config.planned_trials() == 12
    assert run_sweep(config).trials == 12


def test_trials_cap_truncates_the_ladder(tmp_path):
    result = run_sweep(
        SweepConfig(target="dummy", trials=7, runs_per_offset=3, results_dir=tmp_path)
    )
    assert result.trials == 7
    # Runs are grouped per offset, so a cap yields a contiguous slice.
    assert [r.run_index for r in result.records] == [1, 2, 3, 1, 2, 3, 1]


def test_run_index_cycles_within_each_offset(tmp_path):
    result = run_sweep(
        SweepConfig(
            target="dummy",
            offsets_ms=[-50.0, 50.0],
            runs_per_offset=2,
            results_dir=tmp_path,
        )
    )
    assert [(r.offset_ms, r.run_index) for r in result.records] == [
        (-50.0, 1),
        (-50.0, 2),
        (50.0, 1),
        (50.0, 2),
    ]


def test_zero_runs_per_offset_rejected(tmp_path):
    with pytest.raises(ValueError, match="runs_per_offset"):
        run_sweep(SweepConfig(target="dummy", runs_per_offset=0, results_dir=tmp_path))


def test_empty_offset_ladder_rejected(tmp_path):
    with pytest.raises(ValueError, match="no offsets"):
        run_sweep(SweepConfig(target="dummy", offsets_ms=[], results_dir=tmp_path))


def test_unknown_gating_word_fails_loudly_before_any_trial(tmp_path):
    """Better to abort than to grade 303 trials against the wrong instant."""
    log = tmp_path / "trials_dummy.jsonl"
    with pytest.raises(KeyError):
        run_sweep(SweepConfig(target="dummy", gating_word="banana", log_path=log))
    assert not log.exists()


def test_unknown_target_rejected():
    with pytest.raises(ValueError, match="unknown target"):
        get_target("wat")


def test_live_mode_is_not_silently_faked(tmp_path):
    """Live mode must never be simulated: that would fake the headline result."""
    with pytest.raises(NotImplementedError, match="live"):
        run_sweep(SweepConfig(target="fenced", mode="live", results_dir=tmp_path))


def test_reset_db_between_trials_keeps_one_booking_at_a_time(tmp_path):
    run_sweep(
        SweepConfig(
            target="fenced",
            trials=4,
            reset_db_between_trials=True,
            results_dir=tmp_path,
        )
    )
    assert bs.count_bookings() == 1


def test_without_reset_every_trial_leaves_an_auditable_row(tmp_path):
    result = run_sweep(SweepConfig(target="fenced", trials=6, results_dir=tmp_path))
    assert bs.count_bookings() == 6
    assert len({r.booking_id for r in result.records}) == 6


def test_keep_records_false_still_writes_the_log(tmp_path):
    result = run_sweep(
        SweepConfig(target="dummy", trials=4, results_dir=tmp_path),
        keep_records=False,
    )
    assert result.records == []
    assert len(read_trials(tmp_path / "trials_dummy.jsonl", strict=True)) == 4


def test_on_trial_callback_sees_every_trial(tmp_path):
    seen = []
    run_sweep(
        SweepConfig(target="dummy", trials=5, results_dir=tmp_path),
        on_trial=seen.append,
    )
    assert [r.trial_id for r in seen] == [1, 2, 3, 4, 5]


def test_target_replay_matches_the_injector_oracle(tmp_path):
    """The target and the independent oracle must never disagree."""
    timeline = load_target_timeline("fenced")
    target = get_target("fenced")

    for offset in (-500, -100, -1, 0, 1, 100, 500):
        target.prepare()
        plan = plan_barge_in(timeline, offset)
        outcome = target.run_trial(timeline, plan)
        assert outcome.expected_committed is plan.expected_heard_gating_word
        assert outcome.mismatch is False


# --- CLI surface ----------------------------------------------------------


def test_cli_fenced_run_reports_pass(runner, tmp_path):
    result = runner.invoke(
        cli,
        ["run", "--target", "fenced", "--trials", "30", "--results-dir", str(tmp_path), "--no-progress"],
    )
    assert result.exit_code == 0, result.output
    assert "PASS" in result.output


def test_cli_naive_run_reports_fail_but_exits_zero(runner, tmp_path):
    """A naive failure is the expected finding, not a broken harness."""
    result = runner.invoke(
        cli,
        ["run", "--target", "naive", "--results-dir", str(tmp_path), "--no-progress"],
    )
    assert result.exit_code == 0, result.output
    assert "FAIL" in result.output


def test_cli_custom_offset_ladder(runner, tmp_path):
    result = runner.invoke(
        cli,
        [
            "run",
            "--target",
            "dummy",
            "--min-ms",
            "-20",
            "--max-ms",
            "20",
            "--step-ms",
            "20",
            "--runs-per-offset",
            "1",
            "--results-dir",
            str(tmp_path),
            "--no-progress",
        ],
    )
    assert result.exit_code == 0, result.output

    records = read_trials(tmp_path / "trials_dummy.jsonl", strict=True)
    assert [r.offset_ms for r in records] == [-20.0, 0.0, 20.0]


def test_cli_rejects_non_positive_step(runner, tmp_path):
    result = runner.invoke(
        cli,
        ["run", "--target", "dummy", "--step-ms", "0", "--results-dir", str(tmp_path)],
    )
    assert result.exit_code != 0
    assert "step-ms" in result.output


def test_cli_rejects_inverted_range(runner, tmp_path):
    result = runner.invoke(
        cli,
        ["run", "--target", "dummy", "--min-ms", "100", "--max-ms", "-100", "--results-dir", str(tmp_path)],
    )
    assert result.exit_code != 0


def test_cli_live_mode_requires_room_and_trial_cap(runner, tmp_path):
    """Live mode spends real quota, so it may never start unbounded."""
    no_room = runner.invoke(
        cli, ["run", "--target", "fenced", "--mode", "live", "--trials", "3"]
    )
    assert no_room.exit_code != 0
    assert "--room" in no_room.output

    no_cap = runner.invoke(
        cli, ["run", "--target", "fenced", "--mode", "live", "--room", "test-room"]
    )
    assert no_cap.exit_code != 0
    assert "--trials" in no_cap.output


def test_cli_timeline_marks_the_gating_word(runner):
    result = runner.invoke(cli, ["timeline"])
    assert result.exit_code == 0, result.output
    assert "gating word" in result.output
    assert "confirmed" in result.output


def test_cli_timeline_json_is_a_valid_rime_frame(runner):
    result = runner.invoke(cli, ["timeline", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    block = payload["word_timestamps"]
    assert len(block["words"]) == len(block["start"]) == len(block["end"])


def test_cli_timeline_unknown_gating_word_exits_nonzero(runner):
    result = runner.invoke(cli, ["timeline", "--gating-word", "banana"])
    assert result.exit_code != 0


def test_cli_heard_answers_the_part2_question(runner):
    result = runner.invoke(cli, ["heard", "--at", "2.1"])
    assert result.exit_code == 0, result.output
    assert "User heard:" in result.output
    assert "Okay," in result.output
    assert "Booking should exist: True" in result.output


def test_cli_heard_before_the_gating_word(runner):
    result = runner.invoke(cli, ["heard", "--at", "0.5"])
    assert result.exit_code == 0
    assert "Booking should exist: False" in result.output


def test_cli_acceptance_runs_both_variants_and_writes_summary(runner, tmp_path):
    result = runner.invoke(
        cli, ["acceptance", "--runs-per-offset", "1", "--results-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output

    assert len(read_trials(tmp_path / "trials_naive.jsonl", strict=True)) == 101
    assert len(read_trials(tmp_path / "trials_fenced.jsonl", strict=True)) == 101

    summary = json.loads((tmp_path / "summary.json").read_text(encoding="utf-8"))
    assert summary["verdict"] == "PASS"
    assert summary["targets"]["fenced"]["mismatches"] == 0
    assert summary["targets"]["naive"]["mismatches"] > 0


# --- the API budget rule --------------------------------------------------


def test_replay_sweep_makes_no_external_api_calls(monkeypatch, tmp_path):
    """The 606-trial acceptance sweep must cost nothing. Enforce it."""
    import requests

    def fail(*args, **kwargs):
        raise AssertionError("external API call attempted during a replay sweep")

    monkeypatch.setattr(requests, "get", fail)
    monkeypatch.setattr(requests, "post", fail)
    monkeypatch.setattr(requests, "request", fail)

    for target in ("dummy", "naive", "fenced"):
        result = run_sweep(SweepConfig(target=target, trials=20, results_dir=tmp_path))
        assert result.trials == 20
