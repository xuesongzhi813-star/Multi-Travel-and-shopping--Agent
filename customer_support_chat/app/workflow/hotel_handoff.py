"""Shared data contracts for the parent graph and the hotel role."""

from dataclasses import asdict, dataclass
from datetime import date, timedelta
import re
from typing import Literal, TypedDict
from unicodedata import normalize

from faq_extension.contracts import FaqSearchResult


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
    source_url: str | None = None
    checked_on: date | None = None
    demo: bool | None = None

    def to_json(self) -> dict:
        fields = asdict(self) | {"check_in": self.check_in.isoformat(), "check_out": self.check_out.isoformat()}
        if self.checked_on is not None:
            fields["checked_on"] = self.checked_on.isoformat()
        return {name: value for name, value in fields.items() if value is not None}

    @classmethod
    def from_json(cls, payload: dict) -> "HotelOption":
        checked_on = payload.get("checked_on")
        return cls(**(payload | {
            "check_in": date.fromisoformat(payload["check_in"]),
            "check_out": date.fromisoformat(payload["check_out"]),
            "checked_on": date.fromisoformat(checked_on) if checked_on is not None else None,
        }))


@dataclass(frozen=True)
class HotelRoleResult:
    status: Literal["hotel_results", "no_availability", "unavailable"]
    options: tuple[HotelOption, ...] = ()
    faq: FaqSearchResult | None = None


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
    faq: FaqSearchResult | None = None


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
    action: Literal["ordinary", "cancel", "hotel", "clarify_filters"]
    request: HotelPending | None = None


REFERENCE = re.compile(r"(?<![A-Z0-9-])DEMO-[A-Z][0-9]{1,3}(?![A-Z0-9-])", re.ASCII)
NIGHTS = re.compile(r"(?<![0-9.])(-?[0-9]+)\s*晚")
CHECK_OUT = re.compile(r"(?:退房(?:日期)?\s*[:：]?\s*([0-9]{4}-[0-9]{2}-[0-9]{2})|([0-9]{4}-[0-9]{2}-[0-9]{2})\s*退房)")


STAY_REPLY = re.compile(
    rf"(?:{NIGHTS.pattern}(?:[ ,，、]+{CHECK_OUT.pattern})?|{CHECK_OUT.pattern})"
)
AREA_AIRPORT = re.compile(r"机场附近|机场酒店|机场区域")
AREA_CITY = re.compile(r"市区|市中心")
STANDARD_ROOM = re.compile(r"标准房|\bstandard\b", re.IGNORECASE)
AMBIGUOUS_FILTER = re.compile(
    rf"(?:不要|不想(?:住|要)?|不考虑|避开|不选|不住)\s*(?:{AREA_AIRPORT.pattern}|"
    rf"{AREA_CITY.pattern}|{STANDARD_ROOM.pattern})", re.IGNORECASE,
)


def resolve_hotel_turn(content: str, pending: HotelPending | None) -> HotelTurn:
    text = normalize("NFKC", content).strip()
    if pending is not None and text in {"取消", "取消查询", "取消酒店查询", "不用查了"}:
        return HotelTurn("cancel")
    references = sorted(set(REFERENCE.findall(text.upper())))
    new_request = ("酒店" in text or "住宿" in text) and (
        references or any(word in text for word in ("找", "查", "推荐", "落地", "到达"))
    )
    bare_reply = text.rstrip("。.!！").strip()
    reference_only = REFERENCE.fullmatch(bare_reply.upper())
    reference_prefix = REFERENCE.match(bare_reply.upper())
    stay_text = bare_reply
    if reference_prefix is not None:
        stay_text = bare_reply[reference_prefix.end():].lstrip(" ,，、")
    stay_reply = STAY_REPLY.fullmatch(stay_text.removeprefix("我要住").removeprefix("住").strip())
    nights = NIGHTS.findall(text)
    dates = [first or second for first, second in CHECK_OUT.findall(text)]

    if new_request:
        request: HotelPending = {"reference": references[0] if len(references) == 1 else None}
    elif pending is None:
        return HotelTurn("ordinary")
    elif reference_prefix is not None and (reference_only or stay_reply):
        request = {"reference": reference_prefix[0]}
        if pending["waiting"] == "reference":
            request = {**pending, **request}
    elif stay_reply:
        request = {**pending, "nights": None, "check_out": None}
    elif pending["waiting"] == "nights" and re.fullmatch(r"-?[0-9]+", bare_reply):
        request = {**pending, "nights": None, "check_out": None}
        nights = [bare_reply]
    else:
        return HotelTurn("ordinary")

    if nights:
        request["nights"] = int(nights[0]) if len(set(nights)) == 1 and len(nights[0]) <= 3 else 0
    if dates:
        request["check_out"] = dates[0] if len(set(dates)) == 1 else "invalid"
    airport, city = AREA_AIRPORT.search(text), AREA_CITY.search(text)
    if AMBIGUOUS_FILTER.search(text) or (airport and city):
        return HotelTurn("clarify_filters")
    if airport or city:
        request["area"] = "airport" if airport else "city"
    if STANDARD_ROOM.search(text):
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
