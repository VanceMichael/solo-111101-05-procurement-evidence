import sqlite3
from pathlib import Path


class TraceStore:
    """追溯工作流的持久化状态。

    所有业务表均以自然键做唯一约束，重复导入走 INSERT OR IGNORE /
    ON CONFLICT 幂等路径；原始凭证按摘要落盘并另存 BLOB，任何输入都不
    覆盖既有归档，验收结论一旦写入即不被后续事件改写。
    """

    def __init__(self, database_path):
        self.database_path = Path(database_path)

    @property
    def archive_root(self):
        return self.database_path.with_name(self.database_path.name + ".archives")

    def connect(self):
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self):
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS trace_attachments (
                    digest TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    supplier TEXT NOT NULL,
                    source_batch TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    content BLOB NOT NULL,
                    received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE TABLE IF NOT EXISTS trace_ingestions (
                    id INTEGER PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    UNIQUE(run_id, source, message_id, digest)
                );
                CREATE TABLE IF NOT EXISTS trace_batches (
                    id INTEGER PRIMARY KEY,
                    supplier TEXT NOT NULL,
                    batch TEXT NOT NULL,
                    manufacturer TEXT,
                    UNIQUE(supplier, batch)
                );
                CREATE TABLE IF NOT EXISTS trace_documents (
                    id INTEGER PRIMARY KEY,
                    supplier TEXT NOT NULL,
                    batch TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    UNIQUE(supplier, batch, digest)
                );
                CREATE TABLE IF NOT EXISTS trace_quarantine (
                    id INTEGER PRIMARY KEY,
                    supplier TEXT NOT NULL,
                    batch TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    manufacturer TEXT NOT NULL,
                    existing_manufacturer TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT 'manufacturer_conflict',
                    quarantined_at TEXT NOT NULL,
                    UNIQUE(supplier, batch, digest)
                );
                CREATE TABLE IF NOT EXISTS trace_certificates (
                    id INTEGER PRIMARY KEY,
                    supplier TEXT NOT NULL,
                    batch TEXT NOT NULL,
                    certificate TEXT NOT NULL,
                    issued_at TEXT,
                    revoked_at TEXT,
                    UNIQUE(supplier, batch, certificate)
                );
                CREATE TABLE IF NOT EXISTS trace_decisions (
                    id INTEGER PRIMARY KEY,
                    supplier TEXT NOT NULL,
                    batch TEXT NOT NULL,
                    receipt TEXT NOT NULL,
                    use_ref TEXT,
                    accepted_at TEXT NOT NULL,
                    result TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    UNIQUE(supplier, batch, receipt)
                );
                CREATE TABLE IF NOT EXISTS trace_notifications (
                    id INTEGER PRIMARY KEY,
                    kind TEXT NOT NULL,
                    supplier TEXT NOT NULL,
                    batch TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(kind, supplier, batch, digest)
                );
                """
            )

    def write_archive(self, digest, content):
        """按摘要写盘：同名不同内容各自独立，重复内容不重复写。"""
        shard = self.archive_root / digest[:2]
        shard.mkdir(parents=True, exist_ok=True)
        target = shard / digest
        if not target.exists():
            target.write_bytes(content)
        return target
