"""Phase 7B-2C: DialogueEngine demo bridge (api/dialogue_bridge.py) and the
/demo/dialogue endpoint. Verifies the Conversation-layer events the UI's
Conversation panel and MetricsBar render are produced by the *real*
DialogueEngine and actually cross the WebSocket -- MetricsTick included.

Requires fastapi/httpx (like tests/test_api.py); skipped by _run_all.py
otherwise.
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from api.app import create_app
from api.dialogue_bridge import run_dialogue_scenario
from api.replay_bridge import available_demo_scenarios


def _run(sid: str):
    return asyncio.run(run_dialogue_scenario(available_demo_scenarios()[sid]))


def test_dialogue_bridge_produces_transcripts_and_backchannels():
    result = _run("normal_tow")
    types = [e["event_type"] for e in result.event_trace]
    assert result.success
    assert "PartialTranscript" in types
    assert "FinalTranscript" in types
    assert "BackchannelSent" in types
    speakers = {e["speaker"] for e in result.event_trace if e["event_type"] == "FinalTranscript"}
    assert speakers == {"caller", "agent"}


def test_barge_in_scenario_emits_real_barge_in_and_metrics_tick_from_engine():
    result = _run("barge_in")
    barge = [e for e in result.event_trace if e["event_type"] == "BargeIn"]
    ticks = [e for e in result.event_trace if e["event_type"] == "MetricsTick"]
    assert len(barge) == 1 and len(ticks) == 1
    # MetricsTick carries exactly the latency the engine measured on BargeIn.
    assert ticks[0]["barge_in_latency_ms"] == barge[0]["latency_ms"]
    assert isinstance(ticks[0]["barge_in_latency_ms"], int)
    # time_to_decision_ms is not produced by any core component yet --
    # it must stay None rather than being fabricated.
    assert ticks[0]["time_to_decision_ms"] is None
    # Barge-in lands mid-utterance, not at position 0.
    assert barge[0]["position_ms"] > 0


def test_scenario_without_barge_in_emits_no_metrics_tick():
    result = _run("normal_tow")
    assert not any(e["event_type"] in ("BargeIn", "MetricsTick") for e in result.event_trace)


def test_dialogue_endpoint_unknown_scenario_returns_404():
    client = TestClient(create_app())
    assert client.post("/demo/dialogue/r/not_a_real_scenario").status_code == 404


def test_dialogue_endpoint_relays_events_in_order_over_websocket():
    client = TestClient(create_app())
    with client.websocket_connect("/ws/replay/dlg1") as ws:
        response = client.post("/demo/dialogue/dlg1/barge_in")
        assert response.status_code == 200
        body = response.json()
        received = [ws.receive_json() for _ in range(len(body["event_trace"]))]
    assert [e["event_type"] for e in received] == [e["event_type"] for e in body["event_trace"]]
    assert body["event_counts"].get("MetricsTick") == 1
    assert body["event_counts"].get("BargeIn") == 1


def test_pace_ms_is_accepted_and_does_not_change_the_trace():
    client = TestClient(create_app())
    fast = client.post("/demo/run/p1/normal_tow").json()
    paced = client.post("/demo/run/p2/normal_tow", params={"pace_ms": 5}).json()
    assert [e["event_type"] for e in fast["event_trace"]] == [
        e["event_type"] for e in paced["event_trace"]
    ]
