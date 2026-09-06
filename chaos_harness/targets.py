"""Trial targets: the three things the harness can audit.

Every target answers the same question with the same interface, so the sweep
engine, the log schema and the report are identical across all three:

    given a confirmation timeline and a barge-in plan,
    what state does the backend end up in?

``fenced``
    The real :class:`shared.audio_fence.AudioFence` decision code, driven by a
    replayed Rime timeline, writing to the real SQLite store. Only the
    transport is simulated -- the fencing rule, the state machine and the
    ``PENDING_AUDIO -> COMMITTED/ROLLED_BACK`` transitions are the exact ones
    the live agent runs.

``naive``
    ``booking_store.book_naive`` on tool call, then the confirmation is spoken
    into the void. Same store, same timeline, no fence. This is the control
    group; its mismatches are what make the fenced result meaningful.

``dummy``
    A synthetic timeline and an in-memory store: no SQLite, no fixture, no
    keys. Exists so the driver/logging pipeline can be smoke-tested in
    isolation (Part 5's definition of done).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Protocol

from shared import booking_store as bs
from shared.audio_fence import AudioFence, FenceOutcome, evaluate_ground_truth
from shared.constants import (
    TEST_BOOKING_TIME,
    TEST_PARTY_SIZE,
    VARIANT_DUMMY,
    VARIANT_FENCED,
    VARIANT_NAIVE,
)
from shared.rime_timestamps import WordTimeline

from .injector import BargeInPlan
from .trial_log import DB_STATUS_MISSING

__all__ = [
    "TARGETS",
    "TrialOutcome",
    "Target",
    "get_target",
]

logger = logging.getLogger("blackbox_audit.targets")


@dataclass
class TrialOutcome:
    """Everything one trial produced, before it becomes a log record."""

    booking_id: int | None
    db_status_after: str
    actually_committed: bool
    expected_committed: bool
    cancel_latency_ms: float | None
    heard_text: str
    fence_outcome: str | None = None
    error: str | None = None

    @property
    def mismatch(self) -> bool:
        """Did the backend disagree with what the caller actually heard?"""
        return self.actually_committed is not self.expected_committed


class Target(Protocol):
    """A thing the harness can run trials against."""

    name: str

    def prepare(self) -> None:
        """Reset any state so each trial audits a clean slate."""

    def run_trial(self, timeline: WordTimeline, plan: BargeInPlan) -> TrialOutcome: ...


def _replay(
    fence: AudioFence | None,
    timeline: WordTimeline,
    plan: BargeInPlan,
) -> tuple[str, float]:
    """Feed the timeline into ``fence`` up to the cancellation instant.

    Returns the heard transcript and the playback position reached. Words are
    delivered only once fully played -- a word cut off mid-syllable was not
    heard, which is the entire point of the fence.
    """
    cancel_at = plan.cancel_at_s if plan.cancels else float("inf")
    heard: list[str] = []
    position = 0.0

    for word in timeline.words:
        if word.end > cancel_at:
            break
        heard.append(word.text)
        position = word.end
        if fence is not None:
            fence.on_timed_word(word.text, word.start, word.end)

    if plan.cancels:
        # The caller barged in partway through: playback position is where the
        # audio was actually cut, which can be mid-word.
        position = max(position, min(cancel_at, timeline.duration))
    else:
        position = timeline.duration

    return " ".join(heard), position


class _MemoryStore:
    """Minimal in-memory stand-in for the booking store (``dummy`` target)."""

    def __init__(self) -> None:
        self._status: dict[int, str] = {}
        self._next_id = 1

    def create_pending_booking(self) -> int:
        booking_id = self._next_id
        self._next_id += 1
        self._status[booking_id] = bs.STATUS_PENDING_AUDIO
        return booking_id

    def commit_booking(self, booking_id: int) -> None:
        self._status[booking_id] = bs.STATUS_COMMITTED

    def rollback_booking(self, booking_id: int) -> None:
        self._status[booking_id] = bs.STATUS_ROLLED_BACK

    def status(self, booking_id: int) -> str:
        return self._status.get(booking_id, DB_STATUS_MISSING)

    def reset(self) -> None:
        self._status.clear()
        self._next_id = 1


class FencedReplayTarget:
    """The fenced agent's decision code, replayed offline against real SQLite."""

    name = VARIANT_FENCED
    variant = VARIANT_FENCED

    def __init__(
        self,
        *,
        party_size: int = TEST_PARTY_SIZE,
        time_str: str = TEST_BOOKING_TIME,
        reset_between_trials: bool = False,
    ) -> None:
        self.party_size = party_size
        self.time_str = time_str
        self.reset_between_trials = reset_between_trials

    def prepare(self) -> None:
        if self.reset_between_trials:
            bs.reset_db()
        else:
            bs.init_db()

    def run_trial(self, timeline: WordTimeline, plan: BargeInPlan) -> TrialOutcome:
        # Tool call: creates PENDING_AUDIO only. Nothing is real yet.
        booking_id = bs.create_pending_booking(
            self.party_size, self.time_str, agent_variant=self.variant
        )
        fence = AudioFence(
            gating_word=plan.gating_word,
            store=bs,
            occurrence=plan.occurrence,
        )
        fence.on_pending(booking_id)

        started = time.perf_counter()
        heard, position = _replay(fence, timeline, plan)

        if plan.cancels:
            fence.on_playback_finished(position=position, interrupted=True)
        else:
            fence.on_playback_finished(position=position, interrupted=False)
        # Belt and braces: a session teardown must never leave PENDING_AUDIO.
        fence.on_session_closed()
        # The latency window closes only once the final state is READABLE, not
        # merely decided: a fence that decides fast but leaves the row visibly
        # PENDING_AUDIO has not actually protected anything.
        status = bs.get_booking(booking_id)["status"]
        latency_ms = (time.perf_counter() - started) * 1000.0

        return TrialOutcome(
            booking_id=booking_id,
            db_status_after=status,
            actually_committed=status == bs.STATUS_COMMITTED,
            expected_committed=evaluate_ground_truth(
                timeline,
                plan.gating_word,
                plan.cancel_at_s if plan.cancels else None,
                occurrence=plan.occurrence,
            ),
            cancel_latency_ms=latency_ms,
            heard_text=heard,
            fence_outcome=fence.outcome.value if fence.outcome else None,
        )


