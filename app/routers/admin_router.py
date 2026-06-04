"""Operador interno: /admin/*.

UI HTML server-side (Jinja2 + HTMX). Acceso restringido a roles internos.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import bind_actor, write_event
from app.core.database import get_db
from app.core.jwt_auth import CurrentUser, require_roles
from app.services.onboarding import OnboardClientPayload, OnboardingError, onboard_client

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="app/templates")

# roles permitidos por seccion
_OPS  = require_roles("super_admin", "finanzas", "lectura")
_WRITE = require_roles("super_admin", "finanzas")
_ADMIN = require_roles("super_admin")


# ---------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: CurrentUser = Depends(_OPS),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    metrics = (await db.execute(text("""
        SELECT
          (SELECT count(*) FROM clients WHERE status='active') AS clients_active,
          (SELECT count(*) FROM clients WHERE status='suspended') AS clients_suspended,
          (SELECT COALESCE(sum(total_cents),0) FROM invoices
             WHERE status IN ('stamped','paid')
               AND date_trunc('month', created_at) = date_trunc('month', now())) AS mtd_income_cents,
          (SELECT count(*) FROM invoices WHERE status='pending_stamp') AS pending_stamp
    """))).mappings().one()
    return templates.TemplateResponse(
        request, "admin/dashboard.html",
        {"user": user, "m": dict(metrics)},
    )


# ---------------------------------------------------------------------
# clientes
# ---------------------------------------------------------------------


@router.get("/clients", response_class=HTMLResponse)
async def list_clients(
    request: Request,
    user: CurrentUser = Depends(_OPS),
    db: AsyncSession = Depends(get_db),
    q: str | None = None,
    page_status: str | None = None,
) -> HTMLResponse:
    sql = """
        SELECT id, legal_name, rfc, status, billing_email, created_at
        FROM clients
        WHERE (:q IS NULL OR legal_name ILIKE '%'||:q||'%' OR rfc ILIKE '%'||:q||'%')
          AND (:st IS NULL OR status = :st)
        ORDER BY created_at DESC LIMIT 200
    """
    rows = (await db.execute(text(sql), {"q": q, "st": page_status})).mappings().all()
    return templates.TemplateResponse(
        request, "admin/clients_list.html",
        {"user": user, "clients": list(rows), "q": q, "page_status": page_status},
    )


@router.get("/clients/new", response_class=HTMLResponse)
async def new_client_form(
    request: Request,
    user: CurrentUser = Depends(_WRITE),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    plans = (await db.execute(
        text("SELECT code, name FROM plans WHERE is_active ORDER BY name")
    )).mappings().all()
    return templates.TemplateResponse(
        request, "admin/client_new.html",
        {"user": user, "plans": list(plans)},
    )


@router.post("/clients")
async def create_client(
    request: Request,
    user: CurrentUser = Depends(_WRITE),
    db: AsyncSession = Depends(get_db),
    legal_name: str = Form(...),
    trade_name: str | None = Form(None),
    rfc: str = Form(...),
    cfdi_use: str = Form("G03"),
    tax_regime: str = Form(...),
    zip_code: str = Form(...),
    billing_email: str = Form(...),
    contact_phone: str | None = Form(None),
    plan_code: str = Form(...),
    titular_full_name: str = Form(...),
    titular_email: str = Form(...),
):
    await bind_actor(db, actor_user_id=user.id,
                      actor_ip=request.client.host if request.client else None,
                      request_id=getattr(request.state, "request_id", None))
    try:
        result = await onboard_client(
            db,
            OnboardClientPayload(
                legal_name=legal_name, trade_name=trade_name, rfc=rfc,
                cfdi_use=cfdi_use, tax_regime=tax_regime, zip_code=zip_code,
                billing_email=billing_email, contact_phone=contact_phone,
                plan_code=plan_code,
                titular_full_name=titular_full_name, titular_email=titular_email,
            ),
            actor_user_id=user.id,
            actor_ip=request.client.host if request.client else None,
            request_id=getattr(request.state, "request_id", None),
        )
    except OnboardingError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e
    return RedirectResponse(f"/admin/clients/{result.client_id}",
                             status_code=status.HTTP_303_SEE_OTHER)


@router.get("/clients/{cid}", response_class=HTMLResponse)
async def client_detail(
    cid: int,
    request: Request,
    user: CurrentUser = Depends(_OPS),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    c = (await db.execute(
        text("SELECT * FROM clients WHERE id=:id"), {"id": cid}
    )).mappings().first()
    if not c:
        raise HTTPException(404, "cliente no existe")
    subs = (await db.execute(text("""
        SELECT s.id, s.status, s.started_at, p.code AS plan_code, p.name AS plan_name
        FROM subscriptions s JOIN plans p ON p.id = s.plan_id
        WHERE s.client_id = :id ORDER BY s.started_at DESC
    """), {"id": cid})).mappings().all()
    return templates.TemplateResponse(
        request, "admin/client_detail.html",
        {"user": user, "c": dict(c), "subs": list(subs)},
    )


@router.patch("/clients/{cid}")
async def edit_client(
    cid: int,
    request: Request,
    user: CurrentUser = Depends(_WRITE),
    db: AsyncSession = Depends(get_db),
    legal_name: str | None = Form(None),
    trade_name: str | None = Form(None),
    cfdi_use: str | None = Form(None),
    tax_regime: str | None = Form(None),
    zip_code: str | None = Form(None),
    billing_email: str | None = Form(None),
    contact_phone: str | None = Form(None),
):
    """Edita datos comerciales/fiscales del cliente (NO el RFC ni los ids cross-core).

    Solo se actualizan los campos enviados (COALESCE a valor actual). La UPDATE
    sobre `clients` dispara el trigger de auditoria (action='update', old/new) que
    lee el actor desde las variables de sesion fijadas por bind_actor.
    """
    await bind_actor(db, actor_user_id=user.id,
                      actor_ip=request.client.host if request.client else None,
                      request_id=getattr(request.state, "request_id", None))
    res = await db.execute(text("""
        UPDATE clients SET
          legal_name    = COALESCE(:ln, legal_name),
          trade_name    = COALESCE(:tn, trade_name),
          cfdi_use      = COALESCE(:cu, cfdi_use),
          tax_regime    = COALESCE(:tr, tax_regime),
          zip_code      = COALESCE(:zp, zip_code),
          billing_email = COALESCE(:be, billing_email),
          contact_phone = COALESCE(:cp, contact_phone),
          updated_at    = now()
        WHERE id = :id
        RETURNING id
    """), {
        "ln": legal_name, "tn": trade_name, "cu": cfdi_use, "tr": tax_regime,
        "zp": zip_code, "be": billing_email, "cp": contact_phone, "id": cid,
    })
    if res.first() is None:
        raise HTTPException(404, "cliente no existe")
    return RedirectResponse(f"/admin/clients/{cid}", status_code=303)


@router.post("/clients/{cid}/suspend")
async def suspend_client(
    cid: int,
    request: Request,
    user: CurrentUser = Depends(_ADMIN),
    db: AsyncSession = Depends(get_db),
    reason: str = Form(...),
):
    await bind_actor(db, actor_user_id=user.id,
                      actor_ip=request.client.host if request.client else None,
                      request_id=getattr(request.state, "request_id", None))
    res = await db.execute(text("""
        UPDATE clients SET status='suspended', suspended_at=now(), suspended_reason=:r,
          updated_at=now()
        WHERE id=:id AND status='active'
        RETURNING id
    """), {"id": cid, "r": reason})
    if res.first() is None:
        raise HTTPException(409, "cliente inexistente o no esta activo")
    # evento explicito de ciclo de vida (ademas del 'update' automatico del trigger)
    await write_event(
        db, actor_user_id=user.id,
        actor_ip=request.client.host if request.client else None,
        entity_type="clients", entity_id=cid, action="suspend",
        new_values={"status": "suspended", "reason": reason},
        request_id=getattr(request.state, "request_id", None),
    )
    return RedirectResponse(f"/admin/clients/{cid}", status_code=303)


@router.post("/clients/{cid}/reactivate")
async def reactivate_client(
    cid: int,
    request: Request,
    user: CurrentUser = Depends(_ADMIN),
    db: AsyncSession = Depends(get_db),
):
    """Reactiva un cliente suspendido -> status='active', limpia suspended_*."""
    await bind_actor(db, actor_user_id=user.id,
                      actor_ip=request.client.host if request.client else None,
                      request_id=getattr(request.state, "request_id", None))
    res = await db.execute(text("""
        UPDATE clients SET status='active', suspended_at=NULL, suspended_reason=NULL,
          updated_at=now()
        WHERE id=:id AND status='suspended'
        RETURNING id
    """), {"id": cid})
    if res.first() is None:
        raise HTTPException(409, "cliente inexistente o no esta suspendido")
    await write_event(
        db, actor_user_id=user.id,
        actor_ip=request.client.host if request.client else None,
        entity_type="clients", entity_id=cid, action="reactivate",
        new_values={"status": "active"},
        request_id=getattr(request.state, "request_id", None),
    )
    return RedirectResponse(f"/admin/clients/{cid}", status_code=303)


@router.post("/clients/{cid}/cancel")
async def cancel_client(
    cid: int,
    request: Request,
    user: CurrentUser = Depends(_WRITE),
    db: AsyncSession = Depends(get_db),
    reason: str = Form(...),
):
    """Baja del cliente -> status='cancelled' (estado terminal). Auditado."""
    await bind_actor(db, actor_user_id=user.id,
                      actor_ip=request.client.host if request.client else None,
                      request_id=getattr(request.state, "request_id", None))
    res = await db.execute(text("""
        UPDATE clients SET status='cancelled', suspended_reason=:r, updated_at=now()
        WHERE id=:id AND status <> 'cancelled'
        RETURNING id
    """), {"id": cid, "r": reason})
    if res.first() is None:
        raise HTTPException(409, "cliente inexistente o ya cancelado")
    await write_event(
        db, actor_user_id=user.id,
        actor_ip=request.client.host if request.client else None,
        entity_type="clients", entity_id=cid, action="cancel",
        new_values={"status": "cancelled", "reason": reason},
        request_id=getattr(request.state, "request_id", None),
    )
    return RedirectResponse(f"/admin/clients/{cid}", status_code=303)


# ---------------------------------------------------------------------
# catalogos (lectura - CRUD completo en sprint posterior)
# ---------------------------------------------------------------------


@router.get("/catalog/products", response_class=HTMLResponse)
async def list_products(
    request: Request,
    user: CurrentUser = Depends(_OPS),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    rows = (await db.execute(text(
        "SELECT * FROM products ORDER BY name"
    ))).mappings().all()
    return templates.TemplateResponse(
        request, "admin/products.html", {"user": user, "rows": list(rows)},
    )


@router.get("/catalog/services", response_class=HTMLResponse)
async def list_services(
    request: Request,
    user: CurrentUser = Depends(_OPS),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    rows = (await db.execute(text(
        "SELECT * FROM services ORDER BY name"
    ))).mappings().all()
    return templates.TemplateResponse(
        request, "admin/services.html", {"user": user, "rows": list(rows)},
    )


@router.get("/catalog/plans", response_class=HTMLResponse)
async def list_plans(
    request: Request,
    user: CurrentUser = Depends(_OPS),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    rows = (await db.execute(text(
        "SELECT * FROM plans ORDER BY name"
    ))).mappings().all()
    return templates.TemplateResponse(
        request, "admin/plans.html", {"user": user, "rows": list(rows)},
    )


@router.get("/catalog/promotions", response_class=HTMLResponse)
async def list_promotions(
    request: Request,
    user: CurrentUser = Depends(_OPS),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    rows = (await db.execute(text(
        "SELECT * FROM promotions ORDER BY valid_from DESC"
    ))).mappings().all()
    return templates.TemplateResponse(
        request, "admin/promotions.html", {"user": user, "rows": list(rows)},
    )


# ---------------------------------------------------------------------
# billing
# ---------------------------------------------------------------------


@router.get("/billing/invoices", response_class=HTMLResponse)
async def list_invoices(
    request: Request,
    user: CurrentUser = Depends(_OPS),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    rows = (await db.execute(text("""
        SELECT i.id, i.client_id, c.legal_name, i.period_start, i.period_end,
               i.total_cents, i.status, i.uuid_cfdi, i.created_at
        FROM invoices i JOIN clients c ON c.id = i.client_id
        ORDER BY i.created_at DESC LIMIT 200
    """))).mappings().all()
    return templates.TemplateResponse(
        request, "admin/invoices.html", {"user": user, "rows": list(rows)},
    )


@router.post("/billing/run-closing")
async def trigger_closing(
    request: Request,
    user: CurrentUser = Depends(_ADMIN),
    db: AsyncSession = Depends(get_db),
):
    await bind_actor(db, actor_user_id=user.id,
                      actor_ip=request.client.host if request.client else None,
                      request_id=getattr(request.state, "request_id", None))
    from app.services.billing import run_monthly_closing
    summary = await run_monthly_closing(db)
    return JSONResponse({"status": "ok", "summary": summary})


# ---------------------------------------------------------------------
# audit log
# ---------------------------------------------------------------------


@router.get("/audit-log", response_class=HTMLResponse)
async def audit_log_view(
    request: Request,
    user: CurrentUser = Depends(_OPS),
    db: AsyncSession = Depends(get_db),
    entity: str | None = None,
    actor: int | None = None,
) -> HTMLResponse:
    rows = (await db.execute(text("""
        SELECT a.id, a.occurred_at, u.email AS actor_email, a.actor_ip,
               a.entity_type, a.entity_id, a.action, a.request_id
        FROM audit_log a LEFT JOIN users u ON u.id = a.actor_user_id
        WHERE (:e IS NULL OR a.entity_type = :e)
          AND (:u IS NULL OR a.actor_user_id = :u)
        ORDER BY a.occurred_at DESC LIMIT 500
    """), {"e": entity, "u": actor})).mappings().all()
    return templates.TemplateResponse(
        request, "admin/audit_log.html",
        {"user": user, "rows": list(rows), "entity": entity, "actor": actor},
    )
