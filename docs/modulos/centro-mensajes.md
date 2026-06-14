# Módulo: Centro de Mensajes (email / WhatsApp / push)
> Nivel 1 — core bajo control del CAF. Despacha y registra notificaciones humanas multi-canal (email operativo; WhatsApp/SMS aún no implementados) abstrayendo proveedores externos, mantiene un catálogo de plantillas versionadas e inmutables, y expone reportes de consumo por canal/cliente que el CAF (Nivel 2) lee para tarificar al cierre.

## Ubicación y operación
- **Repo / dir VPS:** `/opt/inovaweb-centro-mensajes`
- **Contenedores:** `centro_mensajes` (FastAPI/uvicorn, escucha `8001` interno) + `messages_postgres` (postgres:16-alpine, solo red interna, sin mapeo a host).
- **Puerto host:** `127.0.0.1:8005 -> 8001` (loopback; TLS/proxy por la Caddy externa del stack n8n a `centro_mensajes:8001`). Dominio: `https://mensajes.inovaweb.com.mx`.
- **GitHub (verificado):** `github-messages:InovawebSoluciones/inovaweb-centro-mensajes.git`, rama `main`, último commit `f06cf2d` — *"feat(centro): endpoint POST /v1/messages/record (conteo de envios externos) + cierre sesion 2026-06-08"* (2026-06-08). Working tree con cambios locales sin commitear (`.env.example` + backups `*.bak-*` y `backups/` sin trackear).
- **Estado:** `centro_mensajes` Up 6 días (healthy); `messages_postgres` Up 7 días (healthy).
- **Comando de deploy:** `cd /opt/inovaweb-centro-mensajes && docker compose up -d --build`. Migraciones SQL de `./database/` se aplican AUTOMÁTICAMENTE **solo en el primer arranque** del volumen postgres; cambios posteriores a mano.

## Stack técnico
- **Framework:** Python + FastAPI (async), uvicorn.
- **DB:** PostgreSQL 16, async `postgresql+psycopg://` (SQLAlchemy `AsyncSession`, SQL `text()`).
- **Cifrado:** AES-256-GCM (`AES_KEY`) para credenciales de proveedor.
- **Proveedores (`MessageProvider`):** `resend` (email, único `active`), `sendgrid` (email, stub), `meta_whatsapp` (whatsapp, stub), `twilio` (sms, stub).
- **Rate limiting:** opcional vía Redis (degradación graceful).
- **Worker `ledger_retry`:** solo arranca si `REPORT_TO_FINANZAS=True`. En la arquitectura actual (D2) el flag está en `False` → el worker NO corre (el CAF contabiliza).

## Contrato con el CAF (lo que el CAF consume)
El CAF llama al Centro desde `app/core/clients/messages_client.py` (clase `MessagesClient`), header **`X-API-Key`** (`MESSAGES_API_KEY`; base `MESSAGES_BASE_URL`). Tenant resuelto desde la API key, nunca del body.

| Método en `messages_client.py` | Verbo + ruta del Centro | Propósito | Auth |
|---|---|---|---|
| `send_email(...)` | `POST /v1/messages/email` | Email desde plantilla (`origin_kind="template"`, `template_id`=slug). `202` + `message_id`. **Operativo.** | `messages:write` |
| `send_whatsapp(...)` | `POST /v1/messages/whatsapp` | WhatsApp por plantilla. **El Centro responde `501`** (sprint 2). El cliente del CAF asume el mismo body que email (TODO en el propio cliente). | `messages:write` |
| `get_usage(external_user_id, from_ts, to_ts)` | `GET /v1/reports/usage?group_by=client&from_ts&to_ts` | Consumo total del cliente; el CAF filtra la fila `client_id==external_user_id` → `{messages, cost_cents}`. Ceros si no aparece. | `messages:read` |
| `get_usage_by_channel(external_user_id, from_ts, to_ts)` | `GET /v1/reports/usage?group_by=channel,client&from_ts&to_ts` | Conteo por canal (`{canal: cantidad}`) para tarificar email ≠ whatsapp ≠ sms. | `messages:read` |

