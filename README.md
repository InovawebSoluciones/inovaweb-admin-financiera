# inovaweb-admin-financiera

Centro de Administración Financiera (CAF) — módulo Nivel 2 de la plataforma
Inovaweb que convierte la infraestructura técnica de los 4 cores Nivel 1
(medidor, hub-pasarelas, finanzas-core, centro-mensajes) en un producto
comercial completo: onboarding atómico, catálogos, planes, promociones,
cobranza con CFDI 4.0, portal cliente, tableros internos.

**Estado (2026-06-14):** **saldo prepago NATIVO del CAF** (`prepaid_ledger` + `v_client_balance`; el Medidor queda como medidor puro) con **cobro pay-per-use** (`POST /clients/{id}/charge`, 402 si no alcanza) y **onboarding app-facing self-service** (`POST /apps/onboard`, Bearer por app). Apps consumidoras vivas: **LiaForge/Scraping** y **Swigg**, cada una con su Bearer. Sigue vigente: flujo de pago E2E (recarga → Hub → webhook HMAC → CAF → Medidor → Finanzas), onboarding atómico por operador (saga), billing por consumo (`price_catalog`), hardening H1-H5, frontend Jinja2+HTMX. Repo VPS↔GitHub alineados (`af0e078`). Pendiente: DNS/TLS `admin/app.inovaweb.com.mx`, proveedor email en Centro de Mensajes, facturación CFDI (Ecofile, sprint 4). Ver ADR-015/016/017 y `CLAUDE.md §12`.

---

## 1. Arquitectura

### 1.1 Posición en la plataforma

```
NIVEL 1 - APIs core (en producción)
├─ medidor.inovaweb.com.mx          (wallets prepago + mide cada llamada a LLM
│                                    y cobra el costo en pesos/centavos MXN —
│                                    fuente única del costo de IA por cliente)
├─ hub.inovaweb.com.mx              (cobros tarjeta/SPEI/OXXO)
├─ finanzas.inovaweb.com.mx         (ledger inmutable)
└─ mensajes.inovaweb.com.mx         (email/WhatsApp/push)

NIVEL 2 - servicios comerciales
└─ admin-financiera (ESTE PROYECTO)
   ├─ admin.inovaweb.com.mx         operador interno (UI HTMX)
   └─ app.inovaweb.com.mx           portal cliente externo (UI HTMX)

NIVEL 3 - apps cliente (consumidoras de la plataforma)
├─ WebEscolar / MicroFichas / Scraping / Ecofile
```

Un solo backend FastAPI sirve los dos dominios (`admin` y `app`). El
middleware `HostEnforcementMiddleware` enruta por `Host` header y bloquea
cross-domain. Caddy del stack n8n hace TLS y reverse proxy.

### 1.2 Componentes del repo

| Componente | Ruta | Responsabilidad |
|---|---|---|
| Entry FastAPI | `app/main.py` | Wiring de routers, middlewares, static |
| Configuración | `app/core/config.py` | `pydantic-settings` con fail-fast |
| Base de datos | `app/core/database.py` | AsyncEngine + `get_db` |
| Auth JWT | `app/core/jwt_auth.py` | Login, cookies httpOnly, refresh rotation |
| Passwords | `app/core/password.py` | Argon2id |
| Audit log | `app/core/audit.py` | Writer al log inmutable |
| Observabilidad | `app/core/observability.py` | Logging JSON + request-id |
| Clientes cores | `app/core/clients/*` | medidor / hub / finanzas / messages / scraping / pac |
| Router salud | `app/routers/health_router.py` | `/health`, `/health/db` |
| Router auth | `app/routers/auth_router.py` | `/login`, `/logout`, `/signup-request` |
| Router admin | `app/routers/admin_router.py` | `/admin/*` (UI operador) |
| Router portal | `app/routers/portal_router.py` | `/portal/*` (UI cliente) |
| Router API | `app/routers/api_router.py` | `/api/v2/*` (JSON) |
| Router webhooks | `app/routers/webhooks_router.py` | `/webhooks/pac`, `/webhooks/hub-payment-paid` |
| Servicio onboarding | `app/services/onboarding.py` | Saga de alta atómica cross-core + link Scraping + token activación |
| Servicio prepago | `app/services/prepago.py` | Cargo Hub → acreditación wallet Medidor (piloto) |
| Servicio billing | `app/services/billing.py` | Cierre mensual: plan + overage + IA + mensajes por canal |
| Servicio pricing | `app/services/pricing.py` | Tarificación a precio público vía `price_catalog` |
| Servicio invoicing | `app/services/invoicing.py` | Emisión + timbrado (Ecofile, pendiente sprint 4) |
| Servicio promotions | `app/services/promotions.py` | Cupones, descuentos, volumen |
| Worker mensual | `app/workers/monthly_closing.py` | Job nocturno día 1 |
| Worker reintento | `app/workers/invoice_retry.py` | Reintento timbrado fallido |
| Worker vencimientos | `app/workers/overdue_notifier.py` | Recordatorios de mora |
| Templates | `app/templates/` | Jinja2 + HTMX (admin/ + portal/) |
| Schema SQL | `database/001_initial_schema.sql` | Tablas, roles, FKs |
| Seguridad SQL | `database/002_security_constraints.sql` | Triggers append-only + auditoría |
| Tests | `tests/` | health, jwt, password, onboarding, promotions |

