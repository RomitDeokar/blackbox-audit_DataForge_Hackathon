"""The audio fence: commit state only once the caller has *heard* it.

This is the core IP of BlackBox Audit, deliberately kept free of any LiveKit,
Rime, OpenAI or Deepgram import. The fence consumes four abstract events:

``on_pending(booking_id)``
    A tool call created a ``PENDING_AUDIO`` row.
``on_timed_word(text, start, end)``
    A word-level timestamp arrived from the TTS alignment stream.
``on_playback_finished(position, interrupted)``
    The audio segment stopped, either completed or cancelled.
``on_cancelled()``
    An out-of-band cancellation (harness barge-in, session close).

Keeping it abstract buys three things:

1. ``fenced_agent`` wires real ``livekit.agents`` events into it;
2. the chaos harness replays a cached Rime timeline through the exact same
   object, so a 606-trial sweep exercises the real decision code offline;
3. unit tests can assert the state machine directly with mocked events.

Decision rule
-------------
Commit if and only if the gating word's **end** timestamp was reached before
cancellation. A word cut off mid-syllable was not heard, so a fence that
committed on the word's *start* would still create phantom bookings.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Protocol

from .rime_timestamps import TimedWord, WordTimeline, normalize_word

__all__ = [
    "AudioFence",
    "FenceDecision",
    "FenceOutcome",
    "FenceState",
    "evaluate_ground_truth",
]

logger = logging.getLogger("blackbox_audit.audio_fence")


class FenceState(str, Enum):
    """Lifecycle of one fenced utterance."""

    IDLE = "IDLE"
    PENDING = "PENDING"
    COMMITTED = "COMMITTED"
    ROLLED_BACK = "ROLLED_BACK"


class FenceOutcome(str, Enum):
    """Why the fence resolved the way it did -- recorded for the evidence doc."""

    GATING_WORD_HEARD = "GATING_WORD_HEARD"
    PLAYBACK_COMPLETED = "PLAYBACK_COMPLETED"
    CANCELLED_BEFORE_GATING_WORD = "CANCELLED_BEFORE_GATING_WORD"
    SESSION_CLOSED = "SESSION_CLOSED"


class _Store(Protocol):
    """The slice of ``shared.booking_store`` the fence needs."""

    def commit_booking(self, booking_id: int) -> None: ...

    def rollback_booking(self, booking_id: int) -> None: ...


@dataclass
class FenceDecision:
    """The resolved verdict for one fenced utterance."""

    booking_id: int | None
    state: FenceState
    outcome: FenceOutcome | None = None
    gating_word_end: float | None = None
    """Playback-relative end time of the gating word, if it was ever seen."""
    cancelled_at: float | None = None
    """Playback position (s) at cancellation, or ``None`` if never cancelled."""
    heard_text: str = ""
    """Exactly what the caller heard, reconstructed from the timestamp stream."""
    words_heard: int = 0

    @property
    def committed(self) -> bool:
        return self.state is FenceState.COMMITTED

    def to_dict(self) -> dict:
        return {
            "booking_id": self.booking_id,
            "state": self.state.value,
            "outcome": self.outcome.value if self.outcome else None,
            "gating_word_end": self.gating_word_end,
            "cancelled_at": self.cancelled_at,
            "heard_text": self.heard_text,
            "words_heard": self.words_heard,
        }


@dataclass
class AudioFence:
    """State machine gating a DB commit on confirmed audio playback.

    Args:
        gating_word: The word whose completed playback authorises the commit.
        store: Object exposing ``commit_booking`` / ``rollback_booking``. When
            ``None`` the fence decides but persists nothing (used by the
            offline simulator and by pure unit tests).
        occurrence: Which occurrence of ``gating_word`` gates the commit, if the
            sentence repeats it.
        on_resolved: Optional callback invoked once with the final decision.
    """

    gating_word: str
    store: _Store | None = None
    occurrence: int = 1
    on_resolved: Callable[[FenceDecision], None] | None = None

    state: FenceState = FenceState.IDLE
    booking_id: int | None = None
    outcome: FenceOutcome | None = None
    gating_word_end: float | None = None
    cancelled_at: float | None = None
    _words: list[TimedWord] = field(default_factory=list, repr=False)
    _gating_seen: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        if self.occurrence < 1:
            raise ValueError(f"occurrence must be >= 1, got {self.occurrence}")
        self._needle = normalize_word(self.gating_word)
        if not self._needle:
            raise ValueError("gating_word must contain at least one word character")

    # -- properties ------------------------------------------------------

    @property
    def resolved(self) -> bool:
        return self.state in (FenceState.COMMITTED, FenceState.ROLLED_BACK)

    @property
    def gating_word_heard(self) -> bool:
        """True once the gating word has fully played."""
        return self.gating_word_end is not None

    @property
    def heard_text(self) -> str:
        return " ".join(w.text for w in self._words)

    def timeline(self) -> WordTimeline:
        """The words observed so far, as a :class:`WordTimeline`."""
        return WordTimeline(self._words)

    def decision(self) -> FenceDecision:
        return FenceDecision(
            booking_id=self.booking_id,
            state=self.state,
            outcome=self.outcome,
            gating_word_end=self.gating_word_end,
            cancelled_at=self.cancelled_at,
            heard_text=self.heard_text,
            words_heard=len(self._words),
        )

    # -- events ----------------------------------------------------------

    def on_pending(self, booking_id: int) -> None:
        """Register the ``PENDING_AUDIO`` row this utterance is fencing."""
        if self.state is not FenceState.IDLE:
            logger.warning(
                "on_pending ignored: fence already in state %s (booking %s)",
                self.state.value,
                self.booking_id,
            )
            return
        self.booking_id = booking_id
        self.state = FenceState.PENDING

    def on_timed_word(self, text: str, start: float, end: float) -> None:
        """Feed one word-level timestamp from the TTS alignment stream.

        Reaching the gating word commits immediately -- we do not wait for the
        rest of the sentence, because the caller has by then genuinely heard the
        confirmation.
        """
        if end < start:
            logger.warning("discarding word %r with end (%s) before start (%s)", text, end, start)
            return

        word = TimedWord(text=text, start=float(start), end=float(end))
        self._words.append(word)

        if word.normalized != self._needle:
            return

        self._gating_seen += 1
        if self._gating_seen != self.occurrence:
            return

        self.gating_word_end = word.end
        if self.state is FenceState.PENDING:
            self._commit(FenceOutcome.GATING_WORD_HEARD)

    def on_playback_finished(self, position: float, interrupted: bool) -> None:
        """Handle the end of the audio segment.

        A completed playback commits (belt-and-braces: it means every word,
        including the gating word, reached the caller). An interrupted playback
        rolls back unless the gating word had already been heard.
        """
        if interrupted:
            self.on_cancelled(position=position)
            return

        if self.state is not FenceState.PENDING:
            return

        if self.gating_word_heard:
            self._commit(FenceOutcome.GATING_WORD_HEARD)
        elif self._words and position + 1e-6 >= self._words[-1].end:
            # Alignment never named the gating word, but the audio played to the
            # end of everything we were told about. Trust the completion.
            self._commit(FenceOutcome.PLAYBACK_COMPLETED)
        else:
            # Playback "completed" short of the transcript we were promised:
            # treat as not heard. Silently committing here is the exact bug
            # this project exists to catch.
            logger.warning(
                "playback finished at %.3fs but transcript ran to %.3fs; rolling back",
                position,
                self._words[-1].end if self._words else 0.0,
            )
            self._rollback(FenceOutcome.CANCELLED_BEFORE_GATING_WORD, position)

    def on_cancelled(
        self,
        position: float | None = None,
        outcome: FenceOutcome = FenceOutcome.CANCELLED_BEFORE_GATING_WORD,
    ) -> None:
        """Handle a barge-in or session teardown."""
        if self.state is not FenceState.PENDING:
            return

        self.cancelled_at = position

        # A cancellation that lands after the gating word already played is not
        # a phantom booking -- the caller heard the confirmation.
        if self.gating_word_heard and (
            position is None or position + 1e-9 >= (self.gating_word_end or 0.0)
        ):
            self._commit(FenceOutcome.GATING_WORD_HEARD)
            return

        self._rollback(outcome, position)

    def on_session_closed(self) -> None:
        """Roll back anything still pending when the session tears down."""
        if self.state is FenceState.PENDING:
            self._rollback(FenceOutcome.SESSION_CLOSED, self.cancelled_at)

    # -- transitions -----------------------------------------------------

    def _commit(self, outcome: FenceOutcome) -> None:
        if self.state is not FenceState.PENDING:
            return
        self.state = FenceState.COMMITTED
        self.outcome = outcome
        if self.store is not None and self.booking_id is not None:
            self.store.commit_booking(self.booking_id)
        logger.info(
            "fence COMMIT booking=%s outcome=%s heard=%r",
            self.booking_id,
            outcome.value,
            self.heard_text,
        )
        self._notify()

    def _rollback(self, outcome: FenceOutcome, position: float | None) -> None:
        if self.state is not FenceState.PENDING:
            return
        self.state = FenceState.ROLLED_BACK
        self.outcome = outcome
        self.cancelled_at = position
        if self.store is not None and self.booking_id is not None:
            self.store.rollback_booking(self.booking_id)
        logger.info(
            "fence ROLLBACK booking=%s outcome=%s at=%s heard=%r",
            self.booking_id,
            outcome.value,
            position,
            self.heard_text,
        )
        self._notify()

    def _notify(self) -> None:
        if self.on_resolved is None:
            return
        try:
            self.on_resolved(self.decision())
        except Exception:  # pragma: no cover - callback owner's problem
            logger.exception("on_resolved callback raised")


def evaluate_ground_truth(
    timeline: WordTimeline,
    gating_word: str,
    cancel_at: float | None,
    occurrence: int = 1,
) -> bool:
    """Independent oracle: *should* a booking exist?

    Deliberately computed straight from the timeline rather than from the
    fence, so the harness never grades the fence using the fence's own logic.

    ``cancel_at=None`` means playback was never cancelled.
    """
    if cancel_at is None:
        return True
    return timeline.heard_gating_word(gating_word, cancel_at, occurrence=occurrence)
