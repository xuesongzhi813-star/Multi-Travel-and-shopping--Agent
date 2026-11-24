"""Project-scoped MySQL storage for immutable FAQ releases and publication state."""

from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import re
from typing import Literal
from uuid import UUID

import pymysql
from pymysql.constants.ER import NO_SUCH_TABLE
from pymysql.cursors import DictCursor

from customer_support_chat.app.business.database import local_value

from .catalog import FaqVersion, parse_faq_version, validate_id


class CatalogNotFound(LookupError):
    """The requested FAQ entity does not exist."""


class CatalogConflict(ValueError):
    """The requested change conflicts with existing catalog state."""


@dataclass(frozen=True)
class MySqlFaqSettings:
    host: str
    port: int
    database: str
    user: str
    password: str = field(repr=False)

    @classmethod
    def read(cls) -> "MySqlFaqSettings":
        host = local_value("MYSQL_HOST", "127.0.0.1")
        port = int(local_value("MYSQL_PORT", "3306"))
        database, user, password = (local_value(key) for key in ("MYSQL_DATABASE", "MYSQL_USER", "MYSQL_PASSWORD"))
        if (
            host.lower() not in {"127.0.0.1", "localhost", "::1"} or not 1 <= port <= 65535
            or not database or not user or user.lower() == "root" or not password
        ):
            raise ValueError("Configure the existing local MySQL project account")
        return cls(host, port, database, user, password)


@dataclass(frozen=True)
class FaqIndexProfile:
    kind: Literal["evidence", "hybrid"]
    embedding_model: str | None = None
    dimensions: int | None = None
    tokenizer: str | None = None

    def __post_init__(self) -> None:
        fields = (self.embedding_model, self.dimensions, self.tokenizer)
        if self.kind == "evidence" and all(value is None for value in fields):
            return
        if (self.kind == "hybrid" and self.embedding_model and self.tokenizer
                and type(self.dimensions) is int and self.dimensions > 0):
            return
        raise CatalogConflict("Invalid FAQ index profile")


EVIDENCE_PROFILE = FaqIndexProfile("evidence")


@dataclass(frozen=True)
class FaqBuild:
    build_id: UUID
    version_id: str
    version_digest: str
    collection: str
    status: Literal["building", "verified", "failed"]
    point_count: int
    payload_digest: str | None
    profile: FaqIndexProfile = EVIDENCE_PROFILE


@dataclass(frozen=True)
class PublicationPointer:
    revision: int
    version_id: str | None = None
    build_id: UUID | None = None
    collection: str | None = None
    profile: FaqIndexProfile = EVIDENCE_PROFILE


