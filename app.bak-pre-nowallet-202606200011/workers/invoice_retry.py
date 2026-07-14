"""Worker: reintenta timbrado de facturas en 'pending_stamp' o 'failed'.

Ejecutar cada 5-10 min via cron:
  */10 * * * *  python -m app.workers.invoice_retry
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text

from app.core.database import SessionLocal
from app.core.observability import configure_logging
from app.services.invoicing import stamp_invoice

MAX_PER_RUN = 50


async def main() -> None:
    configure_logging()
    log = logging.getLogger("worker.invoice_retry")
    async with SessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT id FROM invoices
            WHERE status IN ('pending_stamp','failed')
            ORDER BY created_at ASC LIMIT :n
        """), {"n": MAX_PER_RUN})).all()
        ok, fail = 0, 0
        for (iid,) in rows:
            try:
                await stamp_invoice(db, iid)
                await db.commit()
                ok += 1
            except Exception as e:
                await db.rollback()
                fail += 1
                log.error("retry_failed", extra={"invoice_id": iid, "error": str(e)})
        log.info("invoice_retry_done", extra={"ok": ok, "fail": fail, "total": len(rows)})


if __name__ == "__main__":
    asyncio.run(main())
