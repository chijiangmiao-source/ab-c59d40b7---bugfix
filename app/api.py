"""HTTP API: submit one Base64 class file, receive verification report.

Endpoints
---------
GET  /                 review page (single Base64 textarea, <= 64 KiB)
GET  /healthz          liveness response
POST /api/verify       JSON {"class_base64": ..., "method": "..."}

Every rejection is reported with its stable byte offset and never raises
out of the handler: truncation and malformed input clear any previous
conclusion because each request starts from a fresh parse/verify run.
"""

import base64
import binascii
import json

from .classfile import ClassFile
from .errors import ClassFormatError, VerifyError
from .verifier import verify_method

MAX_CLASS_BYTES = 64 * 1024
MAX_BASE64_CHARS = ((MAX_CLASS_BYTES + 2) // 3) * 4 + 4


def run_review(class_b64, method):
    """Pure entry point used by both the HTTP layer and the tests."""
    if not isinstance(class_b64, str):
        return _reject(0, "request field class_base64 must be a string")
    compact = "".join(class_b64.split())
    if not compact:
        return _reject(0, "empty class payload")
    if len(compact) > MAX_BASE64_CHARS:
        return _reject(
            len(compact),
            "base64 payload exceeds the 64 KiB class-file limit",
            {"base64_length": len(compact),
             "max_base64_chars": MAX_BASE64_CHARS})
    try:
        raw = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        return _reject(0, "invalid base64 encoding", {"detail": str(exc)})
    if len(raw) > MAX_CLASS_BYTES:
        return _reject(
            len(raw), "decoded class file exceeds 64 KiB",
            {"class_size": len(raw), "max_class_bytes": MAX_CLASS_BYTES})
    try:
        cf = ClassFile(raw)
    except ClassFormatError as exc:
        return {"result": "reject", "first_rejection": exc.evidence()}
    try:
        report = verify_method(cf, method)
    except ClassFormatError as exc:
        return {"result": "reject", "first_rejection": exc.evidence()}
    except VerifyError as exc:
        return {"result": "reject", "first_rejection": exc.evidence(),
                "class_name": cf.this_class}
    return {"result": "pass", "report": report}


def _reject(offset, reason, detail=None):
    ev = {"stage": "request", "offset": offset, "reason": reason}
    if detail:
        ev["detail"] = detail
    return {"result": "reject", "first_rejection": ev}


def handle_api(body):
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return 400, _reject(0, "request body must be UTF-8 JSON",
                            {"detail": str(exc)})
    if not isinstance(payload, dict):
        return 400, _reject(0, "request body must be a JSON object")
    method = payload.get("method", "verify")
    if not isinstance(method, str) or not method:
        return 400, _reject(0, "method must be a non-empty string")
    return 200, run_review(payload.get("class_base64", ""), method)