**Plantillas `caf-*` (gotcha confirmado):** el CAF envía `origin_kind=template` contra slugs precargados (`caf-pago-confirmado`, `caf-activacion-correo`, `caf-factura-emitida`…). **NO están sembradas.** Las únicas en `templates` hoy: `tpl-bienvenida` (email), `tpl-boleta-mensual` (email), `tpl-recordatorio-pago` (whatsapp). Un `send_email` con slug `caf-*` devuelve **`404`**.

**Registro de consumo (`POST /v1/messages/record`):** registra un mensaje **ya enviado fuera del Centro** (SMTP de Scraping/n8n) insertando `status='sent'` para que entre en `reports/usage` sin despachar nada. **Idempotente por `(tenant, meta.source_ref)`**. Nota: `messages_client.py` del CAF **NO** expone método para `/record` — lo usan otros emisores (Scraping/n8n). Los mensajes `email/sent` presentes en BD vienen por esta vía.

## Superficie del core (endpoints principales)
- `POST /v1/messages/email` — despacha email; `origin_kind` ∈ `{template, ai_generated}`. Flujo: resuelve plantilla → valida `variables` vs `variables_schema` → renderiza → INSERT `queued` → `_dispatch_email`. `202`. (`messages:write`)
- `POST /v1/messages/whatsapp` — **`501`** (stub). · `POST /v1/messages/sms` — **`501`** (stub).
- `POST /v1/messages/record` — cuenta un envío externo sin despachar; `201`; idempotente por `source_ref`. (`messages:write`)
- `GET /v1/messages` — listado paginado (filtros `app/client/service/channel/status/from_ts/to_ts`, `limit≤500`). (`messages:read`)
- `GET /v1/messages/{id}` — detalle + `message_events`; IDOR-guard (404 unificado). (`messages:read`)
- `GET /v1/reports/usage` — agregados `COUNT(*)` + `SUM(amount_cents_charged)` por `group_by` ∈ `channel|client|app` (combinable). **Solo cuenta `status IN ('sent','delivered','bounced')`.** (`messages:read`)
- `POST/GET/PATCH /admin/v1/templates[/{id}]` — CRUD plantillas inmutables (PATCH crea `version+1`). (`admin:templates`)
- `POST/GET /admin/v1/tenants/{id}/channels/{ch}/credentials` — credenciales de proveedor por tenant+canal (AES-256-GCM, nunca se devuelven). (`admin:credentials`)
- `POST /webhooks/{provider_slug}` — eventos del proveedor (firma verificada por tenant; actualiza status a `delivered`/`bounced`/`failed`).
- `GET /v1/track/email/open/{id}`, `.../click` — tracking público firmado HMAC.
- `GET /health`, `GET /health/db` — públicos.

## Datos / BD y canales
Postgres `centro_mensajes`, usuario `messages` (`docker exec messages_postgres psql -U messages -d centro_mensajes`).

