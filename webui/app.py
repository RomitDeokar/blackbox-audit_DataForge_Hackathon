"""FastAPI app: the demo UI + mobile-integration surface for BlackBox Audit.

Run it:

    python -m webui                      # http://0.0.0.0:8080
    python -m webui --port 3000 --reload

Every endpoint delegates to :mod:`webui.service`, which delegates to the real
project modules. This file contains no fencing logic at all -- it is transport
and validation only.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from shared.constants import (
    SWEEP_OFFSET_MAX_MS,
    SWEEP_OFFSET_MIN_MS,
    SWEEP_OFFSET_STEP_MS,
    SWEEP_RUNS_PER_OFFSET,
    TEST_BOOKING_TIME,
    TEST_PARTY_SIZE,
    VARIANT_FENCED,
    VARIANT_NAIVE,
)

from . import __version__
from .service import get_service

logger = logging.getLogger("blackbox_audit.webui")

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(
    title="BlackBox Audit - Demo Console",
    version=__version__,
    description=(
        "Interactive demo for the audio-fenced voice agent experiment. The "
        "commit decision shown in the browser is computed by the same "
        "shared.audio_fence.AudioFence state machine the live LiveKit agent "
        "runs, writing to the same SQLite store."
    ),
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

# The demo is opened from phones, QR codes and shared links; a mobile client on
# a different origin must be able to call the API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class StartCallRequest(BaseModel):
    variant: str = Field(default=VARIANT_FENCED, description="naive | fenced")
    party_size: int = Field(default=TEST_PARTY_SIZE, ge=1, le=99)
    time_str: str = Field(default=TEST_BOOKING_TIME, min_length=1, max_length=40)


class PositionRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    position_s: float = Field(ge=0.0, le=600.0)


class FinishRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    position_s: float | None = Field(default=None, ge=0.0, le=600.0)


class SessionRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)


class SweepRequest(BaseModel):
    target: str = Field(default=VARIANT_FENCED, description="naive | fenced")
    min_ms: float = Field(default=float(SWEEP_OFFSET_MIN_MS), ge=-5000.0, le=5000.0)
    max_ms: float = Field(default=float(SWEEP_OFFSET_MAX_MS), ge=-5000.0, le=5000.0)
    step_ms: float = Field(default=float(SWEEP_OFFSET_STEP_MS), gt=0.0, le=1000.0)
    runs_per_offset: int = Field(default=SWEEP_RUNS_PER_OFFSET, ge=1, le=SWEEP_RUNS_PER_OFFSET)


class AcceptanceRequest(BaseModel):
    runs_per_offset: int = Field(default=SWEEP_RUNS_PER_OFFSET, ge=1, le=SWEEP_RUNS_PER_OFFSET)


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def _bad_request(exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": str(exc)})


def _not_found(exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": str(exc)})


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "version": __version__, "mode": "replay"}


@app.get("/api/config")
def config() -> dict[str, Any]:
    """Provider key status, scenario constants and acceptance parameters."""
    return get_service().config_payload()


@app.get("/api/timeline")
def timeline() -> dict[str, Any]:
    """The cached Rime word-level timeline that paces the demo."""
    return get_service().timeline_payload()


@app.get("/api/fence-states")
def fence_states() -> dict[str, Any]:
    return {"states": get_service().fence_states()}


# ---------------------------------------------------------------------------
# Call simulation (the "mobile integration" surface)
# ---------------------------------------------------------------------------


@app.post("/api/call/start")
def call_start(req: StartCallRequest) -> Any:
    """Place a call: runs the LLM tool call for the chosen variant."""
    try:
        return get_service().start_call(req.variant, req.party_size, req.time_str)
    except ValueError as exc:
        return _bad_request(exc)


@app.post("/api/call/advance")
def call_advance(req: PositionRequest) -> Any:
    """Report the browser's current playback position into the fence."""
    try:
        return get_service().advance_call(req.session_id, req.position_s)
    except KeyError as exc:
        return _not_found(exc)


@app.post("/api/call/barge-in")
def call_barge_in(req: PositionRequest) -> Any:
    """The caller interrupts mid-confirmation. This is the money moment."""
    try:
        return get_service().barge_in(req.session_id, req.position_s)
    except KeyError as exc:
        return _not_found(exc)


@app.post("/api/call/finish")
def call_finish(req: FinishRequest) -> Any:
    """Playback completed with no interruption."""
    try:
        return get_service().finish_call(req.session_id, req.position_s)
    except KeyError as exc:
        return _not_found(exc)


@app.post("/api/call/hang-up")
def call_hang_up(req: SessionRequest) -> Any:
    """Caller hung up: nothing may be left PENDING_AUDIO."""
    try:
        return get_service().hang_up(req.session_id)
    except KeyError as exc:
        return _not_found(exc)


@app.get("/api/call/{session_id}")
def call_state(session_id: str) -> Any:
    try:
        return get_service().session(session_id)
    except KeyError as exc:
        return _not_found(exc)


@app.get("/api/calls")
def recent_calls(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    return {"calls": get_service().recent_calls(limit)}


# ---------------------------------------------------------------------------
# Ground truth helpers
# ---------------------------------------------------------------------------


@app.get("/api/heard")
def heard(at: float = Query(ge=0.0, le=600.0, description="playback seconds")) -> dict[str, Any]:
    """What had the caller actually heard at time ``at``?"""
    return get_service().heard_at(at)


@app.get("/api/plan")
def plan(
    offset_ms: float = Query(default=0.0, ge=-5000.0, le=5000.0),
) -> Any:
    """Injector arithmetic: gating_word_end + offset -> cancellation instant."""
    try:
        return get_service().plan(offset_ms)
    except (KeyError, ValueError) as exc:
        return _bad_request(exc)


# ---------------------------------------------------------------------------
# Sweeps & reporting
# ---------------------------------------------------------------------------


@app.post("/api/sweep")
def sweep(req: SweepRequest) -> Any:
    """Run a real chaos sweep against one variant."""
    try:
        return get_service().run_sweep(
            req.target,
            min_ms=req.min_ms,
            max_ms=req.max_ms,
            step_ms=req.step_ms,
            runs_per_offset=req.runs_per_offset,
        )
    except ValueError as exc:
        return _bad_request(exc)


@app.post("/api/acceptance")
def acceptance(req: AcceptanceRequest) -> Any:
    """The full acceptance test: both variants across the whole offset ladder."""
    try:
        return get_service().acceptance(req.runs_per_offset)
    except ValueError as exc:
        return _bad_request(exc)


@app.get("/api/summary")
def summary() -> dict[str, Any]:
    """``reporting.dashboard.summarize`` over the current trial logs."""
    return get_service().summary()


@app.get("/api/chart.svg")
def chart() -> Response:
    svg = get_service().chart_svg()
    return Response(content=svg, media_type="image/svg+xml")


@app.get("/api/bookings")
def bookings(limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
    """Direct read of the demo SQLite store: the audit view."""
    return get_service().bookings(limit)


@app.post("/api/reset")
def reset() -> dict[str, Any]:
    return get_service().reset()


# ---------------------------------------------------------------------------
# Static UI
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/mobile", response_class=HTMLResponse)
def mobile() -> FileResponse:
    """The phone-shaped caller view, opened via QR code on demo day."""
    return FileResponse(STATIC_DIR / "mobile.html")


if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point (``python -m webui``)."""
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(prog="webui", description="BlackBox Audit demo console")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args(argv)

    uvicorn.run(
        "webui.app:app" if args.reload else app,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
    )
    return 0


__all__ = ["app", "main"]
