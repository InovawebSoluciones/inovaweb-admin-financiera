# Auditoría OWASP — inovaweb-admin-financiera (CAF)

**Fecha:** 2026-06-07 (actualización post-Grupo3)
**Versión:** sesión 2026-06-07 v3 (traslada)
**Alcance:** revisión de código + base de datos + configuración del CAF (Nivel 2).
Incluye código nuevo de sesión 2026-06-07: `scraping_client.py`, `onboarding.py` paso 5b,
`billing.py` secciones 2b+2c, `pricing.py`, migraciones 005-007, hardening H1-H5.

**Veredicto global:** **PASS CON OBSERVACIONES** — sin bloqueo de commit.
El hallazgo C1 (contrato medidor_client) fue **resuelto** en sesión 2026-06-07 y verificado
con pytest 3/3 PASSED. Los hallazgos restantes son observaciones (⚠️) no bloqueantes.

Hallazgos adicionales del code-review de sesión 2026-06-07 (no OWASP, pero se registran
por impacto financiero):
- **CR-1** ⚠️: `billing.py:185,241` — `period_start.isoformat()` genera `"YYYY-MM-DD"` (solo fecha);
  el Medidor espera ISO-8601 con hora. Puede causar 400 o ventana incorrecta. Fix: añadir `"T00:00:00Z"`.
- **CR-2** ⚠️: `billing.py:290` — `unit_price = amount_cents // count` (división entera);
  puede generar `unit_price * count ≠ amount_cents` si no es divisible exacto. Relevante para CFDI.

Estos dos son deuda técnica documentada; no bloquean el commit del MVP (CFDI diferido a sprint 4).

---

## 0. Estado de hallazgos previos

| ID | Severidad | Descripción | Estado |
|----|-----------|-------------|--------|
| **C1** | ✅ RESUELTO | `medidor_client.py` rutas incorrectas de `credit`/`suspend_wallet` | Corregido 2026-06-07; pytest PASSED |

---

## 1. SQL Injection — ✅ PASS

- Todas las consultas usan `sqlalchemy.text()` con **bind params** (`:q`, `:cid`, …),
  sin interpolación de strings. Ej.: `admin_router.py:67-73` (`ILIKE '%'||:q||'%'`,
  donde `||` es operador SQL y `:q` va parametrizado).
- No se detectó construcción dinámica de SQL con input del usuario.

## 2. XSS — ✅ PASS (con observación CSP)

- UI con Jinja2 (`Jinja2Templates(directory="app/templates")`), **autoescape activo
  por defecto** (no se encontró `autoescape=False`).
- ⚠️ **Observación:** CSP con `script-src 'self' 'unsafe-inline'` (necesario para
  HTMX) amplía la superficie XSS. Mitigable migrando a hashes/nonces de HTMX.

## 3. CSRF — ⚠️ WARN (mitigado por SameSite, sin token explícito)

- Cookies de sesión con `SameSite=Strict` + `httpOnly` + `Secure` (no dev)
  (`app/core/jwt_auth.py:52-54`) → protege contra CSRF clásico.
- ⚠️ SECURITY.md menciona "tokens CSRF para POST mutativos" pero **no están
  implementados**. Si `SameSite` se relajara, los POST `/admin/*` quedarían
  expuestos. Recomendado: token CSRF de origen para formularios mutativos.

## 4. Secrets hardcodeados — ✅ PASS

- Ningún secreto en código. Todos en `.env` vía `SecretStr`, con `.get_secret_value()` al usar. `.gitignore` excluye `.env`.
- Validadores fail-fast: el arranque falla si faltan `DATABASE_URL`, `POSTGRES_PASSWORD`, `AES_KEY`, `JWT_SECRET`, las 4 API keys de cores, `SCRAPING_ADMIN_KEY` (nuevo 2026-06-07), `RFC_EMISOR`, `KEY_PASSWORD`.
- `scraping_client.py` — nuevo cliente verificado: usa `SCRAPING_ADMIN_KEY` vía `get_secret_value()`. No se loguea el secreto. ✅
- ⚠️ **Observación:** `HUB_WEBHOOK_SECRET` cae a `HUB_API_KEY` en dev/staging (`config.py`); en prod es obligatorio. Documentar que dev use un secreto dedicado para no mezclar con la API key.

## 5. Gestión de sesiones (JWT/cookies) — ⚠️ WARN

- ✅ Argon2id para passwords (params OWASP 2024: 64 MiB, 3 iter, 4 lanes)
  (`app/core/password.py:7-25`).
