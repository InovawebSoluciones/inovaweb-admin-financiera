"""Operador interno: /admin/*.

UI HTML server-side (Jinja2 + HTMX). Acceso restringido a roles internos.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
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
          (SELECT COALESCE(sum(amount_cents),0) FROM prepaid_ledger
             WHERE kind='debit'
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

    # consumo por core del mes: agregado real del libro prepago, atribuido por el
    # source_core del servicio cobrado. % relativo al total consumido en el mes.
    oc2, op2 = _org_scope(user, "l.organization_id")
    core_rows = (await db.execute(text(f"""
        SELECT s.source_core AS core, COALESCE(sum(l.amount_cents),0) AS cents
        FROM prepaid_ledger l
        JOIN services s ON s.code = l.service_code AND s.organization_id = l.organization_id
        WHERE l.kind='debit'
          AND date_trunc('month', l.created_at) = date_trunc('month', now()){oc2}
        GROUP BY s.source_core
    """), op2)).mappings().all()
    by_core = {r["core"]: int(r["cents"]) for r in core_rows}
    total_core = sum(by_core.values()) or 1
    core_usage = [
        {"core": label, "pct": round(by_core.get(key, 0) / total_core * 100),
         "cents": by_core.get(key, 0)}
        for key, label in (("medidor", "Medidor"), ("hub", "Hub"),
                           ("finanzas", "Finanzas"), ("messages", "Mensajes"))
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
        WHERE (CAST(:q AS text) IS NULL OR legal_name ILIKE '%'||:q||'%' OR rfc ILIKE '%'||:q||'%')
          AND (CAST(:st AS text) IS NULL OR status = :st){oc}
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
    saved: str = Query(""),
    user: CurrentUser = Depends(_OPS),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    oc, op = _org_scope(user)
    rows = (await db.execute(text(
        f"SELECT * FROM services WHERE TRUE{oc} ORDER BY name"
    ), op)).mappings().all()
    return templates.TemplateResponse(
        request, "admin/services.html", {"user": user, "rows": list(rows), "saved": saved},
    )


@router.post("/catalog/services", response_class=HTMLResponse)
async def create_service(
    request: Request,
    user: CurrentUser = Depends(_OPS),
    db: AsyncSession = Depends(get_db),
    code: str = Form(...),
    name: str = Form(...),
    source_core: str = Form(...),
    unit: str = Form("unidad"),
    unit_price_mxn: str = Form(...),
    app_slug: str = Form(""),
) -> HTMLResponse:
    """Alta de un servicio cobrable para la organización del operador."""
    valid_cores = {"medidor", "hub", "messages", "finanzas", "internal"}
    if source_core not in valid_cores:
        return RedirectResponse("/admin/catalog/services?saved=error_core", status_code=303)
    try:
        price_cents = round(float(unit_price_mxn) * 100)
        if price_cents < 0:
            raise ValueError
    except (ValueError, TypeError):
        return RedirectResponse("/admin/catalog/services?saved=error_precio", status_code=303)
    try:
        await db.execute(text("""
            INSERT INTO services (code, name, source_core, unit, unit_price_cents, app_slug, organization_id)
            VALUES (:code, :name, :core, :unit, :price, :app, :org)
        """), {
            "code": code.strip().lower(),
            "name": name.strip(),
            "core": source_core,
            "unit": unit.strip() or "unidad",
            "price": price_cents,
            "app": app_slug.strip() or None,
            "org": user.organization_id,
        })
        await db.commit()
    except Exception:
        await db.rollback()
        return RedirectResponse("/admin/catalog/services?saved=error_dup", status_code=303)
    return RedirectResponse("/admin/catalog/services?saved=ok", status_code=303)


@router.get("/catalog/plans", response_class=HTMLResponse)
async def list_plans(
    request: Request,
    saved: str = Query(""),
    user: CurrentUser = Depends(_OPS),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    oc, op = _org_scope(user, "p.organization_id")
    rows = (await db.execute(text(f"""
        SELECT p.*,
               COALESCE(string_agg(DISTINCT s.code, ', '), '—') AS servicios
        FROM plans p
        LEFT JOIN plan_items pi ON pi.plan_id = p.id
        LEFT JOIN services s ON s.id = pi.service_id
        WHERE TRUE{oc}
        GROUP BY p.id
        ORDER BY p.name
    """), op)).mappings().all()
    return templates.TemplateResponse(
        request, "admin/plans.html", {"user": user, "rows": list(rows), "saved": saved},
    )


@router.post("/catalog/plans", response_class=HTMLResponse)
async def create_plan(
    request: Request,
    user: CurrentUser = Depends(_OPS),
    db: AsyncSession = Depends(get_db),
    code: str = Form(...),
    name: str = Form(...),
    description: str = Form(""),
    monthly_fee_mxn: str = Form("0"),
    monthly_credit_cents: str = Form("0"),
    is_free: str = Form(""),
    app_slug: str = Form(""),
) -> HTMLResponse:
    """Alta de un plan de precios para la organización del operador."""
    try:
        fee_cents = round(float(monthly_fee_mxn) * 100)
        credits = int(monthly_credit_cents)
        if fee_cents < 0 or credits < 0:
            raise ValueError
    except (ValueError, TypeError):
        return RedirectResponse("/admin/catalog/plans?saved=error_precio", status_code=303)
    try:
        await db.execute(text("""
            INSERT INTO plans (code, name, description, monthly_fee_cents,
                               monthly_credit_cents, is_free, app_slug, organization_id)
            VALUES (:code, :name, :desc, :fee, :credits, :free, :app, :org)
        """), {
            "code": code.strip().lower(),
            "name": name.strip(),
            "desc": description.strip() or None,
            "fee": fee_cents,
            "credits": credits if credits > 0 else None,
            "free": (is_free == "on"),
            "app": app_slug.strip() or None,
            "org": user.organization_id,
        })
        await db.commit()
    except Exception:
        await db.rollback()
        return RedirectResponse("/admin/catalog/plans?saved=error_dup", status_code=303)
    return RedirectResponse("/admin/catalog/plans?saved=ok", status_code=303)


@router.get("/catalog/promotions", response_class=HTMLResponse)
async def list_promotions(
    request: Request,
    user: CurrentUser = Depends(_OPS),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    oc, op = _org_scope(user, "p.organization_id")
    rows = (await db.execute(text(f"""
        SELECT p.*, d.name AS distributor_name
        FROM promotions p
        LEFT JOIN distributors d ON d.id = p.distributor_id
        WHERE TRUE{oc} ORDER BY p.created_at DESC
    """), op)).mappings().all()
    oc2, op2 = _org_scope(user)
    distribuidores = (await db.execute(text(
        f"SELECT id, name, is_active FROM distributors WHERE TRUE{oc2} ORDER BY name"
    ), op2)).mappings().all()
    return templates.TemplateResponse(
        request, "admin/promotions.html",
        {"user": user, "rows": list(rows), "distribuidores": list(distribuidores),
         "saved": request.query_params.get("saved")},
    )


@router.post("/catalog/distributors")
async def create_distributor(
    request: Request,
    user: CurrentUser = Depends(_WRITE),
    db: AsyncSession = Depends(get_db),
    name: str = Form(...),
):
    """Da de alta un distribuidor (por ahora solo el nombre)."""
    nm = name.strip()
    if not nm:
        return RedirectResponse("/admin/catalog/promotions?saved=error_nombre", status_code=303)
    await bind_actor(db, actor_user_id=user.id,
                     actor_ip=request.client.host if request.client else None,
                     request_id=getattr(request.state, "request_id", None))
    await db.execute(
        text("INSERT INTO distributors (name, organization_id) VALUES (:n, :org)"),
        {"n": nm, "org": user.organization_id},
    )
    return RedirectResponse("/admin/catalog/promotions?saved=dist_ok", status_code=303)


@router.post("/catalog/promotions/new")
async def create_promo_code(
    request: Request,
    user: CurrentUser = Depends(_WRITE),
    db: AsyncSession = Depends(get_db),
    code: str = Form(...),
    discount_pct: float = Form(...),
    distributor_id: int = Form(...),
):
    """Crea un código de promoción ligado a un distribuidor, con descuento en %.

    El código se aplica al contratar (self-service). kind='referral'; vigencia
    abierta (10 años); el % debe estar entre 0 y 100.
    """
    cd = code.strip().upper()
    if not cd:
        return RedirectResponse("/admin/catalog/promotions?saved=error_code", status_code=303)
    if not (0 < discount_pct <= 100):
        return RedirectResponse("/admin/catalog/promotions?saved=error_pct", status_code=303)
    # el distribuidor debe ser de la org del usuario
    d = (await db.execute(
        text("SELECT 1 FROM distributors WHERE id=:d AND organization_id=:org"),
        {"d": distributor_id, "org": user.organization_id},
    )).first()
    if not d:
        return RedirectResponse("/admin/catalog/promotions?saved=error_dist", status_code=303)
    await bind_actor(db, actor_user_id=user.id,
                     actor_ip=request.client.host if request.client else None,
                     request_id=getattr(request.state, "request_id", None))
    try:
        await db.execute(text("""
            INSERT INTO promotions
              (code, name, kind, discount_pct, valid_from, valid_to,
               distributor_id, organization_id, is_active)
            VALUES (:code, :name, 'referral', :pct, now(), now() + interval '10 years',
                    :did, :org, true)
        """), {"code": cd, "name": f"Código {cd}", "pct": discount_pct,
               "did": distributor_id, "org": user.organization_id})
    except Exception:  # noqa: BLE001 - code duplicado u otra violación
        return RedirectResponse("/admin/catalog/promotions?saved=error_code_dup", status_code=303)
    return RedirectResponse("/admin/catalog/promotions?saved=promo_ok", status_code=303)


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
        WHERE (CAST(:e AS text) IS NULL OR a.entity_type = :e)
          AND (CAST(:u AS integer) IS NULL OR a.actor_user_id = :u){oc}
        ORDER BY a.occurred_at DESC LIMIT 500
    """), {"e": entity, "u": actor, **op})).mappings().all()
    return templates.TemplateResponse(
        request, "admin/audit_log.html",
        {"user": user, "rows": list(rows), "entity": entity, "actor": actor},
    )


