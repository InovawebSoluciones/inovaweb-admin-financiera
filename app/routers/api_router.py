"""API JSON /api/v2/*. Para clientes maquina (otros sistemas Inovaweb)."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import bind_actor
from app.core.clients.medidor_client import MedidorClient
from app.core.database import get_db
from app.core.jwt_auth import CurrentUser, require_roles
from app.services.onboarding import OnboardClientPayload, OnboardingError, onboard_client

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

    En el modelo PREPAGO la fuente de verdad del saldo es la WALLET del Medidor
    (provisionada en el alta). Se lee con medidor.get_balance(medidor_account_id);
    el CAF nunca duplica el saldo. Si el cliente aun no tiene wallet, se reporta 0.
    """
    c = (await db.execute(
        text("SELECT medidor_account_id, status FROM clients WHERE id=:id"),
        {"id": cid},
    )).mappings().first()
    if not c:
        raise HTTPException(404, "cliente no existe")
    if not c["medidor_account_id"]:
        return {"client_id": cid, "status": c["status"],
                "balance_cents": 0, "note": "sin wallet provisionada en medidor"}
    medidor = MedidorClient()
    try:
        bal = await medidor.get_balance(c["medidor_account_id"])
    finally:
        await medidor.close()
    return {"client_id": cid, "status": c["status"],
            "wallet_id": c["medidor_account_id"], **bal}


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
