import json
import tempfile
import unittest
from pathlib import Path

from collector_05.trace import run, trace_receipt
from collector_05.trace_store import TraceStore


def build_mail(message_id, supplier, batch, payload, filename="certificate.json"):
    if isinstance(payload, (dict, list)):
        body = json.dumps(payload, ensure_ascii=False)
    else:
        body = payload
    boundary = "synthetic-boundary"
    return (
        "MIME-Version: 1.0\r\n"
        f"Message-ID: {message_id}\r\n"
        f"X-Supplier: {supplier}\r\n"
        f"X-Batch: {batch}\r\n"
        f"Content-Type: multipart/mixed; boundary=\"{boundary}\"\r\n\r\n"
        f"--{boundary}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\n"
        "Synthetic supplier export.\r\n"
        f"--{boundary}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Disposition: attachment; filename=\"{filename}\"\r\n\r\n"
        f"{body}\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")


def make_fixture(root, route_to_mails):
    root = Path(root)
    routes = {}
    for route, mails in route_to_mails.items():
        entries = []
        for index, mail in enumerate(mails):
            name = f"{route.strip('/').replace('/', '_')}_{index}.eml"
            (root / name).write_bytes(mail)
            entries.append({"status": 200, "file": name})
        routes[route] = entries
    (root / "routes.json").write_text(json.dumps(routes))


def manifest(root, sources, options, run_id="run-test"):
    return {
        "name": "synthetic",
        "run_id": run_id,
        "sources": sources,
        "options": options,
        "fixture_root": str(root),
    }


ACCEPTED = {
    "manufacturer": "Maker-A",
    "certificate": "CERT-1",
    "issued_at": "2026-10-01",
    "receipt": "IN-1",
    "use": "USE-1",
    "accepted_at": "2026-11-01",
    "result": "accepted",
}


