"""SQLite-backed booking store shared by the naive and fenced agents.

Both agent variants import this module so their data layer is byte-for-byte
comparable: any difference in committed state between the two comes from the
*commit gating policy*, never from a different persistence implementation.

Two tables:

``bookings``
    ``id, party_size, time_str, status, agent_variant, created_at``

``transaction_log``
    ``id, booking_id, event_type, timestamp``

The database path comes from the ``DB_PATH`` environment variable and defaults
to ``./results/booking.db``.

Status lifecycle
----------------
* naive agent:   (none) -> ``COMMITTED``            [NAIVE_COMMIT]
* fenced agent:  (none) -> ``PENDING_AUDIO``        [PENDING_CREATED]
                 ``PENDING_AUDIO`` -> ``COMMITTED``  [AUDIO_CONFIRMED_COMMIT]
                 ``PENDING_AUDIO`` -> ``ROLLED_BACK``[ROLLED_BACK]

No external network calls are ever made from this module.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

__all__ = [
    "STATUS_COMMITTED",
    "STATUS_PENDING_AUDIO",
    "STATUS_ROLLED_BACK",
    "VARIANT_FENCED",
    "VARIANT_NAIVE",
    "BookingNotFoundError",
    "BookingStoreError",
    "IllegalTransitionError",
    "book_naive",
    "commit_booking",
    "count_bookings",
    "create_pending_booking",
    "current_db_path",
    "get_booking",
    "get_transaction_log",
    "init_db",
    "reset_db",
    "set_db_path",
]

DEFAULT_DB_PATH = "./results/booking.db"

STATUS_PENDING_AUDIO = "PENDING_AUDIO"
STATUS_COMMITTED = "COMMITTED"
STATUS_ROLLED_BACK = "ROLLED_BACK"

VARIANT_NAIVE = "naive"
VARIANT_FENCED = "fenced"

EVENT_NAIVE_COMMIT = "NAIVE_COMMIT"
EVENT_PENDING_CREATED = "PENDING_CREATED"
EVENT_AUDIO_CONFIRMED_COMMIT = "AUDIO_CONFIRMED_COMMIT"
EVENT_ROLLED_BACK = "ROLLED_BACK"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    party_size    INTEGER NOT NULL,
    time_str      TEXT    NOT NULL,
    status        TEXT    NOT NULL,
    agent_variant TEXT    NOT NULL,
    created_at    TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS transaction_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id INTEGER NOT NULL,
    event_type TEXT    NOT NULL,
    timestamp  TEXT    NOT NULL,
    FOREIGN KEY (booking_id) REFERENCES bookings (id)
);

CREATE INDEX IF NOT EXISTS idx_transaction_log_booking
    ON transaction_log (booking_id);
"""

# The store is touched from agent callbacks running on an asyncio loop and from
# harness code on other threads, so guard the (short) write critical sections.
_LOCK = threading.RLock()
_DB_PATH_OVERRIDE: str | None = None


class BookingStoreError(RuntimeError):
    """Base class for booking-store failures."""


class BookingNotFoundError(BookingStoreError, KeyError):
    """Raised when a booking id does not exist.

    Subclasses :class:`KeyError` so ``except KeyError`` also catches it, but
    carries a readable message instead of the bare-repr ``KeyError`` gives.
    """

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.args[0] if self.args else "booking not found"


