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
                 attrs, raw_length, stack_map_frames, stack_map_count):
        self.max_stack = max_stack
        self.max_locals = max_locals
        self.code = code_bytes
        self.exception_table = exception_table
        self.attributes = attrs
        # StackMapTable frames are fully parsed here (strict: truncated or
        # otherwise malformed tables are rejected at parse time); the
        # verifier additionally checks their bytecode offsets and the
        # declared types against work-queue propagation.
        self.stack_map_frames = stack_map_frames
        self.stack_map_count = stack_map_count
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
        smt = nested.get("StackMapTable", [])
        if len(smt) > 1:
            raise ClassFormatError(
                0, "multiple StackMapTable attributes in Code attribute",
                {"count": len(smt)})
        frames = []
        for body, body_start in smt:
            frames.extend(
                parse_stack_map_table(body, body_start, self.pool))
        return Code(max_stack, max_locals, code_bytes, table, nested,
                    raw_length, frames, len(smt))


def _read_attributes(r, pool):
    """Return ``{name: [(body, body_offset), ...]}``.

    ``body_offset`` is the class-file byte offset at which each attribute
    body begins, so strict sub-parsers (e.g. StackMapTable) can report
    class-file-relative evidence.
    """
    attrs = {}
    count = r.u2()
    for _ in range(count):
        name = pool.utf8(r.u2())
        length = r.u4()
        body_start = r.pos
        body = r.bytes(length)
        attrs.setdefault(name, []).append((body, body_start))
    return attrs


# ---------------------------------------------------------------------------
# StackMapTable (JVMS 4.7.4) - full decoding of every frame form and all
# seven verification type tags.
# ---------------------------------------------------------------------------

# verification_type_info tags
VT_TOP = 0
VT_INTEGER = 1
VT_FLOAT = 2
VT_DOUBLE = 3
VT_LONG = 4
VT_NULL = 5
VT_UNINITIALIZED_THIS = 6
VT_OBJECT = 7
VT_UNINITIALIZED = 8

VT_SINGLETONS = {
    VT_TOP: ("top",),
    VT_INTEGER: ("int",),
    VT_FLOAT: ("float",),
    VT_DOUBLE: ("double",),
    VT_LONG: ("long",),
    VT_NULL: ("null",),
    VT_UNINITIALIZED_THIS: ("uninitthis",),
}


class _AttrReader(Reader):
    """Reader whose truncation errors carry a class-file-relative offset."""

    def __init__(self, data, base_offset):
        super().__init__(data)
        self._base = base_offset

    def _need(self, size):
        if self.pos + size > self.n:
            raise ClassFormatError(
                self._base + self.pos,
                "truncated class attribute",
                {"needed_at": self._base + self.pos,
                 "needed_bytes": size, "attribute_size": self.n})


class StackMapFrame:
    """One stack_map_frame. ``locals`` is already the full effective local
    list of the frame (same/chop/append resolved against the previous
    frame); ``stack`` holds the explicit stack items; ``chopped`` records
    how many locals a chop frame dropped (0 for other forms)."""

    __slots__ = ("frame_type", "offset_delta", "locals", "stack",
                 "chopped", "attr_offset", "pc")

    def __init__(self, frame_type, offset_delta, locals_, stack, chopped,
                 attr_offset):
        self.frame_type = frame_type
        self.offset_delta = offset_delta
        self.locals = locals_
        self.stack = stack
        self.chopped = chopped
        self.attr_offset = attr_offset  # class-file offset of frame type byte
        self.pc = -1                    # resolved bytecode pc (verifier)


def _read_object_vt(r, pool, base):
    pos = r.pos
    cpi = r.u2()
    try:
        e = pool.raw(cpi)
    except ClassFormatError:
        raise ClassFormatError(
            base + pos,
            "StackMapTable Object_variable_info references an invalid "
            "constant pool index",
            {"constant_pool_index": cpi})
    if e["tag"] != CONSTANT_Class:
        raise ClassFormatError(
            base + pos,
            "StackMapTable Object_variable_info must reference "
            "CONSTANT_Class",
            {"constant_pool_index": cpi,
             "got_tag": TAG_NAMES.get(e["tag"], e["tag"])})
    try:
        name = pool.utf8(e["name_index"])
    except ClassFormatError:
        raise ClassFormatError(
            base + pos,
            "StackMapTable Object_variable_info Class entry has an invalid "
            "name_index",
            {"constant_pool_index": cpi,
             "name_index": e["name_index"]})
    return ("ref", name)


