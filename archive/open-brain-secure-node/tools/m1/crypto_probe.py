"""Measure the frozen M1 cryptographic profile without using Brain data."""

from __future__ import annotations

import importlib.metadata
import json
import platform
import statistics
import sys
import time
from typing import Any

import keyring
from argon2.low_level import Type, hash_secret_raw
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ARGON2_MEMORY_KIB = 65_536
ARGON2_ITERATIONS = 3
ARGON2_PARALLELISM = 4
ARGON2_SALT = bytes.fromhex("1f" * 16)
ARGON2_TAG_BYTES = 32
ARGON2_RUNS = 3


def _argon2_measurements() -> list[float]:
    durations: list[float] = []
    for _ in range(ARGON2_RUNS):
        started = time.perf_counter()
        result = hash_secret_raw(
            secret=b"open-brain-m1-synthetic-passphrase",
            salt=ARGON2_SALT,
            time_cost=ARGON2_ITERATIONS,
            memory_cost=ARGON2_MEMORY_KIB,
            parallelism=ARGON2_PARALLELISM,
            hash_len=ARGON2_TAG_BYTES,
            type=Type.ID,
        )
        durations.append(time.perf_counter() - started)
        if len(result) != ARGON2_TAG_BYTES:
            raise AssertionError("Argon2id returned an unexpected tag length")
    return durations


def _probe() -> dict[str, Any]:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
    message = b"open-brain-m1-crypto-probe"
    signature = private_key.sign(message)
    private_key.public_key().verify(signature, message)

    aesgcm = AESGCM(bytes(range(32)))
    nonce = bytes(range(12))
    ciphertext = aesgcm.encrypt(nonce, message, b"m1-associated-data")
    if aesgcm.decrypt(nonce, ciphertext, b"m1-associated-data") != message:
        raise AssertionError("AES-256-GCM round trip failed")

    local_authentication = "not-applicable"
    if sys.platform == "darwin":
        import LocalAuthentication  # type: ignore[import-untyped] # noqa: F401

        local_authentication = "passed"

    durations = _argon2_measurements()
    return {
        "status": "passed",
        "platform": platform.system().lower(),
        "architecture": platform.machine().lower(),
        "python": platform.python_version(),
        "dependencies": {
            "argon2-cffi": importlib.metadata.version("argon2-cffi"),
            "cryptography": importlib.metadata.version("cryptography"),
            "keyring": importlib.metadata.version("keyring"),
        },
        "checks": {
            "aes_256_gcm": "passed",
            "argon2id": "passed",
            "ed25519": "passed",
            "keyring_import": "passed" if keyring is not None else "failed",
            "local_authentication_import": local_authentication,
        },
        "argon2id": {
            "memory_kib": ARGON2_MEMORY_KIB,
            "iterations": ARGON2_ITERATIONS,
            "parallelism": ARGON2_PARALLELISM,
            "salt_bytes": len(ARGON2_SALT),
            "tag_bytes": ARGON2_TAG_BYTES,
            "runs": ARGON2_RUNS,
            "seconds": [round(duration, 6) for duration in durations],
            "median_seconds": round(statistics.median(durations), 6),
        },
    }


def main() -> None:
    print(json.dumps(_probe(), sort_keys=True))


if __name__ == "__main__":
    main()
