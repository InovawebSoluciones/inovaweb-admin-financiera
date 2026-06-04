# Centro de Administración Financiera — Contrato de integración con los 4 cores Nivel 1

**Estado:** especificación para sprints iniciales del proyecto. Los cuatro cores
Nivel 1 (Medidor IA, Hub de Pasarelas, Centro de Mensajes y Finanzas-Core) ya
están en producción. Este documento define cómo el CAF los consume, qué llaves
necesita, qué endpoints invoca y bajo qué reglas firmes.

---

## 1. Vista rápida

```
                     ┌──────────────────────────────────────────┐
                     │  CAF — admin.inovaweb.com.mx             │
                     │       app.inovaweb.com.mx                │
                     │                                          │
   Operador interno  │  - Onboarding atómico (Saga)             │
   y Cliente externo │  - Tableros consolidados                 │
        ───────────► │  - Cierre mensual + CFDI                 │
                     │  - Portal cliente con recarga            │
                     └──────────────────────────────────────────┘
                          │           │            │           │
                  ┌───────┘    ┌──────┘     ┌──────┘   ┌───────┘
                  ▼            ▼            ▼          ▼
              ┌────────┐  ┌────────┐  ┌────────┐  ┌─────────┐
              │MEDIDOR │  │  HUB   │  │FINANZAS│  │MENSAJES │
              │  IA    │  │PASAREL │  │  CORE  │  │         │
              └────────┘  └────────┘  └────────┘  └─────────┘
              wallets    cobros      ledger      notifica
              saldo      tarjetas    inmutable   cliente
              consumo IA SPEI/OXXO   balance     plantillas
```

El CAF es el único consumidor que orquesta los cuatro cores en una sola
transacción atómica (alta de cliente). Cualquier escritura en finanzas-core
con `source_slug=manual` debe pasar por el CAF para garantizar auditoría.

---

## 2. Llaves de aplicación que el CAF necesita

Antes del primer arranque del CAF, el operador interno debe emitir vía SQL en
cada core las siguientes llaves y guardarlas en el `.env` del CAF:

| Variable en `.env` | Core | Scope | Etiqueta |
|---|---|---|---|
| `MEDIDOR_API_KEY` | medidor.inovaweb.com.mx | `admin` | `core-admin-financiera` |
| `HUB_API_KEY` | hub.inovaweb.com.mx | `*` (admin) | `core-admin-financiera` |
| `FINANZAS_API_KEY` | finanzas.inovaweb.com.mx | `*` (admin master) | `core-admin-financiera` |
| `MESSAGES_API_KEY` | mensajes.inovaweb.com.mx | `*` (admin master) | `core-admin-financiera` |

Procedimiento de bootstrap: emitir las 4 llaves manualmente vía SQL, igual que
se hizo para el centro-mensajes en mayo de 2026. Una vez que el CAF esté
operativo, futuros clientes obtendrán sus llaves a través del propio CAF y
ya no por SQL.

---

## 3. Integración con Medidor IA

### 3.1 Naturaleza

El Medidor IA es una **wallet prepago autoritativa**: mantiene el saldo del
cliente, lo valida antes de consumir, lo debita al consumir y registra todo en
un ledger append-only. CAF nunca duplica saldo ni consumo localmente; los pide
cuando los necesita.

El modelo del piloto Scraping es **PREPAGO** (ver `docs/ADR.md` ADR-011):

1. **CAF (llave de scope `ADMIN`)** crea la wallet del cliente y la acredita
   cuando se confirma una recarga vía Hub-Pasarelas.
2. **El consumidor (la app Scraping, llave de scope `CLIENT`)** ejecuta el par
   `authorize → finish` por cada operación cobrable. `authorize` crea un HOLD
   y **valida el saldo**; si es insuficiente, rechaza y ahí es donde se impone
   el bloqueo por saldo agotado. `finish` captura el hold y descuenta el saldo.

El CAF no consume IA ni dispara `authorize/finish`; eso lo hace la app Nivel 3.
El CAF solo crea/acredita la wallet y lee balance/consumo para tableros.

### 3.2 Identidad de la wallet

La wallet se identifica por la tupla **`(tenant_id, external_user_id)`**, que es
UNIQUE en el Medidor. Para el piloto:

