"""Operador interno: /admin/*.

UI HTML server-side (Jinja2 + HTMX). Acceso restringido a roles internos.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import bind_actor, write_event
from app.core.database import get_db
from app.core.jwt_auth import CurrentUser, require_roles
from app.core.clients.medidor_client import MedidorClient
from app.core.clients.finanzas_client import FinanzasClient
from app.services.onboarding import OnboardClientPayload, OnboardingError, onboard_client

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="app/templates")

# roles permitidos por seccion
_OPS  = require_roles("super_admin", "finanzas", "lectura")
_WRITE = require_roles("super_admin", "finanzas")
_ADMIN = require_roles("super_admin")


def _org_scope(user: CurrentUser, column: str = "organization_id") -> tuple[str, dict]:
    """Cláusula de aislamiento por organización para los listados del panel.

    El operador de la plataforma (super_admin de Inovaweb, org 1) ve TODAS las
    organizaciones; cualquier otro usuario solo ve la suya. Devuelve el fragmento
    SQL a concatenar dentro del WHERE y los params. `column` permite calificar
    con alias (p.ej. 'c.organization_id') en queries con JOIN.
    """
    if user.is_platform:
        return "", {}
    return f" AND {column} = :_org", {"_org": user.organization_id}


# ---------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: CurrentUser = Depends(_OPS),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Tablero directivo: tarjetas de metricas + actividad reciente + consumo por core.

    Entrega 4 metricas (ingreso del mes, clientes activos, facturas emitidas en el
    mes, mora/suspendidos), las ultimas 10 entradas del audit_log y barras de
    consumo por core (datos placeholder hasta cablear el agregado real del Medidor).
    """
    oc, op = _org_scope(user)
    metrics = (await db.execute(text(f"""
        SELECT
          (SELECT count(*) FROM clients WHERE status='active'{oc}) AS clients_active,
          (SELECT count(*) FROM clients WHERE status='suspended'{oc}) AS clients_suspended,
          (SELECT COALESCE(sum(total_cents),0) FROM invoices
             WHERE status IN ('stamped','paid')
               AND date_trunc('month', created_at) = date_trunc('month', now()){oc}) AS mtd_income_cents,
          (SELECT count(*) FROM invoices
             WHERE date_trunc('month', created_at) = date_trunc('month', now()){oc}) AS invoices_mtd,
          (SELECT count(*) FROM invoices WHERE status='pending_stamp'{oc}) AS pending_stamp
    """), op)).mappings().one()

    # actividad reciente: ultimas 10 escrituras auditadas (aisladas por org via actor)
    rc = "" if user.is_platform else " WHERE u.organization_id = :_org"
    recent = (await db.execute(text(f"""
        SELECT a.occurred_at, u.email AS actor_email, a.entity_type,
               a.entity_id, a.action
        FROM audit_log a LEFT JOIN users u ON u.id = a.actor_user_id{rc}
        ORDER BY a.occurred_at DESC LIMIT 10
    """), op)).mappings().all()

    # barras de consumo por core (placeholder; el agregado real vive en el Medidor)
    core_usage = [
        {"core": "Medidor", "pct": 0},
        {"core": "Hub", "pct": 0},
        {"core": "Finanzas", "pct": 0},
        {"core": "Mensajes", "pct": 0},
    ]

    return templates.TemplateResponse(
        request, "admin/dashboard.html",
        {"user": user, "m": dict(metrics),
         "recent": list(recent), "core_usage": core_usage},
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
    oc, op = _org_scope(user)
    sql = f"""
        SELECT id, legal_name, rfc, status, billing_email, created_at
        FROM clients
        WHERE (:q IS NULL OR legal_name ILIKE '%'||:q||'%' OR rfc ILIKE '%'||:q||'%')
          AND (:st IS NULL OR status = :st){oc}
        ORDER BY created_at DESC LIMIT 200
    """
    rows = (await db.execute(text(sql), {"q": q, "st": page_status, **op})).mappings().all()
    ctx = {"user": user, "clients": list(rows), "q": q, "page_status": page_status}
    # HTMX: devolver solo el fragmento de la tabla (los filtros usan hx-get).
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request, "admin/_clients_table.html", ctx,
        )
    return templates.TemplateResponse(
        request, "admin/clients_list.html", ctx,
    )


