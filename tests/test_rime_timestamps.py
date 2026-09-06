"""Tests for Rime timestamp parsing and the "what did the user hear" logic.

Everything here is offline: parsing a cached fixture and pure arithmetic.
"""

from __future__ import annotations

import json

import pytest

from shared.rime_timestamps import (
    DEFAULT_TIMELINE_FIXTURE,
    TimedWord,
    WordTimeline,
    heard_text_at,
    load_timeline,
    parse_rime_timestamps,
    synthetic_timeline,
)

REAL_SHAPE = {
    "type": "timestamps",
    "word_timestamps": {
        "words": ["Okay,", "you're", "confirmed", "for", "a", "table"],
        "start": [0.0, 0.5, 0.8, 1.4, 1.6, 1.7],
        "end": [0.45, 0.75, 1.35, 1.55, 1.68, 2.1],
    },
}


# --- parsing ------------------------------------------------------------


def test_parses_real_rime_frame_shape():
    words = parse_rime_timestamps(REAL_SHAPE)

    assert len(words) == 6
    assert words[2].text == "confirmed"
    assert words[2].start == pytest.approx(0.8)
    assert words[2].end == pytest.approx(1.35)


def test_parses_json_string():
    words = parse_rime_timestamps(json.dumps(REAL_SHAPE))
    assert [w.text for w in words] == REAL_SHAPE["word_timestamps"]["words"]


def test_parses_bare_word_timestamps_dict():
    words = parse_rime_timestamps(REAL_SHAPE["word_timestamps"])
    assert len(words) == 6


def test_parses_list_of_dicts():
    words = parse_rime_timestamps(
        [{"word": "hi", "start": 0.0, "end": 0.2}, {"text": "there", "start": 0.3, "end": 0.6}]
    )
    assert [w.text for w in words] == ["hi", "there"]


def test_ragged_arrays_rejected():
    with pytest.raises(ValueError, match="ragged"):
        parse_rime_timestamps({"word_timestamps": {"words": ["a", "b"], "start": [0.0], "end": []}})


def test_unrecognised_dict_shape_rejected():
    """An unknown payload must raise, not be silently guessed at."""
    with pytest.raises(ValueError, match="unrecognised"):
        parse_rime_timestamps({"alignment": {"chars": ["a"]}})


def test_unsupported_type_rejected():
    with pytest.raises(ValueError, match="unsupported"):
        parse_rime_timestamps(42)


def test_entry_missing_fields_rejected():
    with pytest.raises(ValueError, match="missing"):
        parse_rime_timestamps([{"word": "hi", "start": 0.0}])


def test_empty_payload_yields_empty_timeline():
    timeline = WordTimeline.from_rime_payload({"word_timestamps": {"words": [], "start": [], "end": []}})
    assert len(timeline) == 0
    assert not timeline
    assert timeline.duration == 0.0
    assert timeline.heard_text_by(5.0) == ""


def test_out_of_order_words_are_sorted():
    timeline = WordTimeline(
        [
            TimedWord("second", 1.0, 1.5),
            TimedWord("first", 0.0, 0.5),
        ]
    )
    assert [w.text for w in timeline] == ["first", "second"]


def test_negative_duration_word_rejected():
    with pytest.raises(ValueError, match="before it starts"):
        WordTimeline([TimedWord("bad", 1.0, 0.5)])


# --- the core question: what had the user heard? ------------------------


def test_words_heard_requires_full_playback_of_the_word():
    timeline = WordTimeline.from_rime_payload(REAL_SHAPE)

    # Mid-way through "confirmed" (0.8 -> 1.35): it has NOT been heard.
    assert [w.text for w in timeline.words_heard_by(1.1)] == ["Okay,", "you're"]
    # Exactly at its end boundary: heard.
    assert [w.text for w in timeline.words_heard_by(1.35)] == ["Okay,", "you're", "confirmed"]


def test_heard_text_before_anything_plays_is_empty():
    timeline = WordTimeline.from_rime_payload(REAL_SHAPE)
    assert timeline.heard_text_by(0.0) == ""
    assert timeline.heard_text_by(-1.0) == ""


def test_heard_text_after_end_is_everything():
    timeline = WordTimeline.from_rime_payload(REAL_SHAPE)
    assert timeline.heard_text_by(99.0) == timeline.text


def test_heard_text_at_helper_matches_method():
    timeline = WordTimeline.from_rime_payload(REAL_SHAPE)
    assert heard_text_at(timeline, 1.6) == timeline.heard_text_by(1.6)


# --- gating word lookup -------------------------------------------------


def test_find_word_ignores_punctuation_and_case():
    timeline = WordTimeline.from_rime_payload(REAL_SHAPE)
    assert timeline.find_word("okay").text == "Okay,"
    assert timeline.find_word("CONFIRMED").text == "confirmed"


def test_find_word_occurrence():
    timeline = WordTimeline.from_rime_payload(REAL_SHAPE)
    assert timeline.find_word("for", occurrence=1).start == pytest.approx(1.4)
    with pytest.raises(KeyError):
        timeline.find_word("for", occurrence=2)


