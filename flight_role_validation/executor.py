"""Bind the only flight lookup to the host's authenticated user."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
import re
from uuid import UUID

from customer_support_chat.app.business.repository import BusinessRepository


FLIGHT_REFERENCE = r"DEMO-[A-Z][0-9]{1,3}"


@dataclass(frozen=True)
class FlightFacts:
    reference: str
    arrival_airport: str
    arrival_at_utc: datetime
    arrival_timezone: str


@dataclass(frozen=True)
class FlightEvidence:
    facts: FlightFacts
    tool_call_id: str


FlightLookup = Callable[[UUID, str], Awaitable[FlightFacts | None]]


def build_repository_lookup(repository: BusinessRepository) -> FlightLookup:
    async def find(user_id: UUID, reference: str) -> FlightFacts | None:
        for row in await repository.list_itineraries(user_id):
            if row["reference"] == reference:
                return FlightFacts(reference, row["destination"], row["arrival_at"], row["arrival_timezone"])
        return None

    return find


class FlightExecutor:
    def __init__(self, user_id: UUID, lookup: FlightLookup):
        self._user_id = user_id
        self._lookup = lookup
        self._evidence: dict[str, FlightEvidence] = {}

    async def lookup_flight(self, reference: str, tool_call_id: str) -> FlightFacts | None:
        if re.fullmatch(FLIGHT_REFERENCE, reference) is None:
            raise ValueError("Invalid flight reference")
        facts = await self._lookup(self._user_id, reference)
        if facts is not None and facts.reference == reference:
            self._evidence[reference] = FlightEvidence(facts, tool_call_id)
            return facts
        return None

    def evidence(self, reference: str) -> FlightEvidence | None:
        return self._evidence.get(reference)
