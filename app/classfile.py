"""Minimal JVM .class parser: constant pool and Code attributes.

Only what the verifier needs is decoded, but the parser is strict about
truncation: every read goes through :class:`Reader`, which raises
``ClassFormatError`` with the class-file byte offset at which data ran out
or a malformed construct was found.
"""

import struct

from .errors import ClassFormatError

# Constant pool tags
CONSTANT_Utf8 = 1
CONSTANT_Integer = 3
CONSTANT_Float = 4
CONSTANT_Long = 5
CONSTANT_Double = 6
CONSTANT_Class = 7
CONSTANT_String = 8
CONSTANT_Fieldref = 9
CONSTANT_Methodref = 10
CONSTANT_InterfaceMethodref = 11
CONSTANT_NameAndType = 12
CONSTANT_MethodHandle = 15
CONSTANT_MethodType = 16
CONSTANT_Dynamic = 17
CONSTANT_InvokeDynamic = 18
CONSTANT_Module = 19
CONSTANT_Package = 20

TAG_NAMES = {
    1: "Utf8", 3: "Integer", 4: "Float", 5: "Long", 6: "Double",
    7: "Class", 8: "String", 9: "Fieldref", 10: "Methodref",
    11: "InterfaceMethodref", 12: "NameAndType", 15: "MethodHandle",
    16: "MethodType", 17: "Dynamic", 18: "InvokeDynamic",
    19: "Module", 20: "Package",
}


class Reader:
    def __init__(self, data):
        self.data = data
        self.pos = 0
        self.n = len(data)

    def _need(self, size):
        if self.pos + size > self.n:
            raise ClassFormatError(
                self.n,
                "truncated class file",
                {"needed_at": self.pos, "needed_bytes": size,
                 "file_size": self.n},
            )

    def u1(self):
        self._need(1)
        v = self.data[self.pos]
        self.pos += 1
        return v

    def u2(self):
        self._need(2)
        v = struct.unpack_from(">H", self.data, self.pos)[0]
        self.pos += 2
        return v

    def u4(self):
        self._need(4)
        v = struct.unpack_from(">I", self.data, self.pos)[0]
        self.pos += 4
        return v

    def bytes(self, size):
        self._need(size)
        v = self.data[self.pos:self.pos + size]
        self.pos += size
        return v

    def at(self, pos, fn):
        """Run ``fn`` with the reader positioned at ``pos``; restore on exit."""
        saved = self.pos
        self.pos = pos
        try:
            return fn()
        finally:
            self.pos = saved


class ConstantPool:
    """Resolved constant pool, 1-indexed (long/double occupy one entry)."""

    def __init__(self, entries):
        self.entries = entries  # dict index -> dict

    def __contains__(self, index):
        return index in self.entries

    def raw(self, index):
        if index == 0 or index not in self.entries:
            raise ClassFormatError(0, "invalid constant pool index",
                                   {"index": index})
        return self.entries[index]

    def tag(self, index):
        return self.raw(index)["tag"]

    def utf8(self, index):
        e = self.raw(index)
        if e["tag"] != CONSTANT_Utf8:
            raise ClassFormatError(
                e["_at"], "expected CONSTANT_Utf8",
                {"index": index, "got_tag": TAG_NAMES.get(e["tag"], e["tag"])})
        return e["text"]

    def class_name(self, index):
        e = self.raw(index)
        if e["tag"] != CONSTANT_Class:
            raise ClassFormatError(
                e.get("_at", 0), "expected CONSTANT_Class",
                {"index": index, "got_tag": TAG_NAMES.get(e["tag"], e["tag"])})
        return self.utf8(e["name_index"])

    def _ref(self, index, tags, label):
        e = self.raw(index)
        if e["tag"] not in tags:
            raise ClassFormatError(
                e.get("_at", 0), "expected %s" % label,
                {"index": index, "got_tag": TAG_NAMES.get(e["tag"], e["tag"])})
        return e

    def methodref(self, index):
        e = self._ref(index, (CONSTANT_Methodref,
                              CONSTANT_InterfaceMethodref), "Methodref")
        owner = self.class_name(e["class_index"])
        nat = self.raw(e["name_and_type_index"])
        if nat["tag"] != CONSTANT_NameAndType:
            raise ClassFormatError(nat.get("_at", 0),
                                   "expected NameAndType in methodref")
        name = self.utf8(nat["name_index"])
        desc = self.utf8(nat["descriptor_index"])
        return {"owner": owner, "name": name, "descriptor": desc,
                "interface": e["tag"] == CONSTANT_InterfaceMethodref}

    def fieldref(self, index):
        e = self._ref(index, (CONSTANT_Fieldref,), "Fieldref")
        owner = self.class_name(e["class_index"])
        nat = self.raw(e["name_and_type_index"])
        return {"owner": owner, "name": self.utf8(nat["name_index"]),
                "descriptor": self.utf8(nat["descriptor_index"])}

    def name_and_type_desc(self, index):
        e = self.raw(index)
        if e["tag"] != CONSTANT_NameAndType:
            raise ClassFormatError(e.get("_at", 0), "expected NameAndType")
        return self.utf8(e["descriptor_index"])


