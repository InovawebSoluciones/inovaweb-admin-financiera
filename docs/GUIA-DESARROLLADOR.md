# Guía del Desarrollador — inovaweb-admin-financiera (CAF)

Onboarding técnico para trabajar en el CAF (Centro de Administración Financiera),
el servicio Nivel 2 que orquesta los 4 cores de Inovaweb.

> Antes de leer esto, lee [ARQUITECTURA-GLOBAL.md](ARQUITECTURA-GLOBAL.md) para
> entender cómo encaja el CAF en la plataforma.

---

## 1. Arranque local desde cero

```bash
# 1. Variables de entorno
cp .env.example .env
#   Edita .env: DATABASE_URL, POSTGRES_PASSWORD, AES_KEY (base64 32 bytes),
#   JWT_SECRET (>=32 chars), y las 4 *_API_KEY de los cores. En prod además
#   HUB_WEBHOOK_SECRET, PAC_*, RFC_EMISOR, KEY_PASSWORD.

# 2. Levantar (Postgres + backend)
docker compose up -d --build

# 3. Migraciones (orden numérico)
#   database/001_initial_schema.sql
#   database/002_security_constraints.sql
#   database/003_seed_scraping_plans.sql
#   database/004_payments_idempotency.sql

# 4. Verificar
curl http://localhost:8006/health
curl http://localhost:8006/health/db

# 5. Tests
pytest
```

El backend escucha en el puerto **8001** dentro del contenedor; el host lo publica
en **8006**. En el VPS, Nginx enruta `admin.inovaweb.com.mx` y `app.inovaweb.com.mx`
al mismo backend (distingue por `Host` header).

---

## 2. Mapa del código

```
app/
├── main.py                  Wiring FastAPI: routers, middleware, lifespan
├── core/
│   ├── config.py            pydantic-settings con fail-fast (SecretStr)
│   ├── database.py          AsyncEngine + get_db
│   ├── jwt_auth.py          login_user, issue_access/refresh, require_roles, cookies
│   ├── password.py          Argon2id hash/verify
│   ├── audit.py             write_event + bind de actor/IP/request_id por request
│   ├── observability.py     logging JSON + request-id
│   └── clients/             Clientes HTTP a los 4 cores + PAC
│       ├── _base.py         CoreClient: timeout, retries, Bearer
│       ├── medidor_client.py    wallet, credit, balance, usage     ⚠️ ver §6 (C1)
│       ├── hub_client.py        POST /hub/v1/charge
│       ├── finanzas_client.py   POST /v1/ledger/entries, balance, totals
│       ├── messages_client.py   email + whatsapp                   ⚠️ whatsapp 501
│       └── pac_client.py        Protocol + Facturama (CFDI 4.0)
├── routers/
│   ├── health_router.py     /health /health/db
│   ├── auth_router.py       /login /logout /signup-request
│   ├── admin_router.py      /admin/* (UI operador, HTMX)
│   ├── portal_router.py     /portal/* (UI cliente)
│   ├── api_router.py        /api/v2/* (JSON)
│   └── webhooks_router.py   /webhooks/{pac|hub-payment-paid}
├── services/
│   ├── onboarding.py        Saga de alta atómica (wallet + user + sub)
│   ├── prepago.py           Cargo Hub + acreditación + asiento Finanzas (idempotente)
│   ├── billing.py           Cierre mensual + cálculo de cargos
│   ├── invoicing.py         Emisión + timbrado PAC
│   └── promotions.py        Cupones + descuentos
├── workers/
│   ├── monthly_closing.py   Job día 1 del mes
│   ├── invoice_retry.py     Reintento de timbrado
│   └── overdue_notifier.py  Recordatorios de vencimiento
├── templates/               Jinja2 + HTMX (admin/ + portal/)
└── database/                4 migraciones SQL
```

---

## 3. Contratos de integración (qué consume el CAF)

Detalle en [01-admin-financiera-integracion-cores.md](01-admin-financiera-integracion-cores.md)
y en [ARQUITECTURA-GLOBAL.md §6](ARQUITECTURA-GLOBAL.md). Resumen:

| Core | Cliente | Operaciones | Auth | Idempotencia |
|---|---|---|---|---|
| Medidor | `medidor_client.py` | crear wallet, **credit**, balance, usage | Bearer ADMIN | `request_id=caf-recharge-{id}` |
| Hub | `hub_client.py` | `POST /hub/v1/charge` | Bearer | `metadata.purpose` + request |
| Finanzas | `finanzas_client.py` | `POST /v1/ledger/entries`, balance, totals | Bearer | `source_ref` determinista |
| Mensajes | `messages_client.py` | email (template), whatsapp | Bearer | — |
| PAC | `pac_client.py` | stamp/cancel/download CFDI | HTTP Basic | — |

**Convención `source_ref` para Finanzas** (`finanzas_client.py:18-24`):
`caf-invoice-{id}-payment`, `caf-recharge-{id}`, `caf-manual-adj-{id}`,
`caf-sub-{client}-{yyyymm}`, `{original}-reversal`.

---

## 4. Flujos principales paso a paso

- **Onboarding (Saga):** `services/onboarding.py` → ver [ARQUITECTURA-GLOBAL §5.3].
  Punto de compensación: la wallet creada en el Medidor se borra (best-effort) si
  falla el alta del usuario. El audit de fallo se persiste en sesión propia.
- **Pago/recarga:** `services/prepago.py` → `initiate_charge` (crea intento + audit
  `recharge.initiated`) y `process_paid_event` (idempotente por BD + correlación
  purpose/amount + credit Medidor + asiento Finanzas + email).
- **Cierre mensual:** `services/billing.py:run_monthly_closing` (genera invoices
  draft por suscripción, IVA 16% con redondeo entero).
- **Timbrado:** `services/invoicing.py` + `pac_client.py` (CFDI 4.0 vía Facturama).

---

## 5. Convenciones del proyecto

- **Dinero:** centavos BIGINT en todo; nunca floats. Triggers de BD enforcen.
- **Append-only:** `audit_log`, `payments`, `adjustments` no se mutan; `invoices`
  bloquea campos financieros (corrección = nueva factura `supersedes_id`).
- **Auth:** cada endpoint declara su rol mínimo vía `require_roles(...)`.
- **Multi-tenant:** el portal filtra siempre por `user.client_id`.
- **Secretos:** `SecretStr`, nunca en logs. Fail-fast si falta una var obligatoria.
- **Idempotencia:** `request_id`/`source_ref` deterministas hacia los cores.
- **Webhooks:** validar firma + timestamp ANTES de parsear el body.

---

## 6. Trampas conocidas (lee esto antes de tocar integraciones)

- ⚠️ **C1 (CRÍTICO):** `medidor_client.py` acredita en `/admin/v1/wallets/{id}/credit`
  y borra en `/admin/v1/wallets/{id}`, pero el Medidor expone credit en
  **`/v1/wallets/{id}/credit`** y no tiene esas rutas `/admin/v1`. Verifícalo contra
  el Medidor real antes de dar por bueno el flujo prepago. Ver [OWASP.md §0](OWASP.md).
- ⚠️ **WhatsApp** (`messages_client.py:104`, TODO): el Centro de Mensajes responde
  501 a `/v1/messages/whatsapp`. No dependas de ese canal aún.
- ⚠️ **Plantillas:** `caf-pago-confirmado` etc. deben sembrarse en el Centro de
  Mensajes antes del primer envío (si no, 404).
- ⚠️ **Onboarding** no es idempotente por `request_id` (parámetro aceptado pero no
  usado, `onboarding.py:79`). Reenvíos pueden crear clientes duplicados (salvo que
  choque el UNIQUE de `rfc`).
- ⚠️ **OneDrive** deja archivos "solo nube"/truncados; hidrata y verifica en el host
  real antes de testear o commitear.
- ⚠️ **Caddyfile** en la raíz es referencia histórica; el reverse proxy real es
  Nginx en el VPS.

---

## 7. Funciones públicas sin docstring (deuda documentada)

- `services/onboarding.py:73 onboard_client` — falta docstring del patrón Saga.
- `services/promotions.py redeem_coupon` — falta docstring de los `ValueError`.

*Guía del desarrollador — auditoría global Inovaweb 2026-06-06.*
