#!/usr/bin/env bash
# Cierre mensual del SaaS: acumula la cuota del plan caf_saas ($99) a cada org
# cliente activa. Idempotente por (org, periodo) -> correr de mas NO duplica.
# Pensado para crontab el dia 1 de cada mes. Usa el mes EN CURSO.
set -uo pipefail

cd /opt/inovaweb-admin-financiera || exit 1
PERIOD="$(date +%Y-%m)"

docker compose exec -T admin_financiera python -c "
import asyncio
from app.core.database import SessionLocal
from app.services.saas_billing import run_saas_monthly_billing
async def main():
    async with SessionLocal() as db:
        print(await run_saas_monthly_billing(db, '${PERIOD}'))
asyncio.run(main())
"
