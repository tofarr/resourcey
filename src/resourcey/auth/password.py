"""Password hashing using bcrypt with per-hash salts.

Uses the ``bcrypt`` package directly rather than passlib: passlib 1.7 is
incompatible with bcrypt 5.x. bcrypt natively truncates passwords at 72 bytes;
to support arbitrarily long passphrases we pre-hash with SHA-256 first when the
UTF-8 encoding exceeds 72 bytes (the standard ``bcrypt_sha256`` construction).

Stored values are ``bcrypt`` hash strings (``$2b$…``); they are never
reversible to plaintext. Verification re-runs bcrypt over the candidate
password.

Ported from ohev2's ``util/password.py``.
"""

from __future__ import annotations

import hashlib

import bcrypt

_BCRYPT_MAX_BYTES = 72


def _normalize(plaintext: str) -> bytes:
    """Encode a plaintext password to bytes, pre-hashing if over 72 bytes."""
    raw = plaintext.encode("utf-8")
    if len(raw) > _BCRYPT_MAX_BYTES:
        raw = hashlib.sha256(raw).digest()
    return raw


def hash_password(plaintext: str) -> str:
    """Return a bcrypt salted hash of *plaintext* (never the plaintext)."""
    return bcrypt.hashpw(_normalize(plaintext), bcrypt.gensalt()).decode("utf-8")


def verify_password(plaintext: str, hashed: str) -> bool:
    """Return True iff *plaintext* hashes to *hashed*.

    Returns False (never raises) on malformed hashes or non-matching input so
    callers can treat all credential failures uniformly.
    """
    try:
        return bcrypt.checkpw(_normalize(plaintext), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False
