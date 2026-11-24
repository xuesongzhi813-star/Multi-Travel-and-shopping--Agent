"""Deterministic identifiers and synthetic travel data for the local demo."""

import asyncio
from datetime import date, datetime
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from .auth import hash_password
from .hotel_catalog import load_hotel_catalog
from .schema import tables


DEMO_USERS = (
    (UUID("4c6f56bd-2734-4a9c-87b7-917f8252ecb1"), "demo-a"),
    (UUID("4c6f56bd-2734-4a9c-87b7-917f8252ecb2"), "demo-b"),
)
FLIGHTS = (
    ("DEMO-A1", 0, "DM101", "LHR", "ZRH", "2026-12-14T21:55:00+00:00", "2026-12-14T23:30:00+00:00", "Europe/Zurich"),
    ("DEMO-A2", 0, "DM102", "ZRH", "VIE", "2026-12-17T08:00:00+00:00", "2026-12-17T09:20:00+00:00", "Europe/Vienna"),
    ("DEMO-A3", 0, "DM103", "VIE", "ZRH", "2026-12-20T11:00:00+00:00", "2026-12-20T12:30:00+00:00", "Europe/Zurich"),
    ("DEMO-B1", 1, "DM201", "AMS", "ZRH", "2026-12-15T12:00:00+00:00", "2026-12-15T13:30:00+00:00", "Europe/Zurich"),
    ("DEMO-B2", 1, "DM202", "ZRH", "BER", "2026-12-18T14:00:00+00:00", "2026-12-18T15:30:00+00:00", "Europe/Berlin"),
    ("DEMO-B3", 1, "DM203", "BER", "AMS", "2026-12-21T09:00:00+00:00", "2026-12-21T10:30:00+00:00", "Europe/Amsterdam"),
)
HOTELS = (
    {"id": "demo-airport", "name": "Demo Airport Hotel", "area": "airport"},
    {"id": "demo-city", "name": "Demo City Hotel", "area": "city"},
)
INVENTORY = (
    {"hotel_id": "demo-airport", "stay_date": date(2026, 12, 15), "room_type": "standard",
     "quantity": 3, "price_minor": 18000, "currency": "CHF"},
    {"hotel_id": "demo-city", "stay_date": date(2026, 12, 15), "room_type": "standard",
     "quantity": 0, "price_minor": 14500, "currency": "CHF"},
)


def flight_rows():
    return [
        {
            "reference": ref, "user_id": DEMO_USERS[user][0], "flight_number": number,
            "origin": origin, "destination": destination, "departure_at": datetime.fromisoformat(departure),
            "arrival_at": datetime.fromisoformat(arrival), "arrival_timezone": timezone,
        }
        for ref, user, number, origin, destination, departure, arrival, timezone in FLIGHTS
    ]


async def seed_demo(engine: AsyncEngine, schema: str, passwords: tuple[str, str]) -> None:
    if len(passwords) != 2 or passwords[0] == passwords[1] or any(not 12 <= len(item) <= 256 for item in passwords):
        raise ValueError("configure two distinct demo passwords of 12 to 256 characters")
    hashes = await asyncio.to_thread(lambda: [hash_password(password) for password in passwords])
    db = tables(schema)
    catalog = load_hotel_catalog()
    users = [
        {"id": user_id, "username": username, "password_hash": password_hash}
        for (user_id, username), password_hash in zip(DEMO_USERS, hashes, strict=True)
    ]
    async with engine.begin() as connection:
        for table, rows in (
            (db.users, users), (db.flight_itineraries, flight_rows()),
            (db.hotels, HOTELS), (db.hotels, catalog.hotels),
            (db.hotel_inventory, INVENTORY), (db.hotel_inventory, catalog.inventory),
        ):
            statement = insert(table).values(list(rows)).on_conflict_do_nothing(
                index_elements=list(table.primary_key.columns),
            )
            await connection.execute(statement)
