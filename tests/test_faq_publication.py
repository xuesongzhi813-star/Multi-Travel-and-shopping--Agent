from dataclasses import replace
from unittest.mock import patch

import httpx

from customer_support_chat.app.business.database import local_value
from faq_extension.catalog import digest_json, parse_faq_version
from faq_extension.mysql_catalog import CatalogConflict
from faq_extension.publication import FaqNotPublished, FaqPublisher, PublicationError
from test_faq_mysql import MySqlFixture


class FaqPublicationTests(MySqlFixture):
    def setUp(self):
        super().setUp()
        port = int(local_value("QDRANT_PORT", "6333"))
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=8, trust_env=False)
        self.addCleanup(self.client.close)
        self.addCleanup(self.drop_collections)
        self.publisher = FaqPublisher(self.catalog, self.client, collection_prefix=self.prefix)

    def drop_collections(self):
        response = self.client.get("/collections")
        response.raise_for_status()
        for collection in response.json()["result"]["collections"]:
            name = collection["name"]
            if name.startswith(self.prefix + "_"):
                self.client.delete(f"/collections/{name}").raise_for_status()

    def test_publish_failed_build_keeps_old_evidence_and_rebuild_can_rollback(self):
        with self.assertRaises(FaqNotPublished):
            self.publisher.evidence("bag-report")
        self.catalog.import_version(self.release)
        first = self.publisher.build(self.release.version_id)
        a = self.publisher.activate(first.build_id, expected_revision=0)
        original = self.publisher.evidence("bag-report")
        self.assertIsNone(self.publisher.evidence("missing-document"))
        docs = [row.to_json() for row in self.release.documents]
        docs[0]["answer"] += "本轮核对补充。"
        other = parse_faq_version({"version_id": "travel-v2", "documents": docs})
        self.catalog.import_version(other)

        upload = self.publisher._upload

        def interrupted(name, points):
            upload(name, points[:1])
            raise PublicationError("Injected partial write")

        for fault in (interrupted, lambda name, points: upload(name, points[:1])):
            with patch.object(self.publisher, "_upload", side_effect=fault):
                with self.assertRaises(PublicationError):
                    self.publisher.build(other.version_id)
            self.assertEqual(self.catalog.active(), a)
            self.assertEqual(self.publisher.evidence("bag-report"), original)

        second = self.publisher.build(other.version_id)
        b = self.publisher.activate(second.build_id, expected_revision=1)
        self.assertEqual(self.publisher.evidence("bag-report").version_id, other.version_id)
        back = self.publisher.activate(first.build_id, expected_revision=b.revision, rollback=True)
        self.assertEqual(back.revision, 3)
        self.assertEqual(self.publisher.evidence("bag-report").chunks, original.chunks)
        with self.assertRaises(CatalogConflict):
            self.publisher.activate(second.build_id, expected_revision=1)

    def test_same_document_rebuild_keeps_point_ids_and_a_single_pointer_snapshot(self):
        docs = [row.to_json() for row in self.release.documents]
        docs[0]["answer"] += "补充说明。" * 180
        long_version = parse_faq_version({"version_id": "long-faq", "documents": docs})
        self.catalog.import_version(long_version)
        first = self.publisher.build(long_version.version_id)
        second = self.publisher.build(long_version.version_id)
        self.assertNotEqual(first.collection, second.collection)
        self.assertEqual(first.payload_digest, second.payload_digest)
        self.assertGreater(first.point_count, 24)
        self.assertEqual(first.payload_digest, digest_json(self.publisher._read(first.collection)))
        self.publisher.activate(second.build_id, expected_revision=0)
        with patch.object(self.catalog, "active", wraps=self.catalog.active) as active:
            evidence = self.publisher.evidence(docs[0]["id"])
            self.assertEqual(evidence.build_id, second.build_id)
            self.assertGreater(len(evidence.chunks), 1)
            active.assert_called_once_with()
