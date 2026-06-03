"""API JSON /api/v2/*. Para clientes maquina (otros sistemas Inovaweb)."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import bind_actor
from app.core.clients.finanzas_client import FinanzasClient
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
    api_keys: dict[str, str]


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
        temp_password=r.temp_password, api_keys=r.api_keys,
    )


@router.get("/clients/{cid}/balance")
async def api_client_balance(
    cid: int,
    user: CurrentUser = Depends(_READ),
    db: AsyncSession = Depends(get_db),
) -> dict:
    c = (await db.execute(
        text("SELECT finanzas_account_id, status FROM clients WHERE id=:id"),
        {"id": cid},
    )).mappings().first()
    if not c:
        raise HTTPException(404, "cliente no existe")
    if not c["finanzas_account_id"]:
        return {"client_id": cid, "balance_cents": 0, "note": "no provisionado en finanzas"}
    fin = FinanzasClient()
    try:
        bal = await fin.get_balance(c["finanzas_account_id"])
    finally:
        await fin.close()
    return {"client_id": cid, "status": c["status"], **bal}


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


@router.post("/billing/run-closing")
async def api_run_closing(
    request: Request,
    user: CurrentUser = Depends(require_roles("super_admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await bind_actor(db, actor_user_id=user.id,
                      actor_ip=request.client.host if request.client else None,
                      request_id=getattr(request.state, "request_id", None))
    from app.services.billing import run_monthly_closing
    summary = await run_monthly_closing(db)
    return {"status": "ok", "summary": summary}
