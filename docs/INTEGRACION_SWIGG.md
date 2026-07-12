# Integración con CAF — Guía para **Swigg**

> **Qué es CAF:** el *Billing Engine* SaaS de Inovaweb (app `caf_app`, BD `admin_financiera`).
> Cobra créditos por consumo, lleva el saldo prepago (wallet) de cada cliente y factura.
> **Estado de Swigg:** ya está dada de alta como app (organización **1 – Inovaweb**), con su
> `SWIGG_ADMIN_KEY` activa y sus servicios/planes en el catálogo. Esta guía documenta el
> **contrato real** para que Swigg consuma la API de CAF (altas, cobros, saldo, recargas).

- **Servidor:** `89.116.25.222` · contenedor `caf_app` (puerto interno 8001 → host `:8006`) · nginx + Certbot.
- **Base de datos:** `caf_postgres` → `admin_financiera` (usuario `caf`).
- **Fecha de este documento:** 2026-07-11. Verificado directo contra el código y la BD en producción.

---

## 1. Endpoints y autenticación

**Base URL del API app-facing:** `https://admin.inovaweb.com.mx/api/v2`
(también responde en `https://app.inovaweb.com.mx/api/v2`; el `HostEnforcementMiddleware` solo
restringe `/admin/*` y `/portal/*`, **no** `/api/v2/*`).

**Auth:** header `Authorization: Bearer <API_KEY>` en **todas** las llamadas `/api/v2`.
- La llave resuelve la **organización** dueña de la petición (tabla `api_keys`, hash SHA-256).
- Swigg usa la llave legacy de entorno **`SWIGG_ADMIN_KEY`** → resuelve a **org 1 (Inovaweb)**.
- **El tenant SIEMPRE sale de la llave, nunca del body** (regla de aislamiento). No mandes `organization_id`.
- La llave vive en el `.env` de `caf_app` (secreto). **No la hardcodees**: léela del secreto/entorno.

> ⚠️ **No hay CORS.** CAF no manda `Access-Control-Allow-Origin`. Las llamadas a `/api/v2`
> deben hacerse **server-to-server (backend de Swigg → CAF)**, nunca desde el navegador del usuario.
> Si en algún momento se necesita llamar desde el front, hay que agregar CORS en CAF primero.

---

## 2. Modelo de datos (mental)

```
organización (Swigg = org 1)
  └── clients  (cada empresa/cuenta que usa Swigg)     ← se crea con /apps/onboard
        └── wallet / prepaid_ledger  (saldo en centavos MXN, débitos y créditos)
services   (catálogo de conceptos cobrables, con unit_price_cents)  ← por organización
plans      (planes; al contratar acreditan monthly_credit_cents de saldo)  ← por organización
```

- **Moneda:** todo en **centavos de MXN** (`*_cents`). Ej. `unit_price_cents=1000` = $10.00 MXN.
- **Saldo:** `balance_cents` del cliente (vista `v_client_balance`). Un cobro **debita**; una recarga o grant **acredita**.
- **Aislamiento:** un cliente pertenece a una org; la llave de Swigg solo puede operar clientes de la org 1.

---

## 3. Catálogo REAL de Swigg (org 1) — verificado en BD

### Servicios cobrables (`GET /api/v2/services`)
| `service_code` | Nombre | Unidad | Precio (centavos) | = MXN |
|---|---|---|---|---|
| `guion_ia` | Guion IA (Swigg) | guion | 500 | $5.00 |
| `imagen_ia` | Imagen IA para campaña | imagen | 1200 | $12.00 |
| `envio_video` | Envío de video (Swigg) | envio | 1000 | $10.00 |
| `video_producido` | Video producido (Swigg) | video | 1000 | $10.00 |
| `vista_video` | Vista de video (Swigg) | vista | 1000 | $10.00 |

> (La org 1 comparte más servicios con LiaForge: `scraping`, `descubrimiento`, `validacion_email`, etc.
> Swigg debe cobrar **solo los suyos**. Consulta el catálogo vivo con `GET /api/v2/services`.)