| Campo | Valor |
|---|---|
| `tenant_id` | `inovaweb` |
| proyecto | `scraping` |
| `external_user_id` | `Company.id` de Scraping (= `company_id`) |

El mapeo completo de identidad cross-core (`clients.id` del CAF ↔
`Company.caf_client_id` ↔ `Company.id` ↔ `external_user_id` de la wallet) está
documentado en `docs/ADR.md` ADR-011. El CAF guarda el `id` de wallet devuelto
en `clients.medidor_account_id`.

### 3.3 Autenticación y scopes

Auth por API key en header `X-Api-Key` (o `Authorization: Bearer <key>`). El
Medidor distingue dos scopes:

| Scope | Operaciones | Quién la usa |
|---|---|---|
| `ADMIN` | crear wallet, `credit` (recarga), suspend/unsuspend, refund | **CAF** (`MEDIDOR_API_KEY`) |
| `CLIENT` | `authorize`, `finish`, `release`, `quote`, `events/track` | **app consumidora** (Scraping) |

El CAF solo posee la llave `ADMIN`. La llave `CLIENT` la usa la app Nivel 3 y
se emite/entrega por separado (cableado a Scraping en TASK-21).

### 3.4 Endpoints que invoca el CAF (scope ADMIN)

**En el alta del cliente — crear wallet:**

```http
POST https://medidor.inovaweb.com.mx/v1/wallets
X-Api-Key: <MEDIDOR_API_KEY>
Content-Type: application/json

{
  "tenant_id": "inovaweb",
  "external_user_id": "<Company.id de Scraping>",
  "currency": "MXN",
  "metadata": {
    "caf_client_id": "<uuid-cliente-en-caf>",
    "project": "scraping",
    "razon_social": "Norma Sánchez Consultoría"
  }
}
```

Respuesta: `{ "id": "<wallet_id>", "balance_cents": 0, ... }`. La identidad
`(tenant_id, external_user_id)` es UNIQUE: un segundo POST con la misma tupla
devuelve conflicto (idempotente respecto a la creación).

**En recarga confirmada (tras webhook del Hub) — acreditar saldo:**

```http
POST https://medidor.inovaweb.com.mx/v1/wallets/{wallet_id}/credit
X-Api-Key: <MEDIDOR_API_KEY>
Content-Type: application/json

{
  "amount_cents": 40000,
  "currency": "MXN",
  "request_id": "caf-recharge-RCH-2026-06-0042",
  "reason": "Recarga via Hub-Pasarelas, transacción htx_xyz",
  "metadata": { "hub_transaction_id": "htx_xyz" }
}
```

`credit` es **idempotente por `request_id`**: `UNIQUE(wallet_id, request_id)`.
Un webhook duplicado del Hub no produce doble acreditación.

**En consulta de saldo para tableros / portal cliente:**

```http
GET https://medidor.inovaweb.com.mx/v1/wallets/{wallet_id}/balance
X-Api-Key: <MEDIDOR_API_KEY>
```

Respuesta:

```json
{
  "balance_cents": 38500,
  "holds_total": 1200,
  "disponible_cents": 37300
}
```

`disponible_cents = balance_cents - holds_total` (saldo libre tras descontar
los HOLDs activos de operaciones en vuelo).

**En consulta de consumo del periodo:**

```http
GET https://medidor.inovaweb.com.mx/v1/usage?from_ts=2026-05-01T00:00:00Z&to_ts=2026-06-01T00:00:00Z&wallet_id=<wallet_id>
X-Api-Key: <MEDIDOR_API_KEY>
```

**Admin — suspender / reactivar wallet (por mora o decisión operativa):**

```http
POST https://medidor.inovaweb.com.mx/v1/wallets/{wallet_id}/suspend
POST https://medidor.inovaweb.com.mx/v1/wallets/{wallet_id}/unsuspend
X-Api-Key: <MEDIDOR_API_KEY>
```

Una wallet suspendida rechaza nuevos `authorize` aunque tenga saldo.

### 3.5 Endpoints del consumidor (scope CLIENT) — referencia

El CAF **no** llama estos; los documenta porque definen cómo se cobra el saldo
que el CAF acredita. Los ejecuta la app Scraping (cableado en TASK-21):

