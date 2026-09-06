#!/usr/bin/env python3
"""Pre-demo environment check — makes NO paid API calls.

Catches the three most common "mobile demo is broken" causes in one run:

1. LIVEKIT_URL is http://, localhost, or a LAN IP  -> phone can never connect
   and/or the browser refuses microphone access on an insecure origin.
2. Required API keys missing or still set to placeholder values.
3. Installed livekit-* versions drifted from the pinned requirements.txt
   (the fence hooks were verified against livekit-agents 1.8.x).

Exit code 0 = safe to demo. Exit code 1 = fix the FAIL lines first.

Usage:
    python scripts/check_env.py
"""

from __future__ import annotations

import importlib.metadata
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from shared.config import AgentConfig  # noqa: E402

LAN_IP = re.compile(r"^(192\.168\.|10\.|172\.(1[6-9]|2\d|3[01])\.|127\.)")

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
results: list[tuple[str, str]] = []


def report(status: str, msg: str) -> None:
    results.append((status, msg))


def check_livekit_url(cfg: AgentConfig) -> None:
    url = cfg.livekit_url
    if not url:
        report(FAIL, "LIVEKIT_URL is empty — set wss://<project>.livekit.cloud")
        return
    if url.startswith(("http://", "https://")):
        report(
            FAIL,
            f"LIVEKIT_URL={url!r} is HTTP(S). Agents need ws:// or wss://. "
            "For the mobile demo it MUST be wss://<project>.livekit.cloud.",
        )
        return
    host = url.split("://", 1)[-1].split("/")[0].split(":")[0]
    if host in ("localhost",) or LAN_IP.match(host):
        report(
            FAIL,
            f"LIVEKIT_URL host {host!r} is local/LAN — a phone cannot reach it. "
            "Use LiveKit Cloud (wss://<project>.livekit.cloud).",
        )
        return
    if url.startswith("ws://"):
        report(WARN, "LIVEKIT_URL is unencrypted ws:// — fine locally, not for a phone demo.")
        return
    if url.startswith("wss://"):
        report(PASS, f"LIVEKIT_URL is wss:// on a public host ({host})")
        return
    report(WARN, f"LIVEKIT_URL {url!r} has an unrecognised scheme.")


def check_keys(cfg: AgentConfig) -> None:
    placeholders = {"", "...", "xxx", "changeme", "your-key", "your_project"}
    for label, value in (
        ("LIVEKIT_API_KEY", cfg.livekit_api_key),
        ("LIVEKIT_API_SECRET", cfg.livekit_api_secret),
        ("RIME_API_KEY", cfg.rime_api_key),
        ("DEEPGRAM_API_KEY", cfg.deepgram_api_key),
        ("OPENAI_API_KEY", cfg.openai_api_key),
    ):
        v = (value or "").strip().lower()
        if v in placeholders or len(v) < 8:
            report(FAIL, f"{label} is missing or a placeholder")
        else:
            report(PASS, f"{label} present ({value[:4]}…, {len(value)} chars)")


def check_dependency_pins() -> None:
    expected = {
        "livekit-agents": "1.8.0",
        "livekit-plugins-rime": "1.8.0",
        "livekit-plugins-deepgram": "1.8.0",
        "livekit-plugins-openai": "1.8.0",
        "livekit-plugins-silero": "1.8.0",
    }
    for dist, pinned in expected.items():
        try:
            installed = importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            report(FAIL, f"{dist} is not installed (pinned requirement: {pinned})")
            continue
        if installed == pinned:
            report(PASS, f"{dist}=={installed} matches pin")
        else:
            report(
                FAIL,
                f"{dist}=={installed} installed, but the fence hooks were verified "
                f"against {pinned}. Reinstall from the pinned requirements.txt.",
            )


def main() -> int:
    cfg = AgentConfig.from_env()
    check_livekit_url(cfg)
    check_keys(cfg)
    check_dependency_pins()

    width = max((len(m) for _, m in results), default=0)
    for status, msg in results:
        print(f"[{status}] {msg}")
    print("-" * (width + 7))

    fails = sum(1 for s, _ in results if s == FAIL)
    warns = sum(1 for s, _ in results if s == WARN)
    if fails:
        print(f"{fails} FAIL, {warns} WARN — fix the FAIL lines before demoing.")
        return 1
    print(f"0 FAIL, {warns} WARN — environment is demo-safe.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
