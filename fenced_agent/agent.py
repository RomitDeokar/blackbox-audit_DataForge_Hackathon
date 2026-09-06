"""Fenced booking agent — commits only what the caller actually heard.

Which LiveKit / Rime API this hooks into, and why
=================================================
Verified against **livekit-agents 1.8.0** and **livekit-plugins-rime 1.8.0**
by reading the installed packages, not from memory.

1. **Word-level timestamps.** ``rime.TTS(use_websocket=True)`` advertises
   ``TTSCapabilities(streaming=True, aligned_transcript=True)``. Its receive
   loop turns each Rime ``{"type": "timestamps", "word_timestamps": {...}}``
   frame into ``TimedString(text=word + " ", start_time=s, end_time=e)`` and
   pushes it via ``output_emitter.push_timed_transcript(...)``
   (``livekit/plugins/rime/tts.py``, the ``elif t == "timestamps"`` branch).

   Those ``TimedString`` objects reach application code through
   ``Agent.transcription_node(text, model_settings)`` — but **only** when
   ``use_tts_aligned_transcript=True`` is set on the session or agent.
   ``AgentActivity`` gates this on
   ``tts.capabilities.aligned_transcript`` and then swaps the raw LLM text for
   the TTS-aligned stream before calling ``transcription_node``
   (``voice/agent_activity.py``, ``use_aligned_transcript``). Overriding
   ``transcription_node`` is therefore the supported, non-private hook for
   per-word playback timing. We yield every item through unchanged so the room
   transcript is unaffected.

   Caveat, stated honestly: ``transcription_node`` sees a word when it is
   *emitted by the TTS aligner*, which runs ahead of the speaker. The
   authoritative "it actually reached the caller" signal is the playback
   position, which is why the fence also consumes (2).

2. **Playback completion vs. cancellation.** ``session.output.audio`` is an
   ``io.AudioOutput`` (``rtc.EventEmitter``) that emits:

   * ``playback_started`` -> ``PlaybackStartedEvent(created_at)``
   * ``playback_progressed`` -> ``PlaybackProgressedEvent(started_at, offset,
     duration)`` — a stretch of audio that can no longer be discarded, i.e.
     genuinely played. This is our ground truth for elapsed playback.
   * ``playback_finished`` -> ``PlaybackFinishedEvent(playback_position,
     interrupted, synchronized_transcript)`` where ``interrupted=True`` means
     ``clear_buffer()`` was called — a real barge-in
     (``voice/io.py``, ``class AudioOutput``).

   We therefore commit on ``playback_position >= end_time("confirmed")`` and
   roll back when ``interrupted=True`` arrives first.

3. **Interruption mode.** ``turn_handling={"interruption": {"mode": "vad"}}``
   pins detection to plain VAD instead of the newer ``"adaptive"`` ML
   classifier (``voice/turn.py``, ``InterruptionOptions``). Rationale: the
   chaos harness needs deterministic, low-jitter interruption timing to measure
   the fencing logic in isolation. An ML interruption classifier adds inference
   latency and its own variance, which would show up in the sweep as fencing
   jitter that isn't fencing jitter at all.

All of the decision logic lives in ``shared.audio_fence.AudioFence`` — this
file is only the adapter that feeds it real events. That is what lets the
chaos harness replay the identical decision code offline.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import AsyncGenerator, AsyncIterable
from typing import Any

from shared import booking_store
from shared.audio_fence import AudioFence, FenceDecision
from shared.config import AgentConfig
from shared.constants import (
    CONFIRMATION_GATING_WORD,
    RESTAURANT_NAME,
    VARIANT_FENCED,
    confirmation_sentence,
)

logger = logging.getLogger("blackbox_audit.fenced_agent")

INSTRUCTIONS = f"""You are the phone booking assistant for {RESTAURANT_NAME}.

Keep replies to one short sentence — this is a live phone call.

When the caller asks for a table, call the `create_booking` tool with the party
size and the time. Call it exactly once per request. After the tool returns,
say nothing further; the confirmation sentence is spoken for you.

