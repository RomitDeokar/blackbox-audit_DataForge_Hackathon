from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Booking:
    customer_name: str
    party_size: int
    requested_time: str
    confirmed_time: str
    status: str

    def to_dict(self) -> dict[str, str | int]:
        return asdict(self)
