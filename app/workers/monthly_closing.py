"""Worker: cierre mensual el dia 1 a las 02:00 hora local.

Ejecutar via cron en docker-compose o systemd:
  0 2 1 * *  python -m app.workers.monthly_closing
"""

from __future__ import annotations

import asyncio
import logging

from app.core.database import SessionLocal
from app.core.observability import configure_logging
from app.services.billing import run_monthly_closing


async def main() -> None:
    configure_logging()
    log = logging.getLogger("worker.monthly_closing")
    async with SessionLocal() as db:
        summary = await run_monthly_closing(db)
        await db.commit()
    log.info("monthly_closing_done", extra=summary)


if __name__ == "__main__":
    asyncio.run(main())
