"""Tiny .class file builder for tests.

Hand-assembles just enough of a class file: static ()V methods with Code
attributes, an exception table and the constant pool entries reachable
from the bytecode. Constant indices are allocated automatically.
"""

import struct


class CPBuilder:
    def __init__(self):
        self._items = []          # list of (key, blob)
        self._index = {}          # key -> 1-based index

    def add(self, key, blob):
        if key in self._index:
            return self._index[key]
        self._items.append(blob)
        idx = len(self._items)
        self._index[key] = idx
        return idx

    def utf8(self, text):
        raw = text.encode("utf-8")
        return self.add(("u", text),
                        bytes([1]) + struct.pack(">H", len(raw)) + raw)

    def class_(self, name):
        return self.add(("C", name),
                        bytes([7]) + struct.pack(">H", self.utf8(name)))

    def name_and_type(self, name, desc):
        key = ("N", name, desc)
        return self.add(
            key, bytes([12]) + struct.pack(
                ">HH", self.utf8(name), self.utf8(desc)))

    def methodref(self, owner, name, desc):
        key = ("M", owner, name, desc)
        return self.add(
            key, bytes([10]) + struct.pack(
                ">HH", self.class_(owner),
                self.name_and_type(name, desc)))

    def string(self, text):
        return self.add(("S", text),
                        bytes([8]) + struct.pack(">H", self.utf8(text)))

    def render(self):
        out = struct.pack(">H", len(self._items) + 1)
        for blob in self._items:
            out += blob
        return out


