"""Cryptographic primitives (pynacl): Ed25519 signatures + ECIES sealed boxes.

- **Ed25519** signs rule bundles and daily Merkle roots.
- **SealedBox (ECIES: X25519 + XSalsa20-Poly1305)** seals event batches to the
  cloud public key before they ever leave the edge — the queue stores only
  ciphertext.

Dev demo keys
-------------
For the offline/replay demo we derive a *deterministic, obviously non-secret*
keypair from a fixed string so judges can reproduce every byte with zero
provisioning. Production separates these: the signing seed lives only in the
Function Compute environment, the edge ships the public keys
(see PRODUCTION_PLAN / README Status).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from nacl.exceptions import BadSignatureError, CryptoError
from nacl.public import PrivateKey, PublicKey, SealedBox
from nacl.signing import SigningKey, VerifyKey

__all__ = [
    "SignatureInvalid",
    "KeyRing",
    "generate_signing_seed",
    "verify_key_of",
    "sign",
    "verify_signature",
    "generate_sealing_private",
    "sealing_public_of",
    "seal",
    "unseal",
    "dev_keys",
]

_DEV_SIGNING_TAG = b"permafrost dev signing seed v1 -- DEMO ONLY, NOT A SECRET"
_DEV_SEALING_TAG = b"permafrost dev sealing seed v1 -- DEMO ONLY, NOT A SECRET"


class SignatureInvalid(Exception):
    """Raised when a signature check fails."""


# --------------------------------------------------------------------------- Ed25519


def generate_signing_seed() -> str:
    """New random Ed25519 seed (hex)."""
    return SigningKey.generate().encode().hex()


def verify_key_of(signing_seed_hex: str) -> str:
    """Public verify key (hex) for a signing seed."""
    return SigningKey(bytes.fromhex(signing_seed_hex)).verify_key.encode().hex()


def sign(payload: bytes, signing_seed_hex: str) -> str:
    """Detached Ed25519 signature over ``payload`` (hex)."""
    sk = SigningKey(bytes.fromhex(signing_seed_hex))
    return sk.sign(payload).signature.hex()


def verify_signature(payload: bytes, signature_hex: str, verify_key_hex: str) -> bool:
    """True iff ``signature_hex`` is a valid Ed25519 signature by ``verify_key_hex``."""
    try:
        vk = VerifyKey(bytes.fromhex(verify_key_hex))
        vk.verify(payload, bytes.fromhex(signature_hex))
        return True
    except (BadSignatureError, ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- ECIES (SealedBox)


def generate_sealing_private() -> str:
    """New random X25519 private key (hex)."""
    return PrivateKey.generate().encode().hex()


def sealing_public_of(private_hex: str) -> str:
    """X25519 public key (hex) for a private key."""
    return PrivateKey(bytes.fromhex(private_hex)).public_key.encode().hex()


def seal(plaintext: bytes, recipient_public_hex: str) -> bytes:
    """ECIES-seal ``plaintext`` to the recipient public key (anonymous sender)."""
    box = SealedBox(PublicKey(bytes.fromhex(recipient_public_hex)))
    return box.encrypt(plaintext)


def unseal(sealed: bytes, recipient_private_hex: str) -> bytes:
    """Open a sealed box with the recipient private key.

    Raises ``SignatureInvalid`` on any decryption failure (wrong key / tamper).
    """
    try:
        box = SealedBox(PrivateKey(bytes.fromhex(recipient_private_hex)))
        return box.decrypt(sealed)
    except (CryptoError, ValueError) as exc:  # pragma: no cover - message detail
        raise SignatureInvalid(f"sealed batch failed to open: {exc}") from exc


# --------------------------------------------------------------------------- key ring


@dataclass(frozen=True)
class KeyRing:
    """Everything the demo needs in one place.

    Edge holds: ``rules_verify_key`` + ``cloud_sealing_public``.
    Cloud holds: ``signing_seed`` + ``sealing_private``.
    """

    signing_seed: str
    verify_key: str
    sealing_private: str
    sealing_public: str


def dev_keys() -> KeyRing:
    """Deterministic DEMO-ONLY key ring (derived, never stored on disk)."""
    signing_seed = hashlib.sha256(_DEV_SIGNING_TAG).digest().hex()
    sealing_private = hashlib.sha256(_DEV_SEALING_TAG).digest().hex()
    return KeyRing(
        signing_seed=signing_seed,
        verify_key=verify_key_of(signing_seed),
        sealing_private=sealing_private,
        sealing_public=sealing_public_of(sealing_private),
    )
