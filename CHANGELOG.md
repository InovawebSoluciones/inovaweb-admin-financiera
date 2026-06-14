# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/).
Las versiones siguen [SemVer](https://semver.org/lang/es/).

Orden cronológico inverso: lo más reciente primero.

---

## [0.6.1] — 2026-06-14 — Reconciliación VPS↔GitHub + push habilitado + documentación formal

> Sin cambios de código de negocio. Higiene de repositorio + documentación (skill `inovaweb-documentacion`).

### Corregido
- **Drift VPS↔GitHub reconciliado.** El repo del VPS (fuente de verdad = lo que corre) estaba
  divergente con `origin/main`: el VPS adelante con todo el backend saldo-B (jun 9–11) y GitHub solo
  con 2 commits de docs viejos + 1 rebrand duplicado (`147042f` ≡ VPS `42c6b06`, mismo trabajo en 2 clones).
  Reconciliado con `git merge -X ours origin/main` (el VPS gana en conflictos; se conservan los 2 commits
  de docs). Verificado: endpoints saldo-B intactos, la app importa (51 rutas), `/health` ok, `local == origin`
  (`af0e078`). Tag de respaldo `backup/pre-reconcile`.
- **Push a GitHub habilitado** (pendiente histórico "pushear CAF"). El remoto venía HTTPS sin
  credenciales; resuelto con alias SSH `github-caf` → `/root/.ssh/id_ed25519` (cuenta InovawebSoluciones).

### Agregado
- `GET /api/v2/clients/{id}/ledger` + `GET /api/v2/services` commiteados (estaban en el working tree
  del VPS sin commitear; commit `d66442f`).

---

## [0.6.0] — 2026-06-11 — Saldo prepago NATIVO del CAF + cobro pay-per-use + onboarding app-facing

> Sesiones 2026-06-09 a 2026-06-11. El saldo prepago deja de vivir en el Medidor y pasa a ser
> **nativo del CAF** (`prepaid_ledger` + `v_client_balance`); el Medidor queda como medidor puro (solo mide).
> Habilita apps consumidoras self-service (LiaForge/Scraping y Swigg) que cobran por consumo vía Bearer.
> Ver ADR-015/016/017.

### Agregado
- `migrations/030_prepaid_ledger.sql` — libro prepago nativo del CAF: `prepaid_ledger`
  (append-only; `kind` credit/debit, `service_code`, `units`, `source`, `idempotency_key` UNIQUE por
  cliente) + vista de saldo `v_client_balance`. El saldo monetario ya NO se lee del Medidor.
- `POST /api/v2/clients/{id}/charge` — cobro pay-per-use: tarifica por `services.unit_price_cents`,
  valida `v_client_balance`, debita `prepaid_ledger`; **402 `saldo_insuficiente`** (`{balance_cents,
  required_cents}`) si no alcanza. Idempotente por `(client_id, idempotency_key)` con replay;
  `pg_advisory_xact_lock(client_id)` serializa cobros concurrentes (anti doble-gasto).
- `GET /api/v2/clients/{id}/prepaid-balance` — saldo prepago nativo del CAF (app-facing, Bearer).
- `GET /api/v2/clients/{id}/ledger` — movimientos del `prepaid_ledger` + consumo del mes (app-facing).
- `GET /api/v2/services` — catálogo de servicios activos con precio unitario (app-facing).
- `GET /api/v2/clients/{id}/plan-limits` — límites del plan + precios del catálogo (solo lectura;
  lo consume Scraping/LiaForge para medir uso vs. tope SIN cobrar).
- `POST /api/v2/apps/onboard` — alta app-facing self-service (Bearer, sin JWT ni datos fiscales):
  crea cliente + wallet Medidor + suscripción + grant inicial del plan al `prepaid_ledger`.
  Datos fiscales placeholder (se completan al facturar).
- Catálogo sembrado: `plans` (`liaforge_free/starter/growth/scale`, `swigg_free/starter/pro/enterprise`)
  y `services` (email 100¢, descubrimiento 35¢, descubrimiento_local 99¢, validacion_email 18¢,
  validacion_pagina 50¢, validacion_dns 5¢, geocoding 5¢, scraping 10¢, agente_corrida 10¢,
  guion_ia 500¢, video/envio/vista_video 1000¢).

### Cambiado
- `app/services/prepago.py` — la recarga acredita el `prepaid_ledger` del CAF (dual-write con el
  Medidor durante la transición, idempotente por `req_id`). El saldo prepago ya vive en el CAF.
- `app/core/config.py` — `SWIGG_ADMIN_KEY` (opcional): segunda app consumidora con Bearer dedicado.
- `app/routers/api_router.py` — `_verify_app_key` acepta `SCRAPING_ADMIN_KEY` (LiaForge) y
  `SWIGG_ADMIN_KEY`. Los endpoints app-facing se autentican por **Bearer**, no por JWT.

### Seguridad
- `pg_advisory_xact_lock(client_id)` en `/charge` evita doble-gasto concurrente.
- Llave dedicada por app (`SWIGG_ADMIN_KEY` separada de `SCRAPING_ADMIN_KEY`): cierra 2 WARN OWASP
  del flujo de cobro (concurrencia + separación de credenciales por consumidor).

---

## [0.5.0] — 2026-06-07 — Grupo 3: billing consumo + onboarding completo + hardening + frontend

> Sesión v2 + v3 (2026-06-07). Implementación ejecutada por Claude Code en paralelo (4 tareas).
> Estado verificado E2E: flujo de pago completo, saldo Medidor $500, asiento Finanzas, idempotente.
> **Fuente de verdad**: CLAUDE.md §12 (docs formales generados en traslada 2026-06-07).

### Agregado
- `app/core/clients/scraping_client.py` — cliente al core de Scraping para `POST /companies/{id}/link-caf`. Auth: Bearer admin. Cableado en saga de onboarding.
- `database/005_activation_tokens.sql` — tabla `activation_tokens(id, user_id, token_hash UNIQUE, expires_at, used_at)` para activación de cuenta vía email (SHA-256, 24 h, single-use).
- `database/006_idempotencia.sql` — índice parcial UNIQUE en `clients(request_id)` para idempotencia del onboarding atómico.
- `database/007_price_catalog.sql` — catálogo de precios público: `price_catalog(id, meter, unit_code, amount_cents, valid_from, valid_to)`. Incluye entradas semilla para IA/token, email, whatsapp, sms.
- `app/services/pricing.py` — función `price_quantity(meter, unit_code, quantity)` que lee `price_catalog` y devuelve el cargo a precio público (no costo crudo Medidor).
- `app/core/clients/messages_client.get_usage_by_channel()` — nuevo método que llama `GET /v1/reports/usage?group_by=channel,client` y devuelve `{canal: cantidad}`. Usado por billing para facturar por canal.
- Frontend Jinja2 + HTMX: templates base, admin/dashboard, admin/clients, portal/dashboard, portal/invoices.
- `docs/MODELO-COBRO.md` — descripción del modelo de tarificación: precio público vs. costo COGS.

### Cambiado
- `app/services/onboarding.py` — saga extendida con paso 2b (link-caf en Scraping con compensación) y paso 5b (token SHA-256 + email de activación con variables `nombre`, `token_url`, `expiracion_horas`). Campo nuevo `scraping_company_id: int | None`.
- `app/services/billing.py` — secciones 2b (consumo IA vía `medidor.get_usage_summary()` → `price_quantity()`) y 2c (mensajes por canal vía `messages.get_usage_by_channel()` → `price_quantity()`). Asiento Finanzas best-effort en `_close_one_subscription`.
- `app/core/clients/messages_client.py` — `get_usage()` corregido: ruta era `/v1/usage` (inexistente), ahora `/v1/reports/usage?group_by=client` (verificado contra fuente del Centro de Mensajes). Docstrings completos.
- `app/core/config.py` — nuevas vars: `SCRAPING_BASE_URL`, `SCRAPING_ADMIN_KEY`, `HUB_WEBHOOK_SECRET`, `MAX_RECARGA_CENTS` (tope de recarga; default 500,000 MXN en centavos).
- `.env.example` — añadidas todas las vars nuevas con placeholders.

### Corregido
- **D1**: `scraping_client.py` enviaba `caf_client_id` como `str`; Scraping esperaba `int` (BIGINT). Corregido en cliente CAF y en el modelo + router de Scraping (con migración `alembic 0005_caf_client_id_bigint`).
- **C1** (sesión anterior): `medidor_client.py` rutas de `credit` y `suspend_wallet` verificadas contra fuente real del Medidor y corregidas. Pytest 3/3 PASSED en VPS.
- Template `caf-activacion-correo`: variables ajustadas de `{{nombre}}` (doble llave, incorrecto) a `{nombre}`, `{token_url}`, `{expiracion_horas}` (llave simple, contrato real del Centro de Mensajes).
- Hallazgo contradicción doc: `suspend_wallet` usa `/admin/v1/wallets/{id}/suspend` (correcto, verificado en fuente Medidor).

### Hardening H1-H5
- **H1**: Idempotencia de onboarding por `request_id` (índice UNIQUE + check antes del INSERT).
- **H2**: Retry con backoff exponencial para llamadas a cores (en `_base.CoreClient`).
- **H3**: Fail-closed en prod — si `ENV=production`, falla abierta en onboarding lanza excepción; en dev/staging degrada graceful.
- **H4**: Tope de recarga configurable `MAX_RECARGA_CENTS` (default 50,000,000 = $500,000 MXN). Rechaza con 422 si se excede.
- **H5**: Webhook de pago filtra por `client_id` del JWT — no puede acreditar saldo de otro cliente.

---

## [Sin versión] — 2026-06-06 — Auditoría global + documentación

> Sesión de auditoría global de la plataforma (6 proyectos). No modifica código.

### Agregado
- `docs/ARQUITECTURA-GLOBAL.md` — referencia de toda la plataforma: mapa de
  llamadas verificado, mapa de identidad, flujos end-to-end y discrepancias
  diseño↔implementación.
- `docs/OWASP.md` — auditoría OWASP del CAF (PASS con observaciones).
- `docs/GUIA-DESARROLLADOR.md`, `docs/GUIA-USUARIO-OPERADOR.md`,
  `docs/GUIA-USUARIO-CLIENTE.md`, `docs/RESUMEN-EJECUTIVO.md`.

### Hallazgos
- 🔴 **CRÍTICO (C1):** `app/core/clients/medidor_client.py:78,96` acredita/borra en
  `/admin/v1/wallets/{id}/...`; el Medidor expone credit en `/v1/wallets/{id}/credit`
  y no tiene esas rutas `/admin/v1`. Toda recarga/onboarding daría 404. **Bloquea el
  commit del CAF** hasta corregir y re-verificar QA. No corregido en esta sesión
  (regla: no modificar código sin autorización). Detalle en `docs/OWASP.md §0`.
- ⚠️ WhatsApp del Centro de Mensajes responde 501; plantillas `caf-*` sin sembrar;
  onboarding sin idempotencia por `request_id`; `revoked_tokens` no implementada.

---

## [0.3.0] — 2026-06-04 — Flujo prepago end-to-end (Hub → wallet) + idempotencia + hardening

> ⚠️ **Nada de esta entrada está commiteado todavía.** Los cambios viven en el
> working tree de tres repos (CAF, Medidor, Scraping). Esta entrada documenta el
> estado real del árbol del CAF para que otro chat pueda retomar (ver
> `docs/HANDOFF-SESION.md`). El comando de commit se entrega al final de la
> sesión de documentación.

### Agregado
- `app/services/prepago.py` — servicio del flujo prepago del piloto Scraping:
  - `initiate_charge(...)` abre el cargo en el Hub-Pasarelas (Conekta sandbox)
    para `purpose ∈ {plan_purchase, wallet_recharge}`, deja
    `recharge.initiated` en `audit_log` con `recharge_id` + `purpose` +
    `amount_cents` (correlación posterior con el webhook).
  - `extract_event(payload)` parseo robusto del evento `payment.paid`
    (FIX-7: `amount` mal formado → `PrepagoError`, no 500 sin controlar).
  - `process_paid_event(...)` acredita la wallet del cliente en el Medidor
    (`credit`, idempotente por `request_id = caf-recharge-{recharge_id}`),
    postea el asiento en Finanzas (`source_ref = caf-recharge-{recharge_id}`)
    y dispara correo de confirmación vía Centro de Mensajes.
- `app/routers/webhooks_router.py` — `POST /webhooks/hub-payment-paid`:
  verifica HMAC tiempo-constante sobre el body, valida timestamp firmado
  (ventana anti-replay), y delega a `process_paid_event`. Validación antes de
  cualquier I/O.
- `database/004_payments_idempotency.sql` — índice ÚNICO PARCIAL
  `uq_payments_hub ON payments(hub_payment_id) WHERE hub_payment_id IS NOT NULL`.
  Es un índice (no muta filas) → no viola el append-only de `002`.
- `database/003_seed_scraping_plans.sql` — seed del catálogo de planes del
  piloto Scraping en centavos: free `10000`, básico `9900`, medio `20000`,
  premium `40000`.
- Config (`app/core/config.py`): `HUB_WEBHOOK_SECRET` (obligatorio en prod via
  validator, FIX-3), `HUB_WEBHOOK_TOLERANCE_SEC`, `MAX_RECARGA_CENTS` (FIX-6),
  `CAF_PAGO_CONFIRMADO_TEMPLATE`, `CAF_MESSAGES_SERVICE_ID`.
- `app/routers/portal_router.py`: `POST /portal/recharge` y compra de plan que
  abren el flujo en el Hub.
- Tests: `tests/test_hub_webhook.py` (replay/concurrencia, firma, purpose/amount).

### Corregido / Endurecido (correcciones QA TASK-15b)
- **FIX-1 — Idempotencia a nivel BD.** El no-duplicado del pago vive en la BD
  (`INSERT ... ON CONFLICT (hub_payment_id) DO NOTHING`), no en un SELECT
  previo. Dos webhooks concurrentes/repetidos con el mismo `hub_transaction_id`
  acreditan y notifican una sola vez (`status='duplicate_ignored'`, 200).
- **FIX-2 — Correlación purpose/amount contra el intento local.** Antes de
  acreditar, el webhook se cruza por `recharge_id` con el `recharge.initiated`
  previo y valida `purpose` + `amount_cents`. Si no hay intento o no coincide →
  rechazo sin acreditar + audit `hub.paid.rejected`.
- **FIX-3 / FIX-4** — `HUB_WEBHOOK_SECRET` obligatorio en prod (no fallback a
  `HUB_API_KEY`); timestamp firmado exigido en prod.
- **FIX-5** — el portal ya no propaga errores crudos del core al cliente
  (mensaje genérico; detalle solo server-side).
- **FIX-6** — tope de monto de recarga autoservicio (`5000` ≤ monto ≤
  `MAX_RECARGA_CENTS`).
- **FIX-7** — parseo defensivo de `amount` en `extract_event`.

### Estado / pendiente de verificación
- QA había **rechazado** la primera versión de TASK-15 por faltar FIX-1
  (UNIQUE parcial) y FIX-2 (validar purpose/amount). El árbol actual **ya
  contiene** FIX-1…FIX-7. **Falta re-correr la verificación de QA** (compileall,
  001+002+003+004 sobre Postgres limpio, `pytest`) sobre este árbol antes de
  declarar #15b cerrado. Ver `docs/HANDOFF-SESION.md`.
- Specs escritos **sin ejecutar** o pendientes de cierre formal: #15b
  (correcciones prepago — verificar), #8 (CRUD/API `/api/v2`), #16 (onboarding
  crea wallet + liga Scraping + activación email/WhatsApp).

