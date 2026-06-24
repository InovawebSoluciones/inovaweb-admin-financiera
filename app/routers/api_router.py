"""API JSON /api/v2/*. Para clientes maquina (otros sistemas Inovaweb)."""

from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import bind_actor
from app.core.clients.medidor_client import MedidorClient
from app.core.config import get_settings
from app.core.database import get_db
from app.core.jwt_auth import CurrentUser, require_roles
from app.core.tenancy import assert_client_in_org, resolve_app_org
from app.services.onboarding import OnboardClientPayload, OnboardingError, onboard_client
from app.services.prepago import PrepagoError, initiate_charge
from app.services.saas_billing import accrue_transaction

log = logging.getLogger("api_v2")

router = APIRouter(prefix="/api/v2", tags=["api"])

_WRITE = require_roles("super_admin", "finanzas")
_READ  = require_roles("super_admin", "finanzas", "lectura")


# ---------------------------------------------------------------------
# clients
# ---------------------------------------------------------------------


class CreateClientBody(BaseModel):
    legal_name: str
    trade_name: str | None = None
    rfc: str
    cfdi_use: str = "G03"
    tax_regime: str
    zip_code: str
    billing_email: EmailStr
    contact_phone: str | None = None
    plan_code: str
    titular_full_name: str
    titular_email: EmailStr


class CreateClientResponse(BaseModel):
    client_id: int
    user_id: int
    temp_password: str
    wallet_id: str


@router.post("/clients", response_model=CreateClientResponse,
              status_code=status.HTTP_201_CREATED)
async def api_create_client(
    body: CreateClientBody,
    request: Request,
    user: CurrentUser = Depends(_WRITE),
    db: AsyncSession = Depends(get_db),
) -> CreateClientResponse:
    await bind_actor(db, actor_user_id=user.id,
                      actor_ip=request.client.host if request.client else None,
                      request_id=getattr(request.state, "request_id", None))
    try:
        r = await onboard_client(
            db,
            OnboardClientPayload(**body.model_dump()),
            actor_user_id=user.id,
            actor_ip=request.client.host if request.client else None,
            request_id=getattr(request.state, "request_id", None),
        )
    except OnboardingError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e
    return CreateClientResponse(
        client_id=r.client_id, user_id=r.user_id,
        temp_password=r.temp_password, wallet_id=r.wallet_id,
    )