# ---------------------------------------------------------------------
# pasarelas de pago (Hub) — guardar credenciales cifradas SIN tocar SQL.
# El CAF (rector) administra las pasarelas del tenant del Hub; el Hub cifra
# y guarda (POST /admin/hub/v1/gateway-config). Solo super_admin.
# ---------------------------------------------------------------------

# campos de credencial por pasarela (lo que espera el constructor del gateway
# en el Hub). El CAF nunca guarda estos valores: los reenvía al Hub para cifrar.
_GATEWAY_FIELDS = {
    "stripe":  ["secret_key", "webhook_secret"],
    "conekta": ["private_key", "public_key", "webhook_secret"],
}
# campos OBLIGATORIOS para guardar (el resto, como webhook_secret, son opcionales
# y se pueden añadir después). Permite guardar la secret_key y probar el cobro
# antes de crear el webhook.
_GATEWAY_REQUIRED = {
    "stripe":  ["secret_key"],
    "conekta": ["private_key", "public_key"],
}


@router.get("/payment-gateways", response_class=HTMLResponse)
async def payment_gateways(
    request: Request,
    user: CurrentUser = Depends(_ADMIN),
) -> HTMLResponse:
    """Pantalla de pasarelas: estado por pasarela del tenant del Hub + formulario
    para guardar credenciales (Stripe, Conekta). Las llaves NUNCA se muestran."""
    from app.core.clients.hub_client import HubAdminClient
    from app.core.config import get_settings
    s = get_settings()
    configured: list = []
    catalog: list = []
    error: str | None = None
    if not s.HUB_ADMIN_KEY:
        error = "HUB_ADMIN_KEY no configurada en el CAF."
    else:
        cli = HubAdminClient()
        try:
            data = await cli.list_gateways(s.HUB_COMPANY_ID)
            configured = data.get("configured", [])
            catalog = [g for g in data.get("catalog", []) if g.get("status") == "active"]
        except Exception as e:  # noqa: BLE001
            error = f"No se pudo consultar el Hub: {e}"
        finally:
            await cli.close()
    return templates.TemplateResponse(
        request, "admin/payment_gateways.html",
        {"user": user, "company_id": s.HUB_COMPANY_ID, "fields": _GATEWAY_FIELDS,
         "configured": configured, "catalog": catalog, "error": error,
         "saved": request.query_params.get("saved")},
    )