| Endpoint | Qué hace |
|---|---|
| `POST /v1/operations/quote` | Estima el costo de una operación sin reservar saldo. |
| `POST /v1/operations/authorize` | Pre-check: crea un **HOLD** y **valida saldo**. Rechaza si es insuficiente (= bloqueo por saldo agotado). Devuelve `operation_id`. |
| `POST /v1/operations/finish` | Captura el hold → **DEBIT**: descuenta el saldo. Idempotente por `request_id`. |
| `POST /v1/operations/release` | Libera un hold sin cobrar (operación cancelada/fallida). |
| `POST /v1/events/track` | Telemetría de uso. **NO cobra**, solo alimenta `/usage`. |
| `POST /v1/events/refund` | Reembolso de un evento ya cobrado (genera entrada inversa en el ledger). |

Flujo típico del consumidor: `authorize` (reserva + valida) → ejecuta el trabajo
→ `finish` (cobra) o `release` (no cobra). Si `authorize` devuelve saldo
insuficiente, la app no ejecuta el trabajo: ahí termina el draw-down.

### 3.6 Modelo interno del Medidor (garantías que el CAF asume)

- **Ledger append-only** `wallet_transactions`: cada credit/debit/hold/release
  es una fila inmutable.
- **Balance materializado** sobre el ledger, con **locking optimista** para
  resolver concurrencia de operaciones simultáneas sobre la misma wallet.
- **Idempotencia** por `UNIQUE(wallet_id, request_id)` en credit y finish.

### 3.7 Idempotencia desde el CAF

`request_id` del CAF al Medidor sigue patrón `caf-recharge-<RCH-id-local>`.
Es UNIQUE por wallet en el Medidor: garantiza que un webhook duplicado del Hub
no produzca doble acreditación.

### 3.8 Política ante fallo

Reintento automático del CAF con backoff exponencial 3 intentos. Si persiste,
se encola en tabla `medidor_retry_queue` del CAF con reintento cada 60s hasta
8 veces. Tras agotar, queda en estado `manual` para revisión humana.

---

## 4. Integración con Hub de Pasarelas

### 4.1 Naturaleza

CAF inicia cobros para recargas del cliente. Hub procesa el pago y devuelve
webhook al CAF al confirmarse. CAF nunca toca tarjetas directamente.

### 4.2 Endpoints invocados

**Al alta del cliente (configurar pasarela default):**

Hoy el Hub no tiene endpoint admin de creación. Mientras se construye, el CAF
inserta vía SQL la configuración de pasarela del cliente. **Pendiente:** el
Hub debe exponer `POST /admin/hub/v1/companies` con su scope correspondiente.

**Al iniciar recarga desde el portal:**

```http
POST https://hub.inovaweb.com.mx/hub/v1/charge
X-API-Key: <HUB_API_KEY>
Content-Type: application/json

{
  "gateway": "conekta",
  "external_user_id": "client-<uuid-caf>",
  "amount": 40000,
  "currency": "MXN",
  "description": "Recarga de saldo plataforma Inovaweb",
  "metadata": {
    "caf_client_id": "<uuid>",
    "caf_recharge_id": "RCH-2026-06-0042",
    "purpose": "wallet_recharge"
  }
}
```

**Al pagar factura (variante del flujo, payment_purpose distinto):**

```http
POST https://hub.inovaweb.com.mx/hub/v1/charge
X-API-Key: <HUB_API_KEY>
Content-Type: application/json

{
  "gateway": "conekta",
  "external_user_id": "client-<uuid-caf>",
  "amount": 66888,
  "currency": "MXN",
  "description": "Pago factura INV-2026-05-0042",
  "metadata": {
    "caf_invoice_id": "INV-2026-05-0042",
    "purpose": "invoice_payment"
  }
}
```

### 4.3 Webhook recibido del Hub

```http
POST https://admin.inovaweb.com.mx/webhooks/hub-payment-paid
X-Hub-Signature: t=1716643211,v1=<hmac-sha256>
Content-Type: application/json

{
  "event": "payment.paid",
  "hub_transaction_id": "htx_xyz",
  "external_user_id": "client-<uuid-caf>",
  "amount_cents": 40000,
  "currency": "MXN",
  "gateway": "conekta",
  "occurred_at": "2026-06-15T14:23:11Z",
  "metadata": {
    "caf_client_id": "<uuid>",
    "caf_recharge_id": "RCH-2026-06-0042",
    "purpose": "wallet_recharge"
  }
}
```

El CAF valida firma HMAC compartida, identifica el `purpose` desde metadata, y:

- Si `purpose=wallet_recharge`: llama a Medidor IA para acreditar saldo.
- Si `purpose=invoice_payment`: marca la factura local como `paid` y registra
  entrada en Finanzas-Core con `source_slug=hub`, `direction=credit`.

En ambos casos registra el evento en audit log y notifica al cliente vía
Centro de Mensajes.

### 4.4 Idempotencia

UNIQUE por `hub_transaction_id` en tabla `payments` del CAF. Un webhook reenviado
no duplica el cargo.

---

## 5. Integración con Finanzas-Core

### 5.1 Naturaleza

CAF es lector pesado y emisor moderado. Para tableros, balance del cliente,
agregados por periodo: lee. Para ajustes manuales del operador, registros de
pagos de facturas confirmadas, descuentos aplicados: escribe.

### 5.2 Endpoints invocados

**Lectura — balance del cliente para tablero:**

```http
GET https://finanzas.inovaweb.com.mx/v1/ledger/balance?as_of=2026-06-30T23:59:59Z
X-API-Key: <FINANZAS_API_KEY>
```

(El multi-tenant strict del Finanzas-Core resuelve `tenant_id` desde la API
key. El CAF tiene una key admin master que ve todos los tenants. Para vista
filtrada del cliente, el CAF aplica filtros adicionales por `external_user_id`
en `meta` JSONB.)

**Lectura — totales por fuente y periodo:**

```http
GET https://finanzas.inovaweb.com.mx/v1/ledger/totals?from_ts=2026-05-01T00:00:00Z&to_ts=2026-06-01T00:00:00Z
X-API-Key: <FINANZAS_API_KEY>
```

**Lectura — listado de movimientos paginado:**

```http
GET https://finanzas.inovaweb.com.mx/v1/ledger/entries?source=hub&direction=credit&limit=100&offset=0
X-API-Key: <FINANZAS_API_KEY>
```

**Escritura — registrar pago de factura confirmado por Hub:**

```http
POST https://finanzas.inovaweb.com.mx/v1/ledger/entries
X-API-Key: <FINANZAS_API_KEY>
Content-Type: application/json

{
  "source_slug": "hub",
  "source_ref": "caf-invoice-INV-2026-05-0042-payment",
  "direction": "credit",
  "amount_cents": 66888,
  "currency": "MXN",
  "occurred_at": "2026-06-15T14:23:11Z",
  "description": "Pago factura INV-2026-05-0042 confirmado vía Hub",
  "meta": {
    "caf_invoice_id": "INV-2026-05-0042",
    "hub_transaction_id": "htx_xyz",
    "client_id": "<uuid-cliente-caf>"
  }
}
```

**Escritura — ajuste manual con motivo obligatorio:**

```http
POST https://finanzas.inovaweb.com.mx/v1/ledger/entries
X-API-Key: <FINANZAS_API_KEY>
Content-Type: application/json

{
  "source_slug": "manual",
  "source_ref": "caf-manual-adj-12345",
  "direction": "credit",
  "amount_cents": 50000,
  "currency": "MXN",
  "occurred_at": "2026-06-15T15:00:00Z",
  "description": "Bono cortesía por incidente del 14 de junio - ticket #789",
  "meta": {
    "actor_user_id": "<uuid-operador>",
    "reason_code": "courtesy_incident",
    "ticket_id": "789",
    "client_id": "<uuid-cliente-caf>"
  }
}
```

### 5.3 Convención de `source_ref` del CAF

| Operación | Patrón | Ejemplo |
|---|---|---|
| Pago de factura | `caf-invoice-<invoice_id>-payment` | `caf-invoice-INV-2026-05-0042-payment` |
| Recarga acreditada | `caf-recharge-<rch_id>` | `caf-recharge-RCH-2026-06-0042` |
| Ajuste manual | `caf-manual-adj-<adj_id>` | `caf-manual-adj-12345` |
| Cuota suscripción | `caf-sub-<client>-<yyyymm>` | `caf-sub-norma-sanchez-202606` |
| Reversión | `<original>-reversal` | `caf-invoice-INV-2026-05-0042-payment-reversal` |

### 5.4 Política ante fallo

Reintento del CAF con backoff exponencial 3 intentos. Si persiste, encolado en
`finanzas_retry_queue` del CAF con reintento cada 60s hasta 8 veces. Tras
agotar, queda en estado `manual`.

