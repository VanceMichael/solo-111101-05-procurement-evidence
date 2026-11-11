import unittest
from pathlib import Path
from collector_05.adapter import decode


class SupplierMailTests(unittest.TestCase):
    def test_same_filename_keeps_distinct_content_hashes(self):
        root = Path(__file__).resolve().parents[1] / "fixtures"
        first = decode((root / "one.eml").read_bytes())[0]
        second = decode((root / "two.eml").read_bytes())[0]
        self.assertEqual(first["filename"], second["filename"])
        self.assertNotEqual(first["sha256"], second["sha256"])

    def test_headers_and_attachment_payload_survive(self):
        root = Path(__file__).resolve().parents[1] / "fixtures"
        row = decode((root / "one.eml").read_bytes())[0]
        self.assertEqual(row["supplier"], "S1")
        self.assertIn('"receipt": "IN-1"', row["content"])
