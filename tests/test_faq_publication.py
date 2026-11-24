from contextlib import nullcontext, redirect_stdout
import io
import json
from unittest.mock import patch

import httpx

from customer_support_chat.app.business.database import local_value
from faq_extension.catalog import digest_json, parse_faq_version
from faq_extension.mysql_catalog import CatalogConflict
from faq_extension.publication import FaqNotPublished, FaqPublisher, PublicationError
from scripts import manage_faq
from test_faq_mysql import MySqlFixture, SOURCE


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

    def test_cli_dispatch_on_real_services_and_fail_closed(self):
        def cli(*args, code=0):
            stdout = io.StringIO()
            with (
                patch.object(manage_faq.MySqlFaqSettings, "read", return_value=self.catalog.settings),
                patch.object(manage_faq, "MySqlFaqCatalog", return_value=self.catalog),
                patch.object(manage_faq, "FaqPublisher", return_value=self.publisher),
                patch.object(manage_faq.httpx, "Client", return_value=nullcontext(self.client)),
                redirect_stdout(stdout),
            ):
                self.assertEqual(manage_faq.main(list(args)), code)
            return json.loads(stdout.getvalue())

        self.assertEqual(cli("init")["status"], "initialized")
        self.assertEqual(cli("evidence", "bag-report")["status"], "not_published")
        first = cli("import", str(SOURCE))
        self.assertEqual(first["document_count"], 24)
        a = cli("build", first["version_id"])
        cli("activate", a["build_id"], "--expected-revision", "0")
        self.assertEqual(cli("evidence", "bag-report")["version_id"], first["version_id"])
        with patch.object(self.publisher, "_upload", side_effect=PublicationError("Injected CLI build error")):
            self.assertEqual(cli("build", first["version_id"], code=1)["status"], "storage_unavailable")
        self.assertEqual(cli("status")["build_id"], a["build_id"])
        b = cli("build", first["version_id"])
        cli("activate", b["build_id"], "--expected-revision", "1")
        self.assertEqual(cli("rollback", a["build_id"], "--expected-revision", "2")["revision"], 3)
        self.assertEqual(cli("evidence", "bag-report")["build_id"], a["build_id"])

    def test_known_document_with_changed_or_missing_points_is_storage_error(self):
        self.catalog.import_version(self.release)
        build = self.publisher.build(self.release.version_id)
        self.publisher.activate(build.build_id, expected_revision=0)
        [point] = self.publisher._read(build.collection, document_id="bag-report")
        base = f"/collections/{build.collection}/points"
        response = self.client.post(base + "/payload", params={"wait": "true"},
                                    json={"points": [point["id"]], "payload": {"chunk_text": "injected text"}})
        response.raise_for_status()
        with self.assertRaises(PublicationError):
            self.publisher.evidence("bag-report")
        response = self.client.post(base + "/delete", params={"wait": "true"}, json={"points": [point["id"]]})
        response.raise_for_status()
        with self.assertRaises(PublicationError):
            self.publisher.evidence("bag-report")
        self.assertIsNone(self.publisher.evidence("unknown-id"))

    def test_malformed_scroll_offsets_and_point_ids_are_storage_errors(self):
        malformed = (
            {"points": [], "next_page_offset": []},
            {"points": [{"id": 1, "payload": {}}], "next_page_offset": None},
            {"points": [{"id": "bad-uuid", "payload": {}}], "next_page_offset": None},
        )
        for body in malformed:
            with self.subTest(body=body), patch.object(self.publisher, "_call", return_value=body):
                with self.assertRaises(PublicationError):
                    self.publisher._read(self.prefix + "_" + "1" * 32)

    def test_filtered_readback_must_pass_before_build_and_activation(self):
        self.catalog.import_version(self.release)
        first = self.publisher.build(self.release.version_id)
        original = self.publisher.activate(first.build_id, expected_revision=0)
        second = self.publisher.build(self.release.version_id)
        read = self.publisher._read

        def broken_filter(name, *, document_id=None):
            return read(name) if document_id is None else []

        with patch.object(self.publisher, "_read", side_effect=broken_filter):
            with self.assertRaises(PublicationError):
                self.publisher.build(self.release.version_id)
            with self.assertRaises(PublicationError):
                self.publisher.activate(second.build_id, expected_revision=1)
        self.assertEqual(self.catalog.active(), original)

    def test_cli_usage_error_outputs_a_json_status(self):
        for args in (("activate", "wrong-id", "--expected-revision", "0"), ("rollback",)):
            stdout = io.StringIO()
            with self.subTest(args=args):
                with redirect_stdout(stdout), self.assertRaises(SystemExit) as result:
                    manage_faq.main(args)
                self.assertEqual(result.exception.code, 2)
                self.assertEqual(json.loads(stdout.getvalue()), {"status": "invalid_request_or_conflict"})