class IllegalTransitionError(BookingStoreError):
    """Raised when a status transition is not allowed by the lifecycle."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def current_db_path() -> str:
    """Resolve the active database path.

    Precedence: explicit :func:`set_db_path` override, then ``DB_PATH`` from the
    environment, then :data:`DEFAULT_DB_PATH`.
    """
    if _DB_PATH_OVERRIDE is not None:
        return _DB_PATH_OVERRIDE
    return os.environ.get("DB_PATH") or DEFAULT_DB_PATH


def set_db_path(path: str | os.PathLike[str] | None) -> None:
    """Override the database path process-wide (``None`` clears the override).

    Tests use this to point the store at a ``tmp_path`` file.
    """
    global _DB_PATH_OVERRIDE
    _DB_PATH_OVERRIDE = None if path is None else str(path)


@contextmanager
def _connect(path: str | None = None) -> Iterator[sqlite3.Connection]:
    db_path = path or current_db_path()
    parent = Path(db_path).expanduser().parent
    if str(parent):
        parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # WAL keeps the harness' read-side queries from blocking agent writes.
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError:  # pragma: no cover - filesystem dependent
            pass
        yield conn
    finally:
        conn.close()


def init_db(path: str | None = None) -> None:
    """Create the schema if needed. Safe to call repeatedly.

    Passing ``path`` also makes it the active path for subsequent calls, so
    ``init_db("/tmp/x.db")`` is enough to redirect the whole store.
    """
    if path is not None:
        set_db_path(path)
    with _LOCK, _connect() as conn:
        conn.executescript(_SCHEMA)


def reset_db(path: str | None = None) -> None:
    """Drop every row from both tables (schema preserved).

    Used between harness trials so each trial audits a clean slate.
    """
    if path is not None:
        set_db_path(path)
    with _LOCK, _connect() as conn:
        conn.executescript(_SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM transaction_log")
            conn.execute("DELETE FROM bookings")
            conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('bookings','transaction_log')")
        except sqlite3.OperationalError:  # sqlite_sequence absent on a fresh db
            pass
        conn.execute("COMMIT")


def _validate_booking_input(party_size: int, time_str: str) -> None:
    if isinstance(party_size, bool) or not isinstance(party_size, int):
        # Deliberately ValueError: documented input-validation error.
        raise ValueError(f"party_size must be an int, got {type(party_size).__name__}")
    if party_size <= 0:
        raise ValueError(f"party_size must be positive, got {party_size}")
    if not isinstance(time_str, str) or not time_str.strip():
        raise ValueError("time_str must be a non-empty string")


def _log(conn: sqlite3.Connection, booking_id: int, event_type: str) -> None:
    conn.execute(
        "INSERT INTO transaction_log (booking_id, event_type, timestamp) VALUES (?, ?, ?)",
        (booking_id, event_type, _utc_now()),
    )


def _insert_booking(
    party_size: int,
    time_str: str,
    status: str,
    agent_variant: str,
    event_type: str,
) -> int:
    _validate_booking_input(party_size, time_str)
    with _LOCK, _connect() as conn:
        conn.executescript(_SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "INSERT INTO bookings (party_size, time_str, status, agent_variant, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (party_size, time_str, status, agent_variant, _utc_now()),
            )
            booking_id = int(cur.lastrowid or 0)
            _log(conn, booking_id, event_type)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return booking_id


def book_naive(
    party_size: int,
    time_str: str,
    *,
    agent_variant: str = VARIANT_NAIVE,
) -> int:
    """Commit a booking immediately -- the naive (deliberately broken) path.

    The row is ``COMMITTED`` the instant the tool call executes, with no gating
    on whether the caller ever heard the spoken confirmation.
    """
    return _insert_booking(
        party_size,
        time_str,
        STATUS_COMMITTED,
        agent_variant,
        EVENT_NAIVE_COMMIT,
    )


def create_pending_booking(
    party_size: int,
    time_str: str,
    *,
    agent_variant: str = VARIANT_FENCED,
) -> int:
    """Create a booking held in ``PENDING_AUDIO`` -- the fenced path.

    The row only becomes real once :func:`commit_booking` is called, which the
    fenced agent does exclusively on confirmed audio playback.
    """
    return _insert_booking(
        party_size,
        time_str,
        STATUS_PENDING_AUDIO,
        agent_variant,
        EVENT_PENDING_CREATED,
    )


def _transition(booking_id: int, new_status: str, event_type: str) -> None:
    with _LOCK, _connect() as conn:
        conn.executescript(_SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT status FROM bookings WHERE id = ?", (booking_id,)
            ).fetchone()
            if row is None:
                raise BookingNotFoundError(f"no booking with id {booking_id!r}")

            current = row["status"]
            if current == new_status:
                # Idempotent: repeated events from a noisy audio pipeline must
                # not produce a double commit or a double rollback log entry.
                conn.execute("COMMIT")
                return
            if current != STATUS_PENDING_AUDIO:
                raise IllegalTransitionError(
                    f"booking {booking_id} is {current}; cannot transition to {new_status}"
                )

            conn.execute("UPDATE bookings SET status = ? WHERE id = ?", (new_status, booking_id))
            _log(conn, booking_id, event_type)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def commit_booking(booking_id: int) -> None:
    """Promote ``PENDING_AUDIO`` -> ``COMMITTED``. Idempotent."""
    _transition(booking_id, STATUS_COMMITTED, EVENT_AUDIO_CONFIRMED_COMMIT)


def rollback_booking(booking_id: int) -> None:
    """Move ``PENDING_AUDIO`` -> ``ROLLED_BACK``. Idempotent."""
    _transition(booking_id, STATUS_ROLLED_BACK, EVENT_ROLLED_BACK)


def retire_booking(booking_id: int, *, replacement_id: int | None = None) -> None:
    """Explicit user cancellation/replacement, separate from audio rollback.

    A replacement must already be COMMITTED by the original fence. Keep both
    rows and transaction events for audit; never rewrite the original details.
    """
    with _LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT status FROM bookings WHERE id = ?", (booking_id,)).fetchone()
            if row is None:
                raise BookingNotFoundError(f"no booking with id {booking_id}")
            status = "CANCELLED" if replacement_id is None else "SUPERSEDED"
            if row["status"] in ("CANCELLED", "SUPERSEDED", STATUS_ROLLED_BACK):
                conn.execute("COMMIT")
                return
            if replacement_id is not None:
                replacement = conn.execute("SELECT status FROM bookings WHERE id = ?", (replacement_id,)).fetchone()
                if replacement_id == booking_id or replacement is None or replacement["status"] != STATUS_COMMITTED:
                    raise IllegalTransitionError("Replacement must be a different committed booking")
            conn.execute("UPDATE bookings SET status = ? WHERE id = ?", (status, booking_id))
            _log(conn, booking_id, "USER_CANCELLED" if replacement_id is None else f"SUPERSEDED_BY_{replacement_id}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def get_booking(booking_id: int) -> dict:
    """Return a booking row as a dict.

    Raises :class:`BookingNotFoundError` for an unknown id rather than silently
    returning ``None`` -- a harness that mistook "missing" for "not committed"
    would report false passes.
    """
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        row = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
    if row is None:
        raise BookingNotFoundError(f"no booking with id {booking_id!r}")
    return dict(row)


def get_transaction_log(booking_id: int | None = None) -> list[dict]:
    """Return transaction-log rows, oldest first.

    ``booking_id=None`` returns the whole log.
    """
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        if booking_id is None:
            rows = conn.execute("SELECT * FROM transaction_log ORDER BY id ASC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM transaction_log WHERE booking_id = ? ORDER BY id ASC",
                (booking_id,),
            ).fetchall()
    return [dict(r) for r in rows]


def count_bookings(
    status: str | None = None,
    agent_variant: str | None = None,
    *,
    id_lo: int | None = None,
    id_hi: int | None = None,
) -> int:
    """Count bookings, optionally filtered by status, variant and/or id range.

    The id range powers the harness' orphan audit: after a sweep, every booking
    id the sweep created must have left ``PENDING_AUDIO`` -- a row still pending
    after ``on_session_closed`` is a leaked (unresolved) state change.
    """
    clauses: list[str] = []
    params: list[object] = []
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if agent_variant is not None:
        clauses.append("agent_variant = ?")
        params.append(agent_variant)
    if id_lo is not None:
        clauses.append("id >= ?")
        params.append(id_lo)
    if id_hi is not None:
        clauses.append("id <= ?")
        params.append(id_hi)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

    with _connect() as conn:
        conn.executescript(_SCHEMA)
        row = conn.execute(f"SELECT COUNT(*) AS n FROM bookings{where}", params).fetchone()
    return int(row["n"])
