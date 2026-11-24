from datetime import date
from pathlib import Path
import unittest

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from sqlalchemy import func, select

from customer_support_chat.app.business.booking import BookingOwner, BookingScope, BookingSelection
from customer_support_chat.app.business.booking_repository import BookingRepository
from customer_support_chat.app.business.database import connection_kwargs
from customer_support_chat.app.business.demo_seed import DEMO_USERS, seed_demo
from customer_support_chat.app.business.hotel_catalog import load_hotel_catalog
from customer_support_chat.app.business.repository import BusinessRepository
from customer_support_chat.app.workflow.hotel_handoff import HotelOption, HotelQuery
from test_business_migrations import BusinessDatabaseTest
from test_business_seed import PASSWORDS


ROOT = Path(__file__).resolve().parents[1]


class HotelCatalogTests(unittest.TestCase):
    def test_catalog_eight_sourced_hotels_and_demo_only_inventory(self):
        catalog = load_hotel_catalog(ROOT / "customer_support_chat/data/zurich_hotels.json")
        self.assertEqual(len(catalog.hotels), 8)
        self.assertEqual({row["area"] for row in catalog.hotels}, {"city", "airport"})
        self.assertTrue(all(row["demo"] and row["facilities"] and row["demo_terms"] for row in catalog.hotels))
        self.assertTrue(all(row["source_url"].startswith("https://www.zuerich.com/en/accommodation/") for row in catalog.hotels))
        self.assertTrue(all(row["currency"] == "CHF" for row in catalog.inventory))
        self.assertIn(0, {row["quantity"] for row in catalog.inventory})
        self.assertEqual({row["stay_date"] for row in catalog.inventory}, {date(2026, 12, 20), date(2026, 12, 21)})

    def test_legacy_options_omit_provenance_and_new_links_require_https(self):
        day = date(2026, 12, 15)
        legacy = HotelOption("demo-airport", "Demo Airport Hotel", "airport", "standard", day, date(2026, 12, 16), 18000, "CHF")
        payload = legacy.to_json()
        self.assertNotIn("demo", payload)
        self.assertNotIn("source_url", payload)
        self.assertEqual(HotelOption.from_json(payload), legacy)
        html = (ROOT / "web_app/app/templates/local.html").read_text(encoding="utf-8")
        self.assertIn("https:", html)
        self.assertIn("酒店资料来源", html)


class HotelCatalogInventoryTests(BusinessDatabaseTest):
    async def test_multinight_p6_results_and_reseed_preserve_booked_stock(self):
        await seed_demo(self.engine, self.settings.schema, PASSWORDS)
        business = BusinessRepository(self.engine, self.settings.schema)
        query = HotelQuery("Zürich", "Europe/Zurich", date(2026, 12, 20), date(2026, 12, 22), "airport")
        options = await business.list_available_hotels(query)
        selected = next(item for item in options if item.hotel_id == "zurich-kameha")
        self.assertNotIn("zurich-hyatt", {option.hotel_id for option in options})
        self.assertTrue(selected.demo)
        self.assertIn("hotel-kameha-grand", selected.source_url)
        self.assertEqual(HotelOption.from_json(selected.to_json()), selected)
        user = DEMO_USERS[0][0]
        owner = BookingOwner(user, await business.create_thread(user))
        message = await business.append_message(user, owner.thread_id, "演示预订")
        booking = BookingRepository(self.settings)
        async with await AsyncConnection.connect(**connection_kwargs(self.settings), row_factory=dict_row) as connection:
            proposal = await booking.create_proposal(connection, owner, BookingSelection(message, 0), selected)
        scope = BookingScope(owner, proposal.action_id)
        await booking.record_decision(scope, "approve")
        result = await booking.resolve_action(scope)
        self.assertEqual(result.proposal.status, "booked")
        await seed_demo(self.engine, self.settings.schema, PASSWORDS)
        async with self.engine.connect() as connection:
            inventory = self.db.hotel_inventory
            counts = (await connection.execute(select(inventory.c.quantity).where(
                inventory.c.hotel_id == selected.hotel_id,
            ).order_by(inventory.c.stay_date))).scalars().all()
            orders = await connection.scalar(select(func.count()).select_from(self.db.hotel_bookings))
            receipts = await connection.scalar(select(func.count()).select_from(self.db.hotel_booking_receipts))
        self.assertEqual((counts, orders, receipts), ([1, 1], 1, 1))
