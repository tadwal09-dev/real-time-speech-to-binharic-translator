"""Framing and (optional) keyed scrambling of text into bytes.

Frame layout (all big-endian):

    flags      1 byte   0xB0 | mode_id << 1 | encrypted
    [nonce]    4 bytes  only when encrypted
    [keycheck] 2 bytes  only when encrypted
    length     2 bytes  payload length
    payload    N bytes  UTF-8 text (XORed with a keystream when encrypted)
    crc        2 bytes  CRC-16/CCITT over everything above

The keystream is SHA-256(key || nonce || counter). It is enough to keep
casual listeners out; it is not audited cryptography.
"""

from __future__ import annotations

import binascii
import hashlib
import os
from dataclasses import dataclass

MAGIC = 0xB0
MAX_PAYLOAD = 1024
NONCE_LEN = 4
KEYCHECK_LEN = 2


class NeedMore(Exception):
    """The frame is valid so far but more bytes are required."""

    def __init__(self, needed: int):
        super().__init__(f"need {needed} bytes")
        self.needed = needed


class BadFrame(Exception):
    pass


@dataclass
class Message:
    text: str | None
    mode_id: int
    encrypted: bool
    locked: bool = False  # encrypted and we have no (or the wrong) key
    raw: bytes = b""

    def display(self) -> str:
        if self.locked:
            return f"[encrypted, {len(self.raw)} bytes: {self.raw.hex(' ')}]"
        return self.text or ""


def _keystream(key: bytes, nonce: bytes, n: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < n:
        out += hashlib.sha256(key + nonce + counter.to_bytes(4, "big")).digest()
        counter += 1
    return bytes(out[:n])


def _keycheck(key: bytes, nonce: bytes) -> bytes:
    return hashlib.sha256(b"binharic-check" + key + nonce).digest()[:KEYCHECK_LEN]


def _xor(data: bytes, stream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, stream))


def build_frame(text: str, mode_id: int, key: str | None = None, nonce: bytes | None = None) -> bytes:
    payload = text.encode("utf-8")[:MAX_PAYLOAD]
    encrypted = bool(key)
    head = bytearray([MAGIC | (mode_id << 1) | int(encrypted)])
    if encrypted:
        k = key.encode("utf-8")
        nonce = nonce if nonce is not None else os.urandom(NONCE_LEN)
        head += nonce + _keycheck(k, nonce)
        payload = _xor(payload, _keystream(k, nonce, len(payload)))
    head += len(payload).to_bytes(2, "big")
    body = bytes(head) + payload
    return body + binascii.crc_hqx(body, 0xFFFF).to_bytes(2, "big")


def parse_frame(data: bytes, key: str | None = None, mode_id: int | None = None) -> tuple[Message, int]:
    """Parse a frame from the start of ``data``.

    Returns (message, bytes_consumed). Raises NeedMore if ``data`` is a
    truncated but plausible frame and BadFrame if it cannot be one.
    """
    if not data:
        raise NeedMore(1)
    flags = data[0]
    if flags & 0xF0 != MAGIC:
        raise BadFrame("bad magic")
    frame_mode = (flags >> 1) & 0x07
    if mode_id is not None and frame_mode != mode_id:
        raise BadFrame("mode mismatch")
    encrypted = bool(flags & 1)
    pos = 1
    nonce = check = b""
    if encrypted:
        if len(data) < pos + NONCE_LEN + KEYCHECK_LEN:
            raise NeedMore(pos + NONCE_LEN + KEYCHECK_LEN)
        nonce = data[pos:pos + NONCE_LEN]
        check = data[pos + NONCE_LEN:pos + NONCE_LEN + KEYCHECK_LEN]
        pos += NONCE_LEN + KEYCHECK_LEN
    if len(data) < pos + 2:
        raise NeedMore(pos + 2)
    length = int.from_bytes(data[pos:pos + 2], "big")
    pos += 2
    if length > MAX_PAYLOAD:
        raise BadFrame("length out of range")
    total = pos + length + 2
    if len(data) < total:
        raise NeedMore(total)
    body = data[:pos + length]
    if binascii.crc_hqx(body, 0xFFFF) != int.from_bytes(data[pos + length:total], "big"):
        raise BadFrame("crc mismatch")
    payload = data[pos:pos + length]

    if encrypted:
        k = key.encode("utf-8") if key else None
        if not k or _keycheck(k, nonce) != check:
            return Message(None, frame_mode, True, locked=True, raw=payload), total
        payload = _xor(payload, _keystream(k, nonce, len(payload)))
    return Message(payload.decode("utf-8", errors="replace"), frame_mode, encrypted, raw=payload), total


def to_binary(text: str) -> str:
    return " ".join(f"{b:08b}" for b in text.encode("utf-8"))


def to_hex(text: str) -> str:
    return " ".join(f"{b:02X}" for b in text.encode("utf-8"))


def from_binary(s: str) -> str:
    bits = "".join(c for c in s if c in "01")
    return bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits) - len(bits) % 8, 8)).decode("utf-8", "replace")


def from_hex(s: str) -> str:
    return bytes.fromhex("".join(c for c in s if c in "0123456789abcdefABCDEF")).decode("utf-8", "replace")