---

## 6. Integración con Centro de Mensajes

### 6.1 Naturaleza

CAF es emisor de notificaciones humanas hacia el cliente final. El Centro
despacha y reporta el cargo al Finanzas-Core con su propia llave, lo cual
queda reflejado en el balance del cliente y termina facturado el mes siguiente.

### 6.2 Plantillas del catálogo (precargadas al setup)

| Slug | Canal | Cuándo se dispara |
|---|---|---|
| `caf-bienvenida-cliente` | email | Alta exitosa de cliente |
| `caf-bienvenida-operador-interno` | email | Alta de usuario interno con rol |
| `caf-factura-emitida` | email | Al timbrar CFDI, con adjuntos PDF+XML |
| `caf-pago-confirmado` | email | Pago confirmado vía Hub |
| `caf-recordatorio-vencimiento-t5` | email + whatsapp | 5 días antes de vencimiento |
| `caf-recordatorio-vencimiento-t0` | email + whatsapp | Día de vencimiento |
| `caf-recordatorio-vencimiento-tplus5` | email + whatsapp | 5 días después de vencimiento |
| `caf-suspension-por-mora` | email + whatsapp | Al suspender por mora |
| `caf-reactivacion-cliente` | email | Tras pago de mora |
| `caf-cambio-plan-confirmado` | email | Tras cambio de plan exitoso |
| `caf-alerta-saldo-bajo` | email | Saldo por debajo de umbral |

Las plantillas se cargan al Centro de Mensajes vía endpoint admin del propio
Centro durante el bootstrap del CAF. Las variables tipadas se validan contra
el schema del Centro.

### 6.3 Endpoints invocados

**Envío de correo de factura emitida:**

```http
POST https://mensajes.inovaweb.com.mx/v1/messages/email
X-API-Key: <MESSAGES_API_KEY>
Content-Type: application/json

{
  "app_id": "admin-financiera",
  "client_id": "<uuid-cliente-caf>",
  "service_id": "factura-emitida",
  "origin_kind": "template",
  "template_id": "caf-factura-emitida",
  "from": { "email": "facturacion@inovaweb.com.mx", "name": "Inovaweb" },
  "to": { "email": "norma@consultoranorma.com", "name": "Norma Sánchez" },
  "variables": {
    "razon_social": "Norma Sánchez Consultoría",
    "factura_folio": "INV-2026-05-0042",
    "monto_total_humano": "$668.88 MXN",
    "fecha_emision": "1 de junio de 2026",
    "fecha_vencimiento": "15 de junio de 2026",
    "uuid_sat": "F8B9C2D7-1234-5678-90AB-CDEF12345678",
    "url_descarga_pdf": "https://app.inovaweb.com.mx/portal/invoices/INV-2026-05-0042.pdf",
    "url_pagar": "https://app.inovaweb.com.mx/portal/invoices/INV-2026-05-0042/pay"
  },
  "meta": {
    "caf_invoice_id": "INV-2026-05-0042"
  }
}
```

### 6.4 Política ante fallo

El Centro de Mensajes ya tiene su propio worker de reintento. El CAF solo
necesita registrar el `message_id` devuelto para correlación. Si la
notificación falla definitivamente, queda en estado `manual` y se alerta al
operador interno.

---

## 7. Integración con PAC (proveedor externo de timbrado fiscal)

### 7.1 Naturaleza

El PAC es el único servicio externo a Inovaweb que el CAF consume. Recibe el
XML del CFDI sellado por el CAF (con el certificado del SAT), lo firma con
su propio sello de PAC certificado, devuelve el UUID SAT y la cadena original.

### 7.2 Proveedor inicial recomendado

