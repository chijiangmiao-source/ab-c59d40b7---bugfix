"""Bytecode verifier for the supported ()V diagnostic subset.

Type states are propagated with a JVM-style work queue over both normal
control-flow edges and exception edges (JVMS 4.10.1.x, type-inference
style). Safety policy enforced on top of the JVM rules:

* An uninitialized object is tracked *only* as the ``new`` instruction
  that created it (``uninit(new_pc, class)``).
* Before construction finishes it may never be merged with an
  initialized reference (or a value produced by a different ``new``).
* Exception edges enter the handler with a fresh one-element stack
  holding the exception; every uninitialized slot in the incoming locals
  is purged to ``top``. A half-built object can therefore never reach a
  handler, neither on the stack nor through a local.
* A backwards branch may not carry any uninitialized object (one ``new``
  must not be constructed more than once).
"""

from collections import deque

from .errors import ClassFormatError, VerifyError
from .descriptors import parse_method_descriptor, slot_count

# ---------------------------------------------------------------------------
# Operand/instruction tables
# ---------------------------------------------------------------------------

# Format: opcode -> (mnemonic, operand layout)
# Layouts: "" (none), "b" (u1), "s" (u2), "c1" (u1 cp index),
#          "c2" (u2 cp index), "br2", "br4", "switch", "wide",
#          "iface" (u2 cpi,u1 count,u1 0), "idyn" (u2 cpi,u2 0),
#          "multi" (u2 cpi,u1 dims)
OPCODES = {
    0x00: ("nop", ""), 0x01: ("aconst_null", ""),
    0x02: ("iconst_m1", ""), 0x03: ("iconst_0", ""),
    0x04: ("iconst_1", ""), 0x05: ("iconst_2", ""),
    0x06: ("iconst_3", ""), 0x07: ("iconst_4", ""),
    0x08: ("iconst_5", ""),
    0x09: ("lconst_0", "X"), 0x0A: ("lconst_1", "X"),
    0x0B: ("fconst_0", ""), 0x0C: ("fconst_1", ""), 0x0D: ("fconst_2", ""),
    0x0E: ("dconst_0", "X"), 0x0F: ("dconst_1", "X"),
    0x10: ("bipush", "b"), 0x11: ("sipush", "s"),
    0x12: ("ldc", "c1"), 0x13: ("ldc_w", "c2"), 0x14: ("ldc2_w", "X"),
    0x15: ("iload", "b"), 0x16: ("lload", "X"), 0x17: ("fload", "b"),
    0x18: ("dload", "X"), 0x19: ("aload", "b"),
    0x1A: ("iload_0", ""), 0x1B: ("iload_1", ""),
    0x1C: ("iload_2", ""), 0x1D: ("iload_3", ""),
    0x1E: ("lload_0", "X"), 0x1F: ("lload_1", "X"),
    0x20: ("lload_2", "X"), 0x21: ("lload_3", "X"),
    0x22: ("fload_0", ""), 0x23: ("fload_1", ""),
    0x24: ("fload_2", ""), 0x25: ("fload_3", ""),
    0x26: ("dload_0", "X"), 0x27: ("dload_1", "X"),
    0x28: ("dload_2", "X"), 0x29: ("dload_3", "X"),
    0x2A: ("aload_0", ""), 0x2B: ("aload_1", ""),
    0x2C: ("aload_2", ""), 0x2D: ("aload_3", ""),
    0x2E: ("iaload", ""), 0x2F: ("laload", "X"),
    0x30: ("faload", ""), 0x31: ("daload", "X"),
    0x32: ("aaload", ""), 0x33: ("baload", ""),
    0x34: ("caload", ""), 0x35: ("saload", ""),
    0x36: ("istore", "b"), 0x37: ("lstore", "X"),
    0x38: ("fstore", "b"), 0x39: ("dstore", "X"),
    0x3A: ("astore", "b"),
    0x3B: ("istore_0", ""), 0x3C: ("istore_1", ""),
    0x3D: ("istore_2", ""), 0x3E: ("istore_3", ""),
    0x3F: ("lstore_0", "X"), 0x40: ("lstore_1", "X"),
    0x41: ("lstore_2", "X"), 0x42: ("lstore_3", "X"),
    0x43: ("fstore_0", ""), 0x44: ("fstore_1", ""),
    0x45: ("fstore_2", ""), 0x46: ("fstore_3", ""),
    0x47: ("dstore_0", "X"), 0x48: ("dstore_1", "X"),
    0x49: ("dstore_2", "X"), 0x4A: ("dstore_3", "X"),
    0x4B: ("astore_0", ""), 0x4C: ("astore_1", ""),
    0x4D: ("astore_2", ""), 0x4E: ("astore_3", ""),
    0x4F: ("iastore", ""), 0x50: ("lastore", "X"),
    0x51: ("fastore", ""), 0x52: ("dastore", "X"),
    0x53: ("aastore", ""), 0x54: ("bastore", ""),
    0x55: ("castore", ""), 0x56: ("sastore", ""),
    0x57: ("pop", ""), 0x58: ("pop2", ""),
    0x59: ("dup", ""), 0x5A: ("dup_x1", ""), 0x5B: ("dup_x2", ""),
    0x5C: ("dup2", ""), 0x5D: ("dup2_x1", ""), 0x5E: ("dup2_x2", ""),
    0x5F: ("swap", ""),
    0x60: ("iadd", ""), 0x61: ("ladd", "X"), 0x62: ("fadd", ""),
    0x63: ("dadd", "X"),
    0x64: ("isub", ""), 0x65: ("lsub", "X"), 0x66: ("fsub", ""),
    0x67: ("dsub", "X"),
    0x68: ("imul", ""), 0x69: ("lmul", "X"), 0x6A: ("fmul", ""),
    0x6B: ("dmul", "X"),
    0x6C: ("idiv", ""), 0x6D: ("ldiv", "X"), 0x6E: ("fdiv", ""),
    0x6F: ("ddiv", "X"),
    0x70: ("irem", ""), 0x71: ("lrem", "X"), 0x72: ("frem", ""),
    0x73: ("drem", "X"),
    0x74: ("ineg", ""), 0x75: ("lneg", "X"), 0x76: ("fneg", ""),
    0x77: ("dneg", "X"),
    0x78: ("ishl", ""), 0x79: ("lshl", "X"),
    0x7A: ("ishr", ""), 0x7B: ("lshr", "X"),
    0x7C: ("iushr", ""), 0x7D: ("lushr", "X"),
    0x7E: ("iand", ""), 0x7F: ("land", "X"),
    0x80: ("ior", ""), 0x81: ("lor", "X"),
    0x82: ("ixor", ""), 0x83: ("lxor", "X"),
    0x84: ("iinc", "bs"),
    0x85: ("i2l", "X"), 0x86: ("i2f", ""), 0x87: ("i2d", "X"),
    0x88: ("l2i", "X"), 0x89: ("l2f", "X"), 0x8A: ("l2d", "X"),
    0x8B: ("f2i", ""), 0x8C: ("f2l", "X"), 0x8D: ("f2d", "X"),
    0x8E: ("d2i", "X"), 0x8F: ("d2l", "X"), 0x90: ("d2f", "X"),
    0x91: ("i2b", ""), 0x92: ("i2c", ""), 0x93: ("i2s", ""),
    0x94: ("lcmp", "X"), 0x95: ("fcmpl", ""), 0x96: ("fcmpg", ""),
    0x97: ("dcmpl", "X"), 0x98: ("dcmpg", "X"),
    0x99: ("ifeq", "br2"), 0x9A: ("ifne", "br2"),
    0x9B: ("iflt", "br2"), 0x9C: ("ifge", "br2"),
    0x9D: ("ifgt", "br2"), 0x9E: ("ifle", "br2"),
    0x9F: ("if_icmpeq", "br2"), 0xA0: ("if_icmpne", "br2"),
    0xA1: ("if_icmplt", "br2"), 0xA2: ("if_icmpge", "br2"),
    0xA3: ("if_icmpgt", "br2"), 0xA4: ("if_icmple", "br2"),
    0xA5: ("if_acmpeq", "br2"), 0xA6: ("if_acmpne", "br2"),
    0xA7: ("goto", "br2"), 0xA8: ("jsr", "X"), 0xA9: ("ret", "X"),
    0xAA: ("tableswitch", "switch"), 0xAB: ("lookupswitch", "switch"),
    0xAC: ("ireturn", "X"), 0xAD: ("lreturn", "X"),
    0xAE: ("freturn", "X"), 0xAF: ("dreturn", "X"),
    0xB0: ("areturn", "X"), 0xB1: ("return", ""),
    0xB2: ("getstatic", "X"), 0xB3: ("putstatic", "X"),
    0xB4: ("getfield", "X"), 0xB5: ("putfield", "X"),
    0xB6: ("invokevirtual", "X"), 0xB7: ("invokespecial", "c2"),
    0xB8: ("invokestatic", "X"), 0xB9: ("invokeinterface", "X"),
    0xBA: ("invokedynamic", "X"),
    0xBB: ("new", "c2"), 0xBC: ("newarray", "b"), 0xBD: ("anewarray", "c2"),
    0xBE: ("arraylength", ""), 0xBF: ("athrow", ""),
    0xC0: ("checkcast", "c2"), 0xC1: ("instanceof", "c2"),
    0xC2: ("monitorenter", ""), 0xC3: ("monitorexit", ""),
    0xC4: ("wide", "wide"), 0xC5: ("multianewarray", "X"),
    0xC6: ("ifnull", "br2"), 0xC7: ("ifnonnull", "br2"),
    0xC8: ("goto_w", "br4"), 0xC9: ("jsr_w", "X"),
}

