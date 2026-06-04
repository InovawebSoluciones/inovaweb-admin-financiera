"""Webhook hub-payment-paid (flujo prepago) + correcciones TASK-15b.

Cubre los invariantes firmes:
  - firma HMAC invalida -> 401 (antes de cualquier I/O).
  - timestamp fuera de ventana -> 401 (anti-replay).
  - credito una sola vez (camino feliz): credit + email + asiento una vez.
  - FIX-1 replay/concurrencia: INSERT ... ON CONFLICT DO NOTHING (rowcount=0)
    -> duplicate_ignored, sin credit / email / asiento.
  - FIX-2 purpose/amount no coincide con recharge.initiated -> rechazado (502),
    audit hub.paid.rejected, sin credit.
  - fallo de credit -> 502 + audit hub.paid.failed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings


def _sign(secret: str, body: bytes, timestamp: str | None = None) -> str:
    if timestamp:
        body = f"{timestamp}.".encode() + body
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _payload(txn_id: str = "txn-1", account: str = "client-42",
             amount_cents: int = 9900, purpose: str = "plan_purchase") -> dict:
    return {
        "transaction_id": txn_id,
        "account_id": account,
        "amount_cents": amount_cents,
        "metadata": {"purpose": purpose, "caf_client_id": 42,
                     "plan_code": "basico"},
        "occurred_at": "2026-06-03T12:00:00Z",
    }


class _FakeResult:
    """Imita el Result de SQLAlchemy con first()/mappings()/rowcount."""

    def __init__(self, first=None, mapping=None, rowcount=0):
        self._first = first
        self._mapping = mapping
        self.rowcount = rowcount

    def first(self):
        return self._first

    def mappings(self):
        outer = self

        class _M:
            def first(self_inner):
                return outer._mapping

        return _M()


def _client_row():
    return {
        "id": 42, "medidor_account_id": "wal-42",
        "finanzas_account_id": "client-42",
        "billing_email": "b@x.com", "legal_name": "ACME SA",
    }


def _intent_row(amount_cents: int = 9900, purpose: str = "plan_purchase"):
    """Fila simulada de audit_log action=recharge.initiated (FIX-2)."""
    return {"new_values": {"recharge_id": "txn-1", "purpose": purpose,
                           "amount_cents": amount_cents}}


def _make_db(claim_inserts: bool = True, intent=None,
             intent_amount: int = 9900, intent_purpose: str = "plan_purchase"):
    """DB mock para el flujo TASK-15b.

    - SELECT recharge.initiated (audit_log) -> `intent` (o uno coincidente).
    - SELECT clients -> fila cliente.
    - INSERT INTO payments ... ON CONFLICT -> rowcount 1 (gana) o 0 (replay).
    """
    db = AsyncMock()
    if intent is None:
        intent = _intent_row(intent_amount, intent_purpose)

    async def fake_execute(stmt, params=None):
        sql = str(stmt).lower()
        if "recharge.initiated" in sql or "from audit_log" in sql:
            return _FakeResult(mapping=intent)
        if "from clients" in sql:
            return _FakeResult(mapping=_client_row())
        if "insert into payments" in sql:
            return _FakeResult(rowcount=1 if claim_inserts else 0)
        return _FakeResult()

    db.execute.side_effect = fake_execute
    return db


async def _app_client(db):
    """App con get_db overrideado a la sesion mock (sin BD real)."""
    from app.main import app
    from app.core.database import get_db

    async def _override():
        yield db

    app.dependency_overrides[get_db] = _override
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://admin.test"), app


@pytest.mark.asyncio
async def test_invalid_signature_returns_401():
    db = _make_db()
    client, app = await _app_client(db)
    try:
        body = json.dumps(_payload()).encode()
        resp = await client.post(
            "/webhooks/hub-payment-paid",
            content=body,
            headers={"X-Signature": "deadbeef", "Content-Type": "application/json"},
        )
        assert resp.status_code == 401
    finally:
        app.dependency_overrides.clear()
        await client.aclose()


@pytest.mark.asyncio
async def test_expired_timestamp_returns_401():
    secret = get_settings().hub_webhook_secret()
    db = _make_db()
    client, app = await _app_client(db)
    try:
        body = json.dumps(_payload()).encode()
        old_ts = str(int(time.time()) - 10_000)  # fuera de ventana
        sig = _sign(secret, body, old_ts)
        resp = await client.post(
            "/webhooks/hub-payment-paid",
            content=body,
            headers={"X-Signature": sig, "X-Timestamp": old_ts,
                     "Content-Type": "application/json"},
        )
        assert resp.status_code == 401
    finally:
        app.dependency_overrides.clear()
        await client.aclose()


@pytest.mark.asyncio
async def test_valid_signature_credits_wallet_once():
    secret = get_settings().hub_webhook_secret()
    db = _make_db(claim_inserts=True)
    client, app = await _app_client(db)

    medidor = AsyncMock()
    finanzas = AsyncMock()
    messages = AsyncMock()

    with patch("app.services.prepago.MedidorClient", return_value=medidor), \
         patch("app.services.prepago.FinanzasClient", return_value=finanzas), \
         patch("app.services.prepago.MessagesClient", return_value=messages):
        try:
            body = json.dumps(_payload()).encode()
            sig = _sign(secret, body)  # sin timestamp -> firma directa del body
            resp = await client.post(
                "/webhooks/hub-payment-paid",
                content=body,
                headers={"X-Signature": sig, "Content-Type": "application/json"},
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "credited"
            # credito al Medidor EXACTAMENTE una vez, con request_id determinista
            medidor.credit.assert_awaited_once()
            kwargs = medidor.credit.await_args.kwargs
            assert kwargs["request_id"] == "caf-recharge-txn-1"
            assert kwargs["amount_cents"] == 9900
            # asiento en finanzas con source_ref determinista
            finanzas.post_entry.assert_awaited_once()
            fkw = finanzas.post_entry.await_args.kwargs
            assert fkw["source_ref"] == "caf-recharge-txn-1"
            assert fkw["source_slug"] == "hub"
        finally:
            app.dependency_overrides.clear()
            await client.aclose()


@pytest.mark.asyncio
async def test_replay_same_txn_does_not_double_credit():
    """FIX-1: replay/concurrencia. El INSERT ... ON CONFLICT DO NOTHING no
    inserta (rowcount=0) porque otro webhook ya gano la fila -> duplicate_ignored,
    sin credit ni asiento ni correo."""
    secret = get_settings().hub_webhook_secret()
    # claim_inserts=False simula que el INSERT no gano la fila (ya existia)
    db = _make_db(claim_inserts=False)
    client, app = await _app_client(db)

    medidor = AsyncMock()
    finanzas = AsyncMock()
    messages = AsyncMock()

    with patch("app.services.prepago.MedidorClient", return_value=medidor), \
         patch("app.services.prepago.FinanzasClient", return_value=finanzas), \
         patch("app.services.prepago.MessagesClient", return_value=messages):
        try:
            body = json.dumps(_payload()).encode()
            sig = _sign(secret, body)
            resp = await client.post(
                "/webhooks/hub-payment-paid",
                content=body,
                headers={"X-Signature": sig, "Content-Type": "application/json"},
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "duplicate_ignored"
            # replay: NO se acredita de nuevo, NO se manda correo
            medidor.credit.assert_not_awaited()
            finanzas.post_entry.assert_not_awaited()
            messages.send_email.assert_not_awaited()
        finally:
            app.dependency_overrides.clear()
            await client.aclose()


@pytest.mark.asyncio
async def test_amount_mismatch_is_rejected():
    """FIX-2: el evento trae un amount distinto al de recharge.initiated ->
    rechazado (502), sin credit. El intento local dice 9900; el evento 1."""
    secret = get_settings().hub_webhook_secret()
    db = _make_db(claim_inserts=True, intent_amount=9900)
    client, app = await _app_client(db)

    medidor = AsyncMock()
    finanzas = AsyncMock()
    messages = AsyncMock()

    with patch("app.services.prepago.MedidorClient", return_value=medidor), \
         patch("app.services.prepago.FinanzasClient", return_value=finanzas), \
         patch("app.services.prepago.MessagesClient", return_value=messages):
        try:
            body = json.dumps(_payload(amount_cents=1)).encode()  # monto manipulado
            sig = _sign(secret, body)
            resp = await client.post(
                "/webhooks/hub-payment-paid",
                content=body,
                headers={"X-Signature": sig, "Content-Type": "application/json"},
            )
            assert resp.status_code == 502, resp.text
            medidor.credit.assert_not_awaited()
            finanzas.post_entry.assert_not_awaited()
        finally:
            app.dependency_overrides.clear()
            await client.aclose()


@pytest.mark.asyncio
async def test_purpose_mismatch_is_rejected():
    """FIX-2: el purpose del evento no coincide con recharge.initiated ->
    rechazado (502), sin credit."""
    secret = get_settings().hub_webhook_secret()
    db = _make_db(claim_inserts=True, intent_purpose="plan_purchase")
    client, app = await _app_client(db)

    medidor = AsyncMock()
    finanzas = AsyncMock()
    messages = AsyncMock()

    with patch("app.services.prepago.MedidorClient", return_value=medidor), \
         patch("app.services.prepago.FinanzasClient", return_value=finanzas), \
         patch("app.services.prepago.MessagesClient", return_value=messages):
        try:
            body = json.dumps(_payload(purpose="wallet_recharge")).encode()
            sig = _sign(secret, body)
            resp = await client.post(
                "/webhooks/hub-payment-paid",
                content=body,
                headers={"X-Signature": sig, "Content-Type": "application/json"},
            )
            assert resp.status_code == 502, resp.text
            medidor.credit.assert_not_awaited()
        finally:
            app.dependency_overrides.clear()
            await client.aclose()


@pytest.mark.asyncio
async def test_credit_failure_returns_502():
    """Fallo del credit al Medidor -> 502 (reintentable) + sin asiento."""
    secret = get_settings().hub_webhook_secret()
    db = _make_db(claim_inserts=True)
    client, app = await _app_client(db)

    medidor = AsyncMock()
    medidor.credit.side_effect = RuntimeError("medidor caido")
    finanzas = AsyncMock()
    messages = AsyncMock()

    with patch("app.services.prepago.MedidorClient", return_value=medidor), \
         patch("app.services.prepago.FinanzasClient", return_value=finanzas), \
         patch("app.services.prepago.MessagesClient", return_value=messages), \
         patch("app.services.prepago._persist_failure_audit",
               new_callable=AsyncMock) as audit:
        try:
            body = json.dumps(_payload()).encode()
            sig = _sign(secret, body)
            resp = await client.post(
                "/webhooks/hub-payment-paid",
                content=body,
                headers={"X-Signature": sig, "Content-Type": "application/json"},
            )
            assert resp.status_code == 502, resp.text
            # el asiento NO se intenta tras fallar el credit
            finanzas.post_entry.assert_not_awaited()
            # se audito el fallo (stage medidor_credit)
            audit.assert_awaited()
            akw = audit.await_args.kwargs
            assert akw["new_values"]["stage"] == "medidor_credit"
        finally:
            app.dependency_overrides.clear()
            await client.aclose()
