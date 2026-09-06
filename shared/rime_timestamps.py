"""Rime word-level timestamp parsing and "what did the user hear" reasoning.

Ground truth for this whole project is Rime's word-level timestamp stream. A
real Rime WebSocket/SSE ``timestamps`` frame looks like this (shape verified
against a single live call during Part 0, and identical to what
``livekit-plugins-rime`` parses in ``tts.py``)::

    {
      "type": "timestamps",
      "word_timestamps": {
        "words": ["Okay,", "you're", "confirmed", ...],
        "start": [0.0, 0.42, 0.71, ...],
        "end":   [0.40, 0.69, 1.22, ...]
      }
    }

Everything here is pure/offline: no network, no API keys, no cost. The chaos
harness replays a cached fixture of the above through these helpers, which is
how a 606-trial sweep runs without burning 606 TTS calls.
"""

from __future__ import annotations

import json
import re
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

__all__ = [
    "DEFAULT_TIMELINE_FIXTURE",
    "TimedWord",
    "WordTimeline",
    "heard_text_at",
    "load_timeline",
    "normalize_word",
    "parse_rime_timestamps",
    "synthetic_timeline",
]

DEFAULT_TIMELINE_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "rime_confirmation_timestamps.json"
)

_PUNCT_RE = re.compile(r"[^\w']+", re.UNICODE)


def normalize_word(word: str) -> str:
    """Lowercase and strip punctuation so ``"confirmed,"`` matches ``"confirmed"``."""
    return _PUNCT_RE.sub("", word.strip().lower())


# Backwards-compatible private alias.
_normalize = normalize_word


@dataclass(frozen=True)
class TimedWord:
    """One spoken word with its playback window, in seconds from audio start."""

    text: str
    start: float
    end: float

    @property
    def normalized(self) -> str:
        return normalize_word(self.text)

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "start": self.start, "end": self.end}


class WordTimeline:
    """An ordered, validated sequence of :class:`TimedWord`.

    Provides the single question the fencing logic cares about: at playback
    time *t*, which words had actually reached the caller's ear?
    """

    def __init__(self, words: Iterable[TimedWord]) -> None:
        ordered = sorted(words, key=lambda w: (w.start, w.end))
        for w in ordered:
            if w.end < w.start:
                raise ValueError(f"word {w.text!r} ends ({w.end}) before it starts ({w.start})")
        self._words: tuple[TimedWord, ...] = tuple(ordered)

        # Precomputed for bisect-based lookups; a 606-trial sweep does this a lot.
        #
        # Words are ordered by (start, end), so `end` is NOT guaranteed to be
        # ascending: a long word can overlap a shorter following one, which real
        # TTS aligners do emit. Bisecting an unsorted array silently returns the
        # wrong index -- and here that means reporting a word that had not
        # finished playing as *heard*, i.e. authorising exactly the phantom
        # commit this project exists to catch. So bisect only when `end` really
        # is monotonic, and fall back to an explicit scan when it is not.
        ends = tuple(w.end for w in self._words)
        self._ends: tuple[float, ...] = ends
        self._ends_sorted: bool = all(a <= b for a, b in zip(ends, ends[1:]))

    # -- construction ----------------------------------------------------

    @classmethod
    def from_rime_payload(cls, payload: Any) -> WordTimeline:
        return cls(parse_rime_timestamps(payload))

    @classmethod
    def from_json_file(cls, path: str | Path) -> WordTimeline:
        raw = Path(path).read_text(encoding="utf-8")
        return cls.from_rime_payload(json.loads(raw))

    # -- basics ----------------------------------------------------------

    @property
    def words(self) -> tuple[TimedWord, ...]:
        return self._words

    def __len__(self) -> int:
        return len(self._words)

    def __iter__(self):
        return iter(self._words)

    def __bool__(self) -> bool:
        return bool(self._words)

    @property
    def duration(self) -> float:
        """End of the audio: the LATEST end timestamp (0.0 when empty).

        Not simply ``words[-1].end`` -- with overlapping alignment the final
        word by start time can finish before an earlier, longer one.
        """
        return max(self._ends) if self._words else 0.0

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self._words)

    # -- the core questions ---------------------------------------------

    def words_heard_by(self, t: float) -> tuple[TimedWord, ...]:
        """Words fully played by time ``t`` seconds.

        A word counts as heard only once its *end* timestamp has passed: a word
        cut off mid-syllable was not heard, and treating it as heard is exactly
        the bug this project exists to catch.

        Words are returned in playback order. With overlapping alignment a word
        can be skipped while a later one is included -- that is correct: the
        caller heard the short word and not the long one still in progress.
        """
        if self._ends_sorted:
            return self._words[: bisect_right(self._ends, t)]
        return tuple(w for w in self._words if w.end <= t)

    def heard_text_by(self, t: float) -> str:
        return " ".join(w.text for w in self.words_heard_by(t))

    def find_word(self, target: str, occurrence: int = 1) -> TimedWord:
        """Return the ``occurrence``-th word matching ``target`` (punctuation-insensitive)."""
        if occurrence < 1:
            raise ValueError(f"occurrence must be >= 1, got {occurrence}")
        needle = normalize_word(target)
        if not needle:
            raise ValueError("target word must contain at least one word character")
        seen = 0
        for w in self._words:
            if w.normalized == needle:
                seen += 1
                if seen == occurrence:
                    return w
        raise KeyError(
            f"word {target!r} (occurrence {occurrence}) not found in timeline: {self.text!r}"
        )

    def gating_time(self, gating_word: str, occurrence: int = 1) -> float:
        """End timestamp of the gating word -- the instant the fence may open."""
        return self.find_word(gating_word, occurrence).end

    def heard_gating_word(
        self,
        gating_word: str,
        cancel_at: float,
        occurrence: int = 1,
    ) -> bool:
        """Ground truth: was the gating word fully played before ``cancel_at``?

        ``cancel_at`` is playback-relative seconds. ``float("inf")`` means the
        speech was never cancelled.
        """
        return cancel_at >= self.gating_time(gating_word, occurrence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "word_timestamps": {
                "words": [w.text for w in self._words],
                "start": [w.start for w in self._words],
                "end": [w.end for w in self._words],
            }
        }

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"WordTimeline({len(self._words)} words, {self.duration:.3f}s)"


