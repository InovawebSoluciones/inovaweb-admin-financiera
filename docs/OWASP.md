# Auditoría OWASP — inovaweb-admin-financiera (CAF)

**Fecha:** 2026-06-14
**Versión:** commit `af0e078` (rama `main`, VPS == GitHub)
**Alcance:** revisión de código + base de datos + configuración del CAF (Nivel 2),
incluyendo la capa **saldo prepago nativo** (sesiones jun 9–11): `migrations/030_prepaid_ledger.sql`,
endpoints app-facing `POST /clients/{id}/charge`, `GET /prepaid-balance`, `GET /ledger`, `GET /services`,
`GET /clients/{id}/plan-limits`, `POST /apps/onboard`, autenticación por Bearer (`_verify_app_key`).

**Veredicto global:** **PASS CON OBSERVACIONES** — sin bloqueo de commit.
No se hallaron ❌ FALLO. Las observaciones (⚠️) son deuda técnica documentada heredada (token CSRF
explícito; `revoked_tokens`); el flujo de cobro nuevo cierra los riesgos de concurrencia y de
separación de credenciales por app.

---

## Resumen

| Categoría | Estado | Hallazgos |
|---|---|---|
| SQL Injection | ✅ PASS | Todo `sqlalchemy.text()` con bind params; barrido sin f-string/concat SQL |
| XSS | ✅ PASS | Jinja2 autoescape; sin `\|safe`/`innerHTML` con datos de usuario |
| CSRF | ⚠️ REVISAR | Cookies `SameSite=Strict` mitigan; sin token CSRF explícito (API app-facing usa Bearer, exenta) |
| Secrets hardcodeados | ✅ PASS | Todo en `SecretStr`/`.env`; `.env` gitignored; barrido sin secretos en código |
| Gestión de sesiones | ⚠️ REVISAR | JWT httpOnly/Strict/Secure + TTL 15 min + lockout; sin `revoked_tokens` (mitigado por TTL) |
| Endpoints sin auth | ✅ PASS | Públicos acotados; admin/portal por JWT+rol; app-facing por Bearer; webhooks por HMAC |
| Control de acceso / IDOR | ✅ PASS | Portal filtra por `client_id` del JWT; app-facing por client_id explícito + Bearer |
| Cobro pay-per-use | ✅ PASS | Idempotente `(client_id, idempotency_key)` + `pg_advisory_xact_lock`; 402 por saldo |
| Webhooks | ✅ PASS | HMAC-SHA256 `compare_digest` + timestamp firmado anti-replay |
| Dinero / append-only | ✅ PASS | BIGINT centavos; `prepaid_ledger`/`payments`/`audit_log` append-only |

---

## Detalle por categoría

### 1. SQL Injection — ✅ PASS
- Barrido de todo `app/` por SQL construido con f-string, concatenación o `.format()`: **0 hallazgos**.
- Las queries de la capa saldo-B (`api_router.py` charge/ledger/services/prepaid-balance) usan
  `text()` con bind params (`:c`, `:k`, `:l`, `:sc`, `:u`). El `idempotency_key`, `service_code` y
  `client_id` van parametrizados.

### 2. XSS — ✅ PASS
- Jinja2 con autoescape por defecto. Barrido de `templates/` por `|safe`/`innerHTML`: **0 hallazgos**.
- Los endpoints app-facing devuelven JSON (no HTML), sin renderizado de input.

### 3. CSRF — ⚠️ REVISAR (sin cambio respecto a auditoría previa)
- Cookies de sesión `SameSite=Strict` + `httpOnly` + `Secure` (`jwt_auth.py`).
- Los endpoints app-facing (`/apps/onboard`, `/charge`, …) se autentican por **Bearer**, no por cookie:
  **exentos de CSRF** (un sitio tercero no puede adjuntar el Bearer).
- Deuda: token CSRF explícito para los formularios mutativos `/admin/*` si alguna vez se relaja
  `SameSite`.