class NaiveReplayTarget:
    """The naive agent: committed on tool call, regardless of what was heard."""

    name = VARIANT_NAIVE
    variant = VARIANT_NAIVE

    def __init__(
        self,
        *,
        party_size: int = TEST_PARTY_SIZE,
        time_str: str = TEST_BOOKING_TIME,
        reset_between_trials: bool = False,
    ) -> None:
        self.party_size = party_size
        self.time_str = time_str
        self.reset_between_trials = reset_between_trials

    def prepare(self) -> None:
        if self.reset_between_trials:
            bs.reset_db()
        else:
            bs.init_db()

    def run_trial(self, timeline: WordTimeline, plan: BargeInPlan) -> TrialOutcome:
        # THE BUG, ON PURPOSE: the row is COMMITTED before a single word of the
        # confirmation has left the speaker.
        booking_id = bs.book_naive(self.party_size, self.time_str, agent_variant=self.variant)

        started = time.perf_counter()
        heard, _position = _replay(None, timeline, plan)
        # Measured over the identical window as the fenced target. It comes out
        # near-zero for a damning reason: there is no resolution step at all --
        # the row was already COMMITTED before the barge-in happened.
        status = bs.get_booking(booking_id)["status"]
        latency_ms = (time.perf_counter() - started) * 1000.0

        return TrialOutcome(
            booking_id=booking_id,
            db_status_after=status,
            actually_committed=status == bs.STATUS_COMMITTED,
            expected_committed=evaluate_ground_truth(
                timeline,
                plan.gating_word,
                plan.cancel_at_s if plan.cancels else None,
                occurrence=plan.occurrence,
            ),
            cancel_latency_ms=latency_ms,
            heard_text=heard,
            fence_outcome=None,
        )


class DummyTarget:
    """No agent, no SQLite, no fixture -- just the logging pipeline.

    Uses the fenced policy so a dummy sweep is expected to be mismatch-free;
    that makes it a usable self-test of the harness itself.
    """

    name = VARIANT_DUMMY
    variant = VARIANT_DUMMY

    def __init__(self) -> None:
        self.store = _MemoryStore()

    def prepare(self) -> None:
        self.store.reset()

    def run_trial(self, timeline: WordTimeline, plan: BargeInPlan) -> TrialOutcome:
        booking_id = self.store.create_pending_booking()
        fence = AudioFence(
            gating_word=plan.gating_word,
            store=self.store,
            occurrence=plan.occurrence,
        )
        fence.on_pending(booking_id)

        started = time.perf_counter()
        heard, position = _replay(fence, timeline, plan)
        fence.on_playback_finished(position=position, interrupted=plan.cancels)
        fence.on_session_closed()
        status = self.store.status(booking_id)
        latency_ms = (time.perf_counter() - started) * 1000.0

        return TrialOutcome(
            booking_id=booking_id,
            db_status_after=status,
            actually_committed=status == bs.STATUS_COMMITTED,
            expected_committed=evaluate_ground_truth(
                timeline,
                plan.gating_word,
                plan.cancel_at_s if plan.cancels else None,
                occurrence=plan.occurrence,
            ),
            cancel_latency_ms=latency_ms,
            heard_text=heard,
            fence_outcome=fence.outcome.value if fence.outcome else None,
        )


TARGETS: dict[str, type] = {
    VARIANT_FENCED: FencedReplayTarget,
    VARIANT_NAIVE: NaiveReplayTarget,
    VARIANT_DUMMY: DummyTarget,
}


def get_target(name: str, **kwargs: object) -> Target:
    """Instantiate a target by name."""
    try:
        factory = TARGETS[name]
    except KeyError:
        raise ValueError(
            f"unknown target {name!r}; choose one of {', '.join(sorted(TARGETS))}"
        ) from None
    if factory is DummyTarget:
        return factory()  # type: ignore[return-value]
    return factory(**kwargs)  # type: ignore[return-value]


# Re-exported so callers can name outcomes without importing shared.audio_fence.
FENCE_OUTCOMES = tuple(o.value for o in FenceOutcome)
