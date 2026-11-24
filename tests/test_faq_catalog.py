import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from faq_extension.catalog import load_faq_version


SOURCE = Path(__file__).resolve().parents[1] / "faq_extension/data/travel_faq_zh.json"


class FaqCatalogTests(unittest.TestCase):
    def read_modified(self, payload):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
            return load_faq_version(path)

    def test_catalog_has_24_attributed_documents(self):
        release = load_faq_version(SOURCE)
        self.assertEqual(len(release.documents), 24)
        self.assertEqual(len({item.id for item in release.documents}), 24)
        self.assertEqual({item.category for item in release.documents},
                         {"baggage", "disruptions", "changes_refunds", "lodging"})
        for document in release.documents:
            self.assertEqual(document.language, "zh-CN")
            self.assertTrue(document.source_url.startswith(("https://www.swiss.com/", "https://www.zuerich.com/")))
            self.assertTrue(document.checked_on.isoformat().startswith("2026-11-"))
            self.assertTrue(document.question and document.answer and document.applicability)

    def test_digest_is_order_independent_but_detects_content_edits(self):
        payload = json.loads(SOURCE.read_text(encoding="utf-8"))
        original = load_faq_version(SOURCE)
        payload["documents"].reverse()
        self.assertEqual(self.read_modified(payload).digest, original.digest)
        payload["documents"][0]["answer"] += "不同内容。"
        self.assertNotEqual(self.read_modified(payload).digest, original.digest)

    def test_invalid_source_or_duplicate_documents_are_rejected(self):
        original = json.loads(SOURCE.read_text(encoding="utf-8"))
        for override in ({"source_url": "javascript:alert(1)"}, {"source_url": "https://www.swiss.com@fake.test"},
                         {"source_url": "https://www.swiss.com.evil.test/"}, {"checked_on": "2026-11-31"},
                         {"category": True}, {"id": original["documents"][1]["id"]}):
            documents = [dict(item) for item in original["documents"]]
            documents[0].update(override)
            with self.subTest(override=override), self.assertRaises(ValueError):
                self.read_modified(original | {"documents": documents})
        with self.assertRaises(ValueError):
            self.read_modified('{"version_id":"base","version_id":"other","documents":[]}')
