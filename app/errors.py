"""Error types carrying stable byte offsets.

Two stages, two error classes:

* ClassFormatError - the class file itself is truncated/malformed. The
  ``offset`` is the byte offset *inside the class file* at which parsing
  failed.
* VerifyError - the class file parsed, but bytecode verification failed.
  The ``offset`` is a bytecode pc (byte offset inside the Code attribute).
"""


class ClassFormatError(Exception):
    def __init__(self, offset, reason, detail=None):
        super().__init__(reason)
        self.offset = offset
        self.reason = reason
        self.detail = detail or {}

    def evidence(self):
        return {
            "stage": "parse",
            "offset": self.offset,
            "reason": self.reason,
            "detail": self.detail,
        }


class VerifyError(Exception):
    def __init__(self, pc, reason, detail=None):
        super().__init__(reason)
        self.pc = pc
        self.reason = reason
        self.detail = detail or {}

    def evidence(self):
        return {
            "stage": "verify",
            "offset": self.pc,
            "reason": self.reason,
            "detail": self.detail,
        }
