"""Local FastAPI service for authenticated business data."""

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
import re
from secrets import token_urlsafe
from typing import Annotated
from uuid import UUID

import httpx
from redis.asyncio import Redis
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from psycopg import Error as PostgresError
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from starlette.exceptions import HTTPException as StarletteHTTPException

from customer_support_chat.app.business.booking import (
    BookingDecision, BookingOwner, BookingScope, BookingSelection, ProposalConflict, ProposalNotFound,
)
from customer_support_chat.app.business.booking_repository import BookingRepository
from customer_support_chat.app.tooling.audit import PostgresAuditSink
from customer_support_chat.app.tooling.booking import ApprovedBookingExecutor
from customer_support_chat.app.business.auth import hash_password, verify_password
from customer_support_chat.app.business.database import DatabaseSettings, create_engine, local_value, read_settings
from customer_support_chat.app.business.repository import BusinessRepository
from customer_support_chat.app.workflow.checkpoints import (
    ROLE_GRAPH_TIMEOUT, GraphUnavailable, open_graph_runtime, read_checkpoint_schema,
)
from customer_support_chat.app.workflow.hotel_handoff import HotelOption, HotelQuery, HotelRoleResult, RoleUnavailable, VerifiedArrival
from customer_support_chat.app.workflow.parent_graph import FaqSearch, REFUSAL_MESSAGE
from customer_support_chat.app.workflow.thread_lock import try_thread_lock


from faq_extension.cache import FaqEvidenceCache
from faq_extension.contracts import FaqSearchResult, PublicFaqQuestion
from faq_extension.mysql_catalog import MySqlFaqCatalog, MySqlFaqSettings
from faq_extension.retrieval import FaqSearchService
from faq_extension.siliconflow import SiliconFlowFaqClient

from .bookings import BookingCoordinator
from .local_tools import FlightSettings, LocalToolError, LocalToolTimeout, ROLE_PYTHON, run_local_tool
from .operations import error_response, http_error, observe_request, postgres_schema_ready, qdrant_ready, redis_ready


COOKIE_NAME = "travel_session"
SESSION_SECONDS = 8 * 60 * 60
MAX_ACTIVE_TURNS = 4
ROLE_TURN_BUDGET = ROLE_GRAPH_TIMEOUT - 2
role_turn: ContextVar[tuple[UUID, float]] = ContextVar("role_turn")
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
POSTGRES_TEXT = r"^[^\x00]+$"


class RequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(RequestBody):
    username: Annotated[str, StringConstraints(min_length=1, max_length=80, strip_whitespace=True, pattern=POSTGRES_TEXT)]
    password: Annotated[str, StringConstraints(min_length=1, max_length=256)]


class FlightRequest(RequestBody):
    reference: Annotated[str, StringConstraints(pattern=r"^DEMO-[A-Z][0-9]{1,3}$")]


class AirportRequest(RequestBody):
    code: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


class MessageRequest(RequestBody):
    content: Annotated[str, StringConstraints(min_length=1, max_length=4000, strip_whitespace=True, pattern=POSTGRES_TEXT)]


class HotelProposalRequest(RequestBody):
    message_id: UUID
    option_index: Annotated[int, Field(strict=True, ge=0, le=4)]


class BookingDecisionRequest(RequestBody):
    decision: BookingDecision


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    token_digest: str


def token_digest(token: str) -> str:
    return sha256(token.encode("ascii")).hexdigest()


def get_repository(request: Request) -> BusinessRepository:
    return request.app.state.repository


Database = Annotated[BusinessRepository, Depends(get_repository)]


async def get_principal(
    repository: Database,
    token: Annotated[str | None, Cookie(alias=COOKIE_NAME)] = None,
) -> Principal:
    if token is None or TOKEN_PATTERN.fullmatch(token) is None:
        raise HTTPException(401, "Authentication required")
    digest = token_digest(token)
    user_id = await repository.authenticated_user(digest)
    if user_id is None:
        raise HTTPException(401, "Authentication required")
    return Principal(user_id, digest)