---

## 2. Stack tecnológico

| Capa | Tecnología | Versión |
|---|---|---|
| Lenguaje | Python | 3.12 |
| Framework HTTP | FastAPI + uvicorn | 0.115 / 0.32 |
| ORM | SQLAlchemy async + psycopg | 2.0.36 / 3.2 (binary) |
| Base de datos | PostgreSQL | 16 |
| HTTP client | httpx async | 0.27 |
| UI server-side | Jinja2 + HTMX + Tailwind | — (sin build Node) |
| Auth | JWT (`python-jose`) + Argon2id (`argon2-cffi`) | 3.3 / 23.1 |
| Cripto | cryptography (AES-256-GCM) | 43.0 |
| XML CFDI | lxml | 5.3 |
| Validación config | pydantic-settings | 2.6 |
| Contenedores | Docker + Caddy (stack n8n compartido) | — |
| Lint / format | ruff | 0.7 |
| Tests | pytest + pytest-asyncio | 8.3 |

---

## 3. Variables de entorno

Todas las variables marcadas con ✅ son obligatorias. Sin ellas, el contenedor
falla al arrancar (`pydantic-settings` con `Field(...)`).

| Variable | Obligatoria | Default | Notas |
|---|---|---|---|
| `ENV` | ✅ | — | `dev` / `staging` / `prod` |
| `LOG_LEVEL` | — | `INFO` | — |
| `PORT` | — | `8001` | Puerto dentro del contenedor |
| `DATABASE_URL` | ✅ | — | `postgresql+psycopg://...` |
| `POSTGRES_USER` | — | `caf` | — |
| `POSTGRES_PASSWORD` | ✅ | — | — |
| `POSTGRES_DB` | — | `admin_financiera` | — |
| `AES_KEY` | ✅ | — | Base64 32 bytes (AES-256-GCM) |
| `JWT_SECRET` | ✅ | — | HMAC SHA-256, mín. 32 bytes |
| `JWT_ACCESS_TTL_MIN` | — | `15` | — |
| `JWT_REFRESH_TTL_DAYS` | — | `30` | — |
| `ADMIN_DOMAIN` | — | `admin.inovaweb.com.mx` | — |
| `PORTAL_DOMAIN` | — | `app.inovaweb.com.mx` | — |
| `MEDIDOR_BASE_URL` | ✅ | — | `https://medidor.inovaweb.com.mx` |
| `MEDIDOR_API_KEY` | ✅ | — | Scope `admin`, label `core-admin-financiera` |
| `HUB_BASE_URL` | ✅ | — | `https://hub.inovaweb.com.mx` |
| `HUB_API_KEY` | ✅ | — | Scope `*` |
| `HUB_WEBHOOK_SECRET` | ✅ (prod) | — | Secreto HMAC dedicado del webhook del Hub. Obligatorio en prod (fail-fast); en dev cae a `HUB_API_KEY` |
| `HUB_WEBHOOK_TOLERANCE_SEC` | — | `300` | Ventana anti-replay del timestamp firmado |
| `MAX_RECARGA_CENTS` | — | `50000000` | Tope superior de recarga autoservicio (centavos) |
| `MESSAGES_BASE_URL` | ✅ | — | `https://mensajes.inovaweb.com.mx` |
| `MESSAGES_API_KEY` | ✅ | — | Scope `*` (admin master) |
| `FINANZAS_BASE_URL` | ✅ | — | `https://finanzas.inovaweb.com.mx` |
| `FINANZAS_API_KEY` | ✅ | — | Scope `*` (admin master) |
| `PAC_PROVIDER` | ✅ | `facturama` | `facturama` / `factible` / `edicom` |
| `PAC_BASE_URL` | ✅ | — | URL del PAC |
| `PAC_API_KEY` | ✅ | — | Credencial del PAC |
| `PAC_API_SECRET` | ✅ | — | Secret del PAC |
| `RFC_EMISOR` | ✅ | — | RFC de Inovaweb |
| `CER_PATH` | ✅ | `/secrets/csd.cer` | Certificado de Sello Digital |
| `KEY_PATH` | ✅ | `/secrets/csd.key` | Llave privada del CSD |
| `KEY_PASSWORD` | ✅ | — | Contraseña del CSD |
| `SCRAPING_ADMIN_KEY` | ✅ | — | Bearer de la app **LiaForge/Scraping** para los endpoints app-facing (`_verify_app_key`) |
| `SWIGG_ADMIN_KEY` | — | — | Bearer de la app **Swigg** (2ª app consumidora). Una app nueva = una llave nueva aquí + append en `_verify_app_key` |
| `HTTP_TIMEOUT_SEC` | — | `10.0` | Timeout default cores+PAC |
| `HTTP_RETRIES` | — | `3` | — |

