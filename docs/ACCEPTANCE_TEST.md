# Acceptance Test

## Scripted conversation

Caller: "Book a table for 4 at 7pm"

Agent confirmation sentence (fixed, both variants): "Okay, you're confirmed
for a table of four at seven PM." — the confirmation word for timing purposes
is **"confirmed"**.

## Procedure

1. The harness replays the confirmation timeline and injects a barge-in at a
   controlled offset relative to the **end** of the word "confirmed" in Rime's
   word-level timestamp stream — not wall-clock time.
2. Sweep offset from **-500 ms to +500 ms in 10 ms steps (101 offsets) × 3
   runs per offset = 303 trials per agent variant** (606 total).
3. After each trial, query the DB directly: was a booking row committed?
4. Ground truth: a booking should exist **if and only if** playback reached
   the confirmation word before cancellation. The oracle
   (`shared/audio_fence.evaluate_ground_truth`) is computed straight from the
   timestamps, independently of the fence, so the harness never grades the
   fence with the fence's own logic.

A word cut off mid-syllable is *not* heard: a cancellation at
`gating_word_end - 1 ms` must roll back, at `gating_word_end + 1 ms` must
commit.

## Pass condition

**0 mismatches** between "user heard confirmation" and "DB has committed row"
on the fenced agent across all 303 trials. The naive agent is expected to show
mismatches — that's the point of the comparison.

## Running it

### Offline (default, free, ~1 s)

```bash
python -m chaos_harness.cli acceptance          # both variants + summary.json
```

or per target:

```bash
python -m chaos_harness.cli run --target fenced
python -m chaos_harness.cli run --target naive
```

This replays the cached Rime timeline through the real decision code and the
real SQLite store — no API keys, no network, no cost.

### Live (opt-in, paid)

```bash
# terminal 1: start the fenced agent worker
python -m fenced_agent.agent --room bb-fenced

# terminal 2: small capped smoke run against the real room
python -m chaos_harness.cli run --target fenced --mode live --room bb-fenced --trials 3
```

Live mode requires `--room` and an explicit `--trials` cap (API budget rule);
a full 303-trial live sweep should only be run deliberately after the offline
sweep passes.

## Results file

Every trial is appended to `results/trials_{target}.jsonl` with the frozen
schema: `trial_id, target, offset_ms, booking_id, db_status_after, mismatch,
cancel_latency_ms, timestamp` plus diagnostics (heard transcript, gating-word
end time, fence outcome, mode).
