"""Audit the parent graph's narrowly scoped hotel booking resolution."""

import asyncio
from datetime import datetime, timezone
from uuid import UUID

from customer_support_chat.app.business.booking import BookingResolution, BookingScope, ProposalConflict, ProposalNotFound
from customer_support_chat.app.business.booking_repository import BookingRepository

from .executor import AuditSink, ToolExecutionRecord, ToolStatus, write_tool_audit


class ApprovedBookingExecutor:
    def __init__(self, repository: BookingRepository, audit_sink: AuditSink):
        self.repository = repository
        self.audit_sink = audit_sink

    async def execute(self, scope: BookingScope, *, request_id: UUID) -> BookingResolution:
        loop = asyncio.get_running_loop()
        started = loop.time()
        timestamp = datetime.now(timezone.utc)
        status: ToolStatus = "tool_error"
        error_kind = "execution_failure"
        summary = {}
        try:
            result = await self.repository.resolve_action(scope)
            state = result.proposal.status
            status = "ok" if state == "booked" else "permission_denied" if state == "rejected" else "not_found"
            error_kind = None if state == "booked" else state
            summary = {"booking_status": state, "reused_receipt": result.reused_receipt}
            return result
        except ProposalNotFound:
            status, error_kind = "not_found", "missing_action"
            raise
        except ProposalConflict as error:
            status, error_kind = "permission_denied", error.code
            raise
        except (TimeoutError, asyncio.CancelledError):
            status, error_kind = "timeout", "booking_timeout"
            raise
        finally:
            await write_tool_audit(self.audit_sink, ToolExecutionRecord(
                request_id, scope.owner.thread_id, scope.action_id, f"booking-{scope.action_id}",
                "book_hotel", "builtin", status, error_kind, {}, summary, 0,
                int((loop.time() - started) * 1000), timestamp,
            ), timeout=0.5)