If the party size or time is unclear, ask one brief question instead of guessing.
"""


class FenceController:
    """Owns the current :class:`AudioFence` and wires LiveKit events into it.

    Kept separate from the ``Agent`` subclass so tests can drive it with mocked
    events and no LiveKit import at all.
    """

    def __init__(
        self,
        gating_word: str = CONFIRMATION_GATING_WORD,
        store: Any = booking_store,
    ) -> None:
        self.gating_word = gating_word
        self.store = store
        self.fence: AudioFence | None = None
        self.decisions: list[FenceDecision] = []
        self._playback_position = 0.0

    # -- lifecycle -------------------------------------------------------

    def begin(self, booking_id: int) -> AudioFence:
        """Open a fence for a freshly created PENDING_AUDIO row."""
        fence = AudioFence(
            gating_word=self.gating_word,
            store=self.store,
            on_resolved=self.decisions.append,
        )
        fence.on_pending(booking_id)
        self.fence = fence
        self._playback_position = 0.0
        return fence

    @property
    def last_decision(self) -> FenceDecision | None:
        return self.decisions[-1] if self.decisions else None

    # -- LiveKit event adapters -----------------------------------------

    def on_timed_word(self, text: str, start: float, end: float) -> None:
        """A ``TimedString`` arrived from the Rime alignment stream."""
        if self.fence is not None:
            self.fence.on_timed_word(text, start, end)

    def on_playback_started(self, ev: Any) -> None:
        self._playback_position = 0.0

    def on_playback_progressed(self, ev: Any) -> None:
        """Track audio that can no longer be discarded — i.e. genuinely heard."""
        offset = float(getattr(ev, "offset", 0.0) or 0.0)
        duration = float(getattr(ev, "duration", 0.0) or 0.0)
        self._playback_position = max(self._playback_position, offset + duration)

    def on_playback_finished(self, ev: Any) -> None:
        if self.fence is None:
            return
        position = float(getattr(ev, "playback_position", self._playback_position) or 0.0)
        position = max(position, self._playback_position)
        interrupted = bool(getattr(ev, "interrupted", False))
        self.fence.on_playback_finished(position=position, interrupted=interrupted)

    def on_close(self) -> None:
        """Session teardown: never leave a PENDING_AUDIO row orphaned."""
        if self.fence is not None:
            self.fence.on_session_closed()

    def attach(self, audio_output: Any) -> None:
        """Subscribe to a LiveKit ``io.AudioOutput``'s playback events."""
        if audio_output is None:
            logger.warning("no audio output on the session; the fence cannot arm")
            return
        audio_output.on("playback_started", self.on_playback_started)
        audio_output.on("playback_progressed", self.on_playback_progressed)
        audio_output.on("playback_finished", self.on_playback_finished)


def build_agent(config: AgentConfig | None = None):
    """Construct the fenced agent and its :class:`FenceController`."""
    from livekit.agents import Agent, ModelSettings, RunContext, function_tool
    from livekit.agents.types import TimedString
    from livekit.agents.utils import is_given

    cfg = config or AgentConfig.from_env()
    controller = FenceController(gating_word=cfg.gating_word, store=booking_store)

    class FencedBookingAgent(Agent):
        """Creates PENDING_AUDIO on tool call; commits only on heard audio."""

        def __init__(self) -> None:
            super().__init__(
                instructions=INSTRUCTIONS,
                # Deterministic VAD interruption, not the adaptive ML detector:
                # the harness must measure the fence, not a classifier.
                turn_handling={"interruption": {"mode": "vad", "min_duration": 0.1}},
                use_tts_aligned_transcript=True,
            )
            self.controller = controller

        @function_tool
        async def create_booking(
            self,
            ctx: RunContext,
            party_size: int,
            time_str: str,
        ) -> str:
            """Reserve a table.

            Args:
                party_size: Number of guests.
                time_str: Requested time, e.g. "7:00 PM".
            """
            booking_id = booking_store.create_pending_booking(
                party_size,
                time_str,
                agent_variant=VARIANT_FENCED,
            )
            self.controller.begin(booking_id)
            logger.info(
                "pending booking=%s party_size=%s time=%s (awaiting heard confirmation)",
                booking_id,
                party_size,
                time_str,
            )

            ctx.session.say(confirmation_sentence(party_size, time_str))
            return f"Reservation held pending confirmation. booking_id={booking_id}"

        async def transcription_node(
            self,
            text: AsyncIterable[str | TimedString],
            model_settings: ModelSettings,
        ) -> AsyncGenerator[str | TimedString, None]:
            """Tap the TTS-aligned transcript for word-level timing.

            Requires ``use_tts_aligned_transcript=True`` (set above) plus a TTS
            advertising ``aligned_transcript`` — Rime over WebSocket does.
            Every item is yielded through untouched so the room transcript is
            byte-identical to the naive agent's.
            """
            async for chunk in text:
                if isinstance(chunk, TimedString):
                    start = chunk.start_time if is_given(chunk.start_time) else None
                    end = chunk.end_time if is_given(chunk.end_time) else None
                    if start is not None and end is not None:
                        self.controller.on_timed_word(str(chunk), float(start), float(end))
                yield chunk

    return FencedBookingAgent(), controller, cfg