UNARY_INT = {
    "ineg", "i2b", "i2c", "i2s", "i2f", "f2i", "arraylength",
    "checkcast",
}
INT_LOADS = {"iaload", "baload", "caload", "saload"}

# ---------------------------------------------------------------------------
# Type representation
# ---------------------------------------------------------------------------

TOP = ("top",)
INT = ("int",)
FLOAT = ("float",)
NULL = ("null",)
OBJECT = ("ref", "java/lang/Object")
THROWABLE = ("ref", "java/lang/Throwable")


def uninit(new_pc, classname):
    return ("uninit", new_pc, classname)


def is_refish(t):
    return t[0] in ("ref", "null", "uninit")


def is_init_ref(t):
    return t[0] in ("ref", "null")


def type_str(t):
    if t[0] == "top":
        return "top"
    if t[0] == "uninit":
        return "uninitialized(new@%d:%s)" % (t[1], t[2])
    if t[0] == "ref":
        return t[1]
    return t[0]


def merge_value(a, b, target_pc, where, slot):
    """JVM local/stack lattice merge; raises VerifyError on conflict."""
    if a == b:
        return a
    # top absorbs / is absorbed by anything (locals only; stack top errors
    # are caught by the caller).
    if a[0] == "top":
        return b
    if b[0] == "top":
        return a
    if a[0] == "uninit" or b[0] == "uninit":
        # Same new identity merges with itself (handled by a == b above).
        # Merging with any initialized reference is forbidden.
        raise VerifyError(
            target_pc,
            "merging uninitialized object with %s" % (
                "an initialized reference"
                if is_init_ref(a) or is_init_ref(b)
                else "a value from a different new instruction"),
            {"slot": slot, "where": where,
             "type_a": type_str(a), "type_b": type_str(b)})
    if a[0] != b[0]:
        if {a[0], b[0]} == {"ref", "null"}:
            return a if a[0] == "ref" else b
        raise VerifyError(
            target_pc, "incompatible types at control-flow merge",
            {"slot": slot, "where": where,
             "type_a": type_str(a), "type_b": type_str(b)})
    if a[0] == "null":
        return NULL
    if a[0] in ("int", "float"):
        raise VerifyError(
            target_pc, "incompatible types at control-flow merge",
            {"slot": slot, "where": where,
             "type_a": type_str(a), "type_b": type_str(b)})
    # Two distinct reference types: without the class hierarchy we widen
    # to java/lang/Object, which is always a sound supertype.
    return OBJECT


# ---------------------------------------------------------------------------
# Instruction decoding
# ---------------------------------------------------------------------------

class Instruction:
    __slots__ = ("pc", "opcode", "mnemonic", "length", "operands",
                 "branch_targets")

    def __init__(self, pc, opcode, mnemonic, length, operands, targets):
        self.pc = pc
        self.opcode = opcode
        self.mnemonic = mnemonic
        self.length = length
        self.operands = operands
        self.branch_targets = targets


def _s16(v):
    return v - 0x10000 if v >= 0x8000 else v


