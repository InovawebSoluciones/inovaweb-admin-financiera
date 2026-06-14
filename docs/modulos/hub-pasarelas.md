# Módulo: Hub de Pasarelas (central de pagos)
> Nivel 1 — core bajo control del CAF. API única de cobros que abstrae las pasarelas (Conekta/OXXO/SPEI/tarjeta + mock de pruebas): recibe un `charge`, procesa el pago, escucha el webhook de la pasarela y notifica el pago confirmado al CAF para que éste acredite. Regla maestra: **todo pago pasa por el Hub, nunca por una pasarela directa.**

## Ubicación y operación
- **Repo/dir VPS:** `/opt/inovaweb-hub-pasarelas`
- **Contenedores:**
  - `hub_pasarelas` — `Up (healthy)`, `127.0.0.1:8003 -> 8001` (contenedor escucha en 8001; host publica en loopback 8003). FastAPI/uvicorn.
  - `hub_postgres` — `Up (healthy)`, Postgres 16-alpine, `5432` solo red interna.
- **Healthcheck:** `GET http://127.0.0.1:8003/health` → `{"status":"ok","service":"inovaweb-hub-pasarelas"}`. Readiness `GET /health/db` (`SELECT 1`, 503 si Postgres cae).
- **Reverse proxy / TLS:** la Caddy externa del stack n8n (red `n8n_default`, `external`); el Hub se une a esa red para ser alcanzable por nombre `hub_pasarelas:8001`. El servicio `caddy` del propio compose solo con `--profile edge` (NO usar en este VPS, chocaría 80/443).
- **GitHub:** `git@github-hub:InovawebSoluciones/inovaweb-hub-pasarelas.git`, rama `main`.
  - Último commit local (desplegado): `7281722 feat(D2): notificar al CAF en pago confirmado (no acreditar Medidor directo); mock async (pending->webhook); raw_response en SELECT webhook`.
  - **⚠️ DRIFT DETECTADO (ramas divergieron):** `git rev-list --left-right --count origin/main...HEAD` = `2  1`. El commit `7281722` (D2 — lo que documenta este archivo) **NO está pusheado a GitHub**, y el VPS está **2 commits atrás** de `origin/main` (`6d9f726 docs: auditoria global...`, `a91000c chore: sync...`, solo en remoto). **El código que corre en prod es el del VPS; GitHub no lo refleja.** Ver Gotchas.
- **Comando de deploy:**
  ```bash
  cd /opt/inovaweb-hub-pasarelas
  docker compose up -d --build      # reconstruye e inicia hub + postgres
  docker compose logs -f hub        # seguir logs (el servicio se llama 'hub')
  # migraciones posteriores a mano:
  docker compose exec -T postgres psql -U hub -d hub_pasarelas < database/003_rename_tenant_to_company.sql
  ```

## Stack técnico
- **Python 3.12** + **FastAPI** + **uvicorn** (escucha 8001).
- **SQLAlchemy 2 async** con `text()` parametrizado + **psycopg 3** (`postgresql+psycopg://`).
- **PostgreSQL 16** (`hub_postgres`).
- **httpx** async (pasarelas, Medidor, webhook del CAF).
- **cryptography** — AES-256-GCM para cifrar credenciales de pasarela (`app/core/crypto.py`).
- **pydantic-settings**. **Redis** opcional (rate limiting; degrada si no hay `REDIS_URL`).
- Logging JSON-lines con `request_id`.

## Contrato con el CAF (lo que el CAF consume) y flujo de pago
El CAF llama al Hub desde `app/core/clients/hub_client.py`. **El CAF consume un solo endpoint:**

| Método en `hub_client.py` | Verbo + ruta del Hub | Propósito | Auth |
|---|---|---|---|
| `HubClient.charge(...)` | `POST /hub/v1/charge` | Iniciar un cobro (recarga prepago o compra de plan). Envía `gateway` (default `conekta`, pruebas `mock`), `operation` (default `charge_card`), `amount_cents`, `currency="MXN"`, `description`, `customer_email`, `customer_name`, `external_user_id` (= `clients.hub_account_id`, formato `client-<caf_client_id>`). Devuelve `hub_transaction_id`. | `X-API-Key` (`HUB_API_KEY`) scope `payments:write` |