def _read_constant_pool(r):
    count = r.u2()
    entries = {}
    i = 1
    while i < count:
        at = r.pos
        tag = r.u1()
        if tag == CONSTANT_Utf8:
            length = r.u2()
            raw = r.bytes(length)
            try:
                text = raw.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                raise ClassFormatError(at, "malformed modified-UTF8 Utf8")
            entries[i] = {"tag": tag, "text": text, "_at": at}
        elif tag in (CONSTANT_Integer, CONSTANT_Float):
            entries[i] = {"tag": tag, "value": r.u4(), "_at": at}
        elif tag in (CONSTANT_Long, CONSTANT_Double):
            hi = r.u4()
            lo = r.u4()
            entries[i] = {"tag": tag, "high": hi, "low": lo, "_at": at}
            # Historical JVM: long/double take two slots.
            i += 1
        elif tag in (CONSTANT_Class, CONSTANT_String, CONSTANT_MethodType,
                     CONSTANT_Module, CONSTANT_Package):
            entries[i] = {"tag": tag, "name_index": r.u2(), "_at": at}
        elif tag == CONSTANT_NameAndType:
            entries[i] = {"tag": tag, "name_index": r.u2(),
                          "descriptor_index": r.u2(), "_at": at}
        elif tag in (CONSTANT_Dynamic, CONSTANT_InvokeDynamic):
            entries[i] = {"tag": tag, "bootstrap_index": r.u2(),
                          "name_and_type_index": r.u2(), "_at": at}
        elif tag in (CONSTANT_Fieldref, CONSTANT_Methodref,
                     CONSTANT_InterfaceMethodref):
            entries[i] = {"tag": tag,
                          "class_index": r.u2(),
                          "name_and_type_index": r.u2(), "_at": at}
        elif tag == CONSTANT_MethodHandle:
            entries[i] = {"tag": tag, "reference_kind": r.u1(),
                          "reference_index": r.u2(), "_at": at}
        else:
            raise ClassFormatError(
                at, "unknown constant pool tag",
                {"tag": tag, "constant_index": i})
        i += 1
    return ConstantPool(entries)


class ExceptionEntry:
    __slots__ = ("start_pc", "end_pc", "handler_pc", "catch_class", "catch_name")

    def __init__(self, start_pc, end_pc, handler_pc, catch_class, catch_name):
        self.start_pc = start_pc
        self.end_pc = end_pc
        self.handler_pc = handler_pc
        self.catch_class = catch_class
        self.catch_name = catch_name

    def covers(self, pc):
        return self.start_pc <= pc < self.end_pc


class Code:
    def __init__(self, max_stack, max_locals, code_bytes, exception_table,
                 attrs, raw_length):
        self.max_stack = max_stack
        self.max_locals = max_locals
        self.code = code_bytes
        self.exception_table = exception_table
        self.attributes = attrs
        self.stack_map_offsets = _stack_map_offsets(
            attrs.get("StackMapTable", []))
        self.raw_length = raw_length  # byte length of the Code attribute


class MethodInfo:
    def __init__(self, access_flags, name, descriptor, code):
        self.access_flags = access_flags
        self.name = name
        self.descriptor = descriptor
        self.code = code