Identity = Annotated[Principal, Depends(get_principal)]


@asynccontextmanager
async def locked_turn(state, thread_id: UUID):
    if thread_id in state.active_threads:
        raise HTTPException(409, "Thread is busy")
    if len(state.active_threads) >= MAX_ACTIVE_TURNS:
        raise HTTPException(503, "Graph turn capacity reached")
    state.active_threads.add(thread_id)
    try:
        async with try_thread_lock(state.database_settings, state.checkpoint_schema, thread_id) as connection:
            if connection is None:
                raise HTTPException(409, "Thread is busy")
            yield connection
    finally:
        state.active_threads.remove(thread_id)


def booking_conflict(request: Request, error: ProposalConflict):
    details = {
        "booking_pending": "本会话有待处理的预订提案。",
        "proposal_already_pending": "本会话有待处理的预订提案。",
        "quote_changed": "演示库存或价格已改变，请重新查询酒店。",
        "decision_already_recorded": "该提案已有决定。",
    }
    return error_response(request, 409, error.code, details.get(error.code, "请重新查询酒店并选择。"))


@asynccontextmanager
async def open_faq_search():
    try:
        catalog = MySqlFaqCatalog(MySqlFaqSettings.read())
        qdrant_port = int(local_value("QDRANT_PORT", "6333"))
        redis_port = int(local_value("REDIS_PORT", "6379"))
        if not all(1 <= port <= 65535 for port in (qdrant_port, redis_port)):
            raise ValueError("Invalid local FAQ ports")
    except (ValueError, OSError):
        yield None
        return

    provider = None
    if local_value("TRAVEL_FAQ_MODE", "disabled") == "siliconflow":
        key = local_value("SILICONFLOW_API_KEY").strip()
        if key:
            provider = SiliconFlowFaqClient(key)
    async with (
        httpx.AsyncClient(base_url=f"http://127.0.0.1:{qdrant_port}", timeout=8, trust_env=False) as qdrant,
        Redis(host="127.0.0.1", port=redis_port, socket_timeout=0.2, socket_connect_timeout=0.2) as redis,
    ):
        yield FaqSearchService(catalog, qdrant, provider, FaqEvidenceCache(redis))