@router.get("/clients/{cid}/balance")
async def api_client_balance(
    cid: int,
    user: CurrentUser = Depends(_READ),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Saldo consolidado del cliente.

    Fuente de verdad: prepaid_ledger del CAF (SUM credits - SUM debits).
    El wallet del Medidor fue eliminado; el balance vive aqui.
    """
    c = (await db.execute(
        text("SELECT status FROM clients WHERE id=:id"),
        {"id": cid},
    )).mappings().first()
    if not c:
        raise HTTPException(404, "cliente no existe")
    row = (await db.execute(
        text("""
            SELECT COALESCE(SUM(CASE WHEN kind='credit' THEN amount_cents
                                     ELSE -amount_cents END), 0) AS balance_cents
            FROM prepaid_ledger WHERE client_id=:id
        """),
        {"id": cid},
    )).first()
    balance_cents = int(row[0]) if row else 0
    return {"client_id": cid, "status": c["status"], "balance_cents": balance_cents, "currency": "MXN"}


# ---------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------


@router.get("/reports/income")
async def api_income_report(
    user: CurrentUser = Depends(_READ),
    db: AsyncSession = Depends(get_db),
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
    group_by: str = Query("month", regex="^(month|client|product)$"),
) -> dict:
    if group_by == "month":
        sql = """
          SELECT to_char(date_trunc('month', created_at), 'YYYY-MM') AS bucket,
                 sum(total_cents) AS total_cents, count(*) AS invoices
          FROM invoices
          WHERE status IN ('stamped','paid')
            AND created_at::date BETWEEN :f AND :t
          GROUP BY 1 ORDER BY 1
        """
    elif group_by == "client":
        sql = """
          SELECT c.legal_name AS bucket, sum(i.total_cents) AS total_cents,
                 count(*) AS invoices
          FROM invoices i JOIN clients c ON c.id = i.client_id
          WHERE i.status IN ('stamped','paid')
            AND i.created_at::date BETWEEN :f AND :t
          GROUP BY 1 ORDER BY 2 DESC
        """
    else:  # product
        sql = """
          SELECT COALESCE(s.name, ii.description) AS bucket,
                 sum(ii.amount_cents) AS total_cents, count(*) AS items
          FROM invoice_items ii
          LEFT JOIN services s ON s.id = ii.service_id
          JOIN invoices i ON i.id = ii.invoice_id
          WHERE i.status IN ('stamped','paid')
            AND i.created_at::date BETWEEN :f AND :t
          GROUP BY 1 ORDER BY 2 DESC
        """
    rows = (await db.execute(
        text(sql), {"f": from_date, "t": to_date}
    )).mappings().all()
    return {
        "from": from_date.isoformat(), "to": to_date.isoformat(),
        "group_by": group_by, "rows": [dict(r) for r in rows],
    }


# ---------------------------------------------------------------------
# billing trigger manual
# ---------------------------------------------------------------------


@router.post("/billing/run-closing", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def api_run_closing(
    request: Request,
    user: CurrentUser = Depends(require_roles("super_admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Trigger manual del cierre mensual.

    STUB (501) para el piloto PREPAGO: no hay cierre mensual porque el cobro es
    por recarga anticipada de wallet (no postpago). El endpoint queda expuesto y
    auditado para conservar el contrato del API; el cierre real (Fase 4: cargos
    de suscripcion + factura CFDI 4.0 via PAC) se habilitara con run_monthly_closing.
    """
    await bind_actor(db, actor_user_id=user.id,
                      actor_ip=request.client.host if request.client else None,
                      request_id=getattr(request.state, "request_id", None))
    return {
        "status": "not_implemented",
        "detail": "cierre mensual no aplica en el modelo prepago del piloto; "
                  "se habilita en Fase 4 (postpago + CFDI)",
    }


# ---------------------------------------------------------------------
# plan-limits + precios (solo lectura) — lo consume Scraping (Nivel 3)
# para el medidor de 'uso vs tope' y para mostrar precios DINÁMICOS del
# catálogo (services.unit_price_cents). NO toca cobro. Auth: llave
# compartida Scraping<->CAF (SCRAPING_ADMIN_KEY).
# ---------------------------------------------------------------------
class PlanLimitsResponse(BaseModel):
    client_id: int
    plan_code: str | None = None
    plan_name: str | None = None
    limits: dict[str, int] = {}
    prices: dict[str, int] = {}


@router.get('/clients/{client_id}/plan-limits', response_model=PlanLimitsResponse)
async def api_client_plan_limits(
    client_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> PlanLimitsResponse:
    org = await resolve_app_org(request, db)
    await assert_client_in_org(db, client_id, org)
    rows = (await db.execute(text('''
        SELECT p.code AS plan_code, p.name AS plan_name,
               s.code AS servicio, pi.hard_limit_units, s.unit_price_cents
        FROM subscriptions sub
        JOIN plans p ON p.id = sub.plan_id
        JOIN plan_items pi ON pi.plan_id = sub.plan_id
        JOIN services s ON s.id = pi.service_id
        WHERE sub.client_id = :cid AND sub.status = 'active'
        ORDER BY s.id
    '''), {'cid': client_id})).mappings().all()
    if not rows:
        raise HTTPException(status.HTTP_404_NOT_FOUND, 'cliente sin suscripcion activa')
    limits = {r['servicio']: int(r['hard_limit_units']) for r in rows}
    prices = {r['servicio']: int(r['unit_price_cents']) for r in rows}
    return PlanLimitsResponse(
        client_id=client_id,
        plan_code=rows[0]['plan_code'],
        plan_name=rows[0]['plan_name'],
        limits=limits,
        prices=prices,
    )


# ---------------------------------------------------------------------
# B: saldo prepago NATIVO del CAF + cargo de consumo (app-facing, Bearer)
# El saldo vive en el CAF (libro prepaid_ledger). El Medidor NO guarda saldo:
# solo registra consumo (events/track). Aqui el CAF tarifica, valida saldo y debita.
# ---------------------------------------------------------------------
import json as _json


class ChargeBody(BaseModel):
    service_code: str
    units: int = 1
    idempotency_key: str | None = None
    meta: dict | None = None


class ChargeResponse(BaseModel):
    ok: bool
    client_id: int
    service_code: str
    units: int
    charged_cents: int
    balance_cents: int
    idempotent_replay: bool = False


@router.get("/clients/{client_id}/prepaid-balance")
async def api_client_prepaid_balance(
    client_id: int, request: Request, db: AsyncSession = Depends(get_db)
) -> dict:
    """Saldo prepago del cliente calculado del libro del CAF (NO del Medidor)."""
    org = await resolve_app_org(request, db)
    await assert_client_in_org(db, client_id, org)
    bal = (await db.execute(
        text("SELECT balance_cents FROM v_client_balance WHERE client_id=:c"),
        {"c": client_id},
    )).scalar()
    if bal is None:
        raise HTTPException(404, "cliente no existe")
    return {"client_id": client_id, "balance_cents": int(bal)}


@router.post("/clients/{client_id}/recharge")
async def api_client_recharge(
    client_id: int, request: Request, db: AsyncSession = Depends(get_db)
) -> dict:
    """Inicia una recarga de saldo prepago (maquina-a-maquina, Bearer de app).

    Equivalente app-facing de `POST /portal/recharge`: crea el intento de cobro
    en el Hub (pasarela ACTIVA elegida en el panel; fail-safe HUB_GATEWAY) y
    devuelve la URL de checkout para que la app duena (p.ej. LiaForge) redirija
    al cliente. El webhook `hub-payment-paid` acredita el saldo al confirmarse el
    pago. Body JSON: `{"amount_cents": int}` (min $50 MXN; tope MAX_RECARGA_CENTS).
    Reusa el MISMO piso/techo y la misma `initiate_charge` que el portal HTML.
    """
    org = await resolve_app_org(request, db)
    await assert_client_in_org(db, client_id, org)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        amount_cents = int(body.get("amount_cents") or body.get("amount") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "amount_cents invalido")
    # mismo piso/techo que /portal/recharge (piso $50 MXN, tope configurable).
    max_recarga = get_settings().MAX_RECARGA_CENTS
    if amount_cents < 5000:
        raise HTTPException(400, "monto minimo de recarga $50 MXN")
    if amount_cents > max_recarga:
        raise HTTPException(400, f"monto maximo de recarga ${max_recarga / 100:,.2f} MXN")
    c = (await db.execute(
        text("SELECT * FROM clients WHERE id=:id"), {"id": client_id}
    )).mappings().first()
    if not c:
        raise HTTPException(404, "cliente no existe")
    if c["status"] != "active":
        raise HTTPException(402, "cuenta suspendida")
    if not c.get("hub_account_id"):
        raise HTTPException(409, "cuenta no provisionada en hub")
    try:
        intent = await initiate_charge(
            db, client=dict(c), amount_cents=amount_cents,
            purpose="wallet_recharge",
            description=f"Recarga saldo cliente {client_id}",
            actor_user_id=None,
            actor_ip=request.client.host if request.client else None,
            request_id=getattr(request.state, "request_id", None),
        )
    except PrepagoError as e:
        log.error("api_recharge_failed", extra={"caf_client_id": client_id,
                                                 "error": str(e)})
        raise HTTPException(502, "no se pudo iniciar el cobro") from e
    url = intent.get("checkout_url") or intent.get("url")
    if not url:
        raise HTTPException(502, "el hub no devolvio url de checkout")
    return {"checkout_url": url, "amount_cents": amount_cents}


@router.get("/clients/{client_id}/ledger")
async def api_client_ledger(
    client_id: int, request: Request, db: AsyncSession = Depends(get_db),
    limit: int = 50,
) -> dict:
    """Movimientos del libro prepago + consumo del mes en curso (app-facing)."""
    org = await resolve_app_org(request, db)
    await assert_client_in_org(db, client_id, org)
    limit = max(1, min(int(limit), 200))
    rows = (await db.execute(
        text("SELECT created_at, kind, service_code, units, amount_cents, source "
             "FROM prepaid_ledger WHERE client_id=:c "
             "ORDER BY created_at DESC LIMIT :l"),
        {"c": client_id, "l": limit},
    )).all()
    consumo_mes = int((await db.execute(
        text("SELECT coalesce(sum(amount_cents),0) FROM prepaid_ledger "
             "WHERE client_id=:c AND kind='debit' "
             "AND created_at >= date_trunc('month', now())"),
        {"c": client_id},
    )).scalar() or 0)
    return {
        "client_id": client_id,
        "consumo_mes_cents": consumo_mes,
        "movimientos": [
            {
                "fecha": r[0].isoformat(),
                "kind": r[1],
                "service_code": r[2],
                "units": int(r[3]) if r[3] is not None else None,
                "amount_cents": int(r[4]),
                "source": r[5],
            }
            for r in rows
        ],
    }


@router.get("/services")
async def api_services_catalog(request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    """Catálogo de servicios activos de la organización con su precio (app-facing)."""
    org = await resolve_app_org(request, db)
    rows = (await db.execute(
        text("SELECT code, name, unit, unit_price_cents FROM services "
             "WHERE is_active AND organization_id = :org ORDER BY unit_price_cents"),
        {"org": org},
    )).all()
    return {
        "services": [
            {"code": r[0], "name": r[1], "unit": r[2], "unit_price_cents": int(r[3])}
            for r in rows
        ]
    }


@router.post("/clients/{client_id}/charge", response_model=ChargeResponse)
async def api_client_charge(
    client_id: int, body: ChargeBody, request: Request,
    db: AsyncSession = Depends(get_db),
) -> ChargeResponse:
    """Cobra `units` de `service_code` al cliente: tarifica (services.unit_price_cents),
    valida saldo del CAF y debita en el libro prepaid_ledger. 402 si no alcanza."""
    org = await resolve_app_org(request, db)
    await assert_client_in_org(db, client_id, org)
    await db.execute(text("SELECT pg_advisory_xact_lock(:c)"), {"c": client_id})
    if body.units <= 0:
        raise HTTPException(422, "units debe ser > 0")

    # idempotencia: cargo ya registrado -> replay
    if body.idempotency_key:
        prev = (await db.execute(
            text("SELECT amount_cents FROM prepaid_ledger "
                 "WHERE client_id=:c AND idempotency_key=:k"),
            {"c": client_id, "k": body.idempotency_key},
        )).first()
        if prev:
            bal = int((await db.execute(
                text("SELECT balance_cents FROM v_client_balance WHERE client_id=:c"),
                {"c": client_id},
            )).scalar() or 0)
            return ChargeResponse(ok=True, client_id=client_id, service_code=body.service_code,
                                  units=body.units, charged_cents=int(prev[0]),
                                  balance_cents=bal, idempotent_replay=True)

    svc = (await db.execute(
        text("SELECT unit_price_cents FROM services "
             "WHERE code=:c AND is_active AND organization_id=:org"),
        {"c": body.service_code, "org": org},
    )).first()
    if not svc:
        raise HTTPException(404, f"servicio {body.service_code} no existe")
    amount = int(svc[0]) * body.units

    bal = int((await db.execute(
        text("SELECT balance_cents FROM v_client_balance WHERE client_id=:c"),
        {"c": client_id},
    )).scalar() or 0)
    if bal < amount:
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            detail={"error": "saldo_insuficiente", "balance_cents": bal, "required_cents": amount},
        )

    ledger_id = (await db.execute(
        text("INSERT INTO prepaid_ledger (client_id, organization_id, kind, amount_cents, service_code, units, source, idempotency_key, meta) "
             "VALUES (:c, :org, 'debit', :a, :sc, :u, 'consumo', :k, CAST(:m AS jsonb)) RETURNING id"),
        {"c": client_id, "org": org, "a": amount, "sc": body.service_code, "u": body.units,
         "k": body.idempotency_key, "m": _json.dumps(body.meta) if body.meta else None},
    )).scalar_one()
    await db.commit()
    # meta-cobro del SaaS: 1 transaccion del motor = un accrual a la org (best-effort,
    # sesion propia; org plataforma se omite dentro; jamas rompe el cargo del cliente).
    await accrue_transaction(org, f"charge-{ledger_id}")
    return ChargeResponse(ok=True, client_id=client_id, service_code=body.service_code,
                          units=body.units, charged_cents=amount, balance_cents=bal - amount)


# ---------------------------------------------------------------------
# B: alta app-facing (Swigg self-service) — Bearer, sin JWT ni datos fiscales.
# Crea cliente + wallet Medidor + suscripcion + grant inicial del plan al prepaid_ledger.
# Datos fiscales placeholder (publico en general); se completan al facturar.
# ---------------------------------------------------------------------
class AppOnboardBody(BaseModel):
    trade_name: str
    billing_email: EmailStr
    plan_code: str
    external_ref: str | None = None   # id de la empresa en la app (idempotencia + link)
    promo_code: str | None = None     # cupón de bono de créditos para el cliente
    referral_code: str | None = None  # código del distribuidor referidor (genera comisión)


class AppOnboardResponse(BaseModel):
    client_id: int
    wallet_id: str | None = None   # wallet del Medidor eliminado (ledger nativo) — fix 2026-06-23
    plan_code: str
    granted_cents: int                # crédito total acreditado (grant + bono promo)
    promo_applied: bool = False
    promo_code: str | None = None
    discount_pct: float | None = None
    bonus_cents: int = 0


@router.post("/apps/onboard", response_model=AppOnboardResponse, status_code=status.HTTP_201_CREATED)
async def api_app_onboard(
    body: AppOnboardBody, request: Request, db: AsyncSession = Depends(get_db),
) -> AppOnboardResponse:
    org = await resolve_app_org(request, db)
    rid = f"app-{body.external_ref}" if body.external_ref else None
    payload = OnboardClientPayload(
        legal_name=body.trade_name, trade_name=body.trade_name,
        rfc="XAXX010101000", cfdi_use="S01", tax_regime="616",
        zip_code="00000", billing_email=body.billing_email, contact_phone=None,
        plan_code=body.plan_code,
        titular_full_name=body.trade_name, titular_email=body.billing_email,
        organization_id=org,
    )
    try:
        r = await onboard_client(db, payload, actor_user_id=1, actor_ip=None, request_id=rid)
    except OnboardingError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e

    # el plan debe pertenecer a la org de la llave (aislamiento del catalogo)
    grant = (await db.execute(
        text("SELECT monthly_credit_cents FROM plans WHERE code=:c AND organization_id=:org"),
        {"c": body.plan_code, "org": org},
    )).scalar()
    granted = int(grant) if grant else 0
    grant_key = f"grant-{r.client_id}-{body.plan_code}"

    # Reintento idempotente del alta: si el grant ya se acreditó, NO se re-aplica
    # (ni el grant ni el bono/uso de la promo). Evita doble crédito y doble conteo.
    existing = (await db.execute(
        text("SELECT amount_cents FROM prepaid_ledger WHERE client_id=:c AND idempotency_key=:k"),
        {"c": r.client_id, "k": grant_key},
    )).scalar()
    if existing is not None:
        return AppOnboardResponse(client_id=r.client_id, wallet_id=r.wallet_id,
                                 plan_code=body.plan_code, granted_cents=int(existing))

    # Código de promoción (distribuidor): si es válido, su discount_pct se aplica
    # como BONO de crédito sobre el grant del plan (al contratar). El front del
    # registro lo manda la app dueña; el CAF solo valida y aplica. Idempotente: el
    # grant tiene idempotency_key, así que un reintento no duplica el bono.
    promo_applied = False
    discount_pct: float | None = None
    bonus = 0
    promo_meta: dict = {}
    if body.promo_code and granted > 0:
        promo = (await db.execute(text("""
            SELECT id, discount_pct, distributor_id, max_uses, uses_count
            FROM promotions
            WHERE organization_id IN (:org, 1) AND code=:code AND is_active=true
              AND now() BETWEEN valid_from AND valid_to
              AND discount_pct IS NOT NULL
            ORDER BY organization_id DESC
            LIMIT 1
        """), {"org": org, "code": body.promo_code.strip().upper()})).mappings().first()
        if promo and (promo["max_uses"] is None or promo["uses_count"] < promo["max_uses"]):
            # cuenta el uso de forma atómica (respeta max_uses ante carreras)
            used = (await db.execute(text(
                "UPDATE promotions SET uses_count = uses_count + 1 "
                "WHERE id=:id AND (max_uses IS NULL OR uses_count < max_uses) RETURNING id"
            ), {"id": promo["id"]})).first()
            if used:
                discount_pct = float(promo["discount_pct"])
                bonus = round(granted * discount_pct / 100)
                promo_applied = True
                promo_meta = {"promo_code": body.promo_code.strip().upper(),
                              "distributor_id": promo["distributor_id"],
                              "discount_pct": discount_pct, "bonus_cents": bonus}

    total = granted + bonus
    if total > 0:
        meta = {"plan": body.plan_code, "external_ref": body.external_ref}
        meta.update(promo_meta)
        await db.execute(
            text("INSERT INTO prepaid_ledger (client_id, organization_id, kind, amount_cents, source, idempotency_key, meta) "
                 "VALUES (:c, :org, 'credit', :a, 'grant_plan', :k, CAST(:m AS jsonb)) "
                 "ON CONFLICT (client_id, idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING"),
            {"c": r.client_id, "org": org, "a": total, "k": grant_key,
             "m": _json.dumps(meta)},
        )
    # Vincular al distribuidor referidor si se proporcionó código de referido
    if body.referral_code:
        ref_code = body.referral_code.strip().upper()
        dist_id = (await db.execute(text("""
            SELECT id FROM distributors
            WHERE UPPER(referral_code) = :c AND is_active
              AND organization_id IN (:org, 1)
            LIMIT 1
        """), {"c": ref_code, "org": org})).scalar()
        if dist_id:
            await db.execute(
                text("UPDATE clients SET referral_distributor_id = :d WHERE id = :c"),
                {"d": dist_id, "c": r.client_id},
            )

    return AppOnboardResponse(
        client_id=r.client_id, wallet_id=r.wallet_id, plan_code=body.plan_code,
        granted_cents=total, promo_applied=promo_applied,
        promo_code=(body.promo_code.strip().upper() if promo_applied else None),
        discount_pct=discount_pct, bonus_cents=bonus,
    )
