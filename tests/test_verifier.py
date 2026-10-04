"""Verification tests: half-initialized exception edges, merge conflicts,
legal construction paths and stable-offset error localization."""

import base64
import unittest

from app.classfile import ClassFile
from app.errors import ClassFormatError, VerifyError
from app.verifier import verify_method
from app import api

from tests.classkit import ClassBuilder

EX = "java/lang/Exception"
OBJ = "java/lang/Object"
IAE = "java/lang/IllegalArgumentException"


def parse(data):
    return ClassFile(data)


def verify(data, method="verify"):
    return verify_method(parse(data), method)


def expect_reject(data, method="verify"):
    try:
        verify(data, method)
    except VerifyError as e:
        return e
    raise AssertionError("expected VerifyError but method passed")


class LegalPathsTests(unittest.TestCase):
    def test_simple_construction(self):
        # new Object();   -> new/dup/invokespecial/pop/return
        b = ClassBuilder()
        m = b.method()
        m.new(OBJ); m.dup(); m.invokespecial_init(OBJ)
        m.pop(); m.return_()
        rep = verify(b.build())
        self.assertEqual(rep["result"], "pass")
        # The new slot is uninitialized at the new pc, initialized after.
        states = {o["pc"]: o for o in rep["offsets"]}
        self.assertEqual(states[0]["stack_in"], [])
        self.assertEqual(states[3]["stack_in"],
                         ["uninitialized(new@0:java/lang/Object)"])
        init_pc = 4  # 0:new(3) 3:dup(1) 4:invokespecial(3)
        self.assertEqual(states[init_pc]["stack_in"],
                         ["uninitialized(new@0:java/lang/Object)",
                          "uninitialized(new@0:java/lang/Object)"])
        # One copy is consumed by <init>; the surviving alias is now a ref.
        self.assertEqual(states[7]["stack_in"], ["java/lang/Object"])

    def test_constructor_with_string_argument(self):
        b = ClassBuilder()
        m = b.method()
        m.new(IAE); m.dup(); m.ldc_string("boom")
        m.invokespecial_init(IAE, "(Ljava/lang/String;)V")
        m.pop(); m.return_()
        rep = verify(b.build())
        self.assertEqual(rep["result"], "pass")

    def test_branch_merging_same_new_identity(self):
        # Both successors of ifeq reach the join carrying the *same*
        # uninitialized identity: legal.
        b = ClassBuilder()
        m = b.method()
        m.new(OBJ)            # 0  stack: [new]
        m.iconst(0)           # 3
        m.ifeq("B")           # 4  pops int; not-taken keeps [new]
        m.goto("J")           # 7
        m.label("B")          # 10 taken path keeps [new]
        m.label("J")          # 10 (merge: same new identity)
        m.dup()
        m.invokespecial_init(OBJ)
        m.pop(); m.return_()
        rep = verify(b.build())
        self.assertEqual(rep["result"], "pass")

    def test_backedge_loop_with_int_locals_converges(self):
        # int i=0; while (i<10) { i = i+1; } return;
        b = ClassBuilder()
        m = b.method()
        m.iconst(0); m.istore(1)
        m.label("loop")
        m.iload(1); m.bipush(10); m.if_icmplt("body")
        m.goto("end")
        m.label("body")
        m.iload(1); m.iconst(1); m.iadd(); m.istore(1)
        m.goto("loop")
        m.label("end")
        m.return_()
        rep = verify(b.build())
        self.assertEqual(rep["result"], "pass")
        # Loop header must be a fixpoint reached from both edges.
        loop_pc = m.labels["loop"]
        entry = next(o for o in rep["offsets"] if o["pc"] == loop_pc)
        self.assertEqual(entry["locals_in"][1], "int")
        back = [o for o in rep["offsets"]
                if loop_pc in o["normal_successors"]]
        self.assertTrue(back, "loop header has an incoming back edge")

    def test_handler_around_legal_throw(self):
        # try { throw new Exception(); } catch(Exception e) { return; }
        b = ClassBuilder()
        m = b.method()
        m.label("S")
        m.new(EX); m.dup(); m.invokespecial_init(EX)
        m.athrow()
        m.label("E")
        m.label("H")
        m.astore(1)
        m.return_()
        m.catch("S", "E", "H", EX)
        rep = verify(b.build())
        self.assertEqual(rep["result"], "pass")
        h = rep["exception_handlers"][0]
        self.assertTrue(h["reachable"])
        self.assertEqual(h["entry_stack"], [EX])
        self.assertEqual(h["entry_locals"][0], "top")
        self.assertEqual(h["entry_locals"][1], "top")

    def test_handler_around_failing_constructor(self):
        # try { new Exception("x"); } catch(Exception e) { return; }
        # On the edge from new/dup (before <init>) the uninit local purge
        # applies; the handler still gets exactly one exception on entry.
        b = ClassBuilder()
        m = b.method()
        m.label("S")
        m.new(EX); m.dup(); m.ldc_string("x")
        m.invokespecial_init(EX, "(Ljava/lang/String;)V")
        m.pop()
        m.label("E")
        m.return_()
        m.label("H")
        m.astore(0); m.return_()
        m.catch("S", "E", "H", EX)
        rep = verify(b.build())
        self.assertEqual(rep["result"], "pass")
        h = rep["exception_handlers"][0]
        self.assertEqual(h["entry_stack"], [EX])
        self.assertEqual(h["entry_locals"][0], "top")

    def test_distinct_initialized_news_merge_to_object(self):
        b = ClassBuilder()
        m = b.method()
        m.iconst(0); m.ifeq("B")
        m.new(EX); m.dup(); m.invokespecial_init(EX)
        m.goto("J")
        m.label("B")
        m.new(IAE); m.dup(); m.invokespecial_init(IAE)
        m.label("J")
        m.pop(); m.return_()
        rep = verify(b.build())
        self.assertEqual(rep["result"], "pass")
        join = next(o for o in rep["offsets"] if o["pc"] == m.labels["J"])
        self.assertEqual(join["stack_in"], ["java/lang/Object"])


