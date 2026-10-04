"""One-shot verification entrypoint used by the compose ``verify`` service.

Runs, in order:
  1. the unit-test suite (half-initialized exception edges, merge
     conflicts, legal construction paths, structural rejections),
  2. an image-level HTTP/API smoke test against a short-lived server,
  3. an optional compose-network smoke test against the ``web`` service
     when WEB_HEALTH_URL is provided.

Exits 0 only if every stage passes; compose reports that status code.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error


def run_unit_tests():
    print("== [1/3] unit tests ==", flush=True)
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests",
         "-v"], cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if proc.returncode != 0:
        print("UNIT TESTS FAILED (%d)" % proc.returncode, flush=True)
        return False
    print("unit tests: PASS", flush=True)
    return True


def _check_handler_frame_report(report):
    """Shared assertions for the legal javac-style class: the protected
    region constructs an Object, the handler entry carries exactly one
    java/lang/Exception reference, and the declared frames are reported."""
    rows = {o["pc"]: o for o in report["offsets"]}
    init_row = next(o for o in report["offsets"]
                    if o["mnemonic"] == "invokespecial")
    assert init_row["stack_in"] == [
        "uninitialized(new@0:java/lang/Object)",
        "uninitialized(new@0:java/lang/Object)"], init_row
    handlers = report["exception_handlers"]
    assert len(handlers) == 1, handlers
    h = handlers[0]
    assert h["reachable"] and h["entry_stack"] == ["java/lang/Exception"], h
    assert h["entry_locals"][0] == "top", h
    assert rows[h["handler_pc"]]["stack_in"] == ["java/lang/Exception"]
    frames = report["stack_map_frames"]
    assert any(f["offset"] == h["handler_pc"]
               and f["stack"] == ["java/lang/Exception"] for f in frames), \
        frames


def run_inprocess_api_smoke():
    print("== [2/3] in-process API smoke ==", flush=True)
    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    import base64
    from tests.classkit import ClassBuilder, build_handler_frame_class
    from app import api

    # Legal construction path must pass.
    b = ClassBuilder()
    m = b.method()
    m.new("java/lang/Object")
    m.dup()
    m.invokespecial_init("java/lang/Object")
    m.pop()
    m.return_()
    good = base64.b64encode(b.build()).decode()
    r = api.run_review(good, "verify")
    assert r["result"] == "pass", r
    assert r["report"]["offsets"][-1]["mnemonic"] == "return"

    # The legal compiler-generated class with an object-typed handler
    # frame must pass and report the handler entry state.
    r = api.run_review(
        base64.b64encode(build_handler_frame_class()).decode(), "verify")
    assert r["result"] == "pass", r
    _check_handler_frame_report(r["report"])

    # Half-built object on athrow must be rejected at the athrow pc.
    b2 = ClassBuilder()
    m2 = b2.method()
    m2.new("java/lang/Exception")
    athrow_pc = len(m2.code)
    m2.athrow()
    r = api.run_review(base64.b64encode(b2.build()).decode(), "verify")
    assert r["result"] == "reject", r
    assert r["first_rejection"]["stage"] == "verify"
    assert r["first_rejection"]["offset"] == athrow_pc

    # Truncated input must clear the earlier pass and reject at parse.
    r = api.run_review(base64.b64encode(b2.build()[:8]).decode(), "verify")
    assert r["result"] == "reject" and "report" not in r
    assert r["first_rejection"]["stage"] == "parse"
    print("in-process API smoke: PASS", flush=True)
    return True


def run_http_smoke(url, timeout=30.0):
    print("== [3/3] HTTP smoke against %s ==" % url, flush=True)
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/healthz", timeout=2) as resp:
                body = json.loads(resp.read().decode())
            if body.get("status") == "ok":
                break
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last = exc
            time.sleep(0.5)
    else:
        print("health check never succeeded: %r" % last, flush=True)
        return False

    with urllib.request.urlopen(url + "/", timeout=5) as resp:
        page = resp.read().decode("utf-8")
    assert "第三方诊断类" in page, "review page marker missing"

    import base64
    from tests.classkit import ClassBuilder, build_handler_frame_class
    b = ClassBuilder()
    m = b.method()
    m.label("S")
    m.new("java/lang/Exception")
    m.dup()
    m.invokespecial_init("java/lang/Exception")
    m.athrow()
    m.label("E")
    m.label("H")
    m.astore(0)
    m.return_()
    m.catch("S", "E", "H", "java/lang/Exception")
    payload = json.dumps({
        "class_base64": base64.b64encode(b.build()).decode()}).encode()
    req = urllib.request.Request(
        url + "/api/verify", data=payload,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        result = json.loads(resp.read().decode())
    assert result["result"] == "pass", result
    h = result["report"]["exception_handlers"][0]
    assert h["entry_stack"] == ["java/lang/Exception"], h

    # The legal compiler-generated class with an object-typed handler
    # frame must pass over the real HTTP interface as well.
    payload = json.dumps({
        "class_base64": base64.b64encode(
            build_handler_frame_class()).decode()}).encode()
    req = urllib.request.Request(
        url + "/api/verify", data=payload,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        result = json.loads(resp.read().decode())
    assert result["result"] == "pass", result
    _check_handler_frame_report(result["report"])
    print("HTTP smoke: PASS", flush=True)
    return True


def main():
    if not run_unit_tests():
        return 1
    if not run_inprocess_api_smoke():
        return 2
    url = os.environ.get("WEB_HEALTH_URL")
    if url:
        if not run_http_smoke(url.rstrip("/")):
            return 3
    else:
        print("(WEB_HEALTH_URL not set; skipping compose-network smoke)",
              flush=True)
    print("ALL VERIFICATION STAGES PASSED", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
