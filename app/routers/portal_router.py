"""Portal cliente externo: /portal/* en app.inovaweb.com.mx."""

from __future__ import annotations

import logging
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clients.finanzas_client import FinanzasClient
from app.core.clients.medidor_client import MedidorClient
from app.core.config import get_settings
from app.core.database import get_db
from app.core.jwt_auth import CurrentUser, require_roles
from app.services.prepago import PrepagoError, initiate_charge

log = logging.getLogger("portal")
router = APIRouter(prefix="/portal", tags=["portal"])
templates = Jinja2Templates(directory="app/templates")

# FIX-5 (TASK-15b): mensaje generico al cliente; el detalle va al log server-side.
_PAY_ERR = "error al procesar el pago, intenta de nuevo"

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

    today = date.today()

    # saldo desde finanzas-core (tenant via API key; filtro por external_user_id)
    balance = {"balance_cents": 0}
    if c.get("finanzas_account_id"):
        fin = FinanzasClient()
        try:
            balance = await fin.get_balance(
                as_of=f"{today.isoformat()}T23:59:59Z",
                external_user_id=c["finanzas_account_id"],
            )
        finally:
            await fin.close()

    # consumo del mes (medidor)
    usage: dict = {}
    if c.get("medidor_account_id"):
        med = MedidorClient()
        try:
            usage = await med.get_usage(
                c["medidor_account_id"],
                from_ts=today.replace(day=1).isoformat(),
                to_ts=today.isoformat(),
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
                from_ts=today.replace(day=1).isoformat(),
                to_ts=today.isoformat(),
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


def _checkout_url(intent: dict) -> str:
    url = intent.get("checkout_url") or intent.get("url")
    if not url:
        raise HTTPException(502, "el hub no devolvio url de checkout")
    return url


@router.post("/recharge")
async def start_recharge(
    request: Request,
    user: CurrentUser = Depends(_CLIENT),
    db: AsyncSession = Depends(get_db),
    amount_cents: int = 0,
):
    # FIX-6 (TASK-15b): piso $50 MXN y techo configurable (MAX_RECARGA_CENTS).
    max_recarga = get_settings().MAX_RECARGA_CENTS
    if amount_cents < 5000:  # min $50 MXN
        raise HTTPException(400, "monto minimo de recarga $50 MXN")
    if amount_cents > max_recarga:
        raise HTTPException(
            400, f"monto maximo de recarga ${max_recarga / 100:,.2f} MXN"
        )
    c = await _client_or_403(user, db)
    if not c.get("hub_account_id"):
        raise HTTPException(409, "cuenta no provisionada en hub")
    # modelo prepago: el cobro acredita saldo en la wallet del Medidor.
    # initiate_charge persiste el intento en audit_log (recharge.initiated)
    # y el webhook hub-payment-paid acredita al confirmarse el pago.
    try:
        intent = await initiate_charge(
            db, client=c, amount_cents=amount_cents,
            purpose="wallet_recharge",
            description=f"Recarga saldo cliente {c['id']}",
            actor_user_id=user.id,
            actor_ip=request.client.host if request.client else None,
            request_id=getattr(request.state, "request_id", None),
        )
    except PrepagoError as e:
        # FIX-5: log detalle server-side, mensaje generico al cliente.
        log.error("recharge_failed", extra={"caf_client_id": c["id"],
                                             "error": str(e)})
        raise HTTPException(502, _PAY_ERR) from e
    return RedirectResponse(_checkout_url(intent), status_code=303)


@router.post("/plans/{plan_code}/purchase")
async def purchase_plan(
    plan_code: str,
    request: Request,
    user: CurrentUser = Depends(_CLIENT),
    db: AsyncSession = Depends(get_db),
):
    """Contratar un plan (prepago): cobra el credito del plan en el Hub.

    El precio del plan = credito que se acreditara a la wallet del Medidor al
    confirmarse el pago (modelo prepago). purpose='plan_purchase'.
    """
    c = await _client_or_403(user, db)
    if not c.get("hub_account_id"):
        raise HTTPException(409, "cuenta no provisionada en hub")
    plan = (await db.execute(text(
        "SELECT code, name, monthly_fee_cents FROM plans "
        "WHERE code = :code AND is_active = true"
    ), {"code": plan_code})).mappings().first()
    if not plan:
        raise HTTPException(404, "plan no existe o inactivo")
    amount_cents = int(plan["monthly_fee_cents"])
    if amount_cents <= 0:
        raise HTTPException(409, "plan sin precio publico (contacto directo)")
    try:
        intent = await initiate_charge(
            db, client=c, amount_cents=amount_cents,
            purpose="plan_purchase", plan_code=plan["code"],
            description=f"Contratacion plan {plan['name']} cliente {c['id']}",
            actor_user_id=user.id,
            actor_ip=request.client.host if request.client else None,
            request_id=getattr(request.state, "request_id", None),
        )
    except PrepagoError as e:
        # FIX-5: log detalle server-side, mensaje generico al cliente.
        log.error("plan_purchase_failed", extra={"caf_client_id": c["id"],
                                                  "plan_code": plan_code,
                                                  "error": str(e)})
        raise HTTPException(502, _PAY_ERR) from e
    return RedirectResponse(_checkout_url(intent), status_code=303)


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
# TASK-15: recarga y compra de plan via services.prepago.initiate_charge.
