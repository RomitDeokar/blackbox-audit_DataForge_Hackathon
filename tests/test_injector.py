"""Tests for timestamp-keyed barge-in planning.

Pure arithmetic over a cached timeline: no network, no keys, no cost.
"""

from __future__ import annotations

import pytest

from chaos_harness.injector import NEVER_CANCEL, plan_barge_in
from shared.rime_timestamps import TimedWord, WordTimeline, load_timeline

SENTENCE = [
    ("Okay,", 0.0, 0.45),
    ("you're", 0.5, 0.75),
    ("confirmed", 0.8, 1.35),
    ("for", 1.4, 1.55),
    ("a", 1.6, 1.68),
    ("table", 1.7, 2.10),
]


@pytest.fixture
def timeline() -> WordTimeline:
    return WordTimeline([TimedWord(t, s, e) for t, s, e in SENTENCE])


# --- the offset is keyed to the gating word, not wall clock ---------------


def test_zero_offset_lands_exactly_on_the_gating_word_end(timeline):
    plan = plan_barge_in(timeline, 0)

    assert plan.gating_word_end_s == pytest.approx(1.35)
    assert plan.cancel_at_s == pytest.approx(1.35)
    assert plan.cancels is True
    # At exactly the boundary the word HAS finished playing.
    assert plan.expected_heard_gating_word is True


@pytest.mark.parametrize(
    ("offset_ms", "expected_cancel_at", "expected_heard"),
    [
        (-500, 0.85, False),
        (-100, 1.25, False),
        (-10, 1.34, False),
        (-1, 1.349, False),
        (0, 1.35, True),
        (1, 1.351, True),
        (100, 1.45, True),
        (500, 1.85, True),
    ],
)
def test_offset_ladder(timeline, offset_ms, expected_cancel_at, expected_heard):
    plan = plan_barge_in(timeline, offset_ms)

    assert plan.cancel_at_s == pytest.approx(expected_cancel_at)
    assert plan.expected_heard_gating_word is expected_heard
    assert plan.offset_ms == float(offset_ms)


def test_never_cancel_sentinel(timeline):
    plan = plan_barge_in(timeline, NEVER_CANCEL)

    assert plan.cancels is False
    assert plan.expected_heard_gating_word is True
    assert plan.to_dict()["cancel_at_s"] is None


# --- clipping -------------------------------------------------------------


def test_offset_before_audio_start_is_clipped(timeline):
    """-2s from a gating word at 1.35s would be negative: clamp and flag it."""
    plan = plan_barge_in(timeline, -2000)

    assert plan.cancel_at_s == pytest.approx(0.0)
    assert plan.clipped is True
    assert plan.expected_heard_gating_word is False


def test_offset_past_audio_end_is_clipped(timeline):
    plan = plan_barge_in(timeline, 5000)

    assert plan.cancel_at_s == pytest.approx(timeline.duration)
    assert plan.clipped is True
    assert plan.expected_heard_gating_word is True


def test_in_range_offsets_are_not_clipped(timeline):
    for offset in (-500, -250, 0, 250, 500):
        assert plan_barge_in(timeline, offset).clipped is False


# --- failure modes: never guess -------------------------------------------


def test_missing_gating_word_raises(timeline):
    """A silent fallback here would grade every trial against the wrong instant."""
    with pytest.raises(KeyError):
        plan_barge_in(timeline, 0, gating_word="cancelled")


def test_empty_timeline_raises():
    with pytest.raises(ValueError, match="empty timeline"):
        plan_barge_in(WordTimeline([]), 0)


def test_occurrence_is_respected():
    tl = WordTimeline(
        [
            TimedWord("confirmed", 0.0, 0.4),
            TimedWord("and", 0.5, 0.6),
            TimedWord("confirmed", 0.7, 1.1),
        ]
    )
    assert plan_barge_in(tl, 0, occurrence=1).gating_word_end_s == pytest.approx(0.4)
    assert plan_barge_in(tl, 0, occurrence=2).gating_word_end_s == pytest.approx(1.1)


# --- the real fixture -----------------------------------------------------


def test_full_acceptance_ladder_stays_inside_the_real_fixture_audio():
    """The -500..+500ms ladder must not clip on the shipped fixture.

    If it did, offsets at the extremes would silently collapse onto the same
    cancellation instant and the sweep would over-report agreement.
    """
    tl = load_timeline()
    for offset in range(-500, 501, 10):
        assert plan_barge_in(tl, offset).clipped is False


def test_plan_dict_is_json_serialisable(timeline):
    import json

    payload = plan_barge_in(timeline, -120).to_dict()
    assert json.loads(json.dumps(payload))["offset_ms"] == -120.0