class MethodBuilder:
    def __init__(self, cp, name, access, descriptor):
        self.cp = cp
        self.name = name
        self.access = access
        self.descriptor = descriptor
        # Pre-allocate so the indices exist before the pool is rendered.
        self._name_idx = cp.utf8(name)
        self._desc_idx = cp.utf8(descriptor)
        self.code = bytearray()
        self.labels = {}
        # Exception table: either ("lbl", s, e, h, catch) or
        # ("raw", s, e, h, catch).
        self.exception_table = []
        # StackMapTable frames (see FRAME_* helpers); None = no attribute.
        self.stack_map_frames = None
        self.max_stack = 8
        self.max_locals = 8
        self._ph = []  # (emitted_pos, width 2|4, label)
        self._switch_ph = []

    # -- structure ---------------------------------------------------------
    def label(self, name):
        self.labels[name] = len(self.code)

    def catch(self, start, end, handler, catch_class=None):
        ci = self.cp.class_(catch_class) if catch_class else 0
        self.exception_table.append(("lbl", start, end, handler, ci))

    def catch_raw(self, start_pc, end_pc, handler_pc, catch_class=None):
        ci = self.cp.class_(catch_class) if catch_class else 0
        self.exception_table.append(
            ("raw", start_pc, end_pc, handler_pc, ci))

    def stack_map(self, frames):
        """Attach a StackMapTable; frames use the FRAME_* helpers below.

        Frame offsets are absolute pcs here; they are converted to
        offset_deltas when the attribute is rendered.
        """
        self.stack_map_frames = frames

    def emit(self, data):
        if isinstance(data, int):
            data = bytes([data])
        self.code += data

    def patch(self):
        for pos, width, label in self._ph:
            target = self.labels[label]
            delta = target - pos
            if width == 2:
                struct.pack_into(">h", self.code, pos + 1, delta)
            else:
                struct.pack_into(">i", self.code, pos + 1, delta)
        for kind, pos, base, labels, keys in self._switch_ph:
            struct.pack_into(">i", self.code, base,
                             self.labels[labels[0]] - pos)
            if kind == "table":
                for i, lbl in enumerate(labels[1:]):
                    struct.pack_into(">i", self.code, base + 12 + 4 * i,
                                     self.labels[lbl] - pos)
            else:
                for i, (key, lbl) in enumerate(zip(keys, labels[1:])):
                    off = base + 8 + 8 * i
                    struct.pack_into(">i", self.code, off, key)
                    struct.pack_into(">i", self.code, off + 4,
                                     self.labels[lbl] - pos)

    # -- instructions ------------------------------------------------------
    def raw(self, data):
        if isinstance(data, int):
            data = bytes([data])
        self.code += data

    def u1(self, op):
        self.emit(op)

    def aconst_null(self):
        self.u1(0x01)

    def iconst(self, v):
        self.u1({-1: 0x02, 0: 0x03, 1: 0x04, 2: 0x05, 3: 0x06,
                 4: 0x07, 5: 0x08}[v])

    def bipush(self, v):
        self.emit(bytes([0x10]) + struct.pack(">b", v))

    def new(self, classname):
        self.emit(bytes([0xBB]) +
                  struct.pack(">H", self.cp.class_(classname)))

    def dup(self):
        self.u1(0x59)

    def pop(self):
        self.u1(0x57)

    def swap(self):
        self.u1(0x5F)

    def return_(self):
        self.u1(0xB1)

    def athrow(self):
        self.u1(0xBF)

    def istore(self, i):
        self.u1(0x3B + i)

    def iload(self, i):
        self.u1(0x1A + i)

    def astore(self, i):
        self.u1(0x4B + i)

    def aload(self, i):
        self.u1(0x2A + i)

    def iadd(self):
        self.u1(0x60)

    def if_icmplt(self, label):
        self._branch2(0xA1, label)

    def ifeq(self, label):
        self._branch2(0x99, label)

    def ifnonnull(self, label):
        self._branch2(0xC7, label)

    def goto(self, label):
        self._branch2(0xA7, label)

    def tableswitch(self, low, cases, default):
        """cases: list of target labels for consecutive keys low..high."""
        pos = len(self.code)
        self.code.append(0xAA)
        pad = (4 - (len(self.code) % 4)) % 4
        self.code += b"\x00" * pad
        base = pos + 1 + pad  # position of default offset
        self.code += struct.pack(">iii", 0, low, low + len(cases) - 1)
        for _ in cases:
            self.code += struct.pack(">i", 0)
        self._switch_ph.append(
            ("table", pos, base, [default] + list(cases), None))

    def lookupswitch(self, pairs, default):
        """pairs: list of (signed match key, label), sorted & unique."""
        pos = len(self.code)
        self.code.append(0xAB)
        pad = (4 - (len(self.code) % 4)) % 4
        self.code += b"\x00" * pad
        base = pos + 1 + pad
        self.code += struct.pack(">ii", 0, len(pairs))
        for _ in pairs:
            self.code += struct.pack(">ii", 0, 0)
        self._switch_ph.append(
            ("lookup", pos, base, [default] + [l for _, l in pairs],
             [k for k, _ in pairs]))

    def invokespecial_init(self, owner, desc="()V"):
        idx = self.cp.methodref(owner, "<init>", desc)
        self.emit(bytes([0xB7]) + struct.pack(">H", idx))

    def ldc_string(self, text):
        self.emit(bytes([0x12, self.cp.string(text)]))

    def _branch2(self, opcode, label):
        pos = len(self.code)
        self.emit(bytes([opcode, 0, 0]))
        self._ph.append((pos, 2, label))