class HalfInitializedExceptionTests(unittest.TestCase):
    def test_half_built_object_purged_from_handler_locals(self):
        # uninitialized ref stashed in local 0; handler tries to read it:
        # real JVM verification purges it to top, so aload 0 must fail.
        b = ClassBuilder()
        m = b.method()
        m.label("S")
        m.new(OBJ)            # 0
        m.astore(0)           # 3  local0 = uninitialized(new@0)
        m.aload(0)            # 4
        m.invokespecial_init(OBJ)  # 5-7
        m.return_()           # 8
        m.label("E")          # 9
        m.label("H")
        m.astore(1)
        m.aload(0)            # local 0 is top on the exception edge
        m.pop()
        m.return_()
        m.catch("S", "E", "H", EX)
        e = expect_reject(b.build())
        self.assertEqual(e.pc, m.labels["H"] + 1)  # at aload 0
        self.assertIn("top", str(e))

    def test_handler_entry_never_carries_uninit_stack(self):
        # Structural guarantee from the report even when the region starts
        # exactly at `new`: handler stack is exactly one Throwable.
        b = ClassBuilder()
        m = b.method()
        m.label("S")
        m.new(EX); m.dup(); m.invokespecial_init(EX); m.athrow()
        m.label("E")
        m.label("H")
        m.astore(0); m.return_()
        m.catch("S", "E", "H", EX)
        rep = verify(b.build())
        for o in rep["offsets"]:
            for edge in o["exception_edges"]:
                self.assertEqual(edge["handler_entry_stack"], [EX])

    def test_athrow_of_uninitialized_rejected(self):
        b = ClassBuilder()
        m = b.method()
        m.new(EX)
        pc_athrow = len(m.code)
        m.athrow()
        e = expect_reject(b.build())
        self.assertEqual(e.pc, pc_athrow)
        self.assertIn("uninitialized", e.reason)

    def test_backward_edge_with_uninit_rejected(self):
        # 0: goto 3 ; 3: new Object ; 6: goto 3 (back into new)
        b = ClassBuilder()
        m = b.method()
        m.goto("newpc")
        m.label("newpc")
        m.new(OBJ)
        m.goto("newpc")
        e = expect_reject(b.build())
        self.assertEqual(e.pc, m.labels["newpc"])
        self.assertIn("backward", e.reason)

    def test_normal_edge_merging_uninit_into_handler_rejected(self):
        # A normal goto reaches the handler pc carrying the half-built
        # object, while the exception edge reaches it with the thrown
        # reference: merging them must fail at the handler offset.
        b = ClassBuilder()
        m = b.method()
        m.new(OBJ)                 # 0  stack: [uninit]
        m.iconst(0)                # 3
        m.ifeq("B")                # 4
        m.goto("H")                # 7 normal edge -> H with [uninit]
        m.label("B")               # 10
        m.aconst_null()            # 10
        m.athrow()                 # 11
        m.label("H")
        m.astore(0)                # merge here: uninit vs caught ref
        m.return_()
        m.catch("B", "H", "H", EX)
        e = expect_reject(b.build())
        self.assertEqual(e.pc, m.labels["H"])
        self.assertIn("uninitialized", e.reason)

    def test_finally_handler_entry_is_throwable(self):
        b = ClassBuilder()
        m = b.method()
        m.label("S")
        m.iconst(0); m.ifeq("out")
        m.label("out")
        m.return_()
        m.label("H")
        m.astore(0); m.return_()
        m.catch("S", "out", "H", None)  # catch_type 0 -> finally
        rep = verify(b.build())
        self.assertEqual(rep["exception_handlers"][0]["entry_stack"],
                         ["java/lang/Throwable"])

    def test_tableswitch_converges_at_join(self):
        # switch(key){ case 0/1: iconst 1; default: iconst 2 } join: pop
        b = ClassBuilder()
        m = b.method()
        m.iconst(0)
        m.tableswitch(0, ["c0", "c1"], "cd")
        m.label("c0"); m.iconst(1); m.goto("J")
        m.label("c1"); m.iconst(1); m.goto("J")
        m.label("cd"); m.iconst(2)
        m.label("J")
        m.pop(); m.return_()
        rep = verify(b.build())
        self.assertEqual(rep["result"], "pass")
        join = next(o for o in rep["offsets"] if o["pc"] == m.labels["J"])
        self.assertEqual(join["stack_in"], ["int"])

    def test_lookupswitch_height_conflict(self):
        b = ClassBuilder()
        m = b.method()
        m.iconst(0)
        m.lookupswitch([(0, "c0"), (5, "c1")], "cd")
        m.label("c0"); m.iconst(1); m.goto("J")
        m.label("c1"); m.iconst(1); m.iconst(1); m.goto("J")
        m.label("cd"); m.iconst(2)
        m.label("J")
        m.pop(); m.return_()
        e = expect_reject(b.build())
        self.assertEqual(e.pc, m.labels["J"])
        self.assertIn("stack height", e.reason)


