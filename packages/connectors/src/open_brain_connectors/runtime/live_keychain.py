"""Isolated macOS Keychain helper; secrets enter stdin and never command arguments."""

from __future__ import annotations

import ctypes
import json
import re
import sys
from typing import Any


def _operate(value: dict[str, Any]) -> tuple[int, bytes]:
    reference, operation = value["reference"], value["operation"]
    if (
        sys.platform != "darwin"
        or type(reference) is not str
        or re.fullmatch(r"[a-zA-Z0-9._:-]{1,200}", reference) is None
        or operation not in {"get", "set", "delete", "status"}
    ):
        raise ValueError
    security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    core = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    pointer = ctypes.c_void_p
    size = ctypes.c_uint32
    security.SecKeychainFindGenericPassword.argtypes = [
        pointer,
        size,
        ctypes.c_char_p,
        size,
        ctypes.c_char_p,
        ctypes.POINTER(size),
        ctypes.POINTER(pointer),
        ctypes.POINTER(pointer),
    ]
    security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
    security.SecKeychainAddGenericPassword.argtypes = [
        pointer,
        size,
        ctypes.c_char_p,
        size,
        ctypes.c_char_p,
        size,
        ctypes.c_char_p,
        ctypes.POINTER(pointer),
    ]
    security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
    security.SecKeychainItemModifyAttributesAndData.argtypes = [
        pointer,
        pointer,
        size,
        ctypes.c_char_p,
    ]
    security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
    security.SecKeychainItemDelete.argtypes = [pointer]
    security.SecKeychainItemDelete.restype = ctypes.c_int32
    security.SecKeychainItemFreeContent.argtypes = [pointer, pointer]
    security.SecKeychainItemFreeContent.restype = ctypes.c_int32
    core.CFRelease.argtypes = [pointer]
    core.CFRelease.restype = None
    service, account = b"io.openbrain.sources", reference.encode("ascii")
    length, data, item = size(), pointer(), pointer()
    result = security.SecKeychainFindGenericPassword(
        None,
        len(service),
        service,
        len(account),
        account,
        ctypes.byref(length),
        ctypes.byref(data),
        ctypes.byref(item),
    )
    try:
        if operation == "set":
            payload = value["payload"].encode("utf-8")
            if len(payload) > 65_536:
                raise ValueError
            if result == -25300:  # errSecItemNotFound
                result = security.SecKeychainAddGenericPassword(
                    None,
                    len(service),
                    service,
                    len(account),
                    account,
                    len(payload),
                    payload,
                    None,
                )
            elif result == 0:
                result = security.SecKeychainItemModifyAttributesAndData(
                    item,
                    None,
                    len(payload),
                    payload,
                )
        elif operation == "delete" and result == 0:
            result = security.SecKeychainItemDelete(item)
        if result == -25300:
            return 44, b""
        if result != 0 or length.value > 65_536:
            return 1, b""
        return 0, ctypes.string_at(data, length.value) if operation == "get" else b""
    finally:
        if data:
            security.SecKeychainItemFreeContent(None, data)
        if item:
            core.CFRelease(item)


def main() -> None:
    try:
        raw = sys.stdin.buffer.read(131_073)
        if len(raw) > 131_072:
            raise ValueError
        code, output = _operate(json.loads(raw))
    except Exception:
        code, output = 1, b""
    sys.stdout.buffer.write(output)
    sys.exit(code)


if __name__ == "__main__":
    main()
