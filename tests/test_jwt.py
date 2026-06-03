"""Emision y decodificacion de tokens JWT."""

from app.core.jwt_auth import decode, issue_access, issue_refresh


def test_access_token_roundtrip():
    tok = issue_access(42, ["finanzas"], client_id=None)
    p = decode(tok)
    assert p["sub"] == "42"
    assert p["typ"] == "access"
    assert "finanzas" in p["roles"]


def test_refresh_token_type():
    tok = issue_refresh(7)
    p = decode(tok)
    assert p["typ"] == "refresh"