@router.get("/clients/new", response_class=HTMLResponse)
async def new_client_form(
    request: Request,
    user: CurrentUser = Depends(_WRITE),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    oc, op = _org_scope(user)
    plans = (await db.execute(
        text(f"SELECT code, name FROM plans WHERE is_active{oc} ORDER BY name"), op
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
                organization_id=user.organization_id,
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
    oc, op = _org_scope(user)
    c = (await db.execute(
        text(f"SELECT * FROM clients WHERE id=:id{oc}"), {"id": cid, **op}
    )).mappings().first()
    if not c:
        raise HTTPException(404, "cliente no existe")
    subs = (await db.execute(text("""
        SELECT s.id, s.status, s.started_at, p.code AS plan_code, p.name AS plan_name
        FROM subscriptions s JOIN plans p ON p.id = s.plan_id
        WHERE s.client_id = :id ORDER BY s.started_at DESC
    """), {"id": cid})).mappings().all()
    # Saldo real (Medidor) + consumos reales (Finanzas: IA + mensajería).
    saldo_cents = None
    consumos: list[dict] = []
    if c.get("medidor_account_id"):
        med = MedidorClient()
        try:
            bal = await med.get_balance(str(c["medidor_account_id"]))
            saldo_cents = bal.get("balance_cents")
        except Exception:  # noqa: BLE001
            saldo_cents = None
        finally:
            await med.close()
    fin = FinanzasClient()
    try:
        resp = await fin.list_entries(direction="debit", limit=200)
        for e in (resp.get("items") or []):
            if e.get("source_slug") not in ("medidor", "messages"):
                continue
            if str((e.get("meta") or {}).get("caf_client_id")) != str(cid):
                continue
            consumos.append(e)
    except Exception:  # noqa: BLE001
        consumos = []
    finally:
        await fin.close()
    total_consumo = sum(int(e.get("amount_cents") or 0) for e in consumos)
    return templates.TemplateResponse(
        request, "admin/client_detail.html",
        {"user": user, "c": dict(c), "subs": list(subs),
         "saldo_cents": saldo_cents, "consumos": consumos,
         "total_consumo": total_consumo},
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
    oc, op = _org_scope(user)
    rows = (await db.execute(text(
        f"SELECT * FROM products WHERE TRUE{oc} ORDER BY name"
    ), op)).mappings().all()
    return templates.TemplateResponse(
        request, "admin/products.html", {"user": user, "rows": list(rows)},
    )


@router.get("/catalog/services", response_class=HTMLResponse)
async def list_services(
    request: Request,
    user: CurrentUser = Depends(_OPS),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    oc, op = _org_scope(user)
    rows = (await db.execute(text(
        f"SELECT * FROM services WHERE TRUE{oc} ORDER BY name"
    ), op)).mappings().all()
    return templates.TemplateResponse(
        request, "admin/services.html", {"user": user, "rows": list(rows)},
    )


@router.get("/catalog/plans", response_class=HTMLResponse)
async def list_plans(
    request: Request,
    user: CurrentUser = Depends(_OPS),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    oc, op = _org_scope(user)
    rows = (await db.execute(text(
        f"SELECT * FROM plans WHERE TRUE{oc} ORDER BY name"
    ), op)).mappings().all()
    return templates.TemplateResponse(
        request, "admin/plans.html", {"user": user, "rows": list(rows)},
    )


@router.get("/catalog/promotions", response_class=HTMLResponse)
async def list_promotions(
    request: Request,
    user: CurrentUser = Depends(_OPS),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    oc, op = _org_scope(user)
    rows = (await db.execute(text(
        f"SELECT * FROM promotions WHERE TRUE{oc} ORDER BY valid_from DESC"
    ), op)).mappings().all()
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
    oc, op = _org_scope(user, "i.organization_id")
    rows = (await db.execute(text(f"""
        SELECT i.id, i.client_id, c.legal_name, i.period_start, i.period_end,
               i.total_cents, i.status, i.uuid_cfdi, i.created_at
        FROM invoices i JOIN clients c ON c.id = i.client_id
        WHERE TRUE{oc}
        ORDER BY i.created_at DESC LIMIT 200
    """), op)).mappings().all()
    return templates.TemplateResponse(
        request, "admin/invoices.html", {"user": user, "rows": list(rows)},
    )


@router.get("/billing/invoices/{iid}.{ext}")
async def admin_download_invoice(
    iid: int, ext: str,
    user: CurrentUser = Depends(_OPS),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Descarga el PDF/XML de una factura (operador interno).

    A diferencia del portal, el operador puede descargar la factura de cualquier
    cliente (sin filtro por client_id), pero solo si ya esta timbrada (estado
    'stamped' o 'paid'). El acceso esta limitado a roles internos via `_OPS`.
    """
    if ext not in {"pdf", "xml"}:
        raise HTTPException(404, "formato no soportado")
    row = (await db.execute(text("""
        SELECT pdf_path, xml_path, status FROM invoices WHERE id = :id
    """), {"id": iid})).mappings().first()
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
    return Response(
        data, media_type=media,
        headers={"Content-Disposition": f'attachment; filename="factura_{iid}.{ext}"'},
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
    oc, op = _org_scope(user, "u.organization_id")
    rows = (await db.execute(text(f"""
        SELECT a.id, a.occurred_at, u.email AS actor_email, a.actor_ip,
               a.entity_type, a.entity_id, a.action, a.request_id
        FROM audit_log a LEFT JOIN users u ON u.id = a.actor_user_id
        WHERE (:e IS NULL OR a.entity_type = :e)
          AND (:u IS NULL OR a.actor_user_id = :u){oc}
        ORDER BY a.occurred_at DESC LIMIT 500
    """), {"e": entity, "u": actor, **op})).mappings().all()
    return templates.TemplateResponse(
        request, "admin/audit_log.html",
        {"user": user, "rows": list(rows), "entity": entity, "actor": actor},
    )
