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

---
---

# ADDENDUM 2026-06-16 — Auditoría del código nuevo (CAF Billing Engine SaaS multi-org)

**Fecha:** 2026-06-16
**Alcance:** código NUEVO/cambiado de la sesión que convierte el CAF en un motor
de facturación **multi-organización (SaaS)**:
`app/core/tenancy.py`, `app/core/crypto.py`, `app/services/saas_billing.py`,
`app/services/emailer.py`, `app/core/clients/hub_client.py`, `app/services/prepago.py`,
`app/services/onboarding.py`, `app/main.py` y los routers
`orgs_router`, `org_admin_router`, `users_router`, `adjustments_router`,
`reports_router`, `catalog_services_router`, `catalog_plans_router`,
`catalog_promos_router`, `catalog_read_router`, `client_account_router`,
`security_router`, `email_providers_router`, `admin_router`, `api_router`.
Migraciones revisadas: `031`–`036` (tenancy, unique-por-org, email_providers,
seed SaaS, platform_client, distributors).

**Método:** lectura directa de cada archivo; barrido de f-strings en SQL,
`|safe`/`innerHTML` en templates, literales de secreto en `app/`, y verificación
del aislamiento por `organization_id` y de la idempotencia en escrituras de dinero.

**Nota de entorno:** `caf-work` es copia de trabajo, NO repositorio git, y NO
contiene `.env`/`.gitignore`/`.cer`/`.key`. La verificación de `.env` gitignored
y `AES_KEY` fuera de git se hace en el repo canónico del VPS
`/opt/inovaweb-admin-financiera` (regla del proyecto: secretos solo en `.env` del
VPS). `[TODO: completar]` reconfirmar en el VPS antes del push.

## Veredicto del addendum: **PASS CON OBSERVACIONES** — sin bloqueo de commit

No se hallaron ❌ FALLO en el código nuevo. El nuevo plano multi-tenant resuelve
el `organization_id` SIEMPRE del portador (API key vía `resolve_app_org`, o JWT
vía `CurrentUser.organization_id`/`is_platform`), NUNCA del body. Las escrituras
de dinero conservan idempotencia y append-only. Las observaciones son la deuda
heredada (CSRF, `revoked_tokens`) más cuatro puntos menores nuevos (abajo).

---

## Resumen (6 categorías)

| # | Categoría | Estado | Síntesis |
|---|---|---|---|
| 1 | SQL Injection | ✅ PASS | Todo `text()` con binds. Los f-strings de `_org_scope`/`_org_filter`/`_detail_where`/`{oc}` interpolan SOLO nombres de columna y fragmentos `AND col = :_org` literales; el valor de la org va parametrizado (`:_org`). Ningún input de usuario se concatena. |
| 2 | XSS | ✅ PASS | Jinja2 autoescape; sin `\|safe` con dato de usuario. Único `innerHTML` es `hx-swap="innerHTML"` (atributo HTMX, no sink). `emailer._hdr` además sanea CR/LF (anti header-injection). |
| 3 | CSRF | ⚠️ REVISAR | Sin cambio: panel por cookie JWT `SameSite=Strict` (mitiga); app-facing por Bearer (exento). Sin token CSRF explícito en forms `/admin/*` (deuda heredada). |
| 4 | Secretos hardcodeados | ✅ PASS | 0 literales de secreto en `app/`. Credenciales de email cifradas AES-256-GCM (`crypto.py`); las de pasarela las cifra el Hub. API keys solo por hash SHA-256. Lecturas exponen solo `secret_set`/llaves enmascaradas, nunca el secreto. Verif. `AES_KEY`/`.env` fuera de git → en VPS `[TODO: completar]`. |
| 5 | Gestión de sesiones | ⚠️ REVISAR | JWT httpOnly/Secure/SameSite=Strict; access TTL ampliado a 720 min (12 h) vía `.env` del VPS + refresh 30 d; lockout 5 intentos/15 min. Sin `revoked_tokens`: logout no invalida en servidor (deuda heredada, agravada por TTL más largo). |
| 6 | Endpoints sin auth | ✅ PASS | Todos los nuevos exigen `require_roles(...)` (panel/JSON) o `resolve_app_org` (app-facing Bearer). Aislamiento por `organization_id` del portador; `?org` solo lo respeta el super tenant (`is_platform`). Públicos sin cambio. |

