"""PostgreSQL storage for user-scoped hotel preferences."""

from contextlib import asynccontextmanager
from typing import cast
from uuid import UUID

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row

from .database import DatabaseSettings, connection_kwargs, validate_schema
from .preferences import PreferencePatch, PreferenceValues


PREFERENCE_FIELDS = ("room_type", "area", "max_total_chf_minor")


class PreferenceRepository:
    def __init__(self, settings: DatabaseSettings):
        validate_schema(settings.schema)
        self.settings = settings
        self.table = sql.Identifier(settings.schema, "user_preferences")
        self.fields = sql.SQL(", ").join(map(sql.Identifier, PREFERENCE_FIELDS))

    @asynccontextmanager
    async def _connection(self, provided: AsyncConnection | None = None):
        if provided is not None:
            yield provided
            return
        async with await AsyncConnection.connect(
            **connection_kwargs(self.settings), options="-c statement_timeout=8000 -c lock_timeout=5000",
        ) as connection:
            yield connection

    async def get(self, user_id: UUID, *, connection: AsyncConnection | None = None) -> PreferenceValues:
        statement = sql.SQL("SELECT {fields} FROM {table} WHERE user_id=%s").format(
            table=self.table, fields=self.fields,
        )
        async with self._connection(connection) as active:
            async with active.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(statement, (user_id,))
                row = await cursor.fetchone()
        if row is None:
            return {"room_type": None, "area": None, "max_total_chf_minor": None}
        return cast(PreferenceValues, row)

    async def patch(
        self, user_id: UUID, changes: PreferencePatch, *, connection: AsyncConnection | None = None,
    ) -> PreferenceValues:
        if not changes or changes.keys() - set(PREFERENCE_FIELDS):
            raise ValueError("invalid preference fields")
        columns = [sql.Identifier(key) for key in changes]
        comma = sql.SQL(", ")
        statement = sql.SQL(
            "INSERT INTO {table} (user_id, {columns}) VALUES (%s, {values}) "
            "ON CONFLICT (user_id) DO UPDATE SET {assignments} RETURNING {fields}",
        ).format(
            table=self.table, columns=comma.join(columns),
            values=comma.join(sql.Placeholder() for _ in columns),
            assignments=comma.join(sql.SQL("{0} = EXCLUDED.{0}").format(column) for column in columns),
            fields=self.fields,
        )
        async with self._connection(connection) as active:
            async with active.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(statement, (user_id, *changes.values()))
                return cast(PreferenceValues, await cursor.fetchone())

    async def delete(self, user_id: UUID) -> None:
        async with self._connection() as connection:
            await connection.execute(sql.SQL("DELETE FROM {} WHERE user_id=%s").format(self.table), (user_id,))
