"""Tests for the frozen JSONL trial-log schema.

The schema is a contract between the harness and the reporting module, so
these tests assert the field names themselves, not just round-tripping.
"""

from __future__ import annotations

import json

import pytest

from chaos_harness.trial_log import (
    REQUIRED_FIELDS,
    TrialLogWriter,
    TrialRecord,
    iter_trials,
    read_trials,
    trial_log_path,
    write_json_atomic,
    write_trials,
)


def _record(**overrides) -> TrialRecord:
    base = {
        "trial_id": 1,
        "target": "fenced",
        "offset_ms": -120.0,
        "booking_id": 7,
        "db_status_after": "ROLLED_BACK",
        "mismatch": False,
        "cancel_latency_ms": 2.5,
    }
    base.update(overrides)
    return TrialRecord(**base)


# --- the contract ---------------------------------------------------------


def test_required_fields_are_exactly_the_part5_schema():
    assert REQUIRED_FIELDS == (
        "trial_id",
        "target",
        "offset_ms",
        "booking_id",
        "db_status_after",
        "mismatch",
        "cancel_latency_ms",
        "timestamp",
    )


def test_required_fields_come_first_and_in_order():
    keys = list(_record().to_dict())
    assert keys[: len(REQUIRED_FIELDS)] == list(REQUIRED_FIELDS)


def test_every_record_carries_all_required_fields():
    payload = json.loads(_record().to_json())
    for field in REQUIRED_FIELDS:
        assert field in payload


def test_timestamp_is_auto_populated_utc():
    assert _record().timestamp.endswith("+00:00")


# --- round-tripping -------------------------------------------------------


def test_round_trip_preserves_every_field():
    original = _record(heard_text="Okay, you're", fence_outcome="CANCELLED_BEFORE_GATING_WORD")
    again = TrialRecord.from_dict(json.loads(original.to_json()))
    assert again.to_dict() == original.to_dict()


def test_from_dict_ignores_unknown_future_fields():
    """A reader on an older version must not crash on a newer producer."""
    payload = _record().to_dict()
    payload["some_future_field"] = 123
    assert TrialRecord.from_dict(payload).trial_id == 1


def test_from_dict_rejects_missing_required_field():
    payload = _record().to_dict()
    del payload["mismatch"]
    with pytest.raises(ValueError, match="missing required field"):
        TrialRecord.from_dict(payload)


# --- file IO --------------------------------------------------------------


def test_write_then_read_five_valid_lines(tmp_path):
    path = tmp_path / "trials_dummy.jsonl"
    write_trials(path, [_record(trial_id=i) for i in range(1, 6)])

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 5
    records = read_trials(path, strict=True)
    assert [r.trial_id for r in records] == [1, 2, 3, 4, 5]


def test_truncate_replaces_previous_run(tmp_path):
    path = tmp_path / "t.jsonl"
    write_trials(path, [_record(trial_id=1)])
    write_trials(path, [_record(trial_id=9)])
    assert [r.trial_id for r in read_trials(path)] == [9]


def test_append_mode_keeps_previous_run(tmp_path):
    path = tmp_path / "t.jsonl"
    write_trials(path, [_record(trial_id=1)])
    write_trials(path, [_record(trial_id=2)], truncate=False)
    assert [r.trial_id for r in read_trials(path)] == [1, 2]


def test_writer_flushes_each_line_so_a_tail_sees_progress(tmp_path):
    """The live dashboard tails this file; buffering would look like a hang."""
    path = tmp_path / "t.jsonl"
    with TrialLogWriter(path) as writer:
        writer.write(_record(trial_id=1))
        assert len(read_trials(path)) == 1
        writer.write(_record(trial_id=2))
        assert len(read_trials(path)) == 2


def test_writer_outside_context_manager_raises(tmp_path):
    writer = TrialLogWriter(tmp_path / "t.jsonl")
    with pytest.raises(RuntimeError):
        writer.write(_record())


def test_missing_file_reads_as_empty_but_raises_in_strict_mode(tmp_path):
    missing = tmp_path / "nope.jsonl"
    assert read_trials(missing) == []
    with pytest.raises(FileNotFoundError):
        read_trials(missing, strict=True)


def test_partial_final_line_is_skipped_not_fatal(tmp_path):
    """A tailing reader routinely catches a half-written line mid-flush."""
    path = tmp_path / "t.jsonl"
    path.write_text(_record(trial_id=1).to_json() + '\n{"trial_id": 2, "targ', encoding="utf-8")

    assert [r.trial_id for r in iter_trials(path)] == [1]
    with pytest.raises(ValueError):
        read_trials(path, strict=True)


def test_blank_lines_are_ignored(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text("\n" + _record().to_json() + "\n\n", encoding="utf-8")
    assert len(read_trials(path, strict=True)) == 1


def test_log_path_naming_convention():
    assert trial_log_path("fenced", "/tmp/x").name == "trials_fenced.jsonl"


def test_write_json_atomic_leaves_no_temp_files(tmp_path):
    out = tmp_path / "deep" / "summary.json"
    write_json_atomic(out, {"verdict": "PASS"})

    assert json.loads(out.read_text(encoding="utf-8")) == {"verdict": "PASS"}
    assert [p.name for p in out.parent.iterdir()] == ["summary.json"]
