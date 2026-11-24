"""Load sourced hotel facts and explicitly synthetic inventory."""

from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


CATALOG_PATH = Path(__file__).resolve().parents[2] / "data" / "zurich_hotels.json"


@dataclass(frozen=True)
class HotelCatalog:
    hotels: tuple[dict, ...]
    inventory: tuple[dict, ...]


def _date(value: str) -> date:
    checked = date.fromisoformat(value)
    if checked.isoformat() != value:
        raise ValueError("Expected ISO calendar date")
    return checked


def _source(value: str) -> str:
    parts = urlsplit(value)
    if (
        any(char.isspace() or ord(char) < 32 for char in value)
        or parts.scheme != "https" or parts.netloc != "www.zuerich.com"
        or not re.fullmatch(r"/en/accommodation/[a-z0-9-]+", parts.path)
        or parts.query or parts.fragment
    ):
        raise ValueError("Expected an official hotel directory entry")
    return value


def load_hotel_catalog(path: Path = CATALOG_PATH) -> HotelCatalog:
    payload = json.loads(path.read_text(encoding="utf-8"))
    hotels = {}
    inventory = {}
    for entry in payload["hotels"]:
        identifier, name, facilities = entry["id"], entry["name"], entry["facilities"]
        if (
            not re.fullmatch(r"zurich-[a-z0-9-]{1,25}", identifier) or identifier in hotels
            or not isinstance(name, str) or not 1 <= len(name.strip()) <= 120
            or entry["area"] not in {"city", "airport"} or not isinstance(facilities, list) or not facilities
            or any(not isinstance(item, str) or not item.strip() for item in facilities)
        ):
            raise ValueError("Invalid sourced hotel")
        demo = entry["demo"]
        if demo["currency"] != "CHF" or not demo["terms"].startswith("合成演示"):
            raise ValueError("Hotel inventory must be synthetic CHF inventory")
        hotels[identifier] = {
            "id": identifier, "name": name, "area": entry["area"], "source_url": _source(entry["source_url"]),
            "checked_on": _date(entry["checked_on"]), "facilities": facilities, "demo": True,
            "demo_terms": demo["terms"],
        }
        for night in demo["nights"]:
            day, room = _date(night["stay_date"]), night["room_type"]
            key = (identifier, day, room)
            quantity, price = night["quantity"], night["price_minor"]
            if (
                key in inventory or room != "standard"
                or type(quantity) is not int or not 0 <= quantity <= 2_147_483_647
                or type(price) is not int or not 0 <= price <= 2_147_483_647
            ):
                raise ValueError("Invalid synthetic hotel inventory")
            inventory[key] = {
                "hotel_id": identifier, "stay_date": day, "room_type": room, "quantity": quantity,
                "price_minor": price, "currency": demo["currency"],
            }
    return HotelCatalog(tuple(hotels.values()), tuple(inventory.values()))