### Integración cross-repo (no en este repo)
- Medidor (Nivel 1): script `vps/04` para emitir la API key con scope `ADMIN`
  (label `core-admin-financiera`) que el CAF necesita en `.env`.
- Scraping (Nivel 3): `medidor_client.py` + `authorize`/`finish` en
  `semantic_search`; migración `0004` con `Company.medidor_wallet_id` y
  `search_sessions.medidor_hold_id` / `medidor_status`.

---

## [0.2.1] — 2026-06-03 — Contrato del Medidor al API real + ADR del modelo prepago

### Cambiado
- `docs/01-admin-financiera-integracion-cores.md` §3 (Integración con Medidor
  IA): reescrito al **API real** del Medidor (confirmado leyendo su código).
  Documenta la wallet prepago autoritativa con identidad
  `(tenant_id, external_user_id)` UNIQUE; los endpoints `ADMIN` que invoca el
  CAF (`POST /v1/wallets`, `POST /v1/wallets/{id}/credit` idempotente por
  `request_id`, `GET /v1/wallets/{id}/balance` con `balance_cents`/`holds_total`/
  `disponible_cents`, `GET /v1/usage`, suspend/unsuspend); los endpoints
  `CLIENT` del consumidor como referencia (`operations/authorize` que crea HOLD
  y valida saldo, `finish`, `release`, `quote`, `events/track`, `events/refund`);
  scopes `ADMIN`/`CLIENT`; ledger append-only `wallet_transactions` con balance
  materializado y locking optimista. Aclara que **el bloqueo por saldo
  insuficiente lo impone `authorize`** y que el CAF nunca debita.

