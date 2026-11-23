"""Shared data contracts for the parent graph and the hotel role."""

from dataclasses import asdict, dataclass
from datetime import date, timedelta
import re
from typing import Literal, TypedDict
from unicodedata import normalize


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


class RoleUnavailable(RuntimeError):
    """The local role did not return a usable receipt."""


@dataclass(frozen=True)
class VerifiedArrival:
    reference: str
    city: str
    airport: str
    local_date: date
    timezone: str
    tool_call_id: str


class HotelPending(TypedDict, total=False):
    waiting: Literal["reference", "nights"]
    reference: str | None
    nights: int | None
    check_out: str | None
    area: str | None
    room_type: str | None
    arrival_date: str


@dataclass(frozen=True)
class HotelTurn:
    action: Literal["ordinary", "cancel", "hotel"]
    request: HotelPending | None = None


REFERENCE = re.compile(r"(?<![A-Z0-9-])DEMO-[A-Z][0-9]{1,3}(?![A-Z0-9-])", re.ASCII)
NIGHTS = re.compile(r"(?<![0-9.])(-?[0-9]+)\s*晚")
CHECK_OUT = re.compile(r"(?:退房(?:日期)?\s*[:：]?\s*([0-9]{4}-[0-9]{2}-[0-9]{2})|([0-9]{4}-[0-9]{2}-[0-9]{2})\s*退房)")


def resolve_hotel_turn(content: str, pending: HotelPending | None) -> HotelTurn:
    text = normalize("NFKC", content).strip()
    if pending is not None and text in {"取消", "取消查询", "取消酒店查询", "不用查了"}:
        return HotelTurn("cancel")
    references = sorted(set(REFERENCE.findall(text.upper())))
    new_request = ("酒店" in text or "住宿" in text) and (
        references or any(word in text for word in ("找", "查", "推荐", "落地", "到达"))
    )
    nights = NIGHTS.findall(text)
    dates = [first or second for first, second in CHECK_OUT.findall(text)]
    if pending is not None and pending["waiting"] == "nights" and re.fullmatch(r"-?[0-9]+", text):
        nights = [text]

    if new_request:
        request: HotelPending = {"reference": references[0] if len(references) == 1 else None}
    elif pending is not None and pending["waiting"] == "reference" and len(references) == 1:
        request = {**pending, "reference": references[0]}
    elif pending is not None and pending["waiting"] == "nights" and (nights or dates):
        request = {**pending, "nights": None, "check_out": None}
    else:
        return HotelTurn("ordinary")

    if nights:
        request["nights"] = int(nights[0]) if len(set(nights)) == 1 and len(nights[0]) <= 3 else 0
    if dates:
        request["check_out"] = dates[0] if len(set(dates)) == 1 else "invalid"
    if any(term in text for term in ("机场附近", "机场酒店", "机场区域")):
        request["area"] = "airport"
    if any(term in text for term in ("市区", "市中心")):
        request["area"] = "city"
    if "标准房" in text or re.search(r"\bstandard\b", text, re.IGNORECASE):
        request["room_type"] = "standard"
    return HotelTurn("hotel", request)


def stay_checkout(check_in: date, request: HotelPending) -> date | None:
    nights, checkout = request.get("nights"), request.get("check_out")
    try:
        if checkout is not None:
            result = date.fromisoformat(checkout)
            days = (result - check_in).days
            return result if 1 <= days <= 14 and (nights is None or nights == days) else None
        if nights is not None and 1 <= nights <= 14:
            return check_in + timedelta(days=nights)
    except (ValueError, OverflowError):
        return None
    return None
