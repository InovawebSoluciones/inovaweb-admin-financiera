# Módulo: Finanzas-Core (ledger inmutable de ingresos)
> Nivel 1 — core bajo control del CAF. Es el libro contable append-only de la plataforma Inovaweb: registra y agrega hechos económicos crudos (créditos/débitos) de todos los cores, sin lógica de negocio; el CAF (Nivel 2) lo orquesta asentando consumos e ingresos contra él.

## Ubicación y operación
- **Repo / dir en VPS:** `/opt/inovaweb-finanzas-core`
- **Contenedores:** `finanzas_core` (FastAPI/uvicorn, expone `8001` interno) y `finanzas_postgres` (Postgres 16-alpine, `5432` solo en red interna).
- **Puerto host:** `127.0.0.1:8004 -> 8001` (loopback, para curl/debug). El TLS/proxy público lo hace el **Caddy del stack n8n** por nombre `finanzas_core:8001` vía red externa `n8n_default`. Dominio: `https://finanzas.inovaweb.com.mx`.
- **Estado verificado (2026-06-14):** ambos `Up 9 days (healthy)`; `GET /health` → `{"status":"ok","service":"inovaweb-finanzas-core"}`; `GET /health/db` → `{"status":"ok","db":"ok","select1":1}`.
- **GitHub:** `git@github-finanzas:InovawebSoluciones/inovaweb-finanzas-core.git` — rama `main` — último commit `88cd6f2` (`docs: contrato de integracion para cores emisores`, 2026-05-25).
- **Comando de deploy:**
  ```bash
  cd /opt/inovaweb-finanzas-core
  docker compose up -d --build finanzas
  # Postgres aplica database/*.sql SOLO en el primer arranque (volumen vacío).
  # Migraciones posteriores se corren a mano por psql.
  ```

## Stack técnico
- **Framework:** Python 3.12, FastAPI, uvicorn (2 workers, `--proxy-headers`, `--forwarded-allow-ips 127.0.0.1,172.16.0.0/12,192.168.0.0/16`).
- **BD:** PostgreSQL 16-alpine, async vía SQLAlchemy + `psycopg` (`postgresql+psycopg://`).
- **Auth:** API key header `X-API-Key`, hash SHA-256 en BD, scopes en array, rate-limit opcional por Redis (degrada suave).
- **Crypto:** AES-256-GCM (`app/core/crypto.py`) — presente pero **reservado a futuro**; el ledger no cifra nada hoy.
- **Imagen:** multi-stage `python:3.12-slim`, usuario sin privilegios `ledger` (uid 10001). Healthcheck nativo a `/health`.
- **OpenAPI:** `/docs`, `/redoc`, `/openapi.json`.

## Contrato con el CAF (lo que el CAF consume)
El CAF llama a Finanzas desde `app/core/clients/finanzas_client.py` (clase `FinanzasClient`), base `FINANZAS_BASE_URL` + `FINANZAS_API_KEY` (key admin master, prefijo `fz_caf_…`).

| Método de `FinanzasClient` | Verbo + ruta de Finanzas | Propósito | Auth |
|---|---|---|---|
| `post_entry(...)` | `POST /v1/ledger/entries` | Asienta un hecho económico (credit/debit) idempotente | scope `ledger:write` o `*` |
| `get_balance(as_of, external_user_id?)` | `GET /v1/ledger/balance?as_of=` | Saldo neto del tenant a una fecha (credit−debit por currency) | key válida |
| `get_totals(from_ts, to_ts)` | `GET /v1/ledger/totals?from_ts&to_ts` | Agregados por `source_slug`+`direction`+`currency` en ventana | key válida |
| `list_entries(source?, direction?, limit, offset)` | `GET /v1/ledger/entries` | Listado paginado con filtros | key válida |

**Conjunto CERRADO de `source` (`source_slug`):** `{medidor, hub, messages, invoice, subscription, manual}`. Validado en router (`VALID_SOURCES` + enum `direction` `{credit, debit}`) y en BD por FK contra `ledger_sources`. Slug fuera del conjunto → **422**.

