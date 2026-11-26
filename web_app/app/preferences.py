"""Authenticated management of the current user's hotel preferences."""

from collections.abc import Awaitable, Callable
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Request, Response
from psycopg import Error as PostgresError
from pydantic import BaseModel, ConfigDict, Field

from customer_support_chat.app.business.auth import Principal
from customer_support_chat.app.business.preferences import PreferencePatch
from .operations import error_response


class PreferencePatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    room_type: Literal["standard"] | None = None
    area: Literal["airport", "city"] | None = None
    max_total_chf_minor: Annotated[int, Field(strict=True, ge=1, le=1000000)] | None = None


def preference_router(identity_dependency: Callable[..., Awaitable[Principal]]) -> APIRouter:
    router = APIRouter(prefix="/api/me/preferences")
    Identity = Annotated[Principal, Depends(identity_dependency)]

    def unavailable(request: Request):
        return error_response(request, 503, "preferences_unavailable", "Preferences unavailable")

    @router.get("")
    async def get_preferences(request: Request, identity: Identity):
        try:
            return await request.app.state.preferences.get(identity.user_id)
        except PostgresError:
            return unavailable(request)

    @router.patch("")
    async def patch_preferences(body: PreferencePatchRequest, request: Request, identity: Identity):
        if not body.model_fields_set:
            return error_response(request, 422, "invalid_request", "Invalid request")
        changes = cast(PreferencePatch, body.model_dump(exclude_unset=True))
        try:
            return await request.app.state.preferences.patch(identity.user_id, changes)
        except PostgresError:
            return unavailable(request)

    @router.delete("", status_code=204, response_class=Response)
    async def delete_preferences(request: Request, identity: Identity):
        try:
            await request.app.state.preferences.delete(identity.user_id)
        except PostgresError:
            return unavailable(request)
        return Response(status_code=204)

    return router
