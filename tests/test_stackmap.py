"""StackMapTable tests.

Covers the producer-asserted frames that a standard JVM compiler (javac)
emits: compact/extended/append/chop/full forms over the legal verification
types (int/float, object, null, uninitialized), with frame offsets that
must land on instruction boundaries and agree with the work-queue result.

Malformed tables must keep producing stable, locatable rejections:
truncated attributes and bad constant pool references at parse time
(class-file offset), bad uninitialized offsets / jumps into instruction
middles / height and type conflicts at verify time (Code pc).
"""

import struct
import unittest

from app.classfile import ClassFile
from app.errors import ClassFormatError, VerifyError
from app.verifier import verify_method
from app import api

from tests.classkit import (
    ClassBuilder, vt_object, vt_uninit,
    VT_INTEGER, VT_FLOAT, VT_NULL, VT_OBJECT,
)

OBJ = "java/lang/Object"
EX = "java/lang/Exception"
IAE = "java/lang/IllegalArgumentException"


def verify(data, method="verify"):
    return verify_method(ClassFile(data), method)


def expect_reject(data, method="verify"):
    try:
        verify(data, method)
    except VerifyError as e:
        return e
    raise AssertionError("expected VerifyError but method passed")


def expect_parse_error(data):
    try:
        ClassFile(data)
    except ClassFormatError as e:
        return e
    raise AssertionError("expected ClassFormatError but class parsed")


def object_handler_frame_class():
    """javac-shaped class: protected region constructs an object and
    returns normally; the Exception handler stores the exception and
    returns; the handler carries a standard object-reference stack map
    frame (same_locals_1_stack_item, Object_variable_info)."""
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
    m.stack_map("H", "stack", items=[vt_object(EX)])
    return b


