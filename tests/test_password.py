"""Hash y verify con Argon2id."""

from app.core.password import hash_password, verify_password


def test_hash_and_verify_roundtrip():
    h = hash_password("super-secret-123")
    assert h.startswith("$argon2")
    assert verify_password(h, "super-secret-123") is True
    assert verify_password(h, "wrong") is False


def test_hash_is_random_salt():
    h1 = hash_password("same")
    h2 = hash_password("same")
    assert h1 != h2
