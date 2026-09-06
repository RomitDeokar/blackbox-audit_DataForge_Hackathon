"""Tests for the audio fence state machine, driven purely by mocked events.

No LiveKit, no Rime, no network. These tests are the real proof that the
fencing rule is correct; the live agent only has to wire real events into it.
"""

from __future__ import annotations

import pytest

from shared import booking_store as bs
from shared.audio_fence import (
    AudioFence,
    FenceOutcome,
    FenceState,
    evaluate_ground_truth,
)
from shared.rime_timestamps import TimedWord, WordTimeline, load_timeline

SENTENCE = [
    ("Okay,", 0.0, 0.45),
    ("you're", 0.5, 0.75),
    ("confirmed", 0.8, 1.35),
    ("for", 1.4, 1.55),
    ("a", 1.6, 1.68),
    ("table", 1.7, 2.10),
]


class FakeStore:
    """Records fence -> store calls without touching SQLite."""

    def __init__(self) -> None:
        self.commits: list[int] = []
        self.rollbacks: list[int] = []

    def commit_booking(self, booking_id: int) -> None:
        self.commits.append(booking_id)

    def rollback_booking(self, booking_id: int) -> None:
        self.rollbacks.append(booking_id)


def _play(fence: AudioFence, upto: float) -> None:
    """Feed timestamp events for every word that starts before ``upto``."""
    for text, start, end in SENTENCE:
        if start >= upto:
            break
        if end <= upto:
            fence.on_timed_word(text, start, end)


# --- happy path ---------------------------------------------------------


def test_playback_completed_commits():
    store = FakeStore()
    fence = AudioFence(gating_word="confirmed", store=store)
    fence.on_pending(7)

    _play(fence, 99.0)
    fence.on_playback_finished(position=2.10, interrupted=False)

    assert fence.state is FenceState.COMMITTED
    assert fence.outcome is FenceOutcome.GATING_WORD_HEARD
    assert store.commits == [7]
    assert store.rollbacks == []


def test_commit_happens_as_soon_as_gating_word_lands():
    """We do not wait for the tail of the sentence to commit."""
    store = FakeStore()
    fence = AudioFence(gating_word="confirmed", store=store)
    fence.on_pending(1)

    fence.on_timed_word("Okay,", 0.0, 0.45)
    assert fence.state is FenceState.PENDING
    fence.on_timed_word("you're", 0.5, 0.75)
    assert fence.state is FenceState.PENDING

    fence.on_timed_word("confirmed", 0.8, 1.35)

    assert fence.state is FenceState.COMMITTED
    assert fence.gating_word_end == pytest.approx(1.35)
    assert store.commits == [1]


# --- barge-in path ------------------------------------------------------


def test_cancel_before_gating_word_rolls_back():
    store = FakeStore()
    fence = AudioFence(gating_word="confirmed", store=store)
    fence.on_pending(9)

    _play(fence, 0.9)  # mid-way through "confirmed"
    fence.on_cancelled(position=0.9)

    assert fence.state is FenceState.ROLLED_BACK
    assert fence.outcome is FenceOutcome.CANCELLED_BEFORE_GATING_WORD
    assert store.rollbacks == [9]
    assert store.commits == []
    assert fence.heard_text == "Okay, you're"


def test_interrupted_playback_finished_rolls_back():
    store = FakeStore()
    fence = AudioFence(gating_word="confirmed", store=store)
    fence.on_pending(3)

    _play(fence, 0.8)
    fence.on_playback_finished(position=0.8, interrupted=True)

    assert fence.state is FenceState.ROLLED_BACK
    assert store.rollbacks == [3]


def test_cancel_after_gating_word_still_commits():
    """The caller heard the confirmation; a later barge-in must not erase it."""
    store = FakeStore()
    fence = AudioFence(gating_word="confirmed", store=store)
    fence.on_pending(4)

    _play(fence, 1.5)
    fence.on_cancelled(position=1.45)

    assert fence.state is FenceState.COMMITTED
    assert store.commits == [4]
    assert store.rollbacks == []


def test_cancel_at_exact_gating_boundary_commits():
    store = FakeStore()
    fence = AudioFence(gating_word="confirmed", store=store)
    fence.on_pending(5)

    _play(fence, 1.35)
    fence.on_cancelled(position=1.35)

    assert fence.state is FenceState.COMMITTED


def test_cancel_one_millisecond_early_rolls_back():
    store = FakeStore()
    fence = AudioFence(gating_word="confirmed", store=store)
    fence.on_pending(6)

    _play(fence, 1.349)
    fence.on_cancelled(position=1.349)

    assert fence.state is FenceState.ROLLED_BACK


def test_session_close_rolls_back_pending():
    store = FakeStore()
    fence = AudioFence(gating_word="confirmed", store=store)
    fence.on_pending(11)

    _play(fence, 0.6)
    fence.on_session_closed()

    assert fence.state is FenceState.ROLLED_BACK
    assert fence.outcome is FenceOutcome.SESSION_CLOSED
    assert store.rollbacks == [11]