Notas firmes del contrato:
- El Hub **NO** tiene endpoint admin de alta de empresa; la config de pasarela por cliente se inserta por SQL en `company_gateway_config` durante el onboarding (pendiente: `POST /admin/hub/v1/companies`).
- El CAF **nunca toca datos de tarjeta**: la tokenización ocurre en el frontend con el SDK de la pasarela.
- El campo `metadata` se conserva en la firma de `charge()` por compatibilidad pero **NO se envía** (el `HubChargeRequest` no lo tiene; el `purpose` lo resuelve el Hub por defecto a `wallet_recharge`).
- Idempotencia del lado CAF: UNIQUE por `hub_transaction_id` en `payments`.

### Flujo completo de recarga (regla: TODO pago por el Hub)
```
1. Cliente -> CAF  POST /portal/recharge
       └─ portal_router -> services.prepago.initiate_charge(...)
2. CAF  -> Hub     POST /hub/v1/charge   (X-API-Key payments:write)
       gateway='conekta'(prod)|'mock'(pruebas), operation, amount_cents,
       customer_email/name, external_user_id = client.hub_account_id
       └─ Hub: INSERT hub_payment_transactions status='pending' (ANTES de cobrar)
       └─ Hub: delega a la pasarela (Conekta real / Mock simulado)
       └─ Hub: devuelve hub_transaction_id + checkout_url/OXXO/CLABE
       └─ CAF: audit_log 'recharge.initiated' (NO toca payments aún)
3. Pasarela procesa el pago (async; mock también es async)
4. Pasarela -> Hub  POST /webhooks/{gateway}  (firma HMAC del provider)
       └─ Hub: valida firma, localiza tx por gateway_order_id, UPDATE 'pending'->'paid'
5. Hub -> CAF      POST {CAF_WEBHOOK_URL}  (evento payment.paid)
       └─ Body: {transaction_id, external_user_id, amount_cents, metadata:{purpose,plan_code}, paid_at}
       └─ X-Signature = hex(HMAC_SHA256(CAF_WEBHOOK_SECRET, f"{X-Timestamp}." + body_bytes))
       └─ Con CAF_WEBHOOK_URL set, el Hub NO acredita al Medidor directo (D2): el CAF es la ÚNICA capa que acredita y contabiliza.
6. CAF  /webhooks/hub-payment-paid
       └─ Verifica firma HMAC+timestamp ANTES de cualquier I/O (401 si inválida)
       └─ prepago.process_paid_event -> ACREDITA wallet (Medidor) + asienta Finanzas. Idempotente por hub_transaction_id.
       └─ El Hub responde 200 pase lo que pase; si el POST al CAF falla, el CAF reconcilia.
```
Pruebas: `gateway='mock'` (simulado). Prod: `gateway='conekta'` (real, ya cableado).

## Superficie del core (endpoints principales)
**Públicos:** `GET /health`, `GET /health/db`, `/docs`, `/redoc`, `/openapi.json`.
**Autenticados (`X-API-Key`):**
- `GET /hub/v1/gateways` — catálogo de pasarelas/operaciones.
- `POST /hub/v1/charge` — crear cargo (`payments:write`/`*`). Valida gateway en catálogo, operación soportada, no-stub (422), moneda (422). `company_id` desde la API key, nunca del body.
- `GET /hub/v1/transactions` — historial paginado del tenant (`payments:read`/`write`/`*`).
- `GET /hub/v1/transactions/{hub_tx_id}` — detalle (incl. `raw_response`); IDOR-guard (404 si ajeno).

**Webhooks (firma HMAC del provider):**
- `POST /webhooks/{gateway}` — eventos de la pasarela (`conekta`/`evo`/`mock` activos; `stripe`/`mercadopago`/`openpay` → 422). Valida Content-Type, body ≤1 MiB, firma (obligatoria, 401 si falta). Localiza tx por `gateway_order_id`, transición de estado validada, y si quedó `paid` con `external_user_id` → notifica al CAF (si `CAF_WEBHOOK_URL`) o acredita al Medidor (legacy).

