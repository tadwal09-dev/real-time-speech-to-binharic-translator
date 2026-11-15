"""Framing, error correction and encryption of text into bytes.

Frame layout (version 2, all big-endian):

    header     2 bytes  flags (0xC0 | mode_id << 1 | encrypted), payload length
    header rs  4 bytes  Reed-Solomon parity: the header survives 2 wrong bytes
    [nonce]    6 bytes  only when encrypted: random, never reused in practice
    [tag]      6 bytes  only when encrypted: authentication tag
    payload    N bytes  UTF-8 text (encrypted when the flag is set), N <= 186
    crc        2 bytes  CRC-16/CCITT over the header and everything since
    body rs    P bytes  Reed-Solomon parity over nonce..crc: about a quarter
                        more sound, and one wrong byte in eight is repaired

A voice call loses packets and stretches or squeezes the sound to keep up;
each of those turns a tone or two into the wrong one. Version 1 frames (flags
0xB0, a 2-byte length, no parity) died of a single wrong tone. They are still
read, so a friend who hasn't updated yet can be understood, but never sent.

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

MAGIC = 0xC0
MAGIC_V1 = 0xB0     # frames from before error correction: read, never sent
MAX_PAYLOAD = 186   # so that nonce + tag + payload + crc + parity fit one Reed-Solomon block (255)
MAX_PAYLOAD_V1 = 1024
NONCE_LEN = 6
TAG_LEN = 6
HEADER_PARITY = 4
HEADER_LEN = 2 + HEADER_PARITY
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
    repaired: int = 0     # bytes that arrived wrong and were put right

    def display(self) -> str:
        if self.locked:
            return f"[encrypted, {len(self.raw)} bytes: {self.raw.hex(' ')}]"
        return self.text or ""


# --------------------------------------------------------------------------
# Reed-Solomon over GF(256) (polynomial 0x11D, generator 2, first root 2^0).
# A codeword is data + parity; position i stands for x^(n-1-i).

_EXP = [0] * 510
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 510):
    _EXP[_i] = _EXP[_i - 255]


class Uncorrectable(Exception):
    pass


def _mul(a: int, b: int) -> int:
    return _EXP[_LOG[a] + _LOG[b]] if a and b else 0


def _div(a: int, b: int) -> int:
    return _EXP[(_LOG[a] - _LOG[b]) % 255] if a else 0


def _poly_mul(p: list[int], q: list[int]) -> list[int]:
    """Coefficients lowest power first."""
    out = [0] * (len(p) + len(q) - 1)
    for i, a in enumerate(p):
        if a:
            for j, b in enumerate(q):
                out[i + j] ^= _mul(a, b)
    return out


def _horner(coeffs, x: int) -> int:
    """The polynomial with ``coeffs`` (highest power first) at ``x``."""
    if not x:
        return coeffs[-1] if len(coeffs) else 0
    y, lx = 0, _LOG[x]
    for c in coeffs:
        y = (_EXP[_LOG[y] + lx] if y else 0) ^ c
    return y


def _poly_eval(p: list[int], x: int) -> int:
    return _horner(p[::-1], x)


@functools.cache
def _generator(nsym: int) -> tuple[int, ...]:
    g = [1]
    for i in range(nsym):
        g = _poly_mul(g, [_EXP[i], 1])
    return tuple(reversed(g))  # highest power first, monic


def rs_parity(data: bytes, nsym: int) -> bytes:
    if len(data) + nsym > 255:
        raise ValueError("Reed-Solomon block longer than 255 bytes")
    gen = _generator(nsym)
    gen_log = [_LOG[g] for g in gen[1:]]  # the generator has no zero coefficients
    rem = [0] * nsym
    for byte in data:
        f = byte ^ rem[0]
        rem = rem[1:] + [0]
        if f:
            lf = _LOG[f]
            rem = [r ^ _EXP[g + lf] for r, g in zip(rem, gen_log)]
    return bytes(rem)


def _syndromes(block, nsym: int) -> list[int]:
    return [_horner(block, _EXP[j]) for j in range(nsym)]


def rs_correct(block: bytes, nsym: int, erasures: tuple[int, ...] | list[int] = ()) -> tuple[bytes, int]:
    """Repair a codeword (data + ``nsym`` parity bytes). ``erasures`` are positions
    known to be unreliable. Repairs e wrong bytes and f erasures when 2e + f <= nsym.

    Returns (data, bytes changed). Raises Uncorrectable.
    """
    n = len(block)
    if n > 255 or n <= nsym:
        raise Uncorrectable("bad block length")
    synd = _syndromes(block, nsym)
    if not any(synd):
        return bytes(block[:n - nsym]), 0
    erasures = sorted({e for e in erasures if 0 <= e < n})
    if len(erasures) > nsym:
        raise Uncorrectable("too many erasures")
    # erasure locator: a root at X^-1 for every erased position X = 2^(n-1-i)
    gamma = [1]
    for e in erasures:
        gamma = _poly_mul(gamma, [1, _EXP[n - 1 - e]])
    # what the erasures can't explain is left for unknown errors (Berlekamp-Massey)
    forney = _poly_mul(synd, gamma)[len(erasures):nsym]
    lam, prev, length, shift, last = [1], [1], 0, 1, 1
    for k, s in enumerate(forney):
        d = s
        for i in range(1, length + 1):
            if i < len(lam):
                d ^= _mul(lam[i], forney[k - i])
        if d == 0:
            shift += 1
            continue
        coef = _div(d, last)
        new = lam + [0] * (len(prev) + shift - len(lam))
        for i, c in enumerate(prev):
            new[i + shift] ^= _mul(coef, c)
        if 2 * length <= k:
            prev, last, length, shift = lam, d, k + 1 - length, 1
        else:
            shift += 1
        lam = new
    lam = lam[:length + 1]
    if 2 * length + len(erasures) > nsym:
        raise Uncorrectable("too many errors")
    psi = _poly_mul(lam, gamma)
    while len(psi) > 1 and psi[-1] == 0:
        psi.pop()
    inverse = [_EXP[(255 - (n - 1 - i)) % 255] for i in range(n)]  # of each position's X
    high_first = psi[::-1]
    where = [i for i in range(n) if _horner(high_first, inverse[i]) == 0]
    if len(where) != len(psi) - 1:
        raise Uncorrectable("error positions don't add up")
    omega = _poly_mul(synd, psi)[:nsym]
    deriv = [psi[i] if i % 2 else 0 for i in range(1, len(psi))]  # formal derivative (characteristic 2)
    fixed = bytearray(block)
    for i in where:
        den = _poly_eval(deriv, inverse[i])
        if den == 0:
            raise Uncorrectable("can't size an error")
        fixed[i] ^= _mul(_EXP[n - 1 - i], _div(_poly_eval(omega, inverse[i]), den))
    if any(_syndromes(fixed, nsym)):
        raise Uncorrectable("repair didn't hold")
    return bytes(fixed[:n - nsym]), sum(a != b for a, b in zip(fixed, block))


def parity_len(body: int) -> int:
    """Parity bytes for ``body`` data bytes: 4, plus 2 for every 8 (one wrong byte in 8 is repaired)."""
    return 4 + 2 * -(-body // 8)


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


def _seal(key: str, nonce: bytes | None, flags: bytes, length: bytes, payload: bytes) -> tuple[bytes, bytes, bytes]:
    """Encrypt ``payload``: (nonce, tag, ciphertext). The tag covers flags, nonce, length and ciphertext."""
    enc_key, mac_key = _keys(key)
    nonce = nonce if nonce is not None else os.urandom(NONCE_LEN)
    ct = _xor(payload, _keystream(enc_key, nonce, len(payload)))
    return nonce, _tag(mac_key, flags + nonce + length, ct), ct


def _open(key: str | None, flags: bytes, length: bytes, nonce: bytes, tag: bytes, ct: bytes) -> bytes | None:
    """The text of an encrypted frame, or None: no key, the wrong key, a decoy,
    or tampered with. Those are never shown as text."""
    if not key:
        return None
    enc_key, mac_key = _keys(key)
    if not hmac.compare_digest(tag, _tag(mac_key, flags + nonce + length, ct)):
        return None
    return _xor(ct, _keystream(enc_key, nonce, len(ct)))


def _message(frame_mode: int, encrypted: bool, payload: bytes, plain: bytes | None, repaired: int = 0) -> Message:
    if plain is None:
        return Message(None, frame_mode, True, locked=True, raw=payload, repaired=repaired)
    return Message(plain.decode("utf-8", errors="replace"), frame_mode, encrypted, raw=plain, repaired=repaired)


def _crc(data: bytes) -> bytes:
    return binascii.crc_hqx(data, 0xFFFF).to_bytes(2, "big")


def _assemble(flags: int, body: bytes, length: int) -> bytes:
    """header + its parity + body (nonce, tag, payload) + crc + the body's parity"""
    head = bytes([flags, length])
    body += _crc(head + body)
    return head + rs_parity(head, HEADER_PARITY) + body + rs_parity(body, parity_len(len(body)))


