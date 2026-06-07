"""Cierre mensual: consumo + plan + promociones -> invoice draft."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clients.medidor_client import MedidorClient
from app.core.clients.messages_client import MessagesClient
from app.services.promotions import apply_promotions

# Claves SAT genericas para conceptos de servicio facturados por consumo.
# Reusan las que ya emplea billing.py para consumo medido (ClaveProdServ
# 81112200 "Servicios de procesamiento de datos"; ClaveUnidad E48 "Unidad de
# servicio"). Coherentes con el resto de invoice_items.
_SAT_KEY_CONSUMO = "81112200"
_UNIT_SAT_KEY_SERVICIO = "E48"

log = logging.getLogger("billing")


@dataclass(slots=True)
class ClosingSummary:
    period_start: str
    period_end: str
    clients_processed: int
    invoices_created: int
    total_billed_cents: int
    errors: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "period_start": self.period_start, "period_end": self.period_end,
            "clients_processed": self.clients_processed,
            "invoices_created": self.invoices_created,
            "total_billed_cents": self.total_billed_cents,
            "errors": self.errors,
        }


async def run_monthly_closing(db: AsyncSession, ref_date: date | None = None) -> dict[str, Any]:
    """Genera invoice draft para cada suscripcion activa del mes cerrado.

    ref_date = primer dia del mes que se cierra. Default: primer dia del mes actual
    (es decir, cierra el mes anterior).
    """
    today = ref_date or date.today().replace(day=1)
    period_end = today - timedelta(days=1)
    period_start = period_end.replace(day=1)

    subs = (await db.execute(text("""
        SELECT s.id AS subscription_id, s.client_id, s.plan_id,
               p.monthly_fee_cents, p.is_free,
               c.medidor_account_id, c.messages_account_id, c.legal_name
        FROM subscriptions s
        JOIN plans p ON p.id = s.plan_id
        JOIN clients c ON c.id = s.client_id
        WHERE s.status='active' AND c.status='active'
    """))).mappings().all()

    summary = ClosingSummary(
        period_start=period_start.isoformat(),
        period_end=period_end.isoformat(),
        clients_processed=0, invoices_created=0,
        total_billed_cents=0, errors=[],
    )

    medidor = MedidorClient()
    messages = MessagesClient()
    try:
        for s in subs:
            summary.clients_processed += 1
            try:
                inv_id, total = await _close_one_subscription(
                    db, medidor, messages, dict(s), period_start, period_end,
                )
                if inv_id is not None:
                    summary.invoices_created += 1
                    summary.total_billed_cents += total
            except Exception as e:
                log.exception("closing_failed", extra={
                    "client_id": s["client_id"], "subscription_id": s["subscription_id"],
                })
                summary.errors.append({
                    "client_id": s["client_id"],
                    "subscription_id": s["subscription_id"],
                    "error": str(e),
                })
    finally:
        await medidor.close()
        await messages.close()

    return summary.as_dict()


async def _close_one_subscription(
    db: AsyncSession,
    medidor: MedidorClient,
    messages: MessagesClient,
    sub: dict[str, Any],
    period_start: date,
    period_end: date,
) -> tuple[int | None, int]:
    """Genera (si procede) la factura draft de una suscripcion para el periodo.

    Agrega como invoice_items: la cuota fija del plan, el consumo medido por
    servicio (overage), el consumo de IA del Medidor y los mensajes enviados
    via el Centro de Mensajes. Es idempotente por periodo (no duplica factura).

    Args:
        db: sesion async.
        medidor: cliente del core Medidor (consumo IA).
        messages: cliente del core Centro de Mensajes (emails enviados).
        sub: fila de la suscripcion (incluye `medidor_account_id` y
            `messages_account_id` del cliente).
        period_start: primer dia del periodo facturado.
        period_end: ultimo dia del periodo facturado.

    Returns:
        (invoice_id, total_cents) o (None, 0) si no hay nada que facturar o ya
        existia factura del periodo.
    """
    # idempotencia: no duplicar factura del mismo periodo
    existing = (await db.execute(text("""
        SELECT id FROM invoices
        WHERE client_id = :c AND subscription_id = :s
          AND period_start = :ps AND period_end = :pe
          AND status <> 'cancelled'
    """), {
        "c": sub["client_id"], "s": sub["subscription_id"],
        "ps": period_start, "pe": period_end,
    })).first()
    if existing:
        return None, 0

    items: list[dict[str, Any]] = []
    subtotal = 0

    # 1) cargo fijo del plan
    if sub["monthly_fee_cents"] > 0:
        items.append({
            "description": f"Cuota mensual plan ({period_start.isoformat()} a {period_end.isoformat()})",
            "quantity": 1, "unit_price_cents": sub["monthly_fee_cents"],
            "amount_cents": sub["monthly_fee_cents"],
            "sat_key": "81112101", "unit_sat_key": "E48",
            "service_id": None,
        })
        subtotal += sub["monthly_fee_cents"]

    # 2) consumo medido (si tiene cuenta medidor)
    if sub["medidor_account_id"]:
        try:
            usage = await medidor.get_usage(
                sub["medidor_account_id"],
                from_ts=period_start.isoformat(),
                to_ts=period_end.isoformat(),
            )
        except Exception as e:
            log.warning("usage_fetch_failed", extra={
                "client_id": sub["client_id"], "error": str(e),
            })
            usage = {"items": []}

        # plan_items: limites incluidos por service_id
        plan_limits = {
            r["service_id"]: dict(r) for r in (await db.execute(text("""
                SELECT pi.service_id, pi.included_units, pi.overage_price_cents,
                       s.code, s.name, s.unit_price_cents
                FROM plan_items pi JOIN services s ON s.id = pi.service_id
                WHERE pi.plan_id = :p
            """), {"p": sub["plan_id"]})).mappings().all()
        }

        for u in usage.get("items", []):
            service_code = u.get("service_code")
            units = int(u.get("units", 0))
            # localiza service_id por code (cache simple)
            svc_row = (await db.execute(
                text("SELECT id, unit_price_cents FROM services WHERE code = :c"),
                {"c": service_code},
            )).mappings().first()
            if not svc_row:
                continue
            service_id = svc_row["id"]
            unit_price = svc_row["unit_price_cents"]

            limit_info = plan_limits.get(service_id)
            included = limit_info["included_units"] if limit_info else 0
            overage_price = (limit_info or {}).get("overage_price_cents") or unit_price
            billable = max(units - included, 0)
            if billable == 0:
                continue
            amount = billable * overage_price
            items.append({
                "description": f"Consumo {service_code}: {billable} sobre incluidas",
                "quantity": billable, "unit_price_cents": overage_price,
                "amount_cents": amount,
                "sat_key": "81112200", "unit_sat_key": "E48",
                "service_id": service_id,
            })
            subtotal += amount

    # 2b) consumo IA agregado del Medidor (concepto unico por periodo)
    if sub["medidor_account_id"]:
        try:
            ia = await medidor.get_usage_summary(
                sub["medidor_account_id"],
                from_ts=period_start.isoformat(),
                to_ts=period_end.isoformat(),
            )
        except Exception as e:
            log.warning("ia_usage_fetch_failed", extra={
                "client_id": sub["client_id"], "error": str(e),
            })
            ia = {"operations": 0, "cost_cents": 0}

        ia_ops = int(ia.get("operations", 0))
        ia_cost = int(ia.get("cost_cents", 0))
        # solo facturar si hubo costo real en el periodo; si no, OMITIR
        if ia_cost > 0:
            unit_price = ia_cost // ia_ops if ia_ops > 0 else ia_cost
            items.append({
                "description": f"Consumo IA — {ia_ops} operaciones",
                "quantity": ia_ops if ia_ops > 0 else 1,
                "unit_price_cents": unit_price,
                "amount_cents": ia_cost,
                "sat_key": _SAT_KEY_CONSUMO,
                "unit_sat_key": _UNIT_SAT_KEY_SERVICIO,
                "service_id": None,
            })
            subtotal += ia_cost

    # 2c) mensajes enviados via Centro de Mensajes (concepto unico por periodo)
    if sub["messages_account_id"]:
        try:
            msg = await messages.get_usage(
                sub["messages_account_id"],
                from_ts=period_start.isoformat(),
                to_ts=period_end.isoformat(),
            )
        except Exception as e:
            log.warning("messages_usage_fetch_failed", extra={
                "client_id": sub["client_id"], "error": str(e),
            })
            msg = {"messages": 0, "cost_cents": 0}

        msg_count = int(msg.get("messages", 0))
        msg_cost = int(msg.get("cost_cents", 0))
        # si no hubo mensajes/costo en el periodo, OMITIR (no es error)
        if msg_cost > 0:
            unit_price = msg_cost // msg_count if msg_count > 0 else msg_cost
            items.append({
                "description": f"Mensajes enviados — {msg_count} mensajes",
                "quantity": msg_count if msg_count > 0 else 1,
                "unit_price_cents": unit_price,
                "amount_cents": msg_cost,
                "sat_key": _SAT_KEY_CONSUMO,
                "unit_sat_key": _UNIT_SAT_KEY_SERVICIO,
                "service_id": None,
            })
            subtotal += msg_cost

    if not items:
        return None, 0  # plan free sin consumo

    # 3) promociones
    discount = await apply_promotions(db, client_id=sub["client_id"],
                                       subtotal_cents=subtotal,
                                       period_start=period_start, period_end=period_end)

    # 4) impuestos (IVA 16% sobre subtotal - descuento)
    taxable = max(subtotal - discount, 0)
    tax = round(taxable * 0.16)
    total = taxable + tax

    # 5) insert invoice (draft)
    row = await db.execute(text("""
        INSERT INTO invoices
          (client_id, subscription_id, period_start, period_end,
           subtotal_cents, discount_cents, tax_cents, total_cents, status)
        VALUES (:c, :s, :ps, :pe, :sub, :disc, :tax, :tot, 'draft')
        RETURNING id
    """), {
        "c": sub["client_id"], "s": sub["subscription_id"],
        "ps": period_start, "pe": period_end,
        "sub": subtotal, "disc": discount, "tax": tax, "tot": total,
    })
    invoice_id = row.scalar_one()

    for it in items:
        await db.execute(text("""
            INSERT INTO invoice_items
              (invoice_id, service_id, description, quantity, unit_price_cents,
               amount_cents, sat_key, unit_sat_key)
            VALUES (:i, :sv, :d, :q, :up, :a, :sk, :uk)
        """), {
            "i": invoice_id, "sv": it["service_id"], "d": it["description"],
            "q": it["quantity"], "up": it["unit_price_cents"],
            "a": it["amount_cents"], "sk": it["sat_key"], "uk": it["unit_sat_key"],
        })

    # marcar pending_stamp para que invoice_retry worker lo timbre
    await db.execute(text(
        "UPDATE invoices SET status='pending_stamp' WHERE id=:i"
    ), {"i": invoice_id})

    return invoice_id, total
