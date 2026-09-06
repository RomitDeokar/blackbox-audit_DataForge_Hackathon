"""Tests for the fenced agent's LiveKit adapter (``FenceController``).

``fenced_agent.agent`` keeps every LiveKit import inside ``build_agent`` /
``build_session`` precisely so this layer can be tested with mocked events and
no SDK installed. What is exercised here is the wiring: playback progress
tracking, interrupted-vs-completed classification, and session teardown.

No LiveKit, no Rime, no network.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from fenced_agent.agent import FenceController
from shared import booking_store as bs
from shared.audio_fence import FenceOutcome, FenceState

SENTENCE = [
    ("Okay,", 0.0, 0.45),
    ("you're", 0.5, 0.75),
    ("confirmed", 0.8, 1.35),
    ("for", 1.4, 1.55),
    ("a", 1.6, 1.68),
    ("table", 1.7, 2.10),
]


class FakeStore:
    """Records controller -> store calls without touching SQLite."""

    def __init__(self) -> None:
        self.commits: list[int] = []
        self.rollbacks: list[int] = []

    def commit_booking(self, booking_id: int) -> None:
        self.commits.append(booking_id)

    def rollback_booking(self, booking_id: int) -> None:
        self.rollbacks.append(booking_id)


class FakeAudioOutput:
    """Stands in for LiveKit's ``io.AudioOutput`` event emitter."""

    def __init__(self) -> None:
        self.handlers: dict[str, list] = {}

    def on(self, event: str, handler) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def emit(self, event: str, payload) -> None:
        for handler in self.handlers.get(event, []):
            handler(payload)


def _progress(offset: float, duration: float):
    return SimpleNamespace(started_at=0.0, offset=offset, duration=duration)


def _finished(position: float, interrupted: bool):
    return SimpleNamespace(
        playback_position=position,
        interrupted=interrupted,
        synchronized_transcript=None,
    )


def _speak(controller: FenceController, upto: float) -> None:
    """Emit aligned-transcript words that fully played before ``upto``."""
    for text, start, end in SENTENCE:
        if end > upto:
            break
        controller.on_timed_word(text, start, end)


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def controller(store) -> FenceController:
    return FenceController(gating_word="confirmed", store=store)


# --- playback completed before any cancel -> COMMITTED -------------------


def test_full_playback_commits(controller, store):
    controller.begin(1)
    _speak(controller, 99.0)
    controller.on_playback_finished(_finished(2.10, interrupted=False))

    assert controller.fence.state is FenceState.COMMITTED
    assert store.commits == [1]
    assert store.rollbacks == []


def test_decision_is_recorded_for_the_report(controller):
    controller.begin(1)
    _speak(controller, 99.0)
    controller.on_playback_finished(_finished(2.10, interrupted=False))

    decision = controller.last_decision
    assert decision is not None
    assert decision.committed is True
    assert decision.booking_id == 1
    assert "confirmed" in decision.heard_text


# --- cancel before the gating word -> ROLLED_BACK ------------------------


def test_interrupt_before_gating_word_rolls_back(controller, store):
    controller.begin(2)
    _speak(controller, 0.75)  # heard "Okay, you're"
    controller.on_playback_finished(_finished(0.78, interrupted=True))

    assert controller.fence.state is FenceState.ROLLED_BACK
    assert controller.fence.outcome is FenceOutcome.CANCELLED_BEFORE_GATING_WORD
    assert store.rollbacks == [2]
    assert store.commits == []
    assert controller.last_decision.heard_text == "Okay, you're"


def test_interrupt_mid_gating_word_rolls_back(controller, store):
    """A word cut off mid-syllable was NOT heard. This is the core rule."""
    controller.begin(3)
    _speak(controller, 1.34)  # "confirmed" ends at 1.35 -- not yet emitted
    controller.on_playback_finished(_finished(1.34, interrupted=True))

    assert store.rollbacks == [3]


def test_interrupt_just_after_gating_word_commits(controller, store):
    controller.begin(4)
    _speak(controller, 1.35)
    controller.on_playback_finished(_finished(1.36, interrupted=True))

    assert store.commits == [4]
    assert store.rollbacks == []


# --- playback progress tracking ------------------------------------------


def test_playback_progress_is_monotonic(controller):
    controller.begin(1)
    controller.on_playback_started(SimpleNamespace(created_at=0.0))
    controller.on_playback_progressed(_progress(0.0, 0.5))
    controller.on_playback_progressed(_progress(0.5, 0.4))
    # An out-of-order/duplicate event must not rewind the position.
    controller.on_playback_progressed(_progress(0.0, 0.2))

    assert controller._playback_position == pytest.approx(0.9)


def test_progress_position_is_used_when_the_event_omits_it(controller, store):
    """Some emitters report position 0 on the finish event; trust progress."""
    controller.begin(1)
    _speak(controller, 99.0)
    controller.on_playback_progressed(_progress(0.0, 2.10))
    controller.on_playback_finished(_finished(0.0, interrupted=False))

    assert store.commits == [1]


