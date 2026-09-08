"""Service layer: drives the REAL project modules on behalf of the web UI.

Everything the browser displays is computed here by calling the existing
project code. In particular:

* the confirmation timeline comes from ``shared.rime_timestamps.load_timeline``
  (the cached real-shape Rime fixture);
* "what did the caller hear" comes from ``WordTimeline.words_heard_by``;
* the commit decision comes from ``shared.audio_fence.AudioFence`` -- the same
  object the live fenced agent wires LiveKit events into;
* the verdict comes from ``shared.audio_fence.evaluate_ground_truth``, the
  independent oracle, so the UI never grades the fence with the fence's logic;
* rows are written to the real SQLite store in ``shared.booking_store``;
* sweeps run through ``chaos_harness.driver.run_sweep`` and are summarised by
  ``reporting.dashboard.summarize``.

Nothing here re-implements the fencing rule. If this module and the fence ever
disagree, the fence wins, because this module only forwards events to it.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from chaos_harness.driver import SweepConfig, load_target_timeline, run_sweep
from chaos_harness.injector import plan_barge_in
from chaos_harness.trial_log import read_trials
from reporting.chart import mismatch_by_offset, render_mismatch_chart
from reporting.dashboard import summarize
from shared import booking_store as bs
from shared.audio_fence import AudioFence, FenceState, evaluate_ground_truth
from shared.constants import (
    CONFIRMATION_GATING_WORD,
    RESTAURANT_NAME,
    SWEEP_OFFSET_MAX_MS,
    SWEEP_OFFSET_MIN_MS,
    SWEEP_OFFSET_STEP_MS,
    SWEEP_RUNS_PER_OFFSET,
    SWEEP_TRIALS_PER_TARGET,
    TEST_BOOKING_REQUEST,
    TEST_BOOKING_TIME,
    TEST_PARTY_SIZE,
    VARIANT_FENCED,
    VARIANT_NAIVE,
    confirmation_sentence,
)
from shared.rime_timestamps import TimedWord, WordTimeline, load_timeline

__all__ = [
    "CallSession",
    "SessionStore",
    "DemoService",
    "PROJECT_ROOT",
]

logger = logging.getLogger("blackbox_audit.webui.service")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = PROJECT_ROOT / "results"

# The demo writes to its own database file so a judge clicking around in the
# browser can never contaminate the acceptance-sweep numbers in
# results/booking.db. Same module, same schema, same code path -- only the
# file differs.
DEMO_DB_PATH = RESULTS_DIR / "webui_demo.db"

_VALID_VARIANTS = (VARIANT_NAIVE, VARIANT_FENCED)


def configure_demo_db() -> str:
    """Point ``shared.booking_store`` at the UI's own database file."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    bs.set_db_path(DEMO_DB_PATH)
    bs.init_db()
    return str(DEMO_DB_PATH)


# ---------------------------------------------------------------------------
# Live "phone call" sessions
# ---------------------------------------------------------------------------


