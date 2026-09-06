# Mobile Demo Setup — "Rime doesn't work on mobile" runbook

**TL;DR: it is not a Rime problem.** Rime runs inside the agent worker on your
laptop/server. The phone never talks to Rime directly. What breaks on mobile is
the LiveKit client path — and when the client path breaks, the agent never hears
you, so it never speaks, so it *looks* like TTS is broken.

```
Phone (LiveKit client) ──wss──▶ LiveKit room ──▶ agent worker ──wss──▶ Rime
        ▲ THIS is what fails                          ▲ backend chain (fine)
```

## Ranked causes, most likely first

| # | Cause | Symptom | Fix |
|---|-------|---------|-----|
| 1 | Page opened over `http://` or LAN IP (`http://192.168.x.x:3000`) | Mic permission silently denied; agent never hears you | Use an `https://` page only (hosted playground) |
| 2 | `LIVEKIT_URL` points at `localhost`/LAN LiveKit | Phone can't reach the server at all | Use LiveKit Cloud: `wss://<project>.livekit.cloud` |
| 3 | No agent dispatch for the mobile participant | Room connects but is empty; total silence | Worker must be running *before* the phone joins; watch its log |
| 4 | iOS Safari autoplay policy | Agent speaks (worker log shows TTS) but phone plays nothing | Tap-to-connect gesture must call `room.startAudio()` — the hosted playground handles this |
| 5 | Cellular/carrier blocks WebRTC UDP | Works on venue Wi-Fi, dies on LTE | LiveKit Cloud auto-falls back to TURN over TCP 443 |
| 6 | Dependency drift between machines | Worker crashes or hooks never fire on one laptop | Use the pinned `requirements.txt`; run `--check` on every demo machine |

## Working mobile demo, step by step (verified path)

1. **LiveKit Cloud project.** Set in `.env`:
   ```
   LIVEKIT_URL=wss://<your-project>.livekit.cloud
   LIVEKIT_API_KEY=...
   LIVEKIT_API_SECRET=...
   ```
   Do NOT use `localhost` or a LAN IP — a phone cannot reach them.
2. **Start the worker on the laptop** (this machine stays the "backend"):
   ```bash
   python -m fenced_agent.agent dev
   ```
   Wait for the "registered worker / waiting for jobs" log line.
3. **On the phone**, open **https://agents-playground.livekit.io** (HTTPS,
   hosted, handles the iOS audio-unlock gesture). Connect with the same Cloud
   project credentials, choose agent `blackbox-fenced`.
4. **Confirm dispatch**: the instant the phone joins, the worker terminal must
   log the job acceptance. **No log = dispatch problem (cause #3), not audio.**
5. Say: **"Book a table for four at 7 PM."** → you hear Rime's voice.
6. **The money moment — barge in:** while it speaks, say **"Wait, make it 8 PM."**
7. Show the DB on the big screen:
   - fenced variant: `PENDING_AUDIO` → `ROLLED_BACK` (phantom booking killed)
   - naive variant: instant `COMMITTED` (phantom booking — the bug, live)

## Graceful degradation for demo day

| Plan | What you show | Risk |
|------|---------------|------|
| A | Laptop browser as the "caller", phone hotspot for connectivity — same live demo, zero mobile quirks | Low |
| B | Offline replay: `python -m chaos_harness.cli acceptance` + live dashboard — 606 real trials through the real fence code in ~1 s, zero API calls | Cannot fail on stage |
| C | Pre-recorded video of one live call + rollback, then run Plan B live | Safe fallback |

Judges score *voice is essential* + *transparent method*. Plan B **is** your
evidence — run it on stage regardless, narrated as: "the phone call shows the
experience; this sweep is the proof."

## Morning-of checklist

```bash
python scripts/check_env.py                   # catches causes #1/#2/#6 in one run
pytest                                        # full suite, ~6 s
python -m fenced_agent.agent --check          # keys present, no API calls
python -m naive_agent.agent --check
python scripts/test_rime.py --save-fixture    # 1 paid call → real Rime fixture
python -m chaos_harness.cli acceptance        # regenerate trials against real fixture
# then ONE full live call on the phone: venue Wi-Fi AND cellular
```