def build_frame(text: str, mode_id: int, key: str | None = None, nonce: bytes | None = None,
                version: int = 2) -> bytes:
    if version == 1:
        return _build_frame_v1(text, mode_id, key, nonce)
    payload = text.encode("utf-8")[:MAX_PAYLOAD]
    flags = MAGIC | (mode_id << 1) | int(bool(key))
    if not key:
        return _assemble(flags, payload, len(payload))
    return _assemble(flags, b"".join(_seal(key, nonce, bytes([flags]), bytes([len(payload)]), payload)),
                     len(payload))


def build_decoy(mode_id: int, length: int) -> bytes:
    """A frame that looks exactly like an encrypted message (valid header,
    nonce, tag field, length, checksum and parity) but carries random bytes.
    Without the key it can't be told apart from a real one; with the key its
    tag fails, so the receiver drops it."""
    length = max(0, min(length, MAX_PAYLOAD))
    return _assemble(MAGIC | (mode_id << 1) | 1, os.urandom(NONCE_LEN + TAG_LEN + length), length)


def _header(data: bytes) -> tuple[int, int] | None:
    """(flags, length) if ``data`` starts with a version 2 header, repaired if need be."""
    try:
        head, _ = rs_correct(data[:HEADER_LEN], HEADER_PARITY)
    except Uncorrectable:
        return None
    if head[0] & 0xF0 != MAGIC or head[1] > MAX_PAYLOAD:
        return None
    return head[0], head[1]