@dataclass
class CallSession:
    """One simulated phone call, fenced by the real ``AudioFence``.

    The browser owns playback (Web Speech API or a silent timer) and reports
    its playback position back here. This object translates those positions
    into the four abstract fence events, exactly as ``fenced_agent`` translates
    LiveKit events into them.
    """

    session_id: str
    variant: str
    party_size: int
    time_str: str
    timeline: WordTimeline
    gating_word: str = CONFIRMATION_GATING_WORD
    occurrence: int = 1

    booking_id: int | None = None
    fence: AudioFence | None = None
    created_at: float = field(default_factory=time.perf_counter)
    resolved_at: float | None = None
    cancelled_at_s: float | None = None
    completed: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)
    _fed: int = 0

    # -- lifecycle -------------------------------------------------------

    @property
    def sentence(self) -> str:
        return confirmation_sentence(self.party_size, self.time_str)

    @property
    def gating_word_end_s(self) -> float:
        return self.timeline.gating_time(self.gating_word, occurrence=self.occurrence)

    def log_event(self, kind: str, detail: str, *, at_s: float | None = None) -> None:
        self.events.append(
            {
                "kind": kind,
                "detail": detail,
                "at_s": at_s,
                "t_ms": round((time.perf_counter() - self.created_at) * 1000.0, 3),
            }
        )

    def start(self) -> None:
        """Execute the LLM tool call -- the only step the two variants differ in."""
        if self.variant == VARIANT_NAIVE:
            # THE BUG, ON PURPOSE: committed before a word has been spoken.
            self.booking_id = bs.book_naive(
                self.party_size, self.time_str, agent_variant=VARIANT_NAIVE
            )
            self.log_event(
                "tool_call",
                f"book_naive() -> row #{self.booking_id} COMMITTED immediately "
                "(nothing spoken yet)",
                at_s=0.0,
            )
            return

        self.booking_id = bs.create_pending_booking(
            self.party_size, self.time_str, agent_variant=VARIANT_FENCED
        )
        self.fence = AudioFence(
            gating_word=self.gating_word,
            store=bs,
            occurrence=self.occurrence,
        )
        self.fence.on_pending(self.booking_id)
        self.log_event(
            "tool_call",
            f"create_pending_booking() -> row #{self.booking_id} PENDING_AUDIO "
            f"(fence armed on \u201c{self.gating_word}\u201d)",
            at_s=0.0,
        )

    # -- playback --------------------------------------------------------

    def advance(self, position_s: float) -> None:
        """Feed every word fully played by ``position_s`` into the fence.

        A word only counts once its *end* timestamp has passed: a word cut off
        mid-syllable was not heard. That rule lives in ``WordTimeline``; this
        method just respects it.
        """
        words = self.timeline.words
        while self._fed < len(words) and words[self._fed].end <= position_s + 1e-9:
            word = words[self._fed]
            self._fed += 1
            if self.fence is not None:
                self.fence.on_timed_word(word.text, word.start, word.end)
            if word.normalized == self.gating_word.lower():
                self.log_event(
                    "gating_word_heard",
                    f"caller finished hearing \u201c{word.text}\u201d at {word.end:.3f}s",
                    at_s=word.end,
                )

    def barge_in(self, position_s: float) -> None:
        """The caller interrupts mid-confirmation."""
        self.advance(position_s)
        self.cancelled_at_s = position_s
        self.log_event("barge_in", f"caller interrupted at {position_s:.3f}s", at_s=position_s)

        if self.fence is not None:
            self.fence.on_playback_finished(position=position_s, interrupted=True)
            self.fence.on_session_closed()
        else:
            self.log_event(
                "no_fence",
                "naive agent has no resolution step -- the row was already COMMITTED",
                at_s=position_s,
            )
        self.resolved_at = time.perf_counter()

    def finish(self, position_s: float | None = None) -> None:
        """Playback ran to completion without interruption."""
        end = self.timeline.duration if position_s is None else position_s
        self.advance(end)
        self.completed = True
        self.log_event("playback_complete", f"confirmation played in full ({end:.3f}s)", at_s=end)

        if self.fence is not None:
            self.fence.on_playback_finished(position=end, interrupted=False)
            self.fence.on_session_closed()
        self.resolved_at = time.perf_counter()

    def hang_up(self) -> None:
        """Caller hung up: nothing may be left pending."""
        self.log_event("hang_up", "caller hung up before the confirmation finished")
        if self.fence is not None:
            self.fence.on_session_closed()
        self.resolved_at = time.perf_counter()

    # -- reporting -------------------------------------------------------

    def db_status(self) -> str:
        if self.booking_id is None:
            return "MISSING"
        try:
            return bs.get_booking(self.booking_id)["status"]
        except bs.BookingNotFoundError:
            return "MISSING"

    def expected_committed(self) -> bool:
        """Independent oracle: should a booking exist, given what was heard?"""
        return evaluate_ground_truth(
            self.timeline,
            self.gating_word,
            self.cancelled_at_s,
            occurrence=self.occurrence,
        )

    def heard_text(self) -> str:
        cutoff = self.cancelled_at_s if self.cancelled_at_s is not None else self.timeline.duration
        return self.timeline.heard_text_by(cutoff)

    def to_dict(self) -> dict[str, Any]:
        status = self.db_status()
        actually = status == bs.STATUS_COMMITTED
        expected = self.expected_committed()
        resolved = self.resolved_at is not None
        fence = self.fence

        return {
            "session_id": self.session_id,
            "variant": self.variant,
            "restaurant": RESTAURANT_NAME,
            "party_size": self.party_size,
            "time_str": self.time_str,
            "sentence": self.sentence,
            "gating_word": self.gating_word,
            "gating_word_end_s": self.gating_word_end_s,
            "audio_duration_s": self.timeline.duration,
            "booking_id": self.booking_id,
            "db_status": status,
            "actually_committed": actually,
            "expected_committed": expected,
            "mismatch": resolved and (actually is not expected),
            "resolved": resolved,
            "cancelled_at_s": self.cancelled_at_s,
            "completed": self.completed,
            "heard_text": self.heard_text(),
            "words_heard": self._fed,
            "fence_state": (fence.state.value if fence else None),
            "fence_outcome": (fence.outcome.value if fence and fence.outcome else None),
            "resolution_latency_ms": (
                round((self.resolved_at - self.created_at) * 1000.0, 3) if resolved else None
            ),
            "events": list(self.events),
            "transaction_log": (
                bs.get_transaction_log(self.booking_id) if self.booking_id is not None else []
            ),
        }


