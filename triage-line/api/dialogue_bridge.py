"""Phase 7B-2C: drive the existing DialogueEngine for a demo scenario.

Why this exists
---------------
`api/replay_bridge.py` relays `harness.replay.run_scenario`, which drives
the DeliberationEngine + CommitStateMachine directly and deliberately
bypasses the dialogue layer. That means it never emits
`PartialTranscript` / `FinalTranscript` / `BackchannelSent` /
`MetricsTick` -- the events the Conversation panel and MetricsBar render.

The only producer of those events in the codebase is
`core.dialogue.engine.DialogueEngine`. This module runs that *existing*
engine, with the *existing* deterministic mock providers
(`providers/mock/`), over the caller lines of an existing required
scenario, and relays every event it publishes on the real `EventBus` to
WebSocket clients -- exactly the same relay path as `replay_bridge.py`.

What it does NOT do
-------------------
- It computes no metrics. `MetricsTick` values come solely from
  `DialogueEngine.check_for_barge_in()` (the real measured stop latency).
- It makes no dialogue/deliberation/commit decisions.
- It is not a real audio pipeline (no LiveKit/Deepgram/Groq/Rime).

The one thing it publishes itself is a `FinalTranscript(speaker="agent")`
echo of the text the agent actually produced (taken from
`engine.context`), because `DialogueEngine` hands agent text to the TTS
provider but does not publish it -- in the architecture that is the
transport layer's job (it knows what was actually spoken), and this
module is standing in for that transport layer.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from api.replay_bridge import UnknownScenarioError, available_demo_scenarios
from api.websocket import ConnectionManager
from core.dialogue.engine import DialogueEngine
from core.event_bus import EventBus
from core.events import Event, FinalTranscript
from harness.scenario import Scenario, StepType
from providers.interfaces import TranscriptEvent
from providers.mock.mock_audio_io import AudioEvent, MockAudioIO
from providers.mock.mock_llm import MockLLM
from providers.mock.mock_stt import MockSTT
from providers.mock.mock_tts import MockTTS

# How much simulated TTS playback elapses before a scripted barge-in
# lands, so BargeIn.position_ms reflects "mid-sentence" rather than 0.
_PLAYBACK_BEFORE_BARGE_IN_MS = 300


@dataclass
class DialogueDemoResult:
    scenario_id: str
    call_id: str
    success: bool = True
    error: Optional[str] = None
    event_trace: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for evt in self.event_trace:
            counts[evt["event_type"]] = counts.get(evt["event_type"], 0) + 1
        return {
            "scenario_id": self.scenario_id,
            "call_id": self.call_id,
            "success": self.success,
            "error": self.error,
            "event_counts": counts,
            "event_trace": self.event_trace,
        }


def _partials_for(text: str) -> list[str]:
    """Growing word-prefixes of `text`, as a streaming STT would emit them."""
    words = text.split()
    if len(words) <= 1:
        return []
    step = max(1, len(words) // 3)
    return [" ".join(words[:i]) for i in range(step, len(words), step)]


def _clock() -> Callable[[], int]:
    start = time.perf_counter()
    return lambda: int((time.perf_counter() - start) * 1000)


async def run_dialogue_scenario(
    scenario: Scenario,
    on_event: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None,
    pace_ms: int = 0,
) -> DialogueDemoResult:
    """Run `scenario`'s caller lines through the real DialogueEngine.

    `on_event` is awaited, in order, for every event published on the bus,
    so a WebSocket relay receives events in exactly the order produced.
    """

    call_id = f"{scenario.scenario_id}-dialogue-0"
    result = DialogueDemoResult(scenario_id=scenario.scenario_id, call_id=call_id)
    clock_ms = _clock()

    bus = EventBus()

    async def relay(event: Event) -> None:
        payload = event.to_dict()
        result.event_trace.append(payload)
        if on_event is not None:
            await on_event(payload)
        if pace_ms > 0:
            await asyncio.sleep(pace_ms / 1000)

    await bus.subscribe(relay)

    stt, tts, audio_io = MockSTT(), MockTTS(), MockAudioIO()
    engine = DialogueEngine(
        call_id=call_id,
        stt=stt,
        llm=MockLLM(),
        tts=tts,
        audio_io=audio_io,
        bus=bus,
        clock_ms=clock_ms,
    )
    await audio_io.start()

    for step in scenario.steps:
        if step.type == StepType.CALLER_SAYS:
            script = [
                TranscriptEvent("caller", p, False, clock_ms()) for p in _partials_for(step.text)
            ]
            script.append(TranscriptEvent("caller", step.text, True, clock_ms()))
            stt.load_script(script)
            await stt.start()
            while (transcript := await stt.next_transcript()) is not None:
                await engine.handle_transcript(transcript)
            await stt.stop()

            # Transport-layer echo of what the agent actually said.
            agent_turns = [t for t in engine.context if t.speaker == "agent"]
            if agent_turns:
                await bus.publish(
                    FinalTranscript(
                        call_id=call_id,
                        timestamp_ms=clock_ms(),
                        speaker="agent",
                        text=agent_turns[-1].text,
                    )
                )

        elif step.type == StepType.BARGE_IN:
            # Caller starts talking over the agent's current utterance:
            # advance simulated playback part-way, flip the mock AudioIO
            # to "caller speaking", and let the real engine detect it.
            tts.advance(_PLAYBACK_BEFORE_BARGE_IN_MS)
            audio_io.load_script([AudioEvent("caller_interrupt", clock_ms())])
            await audio_io.next_event()
            await engine.check_for_barge_in()
            audio_io.load_script([AudioEvent("caller_speech_end", clock_ms())])
            await audio_io.next_event()

        # CONFIRM / REJECT / DISCONNECT are commit-layer steps; they are
        # rendered by the replay run (api/replay_bridge.py), not here.

    await audio_io.stop()
    return result


async def run_dialogue_demo(
    manager: ConnectionManager,
    channel: str,
    scenario_id: str,
    pace_ms: int = 0,
) -> DialogueDemoResult:
    scenarios = available_demo_scenarios()
    scenario = scenarios.get(scenario_id)
    if scenario is None:
        raise UnknownScenarioError(
            f"Unknown scenario_id {scenario_id!r}; available: {sorted(scenarios)}"
        )

    async def on_event(payload: dict[str, Any]) -> None:
        await manager.broadcast(channel, payload)

    try:
        result = await run_dialogue_scenario(scenario, on_event=on_event, pace_ms=pace_ms)
    except Exception as exc:  # noqa: BLE001 - a failed demo is data, not a crash
        result = DialogueDemoResult(
            scenario_id=scenario_id,
            call_id=f"{scenario_id}-dialogue-0",
            success=False,
            error=f"{type(exc).__name__}: {exc}",
        )
    return result
