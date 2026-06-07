# Arquitectura Global de la Plataforma Inovaweb

**Documento de referencia principal para desarrolladores nuevos.**
Describe los 6 proyectos, cómo se interconectan, los flujos de datos de punta a
punta, los contratos entre capas y el mapa de identidad entre sistemas.

> **Fuente de verdad de este documento:** auditoría global 2026-06-06, verificada
> contra el código fuente real de cada repositorio. Las discrepancias entre el
> diseño previsto y la implementación real están marcadas explícitamente en la
> sección 7 (no se ocultan).

---

## 1. Los tres niveles

```
NIVEL 3 — Apps Cliente (consumen los cores para IA)
  └── scraping-universidades   Búsqueda semántica con LLM. Piloto activo.

NIVEL 2 — Servicios (orquestan los cores, tienen UI y auth humano)
  └── inovaweb-admin-financiera (CAF)   Centro de Administración Financiera.

NIVEL 1 — Cores (API-only, sin UI, autenticación por API key)
  ├── medidor_ia                Wallet prepago en centavos MXN. Mide cada llamada LLM.
  ├── inovaweb-hub-pasarelas    Gateway de pagos reales (Conekta). Cobra con tarjeta.
  ├── inovaweb-finanzas-core    Libro contable (ledger de asientos append-only).
  └── inovaweb-centro-mensajes  Notificaciones email + WhatsApp por plantillas.
```

**Diferencia clave:** los 4 cores Nivel 1 son API-only (sin UI, autenticación por
API key con scopes). El CAF (Nivel 2) tiene UI server-side (Jinja2 + HTMX) y
autenticación de humanos (JWT + cookies). Las apps Nivel 3 consumen los cores
directamente con su propia API key de scope acotado.

---

## 2. Principios de diseño transversales (verificados en los 6 proyectos)

| Principio | Estado verificado |
|---|---|
| **Dinero en centavos BIGINT, nunca floats** | ✅ Cumplido en los 6. Sin floats en almacenamiento monetario. |
| **Append-only en tablas financieras** (triggers SQL) | ✅ Cumplido en medidor, hub, finanzas, mensajes y CAF. |
| **Auditoría inmutable** (actor, IP, timestamp, valor previo/nuevo) | ✅ CAF vía `trg_audit_row`; cores vía sus propias tablas. |
| **El Medidor es la fuente única de saldo** | ✅ Ningún otro proyecto recalcula el saldo; solo leen del Medidor. |
| **Idempotencia por `request_id` / `source_ref`** | ✅ En cores e integraciones (con la salvedad del onboarding del CAF, §7). |
| **Scopes de API key separados** (ADMIN vs CLIENT) | ✅ CAF usa ADMIN; Scraping usa CLIENT. |

---

## 3. Mapa de llamadas entre proyectos (verificado)

```
CAF ──ADMIN──► Medidor:   POST /v1/wallets                 crea wallet del cliente
                          POST /v1/wallets/{id}/credit     acredita saldo  ⚠️ ver §7-C1
                          GET  /v1/wallets/{id}/balance    saldo para portal
                          GET  /v1/usage                   consumo para tablero

CAF ──────────► Hub:      POST /hub/v1/charge              inicia cargo Conekta
Hub ──────────► Medidor:  POST /v1/wallets/{id}/credit     ⚠️ el Hub acredita DIRECTO (ver §7-D2)

CAF ──────────► Finanzas: POST /v1/ledger/entries          registra asiento contable
                          GET  /v1/ledger/balance|totals   ingresos para tablero

CAF ──────────► Mensajes: POST /v1/messages/email          activación, pago, vencimiento
                          POST /v1/messages/whatsapp        ⚠️ devuelve 501 (ver §7-D5)

Scraping ─CLIENT─► Medidor: POST /v1/operations/authorize  reserva HOLD de crédito
                            POST /v1/operations/finish       liquida HOLD con costo real
                            POST /v1/operations/release      cancela HOLD sin cargo
                            POST /v1/operations/quote        precio estimado
                            GET  /v1/wallets/{id}/balance    saldo del cliente
```

**Nota sobre prefijos de ruta del Medidor:** todos los routers de wallet y
operaciones se montan con `prefix="/v1"` (`medidor_ia/src/medidor_ia/main.py:64-68`).
Las operaciones de holds están en `/v1/operations/*`. El crédito de wallet —aunque
requiere scope ADMIN— vive en **`/v1/wallets/{id}/credit`**, NO bajo `/admin/v1`.
El router `/admin/v1` solo contiene suspend/unsuspend/flags/refund_audit/suspicious.

---

## 4. Mapa de identidad entre sistemas

La identidad del cliente se vincula así (estado **real**, ver discrepancia §7-D1):

