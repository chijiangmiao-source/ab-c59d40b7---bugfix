"""One-shot verification entrypoint used by the compose ``verify`` service.

Runs, in order:
  1. the unit-test suite (half-initialized exception edges, merge
     conflicts, legal construction paths, stack map frames, structural
     rejections),
  2. an image-level acceptance/API smoke test against a short-lived server,
  3. an optional compose-network acceptance run against the ``web``
     service when WEB_HEALTH_URL is provided.

The acceptance corpus is the same in stages 2 and 3: it submits the
javac-shaped legal class whose Exception handler carries a standard
object-reference stack map frame (the regression that used to be rejected
at parse time), checks the pass verdict and the handler entry state, and
then regresses illegal stack maps, a half-initialized object escaping on
an exception edge, a control-flow merge conflict and an ordinary legal
construction path.

Exits 0 only if every stage passes; compose reports that status code.
"""

import base64
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

OBJ = "java/lang/Object"
EX = "java/lang/Exception"


# ---------------------------------------------------------------------------
# Acceptance corpus
# ---------------------------------------------------------------------------

def _headline_class():
    """Legal class: the protected region constructs an object and returns
    normally; the catch(Exception) handler stores the exception and returns;
    the handler entry carries a javac-style object stack map frame."""
    from tests.classkit import ClassBuilder, vt_object
    b = ClassBuilder()
    m = b.method()
    m.label("S")
    m.new(OBJ)
    m.dup()
    m.invokespecial_init(OBJ)
    m.pop()
    m.return_()
    m.label("E")
    m.label("H")
    m.astore(0)
    m.return_()
    m.catch("S", "E", "H", EX)
    # same_locals_1_stack_item_frame { Object_variable_info Exception }
    m.stack_map("H", "stack", items=[vt_object(EX)])
    return b


def acceptance_cases():
    """List of (name, class_bytes, check). ``check(result)`` raises
    AssertionError if the review result is not as expected."""
    from tests.classkit import ClassBuilder, vt_object

    cases = []

    # 1. Headline: object-typed handler frame must be accepted, and the
    #    handler entry must show exactly one Exception reference.
    cases.append(("legal_object_handler_frame",
                  _headline_class().build(),
                  _check_object_handler_pass))

    # 2. Ordinary legal construction path.
    b = ClassBuilder()
    m = b.method()
    m.new(OBJ); m.dup(); m.invokespecial_init(OBJ)
    m.pop(); m.return_()
    cases.append(("legal_plain_construction", b.build(), _check_pass))

    # 3. Illegal stack map: frame declares an empty stack where dataflow
    #    propagates one int -> verify-stage rejection at the frame pc.
    b = ClassBuilder()
    m = b.method()
    m.iconst(0)
    m.goto("J")
    m.label("J")
    m.stack_map("J", "full", locals_=[], stack=[])
    m.pop(); m.return_()
    cases.append(("illegal_stackmap_height", b.build(),
                  lambda r: _check_reject(r, "verify", "stack conflicts")))

    # 4. Illegal stack map at parse stage: class ends inside the attribute.
    cases.append(("illegal_stackmap_truncated",
                  _truncated_headline_bytes(),
                  lambda r: _check_reject(r, "parse", "truncated")))

    # 5. Half-initialized object thrown directly.
    b = ClassBuilder()
    m = b.method()
    m.new(EX)
    m.athrow()
    cases.append(("half_initialized_athrow", b.build(),
                  lambda r: _check_reject(r, "verify", "uninitialized")))

    # 6. Half-initialized object smuggled into a handler local: exception
    #    edges purge uninitialized locals, so a frame that still asserts
    #    the uninitialized identity at the handler must be rejected.
    b = ClassBuilder()
    m = b.method()
    m.label("S")
    m.new(OBJ); m.astore(0)
    m.aload(0); m.invokespecial_init(OBJ)
    m.return_()
    m.label("E"); m.label("H")
    m.astore(1); m.return_()
    m.catch("S", "E", "H", EX)
    m.stack_map("H", "full",
                locals_=[("uninit", m.labels["S"])],
                stack=[vt_object(EX)])
    cases.append(("half_initialized_exception_edge", b.build(),
                  lambda r: _check_reject(r, "verify", None)))

    # 7. Merge conflict: int vs reference on one operand stack slot.
    b = ClassBuilder()
    m = b.method()
    m.iconst(0); m.ifeq("B")
    m.aconst_null()
    m.goto("J")
    m.label("B")
    m.iconst(1)
    m.label("J")
    m.pop(); m.return_()
    cases.append(("merge_conflict", b.build(),
                  lambda r: _check_reject(r, "verify", "incompatible types")))

    return cases


def _truncated_headline_bytes():
    data = _headline_class().build()
    return data[:len(data) - 2]  # ends inside the attribute's cp index


def _check_pass(result):
    assert result.get("result") == "pass", result


def _check_object_handler_pass(result):
    _check_pass(result)
    handlers = result["report"]["exception_handlers"]
    assert handlers, "report must list the exception handler"
    h = handlers[0]
    assert h["reachable"] is True, h
    assert h["entry_stack"] == [EX], h
    assert h["entry_locals"], h
    assert all(x == "top" for x in h["entry_locals"]), h
    rows = {o["pc"]: o for o in result["report"]["offsets"]}
    assert rows[h["handler_pc"]]["stack_in"] == [EX], rows[h["handler_pc"]]


def _check_reject(result, stage, reason_substring):
    assert result.get("result") == "reject", result
    ev = result["first_rejection"]
    assert ev["stage"] == stage, ev
    assert isinstance(ev["offset"], int), ev
    if reason_substring is not None:
        assert reason_substring in ev["reason"], ev


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

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


def _run_acceptance(submit, label):
    print("== %s acceptance corpus ==" % label, flush=True)
    from app import api
    for name, class_bytes, check in acceptance_cases():
        b64 = base64.b64encode(class_bytes).decode()
        result = submit(b64)
        try:
            check(result)
        except AssertionError:
            print("ACCEPTANCE CASE FAILED: %s" % name, flush=True)
            print(json.dumps(result, ensure_ascii=False, indent=2),
                  flush=True)
            return False
        verdict = result.get("result")
        print("  %-34s %s" % (name, verdict), flush=True)
    print("%s acceptance: PASS" % label, flush=True)
    return True


def run_inprocess_api_smoke():
    print("== [2/3] in-process API ==", flush=True)
    sys.path.insert(0, os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    from app import api

    def submit(b64):
        return api.run_review(b64, "verify")

    if not _run_acceptance(submit, "in-process API"):
        return False
    # A truncated payload must clear any earlier pass and reject at parse.
    r1 = api.run_review(
        base64.b64encode(_headline_class().build()).decode(), "verify")
    assert r1["result"] == "pass", r1
    r2 = api.run_review(
        base64.b64encode(_headline_class().build()[:8]).decode(), "verify")
    assert r2["result"] == "reject" and "report" not in r2, r2
    assert r2["first_rejection"]["stage"] == "parse", r2
    print("in-process API: PASS", flush=True)
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

    def submit(b64):
        payload = json.dumps({"class_base64": b64}).encode()
        req = urllib.request.Request(
            url + "/api/verify", data=payload,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())

    if not _run_acceptance(submit, "HTTP"):
        return False
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
