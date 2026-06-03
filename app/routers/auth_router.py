"""Auth: login (UI + JSON), logout, signup-request publico."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import write_event
from app.core.database import get_db
from app.core.jwt_auth import (
    ACCESS_COOKIE,
    REFRESH_COOKIE,
    clear_auth_cookies,
    current_user,
    login_user,
    set_auth_cookies,
)

router = APIRouter(tags=["auth"])
templates = Jinja2Templates(directory="app/templates")


# ---------------------------------------------------------------------
# UI HTML
# ---------------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "auth/login.html", {"error": None}
    )


@router.post("/login")
async def login_submit(
    request: Request,
    response: Response,
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    try:
        user, access, refresh = await login_user(db, email, password)
    except HTTPException as e:
        return templates.TemplateResponse(
            request, "auth/login.html",
            {"error": e.detail}, status_code=e.status_code,
        )

    await write_event(
        db, actor_user_id=user.id, actor_ip=request.client.host if request.client else None,
        entity_type="users", entity_id=user.id, action="login",
        request_id=getattr(request.state, "request_id", None),
    )

    # decide destino segun rol y host
    is_internal = user.has("super_admin", "finanzas", "lectura")
    redirect_to = "/admin/dashboard" if is_internal else "/portal/dashboard"
    resp = RedirectResponse(redirect_to, status_code=status.HTTP_303_SEE_OTHER)
    set_auth_cookies(resp, access, refresh)
    return resp


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    try:
        user = await current_user(request)
        await write_event(
            db, actor_user_id=user.id,
            actor_ip=request.client.host if request.client else None,
            entity_type="users", entity_id=user.id, action="logout",
            request_id=getattr(request.state, "request_id", None),
        )
    except HTTPException:
        pass
    resp = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
    clear_auth_cookies(resp)
    return resp


# ---------------------------------------------------------------------
# JSON (para API clients)
# ---------------------------------------------------------------------


class LoginPayload(BaseModel):
    email: EmailStr
    password: str


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


@router.post("/api/v2/auth/login", response_model=LoginResponse)
async def login_json(payload: LoginPayload, db: AsyncSession = Depends(get_db)) -> LoginResponse:
    _, access, refresh = await login_user(db, payload.email, payload.password)
    return LoginResponse(access_token=access, refresh_token=refresh)


# ---------------------------------------------------------------------
# Signup request (publico - solo solicita acceso, no crea cuenta)
# ---------------------------------------------------------------------


class SignupRequestPayload(BaseModel):
    legal_name: str
    rfc: str
    contact_email: EmailStr
    contact_phone: str | None = None
    notes: str | None = None


@router.get("/signup-request", response_class=HTMLResponse)
async def signup_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "auth/signup_request.html", {})


@router.post("/signup-request")
async def signup_submit(
    payload: SignupRequestPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    await write_event(
        db, actor_user_id=None,
        actor_ip=request.client.host if request.client else None,
        entity_type="signup_requests", entity_id=None, action="create",
        new_values=payload.model_dump(),
        request_id=getattr(request.state, "request_id", None),
    )
    return {"status": "received"}
