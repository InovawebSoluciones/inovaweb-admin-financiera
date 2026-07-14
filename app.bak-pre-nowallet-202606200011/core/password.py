"""Hashing de passwords con Argon2id (OWASP recommended)."""

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

# parametros OWASP 2024 (memoria 64 MiB, 3 iter, 4 lanes)
_ph = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,
    parallelism=4,
    hash_len=32,
    salt_len=16,
)


def hash_password(plain: str) -> str:
    return _ph.hash(plain)


def verify_password(hashed: str, plain: str) -> bool:
    try:
        _ph.verify(hashed, plain)
        return True
    except VerifyMismatchError:
        return False


def needs_rehash(hashed: str) -> bool:
    return _ph.check_needs_rehash(hashed)
