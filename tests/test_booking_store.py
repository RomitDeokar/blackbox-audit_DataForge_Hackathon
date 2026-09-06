"""Tests for the shared booking store. No external API calls."""

from __future__ import annotations

import pytest

from shared import booking_store as bs


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Point the store at a throwaway database for every test."""
    db = tmp_path / "booking.db"
    monkeypatch.setenv("DB_PATH", str(db))
    bs.set_db_path(str(db))
    bs.init_db()
    yield db
    bs.set_db_path(None)


def _events(booking_id: int) -> list[str]:
    return [row["event_type"] for row in bs.get_transaction_log(booking_id)]


# --- naive path ---------------------------------------------------------


def test_naive_booking_commits_immediately():
    booking_id = bs.book_naive(4, "7:00 PM")

    booking = bs.get_booking(booking_id)
    assert booking["status"] == bs.STATUS_COMMITTED
    assert booking["party_size"] == 4
    assert booking["time_str"] == "7:00 PM"
    assert booking["agent_variant"] == bs.VARIANT_NAIVE
    assert booking["created_at"]
    assert _events(booking_id) == [bs.EVENT_NAIVE_COMMIT]


# --- fenced path --------------------------------------------------------


def test_pending_booking_starts_in_pending_audio():
    booking_id = bs.create_pending_booking(4, "7:00 PM")

    booking = bs.get_booking(booking_id)
    assert booking["status"] == bs.STATUS_PENDING_AUDIO
    assert booking["agent_variant"] == bs.VARIANT_FENCED
    assert _events(booking_id) == [bs.EVENT_PENDING_CREATED]


def test_commit_transitions_and_logs():
    booking_id = bs.create_pending_booking(4, "7:00 PM")
    bs.commit_booking(booking_id)

    assert bs.get_booking(booking_id)["status"] == bs.STATUS_COMMITTED
    assert _events(booking_id) == [bs.EVENT_PENDING_CREATED, bs.EVENT_AUDIO_CONFIRMED_COMMIT]


def test_rollback_transitions_and_logs():
    booking_id = bs.create_pending_booking(4, "7:00 PM")
    bs.rollback_booking(booking_id)

    assert bs.get_booking(booking_id)["status"] == bs.STATUS_ROLLED_BACK
    assert _events(booking_id) == [bs.EVENT_PENDING_CREATED, bs.EVENT_ROLLED_BACK]


# --- idempotency: repeated audio events must not double-write -----------


def test_repeated_commit_is_idempotent():
    booking_id = bs.create_pending_booking(4, "7:00 PM")
    bs.commit_booking(booking_id)
    bs.commit_booking(booking_id)
    bs.commit_booking(booking_id)

    assert bs.get_booking(booking_id)["status"] == bs.STATUS_COMMITTED
    assert _events(booking_id).count(bs.EVENT_AUDIO_CONFIRMED_COMMIT) == 1


def test_repeated_rollback_is_idempotent():
    booking_id = bs.create_pending_booking(4, "7:00 PM")
    bs.rollback_booking(booking_id)
    bs.rollback_booking(booking_id)

    assert bs.get_booking(booking_id)["status"] == bs.STATUS_ROLLED_BACK
    assert _events(booking_id).count(bs.EVENT_ROLLED_BACK) == 1


def test_cannot_commit_after_rollback():
    booking_id = bs.create_pending_booking(4, "7:00 PM")
    bs.rollback_booking(booking_id)

    with pytest.raises(bs.IllegalTransitionError):
        bs.commit_booking(booking_id)

    assert bs.get_booking(booking_id)["status"] == bs.STATUS_ROLLED_BACK


def test_cannot_rollback_after_commit():
    booking_id = bs.create_pending_booking(4, "7:00 PM")
    bs.commit_booking(booking_id)

    with pytest.raises(bs.IllegalTransitionError):
        bs.rollback_booking(booking_id)

    assert bs.get_booking(booking_id)["status"] == bs.STATUS_COMMITTED


def test_naive_booking_cannot_be_rolled_back():
    """A naive commit is unrecoverable -- that is precisely the failure mode."""
    booking_id = bs.book_naive(4, "7:00 PM")

    with pytest.raises(bs.IllegalTransitionError):
        bs.rollback_booking(booking_id)


# --- lookup errors ------------------------------------------------------


def test_get_booking_unknown_id_raises_clear_error():
    with pytest.raises(bs.BookingNotFoundError) as excinfo:
        bs.get_booking(99999)

    assert "99999" in str(excinfo.value)
    # Also catchable as KeyError for callers using the stdlib idiom.
    assert isinstance(excinfo.value, KeyError)


def test_transition_on_unknown_id_raises():
    with pytest.raises(bs.BookingNotFoundError):
        bs.commit_booking(4242)
    with pytest.raises(bs.BookingNotFoundError):
        bs.rollback_booking(4242)


# --- input validation ---------------------------------------------------


@pytest.mark.parametrize("party_size", [0, -1, -10])
def test_non_positive_party_size_rejected(party_size):
    with pytest.raises(ValueError):
        bs.book_naive(party_size, "7:00 PM")


@pytest.mark.parametrize("party_size", ["4", 4.0, None, True])
def test_non_int_party_size_rejected(party_size):
    with pytest.raises(ValueError):
        bs.create_pending_booking(party_size, "7:00 PM")


@pytest.mark.parametrize("time_str", ["", "   ", None, 7])
def test_bad_time_str_rejected(time_str):
    with pytest.raises(ValueError):
        bs.book_naive(4, time_str)


def test_rejected_input_writes_nothing():
    with pytest.raises(ValueError):
        bs.book_naive(0, "7:00 PM")
    assert bs.count_bookings() == 0
    assert bs.get_transaction_log() == []


# --- housekeeping -------------------------------------------------------


def test_init_db_is_idempotent():
    bs.init_db()
    bs.init_db()
    booking_id = bs.book_naive(2, "6:00 PM")
    assert bs.get_booking(booking_id)["status"] == bs.STATUS_COMMITTED


def test_reset_db_clears_rows_and_ids():
    bs.book_naive(4, "7:00 PM")
    bs.create_pending_booking(2, "8:00 PM")
    assert bs.count_bookings() == 2

    bs.reset_db()

    assert bs.count_bookings() == 0
    assert bs.get_transaction_log() == []
    # AUTOINCREMENT counter is reset too, so trials get comparable ids.
    assert bs.book_naive(4, "7:00 PM") == 1


def test_count_bookings_filters():
    bs.book_naive(4, "7:00 PM")
    pending = bs.create_pending_booking(2, "8:00 PM")
    bs.commit_booking(pending)
    bs.create_pending_booking(6, "9:00 PM")

    assert bs.count_bookings() == 3
    assert bs.count_bookings(status=bs.STATUS_COMMITTED) == 2
    assert bs.count_bookings(status=bs.STATUS_PENDING_AUDIO) == 1
    assert bs.count_bookings(agent_variant=bs.VARIANT_NAIVE) == 1
    assert bs.count_bookings(status=bs.STATUS_COMMITTED, agent_variant=bs.VARIANT_FENCED) == 1


def test_full_log_is_ordered_and_scoped():
    first = bs.book_naive(4, "7:00 PM")
    second = bs.create_pending_booking(2, "8:00 PM")
    bs.commit_booking(second)

    full = bs.get_transaction_log()
    assert [r["event_type"] for r in full] == [
        bs.EVENT_NAIVE_COMMIT,
        bs.EVENT_PENDING_CREATED,
        bs.EVENT_AUDIO_CONFIRMED_COMMIT,
    ]
    assert [r["booking_id"] for r in full] == [first, second, second]
    assert len(bs.get_transaction_log(second)) == 2


def test_db_path_env_var_is_honoured(tmp_path, monkeypatch):
    other = tmp_path / "nested" / "other.db"
    bs.set_db_path(None)
    monkeypatch.setenv("DB_PATH", str(other))
    bs.init_db()

    bs.book_naive(4, "7:00 PM")

    assert other.exists()
    assert bs.current_db_path() == str(other)


def test_store_makes_no_network_calls(monkeypatch):
    import requests

    def fail(*args, **kwargs):
        raise AssertionError("external API call attempted")

    monkeypatch.setattr(requests, "get", fail)
    monkeypatch.setattr(requests, "post", fail)

    booking_id = bs.create_pending_booking(4, "7:00 PM")
    bs.commit_booking(booking_id)
    assert bs.get_booking(booking_id)["status"] == bs.STATUS_COMMITTED