### Agregado
- `docs/ADR.md` ADR-011: **El Medidor core es la wallet prepago autoritativa
  (authorize/finish/credit) y mapeo de identidad del piloto Scraping.**
  Registra el reparto por scope (CAF=ADMIN crea+recarga, Scraping=CLIENT
  authorize→finish) y el mapeo `CAF clients.id ↔ Company.caf_client_id ↔
  Company.id (=company_id) ↔ wallet external_user_id` bajo tenant `inovaweb`.
  Scraping no tenía integración con el Medidor; se wirea en TASK-21.

### Renumeración
- Los placeholders previos ADR-011/012/013 (PAC concreto, backups, 2FA) se
  renumeran a ADR-012/013/014.

### Sin cambios de código
Versión documental. No introduce migraciones SQL ni cambios de contrato HTTP.
No toca `app/` ni `tests/`.

---

## [0.2.0] — 2026-06-03 — Clientes al contrato real + onboarding PREPAGO

### Cambiado
- `app/core/clients/finanzas_client.py`: reescrito al contrato real del
  Finanzas-Core (`docs/01-admin-financiera-integracion-cores.md` §5). El
  `tenant_id` se resuelve desde la API key admin master; la vista por cliente
  se filtra por `external_user_id` en `meta`. Métodos: `get_balance`,
  `get_totals`, `list_entries`, `post_entry` (con `source_ref`
  determinístico documentado en docstring). **Se eliminaron** los métodos
  inventados `create_account`, `delete_account`, `issue_api_key` y los
  `post_charge`/`post_credit` viejos: el CAF jamás crea cuentas ni emite
  llaves en el Finanzas-Core.
