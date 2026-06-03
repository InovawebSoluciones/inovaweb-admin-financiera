# Changelog

Formato basado en [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/).
Las versiones siguen [SemVer](https://semver.org/lang/es/).

Orden cronológico inverso: lo más reciente primero.

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
