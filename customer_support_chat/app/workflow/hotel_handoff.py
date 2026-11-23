"""Shared data contracts for the parent graph and the hotel role."""

from dataclasses import asdict, dataclass
from datetime import date
from typing import Literal


@dataclass(frozen=True)
class HotelQuery:
    city: str
    timezone: str
    check_in: date
    check_out: date
    area: str | None = None
    room_type: str | None = None

    def __post_init__(self) -> None:
        if not 1 <= (self.check_out - self.check_in).days <= 14:
            raise ValueError("A hotel stay must be 1 to 14 nights")

    def to_json(self) -> dict:
        return asdict(self) | {"check_in": self.check_in.isoformat(), "check_out": self.check_out.isoformat()}

    @classmethod
    def from_json(cls, payload: dict) -> "HotelQuery":
        return cls(**(payload | {
            "check_in": date.fromisoformat(payload["check_in"]),
            "check_out": date.fromisoformat(payload["check_out"]),
        }))


@dataclass(frozen=True)
class HotelOption:
    hotel_id: str
    hotel_name: str
    area: str
    room_type: str
    check_in: date
    check_out: date
    total_minor: int
    currency: str

    def to_json(self) -> dict:
        return asdict(self) | {"check_in": self.check_in.isoformat(), "check_out": self.check_out.isoformat()}

    @classmethod
    def from_json(cls, payload: dict) -> "HotelOption":
        return cls(**(payload | {
            "check_in": date.fromisoformat(payload["check_in"]),
            "check_out": date.fromisoformat(payload["check_out"]),
        }))


@dataclass(frozen=True)
class HotelRoleResult:
    status: Literal["hotel_results", "no_availability", "unavailable"]
    options: tuple[HotelOption, ...] = ()
