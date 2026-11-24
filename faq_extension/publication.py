"""Build, verify and read project-scoped Qdrant FAQ evidence collections."""

from dataclasses import dataclass
import re
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx

from vectorizer.app.vectordb.chunkenizer import recursive_character_splitting

from .catalog import FaqVersion, digest_json, validate_id
from .mysql_catalog import CatalogConflict, FaqBuild, MySqlFaqCatalog, PublicationPointer


class PublicationError(RuntimeError):
    """Qdrant did not produce valid evidence."""


class FaqNotPublished(LookupError):
    """The default FAQ pointer is unset."""


@dataclass(frozen=True)
class FaqEvidence:
    revision: int
    version_id: str
    build_id: UUID
    document_id: str
    chunks: tuple[dict, ...]


class FaqPublisher:
    def __init__(
        self, catalog: MySqlFaqCatalog, client: httpx.Client, *, collection_prefix: str = "travel_faq",
    ):
        if re.fullmatch(r"[a-z][a-z0-9_]{0,38}", collection_prefix) is None:
            raise ValueError("Invalid collection prefix")
        self.catalog = catalog
        self.client = client
        self.collection_prefix = collection_prefix

    def _path(self, collection: str) -> str:
        if not re.fullmatch(re.escape(self.collection_prefix) + r"_[0-9a-f]{32}", collection):
            raise PublicationError("Unexpected evidence collection")
        return f"/collections/{collection}"

    def _call(self, method: str, path: str, **kwargs):
        try:
            response = self.client.request(method, path, **kwargs)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("status") != "ok" or "result" not in payload:
                raise PublicationError("Invalid evidence response")
            return payload["result"]
        except (httpx.HTTPError, ValueError) as error:
            raise PublicationError("Evidence storage unavailable") from error

    @staticmethod
    def _points(version: FaqVersion) -> list[dict]:
        result = []
        for document in version.documents:
            chunks = recursive_character_splitting(document.answer)
            metadata = document.to_json()
            metadata.pop("answer")
            for index, text in enumerate(chunks):
                identifier = uuid5(NAMESPACE_URL, f"travel-faq/{version.version_id}/{document.id}/{index}")
                result.append({
                    "id": str(identifier),
                    "payload": metadata | {
                        "document_id": document.id, "version_id": version.version_id,
                        "version_digest": version.digest, "chunk_index": index,
                        "chunk_count": len(chunks), "chunk_text": text,
                    },
                })
        return sorted(result, key=lambda point: point["id"])

    def _upload(self, name: str, points: list[dict]) -> None:
        path = self._path(name)
        for start in range(0, len(points), 64):
            batch = [point | {"vector": {}} for point in points[start:start + 64]]
            result = self._call("PUT", path + "/points", params={"wait": "true"}, json={"points": batch})
            if not isinstance(result, dict) or result.get("status") != "completed":
                raise PublicationError("Evidence write did not complete")

    def _read(self, name: str, *, document_id: str | None = None) -> list[dict]:
        path = self._path(name) + "/points/scroll"
        body = {"limit": 16, "with_payload": True, "with_vector": False}
        if document_id is not None:
            body["filter"] = {"must": [{"key": "document_id", "match": {"value": document_id}}]}
        points = {}
        seen_offsets = set()
        while True:
            result = self._call("POST", path, json=body)
            try:
                for point in result["points"]:
                    identifier, payload = point["id"], point["payload"]
                    if identifier in points or not isinstance(payload, dict):
                        raise PublicationError("Unexpected or repeated evidence payload")
                    points[identifier] = {"id": identifier, "payload": payload}
                next_page = result["next_page_offset"]
            except (KeyError, TypeError) as error:
                raise PublicationError("Malformed evidence result") from error
            if next_page is None:
                break
            if next_page in seen_offsets:
                raise PublicationError("Evidence pagination did not advance")
            seen_offsets.add(next_page)
            body["offset"] = next_page
        return [points[key] for key in sorted(points)]

    def build(self, version_id: str) -> FaqBuild:
        version = self.catalog.read_version(version_id)
        build_id = uuid4()
        name = f"{self.collection_prefix}_{build_id.hex}"
        staged = FaqBuild(build_id, version.version_id, version.digest, name, "building", 0, None)
        self.catalog.add_build(staged)
        points = self._points(version)
        try:
            path = self._path(name)
            self._call("PUT", path, json={"vectors": {}})
            for field_name in ("document_id", "category", "language", "version_id"):
                self._call("PUT", path + "/index", params={"wait": "true"},
                           json={"field_name": field_name, "field_schema": "keyword"})
            self._upload(name, points)
            observed = self._read(name)
            if observed != points:
                raise PublicationError("Evidence build did not match canonical FAQ")
        except PublicationError:
            self.catalog.finish_build(build_id, verified=False, point_count=0, payload_digest=None)
            raise
        self.catalog.finish_build(build_id, verified=True, point_count=len(observed), payload_digest=digest_json(observed))
        return self.catalog.get_build(build_id)

    def activate(self, build_id: UUID, *, expected_revision: int, rollback: bool = False) -> PublicationPointer:
        build = self.catalog.get_build(build_id)
        if build.status != "verified":
            raise CatalogConflict("FAQ build is not verified")
        points = self._read(build.collection)
        if len(points) != build.point_count or digest_json(points) != build.payload_digest:
            raise PublicationError("Evidence build no longer matches verification")
        return self.catalog.activate(build_id, expected_revision=expected_revision, rollback=rollback)

    def evidence(self, document_id: str) -> FaqEvidence | None:
        validate_id(document_id)
        pointer = self.catalog.active()
        if pointer.build_id is None or pointer.version_id is None or pointer.collection is None:
            raise FaqNotPublished()
        points = self._read(pointer.collection, document_id=document_id)
        if not points:
            return None
        try:
            chunks = tuple(sorted((item["payload"] for item in points), key=lambda item: item["chunk_index"]))
            if any(
                chunk["document_id"] != document_id or chunk["version_id"] != pointer.version_id
                or chunk["chunk_count"] != len(chunks) or chunk["chunk_index"] != index
                for index, chunk in enumerate(chunks)
            ):
                raise PublicationError("Evidence does not match the published snapshot")
        except (KeyError, TypeError) as error:
            raise PublicationError("Malformed document evidence") from error
        return FaqEvidence(pointer.revision, pointer.version_id, pointer.build_id, document_id, chunks)
