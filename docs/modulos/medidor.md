# Módulo: Medidor (medición de consumo IA)
> Nivel 1 — core bajo control del CAF. Servicio FastAPI multi-tenant que mide y tarifica el consumo de LLMs/IA (telemetría, holds pre-flight, refunds anti-fraude) y expone wallets prepago en centavos; el CAF (Nivel 2) lo administra con llaves de scope ADMIN.

## Ubicación y operación
- **Repo / dir en VPS:** `/opt/medidor_ia` (ignorar `/opt/medidor_ia.bak.*`, `src.bak/` y los `*.tar.gz`; el código canónico vive en `src/medidor_ia/`).
- **Contenedores** (`docker ps --filter name=medidor`):
  - `medidor-api` — FastAPI/uvicorn, publica `127.0.0.1:8007 -> 8000` (solo loopback; Caddy/n8n lo alcanza por DNS interno `medidor-api:8000`). Estado: `Up (healthy)`.
  - `medidor-jobs` — worker de expiración de holds (`python -m medidor_ia.jobs`), sin puerto HTTP. Estado: `Up (unhealthy)` — **falso positivo conocido** (ver Gotchas).
- **Puerto host 8007:** se eligió porque el `8000` lo ocupa Micro-Fichas en el mismo VPS.
- **Base de datos:** NO levanta Postgres propio; reusa el contenedor `scraping-postgres` (PG16) con database dedicada `medidor_ia` (usuario `medidor`). Redes externas: `scraping-universidades_default` (BD) y `n8n_default` (reverse proxy).
- **Dominio público:** `https://medidor.inovaweb.com.mx` (es la `MEDIDOR_BASE_URL` que usa el CAF).
- **GitHub:** repo `InovawebSoluciones/medidor_ia`. **No verificable desde el VPS:** `/opt/medidor_ia` no es un repositorio git (`git remote -v` → *fatal: not a git repository*); remote/rama/commit no se pueden corroborar in situ. `[TODO: completar desde una copia con .git]`.
- **Versión app:** `__version__ = "0.1.0"`. **Migración BD:** `alembic_version = 0007_seed_deepseek_catalog`.
- **Comando de deploy:**
  ```bash
  cd /opt/medidor_ia
  docker compose -f docker-compose.vps.yml --env-file .env.prod up -d --build
  ```
  Imagen (`medidor_ia:latest`) inmutable: sin bind mounts, código horneado en build. `jobs` arranca solo cuando `api` está `service_healthy`.

## Stack técnico
- **Runtime:** Python 3.12 (`python:3.12-slim`).
- **Framework:** FastAPI + uvicorn; SQLAlchemy async (`AsyncSession`) sobre PostgreSQL 16 (asyncpg).
- **Migraciones:** Alembic (`alembic/versions/0001..0007`).
- **Config:** pydantic-settings (`Settings`), lee `.env.prod`.
- **Logging:** structlog (JSON).
- **Seguridad:** API keys con HMAC-SHA256 (`compare_digest` constant-time).
- **Hardening prod:** `/docs`, `/redoc`, `/openapi.json` deshabilitados salvo `EXPOSE_DOCS=true`; exception handler global sin stack traces.

## Contrato con el CAF (lo que el CAF consume)
El CAF llama al Medidor desde `app/core/clients/medidor_client.py` (clase `MedidorClient`). Auth: una sola llave **ADMIN de bootstrap** (`MEDIDOR_API_KEY`, sembrada por SQL) como header `X-Api-Key`. Todos los métodos que el CAF usa son ADMIN o lectura; **el CAF NUNCA debita ni hace authorize/finish/release** (eso es del consumidor scope CLIENT).

| Método de `medidor_client.py` | Verbo + ruta del Medidor | Propósito | Scope |
|---|---|---|---|
| `create_wallet(external_user_id)` | `POST /v1/wallets` | Crea wallet vacío (alta/onboarding). Body `extra="forbid"`: `external_user_id` + `currency="MXN"`. Respuesta normaliza `wallet_id`→`id`. | ADMIN |
| `get_balance(wallet_id)` | `GET /v1/wallets/{id}/balance` | Saldo + holds (lectura). | tenant válido |
| `get_usage(wallet_id, from_ts, to_ts)` | `GET /v1/usage?from_ts&to_ts&project_id=wallet_id` | Consumo IA del periodo (`items`). | tenant válido |
| `get_usage_summary(...)` | (agrega sobre `get_usage`) | Suma `operations` y `cost_cents` del periodo. No es endpoint nuevo. | tenant válido |
| `credit(wallet_id, amount_cents, request_id, reason)` | `POST /v1/wallets/{id}/credit` | Acredita saldo (recarga). Idempotente por `request_id` (patrón `caf-recharge-<id>`). | **ADMIN** |
| `suspend_wallet(wallet_id, reason)` | `POST /admin/v1/wallets/{id}/suspend` | Compensación best-effort de la Saga de alta (no hay DELETE de wallet). | ADMIN |