class LegalStackMapTests(unittest.TestCase):
    def test_standard_object_handler_frame_is_accepted(self):
        # The reported defect: javac's object-typed handler frame made the
        # parser reject the whole class before any state could be reviewed.
        b = object_handler_frame_class()
        rep = verify(b.build())
        self.assertEqual(rep["result"], "pass")
        h = rep["exception_handlers"][0]
        self.assertTrue(h["reachable"])
        self.assertEqual(h["handler_pc"], b.methods[0].labels["H"])
        self.assertEqual(h["entry_stack"], [EX])
        self.assertTrue(all(x == "top" for x in h["entry_locals"]))
        rows = {o["pc"]: o for o in rep["offsets"]}
        hpc = b.methods[0].labels["H"]
        self.assertEqual(rows[hpc]["stack_in"], [EX])
        # Construction-path states are still reported offset by offset.
        self.assertEqual(rows[3]["stack_in"],
                         ["uninitialized(new@0:java/lang/Object)"])
        self.assertEqual(rows[7]["stack_in"], [OBJ])

    def test_api_accepts_object_handler_frame_base64(self):
        import base64
        data = object_handler_frame_class().build()
        r = api.run_review(base64.b64encode(data).decode(), "verify")
        self.assertEqual(r["result"], "pass", r)
        h = r["report"]["exception_handlers"][0]
        self.assertEqual(h["entry_stack"], [EX])

    def test_compact_same_frame(self):
        b = ClassBuilder()
        m = b.method()
        m.goto("F")
        m.label("F")
        m.stack_map("F", "same")
        m.return_()
        self.assertEqual(verify(b.build())["result"], "pass")

    def test_extended_same_frame(self):
        b = ClassBuilder()
        m = b.method()
        m.goto("F")
        for _ in range(200):
            m.nop()
        m.label("F")
        m.stack_map("F", "same", extended=True)
        m.return_()
        rep = verify(b.build())
        self.assertEqual(rep["result"], "pass")
        self.assertGreater(m.labels["F"] - 2, 63)

    def test_same_locals_1_stack_item_compact_and_extended(self):
        for extended in (False, True):
            b = ClassBuilder()
            m = b.method()
            m.iconst(0)
            if not extended:
                m.goto("J")
            else:
                m.goto("J")
                for _ in range(100):
                    m.nop()
            m.label("J")
            m.stack_map("J", "stack", items=[VT_INTEGER],
                        extended=extended)
            m.pop()
            m.return_()
            self.assertEqual(verify(b.build())["result"], "pass")

    def test_append_frame_with_int_object_null_locals(self):
        b = ClassBuilder()
        m = b.method()
        m.iconst(0); m.istore(0)
        m.new(OBJ); m.dup(); m.invokespecial_init(OBJ); m.astore(1)
        m.aconst_null(); m.astore(2)
        m.goto("J")
        m.label("J")
        m.stack_map("J", "append",
                    items=[VT_INTEGER, vt_object(OBJ), VT_NULL])
        m.return_()
        rep = verify(b.build())
        self.assertEqual(rep["result"], "pass")
        rows = {o["pc"]: o for o in rep["offsets"]}
        j = m.labels["J"]
        self.assertEqual(rows[j]["locals_in"][0], "int")
        self.assertEqual(rows[j]["locals_in"][1], OBJ)
        self.assertEqual(rows[j]["locals_in"][2], "null")

    def test_chop_frame(self):
        b = ClassBuilder()
        m = b.method()
        m.iconst(0); m.istore(0); m.iconst(1); m.istore(1)
        m.goto("A")
        m.label("A")
        m.stack_map("A", "append", items=[VT_INTEGER, VT_INTEGER])
        m.goto("B")
        m.label("B")
        m.stack_map("B", "chop", chop=2)
        m.return_()
        self.assertEqual(verify(b.build())["result"], "pass")

    def test_full_frame_all_legal_verification_types(self):
        # int / float / null / initialized-object locals, empty stack.
        b = ClassBuilder()
        m = b.method()
        m.iconst(0); m.istore(0)
        m.raw(bytes([0x0B]))        # fconst_0
        m.raw(bytes([0x44]))        # fstore_1
        m.aconst_null(); m.astore(2)
        m.new(OBJ); m.dup(); m.invokespecial_init(OBJ); m.astore(3)
        m.goto("J")
        m.label("J")
        m.stack_map(
            "J", "full",
            locals_=[VT_INTEGER, VT_FLOAT, VT_NULL, vt_object(OBJ)],
            stack=[])
        m.return_()
        rep = verify(b.build())
        self.assertEqual(rep["result"], "pass")
        rows = {o["pc"]: o for o in rep["offsets"]}
        self.assertEqual(rows[m.labels["J"]]["locals_in"][:4],
                         ["int", "float", "null", OBJ])

    def test_full_frame_matching_propagated_state(self):
        b = ClassBuilder()
        m = b.method()
        m.iconst(0); m.istore(0)
        m.new(OBJ); m.dup(); m.invokespecial_init(OBJ)
        m.goto("J")
        m.label("J")
        m.stack_map("J", "full",
                    locals_=[VT_INTEGER], stack=[vt_object(OBJ)])
        m.pop(); m.return_()
        rep = verify(b.build())
        self.assertEqual(rep["result"], "pass")
        rows = {o["pc"]: o for o in rep["offsets"]}
        self.assertEqual(rows[m.labels["J"]]["stack_in"], [OBJ])

    def test_uninitialized_verification_type_identity(self):
        b = ClassBuilder()
        m = b.method()
        m.label("newpc")
        m.new(OBJ)
        m.goto("J")
        m.label("J")
        m.stack_map("J", "stack", items=[vt_uninit(m.labels["newpc"])])
        m.dup(); m.invokespecial_init(OBJ); m.pop(); m.return_()
        rep = verify(b.build())
        self.assertEqual(rep["result"], "pass")
        rows = {o["pc"]: o for o in rep["offsets"]}
        self.assertEqual(rows[m.labels["J"]]["stack_in"],
                         ["uninitialized(new@0:java/lang/Object)"])

    def test_null_and_reference_merge_against_declared_object(self):
        # Two edges (null vs initialized reference) meet at a frame that
        # declares java/lang/Object: a sound, standard shape.
        b = ClassBuilder()
        m = b.method()
        m.iconst(0); m.ifeq("B")
        m.aconst_null()
        m.goto("J")
        m.label("B")
        m.new(OBJ); m.dup(); m.invokespecial_init(OBJ)
        m.label("J")
        m.stack_map("J", "stack", items=[vt_object(OBJ)])
        m.pop(); m.return_()
        self.assertEqual(verify(b.build())["result"], "pass")

    def test_float_verification_type(self):
        b = ClassBuilder()
        m = b.method()
        m.raw(bytes([0x0B]))  # fconst_0
        m.goto("J")
        m.label("J")
        m.stack_map("J", "stack", items=[VT_FLOAT])
        m.pop(); m.return_()
        self.assertEqual(verify(b.build())["result"], "pass")