def _s32(v):
    return v - 0x100000000 if v >= 0x80000000 else v


def decode_instructions(code):
    """Decode every instruction; returns pc -> Instruction."""
    instrs = {}
    pc = 0
    n = len(code)
    while pc < n:
        start = pc
        opcode = code[pc]
        if opcode not in OPCODES:
            raise VerifyError(pc, "illegal opcode",
                              {"opcode": "0x%02x" % opcode})
        mnemonic, layout = OPCODES[opcode]
        operands = {}
        targets = []
        pc += 1

        def need(k):
            if pc + k > n:
                raise VerifyError(start,
                                  "instruction truncated by code end",
                                  {"mnemonic": mnemonic, "needs": k})

        if layout == "X":
            raise VerifyError(
                start, "opcode outside supported ()V subset",
                {"mnemonic": mnemonic,
                 "reason": "category-2 value, field access, subroutine, "
                           "non-void return or non-init invocation"})
        elif layout == "":
            pass
        elif layout == "b":
            need(1); operands["value"] = code[pc]; pc += 1
        elif layout == "s":
            need(2)
            operands["value"] = _s16((code[pc] << 8) | code[pc + 1])
            pc += 2
        elif layout == "bs":
            need(3)
            operands["index"] = code[pc]
            operands["value"] = _s16((code[pc + 1] << 8) | code[pc + 2])
            pc += 3
        elif layout == "c1":
            need(1); operands["cp"] = code[pc]; pc += 1
        elif layout == "c2":
            need(2)
            operands["cp"] = (code[pc] << 8) | code[pc + 1]
            pc += 2
        elif layout == "br2":
            need(2)
            delta = _s16((code[pc] << 8) | code[pc + 1])
            pc += 2
            targets.append(start + delta)
        elif layout == "br4":
            need(4)
            v = (code[pc] << 24) | (code[pc + 1] << 16) | \
                (code[pc + 2] << 8) | code[pc + 3]
            pc += 4
            targets.append(start + _s32(v))
        elif layout == "switch":
            if opcode == 0xAA:
                pad = (4 - (pc % 4)) % 4
                need(pad + 12)
                pc += pad
                default = _s32(int.from_bytes(code[pc:pc + 4], "big"))
                low = _s32(int.from_bytes(code[pc + 4:pc + 8], "big"))
                high = _s32(int.from_bytes(code[pc + 8:pc + 12], "big"))
                pc += 12
                if high < low:
                    raise VerifyError(start,
                                      "tableswitch high < low",
                                      {"low": low, "high": high})
                count = high - low + 1
                need(count * 4)
                offsets = []
                for i in range(count):
                    off = _s32(int.from_bytes(code[pc:pc + 4], "big"))
                    pc += 4
                    offsets.append(off)
                operands.update(low=low, high=high,
                                default=start + default)
                targets.append(start + default)
                targets.extend(start + o for o in offsets)
            else:
                pad = (4 - (pc % 4)) % 4
                need(pad + 8)
                pc += pad
                default = _s32(int.from_bytes(code[pc:pc + 4], "big"))
                npairs = _s32(int.from_bytes(code[pc + 4:pc + 8], "big"))
                pc += 8
                if npairs < 0:
                    raise VerifyError(start, "lookupswitch negative npairs")
                need(npairs * 8)
                pairs = []
                last_match = None
                for i in range(npairs):
                    match = _s32(int.from_bytes(code[pc:pc + 4], "big"))
                    off = _s32(int.from_bytes(code[pc + 4:pc + 8], "big"))
                    pc += 8
                    if last_match is not None and match <= last_match:
                        raise VerifyError(
                            start, "lookupswitch matches not sorted/unique",
                            {"match": match, "previous": last_match})
                    last_match = match
                    pairs.append((match, start + off))
                operands.update(default=start + default, pairs=pairs)
                targets.append(start + default)
                targets.extend(t for _, t in pairs)
        elif layout == "wide":
            need(1)
            sub = code[pc]; pc += 1
            if sub not in OPCODES:
                raise VerifyError(start, "wide: illegal opcode",
                                  {"opcode": "0x%02x" % sub})
            sub_name, sub_layout = OPCODES[sub]
            if sub_name == "iinc":
                need(4)
                operands["index"] = int.from_bytes(code[pc:pc + 2], "big")
                operands["value"] = _s16(
                    int.from_bytes(code[pc + 2:pc + 4], "big"))
                pc += 4
            elif sub_name in ("iload", "fload", "aload",
                              "istore", "fstore", "astore"):
                need(2)
                operands["index"] = int.from_bytes(code[pc:pc + 2], "big")
                pc += 2
            else:
                raise VerifyError(
                    start, "wide: opcode cannot be widened",
                    {"mnemonic": sub_name})
            operands["wide_of"] = sub_name
            mnemonic = "wide %s" % sub_name
        else:  # pragma: no cover - table is exhaustive above
            raise VerifyError(start, "internal: unknown layout %r" % layout)

        if pc > n:  # pragma: no cover - need() guards this
            raise VerifyError(start, "instruction truncated by code end")
        instrs[start] = Instruction(start, opcode, mnemonic, pc - start,
                                    operands, targets)
    return instrs


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

ACC_STATIC = 0x0008
TERMINATORS = {"goto", "goto_w", "tableswitch", "lookupswitch",
               "return", "athrow"}
BRANCH_OPS = {"ifeq", "ifne", "iflt", "ifge", "ifgt", "ifle",
              "if_icmpeq", "if_icmpne", "if_icmplt", "if_icmpge",
              "if_icmpgt", "if_icmple", "if_acmpeq", "if_acmpne",
              "ifnull", "ifnonnull"}
INT_BRANCH1 = {"ifeq", "ifne", "iflt", "ifge", "ifgt", "ifle"}
REF_BRANCH1 = {"ifnull", "ifnonnull"}
INT_BRANCH2 = {n for n in BRANCH_OPS if n.startswith("if_icmp")}
REF_BRANCH2 = {"if_acmpeq", "if_acmpne"}


class Frame:
    __slots__ = ("locals", "stack")

    def __init__(self, locals_, stack):
        self.locals = locals_
        self.stack = stack

    def key(self):
        return (tuple(self.locals), tuple(self.stack))

    def copy(self):
        return Frame(list(self.locals), list(self.stack))


