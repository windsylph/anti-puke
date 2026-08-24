"""Python mirror of overlay/protocol.h.

Kept deliberately small and dependency-free. tests/test_protocol.py parses the
C++ header and asserts the two definitions still agree, so this cannot silently
drift from the renderer.
"""

from __future__ import annotations

import struct
from typing import Iterable, NamedTuple, Optional, Tuple

PROTOCOL_VERSION = 1

MAGIC_HELLO = 0x4C48444D  # "MDHL"
MAGIC_INFO = 0x4E49444D   # "MDIN"
MAGIC_DOTS = 0x544F444D   # "MDOT"

MAX_DOTS = 256

# "<" everywhere: little-endian, no padding. Matches the packed structs in C++.
_HELLO = struct.Struct("<IHH")        # magic, version, pad
_INFO = struct.Struct("<IHHII")       # magic, version, pad, width, height
_DOTS_HEADER = struct.Struct("<IHHf")  # magic, version, count, radius
_DOT = struct.Struct("<fff")           # x, y, alpha

HELLO_SIZE = _HELLO.size
INFO_SIZE = _INFO.size
DOTS_HEADER_SIZE = _DOTS_HEADER.size
DOT_SIZE = _DOT.size


class Dot(NamedTuple):
    x: float
    y: float
    alpha: float


def encode_hello() -> bytes:
    return _HELLO.pack(MAGIC_HELLO, PROTOCOL_VERSION, 0)


def decode_info(payload: bytes) -> Optional[Tuple[int, int]]:
    """Return (width, height) from an INFO packet, or None if it is not one."""
    if len(payload) < INFO_SIZE:
        return None
    magic, version, _pad, width, height = _INFO.unpack_from(payload, 0)
    if magic != MAGIC_INFO or version != PROTOCOL_VERSION:
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def encode_dots(dots: Iterable[Dot], radius: float) -> bytes:
    """Serialise a dot frame. Silently truncates at MAX_DOTS -- the renderer
    rejects anything larger, so sending it would just drop the whole frame."""
    dots = list(dots)[:MAX_DOTS]
    out = bytearray(_DOTS_HEADER.pack(MAGIC_DOTS, PROTOCOL_VERSION, len(dots), float(radius)))
    for dot in dots:
        out += _DOT.pack(float(dot.x), float(dot.y), float(dot.alpha))
    return bytes(out)


def decode_dots(payload: bytes) -> Optional[Tuple[float, list]]:
    """Inverse of encode_dots. Only used by the tests and the loopback harness,
    but keeping it here means the two directions are defined side by side."""
    if len(payload) < DOTS_HEADER_SIZE:
        return None
    magic, version, count, radius = _DOTS_HEADER.unpack_from(payload, 0)
    if magic != MAGIC_DOTS or version != PROTOCOL_VERSION:
        return None
    if count > MAX_DOTS:
        return None
    if len(payload) < DOTS_HEADER_SIZE + count * DOT_SIZE:
        return None
    dots = [
        Dot(*_DOT.unpack_from(payload, DOTS_HEADER_SIZE + i * DOT_SIZE))
        for i in range(count)
    ]
    return radius, dots
