# Architecture

## Falsifiable claim

A voice agent that gates database writes behind confirmed audio playback (not
just LLM tool-call completion) will never commit a state change the user
didn't actually hear confirmed — even under 100+ rapid mid-sentence
interruptions — while a naively-implemented agent will.

## Components

- **naive_agent/** — commits the booking the instant the tool call executes,
  before or regardless of whether the confirmation is ever heard. Deliberately
  broken, honestly so — not a strawman.
- **fenced_agent/** — same booking flow, but the tool call only creates a
  `PENDING_AUDIO` row. The row is only committed when Rime's word-level
  timestamp stream confirms the spoken confirmation word was actually played;
  it is rolled back if playback is cancelled first.
- **shared/booking_store.py** — the single SQLite-backed module both agents
  use, so their data layer is byte-for-byte comparable.
- **shared/audio_fence.py** — the commit-gating state machine itself,
  deliberately free of any LiveKit/Rime/OpenAI import so the same object runs
  in the live agent and in the offline sweep.
- **chaos_harness/** — CLI that replays a cached Rime timeline through the
  target's decision code, injects a barge-in at a controlled offset relative to
  Rime's own timestamp stream (not wall-clock time), then audits the DB
  afterward across a -500..+500 ms offset ladder.
- **reporting/** — renders trial results live and computes pass/fail +
  cancellation-latency percentiles.

## Key mechanism

Rime's word-level streaming timestamps are ground truth for "what did the
user actually hear." The fenced agent commits on
`audio_playback_reached_confirmation_word`, not on `tool_call_success`. If the
harness cancels playback before that timestamp fires, the transaction manager
rolls back.

```
caller says "book a table for 4 at 7pm"
        │
        ▼
tool call ──naive──► book_naive() ─────────────► row COMMITTED (nothing spoken yet)
        │
        └──fenced──► create_pending_booking() ──► row PENDING_AUDIO
                            │
        Rime word timestamps stream (WebSocket)  │
        "confirmed" fully played?                │
            ├── yes ──► commit_booking() ───────► row COMMITTED
            └── no (barge-in / hangup) ─► rollback_booking() ──► row ROLLED_BACK
```

## Why the offset is keyed to the timestamp stream

"Cancel 120 ms after the word 'confirmed' finished playing" is a statement
about the caller's ear. "Cancel 2.4 s after the tool call" is a statement
about a network, a jitter buffer and an event loop — it would smear the
measurement across ~100 ms of noise, wider than the effect being measured.
The injector (`chaos_harness/injector.py`) therefore schedules every barge-in
off `gating_word_end + offset_ms`.

## Offline replay parity

The offline sweep is not a mock of the fence: it feeds the exact same
`AudioFence` state machine the live fenced agent runs, and writes to the exact
same SQLite store, only with a cached timeline instead of a live Rime
WebSocket stream. Only the transport is simulated. A test
(`test_replay_sweep_makes_no_external_api_calls`) enforces that the sweep never
touches the network.