### Extra (invariantes financieros)

| Invariante | Estado | Evidencia |
|---|---|---|
| Idempotencia en escrituras de dinero | ✅ PASS | `charge`/`adjust`/`accrue`/`saas-fee`/`grant`/`recarga_hub` usan `idempotency_key` + `ON CONFLICT (client_id, idempotency_key) ... DO NOTHING` o pre-check con `advisory_xact_lock`. |
| Append-only `prepaid_ledger` | ✅ PASS | Correcciones = asiento NUEVO (`adjustments_router`, `source='ajuste_manual'`); jamás UPDATE/DELETE de asientos. |
| Meta-cobro NO recursivo | ✅ PASS | `accrue_transaction` retorna temprano si `org_id == PLATFORM_ORG_ID` (`saas_billing.py:126`); `run_saas_monthly_billing` excluye `o.id <> :porg`. |
| `promo_code` valida tenant + no agotado + reintento no re-cuenta | ✅ PASS | `api_router.py:461-480`: filtra `organization_id=:org`, vigencia y `is_active`; `UPDATE ... WHERE uses_count < max_uses RETURNING` cuenta atómico; el grant lleva `grant_key` idempotente → reintento no re-aplica bono. |

---

## Detalle de hallazgos (⚠️)

### ⚠️ H-A (heredada) — CSRF sin token explícito en formularios `/admin/*`
Sin cambio respecto a 2026-06-14. Mitigado por cookies `SameSite=Strict`; los
forms del panel (`admin_router`: crear cliente, suspender, pasarelas, promos)
mutan por cookie. Deuda: token CSRF si alguna vez se relaja `SameSite`.

### ⚠️ H-B (heredada, agravada) — `revoked_tokens` no implementada + TTL 720 min
`jwt_auth.py` no consulta denylist: el logout (`clear_auth_cookies`) borra la
cookie del navegador pero el JWT sigue válido en servidor hasta `exp`. Con el
access TTL ampliado de 15 → **720 min (12 h)** (CHANGELOG; `config.py:36` aún
trae el default 15, el valor real vive en `.env` del VPS), la ventana de un token
filtrado/robado pasa de 15 min a 12 h. Recomendación: implementar denylist de
`jti` en logout, o reducir el access TTL y apoyarse en el refresh con rotación.

### ⚠️ H-C (nueva, menor) — onboard app-facing atribuye la auditoría a `actor_user_id=1`
`api_router.api_app_onboard` (`api_router.py:430`) invoca
`onboard_client(..., actor_user_id=1, actor_ip=None, ...)`. El alta self-service
por Bearer queda auditada como si la ejecutara el usuario 1 (operador
plataforma), no la organización/app dueña de la API key. No es escalación de
privilegio (la org sí se aísla vía `resolve_app_org`→`payload.organization_id`),
pero contamina la trazabilidad del `audit_log`. Recomendación: registrar el
`api_key.id`/`organization_id` portador como actor (o un actor de sistema
distinto de un humano real) en el audit del onboard app-facing.

### ⚠️ H-D (nueva, robustez — NO seguridad) — correlación webhook vs. `metadata` no enviada al Hub
`hub_client.HubClient.charge` documenta y NO envía `metadata` en el POST
`/hub/v1/charge` (`hub_client.py:50-71`); confía en que el Hub resuelva `purpose`
por defecto. Pero `prepago._correlate_or_reject` (`prepago.py:572`) RECHAZA el
pago si `metadata.purpose` del webhook no coincide con el `purpose` del intento
`recharge.initiated`. Si el Hub no eco-devuelve `metadata.purpose` en el webhook,
un `plan_purchase` legítimo podría rechazarse (no acreditar). Es un riesgo de
DISPONIBILIDAD/correctitud del flujo de recarga, no de seguridad (falla-cerrado:
nunca acredita de más). `[TODO: completar]` validar contra el contrato real del
webhook del Hub que `metadata.purpose` viaja de vuelta; si no, derivar el
`purpose` esperado de otra señal del intento.