def parse_rime_timestamps(payload: Any) -> list[TimedWord]:
    """Parse a Rime ``timestamps`` payload into :class:`TimedWord` objects.

    Accepts, in order of preference:

    * a raw JSON string;
    * a dict with ``word_timestamps`` (the real Rime frame);
    * a bare ``{"words": [...], "start": [...], "end": [...]}`` dict;
    * a list of ``{"text"/"word", "start", "end"}`` dicts (our own fixtures);
    * a list of already-built :class:`TimedWord`.

    Only these documented shapes are accepted -- an unrecognised payload raises
    rather than being guessed at, because a silently mis-parsed timeline would
    invalidate every downstream mismatch verdict.
    """
    if isinstance(payload, (str, bytes, bytearray)):
        payload = json.loads(payload)

    if isinstance(payload, WordTimeline):
        return list(payload.words)

    if isinstance(payload, dict):
        block = payload.get("word_timestamps", payload)
        if not isinstance(block, dict):
            raise ValueError("'word_timestamps' must be an object")
        if "words" not in block:
            raise ValueError(
                "unrecognised Rime timestamp payload: expected a 'word_timestamps' object "
                f"with a 'words' list, got keys {sorted(block)!r}"
            )
        words = block.get("words") or []
        starts = block.get("start") or block.get("starts") or []
        ends = block.get("end") or block.get("ends") or []
        if not (len(words) == len(starts) == len(ends)):
            raise ValueError(
                "Rime timestamp arrays are ragged: "
                f"words={len(words)} start={len(starts)} end={len(ends)}"
            )
        return [
            TimedWord(text=str(w), start=float(s), end=float(e))
            for w, s, e in zip(words, starts, ends)
        ]

    if isinstance(payload, Sequence):
        out: list[TimedWord] = []
        for item in payload:
            if isinstance(item, TimedWord):
                out.append(item)
                continue
            if not isinstance(item, dict):
                raise ValueError(f"unsupported timestamp entry: {item!r}")
            text = item.get("text", item.get("word"))
            if text is None or "start" not in item or "end" not in item:
                raise ValueError(f"timestamp entry missing text/start/end: {item!r}")
            out.append(
                TimedWord(text=str(text), start=float(item["start"]), end=float(item["end"]))
            )
        return out

    raise ValueError(f"unsupported Rime timestamp payload type: {type(payload).__name__}")


def load_timeline(path: str | Path | None = None) -> WordTimeline:
    """Load the cached confirmation timeline fixture (offline, free)."""
    return WordTimeline.from_json_file(path or DEFAULT_TIMELINE_FIXTURE)


def synthetic_timeline(
    sentence: str,
    *,
    words_per_second: float = 2.6,
    gap: float = 0.06,
    start_at: float = 0.0,
) -> WordTimeline:
    """Build an evenly-paced timeline from a sentence.

    Only for unit tests and for the harness' ``dummy`` target -- real trials use
    the cached Rime fixture, which carries genuine per-word pacing.
    """
    if words_per_second <= 0:
        raise ValueError("words_per_second must be positive")
    if gap < 0:
        raise ValueError("gap must be non-negative")

    tokens = sentence.split()
    span = 1.0 / words_per_second
    words: list[TimedWord] = []
    cursor = start_at
    for token in tokens:
        words.append(TimedWord(text=token, start=cursor, end=cursor + span))
        cursor += span + gap
    return WordTimeline(words)


def heard_text_at(timeline: WordTimeline, t: float) -> str:
    """Convenience wrapper: the transcript the caller had actually heard at ``t``."""
    return timeline.heard_text_by(t)