- `app/core/clients/messages_client.py`: reescrito al contrato real del
  Centro de Mensajes (§6). `send_email` con `origin_kind="template"` y
  `from` por defecto `facturacion@inovaweb.com.mx`; `send_whatsapp` análogo
  (endpoint marcado con `# TODO confirmar` por estar fuera del contrato
  documentado). **Se eliminaron** `create_account`, `delete_account`,
  `issue_api_key` y `send_sms` (SMS fuera del piloto).
- `app/services/onboarding.py`: la saga de alta pasa a modelo **PREPAGO**.
  El alta solo toca el **Medidor** para crear la wallet del cliente
  (`create_wallet(external_user_id="client-<id>", ...)`) y guarda su `id` en
  `clients.medidor_account_id`; el resto de cores son multi-tenant resueltos
  por la llave (hub se configura por SQL). Ya no se crean cuentas ni se
  emiten 4 API keys por cliente. `OnboardResult.api_keys` se reemplaza por
  `wallet_id`. La compensación ante fallo posterior a la wallet sigue siendo
  `delete_wallet` (best-effort) + rollback local.

### Corregido
- **Auditoría de fallo de onboarding ahora sí persiste.** En las ramas de
  fallo, el evento `onboard_failed` se escribía después del `rollback()` y
  era descartado por el rollback final de `get_db`, dejando la falla sin
  rastro en `audit_log`. Ahora se persiste en una transacción independiente
  con commit explícito. El password temporal sigue sin escribirse en el
  audit. Cubierto por nuevo test del saga en `tests/test_onboarding.py`
  (path de fallo tras crear la wallet: dispara compensación + persiste el
  audit).
