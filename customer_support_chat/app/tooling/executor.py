"""Local tool registry, execution policy and bounded audit delivery."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import re
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ValidationError


logger = logging.getLogger(__name__)
def valid_tool_call_id(value: str) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value) is not None
        and re.fullmatch(r"DEMO-[A-Z][0-9]{1,3}", value, re.IGNORECASE) is None
    )


ToolStatus = Literal["ok", "invalid_arguments", "permission_denied", "not_found", "timeout", "tool_error"]


@dataclass(frozen=True)
class ToolContext:
    user_id: UUID
    role: str
    run_id: UUID
    thread_id: UUID | None = None
    action_id: UUID | None = None


ToolHandler = Callable[[ToolContext, BaseModel, str], Awaitable[Mapping[str, Any] | None]]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    arguments_model: type[BaseModel]
    result_model: type[BaseModel]
    source: str
    allowed_roles: frozenset[str]
    handler: ToolHandler
    read_only: bool = True
    audit_argument_fields: frozenset[str] = frozenset()
    audit_result_fields: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ToolResult:
    tool_call_id: str
    status: ToolStatus
    data: dict[str, Any] | None = None
    error_kind: str | None = None


@dataclass(frozen=True)
class ToolExecutionRecord:
    run_id: UUID
    thread_id: UUID | None
    action_id: UUID | None
    tool_call_id: str
    tool_name: str
    source: str
    status: ToolStatus
    error_kind: str | None
    arguments: dict[str, Any]
    result_summary: dict[str, Any]
    retry_count: int
    elapsed_ms: int
    created_at: datetime


class AuditSink(Protocol):
    async def write(self, record: ToolExecutionRecord) -> None: ...


class RetryableToolError(RuntimeError):
    """A read-only tool transport failed in a way that permits one retry."""


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        if not definition.read_only:
            raise ValueError("Only read-only tools may be registered")
        if definition.name in self._tools:
            raise ValueError("Tool name already registered")
        self._tools[definition.name] = definition

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def list_for_role(self, role: str) -> tuple[dict[str, Any], ...]:
        return tuple(
            {"name": tool.name, "description": tool.description,
             "input_schema": tool.arguments_model.model_json_schema(), "source": tool.source}
            for tool in self._tools.values() if role in tool.allowed_roles
        )


class ToolExecutor:
    def __init__(
        self, registry: ToolRegistry, audit_sink: AuditSink, *, timeout: float = 2, audit_timeout: float = 0.5,
    ) -> None:
        self.registry = registry
        self._audit_sink = audit_sink
        self._timeout = timeout
        self._audit_timeout = audit_timeout

    async def reject_envelope(self, context: ToolContext, name: str, tool_call_id: str) -> None:
        if not valid_tool_call_id(tool_call_id):
            return
        definition = self.registry.get(name)
        authorized = definition is not None and context.role in definition.allowed_roles
        await self._write_audit(ToolExecutionRecord(
            context.run_id, context.thread_id, context.action_id, tool_call_id,
            definition.name if definition else "unknown", definition.source if definition else "unknown",
            "invalid_arguments" if authorized else "permission_denied",
            "invalid_call_envelope" if authorized else "not_authorized",
            {}, {}, 0, 0, datetime.now(timezone.utc),
        ))

    async def execute(
        self, context: ToolContext, name: str, arguments: Mapping[str, Any], tool_call_id: str,
    ) -> ToolResult:
        if not valid_tool_call_id(tool_call_id):
            return ToolResult(tool_call_id, "invalid_arguments", error_kind="invalid_call_id")
        cancellation = None
        loop = asyncio.get_running_loop()
        started = loop.time()
        timestamp = datetime.now(timezone.utc)
        definition = self.registry.get(name)
        retries = 0
        argument_summary = {}
        result_summary = {}
        if definition is None or context.role not in definition.allowed_roles:
            result = ToolResult(tool_call_id, "permission_denied", error_kind="not_authorized")
        else:
            try:
                if not isinstance(arguments, Mapping) or set(arguments) - definition.arguments_model.model_fields.keys():
                    raise ValueError("Invalid tool arguments")
                validated = definition.arguments_model.model_validate(dict(arguments), strict=True)
            except (ValidationError, ValueError):
                result = ToolResult(tool_call_id, "invalid_arguments", error_kind="input_validation")
            else:
                argument_summary = validated.model_dump(mode="json", include=definition.audit_argument_fields)
                try:
                    async with asyncio.timeout_at(started + self._timeout):
                        try:
                            raw = await definition.handler(context, validated, tool_call_id)
                        except RetryableToolError:
                            retries = 1
                            raw = await definition.handler(context, validated, tool_call_id)
                    if raw is None:
                        result = ToolResult(tool_call_id, "not_found")
                    else:
                        declared = {key: raw[key] for key in definition.result_model.model_fields if key in raw}
                        verified = definition.result_model.model_validate(declared, strict=True)
                        result = ToolResult(tool_call_id, "ok", verified.model_dump(mode="json"))
                        result_summary = verified.model_dump(mode="json", include=definition.audit_result_fields)
                except asyncio.CancelledError as error:
                    cancellation = error
                    result = ToolResult(tool_call_id, "timeout", error_kind="caller_cancelled")
                except TimeoutError:
                    result = ToolResult(tool_call_id, "timeout", error_kind="tool_timeout")
                except ValidationError:
                    result = ToolResult(tool_call_id, "tool_error", error_kind="output_validation")
                except RetryableToolError:
                    result = ToolResult(tool_call_id, "tool_error", error_kind="transport_failure")
                except Exception:
                    result = ToolResult(tool_call_id, "tool_error", error_kind="execution_failure")

        record = ToolExecutionRecord(
            context.run_id, context.thread_id, context.action_id, tool_call_id,
            definition.name if definition else "unknown", definition.source if definition else "unknown",
            result.status, result.error_kind, argument_summary, result_summary, retries,
            int((loop.time() - started) * 1000), timestamp,
        )
        await self._write_audit(record)
        if cancellation is not None:
            raise cancellation
        return result

    async def _write_audit(self, record: ToolExecutionRecord) -> None:
        try:
            async with asyncio.timeout(self._audit_timeout):
                await self._audit_sink.write(record)
        except asyncio.CancelledError:
            logger.warning("tool_audit_failed run_id=%s category=cancelled", record.run_id)
            raise
        except TimeoutError:
            logger.warning("tool_audit_failed run_id=%s category=timeout", record.run_id)
        except Exception:
            logger.warning("tool_audit_failed run_id=%s category=write_failed", record.run_id)