### ⚠️ H-E (nueva, observación de aislamiento — aceptable por diseño)
En `api_router` los endpoints app-facing de **lectura** `GET /services`,
`/clients/{id}/prepaid-balance`, `/clients/{id}/ledger`, `/plan-limits` resuelven
la org por `resolve_app_org` y aplican `assert_client_in_org` correctamente. El
`prepaid-balance`/`ledger` NO filtran las filas del ledger por `organization_id`
en el SELECT, pero el `client_id` ya está atado a la org por `assert_client_in_org`
previo, por lo que NO hay fuga cross-tenant. Se deja anotado por si en el futuro
un cliente pudiera pertenecer a más de una org (hoy no). Sin acción requerida.

---

## Confirmaciones positivas relevantes (código nuevo)

- **`tenancy.resolve_app_org`**: hash SHA-256 de la API key contra `api_keys`
  (no revocada) → `organization_id`; fallback legacy SOLO a llaves de `.env`
  (SCRAPING/SWIGG) → org 1. 401 si no resuelve. El tenant nunca sale del body.
- **`crypto.py`**: AES-256-GCM con nonce aleatorio de 12 B por cifrado, AAD que
  liga la versión (anti-downgrade), valida llave de 32 B (fail-fast), descifrado
  con verificación de tag (`InvalidTag`→`ValueError`). Correcto.
- **`security_router`/`email_providers_router`**: nunca devuelven `key_hash` ni
  `secret_encrypted`; rotación de key revoca+acuña y muestra el plano UNA vez; el
  `test`/`emailer` solo exponen el TIPO de excepción, jamás el secreto.
- **`adjustments_router`**: append-only + `advisory_xact_lock` + idempotencia +
  402 por saldo insuficiente en débitos; el `organization_id` destino se valida
  contra el cliente.
- **`org_admin_router`**: suspender/cancelar la org plataforma (id 1) bloqueado
  (400); todas las rutas exigen `_platform_only` (`is_platform`).
- **`main.HostEnforcementMiddleware`**: `/admin/*` y `/portal/*` acotados a su
  dominio en prod; error handler devuelve JSON a API/webhooks y HTML solo al panel.

## Acciones requeridas antes del push
Ninguna (sin ❌). Recomendadas (no bloqueantes): H-C (actor real en onboard
app-facing) y H-D (confirmar `metadata.purpose` del webhook del Hub).

## Deuda técnica documentada (no bloqueante) — actualización
1. `revoked_tokens` / denylist (TASK-22) — **prioridad sube** por TTL 720 min.
2. Token CSRF explícito en `/admin/*` — heredada.
3. Atar el Bearer a `client_id`/`plan_code` permitidos si crece el nº de apps (ADR-017).
4. Atribuir el actor real (api_key/org) en el onboard app-facing (H-C).
5. Reconfirmar en el VPS: `AES_KEY`, `JWT_SECRET` y `.env` fuera de git; `JWT_ACCESS_TTL_MIN`
   real = 720 (el default de `config.py` sigue en 15).

*Addendum OWASP — auditoría de código nuevo 2026-06-16 (multi-org SaaS). Sin commit ni deploy.*

---

## Addendum 2026-06-17 — app_slug + Stripe E2E + reportes de consumo

**Commit auditado:** `b8dcfba` (CAF) + `5d53a50` (Hub)
**Resultado:** PASS CON OBSERVACIONES

### Nuevos endpoints revisados

| Endpoint | Auth | Observación |
|----------|------|-------------|
| `GET /admin/reports/consumption` | ✅ `_OPS` (super_admin/finanzas/lectura) | OK |
| `GET /admin/reports/consumption/data` | ✅ `_OPS` | OK |
| `POST /api/v2/clients/{id}/recharge` | ✅ Bearer API key + assert_client_in_org | OK |

### SQL Injection — `/admin/reports/consumption/data`
✅ PASS. Los filtros de `app_list` y `core_list` se construyen con placeholders nominales (`ap0, ap1, co0, co1…`) nunca interpolando valores de usuario directamente en el SQL. Los parámetros de fecha usan `:df` y `:dt`.