### Planes de Swigg (`plans`, org 1)
| `plan_code` | Nombre | Cuota mensual | Crédito acreditado | Free |
|---|---|---|---|---|
| `swigg_free` | Swigg Free | $0 | 5,000 cents ($50) | sí |
| `swigg_starter` | Swigg Starter | $49.00 | 50,000 cents ($500) | no |
| `swigg_pro` | Swigg Pro | $149.00 | 200,000 cents ($2,000) | no |
| `swigg_enterprise` | Swigg Enterprise | a medida | — | no |

---

## 4. Flujo de integración end-to-end

### Paso 1 — Alta de un cliente (cuando una empresa se registra en Swigg)
`POST /api/v2/apps/onboard` — alta self-service (Bearer, **sin** JWT ni datos fiscales; RFC placeholder de público en general).

```json
// Request
{
  "trade_name": "Tacos El Güero",
  "billing_email": "pagos@tacoselguero.mx",
  "plan_code": "swigg_free",
  "external_ref": "swigg_company_8842",   // id de la empresa en Swigg (idempotencia + link)
  "promo_code": "LANZAMIENTO20",           // opcional: bono de crédito
  "referral_code": "DIST-CO-01"            // opcional: distribuidor referidor (comisión)
}
```
```json
// 201 Created
{
  "client_id": 137,
  "plan_code": "swigg_free",
  "granted_cents": 5000,      // grant del plan (+ bono de promo si aplica)
  "promo_applied": false,
  "bonus_cents": 0
}
```
- **Guarda `client_id`** ligado a tu empresa (`external_ref`). Lo usarás en todos los cobros.
- **Idempotente:** reintentar con el mismo `external_ref` **no** re-acredita el grant ni la promo.

### Paso 2 — Cobrar consumo (cada vez que Swigg produce/entrega algo)
`POST /api/v2/clients/{client_id}/charge`

```json
// Request — ej. se produjo un video
{
  "service_code": "video_producido",
  "units": 1,
  "idempotency_key": "swigg_video_55901",   // recomendado: id único del evento en Swigg
  "meta": { "video_id": "55901", "campaign": "abarrotes-colima" }
}
```
```json
// 200 OK
{
  "ok": true,
  "client_id": 137,
  "service_code": "video_producido",
  "units": 1,
  "charged_cents": 1000,
  "balance_cents": 4000,
  "idempotent_replay": false
}
```
- CAF **tarifica** (precio del catálogo × `units`), **valida saldo** y **debita** en `prepaid_ledger`.
- **Saldo insuficiente →** `402 Payment Required`:
  ```json
  { "detail": { "error": "saldo_insuficiente", "balance_cents": 300, "required_cents": 1000 } }
  ```
  Swigg debe manejar el 402 (bloquear la acción / invitar a recargar).
- **Idempotencia:** si reenvías el mismo `idempotency_key`, CAF **no** cobra doble y responde `idempotent_replay: true`.
- **`units > 0`** obligatorio (422 si no).

### Paso 3 — Consultar saldo
`GET /api/v2/clients/{client_id}/balance` (o `/prepaid-balance`) → saldo actual.
`GET /api/v2/clients/{client_id}/ledger` → historial de movimientos (créditos y débitos).
`GET /api/v2/clients/{client_id}/plan-limits` → límites por servicio del plan.

### Paso 4 — Recargar saldo (comprar créditos)
`POST /api/v2/clients/{client_id}/recharge`
```json
{ "amount_cents": 20000 }   // mínimo 5000 ($50 MXN); tope = MAX_RECARGA_CENTS
```
- Devuelve una **URL de pago del Hub** (checkout). El usuario paga ahí; un **webhook**
  (`hub-payment-paid`) acredita la wallet al confirmarse. El cliente debe estar `active` y tener `hub_account_id`.

