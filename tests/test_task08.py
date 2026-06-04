"""TASK-08: tests para CRUD clientes (suspend/reactivate) + API /api/v2.

conftest.py ya parchea psycopg/engine antes de la coleccion. Aqui solo se
importa la app y se usan dependency overrides + mocks para los tests.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.jwt_auth import CurrentUser, current_user
from app.core.database import get_db
from app.main import app


# ── helpers ──────────────────────────────────────────────────────────────────

def _user(role: str, uid: int = 1) -> CurrentUser:
    return CurrentUser(id=uid, roles=[role], client_id=None)


def _db_mock(rows: list):
    """AsyncSession mock cuyo execute() devuelve rows."""
    mapping_result = MagicMock()
    mapping_result.first.return_value = rows[0] if rows else None
    mapping_result.all.return_value = rows

    exec_result = MagicMock()
    exec_result.mappings.return_value = mapping_result
    exec_result.first.return_value = rows[0] if rows else None
    exec_result.rowcount = len(rows)

    db = AsyncMock()
    db.execute = AsyncMock(return_value=exec_result)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.close = AsyncMock()
    return db


def _db_empty():
    return _db_mock([])


# ── fixture ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=True)


# ══════════════════════════════════════════════════════════════════════════════
# suspend / reactivate — solo super_admin
# ══════════════════════════════════════════════════════════════════════════════

def test_suspend_changes_status_and_audits(client):
    """super_admin puede suspender: 303 + write_event action='suspend'."""
    db = _db_mock([{"id": 1}])
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: _user("super_admin")

    with patch("app.routers.admin_router.bind_actor", new=AsyncMock()), \
         patch("app.routers.admin_router.write_event", new=AsyncMock()) as mock_write:
        resp = client.post(
            "/admin/clients/1/suspend",
            data={"reason": "mora"},
            follow_redirects=False,
        )

    app.dependency_overrides.clear()
    assert resp.status_code == 303, resp.text
    mock_write.assert_called_once()
    _, kw = mock_write.call_args
    assert kw["action"] == "suspend"
    assert kw["entity_type"] == "clients"
    assert kw["entity_id"] == 1


def test_suspend_409_when_not_active(client):
    """UPDATE no devuelve fila -> cliente inexistente o no activo -> 409."""
    db = _db_empty()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: _user("super_admin")

    with patch("app.routers.admin_router.bind_actor", new=AsyncMock()):
        resp = client.post(
            "/admin/clients/99/suspend",
            data={"reason": "test"},
            follow_redirects=False,
        )

    app.dependency_overrides.clear()
    assert resp.status_code == 409


def test_reactivate_changes_status_and_audits(client):
    """super_admin puede reactivar: 303 + write_event action='reactivate'."""
    db = _db_mock([{"id": 2}])
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: _user("super_admin")

    with patch("app.routers.admin_router.bind_actor", new=AsyncMock()), \
         patch("app.routers.admin_router.write_event", new=AsyncMock()) as mock_write:
        resp = client.post(
            "/admin/clients/2/reactivate",
            follow_redirects=False,
        )

    app.dependency_overrides.clear()
    assert resp.status_code == 303, resp.text
    mock_write.assert_called_once()
    _, kw = mock_write.call_args
    assert kw["action"] == "reactivate"
    assert kw["new_values"]["status"] == "active"


# ══════════════════════════════════════════════════════════════════════════════
# rol lectura y finanzas -> 403 en suspend y reactivate
# ══════════════════════════════════════════════════════════════════════════════

def test_suspend_403_for_lectura(client):
    app.dependency_overrides[get_db] = lambda: _db_mock([{"id": 1}])
    app.dependency_overrides[current_user] = lambda: _user("lectura")

    resp = client.post(
        "/admin/clients/1/suspend",
        data={"reason": "test"},
        follow_redirects=False,
    )
    app.dependency_overrides.clear()
    assert resp.status_code == 403


def test_reactivate_403_for_lectura(client):
    app.dependency_overrides[get_db] = lambda: _db_mock([{"id": 1}])
    app.dependency_overrides[current_user] = lambda: _user("lectura")

    resp = client.post("/admin/clients/1/reactivate", follow_redirects=False)
    app.dependency_overrides.clear()
    assert resp.status_code == 403


def test_suspend_403_for_finanzas(client):
    """finanzas NO puede suspender (solo super_admin) -> 403."""
    app.dependency_overrides[get_db] = lambda: _db_mock([{"id": 1}])
    app.dependency_overrides[current_user] = lambda: _user("finanzas")

    resp = client.post(
        "/admin/clients/1/suspend",
        data={"reason": "test"},
        follow_redirects=False,
    )
    app.dependency_overrides.clear()
    assert resp.status_code == 403


# ══════════════════════════════════════════════════════════════════════════════
# GET /api/v2/clients/{id}/balance -> llama MedidorClient.get_balance
# ══════════════════════════════════════════════════════════════════════════════

def test_balance_calls_medidor(client):
    """Con wallet provisionada, llama get_balance y devuelve JSON correcto."""
    db = _db_mock([{"medidor_account_id": "wallet-abc", "status": "active"}])
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: _user("super_admin")

    fake_balance = {"balance_cents": 10000, "currency": "MXN"}

    with patch("app.routers.api_router.MedidorClient") as MockMedidor:
        instance = AsyncMock()
        instance.get_balance = AsyncMock(return_value=fake_balance)
        instance.close = AsyncMock()
        MockMedidor.return_value = instance

        resp = client.get("/api/v2/clients/1/balance")

    app.dependency_overrides.clear()
    assert resp.status_code == 200
    body = resp.json()
    assert body["client_id"] == 1
    assert body["wallet_id"] == "wallet-abc"
    assert body["balance_cents"] == 10000
    instance.get_balance.assert_called_once_with("wallet-abc")


def test_balance_no_wallet(client):
    """Sin wallet -> balance_cents=0, sin llamar al Medidor."""
    db = _db_mock([{"medidor_account_id": None, "status": "active"}])
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: _user("lectura")

    with patch("app.routers.api_router.MedidorClient") as MockMedidor:
        resp = client.get("/api/v2/clients/5/balance")
        MockMedidor.assert_not_called()

    app.dependency_overrides.clear()
    assert resp.status_code == 200
    assert resp.json()["balance_cents"] == 0


def test_balance_404_unknown_client(client):
    db = _db_empty()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: _user("super_admin")

    resp = client.get("/api/v2/clients/9999/balance")
    app.dependency_overrides.clear()
    assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# /api/v2/billing/run-closing -> 501 stub
# ══════════════════════════════════════════════════════════════════════════════

def test_api_run_closing_501(client):
    db = _db_mock([{"id": 1}])
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[current_user] = lambda: _user("super_admin")

    with patch("app.routers.api_router.bind_actor", new=AsyncMock()):
        resp = client.post("/api/v2/billing/run-closing")

    app.dependency_overrides.clear()
    assert resp.status_code == 501
    assert "not_implemented" in resp.json()["status"]