- `app/services/billing.py`: corregido el llamador de
  `MedidorClient.get_usage`, ahora keyword-only
  (`get_usage(wallet_id, *, from_ts, to_ts)`). Se actualizó la llamada en el
  cierre mensual a la firma nueva.
- `app/core/clients/messages_client.py`: eliminados bytes NUL finales que
  rompían `compileall` en un clone limpio o build Docker.

### Notas de alcance
- El reordenamiento del disparo del alta (que ocurra **después** del pago
  confirmado) pertenece a las tareas de piloto #15/#16; aquí `onboard_client`
  sigue siendo una función invocable.
- La facturación mensual + CFDI 4.0 vía PAC queda **diferida** para el
  piloto Scraping (ver `docs/ADR.md` ADR-010). El modelo del piloto es
  prepago por recarga de wallet.

---

## [0.1.1] — 2026-06-03 — Clarificación del rol del medidor IA

### Cambiado
- `README.md` §1.1: la descripción del medidor IA ahora explicita que
  además de mantener wallets prepago, **mide cada llamada a LLM y cobra
  el costo en pesos mexicanos (centavos enteros MXN)** — es la fuente
  única del costo de IA por cliente en toda la plataforma Inovaweb.
- `README.md` §6.4: el endpoint `GET /api/v2/clients/{id}/balance` deja
  claro que el CAF jamás duplica saldo ni recalcula costo de IA; el
  medidor es siempre la fuente de verdad.
