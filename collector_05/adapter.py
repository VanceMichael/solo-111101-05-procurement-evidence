from email import policy
from email.parser import BytesParser
from hashlib import sha256


def _iter_attachment_rows(content, include_bytes=False):
    message = BytesParser(policy=policy.default).parsebytes(content)
    result = []
    for part in message.iter_attachments():
        payload = part.get_payload(decode=True) or b""
        row = {
            "message_id": str(message["Message-ID"] or ""),
            "batch": str(message["X-Batch"] or ""),
            "supplier": str(message["X-Supplier"] or ""),
            "filename": part.get_filename(),
            "sha256": sha256(payload).hexdigest(),
            "content": payload.decode("utf-8"),
        }
        if include_bytes:
            row["bytes"] = payload
        result.append(row)
    return result


def decode(content):
    return _iter_attachment_rows(content)


def decode_with_bytes(content):
    """追溯工作流使用：在解码记录上保留原始字节以便不可变归档。"""
    return _iter_attachment_rows(content, include_bytes=True)