**Identidad:** wallet único por `(tenant_id, external_user_id)` (`uq_wallets_tenant_id_external_user_id`). El `tenant_id` lo determina la llave ADMIN del CAF; el CAF pasa el `external_user_id`, recibe el `wallet_id` (UUID) y lo persiste para balance/usage/credit/suspend.

**Idempotencia:** `request_id` UNIQUE por `(wallet_id, request_id)` en `wallet_transactions`. Un webhook duplicado del Hub que dispare `credit` con el mismo `request_id` NO duplica acreditación.

**Nota de rutas (bug histórico C1):** `credit` vive en `/v1/wallets/{id}/credit` (NO bajo `/admin/v1`) aunque exija scope ADMIN; `suspend` sí vive en `/admin/v1/...`. Verificado contra el código real (`wallet/router.py` y `admin/router.py`).

**Dónde vive el saldo:** el Medidor **sí mantiene** wallets con `balance_cents` y ledger append-only (`wallet_transactions`), y es fuente de verdad de **consumo IA** (holds/eventos). El saldo monetario "de negocio" del cliente migró al CAF (`prepaid_ledger`); el CAF acredita en el Medidor vía `credit` para que éste pueda autorizar/cobrar IA, pero la contabilidad financiera la lleva el CAF. Existe `POST /v1/wallets/{id}/debit` (ADMIN) pero `medidor_client.py` **no lo invoca**.

## Superficie del core (endpoints principales)
**Meta/salud (sin auth):** `GET /health`, `GET /health/db`.

**Wallet (`wallet/router.py`):**
- `POST /v1/wallets` (ADMIN, 201; 409 si existe).
- `GET /v1/wallets/{id}/balance` (lectura).
- `GET /v1/wallets/{id}/transactions?kind&limit` (lectura, reconciliación CAF→Finanzas).
- `POST /v1/wallets/{id}/credit` (ADMIN, idempotente).
- `POST /v1/wallets/{id}/debit` (ADMIN, idempotente; 402 si insuficiente).

**Operaciones/holds (`wallet/holds_router.py`) — scope CLIENT (consumidor, no el CAF):**
- `POST /v1/operations/authorize` (reserva pre-flight; **CLIENT estricto**).
- `POST /v1/operations/finish` (captura: cobra `max(real_cost, hold)`).
- `POST /v1/operations/release` (libera sin cobro).
- `POST /v1/operations/quote` (estima sin reservar).

**Telemetría/catálogo/refunds:**
- `POST /v1/events/track` (passive meter; idempotente por `idempotency_key`).
- `GET /v1/usage?project_id&days`.
- `GET /v1/catalog` (servicios cobrables por tenant).
- `POST /v1/events/refund` (`mode=auto` decisor kill-switch+circuit-breaker; `mode=manual` requiere ADMIN).

**Admin (`/admin/v1`, todos ADMIN):** `flags` (GET/PUT con audit), `wallets/suspicious`, `wallets/{id}/suspend|unsuspend`, `refund_audit`.

**Proxies LLM (MODELO 1, cobro estricto por llamada):** `POST /llm/deepseek/v1/chat/completions` y `POST /llm/perplexity/v1/chat/completions` — proxy OpenAI-compatible: pre-check saldo (402 si ≤0 sin llamar upstream) → proveedor → DEBIT atómico `max(min_charge_cents, costo)` → telemetría. Aceptan `Authorization: Bearer` (n8n) además de `X-Api-Key`. **Failing-open en cobranza** (si el debit falla tras respuesta OK, se loguea pero no rompe n8n).

## Datos / BD
Database `medidor_ia` en `scraping-postgres` (13 tablas + `alembic_version`). Dinero **siempre centavos BIGINT**.
- **`wallets`** — saldo por `(tenant_id, external_user_id)` UNIQUE. `balance_cents`, `holds_total_cents`, `is_suspended`, `version` (optimistic lock), `min_charge_cents`, `low_balance_threshold_cents`.
- **`wallet_transactions`** — **ledger APPEND-ONLY**. `balance_after_cents` (snapshot), `request_id` (UNIQUE por wallet). `kind` ∈ {credit, debit, refund, hold, hold_release, adjustment} con CheckConstraints de signo.
- **`wallet_holds`** — reservas; `status` ∈ {active, captured, released, expired}; UNIQUE `(wallet_id, authorize_request_id)`.
- **`api_keys`** — `key_hash` (HMAC-SHA256) + `key_prefix` + `scope` (client/admin) + `revoked_at` (nunca se borra). Hoy: 5 ADMIN, 2 CLIENT.
- **`events`** — consumo append-only; base de `usage` y refunds.
- **`service_catalog`** / **`llm_pricing`** — precios vigentes por `effective_from/to`, overrides por tenant; pricing congelado al evento.
- **`refund_audit_log`**, **`tenants`**, **`projects`**, **`business_operations`**, **`system_flags`**.

