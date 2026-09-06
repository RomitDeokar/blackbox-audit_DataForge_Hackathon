"""Reporting: statistics and rendering over the harness' JSONL trial logs."""

from __future__ import annotations

__all__ = ["percentile", "render_summary", "summarize", "summarize_records", "watch"]


def __getattr__(name: str):  # pragma: no cover - lazy import shim
    if name in __all__:
        from . import dashboard

        return getattr(dashboard, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
