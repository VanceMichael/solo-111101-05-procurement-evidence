import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def build_mail(message_id, supplier, batch, payload, filename="certificate.json"):
    body = json.dumps(payload, ensure_ascii=False)
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


class CliWorkflowTests(unittest.TestCase):
    def _invoke(self, *arguments):
        return subprocess.run(
            [sys.executable, "-m", "collector_05.cli", *arguments],
            cwd=ROOT, text=True, capture_output=True, timeout=10,
        )

    def test_run_twice_in_separate_processes_is_stable_and_traceable(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            fixture_root = work / "fixtures"
            fixture_root.mkdir()
            (fixture_root / "routes.json").write_text(json.dumps({
                "/mail/one": [{"status": 200, "file": "one.eml"}],
                "/mail/revoke": [{"status": 200, "file": "revoke.eml"}],
            }))
            (fixture_root / "one.eml").write_bytes(build_mail("<one@x>", "S1", "B-001", {
                "manufacturer": "Maker-A", "certificate": "CERT-1", "issued_at": "2026-10-01",
                "receipt": "IN-9", "use": "USE-9", "accepted_at": "2026-11-01", "result": "accepted",
            }))
            (fixture_root / "revoke.eml").write_bytes(build_mail("<revoke@x>", "S1", "B-001", {
                "manufacturer": "Maker-A", "certificate": "CERT-1", "revoked_at": "2026-11-10",
            }))
            manifest = work / "scenario.json"
            manifest.write_text(json.dumps({
                "name": "cli", "run_id": "run-cli",
                "sources": ["/mail/one", "/mail/revoke"],
                "options": {"aliases": {"B-001": "B001"}, "as_of": "2026-11-12"},
                "fixture_root": str(fixture_root),
            }))
            database = work / "state.sqlite3"
            output = work / "result.json"
            command = ["--run", str(manifest), "--database", str(database), "--output", str(output)]
            first = self._invoke(*command)
            self.assertEqual(first.returncode, 0, first.stderr)
            first_result = json.loads(output.read_text())
            second = self._invoke(*command)
            self.assertEqual(second.returncode, 0, second.stderr)
            second_result = json.loads(output.read_text())
            self.assertEqual(first_result, second_result)
            self.assertEqual(first_result["notifications"], 0)
            self.assertEqual(first_result["archives"], 2)
            self.assertFalse(first_result["current_valid"])
            self.assertEqual(first_result["affected"], [{"receipt": "IN-9", "use": "USE-9"}])
            # 归档目录按摘要分片，绝不只依赖原文件名。
            archived = [p for p in Path(str(database) + ".archives").rglob("*") if p.is_file()]
            self.assertEqual(2, len(archived))
            self.assertTrue(all(len(p.name) == 64 for p in archived))

            traced = self._invoke("--database", str(database), "--trace", "IN-9")
            self.assertEqual(traced.returncode, 0, traced.stderr)
            chain = json.loads(traced.stdout)
            self.assertTrue(chain["found"])
            self.assertEqual(chain["result"], "accepted")
            self.assertEqual(len(chain["documents"]), 2)

    def test_run_without_output_is_a_usage_error(self):
        with tempfile.TemporaryDirectory() as directory:
            run = self._invoke("--run", str(Path(directory) / "x.json"),
                               "--database", str(Path(directory) / "s.sqlite3"))
            self.assertNotEqual(run.returncode, 0)
            self.assertIn("--output", run.stderr)

    def test_capture_entry_point_remains_available(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "raw.sqlite3"
            run = self._invoke("--capture", "fixtures", "--route", "/mail/one",
                               "--database", str(database))
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout)["inserted"], 1)


if __name__ == "__main__":
    unittest.main()