## Runbook operacional
Comandos vía `ssh root@89.116.25.222`. BD: usuario `medidor`, database `medidor_ia`, contenedor `scraping-postgres`.

**1. `medidor-api` caído:** `docker logs medidor-api --tail 50` → `cd /opt/medidor_ia && docker compose -f docker-compose.vps.yml --env-file .env.prod up -d` (o `docker restart medidor-api`). Verificar `curl -fsS http://127.0.0.1:8007/health` y `.../health/db`.

**2. `medidor-jobs` unhealthy:** `docker logs medidor-jobs --tail 20`. Si ves `jobs_worker_starting` + `expire_holds_job_started` sin tracebacks → **sano** (el unhealthy es la healthcheck heredada que cura `:8000` inexistente). Reiniciar solo si hay crash loop real.

**3. Saldo/holds de un wallet:**
```bash
docker exec scraping-postgres psql -U medidor -d medidor_ia -c "SELECT id,balance_cents,holds_total_cents,currency,is_suspended FROM wallets WHERE id='<WALLET_ID>';"
docker exec scraping-postgres psql -U medidor -d medidor_ia -c "SELECT kind,amount_cents,balance_after_cents,request_id,created_at FROM wallet_transactions WHERE wallet_id='<WALLET_ID>' ORDER BY created_at DESC LIMIT 20;"
```

**4. Logs:** `docker logs medidor-api --tail 100 -f` (busca `auth_failed`, `unhandled_exception`).

**5. Migración/esquema:** `docker exec scraping-postgres psql -U medidor -d medidor_ia -tAc "SELECT version_num FROM alembic_version;"` → `0007_seed_deepseek_catalog`.

**6. Conectividad CAF→Medidor:** `curl -fsS -H "X-Api-Key: <ADMIN_KEY>" https://medidor.inovaweb.com.mx/health`. Si el dominio falla pero `127.0.0.1:8007/health` responde → problema en el reverse proxy, no en el core.

## Variables de entorno clave
En `/opt/medidor_ia/.env.prod` (mapean a `Settings`). **Sin valores.**
- `APP_ENV` (`production` activa hardening), `APP_NAME`, `LOG_LEVEL`.
- `DATABASE_URL` (+ pool size/overflow/recycle/statement_timeout).
- `ADMIN_API_KEY` — **doble propósito:** secreto del HMAC de TODAS las API keys + key admin de bootstrap. Si rota, todas las keys dejan de validar.
- `HASH_ALGORITHM` (sha256), `API_HOST`, `API_PORT` (8000).
- `AUTO_REFUND_ENABLED_DEFAULT`, `REFUND_SUSPEND_FAILURE_RATE_PCT`, `REFUND_SUSPEND_MIN_VOLUME` (anti-fraude / circuit breaker).
- `HOLD_EXPIRATION_MINUTES`.
- `DEEPSEEK_API_KEY/BASE_URL`, `PERPLEXITY_API_KEY/BASE_URL`, `LLM_UPSTREAM_TIMEOUT_SECONDS` (proxies LLM).
- `PROXY_DEFAULT_WALLETS` (wallet por defecto de los proxies; la lee el código del proxy).
- `EXPOSE_DOCS` (debe quedar `false`).

## Gotchas y pendientes conocidos
- **`medidor-jobs` "unhealthy" = falso positivo permanente.** Misma imagen que `medidor-api`; el `HEALTHCHECK` del Dockerfile hace `curl localhost:8000/health` pero el worker no sirve HTTP → falla siempre. El worker funciona; **no reiniciar** solo por el estado, confirmar con `docker logs`.
- **Puerto host 8007** (no 8000) porque Micro-Fichas ocupa el 8000.
- **`/opt/medidor_ia` no tiene `.git`** en el VPS: deploy = build local; sincronía con GitHub es manual. `[TODO: confirmar último commit desde repo con .git]`.
- **Seguridad (memoria):** una API key real `mk_prod_...` quedó commiteada en `n8n/track-medidor.workflow.json` en GitHub → **rotar / confirmar repo privado**.
- **`ADMIN_API_KEY` con doble rol** (HMAC + admin): rotarla invalida todas las keys emitidas. Documentar antes de rotar.
- **Discrepancia de moneda:** modelos con `server_default="USD"` pero el CAF crea/acredita en **MXN**. Verificar `SELECT currency,count(*) FROM wallets GROUP BY currency;` para evitar `currency_mismatch` (409) en `credit`.
- **Proxies LLM failing-open:** si el DEBIT falla tras respuesta OK del proveedor, se loguea pero **no se cobra ni bloquea** → posible consumo sin cobro; revisar logs `medidor.llm.*`.
- **`uuid7_default` es realmente uuid4** (stub); irrelevante salvo si se asume orden temporal por PK.