Plantilla completa en `.env.example`.

---

## 4. Cómo correr localmente

### 4.1 Pre-requisitos

- Docker 24+ y Docker Compose v2
- (Opcional) Python 3.12 + `pip` si quieres correr tests fuera del contenedor

### 4.2 Setup inicial

```bash
# 1. clonar el repo
git clone https://github.com/InovawebSoluciones/inovaweb-admin-financiera.git
cd inovaweb-admin-financiera

# 2. preparar .env
cp .env.example .env
# editar .env y poner valores reales (no usar los CHANGE_ME)

# 3. generar AES_KEY y JWT_SECRET
python -c "import secrets; print('AES_KEY=' + secrets.token_urlsafe(32))"
python -c "import secrets; print('JWT_SECRET=' + secrets.token_urlsafe(32))"

# 4. (solo prod) colocar el CSD en ./secrets/
#    secrets/csd.cer y secrets/csd.key con KEY_PASSWORD en .env

# 5. levantar el stack
docker compose up -d --build
```

### 4.3 Verificación

```bash
# liveness
curl http://localhost:8006/health

# readiness (Postgres OK)
curl http://localhost:8006/health/db

# docs (solo en ENV=dev)
open http://localhost:8006/docs
```

### 4.4 Tests

```bash
# dentro del contenedor de la app
docker exec -it caf_app pytest -v

# o local con venv
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -v
```

### 4.5 Lint

```bash
ruff check app tests
ruff format app tests
```

---

## 5. URLs de producción

| Dominio | Audiencia | Notas |
|---|---|---|
| `https://admin.inovaweb.com.mx` | Operador interno | Roles `super_admin`, `finanzas`, `lectura` |
| `https://app.inovaweb.com.mx` | Cliente externo | Roles `cliente_titular`, `cliente_usuario` |

Ambos dominios golpean el mismo contenedor (`caf_app:8001`). El middleware
`HostEnforcementMiddleware` decide qué rutas se sirven en cada uno.

VPS: Contabo `89.116.25.222`, puerto host `8006` → contenedor `8001`. Los
puertos `8000-8005` están ocupados por los cores Nivel 1 y n8n.

---

## 6. Endpoints principales

### 6.1 Públicos
- `GET /health`, `GET /health/db`
- `GET /login`, `POST /login`, `POST /logout`
- `GET /signup-request`, `POST /signup-request`

### 6.2 Admin (UI HTMX, requiere JWT con rol interno)
- `GET /admin/dashboard` — ingresos consolidados
- `GET /admin/clients` · `POST /admin/clients` · `PATCH /admin/clients/{id}`
- `POST /admin/clients/{id}/suspend`
- `GET /admin/catalog/{products|services|plans|promotions}`
- `GET /admin/billing/invoices` · `POST /admin/billing/run-closing`
- `GET /admin/audit-log`

### 6.3 Portal cliente (UI HTMX, requiere JWT con rol cliente)
- `GET /portal/dashboard` — saldo + consumo
- `GET /portal/usage` · `GET /portal/invoices`
- `GET /portal/invoices/{id}.pdf` · `GET /portal/invoices/{id}.xml`
- `POST /portal/recharge` — abre flujo en Hub-Pasarelas
- `GET /portal/account`