**Facturama** (https://apisandbox.facturama.mx) por API REST simple y pricing
por timbre sin compromiso anual. Alternativas mantenidas como stubs:
Solución Factible, Edicom.

### 7.3 Operaciones

**Timbrado:**

```http
POST https://api.facturama.mx/3/cfdis
Authorization: Basic <PAC_API_KEY:PAC_API_SECRET>
Content-Type: application/json

<XML del CFDI 4.0 sellado con certificado del SAT>
```

**Cancelación:**

```http
DELETE https://api.facturama.mx/3/cfdis/{uuid_sat}?type=issued&motive=02
Authorization: Basic <PAC_API_KEY:PAC_API_SECRET>
```

### 7.4 Webhook del PAC

```http
POST https://admin.inovaweb.com.mx/webhooks/pac
X-Facturama-Signature: t=1716643211,v1=<hmac-sha256>
Content-Type: application/json

{
  "event": "cfdi.timbrado",
  "caf_invoice_id": "INV-2026-05-0042",
  "uuid_sat": "F8B9C2D7-...",
  "fecha_timbrado": "2026-06-01T00:00:30Z",
  "cadena_original_sat": "||...||",
  "sello_sat": "..."
}
```

### 7.5 Cola interna de reintento

Tabla `invoice_pac_queue` del CAF con `caf_invoice_id`, `attempts`, `last_error`,
`status`. Reintento exponencial con MAX_ATTEMPTS=8. Tras agotar, queda en
estado `pac_manual` y se notifica al operador.

---

## 8. Reglas firmes

1. **Patrón Saga en alta de cliente.** Si cualquier paso del onboarding falla,
   se ejecutan las compensaciones inversas y se registra el incidente. El
   cliente NO queda parcialmente creado en los cores.
2. **Idempotencia obligatoria.** Todo POST cross-core lleva `request_id` o
   `source_ref` determinístico construido por el CAF.
3. **Centavos enteros BIGINT en toda la cadena.** Nunca floats.
4. **Append-only en operaciones financieras locales.** Las tablas `invoices`,
   `payments`, `adjustments` y `audit_log` solo aceptan INSERT.
5. **Llaves del CAF hacia los cores se rotan trimestralmente** mediante
   procedimiento controlado: emitir nueva, actualizar `.env`, reiniciar CAF,
   revocar la anterior tras confirmar operación correcta.
6. **Webhooks entrantes validan firma antes de cualquier procesamiento.** Si
   firma inválida o timestamp fuera de ventana, rechazo 401 sin más.
7. **Multi-rol estricto en CAF.** Cada endpoint declara su rol mínimo. La
   autorización se verifica antes de cualquier I/O contra los cores.

---

## 9. Manejo de errores cross-core

| Código del core | Causa típica | Acción del CAF |
|---|---|---|
| 200 / 201 | Éxito | Continuar flujo |
| 202 | Aceptado (Centro de Mensajes) | Persistir `message_id` para correlación |
| 401 / 403 | API key revocada o sin scope | Alerta crítica al operador, NO reintentar, encolar manual |
| 404 | Recurso inexistente | Si era expected: investigar inconsistencia. Si no: continuar |
| 409 | Conflict / idempotency replay | Tratar como éxito (operación ya ejecutada) |
| 422 | Body inválido | Bug del CAF, log + manual, NO reintentar |
| 429 | Rate limit | Respetar `Retry-After`, reintentar después |
| 500 / 503 | Caída transitoria del core | Backoff exponencial, encolar en retry queue |

---

## 10. Roadmap de integración

| Sprint | Qué se hace |
|---|---|
| 1 (actual) | Scaffolding del CAF + onboarding atómico via SQL hacia los 4 cores + catálogos básicos |
| 2 | UI interna operativa + tableros consolidados leyendo Finanzas-Core |
| 3 | Portal cliente + recarga via Hub-Pasarelas (flujo de webhooks payment.paid) |
| 4 | Cierre mensual + timbrado CFDI via PAC contratado |
| 5 | Notificaciones programadas via Centro de Mensajes + recordatorios automáticos |
| 6 | Promociones avanzadas + reportes ejecutivos |

---

## 11. Referencias

- Documento técnico completo: `docs/inovaweb-admin-financiera-proyecto-tecnico.md`
- Modelo de seguridad: `SECURITY.md`
- Instrucciones del proyecto: `CLAUDE.md`
- Contrato del Medidor IA: `https://medidor.inovaweb.com.mx/docs`
- Contrato del Hub de Pasarelas: `https://hub.inovaweb.com.mx/docs`
- Contrato del Finanzas-Core: `https://finanzas.inovaweb.com.mx/docs` + `inovaweb-finanzas-core/docs/01-finanzas-core-integracion-cores.md`
- Contrato del Centro de Mensajes: `https://mensajes.inovaweb.com.mx/docs` + `inovaweb-centro-mensajes/docs/01-centro-mensajes-integracion-cores.md`
