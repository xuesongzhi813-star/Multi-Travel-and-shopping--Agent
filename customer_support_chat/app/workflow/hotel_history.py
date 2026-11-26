"""Recover explicit hotel constraints with owned conversation evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re
from typing import TYPE_CHECKING, TypedDict, cast
from unicodedata import normalize
from uuid import UUID

from .hotel_handoff import CHECK_OUT, NIGHTS, REFERENCE, HotelPending
from .preference_commands import AIRPORT, CITY, ROOM, parse_total_chf_minor

if TYPE_CHECKING:
    from .parent_graph import MessageLoader


HISTORY_FIELDS = ("reference", "nights", "check_out", "area", "room_type", "max_total_chf_minor")
REPLAY_REQUEST = "按上次的酒店条件再查"


class HotelHistory(TypedDict):
    request: HotelPending
    source_messages: dict[str, str]


@dataclass(frozen=True)
class HotelReplay:
    changes: HotelPending | None


def _replay_clause(clause: str) -> HotelPending:
    text = clause.removeprefix("这次").strip()
    if AIRPORT.fullmatch(text):
        return {"area": "airport"}
    if CITY.fullmatch(text):
        return {"area": "city"}
    if ROOM.fullmatch(text):
        return {"room_type": "standard"}
    if (budget := parse_total_chf_minor(text)) is not None:
        return {"max_total_chf_minor": budget}
    if reference := REFERENCE.fullmatch(text.removeprefix("航班").strip().upper()):
        return {"reference": reference[0]}
    if nights := NIGHTS.fullmatch(text.removeprefix("住").strip()):
        if len(nights[1]) <= 2 and 1 <= int(nights[1]) <= 14:
            return {"nights": int(nights[1])}
    if checkout := CHECK_OUT.fullmatch(text):
        value = checkout[1] or checkout[2]
        try:
            return {"check_out": date.fromisoformat(value).isoformat()}
        except ValueError:
            return {}
    return {}


def parse_hotel_replay(content: str) -> HotelReplay | None:
    text = normalize("NFKC", content).strip().rstrip("。.!！").strip()
    if not text.startswith(REPLAY_REQUEST):
        return None
    suffix = text.removeprefix(REPLAY_REQUEST).strip()
    if not suffix:
        return HotelReplay({})
    if suffix[0] not in ",、":
        return HotelReplay(None)
    changes: HotelPending = {}
    for part in re.split(r"[,、]", suffix[1:]):
        fields = _replay_clause(part.strip())
        if not fields or changes.keys() & fields.keys():
            return HotelReplay(None)
        changes.update(fields)
    return HotelReplay(changes)


def track_hotel_sources(pending: HotelPending, explicit_fields: frozenset[str], message_id: str) -> HotelPending:
    sources = {key: value for key, value in pending.get("source_messages", {}).items() if pending.get(key) is not None}
    sources.update({key: message_id for key in explicit_fields if pending.get(key) is not None})
    return pending | {"source_messages": sources}


def capture_hotel_history(pending: HotelPending) -> HotelHistory | None:
    request = cast(HotelPending, {key: pending[key] for key in HISTORY_FIELDS if pending.get(key) is not None})
    sources = pending.get("source_messages", {})
    if not request.get("reference") or not (request.get("nights") or request.get("check_out")):
        return None
    if not request.keys() <= sources.keys():
        return None
    return {"request": request, "source_messages": {key: sources[key] for key in request}}


async def load_hotel_history(
    history: HotelHistory | None, user_id: UUID, thread_id: UUID, load_message: MessageLoader,
) -> HotelHistory | None:
    if not isinstance(history, dict):
        return None
    if not isinstance(history.get("request"), dict) or not isinstance(history.get("source_messages"), dict):
        return None
    request, sources = history["request"], history["source_messages"]
    result = capture_hotel_history(request | {"source_messages": sources})
    if result is None or result != history:
        return None
    source_ids = set()
    try:
        for value in sources.values():
            if not isinstance(value, str):
                return None
            source_ids.add(UUID(value))
    except ValueError:
        return None
    for source in source_ids:
        if await load_message(user_id, thread_id, source) is None:
            return None
    return result


def apply_hotel_replay(history: HotelHistory, changes: HotelPending, message_id: str) -> HotelPending:
    request = history["request"].copy()
    if "nights" in changes and "check_out" not in changes:
        request.pop("check_out", None)
    if "check_out" in changes and "nights" not in changes:
        request.pop("nights", None)
    request.update(changes)
    request["source_messages"] = history["source_messages"]
    return track_hotel_sources(request, frozenset(changes), message_id)
