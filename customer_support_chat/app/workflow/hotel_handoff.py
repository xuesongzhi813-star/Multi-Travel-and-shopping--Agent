"""Shared data contracts for the parent graph and the hotel role."""

from dataclasses import asdict, dataclass
from datetime import date, timedelta
import re
from typing import Literal, TypedDict
from unicodedata import normalize

from faq_extension.contracts import FaqSearchResult

from .preference_commands import AIRPORT, BUDGET_MENTION, CITY, ROOM, parse_total_chf_minor


@dataclass(frozen=True)
class HotelQuery:
    city: str
    timezone: str
    check_in: date
    check_out: date
    area: str | None = None
    room_type: str | None = None
    max_total_chf_minor: int | None = None

    def __post_init__(self) -> None:
        if not 1 <= (self.check_out - self.check_in).days <= 14:
            raise ValueError("A hotel stay must be 1 to 14 nights")

        budget = self.max_total_chf_minor
        if budget is not None and (type(budget) is not int or not 1 <= budget <= 1000000):
            raise ValueError("Invalid total hotel budget")

    def to_json(self) -> dict:
        fields = asdict(self) | {"check_in": self.check_in.isoformat(), "check_out": self.check_out.isoformat()}
        if self.max_total_chf_minor is None:
            del fields["max_total_chf_minor"]
        return fields

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
    waiting: Literal["reference", "nights", "budget"]
    reference: str | None
    nights: int | None
    check_out: str | None
    area: str | None
    room_type: str | None
    max_total_chf_minor: int | None
    arrival_date: str
    source_messages: dict[str, str]


@dataclass(frozen=True)
class HotelTurn:
    action: Literal["ordinary", "cancel", "hotel", "clarify_filters", "clarify_budget"]
    request: HotelPending | None = None
    explicit_fields: frozenset[str] = frozenset()


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
    clauses = [clause.strip() for clause in re.split(r"[,、]", text.rstrip("。.!！"))]
    budget_clauses = [clause for clause in clauses if BUDGET_MENTION.search(clause)]
    bare_reply = "，".join(
        clause for clause in clauses
        if clause not in budget_clauses and not (AIRPORT.fullmatch(clause) or CITY.fullmatch(clause) or ROOM.fullmatch(clause))
    ).strip()
    reference_only = REFERENCE.fullmatch(bare_reply.upper())
    reference_prefix = REFERENCE.match(bare_reply.upper())
    stay_text = bare_reply
    if reference_prefix is not None:
        stay_text = bare_reply[reference_prefix.end():].lstrip(" ,，、")
    stay_reply = STAY_REPLY.fullmatch(stay_text.removeprefix("我要住").removeprefix("住").strip())
    nights = NIGHTS.findall(text)
    dates = [first or second for first, second in CHECK_OUT.findall(text)]

    explicit = set()
    if new_request:
        request: HotelPending = {"reference": references[0] if len(references) == 1 else None}
        explicit.add("reference")
    elif pending is None:
        return HotelTurn("ordinary")
    elif pending["waiting"] == "budget" and len(clauses) == 1 and budget_clauses:
        request = pending.copy()
    elif reference_prefix is not None and (reference_only or stay_reply):
        request = {"reference": reference_prefix[0]}
        explicit.add("reference")
        if pending["waiting"] == "reference" or pending.get("reference") == reference_prefix[0]:
            request = {**pending, **request}
            if stay_reply:
                request.update(nights=None, check_out=None)
    elif stay_reply:
        request = {**pending, "nights": None, "check_out": None}
    elif pending["waiting"] == "nights" and re.fullmatch(r"-?[0-9]+", bare_reply):
        request = {**pending, "nights": None, "check_out": None}
        nights = [bare_reply]
    else:
        return HotelTurn("ordinary")

    if nights:
        explicit.add("nights")
        request["nights"] = int(nights[0]) if len(set(nights)) == 1 and len(nights[0]) <= 3 else 0
    if dates:
        explicit.add("check_out")
        request["check_out"] = dates[0] if len(set(dates)) == 1 else "invalid"
    airport, city = AREA_AIRPORT.search(text), AREA_CITY.search(text)
    if AMBIGUOUS_FILTER.search(text) or (airport and city):
        return HotelTurn("clarify_filters")
    if airport or city:
        explicit.add("area")
        request["area"] = "airport" if airport else "city"
    if STANDARD_ROOM.search(text):
        explicit.add("room_type")
        request["room_type"] = "standard"
    if budget_clauses:
        budget = parse_total_chf_minor(budget_clauses[0])
        if len(budget_clauses) != 1 or budget is None:
            request.pop("max_total_chf_minor", None)
            return HotelTurn("clarify_budget", request, frozenset(explicit))
        explicit.add("max_total_chf_minor")
        request["max_total_chf_minor"] = budget
    return HotelTurn("hotel", request, frozenset(explicit))


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