class ClassBuilder:
    def __init__(self, name="Test", super_name="java/lang/Object",
                 major=52, minor=0):
        self.cp = CPBuilder()
        self.name = name
        self.super_name = super_name
        self.major = major
        self.minor = minor
        self.methods = []

    def method(self, name="verify", access=0x0009, descriptor="()V"):
        m = MethodBuilder(self.cp, name, access, descriptor)
        self.methods.append(m)
        return m

    def build(self):
        this_idx = self.cp.class_(self.name)
        super_idx = self.cp.class_(self.super_name)
        code_name = self.cp.utf8("Code")
        # Render method attribute bodies up front: they may allocate
        # constant-pool entries, which must exist before the pool renders.
        smt_name = None
        smt_bodies = {}
        for m in self.methods:
            m.patch()
            if m.stack_map_frames is not None:
                if smt_name is None:
                    smt_name = self.cp.utf8("StackMapTable")
                smt_bodies[id(m)] = _render_stack_map(
                    self.cp, m.stack_map_frames)

        out = struct.pack(">I", 0xCAFEBABE)
        out += struct.pack(">HH", self.minor, self.major)
        out += self.cp.render()
        out += struct.pack(">H", 0x0001)  # public
        out += struct.pack(">HHH", this_idx, super_idx, 0)
        out += struct.pack(">H", 0)       # fields
        out += struct.pack(">H", len(self.methods))
        for m in self.methods:
            code_attr = self._code_attr(
                m, smt_name, smt_bodies.get(id(m)))
            out += struct.pack(">HHH", m.access,
                               self.cp.utf8(m.name),
                               self.cp.utf8(m.descriptor))
            out += struct.pack(">H", 1)
            out += struct.pack(">H", code_name)
            out += struct.pack(">I", len(code_attr))
            out += code_attr
        out += struct.pack(">H", 0)       # class attributes
        return bytes(out)

    def _code_attr(self, m, smt_name=None, smt_body=None):
        out = struct.pack(">HHI", m.max_stack, m.max_locals, len(m.code))
        out += bytes(m.code)
        out += struct.pack(">H", len(m.exception_table))
        for kind, sl, el, hl, catch_idx in m.exception_table:
            if kind == "lbl":
                sp, ep, hp = m.labels[sl], m.labels[el], m.labels[hl]
            else:
                sp, ep, hp = sl, el, hl
            out += struct.pack(">HHHH", sp, ep, hp, catch_idx)
        attrs = b""
        attr_count = 0
        if smt_body is not None:
            attrs += struct.pack(">HI", smt_name, len(smt_body)) + smt_body
            attr_count += 1
        out += struct.pack(">H", attr_count) + attrs
        return out


# ---------------------------------------------------------------------------
# StackMapTable construction helpers
# ---------------------------------------------------------------------------
#
# Verification types mirror the tuples decoded by app.classfile:
#   VT_TOP/VT_INT/VT_FLOAT/VT_DOUBLE/VT_LONG/VT_NULL/VT_UNINIT_THIS,
#   VT_OBJECT(name), VT_UNINIT(offset). VT_OBJECT_IDX emits a raw constant
#   pool index without resolving it (for negative tests).

VT_TOP = ("top",)
VT_INT = ("int",)
VT_FLOAT = ("float",)
VT_DOUBLE = ("double",)
VT_LONG = ("long",)
VT_NULL = ("null",)
VT_UNINIT_THIS = ("uninit_this",)


def VT_OBJECT(name):
    return ("object", name)


def VT_OBJECT_IDX(index):
    return ("object_idx", index)


def VT_UNINIT(offset):
    return ("uninit", offset)


# Frame helpers take absolute pcs; deltas are computed at render time.
def FRAME_SAME(offset):
    return ("same", offset)


def FRAME_SAME_1(offset, vtype):
    return ("same_1", offset, vtype)


def FRAME_SAME_1_EXT(offset, vtype):
    return ("same_1x", offset, vtype)


def FRAME_CHOP(offset, count):
    return ("chop", offset, count)


def FRAME_SAME_EXT(offset):
    return ("same_x", offset)


def FRAME_APPEND(offset, vtypes):
    return ("append", offset, list(vtypes))


def FRAME_FULL(offset, locals_, stack):
    return ("full", offset, list(locals_), list(stack))


def FRAME_RAW(offset, data):
    """Pre-encoded frame bytes (negative tests); ``offset`` is only used
    to compute the delta of the *next* frame."""
    return ("raw", offset, data)