class MySqlFaqCatalog:
    def __init__(self, settings: MySqlFaqSettings, *, table_prefix: str = "travel_faq"):
        if re.fullmatch(r"[a-z][a-z0-9_]{0,38}", table_prefix) is None:
            raise ValueError("Invalid FAQ table prefix")
        self.settings = settings
        self.table_prefix = table_prefix

    def _table(self, suffix: str) -> str:
        return f"`{self.table_prefix}_{suffix}`"

    def _connect(self):
        settings = self.settings
        return pymysql.connect(
            host=settings.host, port=settings.port, database=settings.database, user=settings.user,
            password=settings.password, connect_timeout=5, read_timeout=5, write_timeout=5,
            charset="utf8mb4", cursorclass=DictCursor, autocommit=False,
        )

    @contextmanager
    def _transaction(self):
        with self._connect() as connection, connection.cursor() as cursor:
            try:
                yield cursor
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def initialize(self) -> None:
        versions, documents, builds, active, history = (
            self._table(name) for name in ("versions", "documents", "builds", "active", "history")
        )
        definitions = {
            versions: "version_id VARCHAR(64) PRIMARY KEY, digest CHAR(64) NOT NULL",
            documents: (
                "version_id VARCHAR(64) NOT NULL, document_id VARCHAR(64) NOT NULL, payload JSON NOT NULL,"
                " PRIMARY KEY(version_id,document_id)"
            ),
            builds: (
                "build_id CHAR(36) PRIMARY KEY, version_id VARCHAR(64) NOT NULL, version_digest CHAR(64) NOT NULL,"
                " collection VARCHAR(128) NOT NULL UNIQUE, status VARCHAR(16) NOT NULL,"
                " point_count INT UNSIGNED NOT NULL DEFAULT 0, payload_digest CHAR(64) NULL"
            ),
            active: (
                "singleton TINYINT PRIMARY KEY, revision BIGINT UNSIGNED NOT NULL, build_id CHAR(36) NULL,"
                " CHECK(singleton=1)"
            ),
            self._table("index_profiles"): (
                "build_id CHAR(36) PRIMARY KEY, embedding_model VARCHAR(128) NOT NULL,"
                " dimensions INT UNSIGNED NOT NULL, tokenizer VARCHAR(32) NOT NULL"
            ),
            history: (
                "revision BIGINT UNSIGNED PRIMARY KEY, build_id CHAR(36) NOT NULL, rollback BOOLEAN NOT NULL,"
                " published_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),"
                " INDEX(build_id)"
            ),
        }
        with self._transaction() as cursor:
            for table, definition in definitions.items():
                cursor.execute(f"CREATE TABLE IF NOT EXISTS {table} ({definition}) ENGINE=InnoDB "
                               "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_bin")
            cursor.execute(f"INSERT INTO {active}(singleton,revision) VALUES(1,0) "
                           "ON DUPLICATE KEY UPDATE singleton=singleton")

    def _write_documents(self, cursor, version: FaqVersion) -> None:
        cursor.executemany(
            f"INSERT INTO {self._table('documents')}(version_id,document_id,payload) VALUES(%s,%s,%s)",
            [(version.version_id, item.id, json.dumps(item.to_json(), ensure_ascii=False)) for item in version.documents],
        )

    def import_version(self, version: FaqVersion) -> bool:
        verified = parse_faq_version({
            "version_id": version.version_id, "documents": [item.to_json() for item in version.documents],
        })
        if verified.digest != version.digest:
            raise CatalogConflict("Invalid FAQ digest")
        table = self._table("versions")
        with self._transaction() as cursor:
            created = cursor.execute(
                f"INSERT INTO {table}(version_id,digest) VALUES(%s,%s) ON DUPLICATE KEY UPDATE version_id=version_id",
                (verified.version_id, verified.digest),
            ) == 1
            cursor.execute(f"SELECT digest FROM {table} WHERE version_id=%s FOR UPDATE", (verified.version_id,))
            if cursor.fetchone()["digest"] != verified.digest:
                raise CatalogConflict("FAQ content differs for the existing version")
            if created:
                self._write_documents(cursor, verified)
            return created

    def read_version(self, version_id: str) -> FaqVersion:
        validate_id(version_id)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(f"SELECT digest FROM {self._table('versions')} WHERE version_id=%s", (version_id,))
            known = cursor.fetchone()
            if known is None:
                raise CatalogNotFound("FAQ version not found")
            cursor.execute(f"SELECT payload FROM {self._table('documents')} WHERE version_id=%s ORDER BY document_id",
                           (version_id,))
            version = parse_faq_version({
                "version_id": version_id, "documents": [json.loads(row["payload"]) for row in cursor.fetchall()],
            })
            if version.digest != known["digest"]:
                raise CatalogConflict("Stored FAQ version digest differs")
            return version

    def add_build(self, build: FaqBuild) -> None:
        if (
            build.status != "building" or build.point_count or build.payload_digest is not None
            or re.fullmatch(r"[a-z][a-z0-9_]{0,126}", build.collection) is None
        ):
            raise CatalogConflict("Invalid new FAQ build")
        with self._transaction() as cursor:
            result = cursor.execute(
                f"INSERT INTO {self._table('builds')}(build_id,version_id,version_digest,collection,status) "
                f"SELECT %s,version_id,digest,%s,'building' FROM {self._table('versions')} "
                "WHERE version_id=%s AND digest=%s",
                (str(build.build_id), build.collection, build.version_id, build.version_digest),
            )
            if result != 1:
                raise CatalogConflict("FAQ build does not match a stored version")
            if build.profile.kind == "hybrid":
                cursor.execute(
                    f"INSERT INTO {self._table('index_profiles')}"
                    "(build_id,embedding_model,dimensions,tokenizer) VALUES(%s,%s,%s,%s)",
                    (str(build.build_id), build.profile.embedding_model,
                     build.profile.dimensions, build.profile.tokenizer),
                )

    def finish_build(self, build_id: UUID, *, verified: bool, point_count: int, payload_digest: str | None) -> None:
        if (verified and (point_count <= 0 or not re.fullmatch(r"[0-9a-f]{64}", payload_digest or ""))) or (
            not verified and (point_count < 0 or payload_digest is not None)
        ):
            raise CatalogConflict("Invalid FAQ build verification")
        with self._transaction() as cursor:
            result = cursor.execute(
                f"UPDATE {self._table('builds')} SET status=%s,point_count=%s,payload_digest=%s "
                "WHERE build_id=%s AND status='building'",
                ("verified" if verified else "failed", point_count, payload_digest, str(build_id)),
            )
            if result != 1:
                raise CatalogConflict("FAQ build is no longer running")

    def _get_build(self, cursor, build_id: UUID) -> FaqBuild:
        row = self._read_profiled(
            cursor, "b.*", f"FROM {self._table('builds')} b", "WHERE b.build_id=%s", (str(build_id),),
        )
        if row is None:
            raise CatalogNotFound("FAQ build not found")
        return FaqBuild(**(row | {"build_id": UUID(row["build_id"])}))

    def _read_profiled(self, cursor, columns, tables, condition, params=()):
        try:
            cursor.execute(
                f"SELECT {columns},p.build_id AS profile_id,p.embedding_model,p.dimensions,p.tokenizer "
                f"{tables} LEFT JOIN {self._table('index_profiles')} p ON p.build_id=b.build_id {condition}",
                params,
            )
        except pymysql.err.ProgrammingError as error:
            if not error.args or error.args[0] != NO_SUCH_TABLE:
                raise
            cursor.execute(f"SELECT {columns} {tables} {condition}", params)
        row = cursor.fetchone()
        if row is not None and "profile_id" in row:
            profile_id = row.pop("profile_id")
            model, dimensions, tokenizer = (row.pop(key) for key in ("embedding_model", "dimensions", "tokenizer"))
            if profile_id is not None:
                row["profile"] = FaqIndexProfile("hybrid", model, dimensions, tokenizer)
        return row

    def get_build(self, build_id: UUID) -> FaqBuild:
        with self._connect() as connection, connection.cursor() as cursor:
            return self._get_build(cursor, build_id)

    def active(self) -> PublicationPointer:
        with self._connect() as connection, connection.cursor() as cursor:
            row = self._read_profiled(
                cursor, "a.revision,b.version_id,b.build_id,b.collection",
                f"FROM {self._table('active')} a LEFT JOIN {self._table('builds')} b ON a.build_id=b.build_id",
                "WHERE a.singleton=1",
            )
            if row is None:
                raise CatalogNotFound("FAQ publication is not initialized")
            return PublicationPointer(**(row | {"build_id": UUID(row["build_id"]) if row["build_id"] else None}))

    def activate(self, build_id: UUID, *, expected_revision: int, rollback: bool = False) -> PublicationPointer:
        active, history = self._table("active"), self._table("history")
        with self._transaction() as cursor:
            cursor.execute(f"SELECT revision FROM {active} WHERE singleton=1 FOR UPDATE")
            if cursor.fetchone()["revision"] != expected_revision:
                raise CatalogConflict("FAQ publication revision changed")
            build = self._get_build(cursor, build_id)
            if build.status != "verified":
                raise CatalogConflict("FAQ build is not verified")
            if rollback:
                cursor.execute(f"SELECT 1 FROM {history} WHERE build_id=%s LIMIT 1", (str(build_id),))
                if cursor.fetchone() is None:
                    raise CatalogConflict("FAQ build has not been published")
            revision = expected_revision + 1
            cursor.execute(f"UPDATE {active} SET revision=%s,build_id=%s WHERE singleton=1", (revision, str(build_id)))
            cursor.execute(f"INSERT INTO {history}(revision,build_id,rollback) VALUES(%s,%s,%s)",
                           (revision, str(build_id), rollback))
            return PublicationPointer(revision, build.version_id, build_id, build.collection, build.profile)
