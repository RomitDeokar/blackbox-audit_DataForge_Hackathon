# BlackBox Audit

BlackBox Audit is a voice-agent reliability experiment that tests whether backend actions stay consistent with what the user actually heard during an interrupted voice interaction.

## Setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Paid API usage is intentionally conservative. Keep at least 50% of every provider quota reserved, make the minimum real calls needed, prefer mocks and fixtures during development, and stop for permission before any operation that could make more than 5 external API calls.

## Rime timestamp test

## Running the agent

## Running the chaos harness

## Results
