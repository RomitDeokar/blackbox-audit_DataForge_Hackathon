"""Naive booking agent — the deliberately broken baseline.

The booking is committed the instant the LLM's tool call executes, with no
gating on whether the caller ever heard the confirmation sentence. This is not
a strawman: it is how the overwhelming majority of tool-calling voice agents are
written, because ``tool_call_success`` *feels* like the moment the action
happened.

Do not "fix" this file. Its mismatches under barge-in are the control group
that gives the fenced agent's zero-mismatch result any meaning.

LiveKit API notes (verified against livekit-agents 1.8.0)
---------------------------------------------------------
* ``AgentServer`` + ``@server.rtc_session(...)`` is the current worker entry
  point; the old ``WorkerOptions``/``cli.run_app`` pair is deprecated.
* Tools are plain methods decorated with ``@function_tool`` on an ``Agent``
  subclass; they receive a ``RunContext`` as the first argument.
* ``turn_handling={"interruption": {"mode": "vad"}}`` pins interruption
  detection to VAD. Both variants use the identical setting so the comparison
  isolates the commit policy, not the interruption classifier.
"""

from __future__ import annotations

import argparse
import logging
import sys

from shared import booking_store
from shared.config import AgentConfig
from shared.constants import (
    RESTAURANT_NAME,
    VARIANT_NAIVE,
    confirmation_sentence,
)

logger = logging.getLogger("blackbox_audit.naive_agent")

INSTRUCTIONS = f"""You are the phone booking assistant for {RESTAURANT_NAME}.

Keep replies to one short sentence — this is a live phone call.

When the caller asks for a table, call the `create_booking` tool with the party
size and the time. Call it exactly once per request. After the tool returns, reply with exactly "OK." and nothing else; the
confirmation sentence is spoken for you by a dedicated utterance.

If the party size or time is unclear, ask one brief question instead of guessing.
"""


def build_agent(config: AgentConfig | None = None):
    """Construct the naive agent.

    Imported lazily inside the function so that ``import naive_agent.agent``
    stays cheap and dependency-light for tests that only need the tool logic.
    """
    from livekit.agents import Agent, RunContext, function_tool

    cfg = config or AgentConfig.from_env()

    class NaiveBookingAgent(Agent):
        """Commits on tool call. No audio fence."""

        def __init__(self) -> None:
            super().__init__(
                instructions=INSTRUCTIONS,
                # Identical to the fenced agent: deterministic VAD interruption
                # so the harness measures the commit policy, not an ML
                # interruption classifier's variance.
                turn_handling={"interruption": {"mode": "vad", "min_duration": 0.1}},
            )
            self.last_booking_id: int | None = None

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
            # THE BUG, ON PURPOSE: committed here, before a single word of the
            # confirmation has left the speaker.
            booking_id = booking_store.book_naive(
                party_size,
                time_str,
                agent_variant=VARIANT_NAIVE,
            )
            self.last_booking_id = booking_id
            logger.info(
                "naive commit booking=%s party_size=%s time=%s (nothing spoken yet)",
                booking_id,
                party_size,
                time_str,
            )

            # Speak the locked confirmation sentence. Whether the caller ever
            # hears it has no bearing on the row that already exists.
            #
            # We intentionally capture the SpeechHandle instead of firing and
            # forgetting: the handle makes the confirmation utterance
            # observable (interruption, playback position) in logs, which is
            # the naive agent's whole purpose as the control group.
            self.confirmation_handle = ctx.session.say(
                confirmation_sentence(party_size, time_str),
                allow_interruptions=True,
            )
            # "OK." (not the confirmation sentence) keeps the LLM from
            # paraphrasing a second confirmation on top of session.say(). The
            # word "confirmed" must appear exactly once per booking, or the
            # gating-word oracle in the harness sees it twice with different
            # timings.
            return "OK."

    return NaiveBookingAgent(), cfg


def build_session(config: AgentConfig):
    """Build an AgentSession with the configured STT/LLM/TTS/VAD stack."""
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
            # WebSocket streaming is what exposes word-level timestamps. The
            # naive agent does not use them, but keeping the transport identical
            # to the fenced agent removes it as a confound.
            use_websocket=True,
        ),
        vad=silero.VAD.load(),
        use_tts_aligned_transcript=True,
        turn_handling={"interruption": {"mode": "vad", "min_duration": 0.1}},
    )


def create_server():
    """Build the ``AgentServer`` for ``python -m naive_agent.agent``."""
    from livekit.agents import AgentServer, JobContext, RoomInputOptions

    server = AgentServer()

    @server.rtc_session(agent_name="blackbox-naive")
    async def entrypoint(ctx: JobContext) -> None:
        agent, cfg = build_agent()
        booking_store.init_db(cfg.db_path)
        session = build_session(cfg)

        logger.info("naive agent joining room %s", ctx.room.name)
        await session.start(
            agent=agent,
            room=ctx.room,
            room_input_options=RoomInputOptions(),
        )

    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m naive_agent.agent",
        description="Naive (tool-call-gated) booking agent — the broken baseline.",
    )
    parser.add_argument(
        "--room",
        default=None,
        help="LiveKit room to join in dev mode (defaults to the worker's dispatch).",
    )
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
        print("naive_agent configuration (secrets masked):")
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
