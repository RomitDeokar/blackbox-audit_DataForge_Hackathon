# Demo Script (4 beats, ~2 minutes)

Equipment: two terminals (agent worker + harness/dashboard) or a recording of
the same. The demo is most convincing live: sweep runs take ~1 second, so you
can show real trials appending in the dashboard.

## Beat 1 — The happy path (an ordinary booking works)

```bash
python -m chaos_harness.cli run --target fenced --runs-per-offset 1 --max-ms 50
```

Narrate: *"A normal call. The caller asks for a table at 7 PM, the agent
speaks the confirmation, the caller hears it, and the booking lands in the
database exactly as heard — COMMITTED."*

Point at the dashboard row: `db status COMMITTED`, `mismatch no`.

## Beat 2 — The barge-in (deliberately interrupt mid-confirmation)

```bash
python -m chaos_harness.cli heard --at 1.2
```

Narrate: *"Now the caller interrupts right as the agent is saying the
confirmation — before the word 'confirmed' has finished leaving the speaker.
Here's the transcript the caller had actually heard at that instant."*

Then run one trial at a negative offset and show `ROLLED_BACK`:

```bash
python -m chaos_harness.cli run --target fenced --min-ms -200 --max-ms -200 --runs-per-offset 1
```

## Beat 3 — The audit (no phantom booking)

Open the SQLite store directly (or the transaction log via a one-liner):

```bash
python -c "from shared import booking_store as bs; print([dict(r) for r in bs.get_transaction_log()][-3:])"
```

Narrate: *"The database shows the truth: the row went PENDING_AUDIO and was
rolled back. Nothing was committed that the caller never heard. No phantom
booking, no double-booked table."*

## Beat 4 — The full sweep (it holds at scale, not just once)

```bash
python -m chaos_harness.cli acceptance
python -m reporting.dashboard watch results/trials_fenced.jsonl   # during the run
```

Narrate: *"Now the whole ladder — 303 interruptions per agent across ±500 ms
around the confirmation word. The fenced agent: zero mismatches. The naive
agent — the way most tool-calling voice agents are written — produced 150
bookings the caller never heard confirmed. Same database, same sentence, same
interruptions; the only difference is whether a commit waits for the caller's
ear."*

Close on the green PASS banner from `reporting.dashboard summarize`.