**Idempotencia por `source_ref`:** `UNIQUE(tenant_id, source_slug, source_ref)`. Un `POST` repetido con la misma terna devuelve la entry previa con `idempotent_replay=true`. Convención determinística del CAF: `caf-invoice-<id>-payment`, `caf-recharge-<rch_id>`, `caf-manual-adj-<adj_id>`, `caf-sub-<client>-<yyyymm>`, reversión `<original>-reversal`.

**Resolución de tenant/identidad (clave):**
- `tenant_id`/`company_id` se resuelve **siempre desde la API key**, nunca del body (anti cross-tenant forge). El CAF usa una key **admin master**.
- El CAF **no crea cuentas ni emite llaves** en Finanzas (no existen `create_account`/`issue_api_key`/`delete_account`).
- La vista por cliente final se filtra por `external_user_id`, que vive en `meta` (JSONB) de cada asiento.
- Dinero siempre en centavos (`amount_cents` BIGINT), `currency` fija `MXN` en el cliente del CAF.

## Superficie del core (endpoints principales)
| Verbo + ruta | Scope | Descripción |
|---|---|---|
| `GET /health` | público | Liveness. |
| `GET /health/db` | público | Readiness (`SELECT 1`, 503 si cae). |
| `POST /v1/ledger/entries` | `ledger:write`/`*` | Registra asiento append-only e idempotente (201, o replay). |
| `GET /v1/ledger/entries` | key válida | Lista paginada (`source`,`direction`,`currency`,`from_ts`,`to_ts`,`limit≤500`,`offset`). |
| `GET /v1/ledger/entries/{id}` | key válida | Detalle con guard IDOR (filtra `tenant_id`; 404 si ajeno). |
| `GET /v1/ledger/balance` | key válida | Balance neto por currency (`credit_cents`,`debit_cents`,`net_cents`), opcional `as_of`. |
| `GET /v1/ledger/totals` | key válida | Agregados por `source_slug`+`direction`+`currency` en `[from_ts,to_ts)`. |

Notas: `amount_cents` `>0` y `≤1_000_000_000` (~$10M MXN); `description` 4–500 chars; `meta` JSON solo auditoría. CORS `GET/POST/OPTIONS` desde cualquier origen, sin credenciales.

## Datos / BD
Migraciones en `database/` (aplicadas por `docker-entrypoint-initdb.d` en el primer arranque).
- `tenants` — clientes (multi-tenant). `slug` único, `is_active`, `metadata` JSONB.
- `api_keys` — `key_hash` SHA-256 (UNIQUE), `key_prefix`, `company_id` (FK `tenants`), `scopes TEXT[]` (`ledger:read`/`ledger:write`/`aggregates:read`/`*`), `rate_limit_per_minute` (120), `expires_at`, `revoked_at`.
- `ledger_sources` — catálogo **cerrado**; sembrado con `medidor, hub, messages, invoice, subscription, manual`.
- `ledger_entries` — núcleo **append-only**: `id` UUID, `tenant_id` (FK `ON DELETE RESTRICT`), `source_slug` (FK), `source_ref`, `direction`, `amount_cents` BIGINT `CHECK >0`, `currency CHAR(3)` default MXN, `occurred_at` (emisor), `recorded_at` (server, no manipulable), `description`, `meta` JSONB, `actor_api_key_id`. **`UNIQUE(tenant_id, source_slug, source_ref)`** = idempotencia.

**Invariantes en BD (`002_security_constraints.sql`):**
- `ledger_block_delete`: DELETE prohibido. Reversa = entry inversa con `source_ref="<original>-reversal"`.
- `ledger_block_mutation`: UPDATE bloqueado en columnas críticas; solo `meta` editable.
- `api_keys_block_delete`: keys no se borran, se revocan.
- `CHECK` `direction IN ('credit','debit')` y `currency ~ '^[A-Z]{3}$'`.

