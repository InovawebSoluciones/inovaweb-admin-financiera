# GRUPO 3 — Ejecución paralela (2026-06-06)

Ejecuta las 4 tareas siguientes EN PARALELO usando subagentes (Task tool).
Cada subagente trabaja de forma independiente. No esperes que uno termine para arrancar el siguiente.

---

## CONTEXTO GLOBAL

**Proyecto:** inovaweb-admin-financiera (CAF) — Centro de Administración Financiera, Nivel 2.
**Stack:** Python 3.12 + FastAPI + SQLAlchemy 2 async + Jinja2 + HTMX + Tailwind CSS.
**Repo CAF:** `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera\`
**Repo Scraping:** `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\scraping_comercial\scraping-universidades\`
**GitHub Scraping:** `https://github.com/InovawebSoluciones/scraping-inovaweb`

Reglas globales (aplican a todas las tareas):
- Centavos BIGINT, nunca floats.
- Auditoría obligatoria en toda operación de escritura.
- Type hints + docstrings en toda función pública.
- NO hacer commit, NO hacer push, NO levantar Docker.
- Verificar con `py_compile` / `pytest` al terminar cada tarea.
- Reportar archivos creados/modificados y salida de verificaciones.

---

## TAREA A — #16 Onboarding: wallet + ligar Scraping + activación

### Objetivo
Extender el onboarding del CAF para: (1) ligar la wallet creada en el Medidor con la Company de Scraping, (2) emitir token de activación de un solo uso y enviarlo por correo vía Centro de Mensajes.

### Archivos a leer primero
- `app/services/onboarding.py`
- `app/core/clients/medidor_client.py`
- `app/core/clients/_base.py`
- `app/core/clients/messages_client.py`
- `app/core/config.py`
- `tests/test_onboarding.py`
- En Scraping: explorar modelos `Company` y rutas existentes antes de tocar nada.

### Qué implementar

**CAF — `app/core/clients/scraping_client.py` (nuevo):**
- Cliente HTTP async a Scraping usando el mismo patrón de `_base.py`.
- Método `link_caf(company_id, caf_client_id, medidor_wallet_id)` → `POST /companies/{company_id}/link-caf`.
- Leer `SCRAPING_BASE_URL` y `SCRAPING_ADMIN_KEY` de config (agregarlos si no existen).

**CAF — `app/services/onboarding.py` — extender:**
- `OnboardClientPayload`: agregar campo `scraping_company_id: int | None = None`.
- Paso 2b (después de crear wallet): si `scraping_company_id` no es None, llamar `scraping_client.link_caf(...)`. Si falla → compensar (suspend wallet) + audit_log.
- Paso 5b: generar token de activación (32 bytes hex, hash SHA-256 almacenado en BD, expiración 24h). INSERT en `activation_tokens`. Llamar `messages_client.send_email('caf-activacion-correo', titular_email, {token_url: ...})`. Fallo de mensajería → loguear, NO abortar el onboarding.

**CAF — `database/003_activation_tokens.sql` (nuevo):**
```sql
CREATE TABLE IF NOT EXISTS activation_tokens (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id),
    token_hash  TEXT NOT NULL UNIQUE,
    expires_at  TIMESTAMPTZ NOT NULL DEFAULT now() + interval '24 hours',
    used_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_activation_tokens_user ON activation_tokens(user_id);
```

**CAF — `app/core/config.py`:** agregar si faltan:
```python
SCRAPING_BASE_URL: str = "https://scraping.inovaweb.com.mx"
SCRAPING_ADMIN_KEY: SecretStr
```

**Scraping — nuevo endpoint `POST /companies/{company_id}/link-caf`:**
- Auth: Bearer admin.
- Body: `{caf_client_id: int, medidor_wallet_id: str}`.
- Persiste ambos campos en la fila `companies` (campos `caf_client_id` y `medidor_wallet_id` ya existen de TASK-21).
- Idempotente: re-ejecutar no falla.

### Verificación
- `py_compile` en todos los archivos CAF tocados.
- Nuevos tests en `tests/test_onboarding.py`: mock scraping_client + mock messages_client, flujo sin scraping_company_id, compensación si falla link-caf.
- `py_compile` en archivos Scraping tocados.