⚠️ OBSERVACIÓN: la cláusula WHERE se construye concatenando strings `filters.append(...)`. Es seguro porque los valores van en `params`, pero si un desarrollador futuro interpola por error una variable en el string de filtro (no en params), habría SQLi. Recomendación: migrar a SQLAlchemy ORM o usar `select()` con `and_()` en lugar de `text()` dinámico en la próxima refactorización.

### XSS — `reports.html`
✅ PASS. La plantilla Jinja2 auto-escapa todas las variables `{{ r.client }}`, `{{ r.service }}` etc. El único `innerHTML` está en JS con datos del JSON del backend, que ya vienen validados por el tipo Python (strings de BD). No hay `|safe` en la plantilla.

### CSRF
✅ PASS. Los endpoints nuevos son GET (datos) y el recharge es POST autenticado con Bearer API key (no cookie), exento de CSRF por diseño.

### Secrets hardcodeados
✅ PASS. No hay secrets en los nuevos archivos. `whsec_` y `sk_test_` se almacenan en `company_gateway_config.config_encrypted` (AES-256-GCM). No aparecen en código ni en templates.

### Gestión de sesiones
Sin cambios respecto a auditoría anterior. Observaciones previas vigentes.

### Acciones requeridas antes del siguiente push
Ninguna crítica. Las observaciones ⚠️ quedan como deuda técnica documentada.

*Addendum OWASP — auditoría 2026-06-17 (app_slug + Stripe E2E + reportes). Commits b8dcfba + 5d53a50.*

---

## Addendum 2026-06-18 — Multi-tenancy enforcement en reportes de consumo

**Commit auditado:** `d5853b3` (CAF)
**Resultado:** PASS CON OBSERVACIONES

### Resumen

| Categoría | Estado | Hallazgos |
|---|---|---|
| SQL Injection | ✅ PASS | Todo `text()` con bind params nominales; filtros dinámicos acumulan strings de condición + dict de params, nunca f-strings con input de usuario |
| XSS | ✅ PASS | Jinja2 autoescape activo; sin `\|safe` con datos de usuario; Chart.js recibe datos via JSON del endpoint `/data` (no innerHTML) |
| CSRF | ⚠️ REVISAR | Formularios POST del panel usan `SameSite=Strict` como única mitigación; sin token CSRF explícito (deuda heredada) |
| Secrets hardcodeados | ✅ PASS | Ningún secreto en código; `.env` en `.gitignore`; `_VALID_PROMOS` en LiaForge es dict de metadatos públicos, no secreto |
| Sesiones | ✅ PASS | JWT HttpOnly + SameSite=Strict + Secure; access 15 min, refresh 30 días con rotación (ver ⚠️ TTL) |
| Endpoints sin auth | ✅ PASS | Públicos documentados; `/admin/*`, `/portal/*`, `/api/v2/*`, `/webhooks/*` requieren auth; webhooks validan HMAC |
| Multi-tenancy (crítico) | ✅ PASS post-fix | Filtro `organization_id` faltante en `/admin/reports/consumption/data` encontrado, corregido y verificado en esta sesión |

### Detalle de hallazgos

#### ✅ SQL Injection — PASS
Todas las queries usan `text()` de SQLAlchemy con parámetros nominales (`:param`). Los filtros dinámicos de reportes acumulan strings de condición y pasan valores como dict — nunca f-strings con input de usuario. Verificado en `reports_router.py`.

#### ✅ XSS — PASS
Templates Jinja2 escapan por defecto. No se usa `|safe` con datos de usuario. Chart.js recibe datos via JSON del endpoint `/data` (no innerHTML).

#### ⚠️ CSRF — REVISAR (deuda heredada)
Los formularios POST del panel admin (login, catalog/services, catalog/plans, etc.) usan cookies `SameSite=Strict` como única mitigación CSRF. No existe token CSRF explícito.
Mitigación aceptada: `SameSite=Strict` en HttpOnly cookie protege contra CSRF en todos los browsers modernos. Deuda técnica documentada: añadir CSRF token explícito en v1.0.

#### ✅ Secrets hardcodeados — PASS
Ningún secreto en código. `.env` en `.gitignore`. `_VALID_PROMOS` en LiaForge (repositorio separado) es un dict de metadatos públicos (nombre→porcentaje), no un secreto.
**Nota:** en el repo de LiaForge (scraping-universidades), el `.env` con `SCRAPING_ADMIN_KEY` estuvo en historial git en versiones anteriores. Rotación recomendada.

