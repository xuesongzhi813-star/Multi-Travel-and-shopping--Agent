from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from faq_extension.catalog import load_faq_version, parse_faq_version
from faq_extension.mysql_catalog import (
    CatalogConflict, CatalogNotFound, FaqBuild, MySqlFaqCatalog, MySqlFaqSettings,
)


SOURCE = Path(__file__).resolve().parents[1] / "faq_extension/data/travel_faq_zh.json"


class MySqlFixture(unittest.TestCase):
    def setUp(self):
        self.prefix = f"test_faq_{uuid4().hex[:16]}"
        self.catalog = MySqlFaqCatalog(MySqlFaqSettings.read(), table_prefix=self.prefix)
        self.addCleanup(self.drop_tables)
        self.catalog.initialize()
        self.release = load_faq_version(SOURCE)

    def drop_tables(self):
        names = ("history", "active", "index_profiles", "builds", "documents", "versions")
        with self.catalog._connect() as connection, connection.cursor() as cursor:
            for name in names:
                cursor.execute(f"DROP TABLE IF EXISTS `{self.prefix}_{name}`")

    def build(self, release, *, verified=True):
        self.catalog.import_version(release)
        build_id = uuid4()
        build = FaqBuild(build_id, release.version_id, release.digest, f"{self.prefix}_{build_id.hex}", "building", 0, None)
        self.catalog.add_build(build)
        self.catalog.finish_build(build_id, verified=verified, point_count=len(release.documents),
                                  payload_digest="a" * 64 if verified else None)
        return self.catalog.get_build(build_id)


class FaqMySqlTests(MySqlFixture):
    def test_import_is_atomic_and_same_version_cannot_change(self):
        original = self.catalog._write_documents

        def interrupted(cursor, version):
            original(cursor, replace(version, documents=version.documents[:1]))
            raise RuntimeError("test failure during import")

        with patch.object(self.catalog, "_write_documents", side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, "during import"):
                self.catalog.import_version(self.release)
        with self.assertRaises(CatalogNotFound):
            self.catalog.read_version(self.release.version_id)
        self.assertTrue(self.catalog.import_version(self.release))
        self.assertFalse(self.catalog.import_version(self.release))
        self.assertEqual(self.catalog.read_version(self.release.version_id), self.release)
        content = [document.to_json() for document in self.release.documents]
        content[0]["answer"] += "更新。"
        changed = parse_faq_version({"version_id": self.release.version_id, "documents": content})
        with self.assertRaises(CatalogConflict):
            self.catalog.import_version(changed)
        self.assertEqual(self.catalog.read_version(self.release.version_id), self.release)

    def test_cas_covers_initial_publication_and_rollback_aba(self):
        a = self.build(self.release)
        other = replace(self.release, version_id="another-version")
        b = self.build(other)
        failed = self.build(other, verified=False)
        self.assertEqual(self.catalog.active().revision, 0)
        for target, rollback in ((failed, False), (a, True)):
            with self.assertRaises(CatalogConflict):
                self.catalog.activate(target.build_id, expected_revision=0, rollback=rollback)

        def activate(build):
            try:
                return self.catalog.activate(build.build_id, expected_revision=0)
            except CatalogConflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(activate, (a, b)))
        current, = [result for result in outcomes if result is not None]
        self.assertEqual(current.revision, 1)
        next_build = b if current.build_id == a.build_id else a
        self.assertEqual(self.catalog.activate(next_build.build_id, expected_revision=1).revision, 2)
        self.assertEqual(self.catalog.activate(current.build_id, expected_revision=2, rollback=True).revision, 3)
        with self.assertRaises(CatalogConflict):
            self.catalog.activate(next_build.build_id, expected_revision=1)
        self.assertEqual(self.catalog.active().build_id, current.build_id)
        with self.catalog._connect() as connection, connection.cursor() as cursor:
            cursor.execute(f"SELECT revision FROM `{self.prefix}_history` ORDER BY revision")
            self.assertEqual([row["revision"] for row in cursor.fetchall()], [1, 2, 3])