def validate_exception_table(code, table, instr_pcs):
    """Structural checks; offsets are bytecode pcs."""
    n = len(code)
    for e in table:
        if e.start_pc >= e.end_pc:
            raise VerifyError(
                e.start_pc, "invalid exception handler range",
                {"start_pc": e.start_pc, "end_pc": e.end_pc,
                 "handler_pc": e.handler_pc})
        if e.end_pc > n:
            raise VerifyError(
                e.end_pc if e.end_pc < n else n,
                "exception handler end_pc past code end",
                {"start_pc": e.start_pc, "end_pc": e.end_pc,
                 "code_length": n})
        if e.start_pc not in instr_pcs:
            raise VerifyError(e.start_pc,
                              "exception handler start_pc is not at an "
                              "instruction boundary",
                              {"start_pc": e.start_pc})
        if e.end_pc != n and e.end_pc not in instr_pcs:
            raise VerifyError(e.end_pc,
                              "exception handler end_pc is not at an "
                              "instruction boundary",
                              {"end_pc": e.end_pc})
        if e.handler_pc >= n or e.handler_pc not in instr_pcs:
            raise VerifyError(e.handler_pc,
                              "exception handler_pc is not the start of an "
                              "instruction",
                              {"handler_pc": e.handler_pc,
                               "code_length": n})


def load_stack_map_frames(cf, code_obj, instrs, code_length):
    """Resolve the parse-time StackMapTable frames to bytecode pcs.

    Takes the frames already strictly decoded at class-parse time
    (compact/extended/chop/append/full forms), computes absolute bytecode
    offsets from the offset deltas, and validates them against the
    instruction layout.

    Raises VerifyError (bytecode pc) for frames not on an instruction
    boundary or carrying an illegal Uninitialized_variable_info offset.
    """
    instr_pcs = set(instrs)
    result = []
    offset = -1
    for fr in code_obj.stack_map_frames:
        offset += fr.offset_delta + 1
        if offset < 0 or offset >= code_length:
            raise VerifyError(
                offset if offset >= 0 else 0,
                "StackMapTable frame offset outside code",
                {"frame_offset": offset,
                 "code_length": code_length})
        if offset not in instr_pcs:
            raise VerifyError(
                offset,
                "StackMapTable frame is not at an instruction boundary",
                {"frame_offset": offset})
        for slot, t in enumerate(fr.locals):
            if t[0] == "uninit":
                cls = _check_uninit_offset(
                    t[1], offset, instrs, cf, code_length, "local")
                fr.locals[slot] = ("uninit", t[1], cls)
        for slot, t in enumerate(fr.stack):
            if t[0] == "uninit":
                cls = _check_uninit_offset(
                    t[1], offset, instrs, cf, code_length, "stack")
                fr.stack[slot] = ("uninit", t[1], cls)
        fr.pc = offset
        result.append(fr)
    return result


def _check_uninit_offset(new_pc, frame_pc, instrs, cf, code_length, where):
    """Validate an Uninitialized_variable_info offset; return the class name
    of the new instruction so the frame carries the same identity shape as
    propagated ``("uninit", new_pc, class)`` states."""
    if new_pc >= code_length or new_pc not in instrs:
        raise VerifyError(
            frame_pc,
            "StackMapTable Uninitialized_variable_info offset does not "
            "name a new instruction",
            {"new_pc": new_pc, "frame_pc": frame_pc, "where": where})
    ins = instrs[new_pc]
    if ins.opcode != 0xBB:
        raise VerifyError(
            frame_pc,
            "StackMapTable Uninitialized_variable_info offset does not "
            "name a new instruction",
            {"new_pc": new_pc, "frame_pc": frame_pc, "where": where,
             "found_mnemonic": ins.mnemonic})
    return cf.pool.class_name(ins.operands["cp"])


def normalize_declared_frame(fr, code_obj):
    """Convert a parsed stack map frame to (locals, stack) over the verifier
    type tuples, enforcing the supported-subset restrictions and the
    Code attribute's max_locals / max_stack bounds.

    The declared frame is an *assertion* written by the producer; it never
    seeds dataflow on its own. Uninitialized_variable_info becomes
    ``("uninit", new_pc)`` (identity only); long/double and
    UninitializedThis have no place in a static ()V method of this subset.
    """
    def convert(t, where):
        kind = t[0]
        if kind in ("long", "double"):
            raise VerifyError(
                fr.pc,
                "StackMapTable declares a category-2 value (%s) outside "
                "the supported ()V subset" % kind,
                {"where": where})
        if kind == "uninitthis":
            raise VerifyError(
                fr.pc,
                "UninitializedThis_variable_info is illegal in a static "
                "method", {"where": where})
        if kind == "uninit":
            classname = t[2] if len(t) > 2 else None
            return uninit(t[1], classname)
        if kind == "top":
            return TOP
        if kind == "int":
            return INT
        if kind == "float":
            return FLOAT
        if kind == "null":
            return NULL
        if kind == "ref":
            return ("ref", t[1])
        raise VerifyError(fr.pc,
                          "unknown StackMapTable verification type",
                          {"where": where, "raw": str(t)})  # pragma: no cover

    loc = [convert(t, "local") for t in fr.locals]
    if len(loc) > code_obj.max_locals:
        raise VerifyError(
            fr.pc, "StackMapTable frame declares more locals than max_locals",
            {"declared": len(loc), "max_locals": code_obj.max_locals})
    loc += [TOP] * (code_obj.max_locals - len(loc))
    stk = [convert(t, "stack") for t in fr.stack]
    if len(stk) > code_obj.max_stack:
        raise VerifyError(
            fr.pc, "StackMapTable frame declares a stack deeper than "
                   "max_stack",
            {"declared": len(stk), "max_stack": code_obj.max_stack})
    return loc, stk


def _declared_assignable(declared, actual):
    """Is the propagated ``actual`` type assignable to a frame-declared type?

    Reference hierarchy reconstruction is intentionally not attempted
    (widening to any reference type is accepted, matching the merge
    lattice's conservative java/lang/Object widening); identity of
    uninitialized objects is exact.
    """
    d, a = declared, actual
    if d[0] == "top":
        return True
    if d[0] == "int":
        return a[0] == "int"
    if d[0] == "float":
        return a[0] == "float"
    if d[0] == "null":
        return a[0] == "null"
    if d[0] == "uninit":
        return a[0] == "uninit" and a[1] == d[1]
    if d[0] == "ref":
        # A declared reference accepts an initialized reference or null,
        # never a half-constructed object (half-built objects escape only
        # through explicit uninit identities, which are handled above).
        return a[0] in ("ref", "null")
    return False  # pragma: no cover


