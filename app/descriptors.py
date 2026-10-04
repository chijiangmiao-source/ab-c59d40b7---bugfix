"""JVM field/method descriptor parsing (only what verification needs).

Reference types are represented by their internal name
(``java/lang/String``); array types keep their full descriptor
(``[Ljava/lang/String;``). Category-2 types (J/D) are rejected upstream
because the supported ()V subset never produces them.
"""

from .errors import VerifyError

PRIMITIVE_INTS = {
    "B": "byte", "C": "char", "I": "int", "S": "short", "Z": "boolean",
}


def parse_method_descriptor(desc, pc=0):
    """Return (argument_types, return_type) for a method descriptor.

    Types are tuples: ("int",), ("ref", name), ("cat2", "J"/"D"),
    ("float",), ("void",).
    """
    if not desc.startswith("("):
        raise VerifyError(pc, "malformed method descriptor", {"desc": desc})
    i = 1
    args = []
    while i < len(desc) and desc[i] != ")":
        t, i = _parse_type(desc, i, pc)
        args.append(t)
    if i >= len(desc):
        raise VerifyError(pc, "malformed method descriptor", {"desc": desc})
    i += 1  # skip ')'
    if i >= len(desc):
        raise VerifyError(pc, "malformed method descriptor", {"desc": desc})
    ret, i = _parse_type(desc, i, pc)
    if i != len(desc):
        raise VerifyError(pc, "malformed method descriptor", {"desc": desc})
    return args, ret


def _parse_type(desc, i, pc):
    c = desc[i]
    if c in PRIMITIVE_INTS:
        return ("int",), i + 1
    if c == "F":
        return ("float",), i + 1
    if c in ("J", "D"):
        return ("cat2", c), i + 1
    if c == "V":
        return ("void",), i + 1
    if c == "L":
        end = desc.find(";", i + 1)
        if end == -1:
            raise VerifyError(pc, "malformed object descriptor",
                              {"desc": desc})
        return ("ref", desc[i + 1:end]), end + 1
    if c == "[":
        j = i + 1
        while j < len(desc) and desc[j] == "[":
            j += 1
        if j >= len(desc):
            raise VerifyError(pc, "malformed array descriptor",
                              {"desc": desc})
        if desc[j] == "L":
            end = desc.find(";", j + 1)
            if end == -1:
                raise VerifyError(pc, "malformed array descriptor",
                                  {"desc": desc})
            return ("ref", desc[i:end + 1]), end + 1
        if desc[j] in PRIMITIVE_INTS or desc[j] in "FD":
            return ("ref", desc[i:j + 1]), j + 1
        raise VerifyError(pc, "malformed array descriptor", {"desc": desc})
    raise VerifyError(pc, "unsupported descriptor character",
                      {"char": c, "desc": desc})


def slot_count(t):
    return 2 if t[0] == "cat2" else 1