- ✅ Access token 15 min + refresh 30 días, cookies `httpOnly`/`SameSite=Strict`/`Secure`.
- ✅ Bloqueo de fuerza bruta: 5 intentos → lock 15 min (`jwt_auth.py:162-164`).
  Mensajes de error genéricos (sin enumeración de usuarios).
- ⚠️ **Hallazgo:** tabla `revoked_tokens` referida en SECURITY.md **no existe** en el
  schema. Logout solo borra cookies del navegador; un JWT robado sigue válido hasta
  expirar (≤15 min). Mitigación parcial por TTL corto. Recomendado: implementar
  denylist de refresh tokens (TASK-22).

## 6. Endpoints sin autenticación — ✅ PASS

- Públicos por diseño: `/health`, `/health/db`, `/login` (GET/POST), `/logout`,
  `/signup-request` (GET/POST). Ninguno revela datos sensibles.
- Todos los `/admin/*`, `/portal/*`, `/api/v2/*` usan `Depends(require_roles(...))`.
- Webhooks (`/webhooks/pac`, `/webhooks/hub-payment-paid`) validan firma HMAC +
  timestamp **antes** de parsear el body (`webhooks_router.py:118-124`).

## 7. Control de acceso / multi-tenant (IDOR) — ✅ PASS

- Portal filtra **siempre** por `user.client_id` (`portal_router.py:31-41` y queries);
  404 sin diferenciar "no existe" de "no es tuyo".
- Roles declarados por endpoint (`super_admin`/`finanzas`/`lectura`/`cliente_*`).

## 8. Webhooks (integridad) — ✅ PASS

- HMAC-SHA256 con `hmac.compare_digest` (tiempo constante).
- Timestamp firmado con ventana anti-replay (`HUB_WEBHOOK_TOLERANCE_SEC`, 300 s);
  en prod el timestamp es **obligatorio**.
- Idempotencia a nivel BD: `INSERT ... ON CONFLICT (hub_payment_id) DO NOTHING`
  (`004_payments_idempotency.sql`) + correlación purpose/amount contra `audit_log`.

## 9. Dinero / integridad financiera — ✅ PASS

- 100% centavos BIGINT en `database/001_initial_schema.sql`. Triggers append-only en
  `audit_log`, `payments`, `adjustments` y bloqueo de campos financieros en `invoices`
  (`002_security_constraints.sql`).
- Parseo defensivo contra floats no enteros en webhooks (`prepago.py` FIX-7).
- ⚠️ **Observación:** los modelos Pydantic de `/api/v2` no validan explícitamente que
  el monto sea entero antes de llegar a la capa de servicio; el rechazo ocurre en
  `prepago.py:159-161`. Recomendado: validar `amount_cents: int` en el schema de entrada.

---

## Resumen (2026-06-07)

| Categoría | Veredicto | Notas |
|---|---|---|
| SQL Injection | ✅ PASS | Sin interpolación de input de usuario en SQL |
| XSS | ✅ PASS | Jinja2 autoescape activo; `hx-swap=innerHTML` solo con respuesta interna |
| CSRF | ⚠️ WARN | SameSite=Strict mitiga; sin token CSRF explícito |
| Secrets | ✅ PASS | Sin hardcode; `SCRAPING_ADMIN_KEY` correctamente en `SecretStr` |
| Sesiones | ⚠️ WARN | Sin `revoked_tokens`; mitigado por TTL 15 min del access token |
| Endpoints sin auth | ✅ PASS | Todos los endpoints sensibles requieren JWT + rol |
| Control de acceso / IDOR | ✅ PASS | Portal filtra por `client_id` del JWT |
| Webhooks | ✅ PASS | HMAC-SHA256 + anti-replay; H5 filtra por client_id |
| Dinero / append-only | ✅ PASS | BIGINT centavos, triggers append-only |
| Code review financiero | ⚠️ WARN | CR-1 (timestamp fecha vs datetime), CR-2 (división entera) — deuda sprint 4 |

**Acciones antes del commit (2026-06-07):** ningún bloqueante.

**Deuda técnica documentada (no bloqueante):**
1. CR-1: `billing.py` — `period_start.isoformat()` → agregar `"T00:00:00Z"` para compatibilidad Medidor.
2. CR-2: `billing.py:290` — reconsiderar `unit_price // count` para CFDI correcto (sprint 4).
3. Implementar `revoked_tokens` / denylist de refresh tokens (TASK-22 existente).
4. Token CSRF explícito en formularios mutativos (sprint 2+).
5. `HUB_WEBHOOK_SECRET` separado del API key en dev (config).

*Auditoría OWASP — auditoría global Inovaweb 2026-06-06.*
