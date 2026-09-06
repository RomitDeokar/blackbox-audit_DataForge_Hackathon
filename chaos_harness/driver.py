"""The sweep engine: run N trials against a target and write the trial log.

This is the part that turns "the fence looks right" into a falsifiable number.
For each offset on the ladder it interrupts the confirmation at that instant,
then asks two *independent* questions:

1. what does the booking store actually hold?  (the target)
2. what should it hold, given what the caller heard?  (the oracle in
   ``shared.audio_fence.evaluate_ground_truth``, computed straight from the
   timestamps, never from the fence)

A disagreement is a mismatch. The fenced target must produce zero.

Cost note: the default ``replay`` mode makes **no external API calls at all**.
The 606-trial acceptance sweep is free and takes under a second. Live mode
(``--mode live``) is opt-in, requires an explicit trial cap, and is the only
path that spends provider quota.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from shared import booking_store as bs
from shared.constants import (
    CONFIRMATION_GATING_WORD,
    SWEEP_RUNS_PER_OFFSET,
    VARIANT_DUMMY,
    VARIANT_FENCED,
    confirmation_sentence,
    sweep_offsets_ms,
)
from shared.rime_timestamps import (
    DEFAULT_TIMELINE_FIXTURE,
    WordTimeline,
    load_timeline,
    synthetic_timeline,
)

from .injector import BargeInPlan, plan_barge_in
from .targets import TrialOutcome, get_target
from .trial_log import TrialLogWriter, TrialRecord, trial_log_path

__all__ = [
    "SweepConfig",
    "SweepResult",
    "load_target_timeline",
    "run_sweep",
]

logger = logging.getLogger("blackbox_audit.driver")

DUMMY_SENTENCE = confirmation_sentence(4, "7:00 PM")


def load_target_timeline(
    target: str,
    fixture: str | Path | None = None,
) -> WordTimeline:
    """The confirmation timeline a target speaks.

    ``dummy`` gets an evenly-paced synthetic sentence (no fixture needed, so the
    harness self-test cannot be broken by a missing file). Everything else
    replays the cached real-shape Rime fixture.
    """
    if target == VARIANT_DUMMY and fixture is None:
        return synthetic_timeline(DUMMY_SENTENCE)
    return load_timeline(fixture or DEFAULT_TIMELINE_FIXTURE)


@dataclass
class SweepConfig:
    """One sweep's parameters."""

    target: str
    trials: int | None = None
    """Cap the trial count (takes the first N of the ladder). ``None`` = full sweep."""
    offsets_ms: list[float] = field(default_factory=lambda: [float(o) for o in sweep_offsets_ms()])
    runs_per_offset: int = SWEEP_RUNS_PER_OFFSET
    gating_word: str = CONFIRMATION_GATING_WORD
    occurrence: int = 1
    fixture: str | Path | None = None
    results_dir: str | Path | None = None
    log_path: str | Path | None = None
    mode: str = "replay"
    reset_db_between_trials: bool = False

    def resolved_log_path(self) -> Path:
        if self.log_path is not None:
            return Path(self.log_path)
        return trial_log_path(self.target, self.results_dir)

    def planned_trials(self) -> int:
        total = len(self.offsets_ms) * self.runs_per_offset
        return min(total, self.trials) if self.trials is not None else total


@dataclass
class SweepResult:
    """What a completed sweep produced."""

    target: str
    log_path: Path
    trials: int
    mismatches: int
    orphans: int = 0
    """Unresolved PENDING_AUDIO rows left behind by the sweep (fenced target)."""
    records: list[TrialRecord] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.mismatches == 0

    def summary_line(self) -> str:
        verdict = "PASS" if self.passed else f"FAIL ({self.mismatches} mismatches)"
        return f"{self.target}: {self.trials} trials -> {verdict}"


def _ladder(config: SweepConfig) -> Iterator[tuple[float, int]]:
    """Yield ``(offset_ms, run_index)`` pairs, capped at ``config.trials``.

    Runs are grouped per offset so a truncated sweep still covers a contiguous
    slice of the ladder rather than a sparse, unanalysable sample.
    """
    if config.runs_per_offset < 1:
        raise ValueError(f"runs_per_offset must be >= 1, got {config.runs_per_offset}")
    if config.trials is not None and config.trials < 0:
        raise ValueError(f"trials must be non-negative, got {config.trials}")
    if not config.offsets_ms:
        raise ValueError("sweep has no offsets")

    emitted = 0
    for offset in config.offsets_ms:
        for run_index in range(1, config.runs_per_offset + 1):
            if config.trials is not None and emitted >= config.trials:
                return
            emitted += 1
            yield float(offset), run_index