```
CAF:      clients.id                  (PK local del CAF)
          clients.medidor_account_id  = wallet_id devuelto por el Medidor
          clients.hub_account_id      = external_user_id
          clients.finanzas_account_id = external_user_id
          clients.messages_account_id = external_user_id

Medidor:  wallets.external_user_id    = "client-{clients.id}"   ← lo fija el CAF
          wallets.tenant_id           = (resuelto por la API key, tenant 'inovaweb')
          wallets.id                  = "wlt-..."

Scraping: companies.caf_client_id     = clients.id del CAF
          companies.medidor_wallet_id = wallet_id del Medidor
```

El CAF crea la wallet en el Medidor usando `external_user_id = "client-{client_id}"`
(su propio id de cliente). **El diseño original (§3.2 de la instrucción) preveía usar
el `Company.id` de Scraping como `external_user_id` y un endpoint `link-caf` en
Scraping; eso NO está implementado** (ver §7-D1).

---

## 5. Flujos de punta a punta

### 5.1 Pago / recarga (el más crítico)

**Diseño previsto (instrucción §3.3):**
```
1. Cliente elige plan en portal CAF
2. CAF → Hub: POST /hub/v1/charge {amount_cents, source}
3. Hub → Conekta: procesa el cargo bancario
4. Hub → CAF: webhook payment.paid
5. CAF valida HMAC + timestamp anti-replay + correlación purpose/amount
6. CAF → Medidor: POST credit {amount_cents, request_id=caf-recharge-{id}}
7. CAF → Finanzas: POST /v1/ledger/entries
8. CAF → Mensajes: email caf-pago-confirmado
```

**Realidad implementada:** el **Hub acredita el wallet del Medidor directamente**
al recibir el webhook de Conekta (`hub/app/routers/webhooks_router.py`), usando
`request_id = "<gateway>-payment-<provider_payment_id>"`. El handler
`POST /webhooks/hub-payment-paid` del CAF existe y está bien construido (valida
HMAC + timestamp + correlación), pero **el Hub no lo invoca**. Ver §7-D2 para el
riesgo de doble acreditación si ambos caminos se activan.

### 5.2 Consumo IA (Scraping)

```
1. Usuario busca en Scraping
2. Scraping → Medidor: POST /v1/operations/authorize {wallet_id, units_estimated}
   └─ Medidor crea HOLD, verifica saldo, rechaza con 402 si insuficiente
3. Scraping → Perplexity/LLM: ejecuta la búsqueda
4a. Éxito con costo>0  → Medidor: POST /v1/operations/finish {hold_id, real_cost_cents}
4b. Costo 0 o fallo    → Medidor: POST /v1/operations/release {hold_id}
5. (Diferido) Scraping → Finanzas: POST /v1/ledger/entries
6. Usuario recibe resultados
```

El manejo de saldo insuficiente y la liberación de HOLDs en error están
correctamente implementados (bloqueo limpio 402, release automático). Riesgo
residual: si `finish`/`release` fallan por red, el HOLD se reintenta manualmente
(deuda operativa); el TTL del HOLD en el Medidor es la red de seguridad.

### 5.3 Onboarding atómico (Saga, CAF)

```
1. INSERT clients (status=active)
2. Medidor: POST /v1/wallets {external_user_id="client-{id}"}   ← punto de compensación
3. UPDATE clients con medidor_account_id + referencias externas
4. INSERT subscriptions (plan activo)
5. INSERT users (titular) + user_roles  → password temporal Argon2id
   └─ si falla: _compensate(suspend_wallet) best-effort + rollback + audit en sesión propia
```

La Saga compensa correctamente la wallet creada si el alta del usuario falla. El
audit de fallos se persiste en una sesión independiente (sobrevive al rollback).
Deuda: el onboarding **no es idempotente por `request_id`** (ver §7).

---

## 6. Contratos clave entre capas

### 6.1 Medidor `/v1/wallets/{id}/credit` (scope ADMIN)
```json
{ "amount_cents": 50000, "currency": "MXN",
  "request_id": "caf-recharge-<rch_id>", "reason": "...", "metadata": {} }
```
Idempotente por `(wallet_id, request_id)` UNIQUE.

### 6.2 Finanzas `/v1/ledger/entries` (scope ledger:write)
```json
{ "source_slug": "hub|medidor|messages|invoice|subscription|manual",
  "source_ref": "caf-recharge-<id>", "direction": "credit|debit",
  "amount_cents": 50000, "currency": "MXN", "occurred_at": "ISO-8601",
  "description": "...", "meta": {} }
```
- Idempotente por `(tenant_id, source_slug, source_ref)`.
- **`source_slug` solo admite el conjunto cerrado de arriba.** Valores como
  `scraping_search` serían rechazados (ver §7-D3).

### 6.3 Hub `/hub/v1/charge` (scope payments:write)
```json
{ "gateway": "conekta", "operation": "charge_card",
  "amount_cents": 19900, "currency": "MXN",
  "external_user_id": "client-<id>",
  "metadata": { "purpose": "wallet_recharge|plan_purchase", "caf_client_id": "..." } }
```

### 6.4 Mensajes `/v1/messages/email` (scope messages:write)
```json
{ "origin_kind": "template", "template_id": "caf-pago-confirmado",
  "to": {"email": "...", "name": "..."}, "variables": {...},
  "client_id": "...", "service_id": "..." }
```
- El `template_id` debe existir **previamente sembrado** en el tenant del Centro de
  Mensajes, o el envío devuelve 404 (ver §7-D4).