class TraceWorkflowTests(unittest.TestCase):
    def test_alias_normalization_groups_batches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "fixtures"
            root.mkdir()
            make_fixture(root, {
                "/mail/a": [build_mail("<a@x>", "S1", "B-001", ACCEPTED)],
                "/mail/b": [build_mail("<b@x>", "S1", "B001", {
                    "manufacturer": "Maker-A", "certificate": "CERT-1", "issued_at": "2026-10-02"})],
            })
            db = Path(directory) / "state.sqlite3"
            result = run(manifest(root, ["/mail/a", "/mail/b"], {"aliases": {"B-001": "B001"}}), db)
            self.assertEqual(result["batches"], [{"supplier": "S1", "batch": "B001", "documents": 2}])
            self.assertEqual(result["archives"], 2)

    def test_unknown_batch_keeps_original_identifier(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "fixtures"
            root.mkdir()
            make_fixture(root, {"/mail/a": [build_mail("<a@x>", "S9", "RAW-7", ACCEPTED)]})
            db = Path(directory) / "state.sqlite3"
            result = run(manifest(root, ["/mail/a"], {"aliases": {}}), db)
            self.assertEqual(result["batches"], [{"supplier": "S9", "batch": "RAW-7", "documents": 1}])

    def test_missing_supplier_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "fixtures"
            root.mkdir()
            make_fixture(root, {"/mail/a": [build_mail("<a@x>", "", "B001", ACCEPTED)]})
            db = Path(directory) / "state.sqlite3"
            with self.assertRaisesRegex(ValueError, "X-Supplier"):
                run(manifest(root, ["/mail/a"], {}), db)

    def test_manufacturer_conflict_quarantines_without_merging_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "fixtures"
            root.mkdir()
            make_fixture(root, {
                "/mail/a": [build_mail("<a@x>", "S1", "B001", ACCEPTED)],
                "/mail/b": [build_mail("<b@x>", "S1", "B001", {"manufacturer": "Maker-B", "certificate": "CERT-X"})],
            })
            db = Path(directory) / "state.sqlite3"
            result = run(manifest(root, ["/mail/a", "/mail/b"], {"aliases": {"B-001": "B001"}}), db)
            self.assertEqual(1, len(result["quarantine"]))
            self.assertEqual("manufacturer_conflict", result["quarantine"][0]["reason"])
            self.assertEqual(result["batches"], [{"supplier": "S1", "batch": "B001", "documents": 1}])
            # 冲突凭证仍然不可丢失地归档，但不合并进批次凭证。
            self.assertEqual(result["archives"], 2)
            self.assertEqual(result["notifications"], 1)
            # 冲突邮件里的证书与厂商不得污染原批次。
            with TraceStore(db).connect() as connection:
                certificates = [row["certificate"] for row in connection.execute(
                    "SELECT certificate FROM trace_certificates WHERE batch='B001'")]
            self.assertEqual(certificates, ["CERT-1"])

    def test_same_filename_different_digest_both_archived(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "fixtures"
            root.mkdir()
            first = dict(ACCEPTED, issued_at="2026-10-01")
            second = dict(ACCEPTED, issued_at="2026-10-03")
            make_fixture(root, {
                "/mail/a": [build_mail("<a@x>", "S1", "B001", first)],
                "/mail/b": [build_mail("<b@x>", "S1", "B001", second)],
            })
            db = Path(directory) / "state.sqlite3"
            run(manifest(root, ["/mail/a", "/mail/b"], {}), db)
            store = TraceStore(db)
            files = [p for p in store.archive_root.rglob("*") if p.is_file()]
            self.assertEqual(2, len(files))
            for path in files:
                # 归档文件名是内容摘要，不依赖原始文件名 certificate.json。
                self.assertNotEqual(path.name, "certificate.json")
                self.assertEqual(len(path.name), 64)
                int(path.name, 16)

    def test_identical_digest_dedupicates_even_across_repeated_mails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "fixtures"
            root.mkdir()
            mail = build_mail("<a@x>", "S1", "B001", ACCEPTED)
            make_fixture(root, {"/mail/a": [mail, mail, mail]})
            db = Path(directory) / "state.sqlite3"
            scenario = manifest(root, ["/mail/a"], {})
            first = run(scenario, db)
            second = run(scenario, db)
            self.assertEqual(first["archives"], 1)
            self.assertEqual(second["archives"], 1)
            self.assertEqual(first["batches"][0]["documents"], 1)
            self.assertEqual(second["notifications"], 0)
            self.assertEqual(first["affected"], second["affected"])

    def test_same_message_id_changed_content_keeps_both_archives(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "fixtures"
            root.mkdir()
            old = build_mail("<same@x>", "S1", "B001", ACCEPTED)
            new = build_mail("<same@x>", "S1", "B001", dict(ACCEPTED, issued_at="2026-10-09"))
            make_fixture(root, {"/mail/a": [old], "/mail/b": [new]})
            db = Path(directory) / "state.sqlite3"
            result = run(manifest(root, ["/mail/a", "/mail/b"], {}), db)
            self.assertEqual(result["archives"], 2)
            self.assertEqual(result["batches"][0]["documents"], 2)
            store = TraceStore(db)
            with store.connect() as connection:
                message_ids = [row["message_id"] for row in connection.execute(
                    "SELECT DISTINCT message_id FROM trace_attachments")]
            self.assertEqual(message_ids, ["<same@x>"])

    def test_revocation_is_an_event_not_a_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "fixtures"
            root.mkdir()
            make_fixture(root, {
                "/mail/a": [build_mail("<a@x>", "S1", "B001", ACCEPTED)],
                "/mail/b": [build_mail("<b@x>", "S1", "B001", {
                    "manufacturer": "Maker-A", "certificate": "CERT-1", "revoked_at": "2026-11-10"})],
            })
            db = Path(directory) / "state.sqlite3"
            run(manifest(root, ["/mail/a", "/mail/b"], {"as_of": "2026-11-12"}), db)
            store = TraceStore(db)
            # 归档仍为两份，失效邮件没有删除任何文件。
            self.assertEqual(2, len([p for p in store.archive_root.rglob("*") if p.is_file()]))
            result = run(manifest(root, ["/mail/a", "/mail/b"], {"as_of": "2026-11-12"}), db)
            self.assertFalse(result["current_valid"])
            self.assertEqual(result["affected"], [{"receipt": "IN-1", "use": "USE-1"}])
            # 历史验收事实保持原样，没有被改写成拒收。
            self.assertEqual(result["decisions"], [{
                "receipt": "IN-1", "use": "USE-1", "accepted_at": "2026-11-01",
                "result": "accepted", "supplier": "S1", "batch": "B001",
            }])

    def test_historical_as_of_ignores_future_revocation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "fixtures"
            root.mkdir()
            make_fixture(root, {
                "/mail/a": [build_mail("<a@x>", "S1", "B001", ACCEPTED)],
                "/mail/b": [build_mail("<b@x>", "S1", "B001", {
                    "manufacturer": "Maker-A", "certificate": "CERT-1", "revoked_at": "2026-11-10"})],
            })
            db = Path(directory) / "state.sqlite3"
            scenario = manifest(root, ["/mail/a", "/mail/b"], {"as_of": "2026-11-02"})
            result = run(scenario, db)
            self.assertTrue(result["current_valid"])
            self.assertEqual(result["affected"], [])

    def test_supplemental_document_after_acceptance_is_added_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "fixtures"
            root.mkdir()
            make_fixture(root, {
                "/mail/a": [build_mail("<a@x>", "S1", "B001", ACCEPTED)],
                "/mail/b": [build_mail("<b@x>", "S1", "B001", {
                    "manufacturer": "Maker-A", "certificate": "CERT-1",
                    "issued_at": "2026-10-01", "supplement": "inspection-2026-11-05"})],
            })
            db = Path(directory) / "state.sqlite3"
            result = run(manifest(root, ["/mail/a", "/mail/b"], {}), db)
            self.assertEqual(result["batches"][0]["documents"], 2)
            self.assertEqual(result["archives"], 2)
            # 原始验收结论不变。
            self.assertEqual(result["decisions"][0]["accepted_at"], "2026-11-01")
            self.assertEqual(result["decisions"][0]["result"], "accepted")

    def test_current_valid_false_when_no_known_certificate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "fixtures"
            root.mkdir()
            make_fixture(root, {"/mail/a": [build_mail("<a@x>", "S1", "B001", {
                "manufacturer": "Maker-A", "receipt": "IN-2", "accepted_at": "2026-11-03", "result": "accepted"})]})
            db = Path(directory) / "state.sqlite3"
            result = run(manifest(root, ["/mail/a"], {}), db)
            self.assertFalse(result["current_valid"])

    def test_trace_receipt_reconstructs_evidence_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "fixtures"
            root.mkdir()
            make_fixture(root, {
                "/mail/a": [build_mail("<a@x>", "S1", "B001", ACCEPTED)],
                "/mail/b": [build_mail("<b@x>", "S1", "B001", {
                    "manufacturer": "Maker-A", "certificate": "CERT-1", "revoked_at": "2026-11-10"})],
            })
            db = Path(directory) / "state.sqlite3"
            run(manifest(root, ["/mail/a", "/mail/b"], {"as_of": "2026-11-12"}), db)
            traced = trace_receipt(TraceStore(db), "IN-1")
            self.assertTrue(traced["found"])
            self.assertEqual(traced["result"], "accepted")
            self.assertEqual(len(traced["documents"]), 2)
            self.assertEqual(traced["certificates"][0]["revoked_at"], "2026-11-10")
            self.assertFalse(trace_receipt(TraceStore(db), "missing")["found"])

    def test_malformed_attachment_is_rejected_after_archiving(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "fixtures"
            root.mkdir()
            make_fixture(root, {"/mail/a": [build_mail("<a@x>", "S1", "B001", "not-json{")]})
            db = Path(directory) / "state.sqlite3"
            with self.assertRaisesRegex(ValueError, "合法 JSON"):
                run(manifest(root, ["/mail/a"], {}), db)
            # 即使业务解析失败，原始凭证已经落盘，不会静默丢失。
            store = TraceStore(db)
            self.assertEqual(1, len([p for p in store.archive_root.rglob("*") if p.is_file()]))

    def test_supplier_participates_in_batch_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "fixtures"
            root.mkdir()
            make_fixture(root, {
                "/mail/a": [build_mail("<a@x>", "S1", "B001", ACCEPTED)],
                "/mail/b": [build_mail("<b@x>", "S2", "B001", dict(ACCEPTED, receipt="IN-2", use="USE-2"))],
            })
            db = Path(directory) / "state.sqlite3"
            result = run(manifest(root, ["/mail/a", "/mail/b"], {}), db)
            self.assertEqual(
                sorted((row["supplier"], row["batch"]) for row in result["batches"]),
                [("S1", "B001"), ("S2", "B001")],
            )
            self.assertEqual(result["archives"], 2)
            self.assertEqual(result["quarantine"], [])

    def test_repeated_full_run_is_idempotent_across_notifications_and_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "fixtures"
            root.mkdir()
            make_fixture(root, {
                "/mail/a": [build_mail("<a@x>", "S1", "B001", ACCEPTED)],
                "/mail/b": [build_mail("<b@x>", "S1", "B001", {
                    "manufacturer": "Maker-B", "certificate": "CERT-X"})],
                "/mail/c": [build_mail("<c@x>", "S1", "B001", {
                    "manufacturer": "Maker-A", "certificate": "CERT-1", "revoked_at": "2026-11-10"})],
            })
            db = Path(directory) / "state.sqlite3"
            scenario = manifest(root, ["/mail/a", "/mail/b", "/mail/c"], {"as_of": "2026-11-12"})
            first = run(scenario, db)
            second = run(scenario, db)
            self.assertEqual(first["notifications"], 1)
            self.assertEqual(second["notifications"], 0)
            for key in ("batches", "archives", "quarantine", "affected", "decisions", "current_valid"):
                self.assertEqual(first[key], second[key])
            self.assertEqual(1, len(first["quarantine"]))
            self.assertEqual(first["affected"], [{"receipt": "IN-1", "use": "USE-1"}])


if __name__ == "__main__":
    unittest.main()
