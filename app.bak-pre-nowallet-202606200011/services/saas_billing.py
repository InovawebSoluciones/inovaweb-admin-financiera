"""Meta-cobro del SaaS — el CAF se cobra a si mismo su propio consumo.

Modelo:
  - La org 1 (Inovaweb) es la "org plataforma": dueña del catalogo SaaS
    (servicio `saas_transaccion` @ 99 centavos/unidad, plan `caf_saas` @ 9900/mes).
  - Cada organizacion cliente (org_id != 1) se representa como UN cliente de la
    org plataforma: una fila en `clients` con organization_id=1 y datos fiscales
    placeholder (patron app-onboard). Ese "cliente de plataforma" es el sujeto
    de cobro del SaaS.
  - El consumo del motor por esa org se acumula como DEBITOS en `prepaid_ledger`
    (organization_id=1) contra su cliente de plataforma.

POSTPAGO: los accrual son debitos puros que PUEDEN dejar saldo negativo (la org
debe; se liquida mensualmente). NO se valida saldo ni se devuelve 402, y NO se
usa el endpoint /charge (evita recursion: el cobro del SaaS no debe disparar
otro cobro del SaaS). Se inserta directo en `prepaid_ledger`.

El vinculo org -> su cliente de plataforma se guarda en
`organizations.platform_client_id` (FK -> clients.id). Requiere la migracion
035 (ADD COLUMN platform_client_id).
"""

from __future__ import annotations

import json as _json
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import SessionLocal

log = logging.getLogger("saas_billing")

# Org dueña del catalogo SaaS y libro contable del meta-cobro.
PLATFORM_ORG_ID = 1

# Codigos canonicos sembrados en la org plataforma.
_SAAS_SERVICE_CODE = "saas_transaccion"   # 99 centavos/unidad (services)
_SAAS_PLAN_CODE = "caf_saas"              # 9900/mes (plans.monthly_fee_cents)


# ---------------------------------------------------------------------
# alta del "cliente de plataforma" que representa una org
# ---------------------------------------------------------------------


async def register_org_as_platform_client(
    db: AsyncSession, org_id: int, org_name: str
) -> int:
    """Crea (o devuelve) el cliente de plataforma que representa a `org_id`.

    Idempotente por `clients.request_id = f"saas-org-{org_id}"`. Si ya existe esa
    fila se devuelve su id (y se asegura que organizations.platform_client_id
    apunte a el). NO crea wallet en el Medidor: el SaaS se contabiliza solo en
    el prepaid_ledger del CAF. Devuelve el client_id del cliente de plataforma.
    """
    request_id = f"saas-org-{org_id}"

    existing = (await db.execute(
        text("SELECT id FROM clients WHERE request_id = :rid"),
        {"rid": request_id},
    )).scalar()
    if existing is not None:
        client_id = int(existing)
        await db.execute(
            text("UPDATE organizations SET platform_client_id = :cid, updated_at = now() "
                 "WHERE id = :org AND platform_client_id IS DISTINCT FROM :cid"),
            {"cid": client_id, "org": org_id},
        )
        return client_id

    # INSERT del cliente de plataforma en la org 1, con placeholders fiscales
    # (patron app-onboard). RFC XAXX010101000 queda FUERA del unique parcial.
    client_id = (await db.execute(
        text("""
            INSERT INTO clients
              (organization_id, legal_name, trade_name, rfc, cfdi_use, tax_regime,
               zip_code, billing_email, status, request_id)
            VALUES
              (:org, :name, :name, 'XAXX010101000', 'S01', '616',
               '00000', :email, 'active', :rid)
            RETURNING id
        """),
        {"org": PLATFORM_ORG_ID, "name": org_name,
         "email": f"saas-org-{org_id}@inovaweb.com.mx", "rid": request_id},
    )).scalar_one()

    plan_id = (await db.execute(
        text("SELECT id FROM plans WHERE code = :c AND organization_id = :org"),
        {"c": _SAAS_PLAN_CODE, "org": PLATFORM_ORG_ID},
    )).scalar()
    if plan_id is None:
        raise RuntimeError(
            f"plan '{_SAAS_PLAN_CODE}' no existe en la org plataforma {PLATFORM_ORG_ID}"
        )
    await db.execute(
        text("INSERT INTO subscriptions (client_id, plan_id, status, started_at, organization_id) "
             "VALUES (:cid, :pid, 'active', CURRENT_DATE, :org)"),
        {"cid": client_id, "pid": int(plan_id), "org": PLATFORM_ORG_ID},
    )
    await db.execute(
        text("UPDATE organizations SET platform_client_id = :cid, updated_at = now() "
             "WHERE id = :org"),
        {"cid": client_id, "org": org_id},
    )
    log.info("saas_platform_client_created",
             extra={"org_id": org_id, "client_id": client_id})
    return client_id


