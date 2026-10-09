"""minisign-compatible Ed25519 signatures (https://jedisct1.github.io/minisign/).

Signature files:
    untrusted comment: <text>
    base64(alg[2] || key_id[8] || signature[64])      alg "ED" = BLAKE2b-512 prehashed, "Ed" = legacy
    trusted comment: <text>
    base64(global_signature[64])                       over signature || trusted_comment
Public keys:
    untrusted comment: <text>
    base64("Ed" || key_id[8] || public_key[32])
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from cygnus.core.errors import CygnusError


class SignatureError(CygnusError):
    pass


@dataclass(frozen=True, slots=True)
class PublicKey:
    key_id: bytes
    raw: bytes

    @property
    def key_id_hex(self) -> str:
        return self.key_id[::-1].hex().upper()  # minisign prints the little-endian key number

    def serialize(self, comment: str = "minisign public key") -> str:
        blob = base64.b64encode(b"Ed" + self.key_id + self.raw).decode()
        return f"untrusted comment: {comment} {self.key_id_hex}\n{blob}\n"


def _b64(line: str, expected: int, what: str) -> bytes:
    try:
        data = base64.b64decode(line.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SignatureError(f"malformed {what}") from exc
    if len(data) != expected:
        raise SignatureError(f"malformed {what}: wrong length")
    return data


def parse_public_key(text: str) -> PublicKey:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if len(lines) == 2 and lines[0].startswith("untrusted comment:"):
        lines = lines[1:]
    if len(lines) != 1:
        raise SignatureError("malformed public key")
    data = _b64(lines[0], 42, "public key")
    if data[:2] != b"Ed":
        raise SignatureError("unsupported public key algorithm")
    return PublicKey(key_id=data[2:10], raw=data[10:])


@dataclass(frozen=True, slots=True)
class VerifiedSignature:
    key_id_hex: str
    trusted_comment: str


def verify(message: bytes, signature_text: str, key: PublicKey) -> VerifiedSignature:
    lines = signature_text.strip().splitlines()
    if len(lines) != 4 or not lines[0].startswith("untrusted comment:") or not lines[2].startswith("trusted comment: "):
        raise SignatureError("malformed signature file")
    sig_blob = _b64(lines[1], 74, "signature")
    alg, key_id, sig = sig_blob[:2], sig_blob[2:10], sig_blob[10:]
    if key_id != key.key_id:
        raise SignatureError("signature was made with a different key")
    if alg == b"ED":
        signed = hashlib.blake2b(message, digest_size=64).digest()
    elif alg == b"Ed":
        signed = message
    else:
        raise SignatureError("unsupported signature algorithm")
    pub = Ed25519PublicKey.from_public_bytes(key.raw)
    trusted = lines[2][len("trusted comment: "):]
    global_sig = _b64(lines[3], 64, "global signature")
    try:
        pub.verify(sig, signed)
        pub.verify(global_sig, sig + trusted.encode())
    except InvalidSignature as exc:
        raise SignatureError("signature verification failed") from exc
    return VerifiedSignature(key_id_hex=key.key_id_hex, trusted_comment=trusted)


def generate_keypair() -> tuple[Ed25519PrivateKey, PublicKey]:
    priv = Ed25519PrivateKey.generate()
    from cryptography.hazmat.primitives import serialization

    raw = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return priv, PublicKey(key_id=os.urandom(8), raw=raw)


def sign(message: bytes, priv: Ed25519PrivateKey, key: PublicKey, trusted_comment: str,
         untrusted_comment: str = "signature from cygnus") -> str:
    if "\n" in trusted_comment:
        raise ValueError("trusted comment must be a single line")
    sig = priv.sign(hashlib.blake2b(message, digest_size=64).digest())
    global_sig = priv.sign(sig + trusted_comment.encode())
    return (f"untrusted comment: {untrusted_comment}\n"
            f"{base64.b64encode(b'ED' + key.key_id + sig).decode()}\n"
            f"trusted comment: {trusted_comment}\n"
            f"{base64.b64encode(global_sig).decode()}\n")
