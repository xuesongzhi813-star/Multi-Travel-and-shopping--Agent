"""Coordinate the official booking API with its already-held conversation lock."""

from uuid import UUID

from psycopg import AsyncConnection

from customer_support_chat.app.business.booking import (
    BookingDecision, BookingOwner, BookingScope, ProposalConflict, ProposalNotFound, ProposalView,
)
from customer_support_chat.app.business.booking_repository import BookingRepository
from customer_support_chat.app.workflow.checkpoints import GraphRuntime, GraphUnavailable


class BookingCoordinator:
    def __init__(self, repository: BookingRepository):
        self.repository = repository

    async def projection(self, graph: GraphRuntime, owner: BookingOwner) -> dict | None:
        proposal = await self.repository.latest_proposal(owner)
        if proposal is None:
            return None
        state = await graph.read_booking(owner.thread_id)
        return proposal.to_json() | {
            "resume_required": proposal.decision is not None and state.awaiting_resolution
            and state.action_id == proposal.action_id,
        }

    async def before_message(
        self, graph: GraphRuntime, owner: BookingOwner, connection: AsyncConnection, request_id: UUID,
    ) -> None:
        paused = await graph.read_booking(owner.thread_id, connection=connection)
        if not paused.awaiting_resolution:
            return
        if paused.action_id is None:
            raise GraphUnavailable("Missing suspended booking")
        scope = BookingScope(owner, paused.action_id)
        proposal = await self.repository.get_proposal(scope, connection=connection)
        if proposal is None:
            raise GraphUnavailable("Missing suspended proposal")
        if proposal.status == "expired" and proposal.decision is None:
            await graph.resume_booking(scope, connection=connection, request_id=request_id)
            return
        raise ProposalConflict("booking_pending")

    async def decide(
        self, graph: GraphRuntime, scope: BookingScope, decision: BookingDecision,
        connection: AsyncConnection, request_id: UUID,
    ) -> ProposalView:
        proposal = await self.repository.get_proposal(scope, connection=connection)
        if proposal is None:
            raise ProposalNotFound()
        paused = await graph.read_booking(scope.owner.thread_id, connection=connection)
        if proposal.status in {"awaiting_decision", "decided"} and (
            not paused.awaiting_resolution or paused.action_id != scope.action_id
        ):
            raise GraphUnavailable("Pending proposal without its checkpoint")
        await self.repository.record_decision(scope, decision)
        return await graph.resume_booking(scope, connection=connection, request_id=request_id)