class StackMapRejectionTests(unittest.TestCase):
    def test_truncated_stack_map_attribute_body_parse_stage(self):
        # A class whose file ends in the middle of the StackMapTable
        # attribute is rejected at parse time with a file offset.
        b = object_handler_frame_class()
        data = b.build()
        # Drop the final two bytes (inside the Object_variable_info cp index
        # of the trailing StackMapTable attribute).
        cut = data[:len(data) - 2]
        with self.assertRaises(ClassFormatError) as cm:
            ClassFile(cut)
        self.assertIn("truncated", cm.exception.reason)
        self.assertIsInstance(cm.exception.offset, int)

    def test_internally_truncated_frame_parse_stage(self):
        # The attribute length is correct but its frame payload is short.
        b = ClassBuilder()
        m = b.method()
        m.return_()
        m.stack_map_raw = bytes([0, 1, 255, 0, 5])  # full frame, cut short
        e = expect_parse_error(b.build())
        self.assertIn("truncated", e.reason)

    def test_reserved_frame_type_rejected(self):
        b = ClassBuilder()
        m = b.method()
        m.return_()
        m.stack_map_raw = bytes([0, 1, 200])
        e = expect_parse_error(b.build())
        self.assertIn("reserved", e.reason)

    def test_object_variable_info_bad_constant_pool_index(self):
        b = ClassBuilder()
        m = b.method()
        m.label("S")
        m.new(OBJ); m.dup(); m.invokespecial_init(OBJ); m.pop()
        m.return_()
        m.label("E"); m.label("H")
        m.astore(0); m.return_()
        m.catch("S", "E", "H", EX)
        m.stack_frames.append(dict(
            label="H", kind="stack", extended=False,
            items=[("oidx", 9999)], locals=[], stack=[], chop=None))
        e = expect_parse_error(b.build())
        self.assertIn("constant pool index", e.reason)

    def test_object_variable_info_wrong_constant_tag(self):
        # Tag-7 item referencing a Utf8 index rather than a Class entry.
        b = ClassBuilder()
        m = b.method()
        m.goto("H")
        m.label("H"); m.return_()
        utf_idx = m.cp.utf8("java/lang/Object")
        body = struct.pack(">HBBH", 1, 64 + m.labels["H"] - 0,
                           VT_OBJECT, utf_idx)
        m.stack_map_raw = body
        e = expect_parse_error(b.build())
        self.assertIn("CONSTANT_Class", e.reason)

    def test_frame_offset_into_instruction_middle(self):
        b = ClassBuilder()
        m = b.method()
        m.bipush(0)      # pc 0..1
        m.return_()      # pc 2
        m.stack_frames.append(dict(
            label="X", kind="same", extended=False, items=[],
            locals=[], stack=[], chop=None))
        m.labels["X"] = 1   # operand byte of bipush
        e = expect_reject(b.build())
        self.assertEqual(e.pc, 1)
        self.assertIn("instruction boundary", e.reason)

    def test_uninitialized_offset_not_a_new(self):
        b = ClassBuilder()
        m = b.method()
        m.return_()       # pc 0 is a return, not a new
        m.stack_map("F", "full",
                    locals_=[], stack=[vt_uninit(0)])
        m.labels["F"] = 0
        e = expect_reject(b.build())
        self.assertEqual(e.pc, 0)
        self.assertIn("new instruction", e.reason)

    def test_uninitialized_offset_outside_code(self):
        b = ClassBuilder()
        m = b.method()
        m.goto("F")
        m.label("F"); m.return_()
        m.stack_map("F", "full",
                    locals_=[], stack=[vt_uninit(999)])
        e = expect_reject(b.build())
        self.assertEqual(e.detail["new_pc"], 999)
        self.assertIn("new instruction", e.reason)

    def test_declared_stack_height_conflict(self):
        b = ClassBuilder()
        m = b.method()
        m.iconst(0)
        m.goto("J")
        m.label("J")
        m.stack_map("J", "full", locals_=[], stack=[])  # actual [int]
        m.pop(); m.return_()
        e = expect_reject(b.build())
        self.assertEqual(e.pc, m.labels["J"])
        self.assertIn("stack conflicts", e.reason)
        self.assertEqual(e.detail["declared_height"], 0)
        self.assertEqual(e.detail["propagated_height"], 1)

    def test_declared_stack_type_conflict_int_vs_float(self):
        b = ClassBuilder()
        m = b.method()
        m.iconst(0)
        m.goto("J")
        m.label("J")
        m.stack_map("J", "stack", items=[VT_FLOAT])
        m.pop(); m.return_()
        e = expect_reject(b.build())
        self.assertEqual(e.pc, m.labels["J"])
        self.assertIn("stack type conflicts", e.reason)

    def test_declared_local_type_conflict(self):
        b = ClassBuilder()
        m = b.method()
        m.new(OBJ); m.dup(); m.invokespecial_init(OBJ); m.astore(0)
        m.goto("J")
        m.label("J")
        m.stack_map("J", "append", items=[VT_NULL])  # actual is Object ref
        m.return_()
        e = expect_reject(b.build())
        self.assertEqual(e.pc, m.labels["J"])
        self.assertIn("local type conflicts", e.reason)

    def test_declared_top_on_live_stack_rejected(self):
        b = ClassBuilder()
        m = b.method()
        m.iconst(0)
        m.goto("J")
        m.label("J")
        m.stack_map("J", "full", locals_=[], stack=[0])  # top on stack
        m.pop(); m.return_()
        e = expect_reject(b.build())
        self.assertIn("top", e.reason)

    def test_handler_frame_cannot_smuggle_half_initialized_local(self):
        # Exception edges purge uninitialized locals; a declared handler
        # frame that still asserts the uninitialized identity must fail.
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
                    locals_=[vt_uninit(m.labels["S"])],
                    stack=[vt_object(EX)])
        e = expect_reject(b.build())
        self.assertEqual(e.pc, m.labels["H"])

    def test_unreachable_declared_frame_rejected(self):
        # Frame at an instruction no edge (normal or exception) reaches.
        b = ClassBuilder()
        m = b.method()
        m.return_()
        m.iconst(0)  # dead code at pc 1
        m.stack_frames.append(dict(
            label="D", kind="same", extended=False, items=[],
            locals=[], stack=[], chop=None))
        m.labels["D"] = 1
        e = expect_reject(b.build())
        self.assertEqual(e.pc, 1)
        self.assertIn("never", e.reason)

    def test_chop_more_than_previous_locals(self):
        b = ClassBuilder()
        m = b.method()
        m.goto("F")
        m.label("F"); m.return_()
        # First frame chops 3 locals although the implicit previous frame
        # (method entry) defines none.
        m.stack_map_raw = struct.pack(">HBBH", 1, 248, 0, m.labels["F"])
        e = expect_parse_error(b.build())
        self.assertIn("chop", e.reason)

    def test_duplicate_stack_map_table_attribute(self):
        b = object_handler_frame_class()
        b.methods[0].duplicate_stack_map_attr = True
        e = expect_parse_error(b.build())
        self.assertIn("multiple StackMapTable", e.reason)


if __name__ == "__main__":
    unittest.main()
