import json
from pathlib import Path

from shared.models import Booking


REPORTS_DIR = Path(__file__).resolve().parents[1] / "reports"
DEFAULT_BOOKING_REPORT = REPORTS_DIR / "latest_booking.json"


def create_booking(
    customer_name: str,
    party_size: int,
    requested_time: str,
    confirmed_time: str | None = None,
    report_path: Path = DEFAULT_BOOKING_REPORT,
) -> Booking:
    booking = Booking(
        customer_name=customer_name,
        party_size=party_size,
        requested_time=requested_time,
        confirmed_time=confirmed_time or requested_time,
        status="confirmed",
    )

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(booking.to_dict(), indent=2) + "\n", encoding="utf-8")
    return booking
