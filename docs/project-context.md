# BlackBox Audit: Prompts, Context, and Progress

## Project Goal

BlackBox Audit is a voice-agent reliability experiment.

The main question is:

```text
If a voice agent starts confirming something out loud, and the user interrupts mid-sentence, does the backend action still match what the user actually heard and intended?
```

Example test case:

```text
User: Book a table for 4 at 7 PM.
Agent: Okay, you're confirmed for a table for 4 at 7 PM.
User: Wait! Make that 8 PM instead.
```

The project should eventually check whether the backend booked 7 PM or 8 PM, and whether that matches the spoken interaction.

## Original Setup Prompt

The requested project name was:

```text
blackbox-audit
```

The requested setup was a Python 3.11 project with only the initial scaffold and first tiny proof-of-concept setup.

The instruction was explicitly not to build the full agent, database logic, chaos harness, or dashboard yet.

Requested folder structure:

```text
blackbox-audit/
├── agent/
├── harness/
├── shared/
├── reports/
├── docs/
├── scripts/
├── .env.example
├── .gitignore
├── README.md
└── requirements.txt
```

Requested `requirements.txt` packages:

```text
livekit-agents
openai
deepgram-sdk
requests
python-dotenv
rich
pytest
```

Versions were intentionally not pinned yet.

## Environment Prompt

The prompt originally included environment variables for LiveKit, Rime, OpenAI, and Deepgram.

Final committed `.env.example` uses blank values only:

```text
LIVEKIT_URL=
LIVEKIT_API_KEY=
LIVEKIT_API_SECRET=
RIME_API_KEY=
OPENAI_API_KEY=
DEEPGRAM_API_KEY=
```

Real API keys were kept out of Git. The local `.env` file was not committed or pushed.

## Git Ignore Prompt

Requested `.gitignore` entries:

```text
.env
**pycache**/
*.pyc
.venv/
venv/
reports/*.json
.DS_Store
```

`.pytest_cache/` was also added later so local pytest cache files are not committed.

## Constants Prompt

Requested `shared/constants.py`:

```python
RESTAURANT_NAME = "BlackBox Bistro"
TEST_CUSTOMER_NAME = "Amitoj"
TEST_PARTY_SIZE = 4
TEST_BOOKING_TIME = "7:00 PM"
TEST_BOOKING_REQUEST = "Book a table for 4 at 7 PM."
CONFIRMATION_PHRASE = "Okay, you're confirmed for a table for 4 at 7 PM."
INTERRUPTION_PHRASE = "Wait! Make that 8 PM instead."
```

## Rime Test Prompt

Requested script:

```text
scripts/test_rime.py
```

Purpose:

- Test the Rime API independently before any voice-agent code is written.
- Load `RIME_API_KEY` from `.env` using `python-dotenv`.
- Send this exact sentence to Rime TTS:

```text
Okay, you're confirmed for a table for 4 at 7 PM.
```

- Print the raw response returned by Rime.
- If the response contains word-level timestamps or timing/alignment information, print that section separately and clearly.
- Do not invent the expected Rime JSON format.
- Use the current official Rime API format only if it can be determined.
- If the endpoint or response shape cannot be verified, stop instead of guessing.
- Do not integrate LiveKit, OpenAI, Deepgram, database code, or interruption handling yet.

## API Budget Prompt

The project has a strict paid API budget rule:

- Treat 50% of every external API quota or credit balance as reserved.
- Make the minimum number of real API calls necessary.
- Never run large loops, repeated retries, stress tests, or trial sweeps unless explicitly authorized.
- Prefer mocks, fixtures, cached responses, and local simulation.
- If one successful API call gives the response structure needed, save/reuse that response instead of calling repeatedly.
- Do not automatically retry failed API calls more than once.
- Do not make API calls just to test formatting, CLI output, database logic, rollback logic, parsing, or reporting.
- Before running anything that could make more than 5 external API calls, stop and ask for permission.
- Never launch the 300-trial chaos test using real paid API calls by default.
- For LLM calls, keep prompts and outputs small and use the cheapest suitable model configured by the project.
- Print or log an API-call counter during development where practical.
- Never expose, print, commit, or hardcode API keys.
- If remaining provider quota cannot be determined programmatically, minimize usage and warn before potentially expensive operations.