def test_find_word_missing_raises():
    timeline = WordTimeline.from_rime_payload(REAL_SHAPE)
    with pytest.raises(KeyError, match="cancelled"):
        timeline.find_word("cancelled")


def test_find_word_rejects_bad_arguments():
    timeline = WordTimeline.from_rime_payload(REAL_SHAPE)
    with pytest.raises(ValueError):
        timeline.find_word("confirmed", occurrence=0)
    with pytest.raises(ValueError):
        timeline.find_word("!!!")


def test_gating_time_is_the_word_end():
    timeline = WordTimeline.from_rime_payload(REAL_SHAPE)
    assert timeline.gating_time("confirmed") == pytest.approx(1.35)


@pytest.mark.parametrize(
    ("cancel_at", "expected"),
    [
        (0.0, False),
        (1.0, False),
        (1.349, False),
        (1.35, True),
        (1.4, True),
        (float("inf"), True),
    ],
)
def test_heard_gating_word_boundary(cancel_at, expected):
    timeline = WordTimeline.from_rime_payload(REAL_SHAPE)
    assert timeline.heard_gating_word("confirmed", cancel_at) is expected


# --- the cached fixture -------------------------------------------------


def test_default_fixture_loads_and_contains_gating_word():
    timeline = load_timeline()

    assert DEFAULT_TIMELINE_FIXTURE.exists()
    assert len(timeline) > 0
    gating = timeline.find_word("confirmed")
    assert 0.0 < gating.end < timeline.duration
    # Sanity: offsets of +/-500ms around the gating word stay inside the audio,
    # otherwise the acceptance sweep would clip at the edges.
    assert gating.end - 0.5 > 0.0


def test_fixture_round_trips_through_to_dict():
    timeline = load_timeline()
    again = WordTimeline.from_rime_payload(timeline.to_dict())
    assert [w.to_dict() for w in again] == [w.to_dict() for w in timeline]


def test_fixture_timestamps_are_monotonic():
    timeline = load_timeline()
    for prev, nxt in zip(timeline.words, timeline.words[1:]):
        assert prev.start <= nxt.start
        assert prev.end <= nxt.end
        assert prev.end <= nxt.start + 1e-9


# --- synthetic timelines ------------------------------------------------


def test_synthetic_timeline_pacing():
    timeline = synthetic_timeline("one two three", words_per_second=2.0, gap=0.0)

    assert len(timeline) == 3
    assert timeline.words[0].start == pytest.approx(0.0)
    assert timeline.words[0].end == pytest.approx(0.5)
    assert timeline.words[1].start == pytest.approx(0.5)
    assert timeline.duration == pytest.approx(1.5)


def test_synthetic_timeline_rejects_bad_params():
    with pytest.raises(ValueError):
        synthetic_timeline("hi", words_per_second=0)
    with pytest.raises(ValueError):
        synthetic_timeline("hi", gap=-0.1)


def test_synthetic_timeline_empty_sentence():
    timeline = synthetic_timeline("   ")
    assert len(timeline) == 0


# --- overlapping alignment (regression) ---------------------------------


def _overlapping() -> WordTimeline:
    """A long word overlapping a shorter following one.

    Real TTS aligners emit this. Words sort by (start, end), so `end` is not
    monotonic here -- and a bisect over an unsorted array silently reports the
    unfinished long word as already heard, which would authorise exactly the
    phantom commit this project exists to catch.
    """
    return WordTimeline(
        [
            TimedWord("hellooooo", 0.0, 2.0),
            TimedWord("hi", 0.5, 0.8),
        ]
    )


def test_overlapping_words_do_not_report_an_unfinished_word_as_heard():
    timeline = _overlapping()

    assert timeline.heard_text_by(0.7) == ""
    # Only the short word finished; the long one is still playing.
    assert [w.text for w in timeline.words_heard_by(1.0)] == ["hi"]
    assert [w.text for w in timeline.words_heard_by(2.0)] == ["hellooooo", "hi"]


def test_duration_is_the_latest_end_not_the_last_words_end():
    assert _overlapping().duration == pytest.approx(2.0)


def test_gating_word_boundary_holds_under_overlap():
    timeline = _overlapping()

    assert timeline.gating_time("hellooooo") == pytest.approx(2.0)
    assert timeline.heard_gating_word("hellooooo", 1.999) is False
    assert timeline.heard_gating_word("hellooooo", 2.0) is True


def test_heard_words_stay_in_playback_order_under_overlap():
    timeline = WordTimeline(
        [
            TimedWord("a", 0.0, 3.0),
            TimedWord("b", 0.1, 0.2),
            TimedWord("c", 0.3, 0.4),
        ]
    )
    assert [w.text for w in timeline.words_heard_by(0.5)] == ["b", "c"]
    assert [w.text for w in timeline.words_heard_by(9.0)] == ["a", "b", "c"]


def test_monotonic_fixture_still_uses_the_fast_bisect_path():
    """The real fixture is monotonic, so the O(log n) path must stay engaged."""
    assert load_timeline()._ends_sorted is True
    assert _overlapping()._ends_sorted is False