### 6.4 API JSON (`/api/v2/*`)
- `POST /api/v2/clients` — alta atómica programática (Saga)
- `GET /api/v2/clients/{id}/balance` — saldo y consumo IA del cliente
  (el CAF consulta al medidor, nunca duplica el saldo ni el costo de IA;
  el medidor es la fuente de verdad)
- `GET /api/v2/reports/income` — agregados de ingreso
- `POST /api/v2/billing/run-closing` — trigger manual de cierre

#### App-facing (autenticados por **Bearer** de app, no JWT — `_verify_app_key`)
- `POST /api/v2/apps/onboard` — alta self-service (cliente + wallet + plan; sin JWT/fiscales). Ver ADR-017.
- `POST /api/v2/clients/{id}/charge` — cobro pay-per-use: tarifica `services.unit_price_cents`, debita
  `prepaid_ledger`, **402 `saldo_insuficiente`** si no alcanza. Idempotente + advisory lock. Ver ADR-016.
- `GET /api/v2/clients/{id}/prepaid-balance` — saldo prepago **nativo del CAF** (`v_client_balance`).
- `GET /api/v2/clients/{id}/ledger` — movimientos del `prepaid_ledger` + consumo del mes.
- `GET /api/v2/clients/{id}/plan-limits` — límites del plan + precios (solo lectura; medición de uso).
- `GET /api/v2/services` — catálogo de servicios activos con precio unitario.

### 6.5 Webhooks
- `POST /webhooks/pac` — timbrado exitoso / fallido (diferido con CFDI)
- `POST /webhooks/hub-payment-paid` — pago confirmado por el Hub-Pasarelas.
  Verifica HMAC + timestamp firmado (anti-replay), reclama el pago con
  idempotencia a nivel BD (`uq_payments_hub`), valida `purpose`/`amount` contra
  el intento local y acredita la wallet del cliente en el Medidor (`credit`,
  idempotente). Ver `app/services/prepago.py`.

---

## 7. Convenciones firmes

- **Dinero en BIGINT (centavos).** Nunca floats.
- **Append-only en finanzas:** `invoices`, `payments`, `adjustments`,
  `audit_log` no se modifican; correcciones generan nuevas entradas (notas
  de crédito, ajustes con motivo). Triggers en BD lo enforcen
  (`database/002_security_constraints.sql`).
- **Auditoría obligatoria:** cada escritura registra actor, IP, timestamp,
  valor_anterior, valor_nuevo.
- **Saga atómica de onboarding (prepago):** el alta crea la wallet del
  cliente en el Medidor; si falla, se compensa (`delete_wallet`) y la falla
  queda registrada en `audit_log`. No emite 4 API keys por cliente: los
  cores son multi-tenant resueltos por la llave admin master del CAF.
- **Argon2id** para passwords. JWT en cookie httpOnly, SameSite=Strict,
  access 15 min + refresh 30 días con rotación.
- **Cobro prepago en el piloto:** el cliente recarga su wallet en el Medidor
  y el consumo se debita en vivo (ADR-010). La cobranza mensual pospago +
  **CFDI 4.0** vía PAC quedan diferidas hasta seleccionar PAC (el código de
  cierre/timbrado existe pero no se ejercita en el piloto).

---

## 8. Deploy

Ver `docs/DEPLOY.md` para el procedimiento completo (SSH, SCP, migraciones
SQL vía PowerShell, rollback). Resumen:

```bash
ssh root@89.116.25.222
cd /opt/inovaweb-admin-financiera
git pull
docker compose up -d --build
```

---

## 9. Documentos del proyecto

| Doc | Ubicación |
|---|---|
| Contexto técnico completo | `docs/inovaweb-admin-financiera-proyecto-tecnico.md` |
| Contrato con cores Nivel 1 | `docs/01-admin-financiera-integracion-cores.md` |
| Decisiones de arquitectura | `docs/ADR.md` |
| **Los 4 módulos que administra el CAF** | `docs/modulos/` (medidor, finanzas, centro-mensajes, hub-pasarelas + índice) |
| Runbook operacional | `docs/RUNBOOK.md` |
| Guía de deploy | `docs/DEPLOY.md` |
| Modelo de seguridad | `SECURITY.md` |
| Convenciones para Claude | `CLAUDE.md` |
| Changelog | `CHANGELOG.md` |

Repositorio: https://github.com/InovawebSoluciones/inovaweb-admin-financiera