def test_session_close_after_commit_is_noop():
    store = FakeStore()
    fence = AudioFence(gating_word="confirmed", store=store)
    fence.on_pending(12)
    _play(fence, 99.0)
    fence.on_session_closed()

    assert fence.state is FenceState.COMMITTED
    assert store.rollbacks == []


# --- idempotency / duplicate events -------------------------------------


def test_no_double_commit_on_repeated_events():
    store = FakeStore()
    fence = AudioFence(gating_word="confirmed", store=store)
    fence.on_pending(1)

    _play(fence, 99.0)
    fence.on_playback_finished(position=2.1, interrupted=False)
    fence.on_playback_finished(position=2.1, interrupted=False)
    fence.on_timed_word("confirmed", 0.8, 1.35)
    fence.on_session_closed()

    assert store.commits == [1]
    assert store.rollbacks == []


def test_no_double_rollback_on_repeated_events():
    store = FakeStore()
    fence = AudioFence(gating_word="confirmed", store=store)
    fence.on_pending(2)

    fence.on_cancelled(position=0.3)
    fence.on_cancelled(position=0.3)
    fence.on_playback_finished(position=0.3, interrupted=True)
    fence.on_session_closed()

    assert store.rollbacks == [2]
    assert store.commits == []


def test_rollback_then_late_gating_word_does_not_commit():
    """A timestamp that arrives after cancellation must not resurrect the row."""
    store = FakeStore()
    fence = AudioFence(gating_word="confirmed", store=store)
    fence.on_pending(8)

    fence.on_cancelled(position=0.5)
    fence.on_timed_word("confirmed", 0.8, 1.35)

    assert fence.state is FenceState.ROLLED_BACK
    assert store.commits == []


def test_events_before_pending_do_not_commit():
    store = FakeStore()
    fence = AudioFence(gating_word="confirmed", store=store)

    _play(fence, 99.0)
    fence.on_playback_finished(position=2.1, interrupted=False)

    assert fence.state is FenceState.IDLE
    assert store.commits == []


def test_second_on_pending_is_ignored():
    store = FakeStore()
    fence = AudioFence(gating_word="confirmed", store=store)
    fence.on_pending(1)
    fence.on_pending(2)

    _play(fence, 99.0)
    assert store.commits == [1]


# --- degenerate / defensive cases ---------------------------------------


def test_playback_completed_without_gating_word_in_transcript():
    """Alignment never named the gating word, but all audio played: commit."""
    store = FakeStore()
    fence = AudioFence(gating_word="confirmed", store=store)
    fence.on_pending(1)

    fence.on_timed_word("Sure,", 0.0, 0.4)
    fence.on_timed_word("done.", 0.5, 0.9)
    fence.on_playback_finished(position=0.9, interrupted=False)

    assert fence.state is FenceState.COMMITTED
    assert fence.outcome is FenceOutcome.PLAYBACK_COMPLETED


def test_playback_finished_short_of_transcript_rolls_back():
    """A clean 'finished' that stopped early is treated as not heard."""
    store = FakeStore()
    fence = AudioFence(gating_word="confirmed", store=store)
    fence.on_pending(1)

    fence.on_timed_word("Okay,", 0.0, 0.45)
    fence.on_timed_word("you're", 0.5, 0.75)
    fence.on_playback_finished(position=0.5, interrupted=False)

    assert fence.state is FenceState.ROLLED_BACK
    assert fence.outcome is FenceOutcome.CANCELLED_BEFORE_GATING_WORD


def test_playback_finished_with_no_words_at_all_rolls_back():
    store = FakeStore()
    fence = AudioFence(gating_word="confirmed", store=store)
    fence.on_pending(1)

    fence.on_playback_finished(position=0.0, interrupted=False)

    assert fence.state is FenceState.ROLLED_BACK


def test_malformed_word_is_discarded():
    """A word whose end precedes its start is dropped, not used to open the fence."""
    store = FakeStore()
    fence = AudioFence(gating_word="confirmed", store=store)
    fence.on_pending(1)
    fence.on_timed_word("confirmed", 1.35, 0.80)  # end before start

    assert fence.state is FenceState.PENDING
    assert fence.heard_text == ""
    assert fence.decision().words_heard == 0
    assert store.commits == []


def test_fence_works_without_a_store():
    fence = AudioFence(gating_word="confirmed", store=None)
    fence.on_pending(1)
    _play(fence, 99.0)

    assert fence.state is FenceState.COMMITTED  # decided, just not persisted


def test_gating_word_matching_ignores_punctuation_and_case():
    fence = AudioFence(gating_word="CONFIRMED", store=FakeStore())
    fence.on_pending(1)
    fence.on_timed_word("Confirmed,", 0.1, 0.6)

    assert fence.state is FenceState.COMMITTED