# ---------------------------------------------------------------------
# accrual por transaccion del motor (best-effort, sesion propia)
# ---------------------------------------------------------------------


async def accrue_transaction(org_id: int, source_ref: str) -> None:
    """Acumula 1 transaccion SaaS (99c) como debito contra el cliente de plataforma.

    BEST-EFFORT: corre en su PROPIA sesion (SessionLocal + commit), todo envuelto
    en try/except que loguea y NUNCA propaga. Idempotente por
    idempotency_key = f"saas-tx-{source_ref}". POSTPAGO: debito puro, puede dejar
    saldo negativo, sin validar saldo.
    """
    try:
        if org_id == PLATFORM_ORG_ID:
            return  # la plataforma no se cobra a si misma (evita recursion)

        async with SessionLocal() as s:
            platform_client_id = (await s.execute(
                text("SELECT platform_client_id FROM organizations WHERE id = :org"),
                {"org": org_id},
            )).scalar()
            if platform_client_id is None:
                log.warning("saas_accrue_no_platform_client",
                            extra={"org_id": org_id, "source_ref": source_ref})
                return

            precio = (await s.execute(
                text("SELECT unit_price_cents FROM services "
                     "WHERE code = :c AND organization_id = :org"),
                {"c": _SAAS_SERVICE_CODE, "org": PLATFORM_ORG_ID},
            )).scalar()
            if precio is None:
                log.warning("saas_accrue_no_service",
                            extra={"org_id": org_id, "source_ref": source_ref})
                return

            meta = _json.dumps({"src_org_id": org_id, "source_ref": source_ref})
            await s.execute(
                text("INSERT INTO prepaid_ledger "
                     "(client_id, organization_id, kind, amount_cents, service_code, units, source, idempotency_key, meta) "
                     "VALUES (:cid, :org, 'debit', :amt, :sc, 1, 'saas_usage', :k, CAST(:m AS jsonb)) "
                     "ON CONFLICT (client_id, idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING"),
                {"cid": int(platform_client_id), "org": PLATFORM_ORG_ID,
                 "amt": int(precio), "sc": _SAAS_SERVICE_CODE,
                 "k": f"saas-tx-{source_ref}", "m": meta},
            )
            await s.commit()
    except Exception as e:  # nunca propagar: el meta-cobro es best-effort
        log.error("saas_accrue_transaction_failed",
                  extra={"org_id": org_id, "source_ref": source_ref, "error": str(e)})


# ---------------------------------------------------------------------
# cuota mensual del SaaS (cierre de periodo)
# ---------------------------------------------------------------------