@router.post("/payment-gateways")
async def save_payment_gateway(
    request: Request,
    user: CurrentUser = Depends(_ADMIN),
    gateway_slug: str = Form(...),
    is_default: str | None = Form(None),
    is_active: str | None = Form(None),
):
    """Recibe el formulario y manda las credenciales al Hub (que las cifra).

    Lee dinámicamente los campos `cred_<nombre>` según la pasarela; descarta
    vacíos. El CAF no persiste ninguna credencial.
    """
    from app.core.clients.hub_client import HubAdminClient
    from app.core.config import get_settings
    s = get_settings()
    slug = gateway_slug.lower().strip()
    if slug not in _GATEWAY_FIELDS:
        raise HTTPException(422, "pasarela no soportada por este formulario")

    form = await request.form()
    creds = {
        f: str(form.get(f"cred_{f}", "")).strip()
        for f in _GATEWAY_FIELDS[slug]
        if str(form.get(f"cred_{f}", "")).strip()
    }
    # solo se exigen los campos obligatorios; webhook_secret es opcional (se
    # puede añadir luego, al crear el webhook en la pasarela).
    missing = [f for f in _GATEWAY_REQUIRED.get(slug, _GATEWAY_FIELDS[slug]) if f not in creds]
    if missing:
        return RedirectResponse(
            "/admin/payment-gateways?saved=error_faltan_campos", status_code=303)

    if not s.HUB_ADMIN_KEY:
        return RedirectResponse(
            "/admin/payment-gateways?saved=error_sin_admin_key", status_code=303)

    cli = HubAdminClient()
    try:
        await cli.save_gateway(
            company_id=s.HUB_COMPANY_ID, gateway_slug=slug, credentials=creds,
            is_default=(is_default == "on"), is_active=(is_active != "off"),
        )
    except Exception:  # noqa: BLE001
        return RedirectResponse(
            "/admin/payment-gateways?saved=error", status_code=303)
    finally:
        await cli.close()
    return RedirectResponse("/admin/payment-gateways?saved=1", status_code=303)


