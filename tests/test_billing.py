"""Cierre mensual: facturacion del consumo IA (Medidor) y emails (Mensajes).

Cubre TASK #3-D: al cierre, el CAF lee el consumo real de IA y de emails de
cada cliente y lo agrega como invoice_items a la factura del periodo.

Estos tests ejercitan `_close_one_subscription` directamente con un mock de
AsyncSession (sin BD real) y mocks de los clientes de cores, siguiendo el
estilo de `tests/test_onboarding.py`. pytest NO esta instalado en Windows y el
.venv es de Linux: la ejecucion queda diferida a Docker/VPS; aqui se valida
sintaxis con `python -m py_compile`.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import billing


def _make_db(captured_items: list[dict], invoice_id: int = 100) -> AsyncMock:
    """Construye un AsyncSession mock para `_close_one_subscription`.

    Captura cada INSERT en invoice_items dentro de `captured_items` para poder
    aseverar los conceptos generados. El resto de queries devuelve resultados
    coherentes con el flujo (sin factura previa, sin plan_items, sin overage).
    """
    db = AsyncMock()

    async def fake_execute(stmt, params=None):
        sql = str(getattr(stmt, "text", stmt))
        res = MagicMock()

        if "FROM invoices" in sql and "period_start" in sql:
            # 1) idempotencia: no existe factura previa del periodo
            res.first = MagicMock(return_value=None)
            return res
        if "plan_items" in sql:
            # limites del plan: ninguno
            mappings = MagicMock()
            mappings.all = MagicMock(return_value=[])
            res.mappings = MagicMock(return_value=mappings)
            return res
        if "FROM services WHERE code" in sql:
            mappings = MagicMock()
            mappings.first = MagicMock(return_value=None)
            res.mappings = MagicMock(return_value=mappings)
            return res
        if "INSERT INTO invoices" in sql:
            res.scalar_one = MagicMock(return_value=invoice_id)
            return res
        if "INSERT INTO invoice_items" in sql:
            captured_items.append(dict(params or {}))
            return res
        # UPDATE invoices SET status, y cualquier otro: ok
        return res

    db.execute.side_effect = fake_execute
    return db


def _sub(*, medidor: str | None = None, messages: str | None = None) -> dict:
    """Fila de suscripcion minima para el cierre (plan sin cuota fija)."""
    return {
        "subscription_id": 1,
        "client_id": 7,
        "plan_id": 3,
        "monthly_fee_cents": 0,
        "is_free": False,
        "medidor_account_id": medidor,
        "messages_account_id": messages,
        "legal_name": "ACME SA",
    }


@pytest.mark.asyncio
async def test_ia_usage_creates_invoice_item():
    """Con consumo IA del Medidor se crea un invoice_item con el costo correcto."""
    captured: list[dict] = []
    db = _make_db(captured)

    medidor = AsyncMock()
    medidor.get_usage.return_value = {"items": []}  # sin overage por servicio
    medidor.get_usage_summary.return_value = {"operations": 120, "cost_cents": 4500}
    messages = AsyncMock()

    with patch.object(billing, "apply_promotions", AsyncMock(return_value=0)):
        inv_id, total = await billing._close_one_subscription(
            db, medidor, messages, _sub(medidor="wal-1"),
            date(2026, 5, 1), date(2026, 5, 31),
        )

    assert inv_id == 100
    ia_items = [c for c in captured if "Consumo IA" in c["d"]]
    assert len(ia_items) == 1
    item = ia_items[0]
    assert item["a"] == 4500          # amount_cents == costo del Medidor
    assert item["q"] == 120           # quantity == operaciones
    assert "120 operaciones" in item["d"]
    assert item["sk"] and item["uk"]  # sat_key / unit_sat_key provistos
    # subtotal IA (4500) + IVA 16% (720) = 5220
    assert total == 4500 + round(4500 * 0.16)


@pytest.mark.asyncio
async def test_messages_usage_creates_invoice_item():
    """Con mensajes enviados se crea un invoice_item con el costo correcto."""
    captured: list[dict] = []
    db = _make_db(captured)

    medidor = AsyncMock()
    messages = AsyncMock()
    messages.get_usage.return_value = {"messages": 30, "cost_cents": 1500}

    with patch.object(billing, "apply_promotions", AsyncMock(return_value=0)):
        inv_id, total = await billing._close_one_subscription(
            db, medidor, messages, _sub(messages="msg-1"),
            date(2026, 5, 1), date(2026, 5, 31),
        )

    assert inv_id == 100
    msg_items = [c for c in captured if "Mensajes enviados" in c["d"]]
    assert len(msg_items) == 1
    item = msg_items[0]
    assert item["a"] == 1500
    assert item["q"] == 30
    assert "30 mensajes" in item["d"]
    assert item["sk"] and item["uk"]
    # el cliente no tiene wallet -> no se consulta el Medidor
    medidor.get_usage_summary.assert_not_called()


@pytest.mark.asyncio
async def test_client_without_wallet_skips_ia_item():
    """Cliente sin medidor_account_id: NO genera item de IA y NO da error."""
    captured: list[dict] = []
    db = _make_db(captured)

    medidor = AsyncMock()
    messages = AsyncMock()
    messages.get_usage.return_value = {"messages": 10, "cost_cents": 500}

    with patch.object(billing, "apply_promotions", AsyncMock(return_value=0)):
        inv_id, _ = await billing._close_one_subscription(
            db, medidor, messages, _sub(medidor=None, messages="msg-1"),
            date(2026, 5, 1), date(2026, 5, 31),
        )

    assert inv_id == 100
    medidor.get_usage.assert_not_called()
    medidor.get_usage_summary.assert_not_called()
    assert not [c for c in captured if "Consumo IA" in c["d"]]


@pytest.mark.asyncio
async def test_no_usage_no_invoice():
    """Sin cuota, sin IA y sin mensajes: no se genera factura (None, 0)."""
    captured: list[dict] = []
    db = _make_db(captured)

    medidor = AsyncMock()
    medidor.get_usage.return_value = {"items": []}
    medidor.get_usage_summary.return_value = {"operations": 0, "cost_cents": 0}
    messages = AsyncMock()
    messages.get_usage.return_value = {"messages": 0, "cost_cents": 0}

    with patch.object(billing, "apply_promotions", AsyncMock(return_value=0)):
        inv_id, total = await billing._close_one_subscription(
            db, medidor, messages, _sub(medidor="wal-1", messages="msg-1"),
            date(2026, 5, 1), date(2026, 5, 31),
        )

    assert inv_id is None
    assert total == 0
    assert captured == []


def test_medidor_get_usage_summary_aggregates():
    """get_usage_summary suma operaciones y costo en centavos sobre `items`."""
    import asyncio

    from app.core.clients.medidor_client import MedidorClient

    core = AsyncMock()
    core.get.return_value = {
        "items": [
            {"operations": 10, "cost_cents": 300},
            {"units": 5, "amount_cents": 150},
        ]
    }
    client = MedidorClient(core)
    out = asyncio.run(
        client.get_usage_summary("wal-1", from_ts="a", to_ts="b")
    )
    assert out == {"operations": 15, "cost_cents": 450}


def test_messages_get_usage_normalizes():
    """get_usage normaliza la respuesta del Centro a {messages, cost_cents}."""
    import asyncio

    from app.core.clients.messages_client import MessagesClient

    core = AsyncMock()
    core.get.return_value = {"count": 42, "amount_cents": 2100}
    client = MessagesClient(core)
    out = asyncio.run(
        client.get_usage("msg-1", from_ts="a", to_ts="b")
    )
    assert out == {"messages": 42, "cost_cents": 2100}
