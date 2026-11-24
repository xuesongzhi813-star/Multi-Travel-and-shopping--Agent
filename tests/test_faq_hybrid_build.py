import httpx

from customer_support_chat.app.business.database import local_value
from faq_extension.mysql_catalog import FaqIndexProfile
from faq_extension.publication import FaqPublisher, PublicationError
from faq_extension.siliconflow import EMBEDDING_DIMENSIONS, EMBEDDING_MODEL
from scripts.manage_faq import parser
from test_faq_mysql import MySqlFixture


def vectors(texts):
    return [[1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1) for _ in texts]


class FaqHybridBuildTests(MySqlFixture):
    def setUp(self):
        super().setUp()
        port = int(local_value("QDRANT_PORT", "6333"))
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=8, trust_env=False)
        self.addCleanup(self.client.close)
        self.addCleanup(self.drop_collections)
        self.publisher = FaqPublisher(
            self.catalog, self.client, collection_prefix=self.prefix, embedder=vectors,
        )
        self.catalog.import_version(self.release)

    def drop_collections(self):
        collections = self.client.get("/collections").raise_for_status().json()["result"]["collections"]
        for collection in collections:
            name = collection["name"]
            if name.startswith(self.prefix + "_"):
                self.client.delete(f"/collections/{name}").raise_for_status()

    def test_legacy_catalog_resolves_evidence_without_profile_table(self):
        with self.catalog._connect() as connection, connection.cursor() as cursor:
            cursor.execute(f"DROP TABLE IF EXISTS {self.catalog._table('index_profiles')}")
        build = self.publisher.build(self.release.version_id)
        pointer = self.publisher.activate(build.build_id, expected_revision=0)
        self.assertEqual(pointer.profile, FaqIndexProfile(kind="evidence"))
        self.assertEqual(self.catalog.active(), pointer)
        self.assertIsNotNone(self.publisher.evidence("bag-report"))

    def test_hybrid_build_validates_dense_native_bm25_and_filtered_readback(self):
        self.assertTrue(parser().parse_args(["build", self.release.version_id, "--hybrid"]).hybrid)
        build = self.publisher.build(self.release.version_id, hybrid=True)
        self.assertEqual(build.profile, FaqIndexProfile("hybrid", EMBEDDING_MODEL, 1024, "multilingual"))
        pointer = self.publisher.activate(build.build_id, expected_revision=0)
        self.assertEqual(self.catalog.active(), pointer)
        self.assertEqual(pointer.profile, build.profile)
        path = f"/collections/{build.collection}/points/query"
        filters = {"must": [
            {"key": key, "match": {"value": value}}
            for key, value in (("version_id", build.version_id), ("category", "baggage"), ("language", "zh-CN"))
        ]}
        for query, field in (({"text": "行李延误"}, "search_text"), (vectors(["行李"])[0], "dense")):
            response = self.client.post(path, json={
                "query": query, "using": field, "filter": filters, "limit": 10, "with_payload": True,
            }).raise_for_status().json()["result"]["points"]
            self.assertTrue(response, field)
            self.assertTrue(all(point["payload"]["category"] == "baggage" for point in response))
        self.assertEqual(self.publisher.evidence("bag-report").build_id, build.build_id)

    def test_failed_hybrid_build_keeps_active_p8_and_rollback_works(self):
        original = self.publisher.build(self.release.version_id)
        a = self.publisher.activate(original.build_id, expected_revision=0)

        def invalid_embedding(texts):
            return [[1.0] for _ in texts]

        bad_publisher = FaqPublisher(
            self.catalog, self.client, collection_prefix=self.prefix, embedder=invalid_embedding,
        )
        with self.assertRaises(PublicationError):
            bad_publisher.build(self.release.version_id, hybrid=True)
        self.assertEqual(self.catalog.active(), a)
        self.assertEqual(self.publisher.evidence("bag-report").build_id, a.build_id)
        hybrid = self.publisher.build(self.release.version_id, hybrid=True)
        self.publisher.activate(hybrid.build_id, expected_revision=1)
        back = self.publisher.activate(original.build_id, expected_revision=2, rollback=True)
        self.assertEqual(back.revision, 3)
        self.assertEqual(self.catalog.active().profile.kind, "evidence")

    def test_activation_revalidates_hybrid_profile(self):
        build = self.publisher.build(self.release.version_id, hybrid=True)
        self.client.delete(
            f"/collections/{build.collection}/index/search_text", params={"wait": "true"},
        ).raise_for_status()
        with self.assertRaises(PublicationError):
            self.publisher.activate(build.build_id, expected_revision=0)
        self.assertEqual(self.catalog.active().revision, 0)
