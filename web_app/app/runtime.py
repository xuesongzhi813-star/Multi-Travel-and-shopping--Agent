"""Local FastAPI service for authenticated business data."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import re
from secrets import token_urlsafe
from typing import Annotated
from uuid import UUID

from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from psycopg import Error as PostgresError
from pydantic import BaseModel, ConfigDict, StringConstraints

from customer_support_chat.app.business.auth import hash_password, verify_password
from customer_support_chat.app.business.database import DatabaseSettings, create_engine, read_settings
from customer_support_chat.app.business.repository import BusinessRepository
from customer_support_chat.app.workflow.checkpoints import GraphUnavailable, open_graph_runtime, read_checkpoint_schema
from customer_support_chat.app.workflow.parent_graph import REFUSAL_MESSAGE
from customer_support_chat.app.workflow.thread_lock import try_thread_lock


COOKIE_NAME = "travel_session"
SESSION_SECONDS = 8 * 60 * 60
MAX_ACTIVE_TURNS = 4
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
POSTGRES_TEXT = r"^[^\x00]+$"


class RequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(RequestBody):
    username: Annotated[str, StringConstraints(min_length=1, max_length=80, strip_whitespace=True, pattern=POSTGRES_TEXT)]
    password: Annotated[str, StringConstraints(min_length=1, max_length=256)]


class MessageRequest(RequestBody):
    content: Annotated[str, StringConstraints(min_length=1, max_length=4000, strip_whitespace=True, pattern=POSTGRES_TEXT)]


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


def create_app(
    settings: DatabaseSettings | None = None, *, checkpoint_schema: str | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config = settings or read_settings()
        schema = checkpoint_schema if checkpoint_schema is not None else read_checkpoint_schema(config.schema)
        engine = create_engine(config)
        try:
            repository = BusinessRepository(engine, config.schema)
            app.state.repository = repository
            app.state.database_settings = config
            app.state.checkpoint_schema = schema
            app.state.active_threads = set()
            app.state.dummy_password_hash = await asyncio.to_thread(hash_password, token_urlsafe(32))
            async with open_graph_runtime(config, schema, repository.read_owned_message) as graph:
                app.state.graph = graph
                yield
        finally:
            await engine.dispose()

    app = FastAPI(title="Local travel business API", lifespan=lifespan)

    @app.middleware("http")
    async def disable_api_caching(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError):
        return JSONResponse(status_code=422, content={"detail": "Invalid request"})

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
        try:
            graph = await request.app.state.graph.read(thread_id)
        except GraphUnavailable:
            graph_unavailable = True
            graph = None
        thread = await repository.load_thread(identity.user_id, thread_id)
        if thread is None:
            raise HTTPException(404, "Thread not found")
        if graph_unavailable:
            return JSONResponse(
                status_code=503, content=jsonable_encoder(thread | {"detail": "Graph state unavailable"}),
            )
        return thread | {"graph": graph}

    @app.post("/api/threads/{thread_id}/messages", status_code=201)
    async def append_message(
        thread_id: UUID, body: MessageRequest, request: Request, repository: Database, identity: Identity,
    ):
        if not await repository.owns_thread(identity.user_id, thread_id):
            raise HTTPException(404, "Thread not found")
        message_id = None
        state = request.app.state
        active_threads = state.active_threads
        if thread_id in active_threads:
            raise HTTPException(409, "Thread is busy")
        if len(active_threads) >= MAX_ACTIVE_TURNS:
            raise HTTPException(503, "Graph turn capacity reached")
        active_threads.add(thread_id)
        try:
            async with try_thread_lock(state.database_settings, state.checkpoint_schema, thread_id) as connection:
                if connection is None:
                    raise HTTPException(409, "Thread is busy")
                message_id = await repository.append_message(identity.user_id, thread_id, body.content)
                if message_id is None:
                    raise HTTPException(404, "Thread not found")
                result = await state.graph.invoke(identity.user_id, thread_id, message_id, connection=connection)
        except (GraphUnavailable, PostgresError):
            failure = {"detail": "Graph turn unavailable"}
            if message_id is not None:
                failure["message_id"] = str(message_id)
            return JSONResponse(status_code=503, content=failure)
        finally:
            active_threads.remove(thread_id)
        response = {"message_id": message_id, "status": result["status"]}
        if result["status"] == "rejected":
            response["reply"] = REFUSAL_MESSAGE
        return response

    @app.get("/api/me/itineraries")
    async def itineraries(repository: Database, identity: Identity):
        return await repository.list_itineraries(identity.user_id)

    return app


app = create_app()