def _read_verification_type(r, pool, base):
    tag_pos = r.pos
    tag = r.u1()
    if tag in VT_SINGLETONS:
        return VT_SINGLETONS[tag]
    if tag == VT_OBJECT:
        return _read_object_vt(r, pool, base)
    if tag == VT_UNINITIALIZED:
        return ("uninit", r.u2())
    raise ClassFormatError(
        base + tag_pos,
        "invalid StackMapTable verification_type_info tag",
        {"tag": tag})


def parse_stack_map_table(body, base_offset, pool):
    """Fully parse one StackMapTable attribute body.

    Raises ClassFormatError (offset relative to the start of the class
    file) on truncation, reserved/unknown frame types, bad verification
    tags or invalid constant pool references.
    """
    r = _AttrReader(body, base_offset)
    count = r.u2()
    frames = []
    prev_locals = []
    for _ in range(count):
        frame_at = r.pos
        ft = r.u1()
        if ft <= 63:                                  # same_frame
            frame = StackMapFrame(ft, ft, list(prev_locals), [], 0,
                                  base_offset + frame_at)
        elif ft <= 127:                               # same_locals_1_stack
            stack = [_read_verification_type(r, pool, base_offset)]
            frame = StackMapFrame(ft, ft - 64, list(prev_locals), stack, 0,
                                  base_offset + frame_at)
        elif ft <= 246:
            raise ClassFormatError(
                base_offset + frame_at,
                "reserved StackMapTable frame type (128..246)",
                {"frame_type": ft})
        elif ft == 247:                               # ..._item_extended
            delta = r.u2()
            stack = [_read_verification_type(r, pool, base_offset)]
            frame = StackMapFrame(ft, delta, list(prev_locals), stack, 0,
                                  base_offset + frame_at)
        elif ft <= 250:                               # chop_frame
            delta = r.u2()
            chopped = 251 - ft
            if chopped > len(prev_locals):
                raise ClassFormatError(
                    base_offset + frame_at,
                    "StackMapTable chop_frame removes more locals than the "
                    "previous frame declares",
                    {"frame_type": ft, "chopped": chopped,
                     "previous_locals": len(prev_locals)})
            kept = prev_locals[:len(prev_locals) - chopped]
            frame = StackMapFrame(ft, delta, kept, [], chopped,
                                  base_offset + frame_at)
        elif ft == 251:                               # same_frame_extended
            delta = r.u2()
            frame = StackMapFrame(ft, delta, list(prev_locals), [], 0,
                                  base_offset + frame_at)
        elif ft <= 254:                               # append_frame
            delta = r.u2()
            k = ft - 251
            new_locals = list(prev_locals)
            for _i in range(k):
                new_locals.append(
                    _read_verification_type(r, pool, base_offset))
            frame = StackMapFrame(ft, delta, new_locals, [], 0,
                                  base_offset + frame_at)
        else:                                         # full_frame (255)
            delta = r.u2()
            nlocals = r.u2()
            locals_ = [_read_verification_type(r, pool, base_offset)
                       for _i in range(nlocals)]
            nstack = r.u2()
            stack = [_read_verification_type(r, pool, base_offset)
                     for _i in range(nstack)]
            frame = StackMapFrame(ft, delta, locals_, stack, 0,
                                  base_offset + frame_at)
        frames.append(frame)
        prev_locals = frame.locals
    if r.pos != r.n:
        raise ClassFormatError(
            base_offset + r.pos,
            "trailing bytes after StackMapTable frames",
            {"consumed": r.pos, "attribute_length": r.n})
    return frames


def _skip_attributes(r, pool):
    count = r.u2()
    for _ in range(count):
        pool.utf8(r.u2())
        length = r.u4()
        r.bytes(length)