def _render_vtype(cp, t):
    tag = t[0]
    if tag == "top":
        return b"\x00"
    if tag == "int":
        return b"\x01"
    if tag == "float":
        return b"\x02"
    if tag == "double":
        return b"\x03"
    if tag == "long":
        return b"\x04"
    if tag == "null":
        return b"\x05"
    if tag == "uninit_this":
        return b"\x06"
    if tag == "object":
        return b"\x07" + struct.pack(">H", cp.class_(t[1]))
    if tag == "object_idx":
        return b"\x07" + struct.pack(">H", t[1])
    if tag == "uninit":
        return b"\x08" + struct.pack(">H", t[1])
    raise ValueError("unknown verification type %r" % (t,))


def _render_stack_map(cp, frames):
    out = struct.pack(">H", len(frames))
    previous = -1
    for fr in frames:
        kind = fr[0]
        offset = fr[1]
        delta = offset - previous - 1
        previous = offset
        if kind == "same":
            if not 0 <= delta <= 63:
                raise ValueError("same_frame delta out of compact range")
            out += bytes([delta])
        elif kind == "same_1":
            if not 0 <= delta <= 63:
                raise ValueError("same_locals_1 delta out of compact range")
            out += bytes([64 + delta]) + _render_vtype(cp, fr[2])
        elif kind == "same_1x":
            out += bytes([247]) + struct.pack(">H", delta) \
                + _render_vtype(cp, fr[2])
        elif kind == "chop":
            if not 1 <= fr[2] <= 3:
                raise ValueError("chop_frame count must be 1..3")
            out += bytes([251 - fr[2]]) + struct.pack(">H", delta)
        elif kind == "same_x":
            out += bytes([251]) + struct.pack(">H", delta)
        elif kind == "append":
            vts = fr[2]
            if not 1 <= len(vts) <= 3:
                raise ValueError("append_frame count must be 1..3")
            out += bytes([251 + len(vts)]) + struct.pack(">H", delta)
            out += b"".join(_render_vtype(cp, t) for t in vts)
        elif kind == "full":
            out += bytes([255]) + struct.pack(">H", delta)
            out += struct.pack(">H", len(fr[2]))
            out += b"".join(_render_vtype(cp, t) for t in fr[2])
            out += struct.pack(">H", len(fr[3]))
            out += b"".join(_render_vtype(cp, t) for t in fr[3])
        elif kind == "raw":
            out += fr[2]
        else:
            raise ValueError("unknown frame kind %r" % (kind,))
    return out


# ---------------------------------------------------------------------------
# Canonical fixtures
# ---------------------------------------------------------------------------

_OBJ = "java/lang/Object"
_EX = "java/lang/Exception"


def build_handler_frame_class():
    """The javac-style class from the review request.

    A static ()V method whose protected region constructs an Object and
    then returns normally; the java/lang/Exception handler stores the
    caught reference in a local and returns. The Code attribute carries a
    StackMapTable with an object-typed same_locals_1_stack_item frame at
    the handler entry, exactly as a standard JVM compiler emits it:

        0: new java/lang/Object
        3: dup
        4: invokespecial java/lang/Object.<init>()V
        7: astore_0
        8: goto 12
       11: astore_1            <- handler, frame: stack=[Exception]
       12: return              <- frame: same
       Exception table: [0, 8) -> 11, catch java/lang/Exception
    """
    b = ClassBuilder(name="Diag")
    m = b.method()
    m.label("S")
    m.new(_OBJ)
    m.dup()
    m.invokespecial_init(_OBJ)
    m.astore(0)
    m.label("E")
    m.goto("end")
    m.label("H")
    m.astore(1)
    m.label("end")
    m.return_()
    m.catch("S", "E", "H", _EX)
    m.stack_map([
        FRAME_SAME_1(m.labels["H"], VT_OBJECT(_EX)),
        FRAME_SAME(m.labels["end"]),
    ])
    return b.build()