class ClassFile:
    def __init__(self, data):
        self.data = data
        r = Reader(data)
        self._parse(r)

    def _parse(self, r):
        start = r.pos
        magic = r.u4()
        if magic != 0xCAFEBABE:
            raise ClassFormatError(0, "bad magic (not a class file)",
                                   {"magic": "0x%08x" % magic})
        self.minor = r.u2()
        self.major = r.u2()
        self.pool = _read_constant_pool(r)
        self.access_flags = r.u2()
        self.this_class = self.pool.class_name(r.u2())
        super_idx = r.u2()
        self.super_class = (self.pool.class_name(super_idx)
                            if super_idx != 0 else None)
        interfaces_count = r.u2()
        self.interfaces = [self.pool.class_name(r.u2())
                           for _ in range(interfaces_count)]
        # fields
        fields_count = r.u2()
        for _ in range(fields_count):
            r.u2(); r.u2(); r.u2()  # access, name, descriptor
            _skip_attributes(r, self.pool)
        # methods
        methods_count = r.u2()
        self.methods = []
        for _ in range(methods_count):
            af = r.u2()
            name = self.pool.utf8(r.u2())
            desc = self.pool.utf8(r.u2())
            code = None
            attr_count = r.u2()
            for _j in range(attr_count):
                attr_name = self.pool.utf8(r.u2())
                attr_len = r.u4()
                attr_start = r.pos
                if attr_name == "Code":
                    code = self._parse_code(r)
                    if r.pos != attr_start + attr_len:
                        raise ClassFormatError(
                            attr_start, "Code attribute length mismatch")
                else:
                    r.bytes(attr_len)
                if r.pos != attr_start + attr_len:
                    raise ClassFormatError(
                        attr_start, "attribute overran declared length",
                        {"attribute": attr_name})
            self.methods.append(MethodInfo(af, name, desc, code))
        # class-level attributes (skip, but validate lengths)
        _skip_attributes(r, self.pool)
        self.eof_offset = r.pos

    def _parse_code(self, r):
        code_attr_start = r.pos - 6  # name_index(2)+length(4) already consumed
        max_stack = r.u2()
        max_locals = r.u2()
        code_length = r.u4()
        if code_length < 1:
            raise ClassFormatError(r.pos, "code_length must be >= 1")
        if code_length > 0xFFFF:
            raise ClassFormatError(r.pos, "code_length too large")
        code_bytes = r.bytes(code_length)
        ex_count = r.u2()
        table = []
        for _ in range(ex_count):
            start_pc = r.u2()
            end_pc = r.u2()
            handler_pc = r.u2()
            catch_idx = r.u2()
            catch_name = None
            if catch_idx != 0:
                catch_name = self.pool.class_name(catch_idx)
            table.append(ExceptionEntry(start_pc, end_pc, handler_pc,
                                        catch_idx, catch_name))
        nested = _read_attributes(r, self.pool)
        raw_length = r.pos - code_attr_start
        return Code(max_stack, max_locals, code_bytes, table, nested,
                    raw_length)


def _read_attributes(r, pool):
    attrs = {}
    count = r.u2()
    for _ in range(count):
        name = pool.utf8(r.u2())
        length = r.u4()
        body = r.bytes(length)
        attrs.setdefault(name, []).append(body)
    return attrs


def _stack_map_offsets(attributes):
    offsets = []
    for data in attributes:
        r = Reader(data)
        count = r.u2()
        previous = -1
        for _ in range(count):
            frame_type = r.u1()
            if frame_type <= 63:
                delta = frame_type
            elif 64 <= frame_type <= 127:
                delta = frame_type - 64
                item_type = r.u1()
                if item_type > 6:
                    raise ClassFormatError(
                        r.pos - 1, "unsupported StackMapTable stack item",
                        {"verification_type": item_type})
            else:
                raise ClassFormatError(
                    r.pos - 1, "unsupported StackMapTable frame",
                    {"frame_type": frame_type})
            previous += delta + 1
            offsets.append(previous)
        if r.pos != r.n:
            raise ClassFormatError(r.pos, "StackMapTable length mismatch",
                                   {"length": r.n})
    return offsets


def _skip_attributes(r, pool):
    count = r.u2()
    for _ in range(count):
        pool.utf8(r.u2())
        length = r.u4()
        r.bytes(length)