def parse_frame(data: bytes, key: str | None = None, mode_id: int | None = None,
                doubt: list[float] | None = None) -> tuple[Message, int]:
    """Parse a frame from the start of ``data``.

    ``doubt``, one number per byte, says how unsure the receiver was of each
    (higher = less sure): when plain repair fails, the most doubtful bytes are
    treated as missing, which repairs up to twice as many.

    Returns (message, bytes_consumed). Raises NeedMore if ``data`` is a
    truncated but plausible frame and BadFrame if it cannot be one.
    """
    if not data:
        raise NeedMore(1)
    old = data[0] & 0xF0 == MAGIC_V1
    if len(data) < HEADER_LEN:
        if old:
            return _parse_frame_v1(data, key, mode_id)  # asks for what it needs
        raise NeedMore(HEADER_LEN)
    head = _header(data)
    if head is None:
        if old:
            return _parse_frame_v1(data, key, mode_id)
        raise BadFrame("bad magic")
    flags, length = head
    frame_mode = (flags >> 1) & 0x07
    if mode_id is not None and frame_mode != mode_id:
        raise BadFrame("mode mismatch")
    encrypted = bool(flags & 1)
    extra = NONCE_LEN + TAG_LEN if encrypted else 0
    body_len = extra + length + 2
    nsym = parity_len(body_len)
    total = HEADER_LEN + body_len + nsym
    if len(data) < total:
        raise NeedMore(total)
    block = data[HEADER_LEN:total]
    head_bytes = bytes([flags, length])
    repaired = sum(a != b for a, b in zip(head_bytes + rs_parity(head_bytes, HEADER_PARITY), data[:HEADER_LEN]))

    def attempt(erasures=()):
        try:
            body, fixed = rs_correct(block, nsym, erasures)
        except Uncorrectable:
            return None
        if _crc(head_bytes + body[:-2]) != body[-2:]:
            return None  # "repaired" into a different valid block: too damaged
        return body[:-2], fixed

    got = attempt()
    if got is None and doubt is not None and len(doubt) >= total:
        # the least certain bytes first; try treating more and more of them as missing
        order = sorted(range(len(block)), key=lambda i: -doubt[HEADER_LEN + i])
        for count in sorted({nsym // 4, nsym // 2, 3 * nsym // 4, nsym - 2} - {0}):
            got = attempt(order[:count])
            if got is not None:
                break
    if got is None:
        raise BadFrame("crc mismatch")
    body, fixed = got
    repaired += fixed
    payload = body[extra:]
    plain = payload
    if encrypted:
        plain = _open(key, bytes([flags]), bytes([length]), body[:NONCE_LEN], body[NONCE_LEN:extra], payload)
    return _message(frame_mode, encrypted, payload, plain, repaired), total


# -- version 1 (no error correction): read for friends who haven't updated ----

def _finish_v1(head: bytes, payload: bytes) -> bytes:
    body = head + payload
    return body + _crc(body)


def _build_frame_v1(text: str, mode_id: int, key: str | None = None, nonce: bytes | None = None) -> bytes:
    payload = text.encode("utf-8")[:MAX_PAYLOAD_V1]
    flags = bytes([MAGIC_V1 | (mode_id << 1) | int(bool(key))])
    length = len(payload).to_bytes(2, "big")
    if not key:
        return _finish_v1(flags + length, payload)
    nonce, tag, ct = _seal(key, nonce, flags, length, payload)
    return _finish_v1(flags + nonce + tag + length, ct)


def _parse_frame_v1(data: bytes, key: str | None = None, mode_id: int | None = None) -> tuple[Message, int]:
    flags = data[0]
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
    if length > MAX_PAYLOAD_V1:
        raise BadFrame("length out of range")
    total = pos + length + 2
    if len(data) < total:
        raise NeedMore(total)
    body = data[:pos + length]
    if _crc(body) != data[pos + length:total]:
        raise BadFrame("crc mismatch")
    payload = data[pos:pos + length]
    plain = _open(key, data[:1], data[pos - 2:pos], nonce, tag, payload) if encrypted else payload
    return _message(frame_mode, encrypted, payload, plain), total


CHUNK_BYTES = 160  # per frame: each frame has its own sync chirp, so long text re-syncs often


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
