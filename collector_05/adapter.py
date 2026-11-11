from email import policy
from email.parser import BytesParser
from hashlib import sha256


def decode(content):
    message = BytesParser(policy=policy.default).parsebytes(content)
    result = []
    for part in message.iter_attachments():
        payload = part.get_payload(decode=True) or b""
        result.append({"message_id": str(message["Message-ID"] or ""), "batch": str(message["X-Batch"] or ""), "supplier": str(message["X-Supplier"] or ""), "filename": part.get_filename(), "sha256": sha256(payload).hexdigest(), "content": payload.decode("utf-8")})
    return result
