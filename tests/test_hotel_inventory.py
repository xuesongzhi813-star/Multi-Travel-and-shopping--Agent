from dataclasses import replace
from datetime import date, timedelta

from test_business_migrations import BusinessDatabaseTest

from customer_support_chat.app.business.repository import BusinessRepository
from customer_support_chat.app.workflow.hotel_handoff import HotelOption, HotelQuery


class HotelInventoryTests(BusinessDatabaseTest):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.repository = BusinessRepository(self.engine, self.settings.schema)
        self.first = date(2026, 12, 15)
        self.query = HotelQuery("Zürich", "Europe/Zurich", self.first, self.first + timedelta(days=2))
        ids = ("available", "cheaper", "no-row", "no-stock", "other-room", "other-currency")
        async with self.engine.begin() as connection:
            await connection.execute(self.db.hotels.insert(), [
                dict(id=hotel, name=hotel, area="city" if hotel == "cheaper" else "airport")
                for hotel in ids
            ])
            for hotel in ids:
                row = dict(hotel_id=hotel, stay_date=self.first, room_type="standard",
                           quantity=1, price_minor=18000, currency="CHF")
                await connection.execute(self.db.hotel_inventory.insert().values(**row))
                if hotel == "no-row":
                    continue
                second = row | dict(stay_date=self.first + timedelta(days=1), price_minor=19000)
                if hotel == "cheaper":
                    second |= dict(price_minor=10000)
                if hotel == "other-room":
                    second |= dict(room_type="suite")
                if hotel == "no-stock":
                    second |= dict(quantity=0)
                if hotel == "other-currency":
                    second |= dict(currency="EUR")
                await connection.execute(self.db.hotel_inventory.insert().values(**second))

    async def test_complete_stay_prices_and_filters_room_types(self):
        options = await self.repository.list_available_hotels(self.query)
        self.assertEqual([(item.hotel_id, item.total_minor, item.currency) for item in options],
                         [("cheaper", 28000, "CHF"), ("available", 37000, "CHF")])
        self.assertEqual(await self.repository.list_available_hotels(
            replace(self.query, area="airport", room_type="standard")), [options[1]])
        self.assertEqual(await self.repository.list_available_hotels(
            replace(self.query, room_type="suite")), [])

    async def test_unsupported_city_never_reuses_zurich_inventory(self):
        self.assertEqual(await self.repository.list_available_hotels(
            replace(self.query, city="Wien", timezone="Europe/Vienna")), [])
        one_night = replace(self.query, check_out=self.first + timedelta(days=1))
        options = await self.repository.list_available_hotels(one_night)
        self.assertEqual(options[0].total_minor, 18000)
        self.assertEqual(options[0].to_json()["check_out"], "2026-12-16")
        self.assertEqual(HotelOption.from_json(options[0].to_json()), options[0])
        self.assertEqual(HotelQuery.from_json(one_night.to_json()), one_night)

    async def test_applies_chf_budget_to_full_stay_before_limit(self):
        async with self.engine.begin() as connection:
            for index in range(5):
                hotel = f"currency-fixture-{index}"
                await connection.execute(self.db.hotels.insert().values(id=hotel, name=hotel, area="airport"))
                await connection.execute(self.db.hotel_inventory.insert(), [
                    {"hotel_id": hotel, "stay_date": self.first + timedelta(days=day), "room_type": "standard",
                     "quantity": 1, "price_minor": 1, "currency": "CAD"}
                    for day in range(2)
                ])
        legacy = await self.repository.list_available_hotels(self.query)
        self.assertEqual({item.currency for item in legacy}, {"CAD"})
        within_budget = await self.repository.list_available_hotels(replace(self.query, max_total_chf_minor=37000))
        self.assertEqual([(item.hotel_id, item.total_minor) for item in within_budget],
                         [("cheaper", 28000), ("available", 37000)])
        lower_budget = await self.repository.list_available_hotels(replace(self.query, max_total_chf_minor=36999))
        self.assertEqual([item.hotel_id for item in lower_budget], ["cheaper"])

    def test_query_payload_accepts_legacy_and_budget(self):
        legacy = self.query.to_json()
        legacy.pop("max_total_chf_minor", None)
        self.assertIsNone(HotelQuery.from_json(legacy).max_total_chf_minor)
        limited = replace(self.query, max_total_chf_minor=37000)
        self.assertEqual(HotelQuery.from_json(limited.to_json()), limited)