---

## TAREA B — #19/22 Hardening antes de multi-cliente

### Objetivo
Aplicar los 5 puntos de hardening identificados en la auditoría antes de abrir el sistema a múltiples clientes.

### Archivos a leer primero
- `app/services/onboarding.py`
- `app/services/prepago.py`
- `app/routers/api_router.py`
- `app/routers/webhooks_router.py`
- `app/core/config.py`
- `database/001_initial_schema.sql`
- `database/002_security_constraints.sql`

### Qué implementar

**H1 — Idempotencia en BD para onboarding:**
- `onboarding.py:79` acepta `request_id` pero no lo usa.
- Agregar columna `request_id TEXT UNIQUE` a `clients` (migración `004_idempotencia.sql`).
- En `onboard_client`: si `request_id` no es None, hacer upsert o check-before-insert para evitar duplicados.

**H2 — Retry de `finish` en prepago:**
- `prepago.py`: el paso `finish` (descuento de saldo en Medidor) puede fallar por red. Agregar reintentos con backoff exponencial (3 intentos, delays 1s/2s/4s) usando `tenacity` o un loop simple.
- Si los 3 reintentos fallan → marcar la operación como `pending_retry` en BD y loguear alarma.

**H3 — Filtro `company_id` explícito:**
- Revisar todas las queries en `api_router.py` y `portal_router.py` que consulten datos de un cliente. Asegurar que cada query filtra explícitamente por el `client_id` del JWT, no confiar solo en el parámetro de URL.

**H4 — Fail-closed en prod:**
- `config.py`: si `ENV=production` y `HUB_WEBHOOK_SECRET` no está seteado (o es igual a `HUB_API_KEY`), lanzar `ValueError` al arrancar la app — no arrancar en prod sin secreto dedicado.

**H5 — Tope de monto en recargas:**
- `webhooks_router.py` / `prepago.py`: agregar validación de que el monto de recarga no supere `MAX_RECHARGE_AMOUNT_CENTS` (configurable, default 500_000_00 = $500,000 MXN). Rechazar con 400 si supera.

### Verificación
- `py_compile` en todos los archivos tocados.
- Al menos 1 test por punto de hardening en `tests/test_hardening.py` (nuevo archivo).

---

## TAREA C — #3-C Frontend: templates Jinja2 + HTMX (admin + portal)

### Objetivo
Construir todas las plantillas UI server-side del CAF. Sin SPA, sin build Node.

### Archivos a leer primero
- `app/main.py` — cómo están montados los routers
- `app/routers/admin_router.py`
- `app/routers/portal_router.py`
- `app/routers/auth_router.py`
- Explorar `app/templates/` — ver qué existe ya

### Stack de la UI
- **Jinja2** para server-side rendering (FastAPI `Jinja2Templates`)
- **HTMX** para interactividad sin JS pesado (`hx-get`, `hx-post`, `hx-swap`)
- **Tailwind CSS** vía CDN (play.tailwindcss.com para prototipo; compilado después)
- Sin frameworks JS, sin build step

### Qué implementar

**`app/templates/base.html`:**
- Navbar con links a: Dashboard, Clientes, Catálogo, Facturación, Audit Log
- Bloque `{% block content %}` principal
- Incluir Tailwind CSS CDN + HTMX CDN
- Flash messages (errores/éxito)
- Mostrar usuario logueado + botón logout

**`app/templates/auth/login.html`:**
- Formulario email + password
- POST a `/login`
- Mensajes de error inline

**`app/templates/admin/dashboard.html`:**
- 4 tarjetas métricas: ingresos del mes, clientes activos, facturas emitidas, mora
- Tabla de actividad reciente (últimas 10 entradas del audit_log)
- Barras de consumo por core (Medidor, Hub, Finanzas, Mensajes) — datos placeholder por ahora

**`app/templates/admin/clients/list.html`:**
- Tabla de clientes: nombre, RFC, plan, estado, monto/mes
- Filtros por estado y plan (HTMX `hx-get` al cambiar)
- Botón "Alta cliente" → `/admin/clients/new`

**`app/templates/admin/clients/new.html`:**
- Formulario de alta atómica: datos fiscales + plan + titular
- POST a `/admin/clients` con feedback de resultado