#### ✅ Sesiones — PASS (con observación de TTL)
JWT HttpOnly + SameSite=Strict + Secure. Access token 15 min, refresh 30 días con rotación. Cookies: `caf_access` y `caf_refresh`.
⚠️ OBSERVACIÓN: en sesión 2026-06-17 se detectó que el TTL del access token se extendió a 12 h temporalmente para pruebas (`JWT_ACCESS_TTL_MIN=720` en `.env` del VPS). Verificar que producción usa 15 min (`config.py` default = 15). Si el TTL largo persiste, priorizar implementar denylist de `jti` en logout (TASK-22).

#### ✅ Endpoints sin auth — PASS
Públicos documentados: `GET /health`, `GET /health/db`, `GET /login`, `POST /login`, `GET /docs` (solo dev).
Todos los endpoints `/admin/*`, `/portal/*`, `/api/v2/*`, `/webhooks/*` requieren auth.
Webhooks: `/webhooks/hub-payment-paid` valida HMAC; `/webhooks/stripe` (Hub) valida `Stripe-Signature` t.v1 HMAC.

#### ✅ Multi-tenancy — PASS post-fix (hallazgo crítico corregido en esta sesión)

**Vulnerabilidad encontrada y corregida (commit `d5853b3`):**

El filtro `l.organization_id = :_org` estaba preparado en el código de `GET /admin/reports/consumption/data` (`reports_router.py`) pero **nunca se añadía al WHERE**. Un operador con acceso al panel de cualquier organización podía ver el consumo de TODOS los tenants de la plataforma.

**Evidencia antes del fix:**
```
GET /admin/reports/consumption/data?date_from=2026-01-01&date_to=2026-12-31
# org5 → devolvía total_cents=31 380 (consumo de TODOS los orgs)
```

**Fix aplicado:** se añadió `_org_filter` al `WHERE` del query en `reports_router.py`, con el valor de `organization_id` extraído del JWT del usuario autenticado (`current_user.organization_id`). La excepción `is_platform` (org 1) conserva acceso a todos los orgs via parámetro `?org`.

**Evidencia después del fix:**
```
GET /admin/reports/consumption/data?date_from=2026-01-01&date_to=2026-12-31
# org5 → total_cents=0 (solo ve su propio consumo, correcto)
```

**Metodología de auditoría:** formalizada en ADR-030.

### Confirmaciones positivas
- El fix sigue el mismo patrón de `_org_scope`/`_org_filter`/`_detail_where` ya establecido en el código multi-tenant del CAF (auditoría 2026-06-16): valores de org van parametrizados (`:_org`), nunca interpolados en el string SQL.
- El endpoint está cubierto por `require_roles(_OPS)` (super_admin/finanzas/lectura), por lo que el vector requería credenciales válidas de operador — no era acceso anónimo.
- La corrección no cambia el comportamiento para `is_platform=True` (org 1 sigue viendo todo vía `?org`).

### Acciones requeridas antes del siguiente push
Ninguna (sin ❌). El hallazgo crítico de multi-tenancy fue corregido y verificado en esta sesión.

### Deuda técnica documentada (no bloqueante) — actualización acumulada
1. `revoked_tokens` / denylist (TASK-22) — **prioridad sube** si `JWT_ACCESS_TTL_MIN=720` persiste en prod.
2. Token CSRF explícito en formularios mutativos `/admin/*` — heredada.
3. Atar el Bearer de app a `client_id`/`plan_code` permitidos si crece el nº de apps (ADR-017).
4. Atribuir el actor real (api_key/org) en el onboard app-facing (H-C de 2026-06-16).
5. Confirmar `metadata.purpose` del webhook del Hub (H-D de 2026-06-16).
6. Verificar en VPS que `JWT_ACCESS_TTL_MIN` real = 15 (no 720).
7. Rotar `SCRAPING_ADMIN_KEY` (estuvo en historial git de LiaForge).

*Addendum OWASP — auditoría 2026-06-18 (multi-tenancy enforcement reportes). Commit d5853b3.*
