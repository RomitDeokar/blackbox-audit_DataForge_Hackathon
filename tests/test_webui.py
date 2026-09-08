"""Tests for the demo web layer.

The point of these tests is not to check that FastAPI works -- it is to pin the
claim the demo makes on stage: **the browser is shown the real fence's verdict,
not a re-implementation of it.** So the assertions here compare API output
against ``shared.audio_fence`` / ``shared.booking_store`` directly, and check
that the UI cannot commit something the caller did not hear.

They also enforce the demo's cost rule: no test in this file may touch the
network, and none of them requires an API key.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi", reason="webui demo layer requires fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from shared import booking_store as bs  # noqa: E402
from shared.audio_fence import evaluate_ground_truth  # noqa: E402
from shared.constants import (  # noqa: E402
    CONFIRMATION_GATING_WORD,
    SWEEP_TRIALS_PER_TARGET,
)
from shared.rime_timestamps import load_timeline  # noqa: E402
from webui import service as svc  # noqa: E402
from webui.app import app  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A client whose store points at a tmp DB, so tests never touch results/."""
    db = tmp_path / "webui_test.db"
    monkeypatch.setattr(svc, "DEMO_DB_PATH", db)
    monkeypatch.setattr(svc, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(svc, "_SERVICE", None)
    bs.set_db_path(db)
    bs.init_db()
    with TestClient(app) as c:
        yield c
    bs.set_db_path(None)


@pytest.fixture()
def timeline():
    return load_timeline()


@pytest.fixture()
def gating_end(timeline):
    return timeline.gating_time(CONFIRMATION_GATING_WORD)


# ---------------------------------------------------------------- meta


def test_health_reports_replay_mode(client):
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["mode"] == "replay"


def test_timeline_matches_the_real_fixture(client, timeline, gating_end):
    body = client.get("/api/timeline").json()
    assert body["duration_s"] == pytest.approx(timeline.duration)
    assert body["gating_word_end_s"] == pytest.approx(gating_end)
    assert len(body["words"]) == len(timeline)
    assert [w["text"] for w in body["words"]] == [w.text for w in timeline.words]


def test_exactly_one_word_is_flagged_as_gating(client):
    words = client.get("/api/timeline").json()["words"]
    gating = [w for w in words if w["is_gating"]]
    assert len(gating) == 1
    assert gating[0]["text"].lower().strip(",.") == CONFIRMATION_GATING_WORD


def test_config_never_leaks_key_values(client):
    body = client.get("/api/config").json()
    raw = json.dumps(body)
    for provider in body["providers"]:
        # Only presence is exposed, never the secret itself.
        assert set(provider) == {"key", "purpose", "configured"}
        assert isinstance(provider["configured"], bool)
    assert "sk-" not in raw


def test_config_advertises_zero_key_demo(client):
    body = client.get("/api/config").json()
    assert body["demo_mode"] == "replay"
    assert body["acceptance"]["trials_per_target"] == SWEEP_TRIALS_PER_TARGET


# ---------------------------------------------------------------- heard/plan oracle parity


@pytest.mark.parametrize("at_s", [0.0, 0.5, 1.0, 1.276, 1.5, 2.5, 3.6])
def test_heard_endpoint_agrees_with_the_oracle(client, timeline, at_s):
    body = client.get("/api/heard", params={"at": at_s}).json()
    assert body["heard_text"] == timeline.heard_text_by(at_s)
    assert body["should_commit"] is evaluate_ground_truth(
        timeline, CONFIRMATION_GATING_WORD, at_s
    )


@pytest.mark.parametrize("offset_ms", [-500, -200, -10, 0, 10, 200, 500])
def test_plan_is_keyed_to_the_gating_word_end(client, gating_end, offset_ms):
    body = client.get("/api/plan", params={"offset_ms": offset_ms}).json()
    assert body["gating_word_end_s"] == pytest.approx(gating_end)
    assert body["cancel_at_s"] == pytest.approx(gating_end + offset_ms / 1000.0)
    assert body["expected_heard_gating_word"] is (offset_ms >= 0)


# ---------------------------------------------------------------- the core claim


def _start(client, variant):
    r = client.post("/api/call/start", json={"variant": variant})
    assert r.status_code == 200, r.text
    return r.json()


def test_fenced_call_starts_pending_not_committed(client):
    data = _start(client, "fenced")
    assert data["db_status"] == bs.STATUS_PENDING_AUDIO
    assert data["fence_state"] == "PENDING"
    assert data["resolved"] is False


def test_naive_call_is_committed_before_a_word_is_spoken(client):
    data = _start(client, "naive")
    # This is the bug, on purpose: committed with words_heard == 0.
    assert data["db_status"] == bs.STATUS_COMMITTED
    assert data["words_heard"] == 0
    assert data["fence_state"] is None


def test_fenced_rolls_back_when_interrupted_before_the_gating_word(client, gating_end):
    session = _start(client, "fenced")
    body = client.post(
        "/api/call/barge-in",
        json={"session_id": session["session_id"], "position_s": gating_end - 0.2},
    ).json()

    assert body["db_status"] == bs.STATUS_ROLLED_BACK
    assert body["expected_committed"] is False
    assert body["mismatch"] is False
    assert body["fence_outcome"] == "CANCELLED_BEFORE_GATING_WORD"
    assert CONFIRMATION_GATING_WORD not in body["heard_text"].lower()


def test_fenced_commits_when_interrupted_after_the_gating_word(client, gating_end):
    session = _start(client, "fenced")
    body = client.post(
        "/api/call/barge-in",
        json={"session_id": session["session_id"], "position_s": gating_end + 0.1},
    ).json()

    assert body["db_status"] == bs.STATUS_COMMITTED
    assert body["expected_committed"] is True
    assert body["mismatch"] is False
    assert body["fence_outcome"] == "GATING_WORD_HEARD"


def test_naive_produces_a_phantom_booking_on_the_same_interruption(client, gating_end):
    session = _start(client, "naive")
    body = client.post(
        "/api/call/barge-in",
        json={"session_id": session["session_id"], "position_s": gating_end - 0.2},
    ).json()

    # The caller never heard "confirmed", yet the row is committed.
    assert body["db_status"] == bs.STATUS_COMMITTED
    assert body["expected_committed"] is False
    assert body["mismatch"] is True


def test_the_only_difference_between_variants_is_the_commit_rule(client, gating_end):
    """The headline A/B claim, asserted end-to-end through the API."""
    cut_at = gating_end - 0.2

    fenced = client.post(
        "/api/call/barge-in",
        json={"session_id": _start(client, "fenced")["session_id"], "position_s": cut_at},
    ).json()
    naive = client.post(
        "/api/call/barge-in",
        json={"session_id": _start(client, "naive")["session_id"], "position_s": cut_at},
    ).json()

    # Identical interruption, identical audio, identical heard transcript...
    assert fenced["heard_text"] == naive["heard_text"]
    assert fenced["expected_committed"] == naive["expected_committed"] is False
    # ...opposite backend outcomes.
    assert fenced["mismatch"] is False
    assert naive["mismatch"] is True


def test_uninterrupted_playback_commits(client):
    session = _start(client, "fenced")
    body = client.post("/api/call/finish", json={"session_id": session["session_id"]}).json()
    assert body["db_status"] == bs.STATUS_COMMITTED
    assert body["mismatch"] is False
    assert body["completed"] is True


def test_hang_up_never_leaves_a_row_pending(client):
    session = _start(client, "fenced")
    body = client.post("/api/call/hang-up", json={"session_id": session["session_id"]}).json()
    assert body["db_status"] != bs.STATUS_PENDING_AUDIO
    assert bs.count_bookings(status=bs.STATUS_PENDING_AUDIO) == 0


@pytest.mark.parametrize("offset", [-0.5, -0.3, -0.1, -0.01, 0.01, 0.1, 0.3, 0.5])
def test_fenced_never_mismatches_across_the_offset_ladder(client, gating_end, offset):
    """A miniature acceptance sweep driven entirely through the HTTP layer."""
    session = _start(client, "fenced")
    position = max(0.0, gating_end + offset)
    body = client.post(
        "/api/call/barge-in",
        json={"session_id": session["session_id"], "position_s": position},
    ).json()
    assert body["mismatch"] is False, f"fence mismatched at offset {offset:+.3f}s"


def test_advance_feeds_words_progressively(client, gating_end):
    session = _start(client, "fenced")
    sid = session["session_id"]

    early = client.post("/api/call/advance", json={"session_id": sid, "position_s": 0.5}).json()
    assert early["words_heard"] >= 1
    assert early["db_status"] == bs.STATUS_PENDING_AUDIO  # still not real

    late = client.post(
        "/api/call/advance", json={"session_id": sid, "position_s": gating_end}
    ).json()
    # Crossing the gating word commits immediately -- the caller heard it.
    assert late["db_status"] == bs.STATUS_COMMITTED


# ---------------------------------------------------------------- validation


def test_unknown_variant_is_rejected(client):
    body = client.post("/api/call/start", json={"variant": "sneaky"}).json()
    assert "error" in body


def test_unknown_session_is_404(client):
    r = client.post("/api/call/barge-in", json={"session_id": "nope", "position_s": 1.0})
    assert r.status_code == 404


def test_negative_position_is_rejected_by_schema(client):
    session = _start(client, "fenced")
    r = client.post(
        "/api/call/barge-in", json={"session_id": session["session_id"], "position_s": -1.0}
    )
    assert r.status_code == 422


# ---------------------------------------------------------------- sweeps


def test_sweep_endpoint_runs_real_trials(client):
    body = client.post(
        "/api/sweep",
        json={"target": "fenced", "min_ms": -100, "max_ms": 100, "step_ms": 50, "runs_per_offset": 1},
    ).json()
    assert body["trials"] == 5
    assert body["mismatches"] == 0
    assert body["passed"] is True
    assert body["orphans"] == 0
    assert len(body["records"]) == 5


def test_naive_sweep_produces_mismatches_on_negative_offsets(client):
    body = client.post(
        "/api/sweep",
        json={"target": "naive", "min_ms": -200, "max_ms": -100, "step_ms": 50, "runs_per_offset": 1},
    ).json()
    assert body["mismatches"] == body["trials"] > 0
    assert body["passed"] is False


def test_sweep_rejects_absurd_offset_counts(client):
    body = client.post(
        "/api/sweep", json={"target": "fenced", "min_ms": -5000, "max_ms": 5000, "step_ms": 1}
    ).json()
    assert "error" in body


def test_acceptance_endpoint_reproduces_the_headline_result(client):
    body = client.post("/api/acceptance", json={"runs_per_offset": 1}).json()
    fenced, naive = body["sweeps"]["fenced"], body["sweeps"]["naive"]

    assert fenced["mismatches"] == 0
    assert fenced["orphans"] == 0
    assert naive["mismatches"] > 0
    assert body["summary"]["verdict"] == "PASS"


def test_summary_endpoint_matches_the_dashboard(client):
    client.post("/api/acceptance", json={"runs_per_offset": 1})
    body = client.get("/api/summary").json()
    assert body["verdict"] == "PASS"
    assert body["targets"]["fenced"]["mismatches"] == 0


def test_chart_endpoint_returns_svg(client):
    client.post("/api/sweep", json={"target": "fenced", "min_ms": -50, "max_ms": 50, "step_ms": 50})
    r = client.get("/api/chart.svg")
    assert r.status_code == 200
    assert "image/svg+xml" in r.headers["content-type"]
    assert r.text.lstrip().startswith(("<?xml", "<svg"))


# ---------------------------------------------------------------- audit views


def test_bookings_endpoint_reflects_the_store(client, gating_end):
    client.post(
        "/api/call/barge-in",
        json={"session_id": _start(client, "naive")["session_id"], "position_s": gating_end - 0.2},
    )
    body = client.get("/api/bookings").json()
    assert body["counts"]["total"] == bs.count_bookings()
    assert body["counts"]["naive_committed"] >= 1
    assert any(e["event_type"] == "NAIVE_COMMIT" for e in body["transaction_log"])


def test_fenced_rollback_is_visible_in_the_transaction_log(client, gating_end):
    client.post(
        "/api/call/barge-in",
        json={"session_id": _start(client, "fenced")["session_id"], "position_s": gating_end - 0.2},
    )
    log = client.get("/api/bookings").json()["transaction_log"]
    events = {e["event_type"] for e in log}
    assert {"PENDING_CREATED", "ROLLED_BACK"} <= events
    assert "AUDIO_CONFIRMED_COMMIT" not in events


def test_reset_clears_the_demo_database(client):
    _start(client, "naive")
    assert client.get("/api/bookings").json()["counts"]["total"] > 0
    client.post("/api/reset", json={})
    assert client.get("/api/bookings").json()["counts"]["total"] == 0


def test_recent_calls_feed_lists_sessions(client):
    _start(client, "fenced")
    _start(client, "naive")
    calls = client.get("/api/calls").json()["calls"]
    assert len(calls) >= 2
    assert {c["variant"] for c in calls} == {"fenced", "naive"}


# ---------------------------------------------------------------- pages & cost


@pytest.mark.parametrize(
    "path,needle",
    [
        ("/", "BlackBox"),
        ("/mobile", "BlackBox Bistro"),
        ("/static/app.js", "api/call/barge-in"),
        ("/static/app.css", "--green"),
        ("/static/qr.js", "QRLite"),
    ],
)
def test_pages_serve(client, path, needle):
    r = client.get(path)
    assert r.status_code == 200
    assert needle in r.text


def test_demo_makes_no_external_api_calls(client, monkeypatch, gating_end):
    """The demo's cost rule: clicking through the UI must never hit the network."""
    import socket

    def _boom(*_a, **_k):  # pragma: no cover - only fires on a regression
        raise AssertionError("the demo attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)

    for variant in ("naive", "fenced"):
        sid = _start(client, variant)["session_id"]
        client.post("/api/call/barge-in", json={"session_id": sid, "position_s": gating_end - 0.1})
    client.post("/api/sweep", json={"target": "fenced", "min_ms": -50, "max_ms": 50, "step_ms": 50})
    client.get("/api/summary")
    client.get("/api/bookings")


def test_session_store_is_bounded():
    """A public demo URL must not be an unbounded memory leak."""
    store = svc.SessionStore(max_sessions=5)
    timeline = load_timeline()
    for i in range(20):
        store.add(
            svc.CallSession(
                session_id=f"s{i}",
                variant="fenced",
                party_size=4,
                time_str="7:00 PM",
                timeline=timeline,
            )
        )
    assert len(store.recent(100)) == 5
    with pytest.raises(KeyError):
        store.get("s0")
