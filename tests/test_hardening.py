"""Hardening #19/22 (TASK-15b) — H1..H5.

Cubre los 5 puntos de hardening de la auditoria previa a multi-cliente:
  H1 — idempotencia en BD del onboarding (request_id dedup).
  H2 — retry con backoff exponencial del credito al Medidor.
  H3 — filtro explicito por client_id del JWT en el portal (fail-closed).
  H4 — fail-closed en prod: HUB_WEBHOOK_SECRET obligatorio y != HUB_API_KEY.
  H5 — tope de monto en recargas (defensa en profundidad en el webhook).

Estilo: mocks de AsyncSession / clientes core (sin BD ni red real), igual que
test_onboarding.py y test_hub_webhook.py. pytest queda diferido a Docker/VPS
(en Windows no esta instalado y el .venv es Linux): aqui solo se asegura
py_compile + cobertura logica.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest


# =====================================================================
# H1 — idempotencia del onboarding por request_id
# =====================================================================


def _payload():
    from app.services import onboarding

    return onboarding.OnboardClientPayload(
        legal_name="ACME SA", trade_name=None, rfc="XAXX010101000",
        cfdi_use="G03", tax_regime="601", zip_code="64000",
        billing_email="b@x.com", contact_phone=None, plan_code="basico",
        titular_full_name="Juan", titular_email="j@x.com",
    )


@pytest.mark.asyncio
async def test_h1_idempotent_replay_returns_existing_client():
    """H1: si ya existe un cliente con el request_id, se devuelve sin re-crear.

    El SELECT inicial por request_id encuentra el alta previa -> NO se ejecuta
    la Saga (no se crea otra wallet) y se devuelve el OnboardResult existente.
    """
    from app.services import onboarding

    existing = {"client_id": 42, "wallet_id": "wal-42", "user_id": 7}

    class _M:
        def first(self):
            return existing

    exec_res = AsyncMock()
    exec_res.mappings = lambda: _M()

    db = AsyncMock()
    db.execute.return_value = exec_res

    med = AsyncMock()
    with patch.object(onboarding, "MedidorClient", return_value=med):
        result = await onboarding.onboard_client(
            db, _payload(),
            actor_user_id=7, actor_ip="1.2.3.4", request_id="req-dup",
        )

    assert result.client_id == 42
    assert result.wallet_id == "wal-42"
    assert result.user_id == 7
    # idempotente: no se creo wallet nueva ni se inserto otro cliente
    med.create_wallet.assert_not_called()
    # solo se ejecuto el SELECT de dedup (1 execute), nada de INSERT clients
    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_h1_no_request_id_skips_dedup_select():
    """H1: sin request_id no se hace el SELECT de dedup (comportamiento previo).

    El primer execute debe ser el SELECT del plan, no la query de dedup.
    """
    from app.services import onboarding

    plan_res = AsyncMock()
    plan_res.first = lambda: (1,)
    client_res = AsyncMock()
    client_res.scalar_one = lambda: 42
    user_res = AsyncMock()
    user_res.scalar_one = lambda: 7
    generic = AsyncMock()

    seen_sql: list[str] = []
    call = {"n": 0}

    async def fake_execute(stmt, params=None):
        seen_sql.append(str(stmt))
        call["n"] += 1
        if call["n"] == 1:
            return plan_res
        if call["n"] == 2:
            return client_res
        if call["n"] == 5:
            return user_res
        return generic

    db = AsyncMock()
    db.execute.side_effect = fake_execute

    med = AsyncMock()
    med.create_wallet.return_value = {"id": "wal-42"}
    scraping = AsyncMock()
    messages = AsyncMock()

    with patch.object(onboarding, "MedidorClient", return_value=med), \
         patch.object(onboarding, "ScrapingClient", return_value=scraping), \
         patch.object(onboarding, "MessagesClient", return_value=messages):
        result = await onboarding.onboard_client(
            db, _payload(),
            actor_user_id=7, actor_ip="1.2.3.4", request_id=None,
        )

    assert result.client_id == 42
    # el primer query NO es el de dedup de clients por request_id
    assert "c.request_id" not in seen_sql[0]
    # el INSERT clients persiste la columna request_id
    assert any("request_id" in s and "INSERT INTO clients" in s for s in seen_sql)


# =====================================================================
# H2 — retry con backoff exponencial del credito al Medidor
# =====================================================================


@pytest.mark.asyncio
async def test_h2_retry_succeeds_after_transient_failures():
    """H2: el helper reintenta y devuelve el resultado del 1er intento exitoso."""
    from app.services import prepago

    attempts = {"n": 0}

    async def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("red caida")
        return "ok"

    with patch("app.services.prepago.asyncio.sleep", new_callable=AsyncMock) as sleep:
        result = await prepago._retry_async(flaky, op_name="test")

    assert result == "ok"
    assert attempts["n"] == 3
    # 2 esperas (tras intento 1 y 2), con backoff 1s y 2s
    assert sleep.await_count == 2
    assert sleep.await_args_list[0].args[0] == 1.0
    assert sleep.await_args_list[1].args[0] == 2.0


@pytest.mark.asyncio
async def test_h2_retry_exhausts_and_reraises():
    """H2: agotados los 3 intentos, re-lanza la ultima excepcion (pending_retry)."""
    from app.services import prepago

    attempts = {"n": 0}

    async def always_fail():
        attempts["n"] += 1
        raise RuntimeError(f"fallo {attempts['n']}")

    with patch("app.services.prepago.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(RuntimeError):
            await prepago._retry_async(always_fail, op_name="test")

    assert attempts["n"] == prepago._CREDIT_MAX_ATTEMPTS  # 3 intentos


@pytest.mark.asyncio
async def test_h2_credit_pending_retry_audits_on_exhaustion():
    """H2: si el credito agota los reintentos -> audit hub.paid.pending_retry."""
    from app.services import prepago

    ev = {
        "hub_transaction_id": "txn-9", "account_id": "client-42",
        "amount_cents": 9900, "purpose": "wallet_recharge",
        "plan_code": None, "occurred_at": "2026-06-03T12:00:00Z",
    }

    class _M:
        def __init__(self, val):
            self._val = val

        def first(self):
            return self._val

    client_row = {"id": 42, "medidor_account_id": "wal-42",
                  "finanzas_account_id": "client-42",
                  "billing_email": "b@x.com", "legal_name": "ACME SA"}

    async def fake_execute(stmt, params=None):
        sql = str(stmt).lower()
        res = AsyncMock()
        if "from clients" in sql:
            res.mappings = lambda: _M(client_row)
        elif "insert into payments" in sql:
            res.rowcount = 1
        else:
            res.mappings = lambda: _M(None)
        return res

    db = AsyncMock()
    db.execute.side_effect = fake_execute

    med = AsyncMock()
    med.credit.side_effect = RuntimeError("medidor caido")

    with patch("app.services.prepago.MedidorClient", return_value=med), \
         patch("app.services.prepago._correlate_or_reject", new_callable=AsyncMock), \
         patch("app.services.prepago.asyncio.sleep", new_callable=AsyncMock), \
         patch("app.services.prepago._persist_failure_audit",
               new_callable=AsyncMock) as audit:
        with pytest.raises(prepago.PrepagoError):
            await prepago._process_wallet_credit(
                db, ev, {"metadata": {}},
                actor_ip="1.2.3.4", request_id="req-1",
            )

    # se reintento el credito 3 veces antes de rendirse
    assert med.credit.await_count == prepago._CREDIT_MAX_ATTEMPTS
    audit.assert_awaited()
    akw = audit.await_args.kwargs
    assert akw["action"] == "hub.paid.pending_retry"
    assert akw["new_values"]["status"] == "pending_retry"


# =====================================================================
# H3 — filtro explicito por client_id del JWT
# =====================================================================


def test_h3_require_client_id_rejects_when_missing():
    """H3: sin client_id en el JWT -> 403 fail-closed (no query con NULL)."""
    from fastapi import HTTPException

    from app.core.jwt_auth import CurrentUser
    from app.routers.portal_router import _require_client_id

    user_none = CurrentUser(id=1, roles=["cliente_titular"], client_id=None)
    with pytest.raises(HTTPException) as ei:
        _require_client_id(user_none)
    assert ei.value.status_code == 403

    user_ok = CurrentUser(id=1, roles=["cliente_titular"], client_id=42)
    assert _require_client_id(user_ok) == 42


# =====================================================================
# H4 — fail-closed en prod del secreto del webhook
# =====================================================================


def _prod_env(**overrides) -> dict[str, str]:
    base = {
        "ENV": "prod",
        "DATABASE_URL": "postgresql+psycopg://caf:caf@localhost/test",
        "POSTGRES_PASSWORD": "caf",
        "AES_KEY": "dGVzdGtleXRlc3RrZXl0ZXN0a2V5dGVzdA==",
        "JWT_SECRET": "test-jwt-secret-at-least-32-bytes-long!",
        "MEDIDOR_API_KEY": "med-key",
        "HUB_API_KEY": "hub-key",
        "MESSAGES_API_KEY": "msg-key",
        "FINANZAS_API_KEY": "fin-key",
        "SCRAPING_ADMIN_KEY": "scr-key",
        "PAC_API_KEY": "pac-key",
        "PAC_API_SECRET": "pac-secret",
        "RFC_EMISOR": "TST000000AAA",
        "KEY_PASSWORD": "kp",
    }
    base.update(overrides)
    return base


def test_h4_prod_requires_webhook_secret():
    """H4: en prod, sin HUB_WEBHOOK_SECRET el arranque falla (ValueError)."""
    from app.core.config import Settings

    with patch.dict(os.environ, _prod_env(), clear=True):
        with pytest.raises(ValueError):
            Settings()  # type: ignore[call-arg]


def test_h4_prod_rejects_webhook_secret_equal_to_api_key():
    """H4: en prod, HUB_WEBHOOK_SECRET == HUB_API_KEY tambien falla."""
    from app.core.config import Settings

    env = _prod_env(HUB_WEBHOOK_SECRET="hub-key")  # == HUB_API_KEY
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(ValueError):
            Settings()  # type: ignore[call-arg]


def test_h4_prod_accepts_dedicated_webhook_secret():
    """H4: con un secreto dedicado distinto, el arranque en prod es valido."""
    from app.core.config import Settings

    env = _prod_env(HUB_WEBHOOK_SECRET="dedicated-webhook-secret")
    with patch.dict(os.environ, env, clear=True):
        s = Settings()  # type: ignore[call-arg]
        assert s.hub_webhook_secret() == "dedicated-webhook-secret"
        # alias del spec apunta al tope canonico
        assert s.MAX_RECHARGE_AMOUNT_CENTS == s.MAX_RECARGA_CENTS


# =====================================================================
# H5 — tope de monto en recargas (webhook)
# =====================================================================


@pytest.mark.asyncio
async def test_h5_over_cap_amount_is_rejected_before_credit():
    """H5: un monto por encima del tope se rechaza ANTES de acreditar."""
    from app.core.config import get_settings
    from app.services import prepago

    cap = get_settings().MAX_RECARGA_CENTS
    ev = {
        "hub_transaction_id": "txn-big", "account_id": "client-42",
        "amount_cents": cap + 1, "purpose": "wallet_recharge",
        "plan_code": None, "occurred_at": "2026-06-03T12:00:00Z",
    }

    db = AsyncMock()
    med = AsyncMock()

    with patch("app.services.prepago.MedidorClient", return_value=med), \
         patch("app.services.prepago._persist_failure_audit",
               new_callable=AsyncMock) as audit:
        with pytest.raises(prepago.PrepagoError):
            await prepago._process_wallet_credit(
                db, ev, {"metadata": {}},
                actor_ip="1.2.3.4", request_id="req-1",
            )

    # no se acredito nada (rechazo antes del credit / INSERT del pago)
    med.credit.assert_not_called()
    audit.assert_awaited()
    assert audit.await_args.kwargs["action"] == "hub.paid.rejected"