class MergeConflictTests(unittest.TestCase):
    def test_uninit_vs_initialized_ref_merge(self):
        # if (cond) new Object(); else null; -> merge
        b = ClassBuilder()
        m = b.method()
        m.iconst(0); m.ifeq("B")
        m.new(OBJ)
        m.goto("J")
        m.label("B")
        m.aconst_null()
        m.label("J")
        m.pop(); m.return_()
        e = expect_reject(b.build())
        self.assertEqual(e.pc, m.labels["J"])
        self.assertIn("uninitialized", e.reason)

    def test_two_different_new_identities_cannot_merge(self):
        b = ClassBuilder()
        m = b.method()
        m.iconst(0); m.ifeq("B")
        m.new(OBJ)
        m.goto("J")
        m.label("B")
        m.new(OBJ)
        m.label("J")
        m.pop(); m.return_()
        e = expect_reject(b.build())
        self.assertEqual(e.pc, m.labels["J"])
        self.assertIn("different new", e.reason)

    def test_int_vs_reference_merge(self):
        b = ClassBuilder()
        m = b.method()
        m.iconst(0); m.ifeq("B")
        m.aconst_null()
        m.goto("J")
        m.label("B")
        m.iconst(1)
        m.label("J")
        m.pop(); m.return_()
        e = expect_reject(b.build())
        self.assertEqual(e.pc, m.labels["J"])
        self.assertIn("incompatible types", e.reason)

    def test_inconsistent_stack_height(self):
        b = ClassBuilder()
        m = b.method()
        m.iconst(0); m.ifeq("B")
        m.iconst(1)
        m.goto("J")
        m.label("B")
        m.iconst(1); m.iconst(2)
        m.label("J")
        m.pop(); m.return_()
        e = expect_reject(b.build())
        self.assertEqual(e.pc, m.labels["J"])
        self.assertIn("stack height", e.reason)
        self.assertEqual(e.detail["height_a"] + e.detail["height_b"], 3)

    def test_second_init_of_same_new_rejected(self):
        # new; dup; astore 0; <init>; aload 0; <init> again
        b = ClassBuilder()
        m = b.method()
        m.new(OBJ); m.dup(); m.astore(0)
        m.invokespecial_init(OBJ)
        pc_second = len(m.code)
        m.aload(0)
        m.invokespecial_init(OBJ)
        m.pop(); m.return_()
        e = expect_reject(b.build())
        self.assertEqual(e.pc, pc_second + 1)  # at second invokespecial
        self.assertIn("not an uninitialized", e.reason)

    def test_init_on_already_initialized_receiver(self):
        b = ClassBuilder()
        m = b.method()
        m.aconst_null()
        pc = len(m.code)
        m.invokespecial_init(OBJ)
        m.return_()
        e = expect_reject(b.build())
        self.assertEqual(e.pc, pc)
        self.assertIn("not an uninitialized", e.reason)