@dataclass
class BrowserCallSession(CallSession):
    """Browser segment acknowledgements, NOT cached Rime timestamps.

    Whole segments count only after speechSynthesis.onend. This deliberately
    conservative demo adapter exercises the original AudioFence and SQLite.
    Browser acknowledgements are untrusted telemetry, not proof of human hearing.
    """

    acknowledged: int = 0
    last_position: float = 0.0
    lock: Any = field(default_factory=threading.RLock, repr=False)

    @property
    def segments(self) -> list[str]:
        return [f"Your table for {self.party_size} at {self.time_str} is", "confirmed.", "Thank you."]

    @property
    def sentence(self) -> str:
        return " ".join(self.segments)

    @property
    def gating_word_end_s(self) -> float | None:
        return next((w.end for w in self.timeline.words if w.normalized == self.gating_word), None)

    def expected_committed(self) -> bool:
        return self.gating_word_end_s is not None

    def heard_text(self) -> str:
        return self.timeline.text

    def acknowledge(self, index: int, position: float) -> None:
        if self.resolved_at is not None:
            return
        if index < self.acknowledged:
            return  # idempotent retry
        if index != self.acknowledged or index >= len(self.segments):
            raise ValueError("Audio segments must be acknowledged in order")
        if position <= self.last_position:
            raise ValueError("Playback position must increase")
        words = list(self.timeline.words)
        for token in self.segments[index].split():
            words.append(TimedWord(token, self.last_position, position))
        self.timeline = WordTimeline(words)
        self.advance(position)
        self.last_position = position
        self.acknowledged += 1
        self.log_event("browser_audio_end", f"Segment {index + 1} completed: {self.segments[index]}", at_s=position)

    def close_browser(self, action: str, position: float) -> None:
        if self.resolved_at is not None:
            return
        position = max(position, self.last_position)
        if action == "complete":
            if self.acknowledged != len(self.segments):
                raise ValueError("Cannot complete before all audio segments are acknowledged")
            self.finish(position)
        else:
            self.barge_in(position)

    def hang_up(self) -> None:
        self.close_browser("interrupt", self.last_position)

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data.update(mode="browser_voice", segments=self.segments,
                    acknowledged_segments=self.acknowledged,
                    evidence="browser speech-synthesis segment completion; not Rime alignment")
        return data


