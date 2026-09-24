# Triage Line

Triage Line is a full-duplex, interruptible voice dispatch agent for
roadside-assistance / incident triage calls. It's built for the
"Interruptible Real-Time Agents — Full-duplex conversation agents with
deliberative reasoning skills" hackathon theme, and is designed to visibly
deliberate before taking any consequential action (like dispatching a tow
truck or escalating to emergency services), rather than acting on a single
LLM pass.

## Current implementation status

**Phase 7B complete; interactive call prototype added.** The original
replay/evaluation pipeline remains available. Interactive calls now accept
caller text, use the existing deliberation and confirmation-safe commit core,
publish real events over `/ws/call/{id}`, and retain their decision/action
trail in SQLite when `TRIAGE_DB_PATH` is configured. Browser speech recognition
and speech synthesis are optional conveniences (browser support and microphone
permission required); text input always works.

This is **not** a production emergency/roadside dispatch service. Confirming
records a proposal in the app; it does **not** contact a tow provider or
emergency services. No LiveKit, Deepgram, Groq, Rime, authenticated operators,
external dispatch, or measured end-to-end audio latency is included. Do not
use for actual emergencies. The replay-only Phase 7D comparison/audit screens
and full 7C provider transport remain future work. See `ui/README.md`.

## Project structure

```text
triage-line/
├── core/            # reasoning core: dialogue, deliberation, commit state machine
├── providers/       # STT/LLM/TTS/AudioIO interfaces + mock/live implementations
├── persistence/      # database + repositories
├── harness/          # offline replay/evaluation harness
├── reporting/         # trial-result summary tables
├── ui/               # live dashboard (WebSocket event consumer)
├── config/            # environment-driven settings
├── tests/            # unit tests
├── docs/             # architecture, requirements, UI spec, commit mechanism
├── run_demo.py        # entry point (placeholder in Phase 1)
└── requirements.txt
```

The system is designed to eventually run fully offline against mocked
STT/LLM/TTS providers, with a real-provider adapter layer (LiveKit +
Deepgram/Groq/Rime, or OpenAI Realtime) that can be wired in later with API
keys.

## Run interactive calls locally

```bash
cd triage-line
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
TRIAGE_DB_PATH=.data/triage.sqlite .venv/bin/uvicorn api.app:app --host 127.0.0.1 --port 8000
# separate terminal, from triage-line/ui
npm ci && npm run dev
```

Open http://localhost:5173, connect to the generated interactive call ID,
then enter a caller message. Review the decision, confirm/reject the pending
action, or correct the facts to force re-deliberation and abort the old action.
For optional voice, use the Mic button in a browser with Web Speech support.
Microphone access generally requires localhost or HTTPS. A newly generated ID
starts a new call; End call aborts any unconfirmed actions. A call's persisted
audit can be inspected with `GET /calls/{id}/audit` in this local prototype.
Run `./.venv/bin/python tests/_run_all.py` and `cd ui && npm run build`.

**Deployment warning:** endpoints are unauthenticated, SQLite uses a single
process, live session state and replayable WS history are memory-resident, and
operator data is exposed by the audit endpoint. Do not expose this service to
an untrusted network without access control, data retention policy, secure
transport, rate limits, session recovery, provider integrations and operational
safety review. Phase 6D reporting is unchanged; absent metrics remain absent.