def run_sweep(
    config: SweepConfig,
    *,
    on_trial: Callable[[TrialRecord], None] | None = None,
    keep_records: bool = True,
) -> SweepResult:
    """Run the sweep and write ``results/trials_{target}.jsonl``.

    Args:
        config: Sweep parameters.
        on_trial: Optional per-trial callback (progress bars, live rendering).
        keep_records: Retain records in memory. A caller streaming a very large
            sweep can set this ``False`` and rely on the JSONL file.
    """
    if config.mode != "replay":
        raise NotImplementedError(
            f"mode {config.mode!r} is not available offline. Live mode spends real "
            "provider quota and is documented in docs/ACCEPTANCE_TEST.md; run it "
            "deliberately with a live LiveKit room and an explicit --trials cap."
        )

    timeline = load_target_timeline(config.target, config.fixture)
    # Fail loudly and early if the gating word is not in the sentence: a sweep
    # keyed off a missing word would silently grade every trial against the
    # wrong instant.
    gating_end = timeline.gating_time(config.gating_word, occurrence=config.occurrence)
    logger.info(
        "sweep target=%s gating=%r ends at %.3fs (audio %.3fs), %d planned trials",
        config.target,
        config.gating_word,
        gating_end,
        timeline.duration,
        config.planned_trials(),
    )

    target = get_target(
        config.target,
        **({} if config.target == VARIANT_DUMMY else {"reset_between_trials": config.reset_db_between_trials}),
    )
    log_path = config.resolved_log_path()

    records: list[TrialRecord] = []
    mismatches = 0
    trial_id = 0

    with TrialLogWriter(log_path, truncate=True) as writer:
        for offset_ms, run_index in _ladder(config):
            trial_id += 1
            target.prepare()

            plan = plan_barge_in(
                timeline,
                offset_ms,
                gating_word=config.gating_word,
                occurrence=config.occurrence,
            )

            try:
                outcome = target.run_trial(timeline, plan)
                error: str | None = None
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("trial %d (offset %+.0fms) crashed", trial_id, offset_ms)
                outcome = TrialOutcome(
                    booking_id=None,
                    db_status_after="ERROR",
                    actually_committed=False,
                    expected_committed=plan.expected_heard_gating_word,
                    cancel_latency_ms=None,
                    heard_text="",
                    error=f"{type(exc).__name__}: {exc}",
                )
                error = outcome.error

            record = _to_record(config, plan, outcome, trial_id, run_index, error)
            writer.write(record)

            if record.mismatch:
                mismatches += 1
                logger.warning(
                    "MISMATCH trial=%d offset=%+.0fms expected_committed=%s db=%s heard=%r",
                    trial_id,
                    offset_ms,
                    outcome.expected_committed,
                    outcome.db_status_after,
                    outcome.heard_text,
                )

            if keep_records:
                records.append(record)
            if on_trial is not None:
                on_trial(record)

    # Orphan audit: every booking id this sweep created must have reached a
    # terminal state. A row still PENDING_AUDIO after on_session_closed is a
    # leaked (unresolved) state change -- the exact failure mode the fence
    # exists to prevent, so it must fail the sweep loudly.
    orphans = 0
    if config.target == VARIANT_FENCED and keep_records and records:
        ids = [r.booking_id for r in records if r.booking_id is not None]
        if ids:
            lo, hi = min(ids), max(ids)
            orphans = bs.count_bookings(status=bs.STATUS_PENDING_AUDIO, id_lo=lo, id_hi=hi)
        if orphans:
            logger.error("sweep left %d PENDING_AUDIO row(s) un-resolved", orphans)

    logger.info("sweep complete: %d trials, %d mismatches -> %s", trial_id, mismatches, log_path)
    return SweepResult(
        target=config.target,
        log_path=log_path,
        trials=trial_id,
        mismatches=mismatches,
        orphans=orphans,
        records=records,
    )


def _to_record(
    config: SweepConfig,
    plan: BargeInPlan,
    outcome: TrialOutcome,
    trial_id: int,
    run_index: int,
    error: str | None,
) -> TrialRecord:
    return TrialRecord(
        trial_id=trial_id,
        target=config.target,
        offset_ms=plan.offset_ms,
        booking_id=outcome.booking_id,
        db_status_after=outcome.db_status_after,
        mismatch=outcome.mismatch,
        cancel_latency_ms=outcome.cancel_latency_ms,
        run_index=run_index,
        expected_committed=outcome.expected_committed,
        actually_committed=outcome.actually_committed,
        gating_word=plan.gating_word,
        gating_word_end_s=plan.gating_word_end_s,
        cancel_at_s=plan.cancel_at_s if plan.cancels else None,
        heard_text=outcome.heard_text,
        fence_outcome=outcome.fence_outcome,
        mode=config.mode,
        error=error,
    )
