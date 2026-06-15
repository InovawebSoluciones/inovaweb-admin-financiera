"""Inovaweb CAF - entrypoint FastAPI.

Mismo backend sirve admin.inovaweb.com.mx y app.inovaweb.com.mx.
El routing por host se aplica mediante middleware que valida que cada
prefijo de path solo se sirva en su dominio correcto.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import get_settings
from app.core.observability import RequestContextMiddleware, configure_logging
from app.routers import (
    admin_router,
    api_router,
    auth_router,
    catalog_plans_router,
    catalog_promos_router,
    catalog_services_router,
    health_router,
    orgs_router,
    portal_router,
    webhooks_router,
)

configure_logging()
log = logging.getLogger("main")
settings = get_settings()

app = FastAPI(
    title="Inovaweb CAF",
    version="0.1.0",
    docs_url="/docs" if settings.ENV == "dev" else None,
    redoc_url="/redoc" if settings.ENV == "dev" else None,
    openapi_url="/openapi.json" if settings.ENV == "dev" else None,
)


# ---------------------------------------------------------------------
# host enforcement middleware
# ---------------------------------------------------------------------


class HostEnforcementMiddleware(BaseHTTPMiddleware):
    """Cada prefijo solo se permite en su host correspondiente.

    /admin/* solo en ADMIN_DOMAIN
    /portal/* solo en PORTAL_DOMAIN
    /health, /login, /logout, /signup-request, /api/*, /webhooks/* en ambos
    En dev (sin host bien definido) no se restringe.
    """

    async def dispatch(self, request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0].lower()
        path = request.url.path

        if settings.ENV != "dev":
            if path.startswith("/admin") and host != settings.ADMIN_DOMAIN.lower():
                return RedirectResponse(
                    f"https://{settings.ADMIN_DOMAIN}{path}",
                    status_code=status.HTTP_308_PERMANENT_REDIRECT,
                )
            if path.startswith("/portal") and host != settings.PORTAL_DOMAIN.lower():
                return RedirectResponse(
                    f"https://{settings.PORTAL_DOMAIN}{path}",
                    status_code=status.HTTP_308_PERMANENT_REDIRECT,
                )

        return await call_next(request)


# orden: request-id PRIMERO para tener rid en logs aun si host falla
app.add_middleware(RequestContextMiddleware)
app.add_middleware(HostEnforcementMiddleware)


# ---------------------------------------------------------------------
# static
# ---------------------------------------------------------------------


app.mount("/static", StaticFiles(directory="static"), name="static")


# ---------------------------------------------------------------------
# routers
# ---------------------------------------------------------------------


app.include_router(health_router.router)
app.include_router(auth_router.router)
app.include_router(admin_router.router)
app.include_router(portal_router.router)
app.include_router(api_router.router)
app.include_router(orgs_router.router)
app.include_router(catalog_services_router.router)
app.include_router(catalog_plans_router.router)
app.include_router(catalog_promos_router.router)
app.include_router(webhooks_router.router)


# ---------------------------------------------------------------------
# raiz: redirige segun host
# ---------------------------------------------------------------------


@app.get("/")
async def root(request: Request):
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if host == settings.PORTAL_DOMAIN.lower():
        return RedirectResponse("/portal/dashboard")
    return RedirectResponse("/admin/dashboard")


# ---------------------------------------------------------------------
# error handler global (JSON para API, HTML mantiene su flujo)
# ---------------------------------------------------------------------


@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    if request.url.path.startswith("/api/") or "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    # HTML: deja que el handler default lo maneje
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.on_event("startup")
async def on_startup() -> None:
    log.info("caf_startup", extra={"env": settings.ENV, "port": settings.PORT})


@app.on_event("shutdown")
async def on_shutdown() -> None:
    log.info("caf_shutdown")
