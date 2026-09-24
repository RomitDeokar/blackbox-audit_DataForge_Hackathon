"""Phase 7B-2C end-to-end browser check (optional; needs `playwright`).

Opens the real built UI (vite preview, which proxies to the real FastAPI
backend), connects the WebSocket, clicks the one demo write action, and
asserts that real events rendered into every panel + the MetricsBar.

    uvicorn api.app:app --port 8000 &
    (cd ui && npm run build && npx vite preview --port 4173) &
    python3 scripts/e2e_ui_check.py [http://localhost:4173] [scenario]
"""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:4173"
SCENARIO = sys.argv[2] if len(sys.argv) > 2 else "barge_in"


def main() -> int:
    checks: list[tuple[str, bool, str]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(URL)
        page.get_by_role("button", name="Connect", exact=True).click()
        page.get_by_text("CONNECTED", exact=True).first.wait_for(timeout=5000)
        checks.append(("websocket connected", True, "badge shows CONNECTED"))

        page.select_option("select[aria-label=scenario]", SCENARIO)
        page.select_option("select[aria-label=pace]", "0")
        page.get_by_role("button", name="▶ Run demo call").click()
        page.get_by_text("demo run complete").wait_for(timeout=15000)

        # text_content: raw DOM text (inner_text applies CSS uppercase).
        body = page.text_content("body")
        def has(label, text):
            checks.append((label, text in body, text))

        has("conversation: caller line", "My car broke down on Highway 9")
        has("conversation: agent line", "Dispatching a tow truck")
        has("conversation: backchannel", "agent (backchannel)")
        has("deliberation: record rendered", "Known")
        has("deliberation: re-deliberation banner", "re-deliberating")
        has("dispatch: finalized state", "FINALIZED")
        has("dispatch: aborted (superseded)", "superseded_by_redeliberation")
        if SCENARIO == "barge_in":
            has("conversation: barge-in divider", "barge-in")
            footer = page.text_content("footer")
            import re
            m = re.search(r"(Interrupted|Barge-ins) · (\d+)", footer)
            checks.append(("metrics: barge-in indicator", bool(m) and int(m.group(2)) >= 1, m.group(0) if m else footer))
            checks.append(("metrics: barge-in latency from MetricsTick", re.search(r"Barge-in latency\s*\d+ ms", footer) is not None and "1 MetricsTick" in page.inner_html("footer"), footer[:60]))
            checks.append(("metrics: time-to-decision not fabricated", "not reported" in footer, "shown as 'not reported'"))
        checks.append(("no page JS errors", not errors, "; ".join(errors) or "none"))

        page.screenshot(path="/tmp/triage_line_e2e.png", full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path="/tmp/triage_line_e2e_mobile.png", full_page=True)
        browser.close()

    ok = True
    for label, passed, detail in checks:
        ok &= passed
        print(f"{'PASS' if passed else 'FAIL'}  {label}  [{detail}]")
    print("E2E RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