- `docs/DEPLOY.md` §1: pre-requisitos identifican por nombre los 4 cores
  (medidor IA, hub-pasarelas, finanzas-core, centro-mensajes) y aclaran
  que sin la API key del medidor el CAF no puede mostrar saldo ni consumo.

### Agregado
- `docs/ADR.md` ADR-009: **El medidor IA es la fuente única del costo de
  consumo de IA por cliente.** Documenta por qué el CAF no recalcula
  tokens → pesos y por qué `medidor_client.py` solo expone lectura
  (`get_balance`, `get_usage_summary`, `get_usage_events`) más
  acreditación por recargas confirmadas — nunca cálculo de tarifa.
- `docs/RUNBOOK.md` §4.3: procedimiento "cargo de IA en la factura del
  cliente parece equivocado" — cómo distinguir si la discrepancia está
  en el agregado del CAF (sobre eventos del finanzas-core) o en el
  medidor (tarifa / tokens reportados), y dónde corregir cada caso.

### Renumeración
- Los placeholders previos ADR-009/010/011 (PAC concreto, backups, 2FA)
  se renumeran a ADR-010/011/012.

### Sin cambios de código
Esta versión es documental. No introduce migraciones SQL ni cambios de
contrato HTTP.

---

## [0.1.0] — 2026-06-03 — Cierre Sprint 1: scaffolding + documentación formal

### Agregado
- Documentación formal para arranque del repositorio en GitHub:
  - `README.md` con arquitectura, stack, variables de entorno, cómo correr
    localmente, URLs de producción y endpoints principales.
  - `docs/ADR.md` con 8 decisiones de arquitectura del sprint 1 (un solo
    backend para dos dominios, saga de onboarding, auditoría en triggers,
    JWT con rotación, PAC adapter, workers como contenedores, catálogos
    editables, colas en Postgres).
  - `docs/RUNBOOK.md` con diagnóstico y mitigación por componente
    (backend, Postgres, workers, integración con cores, webhooks, auth,
    seguridad).
  - `docs/DEPLOY.md` con bootstrap, deploys incrementales, migraciones
    SQL vía PowerShell, rollback y checklist de pre-deploy.

### Sin cambios funcionales en código
Este cierre formaliza la documentación de lo que ya existía. No introduce
nuevas features, fixes ni cambios de contrato.

---

## [0.0.1] — 2026-05-26 — Sprint 1: scaffolding del proyecto

### Agregado
- Estructura base del proyecto con FastAPI + SQLAlchemy 2 async + psycopg 3
  y Postgres 16.
- Configuración centralizada con `pydantic-settings` y fail-fast en
  variables obligatorias (`app/core/config.py`).
- Middleware `HostEnforcementMiddleware` que enruta `admin.inovaweb.com.mx`
  y `app.inovaweb.com.mx` al mismo backend con bloqueo cross-domain.
- Middleware `RequestContextMiddleware` + logging JSON estructurado con
  `request_id` (`app/core/observability.py`).
- Auth JWT con cookies httpOnly, SameSite=Strict, access 15 min + refresh
  30 días con rotación (`app/core/jwt_auth.py`). Hash Argon2id
  (`app/core/password.py`).
- Writer de audit log inmutable (`app/core/audit.py`).
- Clientes HTTP async (`httpx`) para los 4 cores Nivel 1 y para el PAC:
  - `medidor_client.py`, `hub_client.py`, `finanzas_client.py`,
    `messages_client.py`, `pac_client.py`.
  - Clase base `_base.py` con timeout, reintentos y manejo de errores.
