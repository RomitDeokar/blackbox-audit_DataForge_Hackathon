# RIME_EVIDENCE.md — Hard Voice Claim & Full-Sweep Results

## Hard Voice Claim

A voice agent that gates database writes behind **confirmed audio playback**
(not just LLM tool-call completion) will never commit a state change the user
didn't actually hear confirmed — even under 100+ rapid mid-sentence
interruptions — while a naively-implemented agent will.

Ground truth is Rime's word-level streaming timestamp frame (`word_timestamps`
with parallel `words` / `start` / `end` arrays), whose shape was verified
against a live Rime call (see `scripts/test_rime.py`) and against the
`livekit-plugins-rime` parser. The barge-in offset in every trial is measured
relative to the **end of the spoken word "confirmed"** in that stream, never
wall-clock time.

## Acceptance Test

- Caller: "Book a table for 4 at 7pm"
- Fixed confirmation sentence (both variants): "Okay, you're confirmed for a
  table of four at seven PM."
- Gating word: **"confirmed"** — a booking must exist **iff** the end
  timestamp of "confirmed" was reached before cancellation. A word cut off
  mid-syllable is not heard.
- Sweep: offsets **-500 ms…+500 ms in 10 ms steps (101 offsets) × 3 runs =
  303 trials per agent variant** (606 total), keyed to
  `gating_word_end + offset_ms`.
- Oracle: `shared/audio_fence.evaluate_ground_truth` computed directly from
  the timestamps, independent of the fence, so the harness never grades the
  fence with the fence's own logic.

## Method

1. `shared/audio_fence.AudioFence` replays the confirmation timeline through
   the **same state machine the live fenced agent runs** (only the transport
   is simulated: cached timeline instead of a live WebSocket stream).
2. Each trial writes to the **real SQLite store**
   (`shared/booking_store`), shared byte-for-byte with both agent variants.
3. After each trial the DB is queried directly and compared to the oracle.
4. Report: `python -m chaos_harness.cli acceptance`
   (free — a test asserts replay sweeps make zero external API calls).

## Results

Fixtures note: the shipped `fixtures/rime_confirmation_timestamps.json` uses
the **verified real Rime shape** but **placeholder pacing**. Mismatch counts
in the table below are deterministic (they depend only on the timeline and the
offset ladder), but before quoting sweep numbers as Rime-measured timing,
overwrite the fixture with a real capture:

```bash
python scripts/test_rime.py --save-fixture   # one live Rime call
python -m chaos_harness.cli acceptance        # regenerate trials + summary
```

| Agent Variant | Trials | State Mismatches | Cancel Latency p50 | Cancel Latency p95 |
| --- | --- | --- | --- | --- |
| naive | 303 | 150 | 0.010 ms | 0.018 ms |
| fenced | 303 | **0** | 2.53 ms | 3.19 ms |

(`results/summary.json` holds the full breakdown; percentiles are host- and
run-dependent, mismatch counts are not.)

**Verdict: PASS for the fenced agent** — 0 state mismatches across 303
trials. The naive control group produced 150 phantom bookings (every
mismatch is a row committed while the caller had not yet heard "confirmed"),
which is exactly the failure mode this project exists to catch.

## Reproduction

```bash
python -m chaos_harness.cli acceptance                 # 606 trials + summary.json
python -m reporting.dashboard summarize results/trials_naive.jsonl results/trials_fenced.jsonl
python -m reporting.dashboard watch results/trials_fenced.jsonl   # live during a run
```

## Limitations

- **Telephony**: the live path runs over LiveKit WebRTC rooms, not real
  PSTN/telephony carriers; last-mile audio-failure behaviour is out of scope.
- **Timestamp precision**: Rime's word-alignment precision bounds the fencing
  granularity; the fence commits on the gating word's *end* timestamp, which
  is the earliest physically defensible instant.
- **Fixture scope**: the offline sweep currently replays one booking-flow
  fixture (placeholder-paced, real shape); results should be re-run on a
  live-captured fixture before being quoted as Rime-measured.
- **Single flow**: the harness tests one fixed tool schema (create_booking),
  not arbitrary tool schemas or multi-turn state.