async def run_saas_monthly_billing(db: AsyncSession, period: str) -> dict:
    """Acumula la cuota mensual del plan caf_saas a cada org cliente activa.

    `period` = 'YYYY-MM'. Por cada org con status='active', id != 1,
    platform_client_id NOT NULL y suscripcion activa al caf_saas, inserta un
    debito por monthly_fee_cents. Idempotente por
    idempotency_key = f"saas-fee-{platform_client_id}-{period}".
    Devuelve {period, orgs_billed, total_cents}.
    """
    monthly_fee = (await db.execute(
        text("SELECT monthly_fee_cents FROM plans WHERE code = :c AND organization_id = :org"),
        {"c": _SAAS_PLAN_CODE, "org": PLATFORM_ORG_ID},
    )).scalar()
    if monthly_fee is None:
        raise RuntimeError(
            f"plan '{_SAAS_PLAN_CODE}' no existe en la org plataforma {PLATFORM_ORG_ID}"
        )
    monthly_fee = int(monthly_fee)

    rows = (await db.execute(
        text("""
            SELECT o.id AS org_id, o.platform_client_id AS client_id
            FROM organizations o
            JOIN subscriptions sub ON sub.client_id = o.platform_client_id
                                  AND sub.status = 'active'
            JOIN plans p ON p.id = sub.plan_id
                        AND p.code = :plan
                        AND p.organization_id = :porg
            WHERE o.status = 'active'
              AND o.id <> :porg
              AND o.platform_client_id IS NOT NULL
        """),
        {"plan": _SAAS_PLAN_CODE, "porg": PLATFORM_ORG_ID},
    )).mappings().all()

    orgs_billed = 0
    total_cents = 0
    for r in rows:
        platform_client_id = int(r["client_id"])
        meta = _json.dumps({"period": period})
        res = await db.execute(
            text("INSERT INTO prepaid_ledger "
                 "(client_id, organization_id, kind, amount_cents, service_code, units, source, idempotency_key, meta) "
                 "VALUES (:cid, :org, 'debit', :amt, NULL, NULL, 'saas_fee', :k, CAST(:m AS jsonb)) "
                 "ON CONFLICT (client_id, idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING"),
            {"cid": platform_client_id, "org": PLATFORM_ORG_ID, "amt": monthly_fee,
             "k": f"saas-fee-{platform_client_id}-{period}", "m": meta},
        )
        if res.rowcount and res.rowcount > 0:
            orgs_billed += 1
            total_cents += monthly_fee

    await db.commit()
    log.info("saas_monthly_billing_done",
             extra={"period": period, "orgs_billed": orgs_billed, "total_cents": total_cents})
    return {"period": period, "orgs_billed": orgs_billed, "total_cents": total_cents}


# ---------------------------------------------------------------------
# estado de cuenta SaaS de una org
# ---------------------------------------------------------------------


async def get_saas_account(db: AsyncSession, org_id: int) -> dict | None:
    """Estado de cuenta SaaS de la org frente a la plataforma.

    None si la org no tiene cliente de plataforma. Si lo tiene:
      - balance_cents: saldo del cliente de plataforma (v_client_balance);
        NEGATIVO = la org debe (postpago).
      - usage_mes_cents: suma de debitos del MES EN CURSO con
        source in ('saas_usage','saas_fee').
    """
    platform_client_id = (await db.execute(
        text("SELECT platform_client_id FROM organizations WHERE id = :org"),
        {"org": org_id},
    )).scalar()
    if platform_client_id is None:
        return None
    platform_client_id = int(platform_client_id)

    balance_cents = int((await db.execute(
        text("SELECT balance_cents FROM v_client_balance WHERE client_id = :c"),
        {"c": platform_client_id},
    )).scalar() or 0)

    usage_mes_cents = int((await db.execute(
        text("SELECT coalesce(sum(amount_cents), 0) FROM prepaid_ledger "
             "WHERE client_id = :c AND kind = 'debit' "
             "AND source IN ('saas_usage', 'saas_fee') "
             "AND created_at >= date_trunc('month', now())"),
        {"c": platform_client_id},
    )).scalar() or 0)

    return {"org_id": org_id, "platform_client_id": platform_client_id,
            "balance_cents": balance_cents, "usage_mes_cents": usage_mes_cents}