def create_app(
    settings: DatabaseSettings | None = None, *, checkpoint_schema: str | None = None,
    faq_search: FaqSearch | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config = settings or read_settings()
        schema = checkpoint_schema if checkpoint_schema is not None else read_checkpoint_schema(config.schema)
        flight_settings = FlightSettings.read()
        engine = create_engine(config)
        try:
            repository = BusinessRepository(engine, config.schema)
            booking_repository = BookingRepository(config)
            booking_executor = ApprovedBookingExecutor(booking_repository, PostgresAuditSink(engine, config.schema))
            app.state.bookings = BookingCoordinator(booking_repository)
            app.state.repository = repository
            app.state.database_settings = config
            app.state.checkpoint_schema = schema
            app.state.active_threads = set()
            app.state.tool_slots = asyncio.Semaphore(2)
            app.state.flight = flight_settings
            app.state.live_active = False
            app.state.engine = engine
            app.state.dummy_password_hash = await asyncio.to_thread(hash_password, token_urlsafe(32))
            async def role_tool(operation: str, user: UUID, thread: UUID, value: str | dict) -> dict:
                request_id, deadline = role_turn.get()
                slots = app.state.tool_slots
                if slots.locked():
                    raise RoleUnavailable("Local tools are busy")
                try:
                    async with slots:
                        return await run_local_tool(
                            operation, user, value, config, request_id,
                            flight_mode="offline", thread_id=thread, deadline=deadline,
                        )
                except LocalToolError as error:
                    raise RoleUnavailable("Local role failed") from error

            async def flight_lookup(user: UUID, thread: UUID, reference: str) -> VerifiedArrival | None:
                result = await role_tool("flight", user, thread, reference)
                if result.get("status") == "not_found":
                    return None
                try:
                    handoff, airport = result["handoff"], result["airport"]
                    if result["status"] != "verified" or airport["status"] != "ok":
                        raise ValueError("Unverified flight or airport")
                    airport_data = airport["data"]
                    if airport_data["code"] != handoff["arrival_airport"]:
                        raise ValueError("Airport evidence mismatch")
                    return VerifiedArrival(
                        handoff["reference"], airport_data["city"] or "", airport_data["code"],
                        date.fromisoformat(handoff["arrival_date_local"]), handoff["arrival_timezone"],
                        handoff["source_tool_call_id"],
                    )
                except (KeyError, TypeError, ValueError) as error:
                    raise RoleUnavailable("Invalid flight receipt") from error

            async def hotel_search(user: UUID, thread: UUID, query: HotelQuery) -> HotelRoleResult:
                result = await role_tool("hotel", user, thread, query.to_json())
                status = result.get("status")
                if status in ("unavailable", "no_availability"):
                    return HotelRoleResult(status)
                try:
                    if status != "hotel_results":
                        raise ValueError("Invalid hotel outcome")
                    options = tuple(HotelOption.from_json(row) for row in result["options"])
                    if not 1 <= len(options) <= 5 or any(
                        option.check_in != query.check_in or option.check_out != query.check_out
                        or (query.area is not None and option.area != query.area)
                        or (query.room_type is not None and option.room_type != query.room_type)
                        for option in options
                    ):
                        raise ValueError("Invalid hotel options")
                    return HotelRoleResult("hotel_results", options)
                except (KeyError, TypeError, ValueError) as error:
                    raise RoleUnavailable("Invalid hotel receipt") from error

            async with open_faq_search() as faq_service:
                async def search_policy(question: PublicFaqQuestion) -> FaqSearchResult:
                    if faq_service is None:
                        return FaqSearchResult(status="unavailable")
                    _, deadline = role_turn.get()
                    return await faq_service.search(question, deadline=deadline)

                async with open_graph_runtime(
                    config, schema, repository.read_owned_message, flight_lookup=flight_lookup, hotel_search=hotel_search,
                    booking_repository=booking_repository, booking_executor=booking_executor,
                    faq_search=faq_search or search_policy,
                ) as graph:
                    app.state.graph = graph
                    yield
        finally:
            await engine.dispose()

    app = FastAPI(title="Local travel business API", lifespan=lifespan)

    app.middleware("http")(observe_request)
    app.add_exception_handler(StarletteHTTPException, http_error)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError):
        return error_response(request, 422, "invalid_request", "Invalid request")

    @app.get("/health/live")
    async def liveness():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def readiness(request: Request):
        state = request.app.state
        schema = state.database_settings.schema
        business_relations = ("tool_audit_logs", "hotel_booking_proposals", "hotel_bookings", "hotel_booking_receipts")
        business, checkpoints, redis, qdrant = await asyncio.gather(
            postgres_schema_ready(state.engine, *(f"{schema}.{name}" for name in business_relations)),
            postgres_schema_ready(state.engine, f"{state.checkpoint_schema}.checkpoints"),
            redis_ready(), qdrant_ready(),
        )
        worker = ROLE_PYTHON.is_file()
        required_ready = business and checkpoints and worker
        status = "unavailable" if not required_ready else "ok" if redis and qdrant else "degraded"
        return JSONResponse(status_code=200 if required_ready else 503, content={
            "status": status,
            "dependencies": {
                "business_database": business, "graph_checkpoints": checkpoints, "role_worker": worker,
                "redis": redis, "qdrant": qdrant,
            },
            "flight_mode": state.flight.mode,
            "model_connectivity": "not_probed",
        })

    @app.get("/", response_class=FileResponse, include_in_schema=False)
    async def home():
        return FileResponse(Path(__file__).with_name("templates") / "local.html", media_type="text/html")

    async def local_tool(request: Request, identity: Principal, operation: str, value: str):
        state = request.app.state
        slots = state.tool_slots
        mode = state.flight.mode if operation == "flight" else "direct"
        live = mode == "deepseek"
        if live and state.flight.remaining_runs <= 0:
            return error_response(request, 429, "model_budget_exhausted", "本次启动的模型额度已用完")
        if slots.locked() or (live and state.live_active):
            return error_response(request, 503, "tool_busy", "本地工具繁忙")

        if live:
            state.live_active = True
            state.flight.remaining_runs -= 1
        try:
            async with slots:
                result = await run_local_tool(
                    operation, identity.user_id, value, state.database_settings, request.state.request_id,
                    flight_mode=mode, api_key=state.flight.api_key if live else "",
                )
        except LocalToolTimeout:
            return error_response(request, 504, "worker_timeout", "本地工具超时")
        except LocalToolError:
            return error_response(request, 503, "worker_unavailable", "本地工具暂不可用")
        finally:
            if live:
                state.live_active = False
        if live and result["provider_requests"] == 0:
            state.flight.remaining_runs += 1

        reason = result.get("reason") or result.get("error_kind")
        if reason == "provider_rate_limit":
            return error_response(request, 429, reason, "模型服务限流，稍后再试")
        if result["status"] in {"timeout", "tool_error", "unavailable"}:
            code = 504 if (result["status"] == "timeout" or reason == "timeout") else 503
            return error_response(request, code, reason or "tool_unavailable", "查询暂不可用")
        return result

    @app.post("/api/tools/flight")
    async def flight_tool(body: FlightRequest, request: Request, identity: Identity):
        return await local_tool(request, identity, "flight", body.reference)

    @app.post("/api/tools/airport")
    async def airport_tool(body: AirportRequest, request: Request, identity: Identity):
        return await local_tool(request, identity, "airport", body.code)

    @app.post("/api/auth/login", status_code=204)
    async def login(body: LoginRequest, request: Request, response: Response, repository: Database) -> None:
        account = await repository.find_account(body.username)
        stored_hash = account.password_hash if account is not None else request.app.state.dummy_password_hash
        valid = await asyncio.to_thread(verify_password, body.password, stored_hash)
        if account is None or not valid:
            raise HTTPException(401, "Invalid credentials")
        token = token_urlsafe(32)
        await repository.issue_session(
            account.id, token_digest(token), datetime.now(timezone.utc) + timedelta(seconds=SESSION_SECONDS),
        )
        response.set_cookie(
            COOKIE_NAME, token, max_age=SESSION_SECONDS, path="/",
            httponly=True, samesite="strict", secure=request.url.scheme == "https",
        )

    @app.post("/api/auth/logout", status_code=204)
    async def logout(request: Request, response: Response, repository: Database, identity: Identity) -> None:
        await repository.revoke_session(identity.user_id, identity.token_digest)
        response.delete_cookie(
            COOKIE_NAME, path="/", httponly=True, samesite="strict", secure=request.url.scheme == "https",
        )

    @app.post("/api/threads", status_code=201)
    async def create_thread(repository: Database, identity: Identity, body: RequestBody | None = None):
        return {"thread_id": await repository.create_thread(identity.user_id)}

    @app.get("/api/threads/{thread_id}")
    async def get_thread(thread_id: UUID, request: Request, repository: Database, identity: Identity):
        if not await repository.owns_thread(identity.user_id, thread_id):
            raise HTTPException(404, "Thread not found")
        graph_unavailable = False
        booking = None
        try:
            state = request.app.state
            graph = await state.graph.read(thread_id)
            booking = await state.bookings.projection(state.graph, BookingOwner(identity.user_id, thread_id))
        except (GraphUnavailable, PostgresError):
            graph_unavailable = True
            graph = None
        thread = await repository.load_thread(identity.user_id, thread_id)
        if thread is None:
            raise HTTPException(404, "Thread not found")
        if graph_unavailable:
            request.state.error_type = "graph_unavailable"
            return JSONResponse(
                status_code=503, content=jsonable_encoder(thread | {"detail": "Graph state unavailable", "code": "graph_unavailable", "request_id": str(request.state.request_id)}),
            )
        payload = thread | {"graph": graph}
        if booking is not None:
            payload["booking"] = booking
        return payload

    @app.post("/api/threads/{thread_id}/hotel-proposals", status_code=201)
    async def create_hotel_proposal(
        thread_id: UUID, body: HotelProposalRequest, request: Request, repository: Database, identity: Identity,
    ):
        if not await repository.owns_thread(identity.user_id, thread_id):
            raise HTTPException(404, "Thread not found")
        owner = BookingOwner(identity.user_id, thread_id)
        selection = BookingSelection(body.message_id, body.option_index)
        state = request.app.state
        try:
            async with locked_turn(state, thread_id) as connection:
                prior = await state.bookings.repository.find_proposal(owner, selection, connection=connection)
                proposal = prior or await state.graph.start_proposal(owner, selection, connection=connection)
            return JSONResponse(
                status_code=200 if prior is not None else 201,
                content=proposal.to_json(),
            )
        except ProposalNotFound:
            raise HTTPException(404, "Proposal not found") from None
        except ProposalConflict as error:
            return booking_conflict(request, error)
        except (GraphUnavailable, PostgresError):
            return error_response(request, 503, "booking_unavailable", "预订提案暂不可用。")

    @app.post("/api/threads/{thread_id}/approvals/{action_id}/decision")
    async def decide_hotel_proposal(
        thread_id: UUID, action_id: UUID, body: BookingDecisionRequest, request: Request,
        repository: Database, identity: Identity,
    ):
        if not await repository.owns_thread(identity.user_id, thread_id):
            raise HTTPException(404, "Thread not found")
        state = request.app.state
        scope = BookingScope(BookingOwner(identity.user_id, thread_id), action_id)
        try:
            async with locked_turn(state, thread_id) as connection:
                proposal = await state.bookings.decide(
                    state.graph, scope, body.decision, connection, request.state.request_id,
                )
            return proposal.to_json() | {"resume_required": False}
        except ProposalNotFound:
            raise HTTPException(404, "Proposal not found") from None
        except ProposalConflict as error:
            return booking_conflict(request, error)
        except (GraphUnavailable, PostgresError):
            return error_response(
                request, 503, "booking_unavailable", "预订处理暂不可用，请刷新会话后继续。", action_id=str(action_id),
            )

    @app.post("/api/threads/{thread_id}/messages", status_code=201)
    async def append_message(
        thread_id: UUID, body: MessageRequest, request: Request, repository: Database, identity: Identity,
    ):
        if not await repository.owns_thread(identity.user_id, thread_id):
            raise HTTPException(404, "Thread not found")
        message_id = None
        state = request.app.state
        try:
            async with locked_turn(state, thread_id) as connection:
                await state.bookings.before_message(
                    state.graph, BookingOwner(identity.user_id, thread_id), connection, request.state.request_id,
                )
                message_id = await repository.append_message(identity.user_id, thread_id, body.content)
                if message_id is None:
                    raise HTTPException(404, "Thread not found")
                token = role_turn.set((request.state.request_id, asyncio.get_running_loop().time() + ROLE_TURN_BUDGET))
                try:
                    result = await state.graph.invoke(identity.user_id, thread_id, message_id, connection=connection)
                finally:
                    role_turn.reset(token)
        except ProposalConflict as error:
            return booking_conflict(request, error)
        except (GraphUnavailable, PostgresError):
            context = {"message_id": str(message_id)} if message_id is not None else {}
            return error_response(request, 503, "graph_unavailable", "Graph turn unavailable", **context)
        response = {"message_id": message_id, "status": result["status"]}
        if result["status"] == "rejected":
            response["reply"] = REFUSAL_MESSAGE
        for field in ("reply", "hotel_options", "faq"):
            if field in result:
                response[field] = result[field]
        return response

    @app.get("/api/me/itineraries")
    async def itineraries(repository: Database, identity: Identity):
        return await repository.list_itineraries(identity.user_id)

    return app


app = create_app()