### 4. Secrets hardcodeados — ✅ PASS
- Barrido por `password=/secret=/api_key=/token=` con literal: **0 hallazgos** fuera de `SecretStr`.
- Nuevas llaves de app (`SCRAPING_ADMIN_KEY`, `SWIGG_ADMIN_KEY`) son `SecretStr` en `config.py` y se
  comparan con `get_secret_value()` en `_verify_app_key`; no se loguean. `.env` y `.env.*` en
  `.gitignore` (solo `.env.example` trackeado).

### 5. Gestión de sesiones — ⚠️ REVISAR
- ✅ Argon2id; access 15 min + refresh 30 días con rotación; cookies httpOnly/Strict/Secure.
- ✅ Bloqueo de fuerza bruta (lock tras intentos fallidos → `423 LOCKED`); errores genéricos.
- ⚠️ `revoked_tokens` no implementada: logout no invalida el JWT en servidor (válido ≤15 min por TTL).
  Deuda técnica heredada (TASK-22). No aplica a la API app-facing (Bearer estático por app, rotable
  por `.env`).

### 6. Endpoints sin autenticación — ✅ PASS
- Públicos por diseño: `/health`, `/health/db`, `/login`, `/logout`, `/signup-request`.
- `/admin/*`, `/portal/*` → JWT + rol. `/api/v2/*` app-facing (charge, prepaid-balance, ledger,
  services, plan-limits, apps/onboard) → `_verify_app_key` (Bearer; 401 si no coincide).
- Webhooks → HMAC + timestamp antes de cualquier I/O.

### 7. Control de acceso / IDOR — ✅ PASS
- Portal filtra siempre por `user.client_id`. Los endpoints app-facing reciben `client_id` explícito y
  exigen Bearer de app válido; el `client_id` lo controla la app dueña de la llave.
- ⚠️ Observación de diseño (no vulnerabilidad): `_verify_app_key` no distingue **qué** app es la
  portadora del Bearer, así que una app con llave válida podría cobrar a un `client_id` de otra app. Hoy
  hay 2 portadoras de confianza (LiaForge, Swigg). Si crece el número de apps o baja la confianza,
  considerar atar la llave a un conjunto de `client_id`/`plan_code` permitidos (ver ADR-017, aislamiento
  duro diferido).

### 8. Cobro pay-per-use / doble-gasto — ✅ PASS
- `POST /charge` idempotente por `(client_id, idempotency_key)` con replay (no re-debita).
- `pg_advisory_xact_lock(client_id)` serializa cobros concurrentes del mismo cliente.
- 402 `saldo_insuficiente` con `{balance_cents, required_cents}` antes de debitar.

### 9. Webhooks — ✅ PASS
- HMAC-SHA256 con `hmac.compare_digest` (tiempo constante); timestamp firmado con ventana
  `HUB_WEBHOOK_TOLERANCE_SEC` (300 s); en prod `HUB_WEBHOOK_SECRET` es obligatorio y dedicado
  (validator fail-fast, no fallback a `HUB_API_KEY`).

### 10. Dinero / integridad financiera — ✅ PASS
- 100% centavos BIGINT. `prepaid_ledger` append-only (kind credit/debit; correcciones = entrada nueva,
  nunca UPDATE/DELETE), consistente con los triggers de `002_security_constraints.sql` sobre
  `payments`/`audit_log`/`invoices`/`adjustments`.

---

## Acciones requeridas antes del siguiente push
Ninguna (sin ❌).

## Deuda técnica documentada (no bloqueante)
1. `revoked_tokens` / denylist de refresh tokens (TASK-22) — heredada.
2. Token CSRF explícito en formularios mutativos `/admin/*` — heredada.
3. Atar el Bearer de app a `client_id`/`plan_code` permitidos si crece el nº de apps (ADR-017).
4. Retirar el dual-write del saldo en el Medidor cuando termine la transición (ADR-015).
5. CR-1/CR-2 de billing (timestamp ISO con hora; división entera de `unit_price`) — diferidas a CFDI/sprint 4.

*Auditoría OWASP — traslada 2026-06-14, commit `af0e078`.*
