"""Bind the only flight lookup to the host's authenticated user."""

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime
import re
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from customer_support_chat.app.business.repository import BusinessRepository
from customer_support_chat.app.tooling.executor import ToolDefinition, ToolRegistry


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


class LookupArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    reference: Annotated[str, Field(pattern=f"^{FLIGHT_REFERENCE}$")]


class FlightToolData(BaseModel):
    reference: str
    arrival_airport: str
    arrival_at_utc: datetime
    arrival_timezone: str


def register_flight_tool(registry: ToolRegistry, lookup: FlightLookup) -> None:
    async def handle(context, arguments, tool_call_id):
        facts = await lookup(context.user_id, arguments.reference)
        if facts is None or facts.reference != arguments.reference:
            return None
        return asdict(facts)

    registry.register(ToolDefinition(
        "lookup_flight", "Read the authenticated user's synthetic flight by reference",
        LookupArguments, FlightToolData, "builtin", frozenset({"flight"}), handle,
    ))


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
