# BlackBox Audit

BlackBox Audit is a voice-agent reliability experiment with a business argument: when a phone agent books a table, confirms a time, or takes payment, the backend state must match what the caller **actually heard** — not what a model decided to do. Restaurants that double-book, clinics that confirm the wrong slot, and customers charged for something they never heard confirmed are real costs of committing on *tool-call success* instead of on *confirmed audio playback*. This repo builds a chaos harness that measures that failure mode, then proves a fix for it.

The core question:

```text
If a voice agent starts saying "Okay, you're confirmed for a table for 4 at 7 PM,"
and the user interrupts with "Wait! Make that 8 PM instead,"
does the backend booking match what the user actually heard and intended?
```

This is an audit harness, not a booking product. It deliberately ships two agent variants that share the **same** SQLite data layer, so any difference is provably caused by the commit-gating policy and nothing else:

| Variant | Commit rule | Expected result under barge-in |
| --- | --- | --- |
| `naive` | Commit the instant the LLM tool call executes | **Phantom bookings** — rows exist the caller never heard confirmed |
| `fenced` | Create `PENDING_AUDIO`; commit only when Rime's word-level timestamp stream proves the word **"confirmed"** was fully played; roll back on cancellation | **Zero state mismatches** |

The harness interrupts the confirmation at offsets from **-500 ms to +500 ms** relative to the *end of the spoken word "confirmed"* (measured against Rime's own timestamp stream, not wall-clock time), in 10 ms steps × 3 runs = **303 trials per variant**, then compares the database against an independently computed oracle of "what the caller heard."

**Headline result (offline replay, fixture currently placeholder-paced):** fenced **0 / 303 mismatches**, naive **150 / 303**. See [RIME_EVIDENCE.md](RIME_EVIDENCE.md); raw logs live in `results/`.

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a local `.env` from the example file (blank — never commit real keys):

```bash
cp .env.example .env
```

Fill in the keys only when you run the **live** agents/harness. The entire offline harness, dashboard and test suite need **no** API keys.

## Running the harness (offline, free, no API calls)

```bash
# the full 606-trial acceptance sweep (both variants + summary.json), ~1 s
python -m chaos_harness.cli acceptance

# or per target
python -m chaos_harness.cli run --target fenced
python -m chaos_harness.cli run --target naive

# pipeline smoke test (no fixture, no DB)
python -m chaos_harness.cli run --target dummy --trials 5

# inspect the timing ground truth
python -m chaos_harness.cli heard --at 2.1
python -m chaos_harness.cli timeline
```

## Running the dashboard

```bash
# live table while a sweep is running
python -m reporting.dashboard watch results/trials_fenced.jsonl

# summary + PASS/FAIL banner, writes results/summary.json
python -m reporting.dashboard summarize results/trials_naive.jsonl results/trials_fenced.jsonl

# markdown results table for RIME_EVIDENCE.md
python -m reporting.dashboard evidence
```

## Running the voice agents (live, paid APIs)

Both agents are [LiveKit Agents](https://docs.livekit.io/agents/) workers. They share the identical STT/LLM/TTS/VAD stack (Deepgram nova-3, OpenAI gpt-4o-mini, Rime mistv2 over WebSocket, Silero VAD) so the only difference is the commit policy.

```bash
python -m naive_agent.agent            # deliberate broken baseline
python -m fenced_agent.agent           # the audio-fenced variant

# validate config + keys without connecting or spending anything
python -m naive_agent.agent --check
python -m fenced_agent.agent --check
```

`--mode live` on the harness is opt-in, requires `--room` and an explicit `--trials` cap, and is the only path that spends provider quota. The default `replay` mode exercises the **real** decision code (`shared/audio_fence.AudioFence`) and the **real** SQLite store against a cached Rime timeline — only the transport is simulated.

## Validating the Rime timestamp shape

`scripts/test_rime.py` makes one Rime call and prints the raw response plus the word-level timestamp section.

```bash
python scripts/test_rime.py                       # one paid Rime call
python scripts/test_rime.py --save-fixture        # overwrite fixtures/rime_confirmation_timestamps.json with a live capture
```

## Tests

```bash
pytest          # 222 tests in 6 s, all offline, zero API calls
```

## API budget rule

This project treats 50% of every provider quota as reserved. Minimum real calls, mocks/fixtures by default, one retry max, explicit permission before anything that could exceed 5 external calls, and the 300-trial sweep never runs against paid APIs — always `replay` mode. A test enforces that replay sweeps make zero outbound calls.

## Project layout

```text
shared/           booking store (SQLite), audio fence state machine, Rime timestamp parsing, config
naive_agent/      tool-call-gated agent — the deliberately broken baseline
fenced_agent/     audio-fenced agent — commits only on heard confirmation
chaos_harness/    injector, targets, sweep driver, JSONL trial-log contract, CLI
reporting/        live dashboard, summary statistics, PASS/FAIL banner, evidence table
fixtures/         cached Rime confirmation timeline (placeholder pacing until a live capture replaces it)
results/          trial logs + summary.json (git-ignored; regenerate with the commands above)
docs/             ARCHITECTURE.md, ACCEPTANCE_TEST.md, DEMO_SCRIPT.md
```
