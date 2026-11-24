"""Build, verify and read project-scoped Qdrant FAQ evidence collections."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
import math
import re
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx

from vectorizer.app.vectordb.chunkenizer import recursive_character_splitting

from .catalog import FaqVersion, digest_json, validate_id
from .mysql_catalog import CatalogConflict, EVIDENCE_PROFILE, FaqBuild, FaqIndexProfile, MySqlFaqCatalog, PublicationPointer
from .siliconflow import EMBEDDING_BATCH_SIZE, EMBEDDING_DIMENSIONS, EMBEDDING_MODEL, FaqProviderError


FAQ_DENSE = "dense"
FAQ_BM25 = "search_text"
FAQ_TOKENIZER = "multilingual"
HYBRID_PROFILE = FaqIndexProfile("hybrid", EMBEDDING_MODEL, EMBEDDING_DIMENSIONS, FAQ_TOKENIZER)


def _valid_dense(vector: object) -> bool:
    if not isinstance(vector, list) or len(vector) != EMBEDDING_DIMENSIONS:
        return False
    try:
        return all(type(number) in (float, int) and math.isfinite(number) for number in vector) and any(vector)
    except OverflowError:
        return False


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
        embedder: Callable[[Sequence[str]], list[list[float]]] | None = None,
    ):
        if re.fullmatch(r"[a-z][a-z0-9_]{0,38}", collection_prefix) is None:
            raise ValueError("Invalid collection prefix")
        self.catalog = catalog
        self.client = client
        self.collection_prefix = collection_prefix
        self.embedder = embedder

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
            batch = [point if "vector" in point else point | {"vector": {}} for point in points[start:start + 64]]
            result = self._call("PUT", path + "/points", params={"wait": "true"}, json={"points": batch})
            if not isinstance(result, dict) or result.get("status") != "completed":
                raise PublicationError("Evidence write did not complete")

    def _read(self, name: str, *, document_id: str | None = None, with_dense: bool = False) -> list[dict]:
        path = self._path(name) + "/points/scroll"
        body = {"limit": 16, "with_payload": True, "with_vector": [FAQ_DENSE] if with_dense else False}
        if document_id is not None:
            body["filter"] = {"must": [{"key": "document_id", "match": {"value": document_id}}]}
        points = {}
        seen_offsets = set()
        while True:
            result = self._call("POST", path, json=body)
            if not isinstance(result, dict) or not isinstance(result.get("points"), list):
                raise PublicationError("Malformed evidence result")
            for point in result["points"]:
                if not isinstance(point, dict) or not isinstance(point.get("payload"), dict):
                    raise PublicationError("Malformed evidence point")
                if with_dense:
                    vectors = point.get("vector")
                    vector = vectors.get(FAQ_DENSE) if isinstance(vectors, dict) else None
                    if not _valid_dense(vector):
                        raise PublicationError("Hybrid FAQ point is missing a usable dense vector")
                identifier = self._point_id(point.get("id"))
                if identifier in points:
                    raise PublicationError("Repeated evidence point")
                points[identifier] = {"id": identifier, "payload": point["payload"]}
            if "next_page_offset" not in result:
                raise PublicationError("Missing evidence pagination offset")
            next_page = result["next_page_offset"]
            if next_page is None:
                break
            next_page = self._point_id(next_page)
            if next_page in seen_offsets:
                raise PublicationError("Evidence pagination did not advance")
            seen_offsets.add(next_page)
            body["offset"] = next_page
        return [points[key] for key in sorted(points)]

    @staticmethod
    def _point_id(value: object) -> str:
        if isinstance(value, str):
            try:
                if str(UUID(value)) == value:
                    return value
            except ValueError as error:
                raise PublicationError("Invalid evidence point identifier") from error
        raise PublicationError("Invalid evidence point identifier")

    def _verify_sample(self, name: str, points: list[dict]) -> None:
        if not points:
            raise PublicationError("Evidence build contains no points")
        document_id = points[0]["payload"]["document_id"]
        expected = [point for point in points if point["payload"]["document_id"] == document_id]
        if digest_json(self._read(name, document_id=document_id)) != digest_json(expected):
            raise PublicationError("Filtered evidence did not match the verified build")

    @staticmethod
    def _searchable(points: list[dict]) -> list[dict]:
        return [
            point | {"payload": point["payload"] | {
                FAQ_BM25: point["payload"]["question"] + "\n" + point["payload"]["chunk_text"],
            }}
            for point in points
        ]

    def _embed(self, points: list[dict]) -> list[dict]:
        if self.embedder is None:
            raise PublicationError("Hybrid FAQ embeddings are unavailable")
        indexed = []
        for offset in range(0, len(points), EMBEDDING_BATCH_SIZE):
            batch = points[offset:offset + EMBEDDING_BATCH_SIZE]
            try:
                vectors = self.embedder([point["payload"][FAQ_BM25] for point in batch])
            except FaqProviderError:
                raise PublicationError("Hybrid FAQ embedding failed") from None
            if not isinstance(vectors, list) or len(vectors) != len(batch):
                raise PublicationError("Hybrid FAQ embedding count mismatch")
            for point, vector in zip(batch, vectors, strict=True):
                if not _valid_dense(vector):
                    raise PublicationError("Invalid hybrid FAQ vector")
                indexed.append(point | {"vector": {FAQ_DENSE: vector}})
        return indexed

    def _verify_hybrid(self, name: str, points: list[dict], profile: FaqIndexProfile) -> None:
        if profile != HYBRID_PROFILE or not points:
            raise PublicationError("Hybrid FAQ configuration mismatch")
        path = self._path(name)
        info = self._call("GET", path)
        try:
            vector = info["config"]["params"]["vectors"][FAQ_DENSE]
            text = info["payload_schema"][FAQ_BM25]["params"]
            if (vector["size"] != profile.dimensions or vector["distance"] != "Cosine"
                    or text["tokenizer"] != profile.tokenizer or text["scoring"]["type"] != "bm25"):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise PublicationError("Hybrid FAQ indexes are missing or invalid") from None
        sample = points[0]
        payload = sample["payload"]
        filters = {"must": [
            {"key": key, "match": {"value": payload[key]}}
            for key in ("document_id", "category", "language", "version_id")
        ]}
        observed = self._call("GET", path + f"/points/{sample['id']}", params={"with_vector": "true"})
        try:
            sample_vector = observed["vector"][FAQ_DENSE]
            if not isinstance(sample_vector, list) or len(sample_vector) != profile.dimensions:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise PublicationError("Hybrid FAQ sample is missing its dense vector") from None
        for query, field in ((sample_vector, FAQ_DENSE), ({"text": payload["question"]}, FAQ_BM25)):
            result = self._call("POST", path + "/points/query", json={
                "query": query, "using": field, "filter": filters, "limit": len(points), "with_payload": True,
            })
            rows = result.get("points") if isinstance(result, dict) else None
            if not isinstance(rows, list) or not any(
                isinstance(row, dict) and row.get("id") == sample["id"] and row.get("payload") == payload
                for row in rows
            ):
                raise PublicationError("Hybrid FAQ readback did not return the expected evidence")

    def build(self, version_id: str, *, hybrid: bool = False) -> FaqBuild:
        version = self.catalog.read_version(version_id)
        build_id = uuid4()
        name = f"{self.collection_prefix}_{build_id.hex}"
        profile = HYBRID_PROFILE if hybrid else EVIDENCE_PROFILE
        staged = FaqBuild(build_id, version.version_id, version.digest, name, "building", 0, None, profile)
        self.catalog.add_build(staged)
        points = self._points(version)
        try:
            path = self._path(name)
            vectors = {FAQ_DENSE: {"size": EMBEDDING_DIMENSIONS, "distance": "Cosine"}} if hybrid else {}
            self._call("PUT", path, json={"vectors": vectors})
            for field_name in ("document_id", "category", "language", "version_id"):
                self._call("PUT", path + "/index", params={"wait": "true"},
                           json={"field_name": field_name, "field_schema": "keyword"})
            if hybrid:
                self._call("PUT", path + "/index", params={"wait": "true"}, json={
                    "field_name": FAQ_BM25,
                    "field_schema": {"type": "text", "tokenizer": FAQ_TOKENIZER, "scoring": True},
                })
                points = self._searchable(points)
            self._upload(name, self._embed(points) if hybrid else points)
            observed = self._read(name, with_dense=True) if hybrid else self._read(name)
            if digest_json(observed) != digest_json(points):
                raise PublicationError("Evidence build did not match canonical FAQ")
            self._verify_sample(name, points)
            if hybrid:
                self._verify_hybrid(name, points, profile)
        except PublicationError:
            self.catalog.finish_build(build_id, verified=False, point_count=0, payload_digest=None)
            raise
        self.catalog.finish_build(build_id, verified=True, point_count=len(observed), payload_digest=digest_json(observed))
        return self.catalog.get_build(build_id)

    def activate(self, build_id: UUID, *, expected_revision: int, rollback: bool = False) -> PublicationPointer:
        build = self.catalog.get_build(build_id)
        if build.status != "verified":
            raise CatalogConflict("FAQ build is not verified")
        points = (
            self._read(build.collection, with_dense=True) if build.profile.kind == "hybrid"
            else self._read(build.collection)
        )
        if len(points) != build.point_count or digest_json(points) != build.payload_digest:
            raise PublicationError("Evidence build no longer matches verification")
        self._verify_sample(build.collection, points)
        if build.profile.kind == "hybrid":
            self._verify_hybrid(build.collection, points, build.profile)
        return self.catalog.activate(build_id, expected_revision=expected_revision, rollback=rollback)

    def evidence(self, document_id: str) -> FaqEvidence | None:
        validate_id(document_id)
        pointer = self.catalog.active()
        if pointer.build_id is None or pointer.version_id is None or pointer.collection is None:
            raise FaqNotPublished()
        version = self.catalog.read_version(pointer.version_id)
        document = next((item for item in version.documents if item.id == document_id), None)
        if document is None:
            return None
        expected = self._points(replace(version, documents=(document,)))
        if pointer.profile.kind == "hybrid":
            expected = self._searchable(expected)
        points = self._read(pointer.collection, document_id=document_id)
        if digest_json(points) != digest_json(expected):
            raise PublicationError("Evidence does not match the published document")
        chunks = tuple(sorted((point["payload"] for point in points), key=lambda item: item["chunk_index"]))
        return FaqEvidence(pointer.revision, pointer.version_id, pointer.build_id, document_id, chunks)
