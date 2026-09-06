"""Timestamp-keyed barge-in injection.

The whole rigour claim of this project rests on one detail: the barge-in is
scheduled **relative to Rime's own word-level timestamp stream**, not to
wall-clock time. "Cancel 120ms after the word 'confirmed' finished playing" is
a statement about the caller's ear. "Cancel 2.4 seconds after the tool call"
is a statement about a network, a jitter buffer and an event loop, and would
smear the measurement across ~100ms of noise -- wider than the effect being
measured.

Two injection modes, sharing this module's arithmetic:

``replay`` (default, free)
    A cached Rime timeline is replayed through the real decision code
    (``shared.audio_fence.AudioFence``) and the real SQLite store. Nothing is
    mocked except the transport. This is what makes a 606-trial sweep possible
    under the project's API budget rule.

``live`` (opt-in, paid)
    A real LiveKit room, a real agent, real Rime audio. ``chaos_harness.driver``
    speaks the scripted request and this module schedules the barge-in off the
    agent's actual emitted timestamps.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from shared.constants import CONFIRMATION_GATING_WORD
from shared.rime_timestamps import WordTimeline

__all__ = [
    "BargeInPlan",
    "NEVER_CANCEL",
    "plan_barge_in",
]

logger = logging.getLogger("blackbox_audit.injector")

NEVER_CANCEL = float("inf")
"""Sentinel offset meaning "let the confirmation play to the end"."""


@dataclass(frozen=True)
class BargeInPlan:
    """When to interrupt, expressed in playback-relative seconds."""

    gating_word: str
    gating_word_end_s: float
    offset_ms: float
    cancel_at_s: float
    occurrence: int = 1
    clipped: bool = False
    """True when the requested offset fell outside the audio and was clamped."""

    @property
    def cancels(self) -> bool:
        return self.cancel_at_s != NEVER_CANCEL

    @property
    def expected_heard_gating_word(self) -> bool:
        """Ground truth: had the caller heard the gating word at cancel time?"""
        if not self.cancels:
            return True
        return self.cancel_at_s + 1e-9 >= self.gating_word_end_s

    def to_dict(self) -> dict[str, object]:
        return {
            "gating_word": self.gating_word,
            "gating_word_end_s": self.gating_word_end_s,
            "offset_ms": self.offset_ms,
            "cancel_at_s": None if not self.cancels else self.cancel_at_s,
            "occurrence": self.occurrence,
            "clipped": self.clipped,
        }


def plan_barge_in(
    timeline: WordTimeline,
    offset_ms: float,
    *,
    gating_word: str = CONFIRMATION_GATING_WORD,
    occurrence: int = 1,
) -> BargeInPlan:
    """Compute the cancellation instant for one trial.

    Args:
        timeline: The word-level timeline for the confirmation sentence.
        offset_ms: Milliseconds relative to the END of the gating word.
            Negative interrupts before the caller heard it; positive after.
            ``float('inf')`` means never cancel.
        gating_word: The word whose completed playback authorises the commit.
        occurrence: Which repeat of ``gating_word`` gates the commit.

    Raises:
        KeyError: the gating word is absent from the timeline -- a silent
            fallback here would invalidate every downstream verdict.
    """
    if not timeline:
        raise ValueError("cannot plan a barge-in against an empty timeline")

    gating_end = timeline.gating_time(gating_word, occurrence=occurrence)

    if offset_ms == NEVER_CANCEL:
        return BargeInPlan(
            gating_word=gating_word,
            gating_word_end_s=gating_end,
            offset_ms=offset_ms,
            cancel_at_s=NEVER_CANCEL,
            occurrence=occurrence,
        )

    cancel_at = gating_end + float(offset_ms) / 1000.0

    # Clamp into the audio window. A negative cancel time is physically
    # meaningless (the barge-in would precede the first sample) and a time past
    # the end of the audio is indistinguishable from "never cancelled" -- both
    # are recorded as clipped so the report can say so honestly instead of
    # quietly counting them as ordinary trials.
    clipped = False
    if cancel_at < 0.0:
        cancel_at, clipped = 0.0, True
    elif cancel_at > timeline.duration:
        cancel_at, clipped = timeline.duration, True

    if clipped:
        logger.debug(
            "offset %+.0fms clipped to %.3fs (audio spans 0..%.3fs)",
            offset_ms,
            cancel_at,
            timeline.duration,
        )

    return BargeInPlan(
        gating_word=gating_word,
        gating_word_end_s=gating_end,
        offset_ms=float(offset_ms),
        cancel_at_s=cancel_at,
        occurrence=occurrence,
        clipped=clipped,
    )