Reglas de oro: dinero en centavos BIGINT; solo INSERT en el ledger; toda query filtra por `tenant_id`.

## Runbook operacional
| Síntoma | Diagnóstico | Comando | Verificación |
|---|---|---|---|
| Contenedor caído | Estado/salud | `docker ps --filter name=finanzas` · `docker logs --tail 100 finanzas_core` | `curl -s http://127.0.0.1:8004/health` → `status:ok` |
| Postgres caído (503 en `/health/db`) | BD sin readiness | `docker logs --tail 80 finanzas_postgres` | `curl -s http://127.0.0.1:8004/health/db` → `db:ok` |
| Asiento 422 source inválido | `source_slug` fuera del conjunto cerrado | Revisar payload; válidos: `medidor,hub,messages,invoice,subscription,manual` | Reintentar → 201 |
| Asiento 422 otro motivo | `direction`≠credit/debit, `amount_cents`≤0 o >tope, `description` corta | `docker logs finanzas_core` (detalle de validación) | Reenviar corregido |
| "Doble cobro" aparente | Idempotencia mismo `(tenant,source,source_ref)` | n/a (esperado) | Respuesta `idempotent_replay:true` |
| Cuadre de un cliente | Balance por `external_user_id` | `curl -s -H "X-API-Key: <fz_caf_…>" "http://127.0.0.1:8004/v1/ledger/balance?external_user_id=<id>"` | Comparar `net_cents` |
| Revertir un asiento | DELETE/UPDATE bloqueados | Insertar inversa: `direction` opuesto + `source_ref="<original>-reversal"` | `balance`/`totals` refleja compensación |
| Logs | json-file 10m×5 | `docker logs -f finanzas_core` | `request_id` por línea |

> Operaciones SOLO LECTURA / diagnóstico. No reiniciar ni mutar sin autorización.

## Variables de entorno clave
(En `.env` del repo de Finanzas — nombres, sin valores.)
- `DATABASE_URL` (`postgresql+psycopg://…`; en compose se compone de `POSTGRES_*`).
- `POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB`.
- `AES_KEY` — AES-256 (32 bytes base64). Validada al arranque (fail-fast). Reservada a futuro.
- `PORT` (8001), `ENV` (`dev`/`staging`/`prod`, sin default), `LOG_LEVEL`.

Lado CAF: `FINANZAS_BASE_URL`, `FINANZAS_API_KEY` (`fz_caf_…`), + `HTTP_TIMEOUT_SEC`/`HTTP_RETRIES`.

## Gotchas y pendientes conocidos
- **Acceso público solo vía Caddy del stack n8n** (`finanzas_core:8001` en `n8n_default`). Si esa red/Caddy caen, el dominio deja de resolver aunque el contenedor esté `healthy` (8004 es loopback). El `Caddyfile` del repo es referencia, no el que sirve en prod.
- **Migraciones automáticas solo en primer arranque** (volumen `finanzas_pg_data` vacío). Una nueva `003_*.sql` NO se aplica sola; correrla a mano con `psql`.
- **Reversa = compensación, no edición.** DELETE/UPDATE bloqueados por trigger; corrección = entry inversa nueva. No `UPDATE` salvo `meta`.
- **`tenant_id` jamás del body.** Siempre de la API key; cliente final por `meta.external_user_id`.
- **`actor_api_key_id` queda NULL** en el flujo normal; el audit se apoya en `last_used_at`. Header opcional `X-Actor-Key-Id` para relays.
- **Rate limit depende de Redis** y degrada suave (si no hay Redis, no se aplica). No se observa Redis en el compose.
- **CORS abierto** (`*`, sin credenciales) — aceptable por ser API server-to-server con `X-API-Key`.
- **`AES_KEY` obligatoria al arranque** aunque el ledger no la use (fail-fast si falta o no es base64 de 32 bytes).
- **`source_ref` ≤160 chars**, `source_slug` ≤40; respetar la convención determinística del CAF para no romper idempotencia.