- Routers:
  - `health_router.py` (`/health`, `/health/db`).
  - `auth_router.py` (`/login`, `/logout`, `/signup-request`).
  - `admin_router.py` (`/admin/*` UI HTMX operador).
  - `portal_router.py` (`/portal/*` UI HTMX cliente).
  - `api_router.py` (`/api/v2/*` JSON).
  - `webhooks_router.py` (`/webhooks/pac`, `/webhooks/hub-payment-paid`).
- Servicios:
  - `onboarding.py` — Saga atómica con compensación cross-core.
  - `billing.py` — cálculo de cierre mensual con planes + promociones.
  - `invoicing.py` — emisión de factura y timbrado vía PAC.
  - `promotions.py` — aplicación de cupones, descuentos por temporada,
    descuentos por volumen.
- Workers:
  - `monthly_closing.py` — job nocturno del día 1 de cada mes.
  - `invoice_retry.py` — reintento de timbrado con backoff.
  - `overdue_notifier.py` — recordatorios pre, en y post vencimiento.
- Templates Jinja2 + HTMX para UI admin y portal cliente
  (`app/templates/admin/`, `app/templates/portal/`, `_layout.html`,
  `auth/`).
- Schema SQL inicial (`database/001_initial_schema.sql`, 308 líneas):
  - Roles, usuarios, clientes, productos, servicios, planes, suscripciones,
    promociones, facturas, pagos, ajustes, audit log.
  - Dinero en BIGINT centavos. Timestamps con TZ.
- Restricciones de seguridad SQL (`database/002_security_constraints.sql`,
  165 líneas):
  - Triggers PL/pgSQL append-only para `audit_log`, `payments`, `invoices`,
    `adjustments`.
  - Triggers de auditoría automática (AFTER INSERT/UPDATE/DELETE) con diff
    `to_jsonb`.
- Suite de tests con `pytest` + `pytest-asyncio`:
  - `test_health.py`, `test_jwt.py`, `test_password.py`,
    `test_onboarding.py`, `test_promotions.py`.
- Docker Compose multi-servicio:
  - `postgres` con healthcheck e init scripts montados.
  - `admin_financiera` (backend FastAPI, puerto host 8006 → contenedor
    8001).
  - `monthly_closing`, `invoice_retry`, `overdue_notifier` con
    `profiles: ["jobs"]` y `restart: "no"`.
  - Volumen `caf_invoices` para PDFs/XML.
  - Red `caf_net` interna + conexión a `n8n_default` para Caddy.
- `Dockerfile` con Python 3.12 slim, healthcheck y entrypoint uvicorn con
  `--proxy-headers`.
- `Caddyfile` de referencia para los dos dominios públicos con HSTS,
  CSP y logs JSON.
- `.env.example` completo con todas las variables documentadas.
- Documentos técnicos del proyecto en `docs/`:
  - `inovaweb-admin-financiera-proyecto-tecnico.md` (documento marco para
    dirección/stakeholders).
  - `01-admin-financiera-integracion-cores.md` (contrato con los 4 cores
    Nivel 1).
  - `prompt-arranque-cowork.md` (instrucciones de arranque del workspace).
- `CLAUDE.md` con convenciones para el agente.
- `SECURITY.md` con modelo de amenazas y controles aplicados.
- `.gitignore` estándar Python + `.env` + `secrets/`.

### Seguridad
- Auditoría inmutable enforced por base de datos, no por aplicación
  (triggers PL/pgSQL).
- Append-only en tablas financieras: `payments` y `audit_log` con UPDATE y
  DELETE bloqueados; `invoices` y `adjustments` con DELETE bloqueado y
  UPDATE solo a lista blanca de campos no financieros.
- Passwords con Argon2id, nunca en plaintext.
- Sesiones JWT en cookies httpOnly + SameSite=Strict; rotación de refresh
  con detección de reuse → invalidación de cadena completa.
- Secretos del PAC (CSD `.cer` + `.key` + password) montados como volumen
  read-only `/secrets`.
- AES-256-GCM disponible para cifrado de campos sensibles vía
  `cryptography`.

### Notas
- Sin git inicializado en este árbol al momento del cierre (se inicializa
  con el commit que documenta este `CHANGELOG`).
- Sprint 2 (siguiente) cubrirá: integración real con cores Nivel 1
  (mock-out actual), UI HTMX funcional, primer cliente piloto.
- ADR-009 (selección de PAC concreto), ADR-010 (backups y RPO/RTO) y
  ADR-011 (2FA para super-admin) quedan diferidos.
