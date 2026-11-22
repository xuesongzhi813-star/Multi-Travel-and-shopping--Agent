"""Ownership-scoped database operations for the local HTTP application."""

from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import Text, Uuid, func, literal, select
from sqlalchemy.ext.asyncio import AsyncEngine

from .schema import tables


class BusinessRepository:
    def __init__(self, engine: AsyncEngine, schema: str):
        self.engine = engine
        self.db = tables(schema)

    async def find_account(self, username: str):
        user = self.db.users
        async with self.engine.connect() as connection:
            result = await connection.execute(
                select(user.c.id, user.c.password_hash).where(user.c.username == username),
            )
            return result.one_or_none()

    async def issue_session(self, user_id: UUID, digest: str, expires_at: datetime) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(self.db.auth_sessions.insert().values(
                user_id=user_id, token_digest=digest, expires_at=expires_at,
            ))

    async def authenticated_user(self, digest: str) -> UUID | None:
        session = self.db.auth_sessions
        async with self.engine.connect() as connection:
            return await connection.scalar(
                select(session.c.user_id).where(session.c.token_digest == digest, session.c.expires_at > func.now()),
            )

    async def revoke_session(self, user_id: UUID, digest: str) -> None:
        session = self.db.auth_sessions
        async with self.engine.begin() as connection:
            await connection.execute(session.delete().where(session.c.user_id == user_id, session.c.token_digest == digest))

    async def create_thread(self, user_id: UUID) -> UUID:
        thread_id = uuid4()
        async with self.engine.begin() as connection:
            await connection.execute(self.db.threads.insert().values(id=thread_id, user_id=user_id))
        return thread_id

    async def load_thread(self, user_id: UUID, thread_id: UUID):
        thread, message = self.db.threads, self.db.messages
        query = (
            select(thread.c.id.label("thread_id"), message.c.id.label("message_id"), message.c.content)
            .select_from(thread.outerjoin(message, message.c.thread_id == thread.c.id))
            .where(thread.c.id == thread_id, thread.c.user_id == user_id)
            .order_by(message.c.created_at, message.c.id)
        )
        async with self.engine.connect() as connection:
            rows = (await connection.execute(query)).all()
        if not rows:
            return None
        return {
            "thread_id": thread_id,
            "messages": [{"message_id": row.message_id, "content": row.content} for row in rows if row.message_id is not None],
        }

    async def append_message(self, user_id: UUID, thread_id: UUID, content: str) -> UUID | None:
        thread, message = self.db.threads, self.db.messages
        source = select(literal(uuid4(), type_=Uuid()), thread.c.id, literal(content, type_=Text())).where(
            thread.c.id == thread_id, thread.c.user_id == user_id,
        )
        query = message.insert().from_select(("id", "thread_id", "content"), source).returning(message.c.id)
        async with self.engine.begin() as connection:
            return await connection.scalar(query)

    async def list_itineraries(self, user_id: UUID):
        flight = self.db.flight_itineraries
        columns = (
            flight.c.reference, flight.c.flight_number, flight.c.origin, flight.c.destination,
            flight.c.departure_at, flight.c.arrival_at, flight.c.arrival_timezone,
        )
        async with self.engine.connect() as connection:
            rows = (await connection.execute(
                select(*columns).where(flight.c.user_id == user_id).order_by(flight.c.departure_at, flight.c.reference),
            )).mappings().all()
        return [
            dict(row) | {
                "departure_at": row["departure_at"].astimezone(timezone.utc),
                "arrival_at": row["arrival_at"].astimezone(timezone.utc),
            }
            for row in rows
        ]
