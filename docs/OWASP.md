# Auditoría OWASP — inovaweb-admin-financiera (CAF)

**Fecha:** 2026-06-06
**Alcance:** revisión de código + base de datos + configuración del CAF (Nivel 2).
**Veredicto global:** **PASS CON OBSERVACIONES** en seguridad web; **1 hallazgo
CRÍTICO de contrato** (C1) que bloquea el commit hasta su resolución (ver §0).

---

## 0. Bloqueo de commit (regla de la auditoría global)

| ID | Severidad | Descripción | Bloquea commit |
|----|-----------|-------------|----------------|
| **C1** | 🔴 CRÍTICO | `MedidorClient` del CAF acredita en `POST /admin/v1/wallets/{id}/credit` y compensa en `DELETE /admin/v1/wallets/{id}`, rutas que el Medidor **no expone** (credit real: `/v1/wallets/{id}/credit`). Toda recarga/onboarding daría 404. | **SÍ** |

`app/core/clients/medidor_client.py:78` y `:96`. No es vulnerabilidad de seguridad
web (OWASP), sino contrato roto con potencial de inconsistencia financiera (recarga
"confirmada" en Hub/Conekta sin acreditar saldo). Se registra aquí por su impacto
financiero. **No corregido en esta sesión** (regla: no modificar código sin
autorización). Mientras no se corrija y re-verifique QA, no emitir commit del CAF.

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

- Ningún secreto en código. Todos en `.env` vía `SecretStr` (`config.py:28-61`),
  con `.get_secret_value()` al usar. `.gitignore` excluye `.env`.
- Validadores fail-fast: el arranque falla si faltan `DATABASE_URL`,
  `POSTGRES_PASSWORD`, `AES_KEY`, `JWT_SECRET`, las 4 API keys de cores,
  `PAC_API_KEY/SECRET`, `RFC_EMISOR`, `KEY_PASSWORD`.
- ⚠️ **Observación:** `HUB_WEBHOOK_SECRET` cae a `HUB_API_KEY` en dev/staging
  (`config.py:97-105`); en prod es obligatorio (validator `:84-95`). Documentar que
  dev use un secreto dedicado.

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

## Resumen

| Categoría | Veredicto |
|---|---|
| SQL Injection | ✅ PASS |
| XSS | ✅ PASS (CSP mejorable) |
| CSRF | ⚠️ WARN (SameSite ok, sin token) |
| Secrets | ✅ PASS (fallback dev a vigilar) |
| Sesiones | ⚠️ WARN (sin revocación de JWT) |
| Endpoints sin auth | ✅ PASS |
| Control de acceso / multi-tenant | ✅ PASS |
| Webhooks | ✅ PASS |
| Dinero / append-only | ✅ PASS |
| **Contrato Medidor (C1)** | 🔴 **CRÍTICO — bloquea commit** |

**Acciones antes del commit del CAF:**
1. Resolver C1 (ruta `/admin/v1` → `/v1` en credit y revisar delete/compensación).
2. Re-verificar QA del flujo prepago end-to-end (TASK-15b).
3. (Recomendado, no bloqueante) token CSRF, `revoked_tokens`, validación de monto en `/api/v2`.

*Auditoría OWASP — auditoría global Inovaweb 2026-06-06.*