**Tablas:** `tenants`, `api_keys`, `messages`, `message_events`, `templates`, `message_providers`, `tenant_channel_credentials`.
- **`messages` — append-only y lifecycle por trigger:** `trg_messages_block_delete`, `trg_messages_block_mutation` (UPDATE solo columnas de lifecycle/ledger), `trg_messages_status_lifecycle` (`queued→sent|failed`, `sent→delivered|bounced|failed`; `delivered`/`bounced` terminales). CHECKs: `status`, `ledger_status`, `channel IN (email,whatsapp,sms)`, `origin_kind` solo email, `amount_cents_charged > 0` (centavos). **Status que cuentan en `usage`:** `sent`, `delivered`, `bounced`. Precios placeholder en código (`DEFAULT_PRICES_CENTS`: email 50, whatsapp 100, sms 150) — la autoridad de precio real es el CAF.
- **`message_events`:** append-only; dedup `UNIQUE(external_message_id, event_type)`.
- **`templates`:** inmutables (corrección = `version+1`). Sembradas: `tpl-bienvenida`, `tpl-boleta-mensual`, `tpl-recordatorio-pago`. **Sin `caf-*`.**
- **`message_providers`:** `resend`=email/**active**, `sendgrid`/`meta_whatsapp`/`twilio`=stub.
- **`tenant_channel_credentials`: VACÍA (0 filas).** Sin proveedor de email configurado → un `send_email` real falla en `_dispatch_email` con `status='failed'`, `last_error="no_credentials"`.
- **`api_keys`:** SHA-256. Activas: `admin-master` y `caf-core`, ambas scope `{*}` sobre el tenant `06b6160d-…`.

## Runbook operacional
| Síntoma | Diagnóstico | Comando | Verificación |
|---|---|---|---|
| Contenedor caído | Estado/salud | `docker ps -a \| grep -E "centro_mensajes\|messages_postgres"` · `docker logs --tail 100 centro_mensajes` | `curl -fsS http://127.0.0.1:8005/health` → `ok`; `.../health/db` → `db:ok` |
| Redeploy | Build+recreación | `cd /opt/inovaweb-centro-mensajes && docker compose up -d --build` | ambos `healthy` |
| `send_email` **404** (plantilla `caf-*`) | Plantilla no sembrada | `docker exec messages_postgres psql -U messages -d centro_mensajes -c "SELECT slug,channel,version,is_active FROM templates ORDER BY slug;"` | Sembrar vía `POST /admin/v1/templates` (`admin:templates`); reintentar |
| `send_email` `failed` `no_credentials` | `tenant_channel_credentials` vacío | `... -c "SELECT tenant_id,channel,provider_slug,is_default,is_active FROM tenant_channel_credentials;"` | Registrar Resend vía `POST /admin/v1/tenants/{id}/channels/email/credentials` (provider=`resend`, default=true) |
| WhatsApp/SMS **501** | No implementado | n/a (esperado) | `POST /v1/messages/whatsapp` → `501` |
| Uso de un cliente | Reporte | `GET /v1/reports/usage?group_by=channel,client&from_ts&to_ts` (`messages:read`) o SQL `SELECT channel,client_id,COUNT(*),SUM(amount_cents_charged) FROM messages WHERE status IN ('sent','delivered','bounced') GROUP BY 1,2;` | Cruzar `client_id` con `clients.messages_account_id` del CAF |
| Logs | JSON-lines | `docker logs -f --tail 200 centro_mensajes` | `request_id` |

## Variables de entorno clave
(`.env` de `/opt/inovaweb-centro-mensajes`; nombres sin valores)
- `DATABASE_URL` (compose la apunta a `postgres:5432`). · `POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB`.
- `AES_KEY` (32 bytes base64) — cifra credenciales de proveedor.
- `FINANZAS_BASE_URL`/`FINANZAS_API_KEY` — solo si `REPORT_TO_FINANZAS=True`.
- `REPORT_TO_FINANZAS` — flag D2. **Default `False`** (el Centro NO auto-reporta; el CAF contabiliza; `ledger_retry` no arranca).
- `PUBLIC_BASE_URL` (tracking pixel/click). · `PORT` (8001). · `ENV` (`prod` oculta `/docs` y desactiva CORS). · `LOG_LEVEL`.

Lado CAF: `MESSAGES_BASE_URL`, `MESSAGES_API_KEY`.

## Gotchas y pendientes conocidos
- **WhatsApp y SMS responden `501`** (stubs sprint 2). El cliente del CAF expone `send_whatsapp` pero recibirá 501.
- **Plantillas `caf-*` NO sembradas** → `send_email` con esos slugs da **404**. Pendiente: sembrarlas vía `POST /admin/v1/templates`.
- **Proveedor de email NO configurado:** `tenant_channel_credentials` VACÍO; aunque `resend` esté `active`, un `send_email` real termina `failed` (`no_credentials`). Pendiente: Resend / M365. Hoy el consumo email viene de `POST /v1/messages/record` (no despacha).
- **`/reports/usage` solo cuenta `sent|delivered|bounced`**: los `failed` no se tarifican; los `record` entran como `sent` y sí cuentan.
- **Precios en código son placeholder**; la autoridad de precio es el CAF.
- **Migraciones solo en primer arranque** del volumen; aplicar cambios de esquema a mano.
- **Working tree del repo con cambios sin commitear** (`.env.example` + backups).
- **`api_keys` con scope `{*}`** (`admin-master`, `caf-core`). `[TODO: confirmar si conviene acotar `caf-core` a `messages:write/read`]`.