---

## 7. Discrepancias diseño ↔ implementación (hallazgos de la auditoría)

> Estos hallazgos son la entrega más importante de la revisión cruzada. No
> bloquean la operación actual porque varios flujos aún no se ejecutan end-to-end,
> pero deben resolverse antes de producción multi-cliente.

### C1 — 🔴 CRÍTICO: ruta de `credit` del CAF desalineada
- El CAF llama `POST /admin/v1/wallets/{id}/credit` y `DELETE /admin/v1/wallets/{id}`
  (`app/core/clients/medidor_client.py:78,96`).
- El Medidor expone `credit` en **`/v1/wallets/{id}/credit`** y **no tiene** rutas
  bajo `/admin/v1/wallets/{id}/credit` ni `DELETE` de wallet.
- **Efecto:** toda recarga/acreditación y la compensación Saga darían **404**.
- **Por qué está latente:** el flujo prepago end-to-end del CAF nunca se ejecutó
  (tasks #16 y #18 pendientes). El Hub, en cambio, usa la ruta correcta.
- **Fix sugerido (1 línea c/u):** `/admin/v1/wallets/{id}/credit` → `/v1/wallets/{id}/credit`;
  y confirmar con el equipo Medidor si existe un `DELETE` de wallet o si la
  compensación debe usar `suspend` en su lugar.

### D1 — Identidad cross-sistema no cableada
El `external_user_id` lo fija el CAF como `client-{id}` (no el `Company.id` de
Scraping). El endpoint `POST /companies/{id}/link-caf` **no existe** en Scraping;
hoy el vínculo se haría vía `PATCH /admin/companies/{id}`. La integración
Scraping↔CAF está diferida (ADR-002/ADR-010 de Scraping).

### D2 — Doble camino de acreditación
El Hub acredita el Medidor directamente (Conekta→Hub→Medidor) y el CAF también
tiene un camino de acreditación (webhook→CAF→Medidor) con **`request_id`
distinto**. Hoy solo el Hub está activo (no emite webhook al CAF), así que no hay
doble cargo, pero si se cablea el webhook al CAF habría que unificar el
`request_id` o elegir un único responsable de acreditar.

### D3 — `source_slug` de Scraping incompatible con Finanzas
Scraping pretende usar `source_slug=scraping_*`, pero Finanzas solo admite
`{medidor, hub, messages, invoice, subscription, manual}`. Integración diferida;
al implementarla, agregar `scraping` (o un slug admitido) al conjunto de Finanzas.

### D4 — Plantillas del CAF no sembradas en Mensajes
`caf-pago-confirmado`, `caf-activacion-correo`, `caf-activacion-otp` no existen en
el Centro de Mensajes; deben crearse por-tenant vía `POST /admin/v1/templates`
antes del primer envío, o el `send_email` devuelve 404.

### D5 — WhatsApp del CAF asume endpoint no implementado
`messages_client.send_whatsapp` apunta a `POST /v1/messages/whatsapp`, que el
Centro de Mensajes responde con **501 (no implementado)**. El TODO está marcado en
`messages_client.py:104`.

### Otros (no bloqueantes)
- **Scraping**: redondeo de micro-costos a 0 (`semantic_search.py:384`) →
  sub-facturación de búsquedas baratas.
- **CAF**: onboarding sin idempotencia por `request_id`; tabla `revoked_tokens`
  mencionada pero no implementada (logout no invalida JWT en servidor).

---

## 8. Infraestructura

- **VPS Contabo** `89.116.25.222`. Cada core en su puerto host; CAF en 8006 (contenedor 8001).
- **Reverse proxy: Nginx** (reemplazó a Caddy). Los `Caddyfile` en los repos son
  referencia histórica y **no** reflejan la config real del VPS.
- TLS por Let's Encrypt. Dos dominios del CAF (`admin.inovaweb.com.mx` y
  `app.inovaweb.com.mx`) enrutan al mismo backend; el backend distingue por `Host`.

> `[TODO: verificar config Nginx real en VPS]` — la configuración de virtual hosts,
> manejo de TLS y red Docker debe leerse de `/etc/nginx` en el VPS antes de tratar
> esta sección como definitiva. No se documenta config inventada.

---

## 9. Repositorios

| Proyecto | Ruta local | GitHub (planeado) |
|---|---|---|
| CAF | `inovaweb-admin-financiera` | InovawebSoluciones/inovaweb-admin-financiera |
| Medidor | `medidor_ia` | — |
| Hub | `inovaweb-hub-pasarelas` | — |
| Finanzas | `inovaweb-finanzas-core` | — |
| Mensajes | `inovaweb-centro-mensajes` | — |
| Scraping | `scraping_comercial` | InovawebSoluciones/scraping-inovaweb |

---

*Generado por la auditoría global Inovaweb — 2026-06-06. Verificado contra código fuente.*
