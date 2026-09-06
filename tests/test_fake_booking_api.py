import json

from harness.fake_booking_api import create_booking
from shared.constants import TEST_CUSTOMER_NAME, TEST_PARTY_SIZE, TEST_BOOKING_TIME


def test_booking_for_four_at_7_pm_records_7_pm(tmp_path):
    report_path = tmp_path / "booking.json"

    booking = create_booking(
        customer_name=TEST_CUSTOMER_NAME,
        party_size=TEST_PARTY_SIZE,
        requested_time=TEST_BOOKING_TIME,
        report_path=report_path,
    )

    assert booking.party_size == 4
    assert booking.confirmed_time == "7:00 PM"
    assert booking.status == "confirmed"


def test_booking_writes_predictable_json_shape(tmp_path):
    report_path = tmp_path / "booking.json"

    create_booking(
        customer_name=TEST_CUSTOMER_NAME,
        party_size=TEST_PARTY_SIZE,
        requested_time=TEST_BOOKING_TIME,
        report_path=report_path,
    )

    assert json.loads(report_path.read_text(encoding="utf-8")) == {
        "customer_name": "Amitoj",
        "party_size": 4,
        "requested_time": "7:00 PM",
        "confirmed_time": "7:00 PM",
        "status": "confirmed",
    }


def test_fake_booking_api_does_not_call_external_services(monkeypatch, tmp_path):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("External API call attempted")

    monkeypatch.setattr("requests.get", fail_if_called)
    monkeypatch.setattr("requests.post", fail_if_called)

    booking = create_booking(
        customer_name=TEST_CUSTOMER_NAME,
        party_size=TEST_PARTY_SIZE,
        requested_time=TEST_BOOKING_TIME,
        report_path=tmp_path / "booking.json",
    )

    assert booking.status == "confirmed"
