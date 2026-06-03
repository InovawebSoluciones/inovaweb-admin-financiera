"""Smoke test del endpoint /health (sin DB)."""

from fastapi.testclient import TestClient

from app.main import app


def test_liveness():
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