def test_occurrence_selects_the_right_repeat():
    fence = AudioFence(gating_word="for", store=FakeStore(), occurrence=2)
    fence.on_pending(1)

    fence.on_timed_word("for", 0.0, 0.2)
    assert fence.state is FenceState.PENDING

    fence.on_timed_word("for", 0.5, 0.7)
    assert fence.state is FenceState.COMMITTED
    assert fence.gating_word_end == pytest.approx(0.7)


def test_invalid_construction_rejected():
    with pytest.raises(ValueError):
        AudioFence(gating_word="confirmed", occurrence=0)
    with pytest.raises(ValueError):
        AudioFence(gating_word="!!!")


def test_on_resolved_callback_fires_once():
    seen = []
    fence = AudioFence(
        gating_word="confirmed",
        store=FakeStore(),
        on_resolved=seen.append,
    )
    fence.on_pending(1)
    _play(fence, 99.0)
    fence.on_playback_finished(position=2.1, interrupted=False)

    assert len(seen) == 1
    assert seen[0].committed is True
    assert seen[0].booking_id == 1


def test_on_resolved_exception_does_not_break_the_fence():
    def boom(_decision):
        raise RuntimeError("callback exploded")

    store = FakeStore()
    fence = AudioFence(gating_word="confirmed", store=store, on_resolved=boom)
    fence.on_pending(1)
    _play(fence, 99.0)

    assert fence.state is FenceState.COMMITTED
    assert store.commits == [1]


def test_decision_dict_is_serialisable():
    fence = AudioFence(gating_word="confirmed", store=FakeStore())
    fence.on_pending(1)
    _play(fence, 0.9)
    fence.on_cancelled(position=0.9)

    payload = fence.decision().to_dict()
    assert payload["state"] == "ROLLED_BACK"
    assert payload["outcome"] == "CANCELLED_BEFORE_GATING_WORD"
    assert payload["heard_text"] == "Okay, you're"
    assert payload["words_heard"] == 2
    assert payload["cancelled_at"] == pytest.approx(0.9)


def test_timeline_view_reconstructs_heard_words():
    fence = AudioFence(gating_word="confirmed")
    fence.on_pending(1)
    _play(fence, 0.9)

    assert fence.timeline().text == "Okay, you're"


# --- integration with the real SQLite store -----------------------------


def test_fence_drives_real_store_commit(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "b.db"))
    bs.set_db_path(str(tmp_path / "b.db"))
    bs.init_db()
    try:
        booking_id = bs.create_pending_booking(4, "7:00 PM")
        fence = AudioFence(gating_word="confirmed", store=bs)
        fence.on_pending(booking_id)
        _play(fence, 99.0)

        assert bs.get_booking(booking_id)["status"] == bs.STATUS_COMMITTED
    finally:
        bs.set_db_path(None)


def test_fence_drives_real_store_rollback(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "b.db"))
    bs.set_db_path(str(tmp_path / "b.db"))
    bs.init_db()
    try:
        booking_id = bs.create_pending_booking(4, "7:00 PM")
        fence = AudioFence(gating_word="confirmed", store=bs)
        fence.on_pending(booking_id)
        _play(fence, 0.7)
        fence.on_cancelled(position=0.7)

        assert bs.get_booking(booking_id)["status"] == bs.STATUS_ROLLED_BACK
        events = [r["event_type"] for r in bs.get_transaction_log(booking_id)]
        assert events == [bs.EVENT_PENDING_CREATED, bs.EVENT_ROLLED_BACK]
    finally:
        bs.set_db_path(None)


# --- the independent oracle ---------------------------------------------


@pytest.mark.parametrize(
    ("cancel_at", "expected"),
    [(None, True), (0.0, False), (1.34, False), (1.35, True), (5.0, True)],
)
def test_evaluate_ground_truth(cancel_at, expected):
    timeline = WordTimeline(
        [TimedWord(text=text, start=start, end=end) for text, start, end in SENTENCE]
    )
    assert evaluate_ground_truth(timeline, "confirmed", cancel_at) is expected


def test_ground_truth_and_fence_agree_across_the_sweep():
    """The two independent implementations must never disagree.

    This is the whole acceptance test in miniature, run offline against the
    cached Rime timeline: the fence's verdict is compared to an oracle computed
    straight from the timestamps.
    """
    timeline = load_timeline()
    gating_end = timeline.gating_time("confirmed")

    for offset_ms in range(-500, 501, 10):
        cancel_at = gating_end + offset_ms / 1000.0
        fence = AudioFence(gating_word="confirmed", store=FakeStore())
        fence.on_pending(1)
        for word in timeline.words:
            if word.end <= cancel_at:
                fence.on_timed_word(word.text, word.start, word.end)
        fence.on_playback_finished(position=cancel_at, interrupted=True)

        expected = evaluate_ground_truth(timeline, "confirmed", cancel_at)
        assert fence.decision().committed is expected, f"disagreement at offset {offset_ms}ms"