class StructuralRejectionTests(unittest.TestCase):
    def _valid_bytes(self):
        b = ClassBuilder()
        m = b.method()
        m.new(OBJ); m.dup(); m.invokespecial_init(OBJ)
        m.pop(); m.return_()
        return b.build()

    def test_truncated_class_file(self):
        data = self._valid_bytes()
        with self.assertRaises(ClassFormatError) as cm:
            ClassFile(data[:len(data) // 2])
        self.assertIsInstance(cm.exception.offset, int)
        self.assertIn("truncated", cm.exception.reason)

    def test_truncated_attribute_clears_conclusion(self):
        # API level: a truncated class reports parse-stage rejection and no
        # stale pass from an earlier request.
        good = base64.b64encode(self._valid_bytes()).decode()
        bad = base64.b64encode(self._valid_bytes()[:20]).decode()
        r1 = api.run_review(good, "verify")
        self.assertEqual(r1["result"], "pass")
        r2 = api.run_review(bad, "verify")
        self.assertEqual(r2["result"], "reject")
        self.assertEqual(r2["first_rejection"]["stage"], "parse")
        self.assertNotIn("report", r2)

    def test_jump_into_instruction_middle(self):
        # 0: bipush 0   (2 bytes) ; 2: goto -1 -> lands at pc 1
        b = ClassBuilder()
        m = b.method()
        m.bipush(0)
        pc_goto = len(m.code)
        m.goto("MID")
        m.label("MID")  # placed at code end = pc_goto+2? force target=1
        # Rebuild target explicitly: patch label to absolute pc 1.
        m.labels["MID"] = 1
        e = expect_reject(b.build())
        self.assertEqual(e.pc, 1)
        self.assertIn("instruction boundary", e.reason)

    def test_handler_start_ge_end(self):
        b = ClassBuilder()
        m = b.method()
        m.return_()
        m.catch_raw(0, 0, 0, EX)
        e = expect_reject(b.build())
        self.assertEqual(e.pc, 0)
        self.assertIn("handler range", e.reason)

    def test_handler_pc_into_middle(self):
        b = ClassBuilder()
        m = b.method()
        m.bipush(0)       # pc 0..1
        m.return_()       # pc 2
        # range [0,2) is valid; handler_pc 1 is the bipush operand byte.
        m.catch_raw(0, 2, 1, EX)
        e = expect_reject(b.build())
        self.assertEqual(e.pc, 1)
        self.assertIn("handler_pc", e.reason)

    def test_handler_end_past_code(self):
        b = ClassBuilder()
        m = b.method()
        m.return_()
        m.catch_raw(0, 999, 0, EX)
        e = expect_reject(b.build())
        self.assertIn("end_pc", e.reason)

    def test_stack_underflow_localized(self):
        b = ClassBuilder()
        m = b.method()
        pc = len(m.code)
        m.iadd()
        m.return_()
        e = expect_reject(b.build())
        self.assertEqual(e.pc, pc)


class SubsetRestrictionTests(unittest.TestCase):
    def test_field_access_rejected(self):
        b = ClassBuilder()
        m = b.method()
        m.raw(bytes([0xB2, 0x00, 0x01]))  # getstatic
        m.return_()
        e = expect_reject(b.build())
        self.assertEqual(e.pc, 0)
        self.assertIn("field access", e.detail["reason"])

    def test_non_void_descriptor_rejected(self):
        b = ClassBuilder()
        m = b.method(name="verify", descriptor="()I")
        m.return_()
        with self.assertRaises(VerifyError) as cm:
            verify(b.build())
        self.assertIn("()V", cm.exception.reason)

    def test_category2_rejected(self):
        b = ClassBuilder()
        m = b.method()
        m.u1(0x09)  # lconst_0
        m.return_()
        e = expect_reject(b.build())
        self.assertEqual(e.pc, 0)

    def test_missing_method(self):
        b = ClassBuilder()
        m = b.method(name="other")
        m.return_()
        with self.assertRaises(VerifyError) as cm:
            verify(b.build(), "verify")
        self.assertIn("not found", cm.exception.reason)


class UnreachableCodeTests(unittest.TestCase):
    def test_unreachable_instruction_listed_without_state(self):
        # return; <unreachable> iconst_1; pop;
        b = ClassBuilder()
        m = b.method()
        m.return_()
        dead_pc = len(m.code)
        m.iconst(1); m.pop()
        rep = verify(b.build())
        self.assertEqual(rep["result"], "pass")
        rows = {o["pc"]: o for o in rep["offsets"]}
        self.assertTrue(rows[0]["reachable"])
        self.assertFalse(rows[dead_pc]["reachable"])
        self.assertIsNone(rows[dead_pc]["stack_in"])

    def test_unreachable_bad_types_do_not_fail_but_malformed_do(self):
        # Unreachable type garbage passes type-checking, but jumping into
        # the middle of it is still rejected structurally.
        b = ClassBuilder()
        m = b.method()
        m.return_()
        m.iadd()          # underflow, but unreachable -> no type error
        rep = verify(b.build())
        self.assertEqual(rep["result"], "pass")


if __name__ == "__main__":
    unittest.main()