def verify_method(cf, method_name):
    """Verify the unique static ``method_name`` ()V method.

    Returns a JSON-able report dict. Raises ClassFormatError / VerifyError
    (both carry a stable offset) on rejection.
    """
    method = _select_method(cf, method_name)
    code_obj = method.code
    code = code_obj.code

    instrs = decode_instructions(code)
    pcs = set(instrs)
    validate_exception_table(code, code_obj.exception_table, pcs)
    declared_frames = load_stack_map_frames(
        cf, code_obj, instrs, len(code))
    declared_by_pc = {}
    for fr in declared_frames:
        if fr.pc in declared_by_pc:
            raise VerifyError(
                fr.pc, "duplicate StackMapTable frame at one bytecode offset",
                {"frame_offset": fr.pc})
        declared_by_pc[fr.pc] = fr

    # All explicit control-flow targets must land on instruction starts.
    for ins in instrs.values():
        for t in ins.branch_targets:
            if t < 0 or t >= len(code) or t not in pcs:
                raise VerifyError(
                    t if 0 <= t < len(code) else ins.pc,
                    "control-flow target is not at an instruction boundary"
                    if 0 <= t < len(code)
                    else "control-flow target outside code",
                    {"source_pc": ins.pc, "target_pc": t,
                     "mnemonic": ins.mnemonic})

    max_stack = code_obj.max_stack
    max_locals = code_obj.max_locals

    # Static ()V method: no incoming arguments.
    locals0 = [TOP] * max_locals
    stack0 = []

    # Producer-asserted stack map frames authoritatively define the type
    # state at their offset: every incoming edge must be assignable to the
    # declared frame, and propagation continues *from* the declared frame
    # (JVMS 4.10.1 type checking with StackMapTable).
    declared_states = {}
    for pc, fr in declared_by_pc.items():
        dloc, dstk = normalize_declared_frame(fr, code_obj)
        declared_states[pc] = Frame(dloc, dstk)

    states = {}
    queue = deque()

    def coerce_to_declared(pc, incoming, cause):
        """Check one incoming edge against the frame declared at ``pc``.

        Returns the authoritative frame to propagate onward; raises
        VerifyError at ``pc`` on a stack-height or per-slot mismatch.
        """
        declared = declared_states.get(pc)
        if declared is None:
            return incoming
        if len(declared.stack) != len(incoming.stack):
            raise VerifyError(
                pc, "StackMapTable stack conflicts with propagated stack "
                    "height",
                {"frame_pc": pc, "from_pc": cause,
                 "declared_height": len(declared.stack),
                 "propagated_height": len(incoming.stack)})
        for i, (d, a) in enumerate(zip(declared.stack, incoming.stack)):
            if d[0] == "top":
                # Top_variable_info is legal for dead locals but can never
                # name a live operand-stack slot.
                raise VerifyError(
                    pc, "StackMapTable declares top for a live operand "
                        "stack slot",
                    {"slot": i, "from_pc": cause})
            if not _declared_assignable(d, a):
                raise VerifyError(
                    pc, "StackMapTable stack type conflicts with propagated "
                        "type state",
                    {"slot": i, "from_pc": cause,
                     "declared": type_str(d), "propagated": type_str(a)})
        for i, (d, a) in enumerate(zip(declared.locals, incoming.locals)):
            if not _declared_assignable(d, a):
                raise VerifyError(
                    pc, "StackMapTable local type conflicts with propagated "
                        "type state",
                    {"slot": i, "from_pc": cause,
                     "declared": type_str(d), "propagated": type_str(a)})
        return declared

    def push_state(pc, frame, cause):
        if pc < 0 or pc >= len(code) or pc not in pcs:
            raise VerifyError(
                pc if 0 <= pc < len(code) else cause,
                "successor is not at an instruction boundary",
                {"from_pc": cause, "target_pc": pc})
        frame = coerce_to_declared(pc, frame, cause)
        if pc not in states:
            states[pc] = frame
            queue.append(pc)
            return
        old = states[pc]
        if len(old.stack) != len(frame.stack):
            raise VerifyError(
                pc, "inconsistent stack height at control-flow merge",
                {"height_a": len(old.stack), "height_b": len(frame.stack),
                 "from_pc": cause})
        changed = False
        new_locals = list(old.locals)
        for i, (a, b) in enumerate(zip(old.locals, frame.locals)):
            m = merge_value(a, b, pc, "local", i)
            if m != a:
                new_locals[i] = m
                changed = True
        new_stack = list(old.stack)
        for i, (a, b) in enumerate(zip(old.stack, frame.stack)):
            if a[0] == "top" or b[0] == "top":
                raise VerifyError(pc, "corrupt operand stack slot at merge",
                                  {"slot": i, "from_pc": cause})
            m = merge_value(a, b, pc, "stack", i)
            if m != a:
                new_stack[i] = m
                changed = True
        if changed:
            states[pc] = Frame(new_locals, new_stack)
            queue.append(pc)

    def exception_frame(catch_name):
        """Handler frame: stack = [caught throwable]; uninit locals purged.

        A typed handler receives exactly its catch class; a finally-style
        entry (catch_type = 0) receives Throwable. Either way the stack is
        rebuilt from scratch, so a half-built operand stack can never be
        carried into the handler.
        """
        entry = ("ref", catch_name) if catch_name else THROWABLE
        clean = [TOP if t[0] == "uninit" else t for t in frame.locals]
        return Frame(clean, [entry])

    def has_uninit(frame):
        return any(t[0] == "uninit" for t in frame.locals) or \
            any(t[0] == "uninit" for t in frame.stack)

    def guard_backward(target, frame, source_pc):
        if target <= source_pc and has_uninit(frame):
            raise VerifyError(
                target,
                "backward control-flow edge carries an uninitialized "
                "object (a single new may be initialized only once)",
                {"source_pc": source_pc, "target_pc": target})

    # Method entry; if a frame is declared at offset 0 the initial state is
    # checked against it exactly like any other incoming edge.
    push_state(0, Frame(locals0, stack0), 0)

    # Defensive convergence bound: the lattice is finite and monotone, so
    # a fixpoint is always reached; this only guards an implementation bug.
    step_limit = 20000 + 256 * len(code)
    steps = 0

    while queue:
        steps += 1
        if steps > step_limit:
            raise VerifyError(
                0, "type-state work queue did not converge",
                {"code_length": len(code), "steps": steps})
        pc = queue.popleft()
        ins = instrs[pc]
        frame = states[pc]
        out = frame.copy()

        branch_successors, terminal = transfer(
            ins, out, cf, max_stack, max_locals, code, instrs)

        # Exception edges are registered for every covered instruction
        # (conservative JVMS 4.10.1.9 fan-in).
        for e in code_obj.exception_table:
            if e.covers(pc):
                ef = exception_frame(e.catch_name)
                guard_backward(e.handler_pc, ef, pc)
                push_state(e.handler_pc, ef, pc)

        successors = []
        if terminal:
            if branch_successors:
                # goto / goto_w / tableswitch / lookupswitch: listed pcs
                # are the only successors.
                successors = branch_successors
        else:
            successors = list(branch_successors)
            fall = pc + ins.length
            if fall >= len(code):
                raise VerifyError(
                    pc, "execution falls off the end of the code",
                    {"mnemonic": ins.mnemonic})
            successors.append(fall)

        for t in successors:
            guard_backward(t, out, pc)
            push_state(t, out, pc)

    # Every declared frame must actually have been reached by at least one
    # propagated edge (normal or exception); a frame with no incoming state
    # cannot be reconciled with the work-queue propagation result.
    for pc in sorted(declared_by_pc):
        if pc not in states:
            raise VerifyError(
                pc, "StackMapTable frame at an offset the work queue never "
                    "reached (declared frame with no propagated state)",
                {"frame_type": declared_by_pc[pc].frame_type})

    return build_report(cf, method, code_obj, instrs, states)


