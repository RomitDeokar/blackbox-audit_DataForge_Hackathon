# Triage Line UI (Phase 7B dashboard + interactive call prototype)

React + Vite + Tailwind **Live Call View** (docs/UI_SPEC.md §1.1) wired to
the FastAPI/WebSocket backend. Every panel reads from ONE WebSocket event
stream; the UI never queries core/ or the database.

| Panel / element | Events consumed |
|---|---|
| Conversation | `PartialTranscript`, `FinalTranscript`, `BackchannelSent`, `BargeIn` (divider) |
| Deliberation | `DeliberationStarted`, `DeliberationResolved`, `SelfCritiqueFailed`, `ReDeliberationTriggered` |
| Dispatch state | `ActionProposed`, `ActionPending`, `ActionFinalized`, `ActionAborted` |
| MetricsBar (bottom) | `MetricsTick` (values shown verbatim; unset fields = "not reported"), `BargeIn` (count + interruption flash), `TurnStarted` (floor), `call_id`, WebSocket status |
| Raw event stream | everything (collapsible) |

## Run it

```bash
# terminal 1 — backend (repo root)
pip install -r requirements.txt
TRIAGE_DB_PATH=.data/triage.sqlite uvicorn api.app:app --port 8000

# terminal 2 — frontend
cd ui && npm install
npm run dev              # http://localhost:5173  (or: npm run build && npx vite preview → :4173)
```

Click **Connect** on the generated Interactive call ID, send a text turn or
use Mic if your browser supports speech recognition, then review the proposal
and explicitly confirm or reject it. The browser synthesizes the answer if
supported; typing during speech cancels playback locally. No external
provider is contacted by a finalized action. To use the older evaluation
harness instead, switch to Replay run, connect and click **Run demo call**:

1. `POST /demo/dialogue/{run_id}/{scenario}` — the existing `DialogueEngine`
   + mock providers → transcripts, backchannels, `BargeIn`, `MetricsTick`.
2. `POST /demo/run/{run_id}/{scenario}?agent_type=…` — the existing replay
   harness → real `DeliberationEngine` + `CommitStateMachine` events.

`pace_ms` (instant/fast/normal/slow) only spaces out delivery so a viewer
can follow; it never changes which events are produced.

## Verify end-to-end (real browser)

```bash
pip install playwright && python -m playwright install chromium
python3 scripts/e2e_ui_check.py http://localhost:4173 barge_in
```

## Files

- `src/lib/config.js` — WS URL builder (same-origin; Vite proxies `/ws`, `/demo`, `/calls`, `/health`).
- `src/lib/socket.js` — WebSocket wrapper. `src/lib/useEventSocket.js` — status + event history hook.
- `src/components/` — `ConversationPanel`, `DeliberationPanel`, `DispatchPanel`,
  `MetricsBar`, `LiveControls`, `DemoControls`, `ConnectionBadge`, `Panel`, `DebugEventStream`.
- `src/App.jsx` — layout: 3 columns ≥1024px, stacked below.

## Known limitations (honest)

- Live text call turns use `DeliberationEngine` and `CommitStateMachine` directly;
  they do not run through the mock `DialogueEngine` STT/TTS pipeline. Browser
  Web Speech features are browser-dependent, not a low-latency provider stack.
- `MetricsTick.time_to_decision_ms` is defined in `core/events.py` but no
  core component populates it yet, so the UI shows "not reported".
- `barge_in_latency_ms` is the engine's real measured stop latency; against
  mock TTS/AudioIO this is ~0 ms. Real numbers need real audio (Phase 7C+).
- "Live stream" is derived from event arrival — `core/events.py` has no
  explicit call-start/call-end event.
- The replay harness itself emits a `BargeIn` with a fixed `latency_ms=90`
  (harness/replay.py); it is counted in the MetricsBar but not drawn as a
  conversation divider, since that stream has no transcript to anchor it.