### (Alternativa) Alta completa con datos fiscales
`POST /api/v2/clients` — si Swigg captura RFC/régimen/CFDI reales (para facturar desde el inicio):
`legal_name, rfc, cfdi_use, tax_regime, zip_code, billing_email, plan_code, titular_full_name, titular_email`.
Devuelve además `user_id` y `temp_password` (acceso al portal `app.inovaweb.com.mx/portal`).

---

## 5. Reglas y "gotchas"

1. **Tenant por llave, no por body.** Nunca mandes `organization_id`; se ignora/rechaza.
2. **Todo en centavos.** No mandes pesos.
3. **Idempotencia siempre** en `charge` (usa el id del evento de Swigg) y en `onboard` (usa `external_ref`).
4. **402 = saldo insuficiente**, no error de tu lado: es flujo esperado → UI de recarga.
5. **Server-to-server** (no CORS). La `SWIGG_ADMIN_KEY` jamás debe llegar al navegador.
6. **Cobra solo los `service_code` de Swigg.** El catálogo es compartido en la org 1; no cobres servicios de LiaForge.
7. **Meta-cobro SaaS:** cada `charge` genera además un accrual interno a la org (transparente para Swigg; no requiere acción).
8. **Precios/planes** pueden cambiar: no los hardcodees, léelos de `GET /api/v2/services` y de la tabla `plans`.

---

## 6. Prueba rápida (curl, server-to-server)

```bash
CAF="https://admin.inovaweb.com.mx/api/v2"
KEY="$SWIGG_ADMIN_KEY"     # del secreto, nunca en claro en el repo

# catálogo
curl -s "$CAF/services" -H "Authorization: Bearer $KEY"

# alta de cliente
curl -s -X POST "$CAF/apps/onboard" -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"trade_name":"Demo Swigg","billing_email":"demo@swigg.mx","plan_code":"swigg_free","external_ref":"swigg_demo_1"}'

# cobro (usa el client_id devuelto)
curl -s -X POST "$CAF/clients/137/charge" -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{"service_code":"video_producido","units":1,"idempotency_key":"swigg_video_1"}'

# saldo
curl -s "$CAF/clients/137/balance" -H "Authorization: Bearer $KEY"
```

---

## 7. Checklist de implementación (lado Swigg)

- [ ] Guardar `SWIGG_ADMIN_KEY` en el secreto/entorno del backend de Swigg (no en el repo).
- [ ] Cliente HTTP server-to-server a `https://admin.inovaweb.com.mx/api/v2` con el header Bearer.
- [ ] En el **registro de empresa** → llamar `/apps/onboard` y **persistir `client_id` ↔ empresa** (idempotente por `external_ref`).
- [ ] En cada **evento cobrable** (guion_ia, imagen_ia, video_producido, envio_video, vista_video) → `/charge` con `idempotency_key` = id del evento.
- [ ] Manejar **402** → UI/flujo de recarga (`/recharge` → redirigir a la URL de pago del Hub).
- [ ] Pantallas de **saldo/consumo** con `/balance` y `/ledger`.
- [ ] Reintentos con **idempotencia** (nunca doble cobro).
- [ ] Pruebas E2E contra un cliente de prueba (plan `swigg_free`) antes de producción.

---

## 8. Referencias de código (en `caf_app`, para verificar)

- Auth/tenant: `app/core/tenancy.py` (`resolve_app_org`, `assert_client_in_org`).
- API app-facing: `app/routers/api_router.py` (prefix `/api/v2`): `/clients`, `/clients/{id}/balance`,
  `/clients/{id}/charge`, `/clients/{id}/recharge`, `/clients/{id}/ledger`, `/clients/{id}/plan-limits`,
  `/services`, `/apps/onboard`.
- Config/llaves: `app/core/config.py` (`SWIGG_ADMIN_KEY`, `ADMIN_DOMAIN`, `PORTAL_DOMAIN`).
- Onboarding: `app/services/onboarding.py`. Prepago/recarga: `app/services/prepago.py`.
- Catálogo (BD): tablas `services`, `plans`, `promotions`, `prepaid_ledger`, `clients`, `api_keys`.