## Datos / BD y pasarelas
**BD:** Postgres `hub_pasarelas` (`hub_postgres`). Migraciones en `database/` (001 esquema, 002 constraints, 003 rename `tenant_id`→`company_id`). Tablas:
- `tenants` — clientes (multi-tenant; UUID alineado con el Medidor).
- `payment_gateways` — catálogo (mock/conekta/evo `active`; stripe/mercadopago/openpay `stub`).
- `company_gateway_config` — credenciales por tenant en `config_encrypted` (AES-256-GCM); `UNIQUE(company_id, gateway_slug)` + índice de una sola `is_default` activa por tenant.
- `api_keys` — `key_hash` = SHA-256 hex, `scopes TEXT[]`, `rate_limit_per_minute`. Append-only (revoca con `is_active=false`).
- `hub_payment_transactions` — **log canónico (append-only)**: `hub_transaction_id TEXT UNIQUE` (`htx_<32 hex>`), `company_id`, `external_user_id` (nullable B2B), `gateway_slug`, `gateway_order_id`, `operation`, `amount_cents INTEGER CHECK(>0)`, `currency CHAR(3)`, `status` (`pending->paid|failed`, `paid->refunded`, enforced por trigger+CHECK), `raw_response JSONB` (nunca PAN/CVV), y trazabilidad legacy al Medidor (`medidor_wallet_id`, `medidor_request_id TEXT UNIQUE`, `medidor_credit_status`, `medidor_credit_attempts`, `medidor_last_error`).

**Idempotencia:** por `hub_transaction_id UNIQUE`; en el camino legacy a Medidor, `medidor_request_id UNIQUE` (`"<gateway>-payment-<provider_payment_id>"`). **Nota:** el INSERT del registro `pending` **no** usa `ON CONFLICT`; la idempotencia se garantiza con los índices UNIQUE y, en el webhook, con la validación de transición (`pending->paid` una sola vez). El nombre de columna canónico es `hub_transaction_id` (no `hub_payment_id`).

**Dinero:** centavos enteros en toda la cadena (nunca floats). Tope por cargo `le=10_000_000` (≈$100,000 MXN). `currency` ISO-4217 (`CHECK ~ '^[A-Z]{3}$'`).

**Pasarelas:** `mock` (active, sandbox; `create_order` devuelve `status='pending'` → confirma por webhook; sin validación de firma), `conekta` (active, productiva MX: tarjeta/OXXO/SPEI/MSI/refund; valida firma webhook HMAC-SHA256), `evo` (active, única con USD), `stripe`/`mercadopago`/`openpay` (stub, 422 antes de cobrar).

## Runbook operacional
| Síntoma | Diagnóstico | Comando (VPS) | Verificación |
|---|---|---|---|
| Contenedor caído | Estado/reinicio | `docker ps --filter name=hub` → `cd /opt/inovaweb-hub-pasarelas && docker compose up -d` | `curl -fsS http://127.0.0.1:8003/health` → `ok` |
| 503 en cargos | Postgres caído | `docker compose logs --tail=100 postgres`; `docker compose ps` | `curl -fsS http://127.0.0.1:8003/health/db` → `db:ok` |
| Recarga colgada (`pending`) | Webhook no llegó o `gateway_order_id` no matcheó | `docker compose exec postgres psql -U hub -d hub_pasarelas -c "SELECT hub_transaction_id,status,gateway_slug,gateway_order_id,created_at FROM hub_payment_transactions WHERE status='pending' ORDER BY created_at DESC LIMIT 20;"` | Reenviar/replay del webhook desde el panel de la pasarela |
| Webhook llegó pero el CAF no acreditó | POST `{CAF_WEBHOOK_URL}` falló o `external_user_id` nulo | `docker compose logs hub \| grep -E "CAF notify"` (busca `OK` vs `no-2xx`/`fallo`; `sin external_user_id` = B2B) | Si firma inválida → `CAF_WEBHOOK_SECRET` (Hub) debe == `HUB_WEBHOOK_SECRET` (CAF) |
| Tx `paid` huérfanas | `CAF_WEBHOOK_URL` vacío (legacy) o notificación falló | `... "SELECT hub_transaction_id,external_user_id,amount_cents,raw_response->>'purpose' FROM hub_payment_transactions WHERE status='paid' ORDER BY created_at DESC LIMIT 20;"`; confirmar `CAF_WEBHOOK_URL` | El CAF reconcilia (idempotente por `hub_transaction_id`) |
| Logs | Cualquier evento | `cd /opt/inovaweb-hub-pasarelas && docker compose logs -f hub` | Buscar por `hub_transaction_id`/`gateway` |