def test_playback_started_resets_position_between_utterances(controller):
    controller.begin(1)
    controller.on_playback_progressed(_progress(0.0, 1.5))
    controller.on_playback_started(SimpleNamespace(created_at=0.0))

    assert controller._playback_position == 0.0


def test_malformed_events_do_not_crash_the_controller(controller):
    controller.begin(1)
    controller.on_playback_progressed(SimpleNamespace())  # no offset/duration
    controller.on_playback_progressed(SimpleNamespace(offset=None, duration=None))

    assert controller._playback_position == 0.0
    assert controller.fence.state is FenceState.PENDING


# --- no double commit / double rollback ---------------------------------


def test_repeated_finished_events_commit_once(controller, store):
    controller.begin(1)
    _speak(controller, 99.0)
    controller.on_playback_finished(_finished(2.10, interrupted=False))
    controller.on_playback_finished(_finished(2.10, interrupted=False))
    controller.on_playback_finished(_finished(2.10, interrupted=True))

    assert store.commits == [1]
    assert store.rollbacks == []


def test_repeated_interrupt_events_roll_back_once(controller, store):
    controller.begin(1)
    _speak(controller, 0.5)
    controller.on_playback_finished(_finished(0.5, interrupted=True))
    controller.on_playback_finished(_finished(0.5, interrupted=True))
    controller.on_close()

    assert store.rollbacks == [1]
    assert store.commits == []


def test_late_gating_word_after_rollback_does_not_resurrect_the_row(controller, store):
    controller.begin(1)
    controller.on_playback_finished(_finished(0.3, interrupted=True))
    controller.on_timed_word("confirmed", 0.8, 1.35)

    assert store.commits == []
    assert store.rollbacks == [1]


def test_only_one_decision_per_utterance(controller):
    controller.begin(1)
    _speak(controller, 99.0)
    controller.on_playback_finished(_finished(2.10, interrupted=False))
    controller.on_playback_finished(_finished(2.10, interrupted=False))
    controller.on_close()

    assert len(controller.decisions) == 1


# --- session teardown ----------------------------------------------------


def test_hangup_mid_confirmation_rolls_back(controller, store):
    """A dropped call must never leave a phantom PENDING_AUDIO row."""
    controller.begin(9)
    _speak(controller, 0.5)
    controller.on_close()

    assert controller.fence.outcome is FenceOutcome.SESSION_CLOSED
    assert store.rollbacks == [9]


def test_close_without_any_booking_is_a_noop(controller, store):
    controller.on_close()
    assert store.rollbacks == [] and store.commits == []


def test_events_before_begin_are_ignored(controller, store):
    controller.on_timed_word("confirmed", 0.8, 1.35)
    controller.on_playback_finished(_finished(2.1, interrupted=False))

    assert controller.fence is None
    assert store.commits == []


# --- consecutive calls in one session ------------------------------------


def test_second_booking_gets_a_fresh_fence(controller, store):
    controller.begin(1)
    _speak(controller, 99.0)
    controller.on_playback_finished(_finished(2.10, interrupted=False))

    controller.begin(2)
    _speak(controller, 0.6)
    controller.on_playback_finished(_finished(0.6, interrupted=True))

    assert store.commits == [1]
    assert store.rollbacks == [2]
    assert len(controller.decisions) == 2


# --- event subscription --------------------------------------------------


def test_attach_subscribes_to_all_three_playback_events(controller):
    output = FakeAudioOutput()
    controller.attach(output)

    assert set(output.handlers) == {
        "playback_started",
        "playback_progressed",
        "playback_finished",
    }


def test_attached_events_drive_the_fence(store):
    controller = FenceController(gating_word="confirmed", store=store)
    output = FakeAudioOutput()
    controller.attach(output)

    controller.begin(5)
    output.emit("playback_started", SimpleNamespace(created_at=0.0))
    _speak(controller, 0.75)
    output.emit("playback_progressed", _progress(0.0, 0.78))
    output.emit("playback_finished", _finished(0.78, interrupted=True))

    assert store.rollbacks == [5]


def test_attach_none_does_not_raise(controller):
    controller.attach(None)  # logs a warning; must not crash the session


# --- integration with the real SQLite store ------------------------------


def test_controller_drives_the_real_store(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "b.db"))
    bs.set_db_path(str(tmp_path / "b.db"))
    bs.init_db()
    try:
        controller = FenceController(gating_word="confirmed", store=bs)

        committed = bs.create_pending_booking(4, "7:00 PM")
        controller.begin(committed)
        _speak(controller, 99.0)
        controller.on_playback_finished(_finished(2.10, interrupted=False))

        rolled_back = bs.create_pending_booking(4, "7:00 PM")
        controller.begin(rolled_back)
        _speak(controller, 0.6)
        controller.on_playback_finished(_finished(0.6, interrupted=True))

        assert bs.get_booking(committed)["status"] == bs.STATUS_COMMITTED
        assert bs.get_booking(rolled_back)["status"] == bs.STATUS_ROLLED_BACK
        assert bs.count_bookings(status=bs.STATUS_PENDING_AUDIO) == 0
    finally:
        bs.set_db_path(None)
