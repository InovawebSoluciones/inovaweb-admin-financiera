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
            SELECT i.id, i.total_cents, i.created_at, c.billing_email, c.legal_name,
                   c.messages_account_id
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
                        to=r["billing_email"],
                        subject=f"Factura {r['id']} vencida",
                        html=_render_overdue(r),
                        account_id=r["messages_account_id"],
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


def _render_overdue(r) -> str:
    total = r["total_cents"] / 100
    return f"""
    <p>Hola {r['legal_name']},</p>
    <p>La factura #{r['id']} por <b>${total:,.2f} MXN</b> emitida el
    {r['created_at']:%Y-%m-%d} se encuentra vencida.</p>
    <p>Por favor liquidela cuanto antes para evitar la suspension del servicio.</p>
    <p>Gracias.<br>Inovaweb</p>
    """


if __name__ == "__main__":
    asyncio.run(main())