## Variables de entorno clave
(`/opt/inovaweb-hub-pasarelas/.env`; nombres, sin valores)
| Variable | Para qué |
|---|---|
| `DATABASE_URL` | Conexión async a Postgres (compose la apunta a `hub_postgres`). |
| `POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB` | Credenciales del Postgres del compose. |
| `AES_KEY` | AES-256 (32 bytes base64) para descifrar `company_gateway_config.config_encrypted`. |
| `MEDIDOR_BASE_URL` | URL del Medidor (camino legacy de crédito directo, en desuso con el CAF). |
| `MEDIDOR_API_KEY` | Key scope `admin` para acreditar wallets en el Medidor (legacy). |
| `CAF_WEBHOOK_URL` | **Destino del webhook saliente `payment.paid` hacia el CAF.** Set → notifica al CAF y NO acredita al Medidor (D2). Vacío = legacy. |
| `CAF_WEBHOOK_SECRET` | **Secreto HMAC-SHA256** con que el Hub firma el webhook al CAF. Debe coincidir con `HUB_WEBHOOK_SECRET` del CAF. |
| `PORT` (8001), `ENV` (sin default), `LOG_LEVEL`, `REDIS_URL` (opcional) | |

Credenciales de **Conekta** (`private_key`, `public_key`, `webhook_secret`) NO son env: viven cifradas por-tenant en `company_gateway_config.config_encrypted` (AES-256-GCM). Existe `.env.bak-d2-102812` (backup pre-D2, `chmod 600`).

## Gotchas y pendientes conocidos
- **DRIFT VPS ↔ GitHub (importante):** el commit `7281722` (D2 — todo lo que documenta este archivo) **está desplegado y corriendo en el VPS pero NO pusheado** a `origin/main`. Además el VPS está 2 commits **atrás** de remoto (`6d9f726`, `a91000c`, docs/sync 2026-06-06 solo en GitHub). Ramas divergidas (`origin...HEAD = 2  1`). **GitHub no refleja producción.** Pendiente: reconciliar (merge cuidadoso favoreciendo el VPS, como se hizo con el CAF) y pushear, tras revisar que los 2 commits remotos no pisen el trabajo D2. Working tree limpio salvo backups sin trackear (`*.bak-d2`, `*.bak-pending`, etc.).
- **Mock async vs síncrono:** `MockGateway.create_order` devuelve `status='pending'` a propósito (no `paid`) para ejercitar recarga→webhook→notifica. El `paid` solo llega con un webhook a `POST /webhooks/mock`. Una "prueba" con mock requiere disparar también el webhook. (Su `raw_response.status` dice `"paid"`, engañoso pero irrelevante para el lifecycle.)
- **`_extract_payment_id`:** Conekta `payload.data.object.id`; EVO `transaction_id`/`id`; mock `payment_id`/`id`. Si no lo encuentra → `200 {"ok":true,"ignored":...}` (para que la pasarela no reintente).
- **CLAUDE.md y `database/001` describen el modelo LEGACY** (Hub acreditaba directo al Medidor). Ese camino sigue en el código (`_credit_to_medidor`, job `medidor_retry`) pero queda **inactivo** con `CAF_WEBHOOK_URL` configurado (caso productivo D2). Fuente de verdad del contrato actual: `webhooks_router._notify_caf` + `hub_client.py` del CAF.
- **Firma del webhook al CAF (exacta):** `X-Signature = hex(HMAC_SHA256(CAF_WEBHOOK_SECRET, f"{X-Timestamp}." + body_bytes))`, con `body_bytes` el JSON `separators=(",",":")` (byte-idéntico al enviado). El CAF valida firma+timestamp antes de I/O y exige timestamp en `prod`. Si `CAF_WEBHOOK_SECRET` (Hub) ≠ `HUB_WEBHOOK_SECRET` (CAF) → 401 (gotcha histórico: hubo duplicados de esta clave en el `.env` del CAF).
- **Verificación de firma por pasarela:** Conekta y mock resueltos; los stubs lanzan `NotImplementedError` → 501. HMAC real de EVO pendiente.
- **Rate limiting Redis:** código listo pero `REDIS_URL` no configurada → sin rate limit (degradación silenciosa).
- **Endpoints admin** (alta de empresa, CRUD de keys/config cifrada) no existen: hoy por SQL directo. `POST /admin/hub/v1/companies` es deuda conocida.
