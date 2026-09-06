import json
import os
import sys

import requests
from dotenv import load_dotenv

SENTENCE = "Okay, you're confirmed for a table for 4 at 7 PM."
RIME_TTS_URL = "https://users.rime.ai/v1/rime-tts"


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


def main():
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

        if timing_sections:
            print("\nTiming/alignment information:")
            for section in timing_sections:
                print(json.dumps(section, indent=2) if isinstance(section, dict) else section)
        else:
            print("\nNo word-level timestamps or timing/alignment section found in the response.")

    except requests.RequestException as exc:
        print("Rime request failed. No automatic retry was attempted.")
        print(f"Error: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
