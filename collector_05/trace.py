import json
from datetime import datetime, timezone
from pathlib import Path

from .adapter import decode_with_bytes
from .sources import FixtureTransport
from .trace_store import TraceStore


def parse_instant(value):
    """解析日期或 ISO-8601 时间；仅日期按 UTC 零点处理，失败返回 None。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) == 10:
        return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _require_manifest(manifest):
    for key in ("run_id", "sources", "fixture_root"):
        if key not in manifest or manifest[key] in (None, "", []):
            raise ValueError(f"场景缺少 {key}")
    if not isinstance(manifest["sources"], list):
        raise ValueError("场景 sources 必须是数组")
    options = manifest.get("options") or {}
    if not isinstance(options, dict):
        raise ValueError("options 必须是对象")
    return options


def run_manifest(manifest_path, database, output_path, observed_at=None):
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text())
    result = run(manifest, database, observed_at=observed_at)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return result


def run(manifest, database, observed_at=None):
    options = _require_manifest(manifest)
    store = TraceStore(database)
    store.initialize()
    observed_at = observed_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    transport = FixtureTransport(manifest["fixture_root"])
    notifications = 0
    for source in manifest["sources"]:
        response = transport.request(source)
        if response.status != 200:
            raise ValueError(f"来源 {source} 状态异常: {response.status}")
        notifications += _ingest(
            store=store,
            attachments=decode_with_bytes(response.body),
            run_id=str(manifest["run_id"]),
            source=source,
            observed_at=observed_at,
            aliases=options.get("aliases") or {},
        )
    result = project(store, options.get("as_of"))
    result["notifications"] = notifications
    return result


def _ingest(store, attachments, run_id, source, observed_at, aliases):
    notifications = 0
    with store.connect() as connection:
        for attachment in attachments:
            supplier = (attachment.get("supplier") or "").strip()
            source_batch = (attachment.get("batch") or "").strip()
            message_id = (attachment.get("message_id") or "").strip()
            digest = attachment["sha256"]
            content = attachment["bytes"]
            if not supplier:
                raise ValueError(f"来源 {source} 的凭证缺少 X-Supplier，无法归批")
            if not source_batch:
                raise ValueError(f"来源 {source} 的凭证缺少 X-Batch，无法归批")
            normalized_batch = aliases.get(source_batch, source_batch)

            # 先归档原始凭证：任何业务判断都不允许丢证据，且永不覆盖。
            store.write_archive(digest, content)
            connection.execute(
                "INSERT OR IGNORE INTO trace_attachments"
                "(digest, filename, message_id, supplier, source_batch, raw_json, content)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    digest,
                    attachment.get("filename") or "",
                    message_id,
                    supplier,
                    source_batch,
                    attachment.get("content") or content.decode("utf-8"),
                    content,
                ),
            )
            connection.execute(
                "INSERT OR IGNORE INTO trace_ingestions"
                "(run_id, source, message_id, digest, observed_at) VALUES (?,?,?,?,?)",
                (run_id, source, message_id, digest, observed_at),
            )

            try:
                data = json.loads(content.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError(f"凭证 {digest} 不是合法 JSON 附件") from error
            if not isinstance(data, dict):
                raise ValueError(f"凭证 {digest} 的 JSON 必须是对象")
            manufacturer = (data.get("manufacturer") or "").strip() or None

            row = connection.execute(
                "SELECT manufacturer FROM trace_batches WHERE supplier=? AND batch=?",
                (supplier, normalized_batch),
            ).fetchone()
            existing_manufacturer = row["manufacturer"] if row else None

            conflict = bool(
                manufacturer and existing_manufacturer and manufacturer != existing_manufacturer
            )
            if conflict:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO trace_quarantine"
                    "(supplier, batch, digest, manufacturer, existing_manufacturer, reason, quarantined_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (
                        supplier,
                        normalized_batch,
                        digest,
                        manufacturer,
                        existing_manufacturer,
                        "manufacturer_conflict",
                        observed_at,
                    ),
                )
                if cursor.rowcount:
                    connection.execute(
                        "INSERT OR IGNORE INTO trace_notifications"
                        "(kind, supplier, batch, digest, created_at) VALUES (?,?,?,?,?)",
                        (
                            "manufacturer_conflict",
                            supplier,
                            normalized_batch,
                            digest,
                            observed_at,
                        ),
                    )
                notifications += cursor.rowcount
                # 冲突凭证不合并：批次、证书、验收链均不引用该摘要。
                continue

            connection.execute(
                "INSERT INTO trace_batches(supplier, batch, manufacturer) VALUES (?,?,?)"
                " ON CONFLICT(supplier, batch) DO UPDATE SET"
                " manufacturer=COALESCE(trace_batches.manufacturer, excluded.manufacturer)",
                (supplier, normalized_batch, manufacturer),
            )
            connection.execute(
                "INSERT OR IGNORE INTO trace_documents(supplier, batch, digest) VALUES (?,?,?)",
                (supplier, normalized_batch, digest),
            )

            certificate = (data.get("certificate") or "").strip()
            if certificate:
                cert_row = connection.execute(
                    "SELECT issued_at, revoked_at FROM trace_certificates"
                    " WHERE supplier=? AND batch=? AND certificate=?",
                    (supplier, normalized_batch, certificate),
                ).fetchone()
                issued_at = data.get("issued_at")
                revoked_at = data.get("revoked_at")
                if cert_row is None:
                    connection.execute(
                        "INSERT INTO trace_certificates"
                        "(supplier, batch, certificate, issued_at, revoked_at)"
                        " VALUES (?,?,?,?,?)",
                        (supplier, normalized_batch, certificate, issued_at, revoked_at),
                    )
                else:
                    # 补件只增补信息，绝不抹掉既有的签发/失效时点。
                    merged_issued = cert_row["issued_at"] or issued_at
                    merged_revoked = cert_row["revoked_at"] or revoked_at
                    connection.execute(
                        "UPDATE trace_certificates SET issued_at=?, revoked_at=?"
                        " WHERE supplier=? AND batch=? AND certificate=?",
                        (
                            merged_issued,
                            merged_revoked,
                            supplier,
                            normalized_batch,
                            certificate,
                        ),
                    )

            receipt = (data.get("receipt") or "").strip()
            if receipt:
                connection.execute(
                    "INSERT OR IGNORE INTO trace_decisions"
                    "(supplier, batch, receipt, use_ref, accepted_at, result, digest)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (
                        supplier,
                        normalized_batch,
                        receipt,
                        (data.get("use") or "").strip() or None,
                        str(data.get("accepted_at") or ""),
                        str(data.get("result") or ""),
                        digest,
                    ),
                )
    return notifications


def project(store, as_of=None):
    as_of_instant = parse_instant(as_of) if as_of is not None else datetime.now(timezone.utc)
    with store.connect() as connection:
        batches = [
            {"supplier": row["supplier"], "batch": row["batch"], "documents": row["documents"]}
            for row in connection.execute(
                "SELECT b.supplier AS supplier, b.batch AS batch,"
                " (SELECT count(*) FROM trace_documents d"
                "  WHERE d.supplier=b.supplier AND d.batch=b.batch) AS documents"
                " FROM trace_batches b ORDER BY b.supplier, b.batch"
            )
        ]
        archives = connection.execute(
            "SELECT count(*) AS total FROM trace_attachments"
        ).fetchone()["total"]
        quarantine = [
            {
                "supplier": row["supplier"],
                "batch": row["batch"],
                "digest": row["digest"],
                "manufacturer": row["manufacturer"],
                "existing_manufacturer": row["existing_manufacturer"],
                "reason": row["reason"],
                "quarantined_at": row["quarantined_at"],
            }
            for row in connection.execute(
                "SELECT supplier, batch, digest, manufacturer, existing_manufacturer, reason, quarantined_at"
                " FROM trace_quarantine ORDER BY quarantined_at, digest"
            )
        ]
        decisions = [
            {
                "receipt": row["receipt"],
                "use": row["use_ref"],
                "accepted_at": row["accepted_at"],
                "result": row["result"],
                "supplier": row["supplier"],
                "batch": row["batch"],
            }
            for row in connection.execute(
                "SELECT receipt, use_ref, accepted_at, result, supplier, batch"
                " FROM trace_decisions ORDER BY accepted_at, receipt"
            )
        ]

        active_revocations = []
        valid_certificate_seen = False
        for cert in connection.execute(
            "SELECT supplier, batch, certificate, issued_at, revoked_at FROM trace_certificates"
        ):
            revoked_instant = parse_instant(cert["revoked_at"])
            is_revoked = revoked_instant is not None and revoked_instant <= as_of_instant
            issued_instant = parse_instant(cert["issued_at"])
            is_issued = issued_instant is None or issued_instant <= as_of_instant
            if is_revoked:
                active_revocations.append((cert["supplier"], cert["batch"]))
            elif is_issued:
                valid_certificate_seen = True

        affected = []
        if active_revocations:
            clauses = " OR ".join("(supplier=? AND batch=?)" for _ in active_revocations)
            values = [value for pair in active_revocations for value in pair]
            for row in connection.execute(
                f"SELECT DISTINCT receipt, use_ref FROM trace_decisions"
                f" WHERE {clauses} ORDER BY receipt",
                values,
            ):
                affected.append(
                    {"receipt": row["receipt"], "use": row["use_ref"]}
                )

        has_certificates = connection.execute(
            "SELECT 1 FROM trace_certificates LIMIT 1"
        ).fetchone() is not None
        current_valid = has_certificates and valid_certificate_seen

    return {
        "batches": batches,
        "archives": archives,
        "quarantine": quarantine,
        "affected": affected,
        "decisions": decisions,
        "current_valid": current_valid,
    }


def trace_receipt(store, receipt):
    """从入库单号反查验收事实与当时依据的全部凭证。"""
    with store.connect() as connection:
        decision = connection.execute(
            "SELECT supplier, batch, receipt, use_ref, accepted_at, result, digest"
            " FROM trace_decisions WHERE receipt=?",
            (receipt,),
        ).fetchone()
        if decision is None:
            return {"receipt": receipt, "found": False}
        documents = [
            {"digest": row["digest"], "filename": row["filename"], "message_id": row["message_id"]}
            for row in connection.execute(
                "SELECT a.digest AS digest, a.filename AS filename, a.message_id AS message_id"
                " FROM trace_documents d JOIN trace_attachments a ON a.digest=d.digest"
                " WHERE d.supplier=? AND d.batch=? ORDER BY a.digest",
                (decision["supplier"], decision["batch"]),
            )
        ]
        certificates = [
            dict(row)
            for row in connection.execute(
                "SELECT certificate, issued_at, revoked_at FROM trace_certificates"
                " WHERE supplier=? AND batch=? ORDER BY certificate",
                (decision["supplier"], decision["batch"]),
            )
        ]
        return {
            "receipt": receipt,
            "found": True,
            "supplier": decision["supplier"],
            "batch": decision["batch"],
            "use": decision["use_ref"],
            "accepted_at": decision["accepted_at"],
            "result": decision["result"],
            "decision_digest": decision["digest"],
            "documents": documents,
            "certificates": certificates,
        }
