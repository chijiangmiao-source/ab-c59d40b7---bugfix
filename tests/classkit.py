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

        out = struct.pack(">I", 0xCAFEBABE)
        out += struct.pack(">HH", self.minor, self.major)
        out += self.cp.render()
        out += struct.pack(">H", 0x0001)  # public
        out += struct.pack(">HHH", this_idx, super_idx, 0)
        out += struct.pack(">H", 0)       # fields
        out += struct.pack(">H", len(self.methods))
        for m in self.methods:
            m.patch()
            code_attr = self._code_attr(m)
            out += struct.pack(">HHH", m.access,
                               self.cp.utf8(m.name),
                               self.cp.utf8(m.descriptor))
            out += struct.pack(">H", 1)
            out += struct.pack(">H", code_name)
            out += struct.pack(">I", len(code_attr))
            out += code_attr
        out += struct.pack(">H", 0)       # class attributes
        return bytes(out)

    def _code_attr(self, m):
        out = struct.pack(">HHI", m.max_stack, m.max_locals, len(m.code))
        out += bytes(m.code)
        out += struct.pack(">H", len(m.exception_table))
        for kind, sl, el, hl, catch_idx in m.exception_table:
            if kind == "lbl":
                sp, ep, hp = m.labels[sl], m.labels[el], m.labels[hl]
            else:
                sp, ep, hp = sl, el, hl
            out += struct.pack(">HHHH", sp, ep, hp, catch_idx)
        out += struct.pack(">H", 0)
        return out
