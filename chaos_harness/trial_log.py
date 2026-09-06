"""The trial-log contract: one JSON object per line in ``results/trials_{target}.jsonl``.

This schema is frozen. ``reporting/dashboard.py`` reads it verbatim, so fields
are only ever *added*, never renamed or removed.

Required fields (the Part-5 contract)
-------------------------------------
``trial_id``          Monotonic 1-based index within the file.
``target``            ``naive`` | ``fenced`` | ``dummy``.
``offset_ms``         Barge-in offset relative to the END of the gating word.
                      Negative = interrupted before the caller heard it.
``booking_id``        Row id in the booking store (``None`` if none was made).
``db_status_after``   ``COMMITTED`` | ``PENDING_AUDIO`` | ``ROLLED_BACK`` |
                      ``MISSING``.
``mismatch``          ``True`` when committed state disagrees with what the
                      caller actually heard. This is the headline metric.
``cancel_latency_ms``  Wall-clock ms from issuing the barge-in to the target
                      resolving the booking's final state.
``timestamp``         UTC ISO-8601 of when the trial finished.

Additional diagnostic fields are appended after those, so a reader that only
knows the eight above still parses every row correctly.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

__all__ = [
    "DB_STATUS_MISSING",
    "REQUIRED_FIELDS",
    "TrialRecord",
    "iter_trials",
    "read_trials",
    "trial_log_path",
    "write_trials",
]

REQUIRED_FIELDS: tuple[str, ...] = (
    "trial_id",
    "target",
    "offset_ms",
    "booking_id",
    "db_status_after",
    "mismatch",
    "cancel_latency_ms",
    "timestamp",
)

DB_STATUS_MISSING = "MISSING"

DEFAULT_RESULTS_DIR = Path(__file__).resolve().parents[1] / "results"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def trial_log_path(target: str, results_dir: str | os.PathLike[str] | None = None) -> Path:
    """Canonical log path for a target: ``results/trials_{target}.jsonl``."""
    base = Path(results_dir) if results_dir is not None else DEFAULT_RESULTS_DIR
    return base / f"trials_{target}.jsonl"


@dataclass
class TrialRecord:
    """One chaos trial. Serialises to exactly one JSONL line."""

    trial_id: int
    target: str
    offset_ms: float
    booking_id: int | None
    db_status_after: str
    mismatch: bool
    cancel_latency_ms: float | None
    timestamp: str = field(default_factory=_utc_now)

    # --- diagnostics (optional for readers) ---
    run_index: int = 1
    """Which repeat of this offset (1..runs_per_offset)."""
    expected_committed: bool | None = None
    """Independent oracle verdict: should a booking exist?"""
    actually_committed: bool | None = None
    """What the store really holds."""
    gating_word: str = ""
    gating_word_end_s: float | None = None
    cancel_at_s: float | None = None
    """Playback-relative cancellation time, seconds."""
    heard_text: str = ""
    """Reconstructed transcript the caller actually heard."""
    fence_outcome: str | None = None
    mode: str = "replay"
    """``replay`` (offline, free) or ``live`` (real LiveKit + paid APIs)."""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # Required fields first, in contract order, for human-readable diffs.
        ordered = {name: data.pop(name) for name in REQUIRED_FIELDS}
        ordered.update(data)
        return ordered

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=False)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> TrialRecord:
        missing = [name for name in REQUIRED_FIELDS if name not in payload]
        if missing:
            raise ValueError(f"trial record missing required field(s): {', '.join(missing)}")
        known = {f for f in cls.__dataclass_fields__}  # noqa: SIM118 - explicit is clearer
        return cls(**{k: v for k, v in payload.items() if k in known})


class TrialLogWriter:
    """Append-only JSONL writer that flushes each line.

    Flushing per line is deliberate: ``reporting.dashboard watch`` tails the
    file live, and a buffered writer would make a 600-trial sweep look frozen.
    """

    def __init__(self, path: str | os.PathLike[str], *, truncate: bool = True) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._mode = "w" if truncate else "a"
        self._fh = None

    def __enter__(self) -> TrialLogWriter:
        self._fh = self.path.open(self._mode, encoding="utf-8")
        return self

    def write(self, record: TrialRecord) -> None:
        if self._fh is None:  # pragma: no cover - misuse guard
            raise RuntimeError("TrialLogWriter used outside its context manager")
        self._fh.write(record.to_json() + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def __exit__(self, *_exc: object) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def write_trials(
    path: str | os.PathLike[str],
    records: Iterable[TrialRecord],
    *,
    truncate: bool = True,
) -> Path:
    """Write records atomically-ish (single open, flushed per line)."""
    target = Path(path)
    with TrialLogWriter(target, truncate=truncate) as writer:
        for record in records:
            writer.write(record)
    return target


def iter_trials(
    path: str | os.PathLike[str],
    *,
    strict: bool = False,
) -> Iterator[TrialRecord]:
    """Stream trial records from a JSONL file.

    Malformed or truncated lines are skipped unless ``strict``. A tailing
    dashboard regularly reads a half-written final line, so skipping is the
    right default there; the test suite uses ``strict=True``.
    """
    file_path = Path(path)
    if not file_path.exists():
        if strict:
            raise FileNotFoundError(f"no trial log at {file_path}")
        return

    with file_path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield TrialRecord.from_dict(json.loads(line))
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                if strict:
                    raise ValueError(f"{file_path}:{lineno}: {exc}") from exc
                continue


def read_trials(path: str | os.PathLike[str], *, strict: bool = False) -> list[TrialRecord]:
    """Eagerly read a whole trial log."""
    return list(iter_trials(path, strict=strict))


def write_json_atomic(path: str | os.PathLike[str], payload: Any) -> Path:
    """Write JSON via a temp file + rename so readers never see a partial file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target