def build_session(config: AgentConfig):
    """Build an AgentSession identical to the naive agent's, bar the fence."""
    from livekit.agents import AgentSession
    from livekit.plugins import deepgram, openai, rime, silero

    config.require_live()

    return AgentSession(
        stt=deepgram.STT(model=config.stt_model, api_key=config.deepgram_api_key),
        llm=openai.LLM(model=config.llm_model, api_key=config.openai_api_key),
        tts=rime.TTS(
            model=config.tts_model,
            speaker=config.tts_speaker,
            api_key=config.rime_api_key,
            # Mandatory for the fence: word-level timestamps only exist on the
            # WebSocket transport.
            use_websocket=True,
        ),
        vad=silero.VAD.load(),
        # Routes Rime's TimedStrings into transcription_node.
        use_tts_aligned_transcript=True,
        turn_handling={"interruption": {"mode": "vad", "min_duration": 0.1}},
    )


def create_server():
    """Build the ``AgentServer`` for ``python -m fenced_agent.agent``."""
    from livekit.agents import AgentServer, JobContext, RoomInputOptions

    server = AgentServer()

    @server.rtc_session(agent_name="blackbox-fenced")
    async def entrypoint(ctx: JobContext) -> None:
        agent, controller, cfg = build_agent()
        booking_store.init_db(cfg.db_path)
        session = build_session(cfg)

        logger.info("fenced agent joining room %s", ctx.room.name)
        await session.start(
            agent=agent,
            room=ctx.room,
            room_input_options=RoomInputOptions(),
        )

        # Arm the fence on the live audio output, and make sure a hang-up
        # mid-confirmation rolls back rather than leaking a PENDING row.
        controller.attach(session.output.audio)
        ctx.add_shutdown_callback(_shutdown(controller))

    return server


def _shutdown(controller: FenceController):
    async def _cb(*_args: Any, **_kwargs: Any) -> None:
        controller.on_close()

    return _cb


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fenced_agent.agent",
        description="Fenced booking agent — commits only on confirmed audio playback.",
    )
    parser.add_argument("--room", default=None, help="LiveKit room to join in dev mode.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate configuration and exit without connecting (makes no API calls).",
    )
    parser.add_argument("--log-level", default="INFO")
    args, passthrough = parser.parse_known_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = AgentConfig.from_env()
    if args.check:
        print("fenced_agent configuration (secrets masked):")
        for key, value in cfg.redacted().items():
            print(f"  {key} = {value}")
        if cfg.missing:
            print(f"\nMISSING: {', '.join(cfg.missing)}")
            return 1
        print("\nAll required variables present. No API calls were made.")
        return 0

    from livekit.agents import cli

    booking_store.init_db(cfg.db_path)
    argv_for_cli = [sys.argv[0], *passthrough]
    if args.room:
        argv_for_cli += ["--room", args.room]
    elif len(passthrough) == 0:
        argv_for_cli += ["dev"]

    sys.argv = argv_for_cli
    cli.run_app(create_server())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
