"""Portal cliente externo: /portal/* en app.inovaweb.com.mx."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clients.finanzas_client import FinanzasClient
from app.core.clients.hub_client import HubClient
from app.core.clients.medidor_client import MedidorClient
from app.core.config import get_settings
from app.core.database import get_db
from app.core.jwt_auth import CurrentUser, require_roles

router = APIRouter(prefix="/portal", tags=["portal"])
templates = Jinja2Templates(directory="app/templates")

_CLIENT = require_roles("cliente_titular", "cliente_usuario")


async def _client_or_403(user: CurrentUser, db: AsyncSession) -> dict:
    if user.client_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "usuario sin cliente asignado")
    row = (await db.execute(
        text("SELECT * FROM clients WHERE id=:id"), {"id": user.client_id}
    )).mappings().first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "cliente no existe")
    if row["status"] != "active":
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, "cuenta suspendida")
    return dict(row)


# ---------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: CurrentUser = Depends(_CLIENT),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    c = await _client_or_403(user, db)

    # saldo desde finanzas-core
    balance = {"balance_cents": 0}
    if c.get("finanzas_account_id"):
        fin = FinanzasClient()
        try:
            balance = await fin.get_balance(c["finanzas_account_id"])
        finally:
            await fin.close()

    # consumo del mes (medidor)
    usage: dict = {}
    if c.get("medidor_account_id"):
        med = MedidorClient()
        try:
            today = date.today()
            usage = await med.get_usage(
                c["medidor_account_id"],
                today.replace(day=1).isoformat(),
                today.isoformat(),
            )
        finally:
            await med.close()

    return templates.TemplateResponse(
        request, "portal/dashboard.html",
        {"user": user, "client": c, "balance": balance, "usage": usage},
    )


@router.get("/usage", response_class=HTMLResponse)
async def usage_detail(
    request: Request,
    user: CurrentUser = Depends(_CLIENT),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    c = await _client_or_403(user, db)
    usage: dict = {}
    if c.get("medidor_account_id"):
        med = MedidorClient()
        try:
            today = date.today()
            usage = await med.get_usage(
                c["medidor_account_id"],
                today.replace(day=1).isoformat(),
                today.isoformat(),
            )
        finally:
            await med.close()
    return templates.TemplateResponse(
        request, "portal/usage.html",
        {"user": user, "client": c, "usage": usage},
    )


# ---------------------------------------------------------------------
# facturas
# ---------------------------------------------------------------------


@router.get("/invoices", response_class=HTMLResponse)
async def my_invoices(
    request: Request,
    user: CurrentUser = Depends(_CLIENT),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    rows = (await db.execute(text("""
        SELECT id, period_start, period_end, total_cents, status, uuid_cfdi, created_at
        FROM invoices WHERE client_id = :cid
        ORDER BY created_at DESC LIMIT 100
    """), {"cid": user.client_id})).mappings().all()
    return templates.TemplateResponse(
        request, "portal/invoices.html",
        {"user": user, "rows": list(rows)},
    )


@router.get("/invoices/{iid}.{ext}")
async def download_invoice(
    iid: int, ext: str,
    user: CurrentUser = Depends(_CLIENT),
    db: AsyncSession = Depends(get_db),
):
    if ext not in {"pdf", "xml"}:
        raise HTTPException(404, "formato no soportado")
    row = (await db.execute(text("""
        SELECT pdf_path, xml_path, status FROM invoices
        WHERE id = :id AND client_id = :cid
    """), {"id": iid, "cid": user.client_id})).mappings().first()
    if not row:
        raise HTTPException(404, "factura no existe")
    if row["status"] not in {"stamped", "paid"}:
        raise HTTPException(409, "factura aun no timbrada")
    path = row["pdf_path"] if ext == "pdf" else row["xml_path"]
    if not path:
        raise HTTPException(404, "archivo no disponible")
    with open(path, "rb") as fh:
        data = fh.read()
    media = "application/pdf" if ext == "pdf" else "application/xml"
    return Response(data, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="factura_{iid}.{ext}"'})


# ---------------------------------------------------------------------
# recarga (inicia flujo hub-pasarelas)
# ---------------------------------------------------------------------


@router.post("/recharge")
async def start_recharge(
    request: Request,
    user: CurrentUser = Depends(_CLIENT),
    db: AsyncSession = Depends(get_db),
    amount_cents: int = 0,
):
    if amount_cents < 5000:  # min $50 MXN
        raise HTTPException(400, "monto minimo de recarga $50 MXN")
    c = await _client_or_403(user, db)
    if not c.get("hub_account_id"):
        raise HTTPException(409, "cuenta no provisionada en hub")
    hub = HubClient()
    try:
        intent = await hub.create_payment_intent(
            c["hub_account_id"],
            amount_cents=amount_cents,
            concept=f"Recarga saldo cliente {c['id']}",
            return_url=f"https://{get_settings().PORTAL_DOMAIN}/portal/dashboard",
        )
    finally:
        await hub.close()
    return RedirectResponse(intent["checkout_url"], status_code=303)


# ---------------------------------------------------------------------
# datos comerciales
# ---------------------------------------------------------------------


@router.get("/account", response_class=HTMLResponse)
async def my_account(
    request: Request,
    user: CurrentUser = Depends(_CLIENT),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    c = await _client_or_403(user, db)
    return templates.TemplateResponse(
        request, "portal/account.html", {"user": user, "client": c},
    )
