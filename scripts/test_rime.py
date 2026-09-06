import argparse
import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from shared.constants import TEST_BOOKING_TIME, TEST_PARTY_SIZE, confirmation_sentence

# One source of truth: the exact sentence the agents speak, so the fixture a
# real capture produces always matches what the harness replays.
SENTENCE = confirmation_sentence(TEST_PARTY_SIZE, TEST_BOOKING_TIME)
RIME_TTS_URL = "https://users.rime.ai/v1/rime-tts"
DEFAULT_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "rime_confirmation_timestamps.json"


def parse_sse_events(response):
    """Yield ``(event_type, data)`` pairs from a Rime SSE stream.

    Rime frames arrive either as ``event: <type>`` + ``data: <json>`` pairs or
    as bare ``data: <json>`` lines whose ``type`` field carries the frame kind.
    Handle both so the timestamps frame is found regardless of which style the
    endpoint uses — guessing only one style is exactly the kind of assumption
    this script exists to verify rather than make.
    """
    event_type = None
    data_lines = []

    def flush():
        if not (event_type or data_lines):
            return None
        data = "\n".join(data_lines)
        etype = event_type
        if etype is None:
            # No event: header — fall back to the JSON payload's own "type".
            try:
                etype = json.loads(data).get("type")
            except (json.JSONDecodeError, AttributeError):
                etype = None
        return etype, data

    for raw_line in response.iter_lines(decode_unicode=True):
        if raw_line is None:
            continue

        line = raw_line.strip()
        # Audio frames arrive as long base64 data lines; printing them raw turns
        # the terminal into noise around the timestamps frame we actually want.
        # Show event headers in full and truncate data payloads.
        if line.startswith("data:"):
            print(line[:160] + ("..." if len(line) > 160 else ""))
        else:
            print(line)

        if not line:
            flushed = flush()
            if flushed is not None:
                yield flushed
            event_type = None
            data_lines = []
            continue

        if line.startswith("event:"):
            event_type = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").strip())

    flushed = flush()
    if flushed is not None:
        yield flushed


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

    print(f"Sentence sent to Rime: {SENTENCE!r}")
    print("API calls planned: 1")
    print("API calls made: 0")
    print("Sending one Rime TTS request using the documented text/event-stream format.")
    print("Raw Rime response (data payloads truncated):")

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
        seen_payloads = []
        for event_type, data in parse_sse_events(response):
            seen_payloads.append(data)
            if event_type == "timestamps":
                try:
                    timing_sections.append(json.loads(data))
                except json.JSONDecodeError:
                    timing_sections.append(data)

        # Some frames embed the word timings inside a wrapper (e.g.
        # {"type": "chunk", "timestamps": {...}}); scan the captured payloads
        # one level deep rather than reporting a false "no timestamps found".
        if not timing_sections:
            for data in seen_payloads:
                try:
                    payload_json = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload_json, dict):
                    nested = payload_json.get("timestamps")
                    if isinstance(nested, dict) and "word_timestamps" in nested:
                        timing_sections.append(nested)

        if not timing_sections:
            print("\nNo word-level timestamps or timing/alignment section found in the response.")
            print("(Checked both 'event: timestamps' frames and JSON payloads with type='timestamps'.)")
            return 1

        print("\n--- word-level timestamps / alignment found ---")
        for section in timing_sections:
            print(json.dumps(section, indent=2)[:4000])

        if args.save_fixture:
            fixture_path = Path(args.save_fixture)
            fixture_path.parent.mkdir(parents=True, exist_ok=True)

            payload_copy = dict(payload)
            payload_copy.pop("text", None)
            stored = {
                "_comment": (
                    "Real Rime 'timestamps' capture. The harness replays this "
                    "offline, so sweep numbers quoted from it are Rime-measured."
                ),
                "_provenance": f"live-capture-{os.path.basename(fixture_path)}",
                "_sentence": SENTENCE,
                **payload_copy,
                "type": "timestamps",
                "word_timestamps": timing_sections[-1],
            }
            fixture_path.write_text(json.dumps(stored, indent=2) + "\n", encoding="utf-8")
            print(f"\nFixture written to {fixture_path}")

        return 0
    except requests.RequestException as exc:
        print(f"\nRime request failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