def _select_method(cf, name):
    matches = [m for m in cf.methods if m.name == name]
    if not matches:
        raise VerifyError(
            None, "target method not found",
            {"method": name,
             "available": [m.name + m.descriptor for m in cf.methods]})
    if len(matches) > 1:
        raise VerifyError(None, "method name is ambiguous",
                          {"method": name})
    m = matches[0]
    if not (m.access_flags & ACC_STATIC):
        raise VerifyError(None, "target method must be static",
                          {"method": name + m.descriptor})
    if m.descriptor != "()V":
        raise VerifyError(
            None, "target method descriptor must be ()V",
            {"method": name + m.descriptor})
    if m.code is None:
        raise VerifyError(None, "target method has no Code attribute",
                          {"method": name + m.descriptor})
    return m


# ---------------------------------------------------------------------------
# Per-instruction transfer
# ---------------------------------------------------------------------------

def transfer(ins, f, cf, max_stack, max_locals, code, instrs):
    """Mutate frame ``f`` to the post-instruction state.

    Returns (normal_successor_pcs, terminal). The returned successor list
    excludes the ordinary fall-through (added by caller unless terminal).
    """
    pc, mn, ops = ins.pc, ins.mnemonic, ins.operands
    st, lv = f.stack, f.locals

    def need_stack(k):
        if len(st) < k:
            raise VerifyError(pc, "operand stack underflow",
                              {"mnemonic": mn, "needed": k,
                               "have": len(st)})

    def push(t):
        if len(st) + slot_count(t) > max_stack:
            raise VerifyError(pc, "operand stack exceeds max_stack",
                              {"max_stack": max_stack, "mnemonic": mn})
        st.append(t)

    def pop():
        if not st:
            raise VerifyError(pc, "operand stack underflow",
                              {"mnemonic": mn})
        return st.pop()

    def pop_expect(expected, label=None):
        need_stack(1)
        t = st[-1]
        ok = expected(t)
        if not ok:
            raise VerifyError(
                pc, "wrong type on operand stack",
                {"mnemonic": mn, "expected": label or expected.__name__,
                 "found": type_str(t)})
        return pop()

    def local_get(i):
        if i >= max_locals:
            raise VerifyError(pc, "local variable index exceeds max_locals",
                              {"index": i, "max_locals": max_locals})
        return lv[i]

    def local_set(i, t):
        if i >= max_locals:
            raise VerifyError(pc, "local variable index exceeds max_locals",
                              {"index": i, "max_locals": max_locals})
        lv[i] = t

    is_int = lambda t: t[0] == "int"
    is_float = lambda t: t[0] == "float"
    is_ref = lambda t: t[0] in ("ref", "null")
    is_obj = lambda t: t[0] in ("ref", "null", "uninit")

    name = mn.replace("wide ", "")

    implicit_idx = None
    for base in ("iload", "fload", "aload", "istore", "fstore", "astore"):
        if name.startswith(base + "_") and name[len(base) + 1:] in "0123":
            implicit_idx = int(name[len(base) + 1:])
            name = base
            break

    # ---- constants -------------------------------------------------------
    if mn == "aconst_null":
        push(NULL)
    elif mn in ("iconst_m1",) or mn.startswith("iconst_") or \
            mn in ("bipush", "sipush"):
        push(INT)
    elif mn.startswith("fconst_"):
        push(FLOAT)
    elif mn in ("ldc", "ldc_w"):
        e = cf.pool.raw(ops["cp"])
        tag = e["tag"]
        if tag == 3:
            push(INT)
        elif tag == 4:
            push(FLOAT)
        elif tag == 8:
            push(("ref", "java/lang/String"))
        elif tag == 7:
            push(("ref", "java/lang/Class"))
        else:
            raise VerifyError(pc, "unsupported ldc constant kind",
                              {"constant_index": ops["cp"]})

    # ---- locals ----------------------------------------------------------
    elif name in ("iload", "fload", "aload"):
        idx = ops.get("index", implicit_idx)
        t = local_get(idx)
        wanted = {"iload": (is_int, "int"),
                  "fload": (is_float, "float"),
                  "aload": (is_obj, "reference")}[name]
        if t[0] == "top" or not wanted[0](t):
            raise VerifyError(
                pc, "loading %s from local holding %s" % (
                    wanted[1], type_str(t)),
                {"index": idx})
        push(t)
    elif name in ("istore", "fstore", "astore"):
        idx = ops.get("index", implicit_idx)
        wanted = {"istore": (is_int, "int"),
                  "fstore": (is_float, "float"),
                  "astore": (is_obj, "reference")}[name]
        need_stack(1)
        t = st[-1]
        if not wanted[0](t):
            raise VerifyError(
                pc, "storing %s into %s local" % (type_str(t), wanted[1]),
                {"index": idx})
        pop()
        local_set(idx, t)

    # ---- stack manipulation ---------------------------------------------
    elif mn == "pop":
        pop_expect(lambda t: t[0] != "top", "category-1 value")
    elif mn == "pop2":
        need_stack(2)
        pop(); pop()
    elif mn == "dup":
        need_stack(1); st.append(st[-1])
        _check_max(st, max_stack, pc, mn)
    elif mn == "dup_x1":
        need_stack(2)
        a = st.pop(); b = st.pop(); st.extend([a, b, a])
        _check_max(st, max_stack, pc, mn)
    elif mn == "dup_x2":
        need_stack(3)
        a = st.pop(); b = st.pop(); c = st.pop()
        st.extend([a, c, b, a])
        _check_max(st, max_stack, pc, mn)
    elif mn == "dup2":
        need_stack(2)
        a, b = st[-2], st[-1]; st.extend([a, b])
        _check_max(st, max_stack, pc, mn)
    elif mn == "dup2_x1":
        need_stack(3)
        a = st.pop(); b = st.pop(); c = st.pop()
        st.extend([b, a, c, b, a])
        _check_max(st, max_stack, pc, mn)
    elif mn == "dup2_x2":
        need_stack(4)
        a = st.pop(); b = st.pop(); c = st.pop(); d = st.pop()
        st.extend([b, a, d, c, b, a])
        _check_max(st, max_stack, pc, mn)
    elif mn == "swap":
        need_stack(2)
        a = st.pop(); b = st.pop(); st.extend([a, b])

    # ---- arithmetic / conversion ----------------------------------------
    elif (mn.endswith("add") or mn.endswith("sub") or mn.endswith("mul")
          or mn.endswith("div") or mn.endswith("rem")
          or mn in ("iand", "ior", "ixor", "ishl", "ishr", "iushr")):
        kind = "f" if mn[0] == "f" else "i"
        pop_expect(is_float if kind == "f" else is_int,
                   "float" if kind == "f" else "int")
        pop_expect(is_float if kind == "f" else is_int,
                   "float" if kind == "f" else "int")
        push(FLOAT if kind == "f" else INT)
    elif mn in ("ineg",):
        pop_expect(is_int, "int"); push(INT)
    elif mn == "fneg":
        pop_expect(is_float, "float"); push(FLOAT)
    elif mn in ("fcmpl", "fcmpg"):
        pop_expect(is_float, "float"); pop_expect(is_float, "float")
        push(INT)
    elif mn in ("i2b", "i2c", "i2s"):
        pop_expect(is_int, "int"); push(INT)
    elif mn == "i2f":
        pop_expect(is_int, "int"); push(FLOAT)
    elif mn == "f2i":
        pop_expect(is_float, "float"); push(INT)
    elif name == "iinc":
        t = local_get(ops["index"])
        if t[0] != "int":
            raise VerifyError(pc, "iinc on non-int local",
                              {"index": ops["index"], "found": type_str(t)})

    # ---- arrays ----------------------------------------------------------
    elif mn in INT_LOADS or mn in ("faload", "aaload"):
        pop_expect(is_int, "int (index)")
        pop_expect(is_ref, "array reference")
        if mn == "faload":
            push(FLOAT)
        elif mn == "aaload":
            push(("ref", "java/lang/Object"))
        else:
            push(INT)
    elif mn in ("iastore", "fastore", "aastore", "bastore",
                "castore", "sastore"):
        if mn == "aastore":
            need_stack(1)
            if not is_ref(st[-1]):
                raise VerifyError(
                    pc, "aastore cannot store an uninitialized object",
                    {"found": type_str(st[-1])})
            pop()
        else:
            pop_expect(is_float if mn == "fastore" else is_int,
                       "float" if mn == "fastore" else "int value")
        pop_expect(is_int, "int (index)")
        pop_expect(is_ref, "array reference")
    elif mn == "newarray":
        pop_expect(is_int, "int (count)")
        atype = ops["value"]
        names = {4: "[Z", 5: "[C", 6: "[F", 7: "[D", 8: "[B",
                 9: "[S", 10: "[I", 11: "[J"}
        if atype not in names:
            raise VerifyError(pc, "illegal newarray atype",
                              {"atype": atype})
        push(("ref", names[atype]))
    elif mn == "anewarray":
        pop_expect(is_int, "int (count)")
        classname = cf.pool.class_name(ops["cp"])
        push(("ref", "[" + _array_component(classname)))
    elif mn == "arraylength":
        pop_expect(is_ref, "array reference")
        push(INT)

    # ---- branches --------------------------------------------------------
    elif mn in INT_BRANCH1:
        pop_expect(is_int, "int")
        return ins.branch_targets, False
    elif mn in REF_BRANCH1:
        pop_expect(is_ref, "reference")
        return ins.branch_targets, False
    elif mn in INT_BRANCH2:
        pop_expect(is_int, "int")
        pop_expect(is_int, "int")
        return ins.branch_targets, False
    elif mn in REF_BRANCH2:
        pop_expect(is_ref, "reference")
        pop_expect(is_ref, "reference")
        return ins.branch_targets, False
    elif mn in ("goto", "goto_w", "tableswitch", "lookupswitch"):
        if mn in ("tableswitch", "lookupswitch"):
            pop_expect(is_int, "int (switch key)")
        return ins.branch_targets, True

    # ---- return / throw --------------------------------------------------
    elif mn == "return":
        if st:
            raise VerifyError(pc, "return with non-empty stack",
                              {"stack_height": len(st)})
        return [], True
    elif mn == "athrow":
        need_stack(1)
        t = st[-1]
        if t[0] == "uninit":
            raise VerifyError(
                pc, "athrow of an uninitialized object is not allowed",
                {"new_pc": t[1], "class": t[2]})
        if not is_ref(t):
            raise VerifyError(pc, "athrow requires a throwable reference",
                              {"found": type_str(t)})
        pop()
        return [], True

    # ---- object lifecycle ------------------------------------------------
    elif mn == "new":
        classname = cf.pool.class_name(ops["cp"])
        if classname.startswith("["):
            raise VerifyError(pc, "new cannot create an array type "
                                  "(use newarray/anewarray)",
                              {"class": classname})
        push(uninit(pc, classname))
    elif mn == "invokespecial":
        _do_invokespecial_init(pc, ins, st, lv, cf)
    elif mn == "checkcast":
        need_stack(1)
        if st[-1][0] == "uninit":
            raise VerifyError(
                pc, "checkcast cannot act on an uninitialized object",
                {"new_pc": st[-1][1]})
        pop_expect(is_ref, "reference")
        push(("ref", cf.pool.class_name(ops["cp"])))
    elif mn == "instanceof":
        need_stack(1)
        if st[-1][0] == "uninit":
            raise VerifyError(
                pc, "instanceof cannot act on an uninitialized object",
                {"new_pc": st[-1][1]})
        pop_expect(is_ref, "reference")
        push(INT)
    elif mn in ("monitorenter", "monitorexit"):
        need_stack(1)
        t = st[-1]
        if t[0] == "uninit":
            raise VerifyError(
                pc, "monitor operation on uninitialized object",
                {"new_pc": t[1]})
        pop_expect(is_ref, "reference")
    elif mn == "nop":
        pass
    else:  # pragma: no cover - rejected at decode time via layout 'X'
        raise VerifyError(pc, "unsupported instruction: %s" % mn,
                          {"mnemonic": mn})

    # Conditional branches fall through AND branch (targets already parsed).
    if mn in BRANCH_OPS:
        return ins.branch_targets, False
    return [], False


