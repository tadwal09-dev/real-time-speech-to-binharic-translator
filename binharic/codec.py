"""Framing and encryption of text into bytes.

Frame layout (all big-endian):

    flags      1 byte   0xB0 | mode_id << 1 | encrypted
    [nonce]    6 bytes  only when encrypted: random, never reused in practice
    [tag]      6 bytes  only when encrypted: authentication tag
    length     2 bytes  payload length
    payload    N bytes  UTF-8 text (encrypted when the flag is set)
    crc        2 bytes  CRC-16/CCITT over everything above (catches noise)

Encryption is encrypt-then-MAC with standard building blocks:

* the passphrase is stretched with scrypt (N=2^14, r=8) into a 256-bit
  encryption key and a 256-bit MAC key - done once per passphrase and
  cached, about 0.1 s, so guessing passphrases offline is slow
* the text is XORed with HMAC-SHA256(enc_key, nonce || counter) blocks
* the tag is HMAC-SHA256(mac_key, header || ciphertext), first 48 bits: a
  wrong key, a forged or a changed message is rejected, never shown as garbage

Only the standard library, so every platform (Linux, Windows, iSH) speaks
the same. Not audited; a long passphrase is what keeps it strong. The
message length is visible: use decoys to hide it.
"""

from __future__ import annotations

import binascii
import functools
import hashlib
import hmac
import os
from dataclasses import dataclass

MAGIC = 0xB0
MAX_PAYLOAD = 1024
NONCE_LEN = 6
TAG_LEN = 6
KDF_SALT = b"binharic-v2 kdf"


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


@functools.lru_cache(maxsize=16)
def _keys(passphrase: str) -> tuple[bytes, bytes]:
    try:
        k = hashlib.scrypt(passphrase.encode("utf-8"), salt=KDF_SALT, n=2 ** 14, r=8, p=1,
                           maxmem=64 * 1024 * 1024, dklen=64)
    except (AttributeError, ValueError) as e:  # Python built without OpenSSL scrypt
        raise RuntimeError(f"this Python can't do scrypt key stretching ({e})") from None
    return k[:32], k[32:]


def _keystream(enc_key: bytes, nonce: bytes, n: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < n:
        out += hmac.new(enc_key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:n])


def _tag(mac_key: bytes, header: bytes, ciphertext: bytes) -> bytes:
    return hmac.new(mac_key, header + ciphertext, hashlib.sha256).digest()[:TAG_LEN]


def _xor(data: bytes, stream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, stream))


def _finish(head: bytes, payload: bytes) -> bytes:
    body = head + payload
    return body + binascii.crc_hqx(body, 0xFFFF).to_bytes(2, "big")


def build_frame(text: str, mode_id: int, key: str | None = None, nonce: bytes | None = None) -> bytes:
    payload = text.encode("utf-8")[:MAX_PAYLOAD]
    flags = bytes([MAGIC | (mode_id << 1) | int(bool(key))])
    length = len(payload).to_bytes(2, "big")
    if not key:
        return _finish(flags + length, payload)
    enc_key, mac_key = _keys(key)
    nonce = nonce if nonce is not None else os.urandom(NONCE_LEN)
    ct = _xor(payload, _keystream(enc_key, nonce, len(payload)))
    tag = _tag(mac_key, flags + nonce + length, ct)
    return _finish(flags + nonce + tag + length, ct)


def build_decoy(mode_id: int, length: int) -> bytes:
    """A frame that looks exactly like an encrypted message (valid magic,
    nonce, tag field, length and CRC) but carries random bytes. Without the
    key it can't be told apart from a real one; with the key its tag fails,
    so the receiver drops it."""
    flags = bytes([MAGIC | (mode_id << 1) | 1])
    return _finish(flags + os.urandom(NONCE_LEN + TAG_LEN) + length.to_bytes(2, "big"), os.urandom(length))


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
    nonce = tag = b""
    if encrypted:
        if len(data) < pos + NONCE_LEN + TAG_LEN:
            raise NeedMore(pos + NONCE_LEN + TAG_LEN)
        nonce = data[pos:pos + NONCE_LEN]
        tag = data[pos + NONCE_LEN:pos + NONCE_LEN + TAG_LEN]
        pos += NONCE_LEN + TAG_LEN
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
        if not key:
            return Message(None, frame_mode, True, locked=True, raw=payload), total
        enc_key, mac_key = _keys(key)
        header = data[:1] + nonce + data[pos - 2:pos]
        if not hmac.compare_digest(tag, _tag(mac_key, header, payload)):
            # wrong key, a decoy, or tampered with: never show it as text
            return Message(None, frame_mode, True, locked=True, raw=payload), total
        payload = _xor(payload, _keystream(enc_key, nonce, len(payload)))
    return Message(payload.decode("utf-8", errors="replace"), frame_mode, encrypted, raw=payload), total


CHUNK_BYTES = 240  # per frame: each frame has its own sync chirp, so long text re-syncs often


def split_text(text: str, max_bytes: int = CHUNK_BYTES) -> list[str]:
    """Split text into pieces of at most ``max_bytes`` UTF-8 bytes, at spaces
    where possible and never inside a character."""
    chunks: list[str] = []
    cur = ""
    for word in text.split():
        while len(word.encode("utf-8")) > max_bytes:  # a monster word: cut it at a character boundary
            if cur:
                chunks.append(cur)
                cur = ""
            cut = max_bytes
            while len(word[:cut].encode("utf-8")) > max_bytes:
                cut -= 1
            chunks.append(word[:cut])
            word = word[cut:]
        candidate = f"{cur} {word}" if cur else word
        if len(candidate.encode("utf-8")) > max_bytes:
            chunks.append(cur)
            cur = word
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return chunks


def to_binary(text: str) -> str:
    return " ".join(f"{b:08b}" for b in text.encode("utf-8"))


def to_hex(text: str) -> str:
    return " ".join(f"{b:02X}" for b in text.encode("utf-8"))


def from_binary(s: str) -> str:
    bits = "".join(c for c in s if c in "01")
    if len(bits) % 8:
        raise ValueError(f"{len(bits)} bits isn't a whole number of bytes (8 bits each)")
    return bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)).decode("utf-8", "replace")


def from_hex(s: str) -> str:
    digits = "".join(c for c in s if c in "0123456789abcdefABCDEF")
    if len(digits) % 2:
        raise ValueError(f"{len(digits)} hex digits isn't a whole number of bytes (2 digits each)")
    return bytes.fromhex(digits).decode("utf-8", "replace")
