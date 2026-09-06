import argparse
import json
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

SENTENCE = "Okay, you're confirmed for a table for 4 at 7 PM."
RIME_TTS_URL = "https://users.rime.ai/v1/rime-tts"
DEFAULT_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "rime_confirmation_timestamps.json"


def parse_sse_events(response):
    event_type = None
    data_lines = []

    for raw_line in response.iter_lines(decode_unicode=True):
        if raw_line is None:
            continue

        line = raw_line.strip()
        print(raw_line)

        if not line:
            if event_type or data_lines:
                yield event_type, "\n".join(data_lines)
            event_type = None
            data_lines = []
            continue

        if line.startswith("event:"):
            event_type = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").strip())

    if event_type or data_lines:
        yield event_type, "\n".join(data_lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One-shot Rime TTS timestamp probe (exactly one paid API call)."
    )
    parser.add_argument(
        "--save-fixture",
        metavar="PATH",
        nargs="?",
        const=str(DEFAULT_FIXTURE),
        help=(
            "After a successful capture, write the timestamps frame to PATH "
            "(default: fixtures/rime_confirmation_timestamps.json). Nothing is "
            "written unless a real 'timestamps' event was received."
        ),
    )
    args = parser.parse_args(argv)

    load_dotenv()
    api_key = os.getenv("RIME_API_KEY")

    if not api_key:
        print("Missing RIME_API_KEY. Create a local .env file with RIME_API_KEY set.")
        return 1

    headers = {
        "Accept": "text/event-stream",
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "text": SENTENCE,
        "modelId": "mistv2",
        "speaker": "astra",
        "lang": "eng",
        "samplingRate": 22050,
        "speedAlpha": 1.0,
    }

    print("API calls planned: 1")
    print("API calls made: 0")
    print("Sending one Rime TTS request using the documented text/event-stream format.")
    print("Raw Rime response:")

    try:
        response = requests.post(
            RIME_TTS_URL,
            headers=headers,
            json=payload,
            stream=True,
            timeout=30,
        )
        print("API calls made: 1")
        print(f"HTTP {response.status_code}")
        response.raise_for_status()

        timing_sections = []
        for event_type, data in parse_sse_events(response):
            if event_type == "timestamps":
                try:
                    timing_sections.append(json.loads(data))
                except json.JSONDecodeError:
                    timing_sections.append(data)

        if not timing_sections:
            print("\nNo word-level timestamps or timing/alignment section found in the response.")
            return 1

        print("\nTiming/alignment information:")
        for section in timing_sections:
            print(json.dumps(section, indent=2) if isinstance(section, dict) else section)

        first = timing_sections[0]
        if not isinstance(first, dict) or "word_timestamps" not in first:
            print(
                "\nReceived a timestamps event with an unexpected shape; "
                "refusing to write a fixture without verifying it."
            )
            return 1

        if args.save_fixture:
            out = Path(args.save_fixture)
            out.parent.mkdir(parents=True, exist_ok=True)
            frame = {
                "_comment": (
                    "Live-captured Rime 'timestamps' frame (see scripts/test_rime.py). "
                    "Captured shape: word_timestamps.words/start/end."
                ),
                "_provenance": "live-capture",
                "_sentence": SENTENCE,
                "_model": payload["modelId"],
                "_speaker": payload["speaker"],
                "_sampling_rate": payload["samplingRate"],
                **first,
            }
            out.write_text(json.dumps(frame, indent=2) + "\n", encoding="utf-8")
            print(f"\nFixture written to {out}")

    except requests.RequestException as exc:
        print("Rime request failed. No automatic retry was attempted.")
        print(f"Error: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
