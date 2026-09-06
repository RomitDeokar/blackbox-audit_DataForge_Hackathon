# BlackBox Audit

BlackBox Audit is a voice-agent reliability experiment that tests whether backend actions stay consistent with what the user actually heard during an interrupted voice interaction.

The core question:

```text
If a voice agent starts saying "Okay, you're confirmed for a table for 4 at 7 PM,"
and the user interrupts with "Wait! Make that 8 PM instead,"
does the backend booking match what the user actually heard and intended?
```

This project is an audit harness, not a booking product. The goal is to catch mismatches between spoken confirmations, interruptions, and backend actions before a real voice agent is trusted with real-world state changes.

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Paid API usage is intentionally conservative. Keep at least 50% of every provider quota reserved, make the minimum real calls needed, prefer mocks and fixtures during development, and stop for permission before any operation that could make more than 5 external API calls.

Create a local `.env` from the example file:

```bash
cp .env.example .env
```

Do not commit `.env`. Keep real API keys local or in a proper team secret manager.

## Rime timestamp test

`scripts/test_rime.py` is a standalone proof of concept for Rime TTS timing data.

It sends this sentence to Rime:

```text
Okay, you're confirmed for a table for 4 at 7 PM.
```

It prints the raw response and separately prints any timestamp or alignment section returned by Rime.

Run it only when you intentionally want to spend one Rime API call:

```bash
python scripts/test_rime.py
```

## Running the agent

Not implemented yet.

The real LiveKit/OpenAI/Deepgram voice agent is intentionally out of scope for the current scaffold.

## Running the chaos harness

Not implemented yet.

The future harness will simulate interruption timing against spoken-word timestamps, then compare the user-heard phrase with the final backend booking state.

## Results

Current local proof of concept:

- `harness/fake_booking_api.py` provides a safe fake booking backend.
- `shared/models.py` defines the booking shape.
- `shared/constants.py` keeps the test restaurant/customer phrases.
- `tests/test_fake_booking_api.py` verifies the local booking behavior.

Run the local tests:

```bash
pytest
```

Run the fake booking proof manually:

```bash
python -c "from harness.fake_booking_api import create_booking; print(create_booking('Amitoj', 4, '7:00 PM'))"
```

This writes a local report to:

```text
reports/latest_booking.json
```

No frontend or dashboard exists yet.