def _check_max(st, max_stack, pc, mn):
    if len(st) > max_stack:
        raise VerifyError(pc, "operand stack exceeds max_stack",
                          {"max_stack": max_stack, "mnemonic": mn})


def _array_component(classname):
    # Class entries for anewarray are always plain types here.
    return "L%s;" % classname


def _do_invokespecial_init(pc, ins, st, lv, cf):
    ref = cf.pool.methodref(ins.operands["cp"])
    if ref["name"] != "<init>":
        raise VerifyError(
            pc, "only invokespecial <init> is supported",
            {"invoked": ref["owner"] + "." + ref["name"] +
                        ref["descriptor"]})
    args, ret = parse_method_descriptor(ref["descriptor"], pc)
    if ret != ("void",):
        raise VerifyError(pc, "<init> descriptor must return void",
                          {"descriptor": ref["descriptor"]})

    if len(st) < len(args) + 1:
        raise VerifyError(pc, "operand stack underflow at invokespecial",
                          {"needed": len(args) + 1, "have": len(st)})
    for i, a in enumerate(reversed(args)):
        t = st[len(st) - 1 - i]
        if a[0] == "ref":
            ok = t[0] in ("ref", "null")
            label = "initialized reference"
        elif a[0] == "cat2":
            raise VerifyError(pc, "category-2 constructor argument "
                                  "outside supported subset")
        else:
            ok = t[0] == ("float" if a == ("float",) else "int")
            label = "float" if a == ("float",) else "int"
        if not ok:
            raise VerifyError(
                pc, "wrong constructor argument type",
                {"argument_index": len(args) - 1 - i, "expected": label,
                 "found": type_str(t)})
    receiver_pos = len(st) - len(args) - 1
    receiver = st[receiver_pos]
    if receiver[0] != "uninit":
        raise VerifyError(
            pc, "invokespecial <init> receiver is not an uninitialized "
                "object from a new instruction",
            {"found": type_str(receiver)})
    new_pc, new_class = receiver[1], receiver[2]
    # A direct <init> on a fresh object always initializes it to the class
    # that ``new`` named; the super-class hierarchy cannot be reconstructed
    # from the class file alone, so the owner descriptor is not required to
    # name new_class itself (it may be an unresolvable super class). The
    # critical invariant is receiver identity, enforced above.

    del st[receiver_pos:]
    initialized = ("ref", new_class)
    for i, t in enumerate(lv):
        if t[0] == "uninit" and t[1] == new_pc:
            lv[i] = initialized
    for i, t in enumerate(st):
        if t[0] == "uninit" and t[1] == new_pc:
            st[i] = initialized


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def build_report(cf, method, code_obj, instrs, states):
    offsets = []
    for pc in sorted(instrs):
        ins = instrs[pc]
        exc = []
        for e in code_obj.exception_table:
            if e.covers(pc):
                exc.append({
                    "start_pc": e.start_pc, "end_pc": e.end_pc,
                    "handler_pc": e.handler_pc,
                    "catch_class": e.catch_name or "*",
                    "handler_entry_stack": [
                        e.catch_name or "java/lang/Throwable"],
                })
        operands = dict(ins.operands)
        if ins.mnemonic in TERMINATORS:
            succ = list(ins.branch_targets)  # goto / switches
            falls = False
        else:
            succ = list(ins.branch_targets)
            falls = pc + ins.length < len(code_obj.code)
            if falls:
                succ.append(pc + ins.length)
        row = {
            "pc": pc,
            "opcode": ins.opcode,
            "mnemonic": ins.mnemonic,
            "length": ins.length,
            "operands": _jsonable(operands),
            "normal_successors": sorted(set(succ)),
            "falls_through": falls,
            "exception_edges": exc,
        }
        frame = states.get(pc)
        if frame is None:
            # Decoded and structurally checked, but no control-flow edge
            # carries a type state here.
            row.update(reachable=False, stack_in=None,
                       stack_height=None, locals_in=None)
        else:
            row.update(reachable=True,
                       stack_in=[type_str(t) for t in frame.stack],
                       stack_height=len(frame.stack),
                       locals_in=[type_str(t) for t in frame.locals])
        offsets.append(row)

    handler_entries = []
    for e in code_obj.exception_table:
        entry = {
            "start_pc": e.start_pc, "end_pc": e.end_pc,
            "handler_pc": e.handler_pc,
            "catch_class": e.catch_name or "* (finally)",
        }
        if e.handler_pc in states:
            f = states[e.handler_pc]
            entry["reachable"] = True
            entry["entry_stack"] = [type_str(t) for t in f.stack]
            entry["entry_locals"] = [type_str(t) for t in f.locals]
        else:
            entry["reachable"] = False
        handler_entries.append(entry)

    return {
        "class_name": cf.this_class,
        "super_class": cf.super_class,
        "class_file_version": (cf.major, cf.minor),
        "method": method.name + method.descriptor,
        "max_stack": code_obj.max_stack,
        "max_locals": code_obj.max_locals,
        "code_length": len(code_obj.code),
        "result": "pass",
        "instruction_count": len(instrs),
        "offsets": offsets,
        "exception_handlers": handler_entries,
    }


def _jsonable(v):
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v
