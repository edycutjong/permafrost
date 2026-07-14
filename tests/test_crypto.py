"""Ed25519 signatures + ECIES sealed boxes (pynacl)."""

import pytest

from permafrost.crypto import (
    SignatureInvalid,
    dev_keys,
    generate_sealing_private,
    generate_signing_seed,
    seal,
    sealing_public_of,
    sign,
    unseal,
    verify_key_of,
    verify_signature,
)


def test_sign_verify_roundtrip():
    seed = generate_signing_seed()
    sig = sign(b"payload", seed)
    assert verify_signature(b"payload", sig, verify_key_of(seed))


def test_wrong_key_fails():
    sig = sign(b"payload", generate_signing_seed())
    assert not verify_signature(b"payload", sig, verify_key_of(generate_signing_seed()))


def test_tampered_payload_fails():
    seed = generate_signing_seed()
    sig = sign(b"payload", seed)
    assert not verify_signature(b"payloae", sig, verify_key_of(seed))


def test_tampered_signature_fails():
    seed = generate_signing_seed()
    sig = sign(b"payload", seed)
    bad = ("0" if sig[0] != "0" else "1") + sig[1:]
    assert not verify_signature(b"payload", bad, verify_key_of(seed))


def test_garbage_signature_fails_not_raises():
    assert not verify_signature(b"x", "zz-not-hex", verify_key_of(generate_signing_seed()))


def test_seal_unseal_roundtrip():
    priv = generate_sealing_private()
    sealed = seal(b"secret event batch", sealing_public_of(priv))
    assert unseal(sealed, priv) == b"secret event batch"


def test_seal_wrong_recipient_fails():
    sealed = seal(b"secret", sealing_public_of(generate_sealing_private()))
    with pytest.raises(SignatureInvalid):
        unseal(sealed, generate_sealing_private())


def test_sealed_tamper_fails():
    priv = generate_sealing_private()
    sealed = bytearray(seal(b"secret", sealing_public_of(priv)))
    sealed[len(sealed) // 2] ^= 0xFF
    with pytest.raises(SignatureInvalid):
        unseal(bytes(sealed), priv)


def test_sealing_is_randomized_but_decrypts_same():
    priv = generate_sealing_private()
    pub = sealing_public_of(priv)
    a, b = seal(b"same", pub), seal(b"same", pub)
    assert a != b  # ephemeral sender key
    assert unseal(a, priv) == unseal(b, priv) == b"same"


def test_dev_keys_deterministic_and_consistent():
    k1, k2 = dev_keys(), dev_keys()
    assert k1 == k2
    assert verify_key_of(k1.signing_seed) == k1.verify_key
    assert sealing_public_of(k1.sealing_private) == k1.sealing_public


def test_empty_payload_signable():
    seed = generate_signing_seed()
    assert verify_signature(b"", sign(b"", seed), verify_key_of(seed))


def test_hex_key_formats():
    k = dev_keys()
    for h in (k.signing_seed, k.verify_key, k.sealing_private, k.sealing_public):
        assert len(h) == 64 and bytes.fromhex(h)
