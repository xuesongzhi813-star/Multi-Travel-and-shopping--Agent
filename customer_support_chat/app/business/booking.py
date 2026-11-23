"""Shared booking contracts for the parent graph and the HTTP application."""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal
from uuid import UUID


BookingDecision = Literal["approve", "reject"]
BookingStatus = Literal["awaiting_decision", "decided", "booked", "rejected", "expired", "stale"]


@dataclass(frozen=True)
class BookingOwner:
    user_id: UUID
    thread_id: UUID


@dataclass(frozen=True)
class BookingSelection:
    message_id: UUID
    option_index: int


@dataclass(frozen=True)
class BookingScope:
    owner: BookingOwner
    action_id: UUID


@dataclass(frozen=True)
class ProposalView:
    action_id: UUID
    source_message_id: UUID
    option_index: int
    hotel_name: str
    room_type: str
    check_in: date
    check_out: date
    total_minor: int
    currency: str
    created_at: datetime
    expires_at: datetime
    status: BookingStatus
    decision: BookingDecision | None
    booking_id: UUID | None = None

    def to_json(self) -> dict:
        return {
            "action_id": str(self.action_id), "message_id": str(self.source_message_id),
            "option_index": self.option_index, "hotel_name": self.hotel_name, "room_type": self.room_type,
            "check_in": self.check_in.isoformat(), "check_out": self.check_out.isoformat(),
            "total_minor": self.total_minor, "currency": self.currency,
            "created_at": self.created_at.isoformat(), "expires_at": self.expires_at.isoformat(),
            "status": self.status, "decision": self.decision,
            "booking_id": str(self.booking_id) if self.booking_id is not None else None,
        }


class ProposalNotFound(LookupError):
    """No proposal exists for this authenticated owner and scope."""


class ProposalConflict(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code
