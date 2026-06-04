"""Worker: notifica facturas vencidas via centro-mensajes.

Ejecutar cada 6 horas:
  0 */6 * * *  python -m app.workers.overdue_notifier
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text

from app.core.clients.messages_client import MessagesClient
from app.core.database import SessionLocal
from app.core.observability import configure_logging


async def main() -> None:
    configure_logging()
    log = logging.getLogger("worker.overdue_notifier")
    async with SessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT i.id, i.total_cents, i.created_at, c.id AS client_id,
                   c.billing_email, c.legal_name
            FROM invoices i JOIN clients c ON c.id = i.client_id
            WHERE i.status = 'stamped'
              AND i.paid_at IS NULL
              AND i.created_at < now() - interval '15 days'
              AND NOT EXISTS (
                SELECT 1 FROM audit_log a
                WHERE a.entity_type = 'invoices' AND a.entity_id = i.id
                  AND a.action = 'overdue_notified'
                  AND a.occurred_at > now() - interval '7 days'
              )
            LIMIT 200
        """))).mappings().all()

        if not rows:
            log.info("overdue_no_pending")
            return

        msg = MessagesClient()
        sent = 0
        try:
            for r in rows:
                try:
                    await msg.send_email(
                        client_id=str(r["client_id"]),
                        service_id="recordatorio-vencimiento",
                        template_id="caf-recordatorio-vencimiento-tplus5",
                        to={"email": r["billing_email"], "name": r["legal_name"]},
                        variables=_overdue_variables(r),
                        meta={"caf_invoice_id": str(r["id"])},
                    )
                    await db.execute(text("""
                        INSERT INTO audit_log
                          (actor_user_id, entity_type, entity_id, action, new_values)
                        VALUES (NULL, 'invoices', :i, 'overdue_notified',
                                jsonb_build_object('email', :e))
                    """), {"i": r["id"], "e": r["billing_email"]})
                    sent += 1
                except Exception as e:
                    log.error("overdue_send_failed",
                              extra={"invoice_id": r["id"], "error": str(e)})
            await db.commit()
        finally:
            await msg.close()
        log.info("overdue_notify_done", extra={"sent": sent, "candidates": len(rows)})


def _overdue_variables(r) -> dict:
    """Variables tipadas para la plantilla 'caf-recordatorio-vencimiento-tplus5'.

    El monto se formatea humano a partir de centavos enteros (sin floats en
    almacenamiento; la division es solo presentacion).
    """
    total_humano = f"${r['total_cents'] / 100:,.2f} MXN"
    return {
        "razon_social": r["legal_name"],
        "factura_folio": str(r["id"]),
        "monto_total_humano": total_humano,
        "fecha_emision": f"{r['created_at']:%Y-%m-%d}",
    }


if __name__ == "__main__":
    asyncio.run(main())