@router.post("/payment-gateways/default")
async def set_default_payment_gateway(
    request: Request,
    user: CurrentUser = Depends(_ADMIN),
    gateway_slug: str = Form(...),
):
    """Fija la pasarela ACTIVA con la que cobra el CAF (la que elige el usuario).

    Es la que de verdad decide el cobro: el CAF la lee al iniciar cada cargo. No
    re-envía credenciales (solo marca el default en el Hub).
    """
    from app.core.clients.hub_client import HubAdminClient
    from app.core.config import get_settings
    s = get_settings()
    if not s.HUB_ADMIN_KEY:
        return RedirectResponse(
            "/admin/payment-gateways?saved=error_sin_admin_key", status_code=303)
    cli = HubAdminClient()
    try:
        await cli.set_default(company_id=s.HUB_COMPANY_ID,
                              gateway_slug=gateway_slug.lower().strip())
    except Exception:  # noqa: BLE001
        return RedirectResponse(
            "/admin/payment-gateways?saved=error_default", status_code=303)
    finally:
        await cli.close()
    return RedirectResponse("/admin/payment-gateways?saved=default_ok", status_code=303)


# ---------------------------------------------------------------------
# reportes de consumo
# ---------------------------------------------------------------------


@router.get("/reports/consumption", response_class=HTMLResponse)
async def reports_consumption_page(
    request: Request,
    user: CurrentUser = Depends(_OPS),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    oc, op = _org_scope(user, "organization_id")
    cores_rows = (await db.execute(text(f"""
        SELECT DISTINCT source_core FROM services
        WHERE source_core IS NOT NULL{oc}
        ORDER BY source_core
    """), op)).scalars().all()
    return templates.TemplateResponse(
        request, "admin/reports.html",
        {"user": user, "available_cores": list(cores_rows)},
    )


@router.get("/reports/consumption/data")
async def reports_consumption_data(
    request: Request,
    date_from: str = Query(...),
    date_to: str = Query(...),
    apps: str = Query(""),
    cores: str = Query(""),
    user: CurrentUser = Depends(_OPS),
    db: AsyncSession = Depends(get_db),
) -> dict:
    oc, op = _org_scope(user, "l.organization_id")
    filters = [
        "l.kind = 'debit'",
        "l.created_at::date BETWEEN :df AND :dt",
    ]
    if oc:  # non-platform user: enforce tenant isolation
        filters.append("l.organization_id = :_org")
    params: dict = {**op, "df": date_from, "dt": date_to}

    app_list = [a.strip() for a in apps.split(",") if a.strip()]
    core_list = [c.strip() for c in cores.split(",") if c.strip()]

    if app_list:
        phs = ", ".join(f":ap{i}" for i in range(len(app_list)))
        filters.append(f"COALESCE(s.app_slug, '—') IN ({phs})")
        for i, a in enumerate(app_list):
            params[f"ap{i}"] = a

    if core_list:
        phs = ", ".join(f":co{i}" for i in range(len(core_list)))
        filters.append(f"COALESCE(s.source_core, '—') IN ({phs})")
        for i, c in enumerate(core_list):
            params[f"co{i}"] = c

    where = " AND ".join(filters)

    rows = (await db.execute(text(f"""
        SELECT
          COALESCE(c.trade_name, c.legal_name)  AS client_name,
          COALESCE(s.app_slug, '—')            AS app_slug,
          COALESCE(s.source_core, '—')         AS core,
          l.service_code,
          l.created_at::date                   AS fecha,
          COALESCE(SUM(l.units), 0)            AS units,
          COALESCE(SUM(l.amount_cents), 0)     AS cents
        FROM prepaid_ledger l
        JOIN clients c ON c.id = l.client_id
        LEFT JOIN services s
          ON s.code = l.service_code
          AND s.organization_id = l.organization_id
        WHERE {where}
        GROUP BY COALESCE(c.trade_name, c.legal_name), s.app_slug, s.source_core, l.service_code, l.created_at::date
        ORDER BY cents DESC
        LIMIT 500
    """), params)).mappings().all()

    total_cents = sum(int(r["cents"]) for r in rows)
    unique_clients = len({r["client_name"] for r in rows})

    by_app: dict[str, int] = {}
    by_core: dict[str, int] = {}
    by_svc: dict[str, int] = {}
    for r in rows:
        by_app[r["app_slug"]] = by_app.get(r["app_slug"], 0) + int(r["cents"])
        by_core[r["core"]] = by_core.get(r["core"], 0) + int(r["cents"])
        by_svc[r["service_code"]] = by_svc.get(r["service_code"], 0) + int(r["cents"])

    top_entry = max(by_svc.items(), key=lambda x: x[1], default=("—", 0))

    return {
        "metrics": {
            "total_cents": total_cents,
            "tx_count": len(rows),
            "unique_clients": unique_clients,
            "top_service": top_entry[0],
            "top_service_cents": top_entry[1],
            "top_service_pct": round(top_entry[1] / total_cents * 100) if total_cents else 0,
        },
        "by_app": [
            {"app": k, "cents": v}
            for k, v in sorted(by_app.items(), key=lambda x: -x[1])
        ],
        "by_core": [
            {"core": k, "cents": v}
            for k, v in sorted(by_core.items(), key=lambda x: -x[1])
        ],
        "by_service": [
            {"service": k, "cents": v}
            for k, v in sorted(by_svc.items(), key=lambda x: -x[1])[:5]
        ],
        "rows": [
            {
                "client": r["client_name"],
                "app": r["app_slug"],
                "core": r["core"],
                "service": r["service_code"],
                "fecha": str(r["fecha"]),
                "units": int(r["units"]),
                "cents": int(r["cents"]),
            }
            for r in rows
        ],
    }


# =====================================================================
# DISTRIBUIDORES — módulo de referidos y comisiones
# =====================================================================

@router.get("/distributors", response_class=HTMLResponse)
async def list_distributors(
    request: Request,
    user: CurrentUser = Depends(_OPS),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    oc, op = _org_scope(user, "d.organization_id")
    rows = (await db.execute(text(f"""
        SELECT d.id, d.name, d.referral_code, d.commission_pct, d.is_active,
               COUNT(dc.id) FILTER (WHERE dc.status = 'pending') AS pending_count,
               COALESCE(SUM(dc.commission_cents) FILTER (WHERE dc.status = 'pending'), 0) AS pending_cents,
               COALESCE(SUM(dc.commission_cents) FILTER (WHERE dc.status = 'paid'), 0) AS paid_cents
        FROM distributors d
        LEFT JOIN distributor_commissions dc ON dc.distributor_id = d.id
        WHERE TRUE{oc}
        GROUP BY d.id
        ORDER BY d.name
    """), op)).mappings().all()
    return templates.TemplateResponse(
        request, "admin/distributors.html",
        {"user": user, "rows": list(rows),
         "saved": request.query_params.get("saved")},
    )


@router.post("/distributors", response_class=RedirectResponse)
async def create_distributor_new(
    request: Request,
    user: CurrentUser = Depends(_WRITE),
    db: AsyncSession = Depends(get_db),
    name: str = Form(...),
    referral_code: str = Form(...),
    commission_pct: float = Form(...),
) -> RedirectResponse:
    nm = name.strip()
    rc = referral_code.strip().upper()
    if not nm or not rc:
        return RedirectResponse("/admin/distributors?saved=error_campos", status_code=303)
    if not (0 <= commission_pct <= 100):
        return RedirectResponse("/admin/distributors?saved=error_pct", status_code=303)
    await bind_actor(db, actor_user_id=user.id,
                     actor_ip=request.client.host if request.client else None,
                     request_id=getattr(request.state, "request_id", None))
    try:
        await db.execute(
            text("""INSERT INTO distributors (name, referral_code, commission_pct, organization_id)
                    VALUES (:n, :rc, :pct, :org)"""),
            {"n": nm, "rc": rc, "pct": commission_pct, "org": user.organization_id},
        )
    except Exception:
        return RedirectResponse("/admin/distributors?saved=error_dup", status_code=303)
    return RedirectResponse("/admin/distributors?saved=ok", status_code=303)


@router.get("/distributors/{dist_id}", response_class=HTMLResponse)
async def distributor_detail(
    request: Request,
    dist_id: int,
    user: CurrentUser = Depends(_OPS),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    oc, op = _org_scope(user, "d.organization_id")
    dist = (await db.execute(text(f"""
        SELECT d.id, d.name, d.referral_code, d.commission_pct, d.is_active,
               COUNT(DISTINCT c.id) AS clientes_referidos
        FROM distributors d
        LEFT JOIN clients c ON c.referral_distributor_id = d.id
        WHERE d.id = :did{oc}
        GROUP BY d.id
    """), {"did": dist_id, **op})).mappings().first()
    if not dist:
        raise HTTPException(404, "distribuidor no encontrado")

    comisiones = (await db.execute(text("""
        SELECT dc.id, dc.commission_cents, dc.base_cents, dc.commission_pct,
               dc.status, dc.created_at, dc.paid_at, dc.notes, dc.payment_hub_txn,
               COALESCE(c.trade_name, c.legal_name) AS cliente
        FROM distributor_commissions dc
        JOIN clients c ON c.id = dc.client_id
        WHERE dc.distributor_id = :did
        ORDER BY dc.created_at DESC
        LIMIT 200
    """), {"did": dist_id})).mappings().all()
    return templates.TemplateResponse(
        request, "admin/distributor_detail.html",
        {"user": user, "dist": dict(dist), "comisiones": list(comisiones),
         "saved": request.query_params.get("saved")},
    )


@router.post("/distributors/{dist_id}/commissions/{cid}/pay",
             response_class=RedirectResponse)
async def mark_commission_paid(
    request: Request,
    dist_id: int,
    cid: int,
    user: CurrentUser = Depends(_WRITE),
    db: AsyncSession = Depends(get_db),
    notes: str = Form(default=""),
) -> RedirectResponse:
    await bind_actor(db, actor_user_id=user.id,
                     actor_ip=request.client.host if request.client else None,
                     request_id=getattr(request.state, "request_id", None))
    result = await db.execute(text("""
        UPDATE distributor_commissions
        SET status = 'paid', paid_at = now(), paid_by_user_id = :u, notes = :n
        WHERE id = :cid AND distributor_id = :did AND status = 'pending'
    """), {"cid": cid, "did": dist_id, "u": user.id, "n": notes.strip() or None})
    saved = "paid_ok" if result.rowcount else "error_estado"
    return RedirectResponse(f"/admin/distributors/{dist_id}?saved={saved}", status_code=303)