## Part 1 Prompt

After Part 0, the requested next step was:

```text
Do PART 1 ONLY: repository/scaffold cleanup and verification.
```

Part 1 instructions:

- Do not make real external API calls.
- Do not run loops, retries, stress tests, or integration tests against Rime, OpenAI, LiveKit, or Deepgram.
- Verify or create the requested folder structure.
- Ensure `.env.example` contains blank values.
- Ensure `.gitignore` includes the requested entries.
- Create or verify `shared/constants.py`.
- Do not implement database logic, LiveKit logic, agents, interruption handling, or chaos harness logic yet.
- Do not overwrite the working Rime test.
- Do not modify existing passing tests unless required for scaffold consistency.
- Print the folder tree.
- Run pytest once.
- Show pass/fail output.
- Summarize exactly what changed.
- Stop and do not proceed to Part 2.

## What Has Been Done

The project has been created and pushed to GitHub:

```text
https://github.com/amitojsingh1306-prog/blackbox-audit
```

Completed so far:

- Python 3.11 project scaffold created.
- Virtual environment created and working.
- `requirements.txt` added.
- `.env.example` added with blank API key fields.
- `.gitignore` added.
- README updated with project goal and run instructions.
- Folder structure created:
  - `agent/`
  - `harness/`
  - `shared/`
  - `reports/`
  - `docs/`
  - `scripts/`
  - `tests/`
- Rime standalone timestamp test created.
- Rime test was run once and confirmed word-level timestamp output.
- Local fake booking API created.
- Pytest tests added and passing.
- Repository initialized, committed, and pushed to GitHub.

## Rime Result

Rime returned word-level timestamps in this shape:

```json
{
  "word_timestamps": {
    "words": [],
    "start": [],
    "end": []
  }
}
```

This confirmed that the project can identify which words were spoken by a given timestamp.

## Fake Booking API

File:

```text
harness/fake_booking_api.py
```

This is not a real restaurant API. It is a safe local test backend that creates a pretend restaurant table booking.

Example output:

```json
{
  "customer_name": "Amitoj",
  "party_size": 4,
  "requested_time": "7:00 PM",
  "confirmed_time": "7:00 PM",
  "status": "confirmed"
}
```

Why it exists:

Before using real APIs or a real voice agent, the project needs a safe local backend to compare against what the user heard.

## Tests

Tests live in:

```text
tests/test_fake_booking_api.py
```

Current tests verify:

- A booking for 4 people at 7 PM records `7:00 PM`.
- The JSON output shape is predictable.
- The fake booking API does not call external APIs.

Current result:

```text
3 passed
```

## Current Run Commands

Run tests:

```bash
cd /Users/amitoj/Documents/Codex/2026-09-04/create-a-new-project-folder-named/blackbox-audit
source .venv/bin/activate
pytest
```

Run fake booking locally:

```bash
python -c "from harness.fake_booking_api import create_booking; print(create_booking('Amitoj', 4, '7:00 PM'))"
```

Run Rime timestamp test only when intentionally spending one Rime API call:

```bash
python scripts/test_rime.py
```

## What Is Not Built Yet

The following are not implemented yet:

- Real LiveKit voice agent.
- OpenAI agent logic.
- Deepgram integration.
- Real interruption handling.
- Database logic.
- Chaos harness.
- Dashboard/frontend.
- 300-trial test runner.
- Any production booking system.

## Suggested Next Step

Next should be Part 2.

A good Part 2 would be a local interruption simulator using the confirmed Rime word timestamp shape.

It should not use real LiveKit, OpenAI, or Deepgram yet.

It should answer:

```text
At a given interruption time, which words had the user already heard?
```

Example:

```text
Interrupt at 2.1 seconds
User heard: "Okay, you're confirmed for a table for 4 at seven"
```

Then later the project can compare that heard text against the fake booking backend.