**`app/templates/admin/clients/detail.html`:**
- Detalle: datos, suscripción activa, historial de pagos, botones suspender/reactivar

**`app/templates/admin/billing/invoices.html`:**
- Lista de facturas con estado (timbrada / error / pendiente)
- Botones descarga PDF/XML por fila
- Botón "Forzar cierre mensual"

**`app/templates/portal/dashboard.html`:**
- Saldo disponible + consumo del mes + próxima factura
- Barra de progreso del plan

**`app/templates/portal/invoices.html`:**
- Mis facturas con descarga PDF/XML

**`app/templates/portal/recharge.html`:**
- Formulario de recarga: monto + método de pago → POST a `/portal/recharge`

**Routers — completar los endpoints HTML que devuelvan `TemplateResponse`:**
- `admin_router.py`: `GET /admin/dashboard`, `GET /admin/clients`, `GET /admin/clients/new`, `GET /admin/clients/{id}`, `GET /admin/billing/invoices`
- `portal_router.py`: `GET /portal/dashboard`, `GET /portal/invoices`, `GET /portal/recharge`
- `auth_router.py`: `GET /login`

### Verificación
- `py_compile` en todos los archivos `.py` tocados
- Confirmar que todas las plantillas tienen `{% extends "base.html" %}` y bloque `content`

---

## TAREA D — #3-D Integración Scraping: billing por consumo IA + emails

### Objetivo
El CAF debe leer el consumo real de IA (Medidor) y emails (Centro de Mensajes) de cada cliente Scraping al cierre mensual, calcular el importe y agregarlo a la factura.

### Archivos a leer primero
- `app/services/billing.py`
- `app/workers/monthly_closing.py`
- `app/core/clients/medidor_client.py`
- `app/core/clients/messages_client.py`
- `app/core/clients/finanzas_client.py`
- `database/001_initial_schema.sql` — tablas `clients`, `subscriptions`, `invoices`, `invoice_items`
- En Scraping: explorar cómo se registran los consumos de IA y emails por company

### Qué implementar

**`app/core/clients/medidor_client.py` — verificar/agregar:**
- `get_usage(wallet_id, from_ts, to_ts)` ya existe. Confirmar que devuelve total de operaciones y costo en centavos. Agregar si falta.

**`app/core/clients/messages_client.py` — verificar/agregar:**
- `get_usage(external_user_id, from_ts, to_ts)` → `GET /v1/usage?...` — obtener cantidad de mensajes enviados y costo en centavos del periodo. Agregar si el método no existe.

**`app/services/billing.py` — extender cierre mensual:**
- Para cada cliente con `medidor_account_id` (wallet_id), consultar `medidor_client.get_usage(wallet_id, periodo)`.
- Para cada cliente con `messages_account_id`, consultar `messages_client.get_usage(external_user_id, periodo)`.
- Agregar los consumos como `invoice_items` a la factura del periodo:
  - Concepto: "Consumo IA — {N} operaciones"
  - Concepto: "Mensajes enviados — {N} mensajes"
  - Montos en centavos BIGINT.
- Si el cliente no tiene wallet o no tiene mensajes del periodo → omitir ese concepto (no error).

**`app/workers/monthly_closing.py` — verificar integración:**
- Confirmar que el worker llama a `billing.py` correctamente y que el flujo incluye los nuevos conceptos.

**`database/005_invoice_items.sql` (nuevo, si la tabla no existe):**
```sql
CREATE TABLE IF NOT EXISTS invoice_items (
    id           BIGSERIAL PRIMARY KEY,
    invoice_id   BIGINT NOT NULL REFERENCES invoices(id),
    description  TEXT NOT NULL,
    quantity     BIGINT NOT NULL DEFAULT 1,
    unit_price_cents BIGINT NOT NULL,
    total_cents  BIGINT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### Verificación
- `py_compile` en todos los archivos tocados.
- Nuevos tests en `tests/test_billing.py`:
  - Mock medidor_client.get_usage → verifica que el invoice_item se crea con monto correcto.
  - Mock messages_client.get_usage → ídem.
  - Cliente sin wallet → no genera item de IA (no error).
