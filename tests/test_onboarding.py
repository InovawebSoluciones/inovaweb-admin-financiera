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
async def test_compensation_suspends_wallet():
    """Si el provisioning local falla tras crear la wallet, se SUSPENDE la wallet.

    El Medidor no expone DELETE de wallet; la compensacion la suspende (best-effort).
    """
    from app.services import onboarding

    med = AsyncMock()

    with patch.object(onboarding, "MedidorClient", return_value=med):
        # validamos la compensacion directa, sin DB:
        await onboarding._compensate(
            med, wallet_id="wal-1", client_id=99, error="PG cae",
        )

    med.suspend_wallet.assert_awaited_once_with("wal-1")
    med.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_compensation_skips_suspend_when_no_wallet():
    """Si la wallet nunca se creo (wallet_id None), no se intenta suspender."""
    from app.services import onboarding

    med = AsyncMock()

    await onboarding._compensate(med, wallet_id=None, client_id=99, error="boom")

    med.suspend_wallet.assert_not_called()
    med.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_onboard_client_failure_after_wallet_compensates_and_audits():
    """Path de fallo tras crear la wallet (FIX-2 + FIX-4).

    Verifica que cuando el provisioning local falla DESPUES de crear la wallet:
      - se dispara la compensacion (medidor.suspend_wallet llamado),
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

    # --- cliente Medidor (mock): create_wallet OK, suspend_wallet espiable ---
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

    # compensacion: la wallet creada se suspende
    med.suspend_wallet.assert_awaited_once_with("wal-42")
    # rollback de la sesion del request
    db.rollback.assert_awaited()
    # el audit de fallo se escribio en la sesion INDEPENDIENTE y se confirmo
    assert written["session"] is audit_db
    assert written["action"] == "onboard_failed"
    assert written["new_values"]["stage"] == "local_provisioning"
    audit_db.commit.assert_awaited_once()
    # NO se debe haber commiteado en la sesion del request por el path de fallo
    db.commit.assert_not_called()


# =====================================================================
# TASK-16: link Scraping + token de activacion + correo
# =====================================================================


def _ok_db_mock():
    """DB mock para el camino feliz del onboarding.

    execute() devuelve resultados segun el paso del saga:
      1) SELECT plan        -> first() = (1,)
      2) INSERT clients     -> scalar_one() = 42
      5) INSERT users       -> scalar_one() = 7  (5to execute con RETURNING)
    El resto de execute (UPDATE/INSERT sin RETURNING) devuelve un mock generico.
    """
    plan_res = AsyncMock()
    plan_res.first = lambda: (1,)
    client_res = AsyncMock()
    client_res.scalar_one = lambda: 42
    user_res = AsyncMock()
    user_res.scalar_one = lambda: 7
    generic = AsyncMock()

    call = {"n": 0}

    async def fake_execute(*args, **kwargs):
        call["n"] += 1
        if call["n"] == 1:
            return plan_res
        if call["n"] == 2:
            return client_res
        # SELECT plan(1), INSERT clients(2), UPDATE clients(3),
        # [link va por cliente HTTP, no execute], INSERT subscriptions(4),
        # INSERT users(5) RETURNING id, INSERT user_roles(6),
        # INSERT activation_tokens(7)
        if call["n"] == 5:
            return user_res
        return generic

    db = AsyncMock()
    db.execute.side_effect = fake_execute
    return db


def _payload(scraping_company_id=None):
    from app.services import onboarding

    return onboarding.OnboardClientPayload(
        legal_name="ACME SA", trade_name=None, rfc="XAXX010101000",
        cfdi_use="G03", tax_regime="601", zip_code="64000",
        billing_email="b@x.com", contact_phone=None, plan_code="basico",
        titular_full_name="Juan", titular_email="j@x.com",
        scraping_company_id=scraping_company_id,
    )


@pytest.mark.asyncio
async def test_onboard_without_scraping_company_id_skips_link():
    """Sin `scraping_company_id` no se llama a Scraping; el correo SI se envia."""
    from app.services import onboarding

    db = _ok_db_mock()

    med = AsyncMock()
    med.create_wallet.return_value = {"id": "wal-42"}
    scraping = AsyncMock()
    messages = AsyncMock()

    with patch.object(onboarding, "MedidorClient", return_value=med), \
         patch.object(onboarding, "ScrapingClient", return_value=scraping), \
         patch.object(onboarding, "MessagesClient", return_value=messages):
        result = await onboarding.onboard_client(
            db, _payload(scraping_company_id=None),
            actor_user_id=7, actor_ip="1.2.3.4", request_id="req-1",
        )

    # no se intento ligar Scraping
    scraping.link_caf.assert_not_called()
    # el correo de activacion SI se intento enviar
    messages.send_email.assert_awaited_once()
    # onboarding consumado
    assert result.client_id == 42
    assert result.wallet_id == "wal-42"
    med.suspend_wallet.assert_not_called()


@pytest.mark.asyncio
async def test_onboard_with_scraping_calls_link_caf():
    """Con `scraping_company_id` se liga la Company (link_caf con los 3 args)."""
    from app.services import onboarding

    db = _ok_db_mock()

    med = AsyncMock()
    med.create_wallet.return_value = {"id": "wal-42"}
    scraping = AsyncMock()
    messages = AsyncMock()

    with patch.object(onboarding, "MedidorClient", return_value=med), \
         patch.object(onboarding, "ScrapingClient", return_value=scraping), \
         patch.object(onboarding, "MessagesClient", return_value=messages):
        await onboarding.onboard_client(
            db, _payload(scraping_company_id=99),
            actor_user_id=7, actor_ip="1.2.3.4", request_id="req-1",
        )

    scraping.link_caf.assert_awaited_once_with(
        company_id=99, caf_client_id=42, medidor_wallet_id="wal-42",
    )
    scraping.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_onboard_link_caf_failure_compensates_wallet():
    """Si `link_caf` falla, se compensa la wallet (suspend) y se audita el fallo."""
    from app.services import onboarding

    db = _ok_db_mock()

    med = AsyncMock()
    med.create_wallet.return_value = {"id": "wal-42"}
    scraping = AsyncMock()
    scraping.link_caf.side_effect = RuntimeError("scraping 500")
    messages = AsyncMock()

    audit_db = AsyncMock()
    audit_cm = AsyncMock()
    audit_cm.__aenter__.return_value = audit_db
    audit_cm.__aexit__.return_value = False

    written = {}

    async def fake_write_event(session, **kw):
        written["action"] = kw.get("action")
        written["new_values"] = kw.get("new_values")

    with patch.object(onboarding, "MedidorClient", return_value=med), \
         patch.object(onboarding, "ScrapingClient", return_value=scraping), \
         patch.object(onboarding, "MessagesClient", return_value=messages), \
         patch.object(onboarding, "SessionLocal", lambda: audit_cm), \
         patch.object(onboarding, "write_event", side_effect=fake_write_event):
        with pytest.raises(onboarding.OnboardingError):
            await onboarding.onboard_client(
                db, _payload(scraping_company_id=99),
                actor_user_id=7, actor_ip="1.2.3.4", request_id="req-1",
            )

    # compensacion: la wallet creada se suspende y la Company NO queda ligada
    med.suspend_wallet.assert_awaited_once_with("wal-42")
    db.rollback.assert_awaited()
    assert written["action"] == "onboard_failed"
    assert written["new_values"]["stage"] == "local_provisioning"
    # el cliente Scraping se cierra aun en el fallo (finally)
    scraping.close.assert_awaited_once()
    # no se envia correo de activacion si el alta aborto
    messages.send_email.assert_not_called()


@pytest.mark.asyncio
async def test_onboard_messaging_failure_does_not_abort():
    """Un fallo del Centro de Mensajes NO aborta el onboarding ya consumado."""
    from app.services import onboarding

    db = _ok_db_mock()

    med = AsyncMock()
    med.create_wallet.return_value = {"id": "wal-42"}
    scraping = AsyncMock()
    messages = AsyncMock()
    messages.send_email.side_effect = RuntimeError("mensajes 503")

    with patch.object(onboarding, "MedidorClient", return_value=med), \
         patch.object(onboarding, "ScrapingClient", return_value=scraping), \
         patch.object(onboarding, "MessagesClient", return_value=messages):
        result = await onboarding.onboard_client(
            db, _payload(scraping_company_id=99),
            actor_user_id=7, actor_ip="1.2.3.4", request_id="req-1",
        )

    # el alta se consuma pese al fallo de mensajeria
    assert result.client_id == 42
    assert result.user_id == 7
    # no se compenso la wallet (el fallo de correo no es del provisioning)
    med.suspend_wallet.assert_not_called()
    db.rollback.assert_not_called()
    # el cliente de mensajes se cierra aun en el fallo (finally)
    messages.close.assert_awaited_once()
