"""Mismatch-by-offset chart as a dependency-free SVG.

The acceptance result is one line: the fenced agent's mismatch rate is zero at
every barge-in offset while the naive agent's is flat near 100%. A single chart
carries that message faster than a 606-row table, and SVG needs no plotting
library -- the project stays dependency-light.

    from reporting.chart import render_mismatch_chart
    render_mismatch_chart(records, "results/mismatch_by_offset.svg")
"""

from __future__ import annotations

import html
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

from chaos_harness.trial_log import TrialRecord

__all__ = ["mismatch_by_offset", "render_mismatch_chart"]

_WIDTH = 960
_HEIGHT = 480
_MARGIN = 60

_PALETTE = {"naive": "#d64545", "fenced": "#2e9e5b", "dummy": "#8a8f98"}


def mismatch_by_offset(records: Iterable[TrialRecord]) -> dict[str, dict[float, float]]:
    """Per target: {offset_ms: fraction of trials that mismatched}."""
    buckets: dict[str, dict[float, list[bool]]] = defaultdict(lambda: defaultdict(list))
    for rec in records:
        buckets[rec.target][rec.offset_ms].append(rec.mismatch)
    out: dict[str, dict[float, float]] = {}
    for target, offsets in sorted(buckets.items()):
        out[target] = {off: sum(ms) / len(ms) for off, ms in sorted(offsets.items())}
    return out


def render_mismatch_chart(
    records: Iterable[TrialRecord],
    out_path: str | Path,
    *,
    title: str = "Mismatch rate vs barge-in offset",
) -> Path:
    """Render the per-target mismatch curves and write an SVG file.

    Offsets are measured relative to the end of the gating word; the dashed
    vertical line at 0 ms marks the instant the caller finished hearing the
    confirmation word.
    """
    data = mismatch_by_offset(records)
    target_path = Path(out_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    all_offsets = sorted({off for d in data.values() for off in d})
    if not all_offsets:
        all_offsets = [-500.0, 500.0]
    x_min, x_max = min(all_offsets), max(all_offsets)
    span = max(x_max - x_min, 1.0)

    plot_w = _WIDTH - _MARGIN - 40
    plot_h = _HEIGHT - 80

    def sx(offset: float) -> float:
        return _MARGIN + (offset - x_min) / span * plot_w

    def sy(rate: float) -> float:
        return 40 + (1.0 - rate) * plot_h

    parts: list[str] = [
        '<?xml version="1.0" encoding="utf-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_WIDTH}" height="{_HEIGHT}" viewBox="0 0 {_WIDTH} {_HEIGHT}">',
        f'<rect width="{_WIDTH}" height="{_HEIGHT}" fill="#ffffff"/>',
        (
            f'<text x="{_WIDTH / 2:.0f}" y="28" text-anchor="middle" font-size="18" '
            f'font-family="sans-serif" font-weight="bold">{html.escape(title)}</text>'
        ),
    ]

    for k in range(6):
        rate = k / 5.0
        parts.append(
            f'<line x1="{_MARGIN}" y1="{sy(rate):.1f}" x2="{_WIDTH - 40}" y2="{sy(rate):.1f}" stroke="#e8e8e8"/>'
        )
        parts.append(
            f'<text x="{_MARGIN - 8}" y="{sy(rate) + 4:.1f}" text-anchor="end" '
            f'font-size="11" fill="#666">{rate:.0%}</text>'
        )

    for off in (x_min, 0.0, x_max):
        if x_min <= off <= x_max:
            parts.append(
                f'<text x="{sx(off):.1f}" y="{_HEIGHT - 45}" text-anchor="middle" '
                f'font-size="11" fill="#666">{off:+.0f} ms</text>'
            )
    parts.append(
        f'<line x1="{sx(0.0):.1f}" y1="40" x2="{sx(0.0):.1f}" y2="{_HEIGHT - 60}" stroke="#555" stroke-dasharray="4 4"/>'
    )
    parts.append(
        f'<text x="{sx(0.0):.1f}" y="{_HEIGHT - 22}" text-anchor="middle" '
        f'font-size="11" fill="#555">gating word heard (offset &gt; 0 ms)</text>'
    )
    parts.append(
        f'<text x="{_MARGIN}" y="{_HEIGHT - 8}" font-size="11" fill="#888">'
        f'offset = barge-in time relative to end of the gating word, in milliseconds</text>'
    )

    legend_x = _MARGIN + 12
    legend_y = 62
    for target, series in data.items():
        color = _PALETTE.get(target, "#333333")
        points = " ".join(f"{sx(off):.1f},{sy(rate):.3f}" for off, rate in sorted(series.items()))
        parts.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2.5"/>')
        parts.append(f'<circle cx="{legend_x + 6}" cy="{legend_y}" r="4" fill="{color}"/>')
        parts.append(
            f'<text x="{legend_x + 16}" y="{legend_y + 4}" font-size="12" '
            f'font-family="sans-serif">{html.escape(target)} agent</text>'
        )
        legend_y += 20

    parts.append("</svg>")
    target_path.write_text("\n".join(parts) + "\n", encoding="utf-8")
    return target_path