def parse_booking_text(text: str, party_size: int | None = None,
                       time_str: str | None = None) -> dict[str, Any]:
    """API-free booking dialogue. Latest non-negated slot wins; never guess AM/PM.

    Partial slots are returned even on clarification. An unqualified time is
    retained without a period so a subsequent 'PM' can complete it, rather than
    silently restoring the previous booking time.
    """
    normalized = text.lower().replace("’", "'")
    normalized = re.sub(r"\b([ap])\s*\.?\s*m\.?", r"\1m", normalized)
    units = dict(zip(
        "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split(),
        range(20),
    ))
    tens = dict(zip("twenty thirty forty fifty sixty seventy eighty ninety".split(), range(20, 100, 10)))
    for word, value in tens.items():
        normalized = re.sub(r"\b" + word + r"[- ](" + "|".join(list(units)[1:10]) + r")\b",
                            lambda m: str(value + units[m[1]]), normalized)
    for word, value in {**units, **tens}.items():
        normalized = re.sub(r"\b" + word + r"\b", str(value), normalized)
    normalized = re.sub(r"\bnoon\b", "12 pm", normalized)
    normalized = re.sub(r"\bmidnight\b", "12 am", normalized)
    normalized = re.sub(r"\b(\d{1,2})\s+o'?clock\b", r"\1", normalized)
    normalized = re.sub(r"\b(\d{1,2})\s+(\d{2})(?=\s*(?:am|pm)\b)", r"\1:\2", normalized)
    normalized = re.sub(r"\b(?:in the |this )?(?:evening|afternoon|tonight)\b", "pm", normalized)
    normalized = re.sub(r"\b(?:in the |this )?morning\b", "am", normalized)
    # '8 PM instead of 7 PM' and '8 PM, not 7 PM' must not choose the old slot.
    normalized = re.sub(r"\b(?:instead of|not)\s+\d+(?::\d+)?\s*(?:am|pm|people|guests)?", "", normalized)

    def result(reply: str, **extra: Any) -> dict[str, Any]:
        return {"ready": False, "party_size": party_size, "time_str": time_str,
                "reply": reply, "engine": "local-booking-dialogue", **extra}

    if re.search(r"\b(?:cancel|never mind|nevermind)\b", normalized):
        return result("I will cancel this conversation's booking and keep its audit history.", cancel=True)
    if re.search(r"\b(?:wait|stop|hold on)\b", normalized) and not re.search(r"\d", normalized):
        return result("I've stopped speaking. What would you like to change?")
    if re.search(r"\b(?:new|another|separate)\s+(?:booking|table|reservation)\b", normalized):
        party_size, time_str = None, None

    parties = []
    for pattern in (
        r"\b(?:table for|party of|for|we are|we're|there are|there will be|make (?:it|that)|change (?:it|that) to)\s+(\d{1,3})\b",
        r"\b(\d{1,3})\s+(?:people|persons|guests|of us|seats)\b",
    ):
        for match in re.finditer(pattern, normalized):
            # 'for 8 PM' is a time, not a party size; generic 'make it 8' is ambiguous.
            suffix = normalized[match.end():]
            if re.match(r"\s*(?::|am\b|pm\b)", suffix):
                continue
            if match[0].startswith(("make", "change")) and not re.match(r"\s+(?:people|persons|guests|of us|seats)\b", suffix):
                continue
            parties.append(match)
    party = max(parties, key=lambda m: m.start(), default=None)
    if party:
        party_size = int(party[1])

    times = list(re.finditer(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", normalized))
    for match in re.finditer(r"\b(?:at|for|make (?:it|that)|change (?:it|that) to|move (?:it|that) to)\s+(\d{1,2})(?::(\d{2}))?(?:\s*(am|pm))?\b", normalized):
        if any(p.start() <= match.start() < p.end() for p in parties):
            continue
        if re.match(r"\s+(?:people|persons|guests|of us|seats)\b", normalized[match.end():]):
            continue
        times.append(match)
    at = max(times, key=lambda m: m.start(), default=None)
    if not party and not at:
        bare = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*[.!?]?\s*", normalized)
        if bare and party_size is None and bare[2] is None:
            party_size = int(bare[1])
            party = bare
        elif bare and party_size is not None:
            hour, minute = bare[1], bare[2] or "00"
            normalized = f"at {hour}:{minute}"
            at = re.search(r"at (\d{1,2}):(\d{2})(am|pm)?", normalized)
    if at:
        hour, minute, period = int(at[1]), int(at[2] or 0), at[3]
        if hour > 23 or minute > 59 or (period and not 1 <= hour <= 12):
            time_str = None
            return result("Please give a valid time, including AM or PM.")
        time_str = f"{hour}:{minute:02d}" + (f" {period.upper()}" if period else "")
    else:
        period = re.fullmatch(r"\s*(am|pm)[.!]?\s*", normalized)
        if period and time_str and re.fullmatch(r"\d{1,2}:\d{2}", time_str):
            hour = int(time_str.split(":")[0])
            if 1 <= hour <= 12:
                time_str += " " + period[1].upper()
                at = period

    if party_size is not None and not 1 <= party_size <= 99:
        party_size = None
        return result("Please give a party size between 1 and 99.")
    if time_str and re.fullmatch(r"(?:[1-9]|1[0-2]):\d{2}", time_str):
        return result(f"Is {time_str} AM or PM?", awaiting="period")
    changed = party is not None or at is not None
    if not changed and not re.search(r"\b(?:book|table|reservation)\b", normalized):
        if re.search(r"\b(?:thanks|thank you|yes|correct|that's right)\b", normalized):
            return result("You're welcome. Your current booking details are shown in the audit.")
        return result("Hello! I can help with a table reservation. How many people and what time? You can also correct the size or time.")
    if not party_size:
        return result("How many people is the table for?", awaiting="party_size")
    if not time_str:
        return result(f"What time would you like the table for {party_size}? Include AM or PM.", awaiting="time")
    return result(f"Table for {party_size} at {time_str}.", ready=True)


class SessionStore:
    """Bounded, thread-safe registry of demo sessions.

    Bounded because this is a public demo URL: an unbounded dict is a memory
    leak with a share link attached.
    """

    def __init__(self, max_sessions: int = 256) -> None:
        self._sessions: dict[str, CallSession] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self._max = max_sessions

    def add(self, session: CallSession) -> None:
        with self._lock:
            self._sessions[session.session_id] = session
            self._order.append(session.session_id)
            while len(self._order) > self._max:
                oldest = self._order.pop(0)
                expired = self._sessions.pop(oldest, None)
                if expired and expired.resolved_at is None:
                    expired.hang_up()

    def get(self, session_id: str) -> CallSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"unknown session {session_id!r}")
        return session

    def recent(self, limit: int = 20) -> list[CallSession]:
        with self._lock:
            ids = list(reversed(self._order[-limit:]))
            return [self._sessions[i] for i in ids if i in self._sessions]

    def clear(self) -> int:
        with self._lock:
            n = len(self._sessions)
            self._sessions.clear()
            self._order.clear()
        return n


# ---------------------------------------------------------------------------
# The service facade the HTTP layer talks to
# ---------------------------------------------------------------------------


class DemoService:
    """Everything the API needs, expressed as calls into the real modules."""

    def __init__(self) -> None:
        self.db_path = configure_demo_db()
        self.sessions = SessionStore()
        self._sweep_lock = threading.Lock()
        self._timeline: WordTimeline | None = None

    # -- timeline / config ----------------------------------------------

    def timeline(self) -> WordTimeline:
        if self._timeline is None:
            self._timeline = load_timeline()
        return self._timeline

    def timeline_payload(self) -> dict[str, Any]:
        tl = self.timeline()
        gating_end = tl.gating_time(CONFIRMATION_GATING_WORD)
        return {
            "sentence": tl.text,
            "duration_s": tl.duration,
            "gating_word": CONFIRMATION_GATING_WORD,
            "gating_word_end_s": gating_end,
            "words": [
                {
                    "text": w.text,
                    "start": w.start,
                    "end": w.end,
                    "is_gating": w.normalized == CONFIRMATION_GATING_WORD,
                }
                for w in tl.words
            ],
            "provenance": self._fixture_provenance(),
        }

    def _fixture_provenance(self) -> dict[str, Any]:
        """Report honestly whether the fixture is a live capture or placeholder."""
        import json

        from shared.rime_timestamps import DEFAULT_TIMELINE_FIXTURE

        try:
            raw = json.loads(Path(DEFAULT_TIMELINE_FIXTURE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"provenance": "unknown", "live_capture": False}
        provenance = str(raw.get("_provenance", "unknown"))
        return {
            "provenance": provenance,
            "live_capture": provenance != "placeholder-pacing",
            "model": raw.get("_model"),
            "speaker": raw.get("_speaker"),
            "note": (
                "Word-timestamp SHAPE is the verified real Rime frame; this "
                "fixture's pacing is placeholder. Run "
                "`python scripts/test_rime.py --save-fixture` with a Rime key "
                "to replace it with a live capture."
                if provenance == "placeholder-pacing"
                else "Captured from a live Rime call."
            ),
        }

    def config_payload(self) -> dict[str, Any]:
        """Which live providers are configured, and what the demo does without them."""
        keys = {
            "RIME_API_KEY": "Rime TTS (word-level timestamps = ground truth)",
            "DEEPGRAM_API_KEY": "Deepgram STT",
            "OPENAI_API_KEY": "OpenAI LLM (tool calling)",
            "LIVEKIT_URL": "LiveKit room transport (mobile client)",
            "LIVEKIT_API_KEY": "LiveKit auth",
            "LIVEKIT_API_SECRET": "LiveKit auth",
        }
        providers = [
            {
                "key": name,
                "purpose": purpose,
                "configured": bool(os.environ.get(name, "").strip()),
            }
            for name, purpose in keys.items()
        ]
        return {
            "providers": providers,
            "any_configured": any(p["configured"] for p in providers),
            "demo_mode": "replay",
            "demo_mode_note": (
                "No API keys are needed for this demo. Speech is synthesised "
                "on-device by the browser's free Web Speech API and paced by the "
                "cached Rime word timeline; the commit decision runs through the "
                "real AudioFence and the real SQLite store."
            ),
            "db_path": self.db_path,
            "acceptance": {
                "offset_min_ms": SWEEP_OFFSET_MIN_MS,
                "offset_max_ms": SWEEP_OFFSET_MAX_MS,
                "offset_step_ms": SWEEP_OFFSET_STEP_MS,
                "runs_per_offset": SWEEP_RUNS_PER_OFFSET,
                "trials_per_target": SWEEP_TRIALS_PER_TARGET,
            },
            "scenario": {
                "restaurant": RESTAURANT_NAME,
                "caller_request": TEST_BOOKING_REQUEST,
                "party_size": TEST_PARTY_SIZE,
                "time_str": TEST_BOOKING_TIME,
            },
        }

    # -- sessions --------------------------------------------------------

    def start_call(
        self,
        variant: str,
        party_size: int = TEST_PARTY_SIZE,
        time_str: str = TEST_BOOKING_TIME,
    ) -> dict[str, Any]:
        if variant not in _VALID_VARIANTS:
            raise ValueError(f"variant must be one of {_VALID_VARIANTS}, got {variant!r}")
        session = CallSession(
            session_id=uuid.uuid4().hex[:12],
            variant=variant,
            party_size=party_size,
            time_str=time_str,
            timeline=self.timeline(),
        )
        session.start()
        self.sessions.add(session)
        logger.info("call %s started (variant=%s)", session.session_id, variant)
        return session.to_dict()

    def advance_call(self, session_id: str, position_s: float) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        if isinstance(session, BrowserCallSession):
            raise ValueError("Browser voice sessions require ordered /api/voice/event telemetry")
        session.advance(position_s)
        return session.to_dict()

    def barge_in(self, session_id: str, position_s: float) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        if isinstance(session, BrowserCallSession):
            raise ValueError("Browser voice sessions require ordered /api/voice/event telemetry")
        session.barge_in(position_s)
        return session.to_dict()

    def finish_call(self, session_id: str, position_s: float | None = None) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        if isinstance(session, BrowserCallSession):
            raise ValueError("Browser voice sessions require ordered /api/voice/event telemetry")
        session.finish(position_s)
        return session.to_dict()

    def hang_up(self, session_id: str) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        session.hang_up()
        return session.to_dict()

    def session(self, session_id: str) -> dict[str, Any]:
        return self.sessions.get(session_id).to_dict()

    def recent_calls(self, limit: int = 20) -> list[dict[str, Any]]:
        return [
            {
                "session_id": s.session_id,
                "variant": s.variant,
                "booking_id": s.booking_id,
                "db_status": s.db_status(),
                "mismatch": (
                    s.resolved_at is not None
                    and ((s.db_status() == bs.STATUS_COMMITTED) is not s.expected_committed())
                ),
                "cancelled_at_s": s.cancelled_at_s,
                "heard_text": s.heard_text(),
                "fence_outcome": (s.fence.outcome.value if s.fence and s.fence.outcome else None),
            }
            for s in self.sessions.recent(limit)
        ]

    # -- "what did the caller hear" -------------------------------------

    def heard_at(self, at_s: float) -> dict[str, Any]:
        tl = self.timeline()
        heard = tl.words_heard_by(at_s)
        gating_end = tl.gating_time(CONFIRMATION_GATING_WORD)
        return {
            "at_s": at_s,
            "heard_text": " ".join(w.text for w in heard),
            "words_heard": len(heard),
            "total_words": len(tl),
            "heard_gating_word": at_s >= gating_end,
            "gating_word_end_s": gating_end,
            "should_commit": evaluate_ground_truth(tl, CONFIRMATION_GATING_WORD, at_s),
        }

    def plan(self, offset_ms: float) -> dict[str, Any]:
        """Expose the injector's arithmetic: offset -> cancellation instant."""
        p = plan_barge_in(self.timeline(), offset_ms, gating_word=CONFIRMATION_GATING_WORD)
        payload = p.to_dict()
        payload["expected_heard_gating_word"] = p.expected_heard_gating_word
        payload["heard_text"] = (
            self.timeline().heard_text_by(p.cancel_at_s) if p.cancels else self.timeline().text
        )
        return payload

    # -- sweeps ----------------------------------------------------------

    def run_sweep(
        self,
        target: str,
        *,
        min_ms: float = float(SWEEP_OFFSET_MIN_MS),
        max_ms: float = float(SWEEP_OFFSET_MAX_MS),
        step_ms: float = float(SWEEP_OFFSET_STEP_MS),
        runs_per_offset: int = SWEEP_RUNS_PER_OFFSET,
    ) -> dict[str, Any]:
        """Run a real sweep through ``chaos_harness.driver.run_sweep``."""
        if target not in _VALID_VARIANTS:
            raise ValueError(f"target must be one of {_VALID_VARIANTS}, got {target!r}")
        if step_ms <= 0:
            raise ValueError("step_ms must be positive")
        if max_ms < min_ms:
            raise ValueError("max_ms must be >= min_ms")
        if not 1 <= runs_per_offset <= SWEEP_RUNS_PER_OFFSET:
            raise ValueError(f"runs_per_offset must be 1..{SWEEP_RUNS_PER_OFFSET}")

        offsets: list[float] = []
        cursor = float(min_ms)
        while cursor <= float(max_ms) + 1e-9:
            offsets.append(round(cursor, 6))
            cursor += float(step_ms)
        if len(offsets) > 401:
            raise ValueError("too many offsets for one request; increase step_ms")

        config = SweepConfig(
            target=target,
            offsets_ms=offsets,
            runs_per_offset=runs_per_offset,
            results_dir=RESULTS_DIR,
        )
        # Serialised: two concurrent sweeps would interleave rows in the shared
        # SQLite store and make the orphan audit meaningless.
        with self._sweep_lock:
            started = time.perf_counter()
            result = run_sweep(config)
            elapsed_ms = (time.perf_counter() - started) * 1000.0

        by_offset = mismatch_by_offset(result.records).get(target, {})
        return {
            "target": result.target,
            "trials": result.trials,
            "mismatches": result.mismatches,
            "orphans": result.orphans,
            "passed": result.passed,
            "summary_line": result.summary_line(),
            "log_path": str(result.log_path),
            "elapsed_ms": round(elapsed_ms, 1),
            "offsets": [{"offset_ms": o, "mismatch_rate": r} for o, r in sorted(by_offset.items())],
            "records": [
                {
                    "trial_id": r.trial_id,
                    "offset_ms": r.offset_ms,
                    "booking_id": r.booking_id,
                    "db_status_after": r.db_status_after,
                    "mismatch": r.mismatch,
                    "expected_committed": r.expected_committed,
                    "actually_committed": r.actually_committed,
                    "cancel_latency_ms": r.cancel_latency_ms,
                    "cancel_at_s": r.cancel_at_s,
                    "heard_text": r.heard_text,
                    "fence_outcome": r.fence_outcome,
                }
                for r in result.records
            ],
        }

    def acceptance(self, runs_per_offset: int = SWEEP_RUNS_PER_OFFSET) -> dict[str, Any]:
        """The full both-variant acceptance sweep plus the dashboard summary."""
        out = {
            variant: self.run_sweep(variant, runs_per_offset=runs_per_offset)
            for variant in (VARIANT_NAIVE, VARIANT_FENCED)
        }
        summary = self.summary()
        chart_path = RESULTS_DIR / "mismatch_by_offset.svg"
        try:
            records = read_trials(RESULTS_DIR / f"trials_{VARIANT_NAIVE}.jsonl") + read_trials(
                RESULTS_DIR / f"trials_{VARIANT_FENCED}.jsonl"
            )
            render_mismatch_chart(records, chart_path)
        except Exception:  # pragma: no cover - chart is a nice-to-have
            logger.exception("chart rendering failed")
        return {"sweeps": out, "summary": summary, "chart": "/api/chart.svg"}

    def summary(self) -> dict[str, Any]:
        """``reporting.dashboard.summarize`` over whatever trial logs exist."""
        paths = [
            RESULTS_DIR / f"trials_{VARIANT_NAIVE}.jsonl",
            RESULTS_DIR / f"trials_{VARIANT_FENCED}.jsonl",
        ]
        return summarize([p for p in paths])

    def chart_svg(self) -> str:
        """Render the mismatch-vs-offset chart from the current trial logs."""
        records = []
        for variant in (VARIANT_NAIVE, VARIANT_FENCED):
            path = RESULTS_DIR / f"trials_{variant}.jsonl"
            if path.exists():
                records.extend(read_trials(path))
        out = RESULTS_DIR / "mismatch_by_offset.svg"
        if not records:
            return (
                '<svg xmlns="http://www.w3.org/2000/svg" width="960" height="200">'
                '<text x="24" y="100" font-family="monospace" font-size="18" fill="#8a8f98">'
                "No trials yet - run a sweep first.</text></svg>"
            )
        render_mismatch_chart(records, out)
        return out.read_text(encoding="utf-8")

    # -- database inspection --------------------------------------------

    def bookings(self, limit: int = 50) -> dict[str, Any]:
        """Read the demo store directly -- the audit view judges ask for."""
        import sqlite3

        limit = max(1, min(int(limit), 500))
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT * FROM bookings ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
                log = conn.execute(
                    "SELECT * FROM transaction_log ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            except sqlite3.OperationalError:
                rows, log = [], []

        return {
            "db_path": self.db_path,
            "bookings": [dict(r) for r in rows],
            "transaction_log": [dict(r) for r in log],
            "counts": {
                "total": bs.count_bookings(),
                "committed": bs.count_bookings(status=bs.STATUS_COMMITTED),
                "pending": bs.count_bookings(status=bs.STATUS_PENDING_AUDIO),
                "rolled_back": bs.count_bookings(status=bs.STATUS_ROLLED_BACK),
                "naive_committed": bs.count_bookings(
                    status=bs.STATUS_COMMITTED, agent_variant=VARIANT_NAIVE
                ),
                "fenced_committed": bs.count_bookings(
                    status=bs.STATUS_COMMITTED, agent_variant=VARIANT_FENCED
                ),
            },
        }

    def reset(self) -> dict[str, Any]:
        """Clear the demo database and session registry."""
        bs.reset_db()
        cleared = self.sessions.clear()
        return {"ok": True, "sessions_cleared": cleared, "db_path": self.db_path}

    # -- state machine documentation (rendered in the UI) ---------------

    @staticmethod
    def fence_states() -> list[dict[str, str]]:
        return [
            {
                "state": FenceState.IDLE.value,
                "meaning": "No utterance is being fenced yet.",
            },
            {
                "state": FenceState.PENDING.value,
                "meaning": "Tool call created a PENDING_AUDIO row; nothing is real yet.",
            },
            {
                "state": FenceState.COMMITTED.value,
                "meaning": "The gating word finished playing, so the row is now real.",
            },
            {
                "state": FenceState.ROLLED_BACK.value,
                "meaning": "Playback was cut before the gating word: no phantom booking.",
            },
        ]


_SERVICE: DemoService | None = None
_SERVICE_LOCK = threading.Lock()


def get_service() -> DemoService:
    """Process-wide singleton (the store's DB path override is process-wide too)."""
    global _SERVICE
    if _SERVICE is None:
        with _SERVICE_LOCK:
            if _SERVICE is None:
                _SERVICE = DemoService()
    return _SERVICE
