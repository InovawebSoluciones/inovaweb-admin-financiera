"""Onboarding prepago: compensacion de la Saga (modelo contrato real).

En el modelo prepago el unico recurso provisionado por cliente en el alta es
la WALLET del Medidor. Finanzas-Core y Centro de Mensajes son multi-tenant
resueltos por la API key, y el Hub se configura por SQL: NO hay create_account
/ issue_api_key en ningun core. Por tanto la compensacion solo debe intentar
borrar la wallet ya creada (best-effort).
"""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_compensation_deletes_wallet():
    """Si el provisioning local falla tras crear la wallet, se borra la wallet."""
    from app.services import onboarding

    med = AsyncMock()

    with patch.object(onboarding, "MedidorClient", return_value=med):
        # validamos la compensacion directa, sin DB:
        await onboarding._compensate(
            med, wallet_id="wal-1", client_id=99, error="PG cae",
        )

    med.delete_wallet.assert_awaited_once_with("wal-1")
    med.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_compensation_skips_delete_when_no_wallet():
    """Si la wallet nunca se creo (wallet_id None), no se intenta borrar."""
    from app.services import onboarding

    med = AsyncMock()

    await onboarding._compensate(med, wallet_id=None, client_id=99, error="boom")

    med.delete_wallet.assert_not_called()
    med.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_onboard_client_failure_after_wallet_compensates_and_audits():
    """Path de fallo tras crear la wallet (FIX-2 + FIX-4).

    Verifica que cuando el provisioning local falla DESPUES de crear la wallet:
      - se dispara la compensacion (medidor.delete_wallet llamado),
      - la sesion del request hace rollback,
      - el audit 'onboard_failed' se persiste en una sesion INDEPENDIENTE que
        SI hace commit (no se pierde con el rollback de get_db).
    """
    from app.services import onboarding

    # --- DB del request (mock) -------------------------------------------
    # execute() devuelve resultados distintos segun el paso del saga:
    #   1) SELECT plan -> first() = (1,)
    #   2) INSERT clients RETURNING id -> scalar_one() = 42
    #   3) UPDATE clients ... -> revienta (simula fallo del provisioning local)
    plan_res = AsyncMock()
    plan_res.first = lambda: (1,)
    client_res = AsyncMock()
    client_res.scalar_one = lambda: 42

    call = {"n": 0}

    async def fake_execute(*args, **kwargs):
        call["n"] += 1
        if call["n"] == 1:
            return plan_res
        if call["n"] == 2:
            return client_res
        # tercer execute (UPDATE clients): fallo del provisioning local
        raise RuntimeError("PG cae en UPDATE")

    db = AsyncMock()
    db.execute.side_effect = fake_execute

    # --- cliente Medidor (mock): create_wallet OK, delete_wallet espiable ---
    med = AsyncMock()
    med.create_wallet.return_value = {"id": "wal-42"}

    # --- sesion INDEPENDIENTE del audit de fallo (mock context manager) ----
    audit_db = AsyncMock()
    audit_session_cm = AsyncMock()
    audit_session_cm.__aenter__.return_value = audit_db
    audit_session_cm.__aexit__.return_value = False
    fake_session_local = lambda: audit_session_cm

    payload = onboarding.OnboardClientPayload(
        legal_name="ACME SA", trade_name=None, rfc="XAXX010101000",
        cfdi_use="G03", tax_regime="601", zip_code="64000",
        billing_email="b@x.com", contact_phone=None, plan_code="basico",
        titular_full_name="Juan", titular_email="j@x.com",
    )

    written = {}

    async def fake_write_event(session, **kw):
        # capturamos que el evento se escribe en la sesion de audit (no en db)
        written["session"] = session
        written["action"] = kw.get("action")
        written["new_values"] = kw.get("new_values")

    with patch.object(onboarding, "MedidorClient", return_value=med), \
         patch.object(onboarding, "SessionLocal", fake_session_local), \
         patch.object(onboarding, "write_event", side_effect=fake_write_event):
        with pytest.raises(onboarding.OnboardingError):
            await onboarding.onboard_client(
                db, payload,
                actor_user_id=7, actor_ip="1.2.3.4", request_id="req-1",
            )

    # compensacion: la wallet creada se borra
    med.delete_wallet.assert_awaited_once_with("wal-42")
    # rollback de la sesion del request
    db.rollback.assert_awaited()
    # el audit de fallo se escribio en la sesion INDEPENDIENTE y se confirmo
    assert written["session"] is audit_db
    assert written["action"] == "onboard_failed"
    assert written["new_values"]["stage"] == "local_provisioning"
    audit_db.commit.assert_awaited_once()
    # NO se debe haber commiteado en la sesion del request por el path de fallo
    db.commit.assert_not_called()
