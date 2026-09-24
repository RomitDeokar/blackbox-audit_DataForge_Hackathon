"""FastAPI application entry point for the Triage Line UI backend.

Phase 7A only: this exposes the WebSocket event bridge and a demo trigger
over the existing replay harness. It intentionally does not implement, or
know about, any UI framework -- see docs/UI_SPEC.md section 3, which this
app's shape follows directly:

- `GET  /health`                        -- liveness/readiness probe.
- `GET  /demo/scenarios`                -- lists the existing required
                                            scenario ids this backend can
                                            demo-run (see api/replay_bridge.py).
- `POST /demo/run/{run_id}/{scenario_id}` -- runs one existing scenario
                                            through one existing agent
                                            strategy, relaying its real
                                            event trace live to every
                                            client on `/ws/replay/{run_id}`.
- `POST /demo/dialogue/{run_id}/{scenario_id}` -- (Phase 7B-2C) runs the
                                            same scenario's caller lines
                                            through the existing
                                            DialogueEngine + mock providers,
                                            relaying transcript/backchannel/
                                            BargeIn/MetricsTick events.
- `WS   /ws/call/{call_id}`             -- live-call event channel (no
                                            producer exists for this yet in
                                            Phase 7A -- transport/dialogue
                                            wiring is a later phase -- but
                                            the channel is real and ready).
- `WS   /ws/replay/{run_id}`            -- harness/demo-run event channel.

Run with:  uvicorn api.app:app --reload
"""

from __future__ import annotations

import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from pydantic import BaseModel, Field
from fastapi import HTTPException
from api.live_calls import CallError, LiveCalls
from persistence.repository import ActionRepository, DeliberationRepository
from dataclasses import asdict
from typing import AsyncIterator

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from api.dialogue_bridge import run_dialogue_demo
from api.replay_bridge import (
    UnknownAgentTypeError,
    UnknownScenarioError,
    available_demo_scenarios,
    run_demo_scenario,
)
from api.websocket import ConnectionManager

logger = logging.getLogger("triage_line.api")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("Triage Line API starting up (offline/mock mode)")
    yield
    app.state.live_calls.close()
    logger.info("Triage Line API shutting down")


class CallerTurn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class ActionDecision(BaseModel):
    action_id: str
    confirm: bool


def _valid_id(call_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", call_id):
        raise HTTPException(status_code=400, detail="Invalid call ID")
    return call_id


def create_app(db_path: str | None = None) -> FastAPI:
    """Application factory.

    A factory (rather than a bare module-level `app`) so tests can build a
    fresh app -- and therefore a fresh, isolated `ConnectionManager` with
    no state left over from a previous test -- instead of sharing one
    process-wide instance.
    """

    app = FastAPI(title="Triage Line API", version="7c", lifespan=lifespan)
    app.state.connections = ConnectionManager()
    path = db_path if db_path is not None else os.getenv("TRIAGE_DB_PATH", ":memory:")
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    app.state.live_calls = LiveCalls(app.state.connections, path)

    @app.get("/health")
    async def health() -> dict:
        connections: ConnectionManager = app.state.connections
        return {"status": "ok", "live_connections": connections.connection_count()}

    @app.get("/demo/scenarios")
    async def list_demo_scenarios() -> dict:
        return {"scenarios": sorted(available_demo_scenarios())}

    @app.websocket("/ws/call/{call_id}")
    async def ws_call(websocket: WebSocket, call_id: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", call_id):
            await websocket.close(code=1008)
            return
        session = await app.state.live_calls.session(call_id)
        await _serve_websocket(app, websocket, channel=f"call:{call_id}", history=session.history)

    @app.post("/calls/{call_id}/turn")
    async def caller_turn(call_id: str, body: CallerTurn):
        try:
            return await app.state.live_calls.turn(_valid_id(call_id), body.text)
        except CallError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/calls/{call_id}/interrupt")
    async def interrupt(call_id: str):
        return await app.state.live_calls.interrupt(_valid_id(call_id))

    @app.post("/calls/{call_id}/decision")
    async def decide(call_id: str, body: ActionDecision):
        try:
            return await app.state.live_calls.decide(_valid_id(call_id), body.action_id, body.confirm)
        except CallError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/calls/{call_id}/end")
    async def end_call(call_id: str):
        return await app.state.live_calls.end(_valid_id(call_id))

    @app.get("/calls/{call_id}/audit")
    async def audit(call_id: str):
        call_id = _valid_id(call_id)
        db = app.state.live_calls.db
        return {"call_id": call_id,
                "deliberations": [asdict(r) for r in DeliberationRepository(db).list_for_call(call_id)],
                "actions": [{**asdict(r), "current_state": r.current_state.value}
                            for r in ActionRepository(db).list_for_call(call_id)]}

    @app.websocket("/ws/replay/{run_id}")
    async def ws_replay(websocket: WebSocket, run_id: str) -> None:
        await _serve_websocket(app, websocket, channel=f"replay:{run_id}")

    @app.post("/demo/run/{run_id}/{scenario_id}")
    async def trigger_demo(
        run_id: str, scenario_id: str, agent_type: str = "deliberative", pace_ms: int = 0
    ):
        connections: ConnectionManager = app.state.connections
        channel = f"replay:{run_id}"
        try:
            result = await run_demo_scenario(
                connections, channel, scenario_id, agent_type, pace_ms=_clamp_pace(pace_ms)
            )
        except UnknownScenarioError as exc:
            return JSONResponse(status_code=404, content={"error": str(exc)})
        except UnknownAgentTypeError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        return result.to_dict()

    @app.post("/demo/dialogue/{run_id}/{scenario_id}")
    async def trigger_dialogue_demo(run_id: str, scenario_id: str, pace_ms: int = 0):
        """Phase 7B-2C: run a scenario's caller lines through the existing
        DialogueEngine + mock providers (see api/dialogue_bridge.py), relaying
        its real Conversation-layer events (transcripts, backchannels,
        BargeIn, MetricsTick) to `/ws/replay/{run_id}`."""

        connections: ConnectionManager = app.state.connections
        channel = f"replay:{run_id}"
        try:
            result = await run_dialogue_demo(
                connections, channel, scenario_id, pace_ms=_clamp_pace(pace_ms)
            )
        except UnknownScenarioError as exc:
            return JSONResponse(status_code=404, content={"error": str(exc)})
        return result.to_dict()

    return app


_MAX_PACE_MS = 2000


def _clamp_pace(pace_ms: int) -> int:
    """Optional demo pacing (delay between relayed events) so a human can
    follow the stream live. Presentation-only: never changes which events
    are produced or their order. Clamped to keep a demo run bounded."""

    return max(0, min(int(pace_ms), _MAX_PACE_MS))


async def _serve_websocket(app: FastAPI, websocket: WebSocket, channel: str, history=None) -> None:
    """Accept, register, and hold a WebSocket open for a single channel.

    Per docs/UI_SPEC.md section 3 ("No UI component may read from the
    database directly for live rendering ... the live call view is
    WS-events-only"), this socket is a pure event sink: it has no write
    path into core. The only thing an incoming client message does is get
    discarded; waiting on `receive_text()` is simply how a clean
    disconnect is detected without polling.
    """

    connections: ConnectionManager = app.state.connections
    await connections.connect(websocket, channel)
    try:
        for event in list(history or []):
            await websocket.send_json(event)
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        connections.disconnect(websocket, channel)


app = create_app()
