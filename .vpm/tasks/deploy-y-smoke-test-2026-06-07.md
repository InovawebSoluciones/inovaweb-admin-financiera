# DEPLOY + SMOKE TEST REAL — 2026-06-07

**Repo CAF:** `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera\`
**Repo Scraping:** `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\scraping_comercial\scraping-universidades\`
**VPS:** 89.116.25.222
**CAF en VPS:** /opt/inovaweb-admin-financiera · servicio docker: admin_financiera · puerto 8006
**Dominios:** admin.inovaweb.com.mx (operador) · app.inovaweb.com.mx (cliente)
**Cores Nivel 1 operativos:** medidor.inovaweb.com.mx · hub.inovaweb.com.mx · finanzas.inovaweb.com.mx · mensajes.inovaweb.com.mx

NO simules datos. Todo debe ser una llamada real a la API. Todo debe quedar registrado en BD.

**Repos con acceso completo:**
- CAF: `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera\`
- Scraping: `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\scraping_comercial\scraping-universidades\`
- Medidor: buscar en `C:\Users\conra\OneDrive - Inovaweb\webescolar\` — repo `medidor_ia` o similar
- Centro de Mensajes: buscar en `C:\Users\conra\OneDrive - Inovaweb\webescolar\` — repo `inovaweb-centro-mensajes` o similar

---

## FASE 0 — VALIDAR Y CREAR ENDPOINTS EN SCRAPING

Antes de hacer el deploy, verifica que el repo de Scraping tiene los endpoints necesarios para consultar consumo real. Si no los tiene, créalos.

### 0.1 Validar endpoint de consumo IA (Medidor)

Busca en el repo de Scraping si existe algún endpoint o función que:
- Consulte el saldo o consumo de IA de una Company en el Medidor (`GET /v1/wallets/{wallet_id}/balance` o `GET /v1/usage`)
- Exponga ese dato hacia afuera (por API REST interna o como campo calculado)

**Cómo buscar:**
```
Grep recursivo en C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\scraping_comercial\scraping-universidades\
por: "medidor", "wallet", "balance", "usage", "consumo"
en archivos .py
```

**Si NO existe:**
Crear en Scraping el endpoint `GET /companies/{company_id}/usage` que:
1. Lea `companies.medidor_wallet_id` de la BD.
2. Llame a `GET https://medidor.inovaweb.com.mx/v1/wallets/{wallet_id}/balance` con la API key del Medidor (leer de config/env de Scraping).
3. Llame a `GET https://medidor.inovaweb.com.mx/v1/usage?project_id={wallet_id}&from_ts=&to_ts=` para el periodo actual.
4. Devuelva `{wallet_id, balance_cents, usage_ops, usage_cost_cents, period_from, period_to}`.
5. Auth: Bearer admin (misma key que usa `link-caf`).
6. Docstring + type hints. `py_compile` al terminar.

### 0.2 Validar endpoint de consumo de emails (Centro de Mensajes)

Busca en el repo de Scraping si existe algún endpoint o función que:
- Consulte la cantidad de emails enviados por una Company en el Centro de Mensajes (`GET /v1/reports/usage`)
- Exponga ese dato por API o lo registre en BD

**Cómo buscar:**
```
Grep recursivo en scraping-universidades
por: "mensajes", "messages", "email_count", "reports", "usage"
en archivos .py
```

**Si NO existe:**
Crear en Scraping el endpoint `GET /companies/{company_id}/email-usage` que:
1. Lea `companies.messages_account_id` (o `external_user_id = f"client-{caf_client_id}"`) de la BD.
2. Llame a `GET https://mensajes.inovaweb.com.mx/v1/reports/usage?group_by=client&from_ts=&to_ts=` con la API key del Centro de Mensajes.
3. Busque la fila donde `client_id` coincide con el `external_user_id` de la company.
4. Devuelva `{external_user_id, message_count, cost_cents, period_from, period_to}`.
5. Auth: Bearer admin.
6. Docstring + type hints. `py_compile` al terminar.

### 0.3 Verificar que Scraping tiene las API keys de Medidor y Centro de Mensajes en su config

Busca en `config.py` o `.env.example` de Scraping las variables:
- `MEDIDOR_BASE_URL` / `MEDIDOR_API_KEY`
- `MENSAJES_BASE_URL` / `MENSAJES_API_KEY`

Si no están, agrégalas al `config.py` y al `.env.example` de Scraping.

### 0.4 py_compile de todos los archivos nuevos/modificados en Scraping

Reporta resultado antes de continuar al FASE 1.

---

## FASE 1 — DEPLOY

### 1.1 Commit y push de ambos repos desde Windows

Lee el archivo:
`C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera\.vpm\tasks\commits-listos.md`

Ejecuta los comandos de git que ahí están para hacer commit+push de CAF y Scraping.
Si hay conflictos, resuélvelos (no hay cambios en el remote desde la última sesión).

### 1.2 Deploy en el VPS vía SSH

Conéctate al VPS por SSH y ejecuta en orden:

```bash
# 1. Pull y build del CAF
cd /opt/inovaweb-admin-financiera
git pull
docker compose up -d --build

# 2. Aplicar migraciones nuevas (005 y 006 — idempotentes)
docker compose exec -T postgres psql -U caf -d admin_financiera \
  -f /docker-entrypoint-initdb.d/005_activation_tokens.sql
docker compose exec -T postgres psql -U caf -d admin_financiera \
  -f /docker-entrypoint-initdb.d/006_idempotencia.sql

# 3. Pull y migración de Scraping
cd /root/scraping-universidades
git pull
docker compose run --rm backend alembic upgrade head

# 4. Verificar que el CAF levantó
curl -sf http://localhost:8006/health && echo "CAF OK" || echo "CAF FALLO"
```

Si algo falla en este paso, detente y reporta el error completo antes de continuar.

---

## FASE 2 — CREAR CUENTA INOVAWEB EN CAF (cliente real)

### 2.1 Buscar Company "inovaweb" en BD de Scraping

Conéctate a la BD de Scraping y obtén el `id` de la company de Inovaweb:

```bash
docker compose exec -T postgres psql -U scraping -d scraping \
  -c "SELECT id, name, caf_client_id, medidor_wallet_id FROM companies WHERE name ILIKE '%inovaweb%' OR slug ILIKE '%inovaweb%';"
```

Guarda el `company_id` resultado — lo usarás en el onboarding.

### 2.2 Crear cliente Inovaweb en el CAF vía API

Llama al endpoint de alta atómica del CAF con datos reales:

```bash
curl -s -X POST http://localhost:8006/api/v2/clients \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <TOKEN_ADMIN_CAF>" \
  -d '{
    "legal_name": "Inovaweb Soluciones SA de CV",
    "trade_name": "Inovaweb",
    "rfc": "ISC240101XX1",
    "cfdi_use": "G03",
    "tax_regime": "601",
    "zip_code": "06600",
    "billing_email": "conrado.torres@inovaweb.com.mx",
    "contact_phone": "+525512345678",
    "plan_code": "basico",
    "titular_full_name": "Conrado Torres",
    "titular_email": "conrado.torres@inovaweb.com.mx",
    "scraping_company_id": <COMPANY_ID_DEL_PASO_2.1>
  }'
```

Guarda de la respuesta: `client_id`, `wallet_id`, `user_id`, `temp_password`.

Si no tienes el TOKEN_ADMIN_CAF, obtenlo así:
```bash
curl -s -X POST http://localhost:8006/login \
  -d "email=<ADMIN_EMAIL>&password=<ADMIN_PASSWORD>"
```

### 2.3 Verificar en BD que el onboarding fue completo

```bash
docker compose exec -T postgres psql -U caf -d admin_financiera -c "
  SELECT c.id, c.legal_name, c.medidor_account_id, c.status,
         s.plan_id, u.email
  FROM clients c
  JOIN subscriptions s ON s.client_id = c.id
  JOIN users u ON u.client_id = c.id
  WHERE c.legal_name ILIKE '%inovaweb%';"
```

Verifica que: `medidor_account_id` no es NULL (wallet creada), `status = active`, user con email de Conrado existe.

### 2.4 Verificar en BD de Scraping que el link se guardó

```bash
docker compose exec -T postgres psql -U scraping -d scraping -c "
  SELECT id, name, caf_client_id, medidor_wallet_id
  FROM companies WHERE id = <COMPANY_ID_DEL_PASO_2.1>;"
```

Verifica que `caf_client_id` y `medidor_wallet_id` ya no son NULL.

---

## FASE 3 — CARGAR $500 MXN A LA WALLET

### 3.1 Acreditar saldo en el Medidor

Usa el `wallet_id` obtenido en el paso 2.2:

```bash
curl -s -X POST https://medidor.inovaweb.com.mx/v1/wallets/<WALLET_ID>/credit \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <MEDIDOR_API_KEY>" \
  -d '{
    "amount_cents": 50000,
    "currency": "MXN",
    "request_id": "smoke-test-carga-inicial-2026-06-07",
    "reason": "Carga inicial smoke test",
    "metadata": {"fuente": "smoke_test", "operador": "conrado.torres@inovaweb.com.mx"}
  }'
```

`amount_cents: 50000` = $500 MXN exactos.

### 3.2 Verificar saldo en el Medidor

```bash
curl -s https://medidor.inovaweb.com.mx/v1/wallets/<WALLET_ID>/balance \
  -H "Authorization: Bearer <MEDIDOR_API_KEY>"
```

Respuesta esperada: `balance_cents: 50000`.

### 3.3 Verificar que quedó en audit_log del CAF

```bash
docker compose exec -T postgres psql -U caf -d admin_financiera -c "
  SELECT action, new_values, created_at
  FROM audit_log
  WHERE entity_type = 'clients'
  ORDER BY created_at DESC LIMIT 5;"
```

---

## FASE 4 — 4 DISPAROS DE EMAIL

Envía 4 correos reales vía el Centro de Mensajes. Primero asegúrate de que la plantilla existe:

```bash
curl -s https://mensajes.inovaweb.com.mx/v1/templates/caf-activacion-correo \
  -H "Authorization: Bearer <MENSAJES_API_KEY>"
```

Si no existe, créala con el contenido de:
`C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera\.vpm\tasks\seed-mensajes.md`

### 4.1 Disparo 1 — Activación a Conrado

```bash
curl -s -X POST https://mensajes.inovaweb.com.mx/v1/messages/email \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <MENSAJES_API_KEY>" \
  -d '{
    "template_slug": "caf-activacion-correo",
    "to": "conrado.torres@inovaweb.com.mx",
    "variables": {
      "nombre": "Conrado Torres",
      "token_url": "https://app.inovaweb.com.mx/activar?token=smoke-test-conrado-001",
      "expiracion_horas": "24"
    },
    "metadata": {"smoke_test": true, "disparo": 1}
  }'
```

### 4.2 Disparo 2 — Activación a Beatriz

```bash
curl -s -X POST https://mensajes.inovaweb.com.mx/v1/messages/email \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <MENSAJES_API_KEY>" \
  -d '{
    "template_slug": "caf-activacion-correo",
    "to": "beatriz.naredo@inovaweb.com.mx",
    "variables": {
      "nombre": "Beatriz Naredo",
      "token_url": "https://app.inovaweb.com.mx/activar?token=smoke-test-beatriz-001",
      "expiracion_horas": "24"
    },
    "metadata": {"smoke_test": true, "disparo": 2}
  }'
```

### 4.3 Disparo 3 — Confirmación de alta a Conrado

```bash
curl -s -X POST https://mensajes.inovaweb.com.mx/v1/messages/email \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <MENSAJES_API_KEY>" \
  -d '{
    "template_slug": "caf-pago-confirmado",
    "to": "conrado.torres@inovaweb.com.mx",
    "variables": {
      "nombre": "Conrado Torres",
      "monto": "$500.00",
      "saldo_nuevo": "$500.00"
    },
    "metadata": {"smoke_test": true, "disparo": 3}
  }'
```

### 4.4 Disparo 4 — Confirmación de alta a Beatriz

```bash
curl -s -X POST https://mensajes.inovaweb.com.mx/v1/messages/email \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <MENSAJES_API_KEY>" \
  -d '{
    "template_slug": "caf-pago-confirmado",
    "to": "beatriz.naredo@inovaweb.com.mx",
    "variables": {
      "nombre": "Beatriz Naredo",
      "monto": "$500.00",
      "saldo_nuevo": "$500.00"
    },
    "metadata": {"smoke_test": true, "disparo": 4}
  }'
```

Guarda el `message_id` de cada respuesta.

---

## FASE 5 — CONTABILIZAR CONSUMO REAL

### 5.1 Consumo IA en el Medidor

```bash
curl -s "https://medidor.inovaweb.com.mx/v1/usage?project_id=<WALLET_ID>&from_ts=2026-06-07T00:00:00Z&to_ts=2026-06-07T23:59:59Z" \
  -H "Authorization: Bearer <MEDIDOR_API_KEY>"
```

Guarda: total de operaciones y `total_cost_cents`.

### 5.2 Mensajes enviados en Centro de Mensajes

```bash
curl -s "https://mensajes.inovaweb.com.mx/v1/reports/usage?group_by=client&from_ts=2026-06-07T00:00:00Z&to_ts=2026-06-07T23:59:59Z" \
  -H "Authorization: Bearer <MENSAJES_API_KEY>"
```

Busca la fila donde `client_id` = `messages_account_id` del cliente Inovaweb.
Guarda: `count` (número de mensajes) y `amount_cents`.

### 5.3 Registrar consumo en el CAF — invoice_items

Inserta los consumos reales en la BD del CAF (esto es lo que haría el cierre mensual automático; lo forzamos manualmente para el smoke test):

```bash
docker compose exec -T postgres psql -U caf -d admin_financiera -c "
-- Obtener invoice del cliente Inovaweb del mes actual (o crear uno de prueba)
-- Primero verificar si existe:
SELECT i.id, i.client_id, i.status, i.total_cents
FROM invoices i
JOIN clients c ON c.id = i.client_id
WHERE c.legal_name ILIKE '%inovaweb%'
ORDER BY i.created_at DESC LIMIT 1;"
```

Si no existe invoice, crear uno:
```bash
docker compose exec -T postgres psql -U caf -d admin_financiera -c "
INSERT INTO invoices (client_id, period_start, period_end, status, total_cents, currency)
SELECT id, '2026-06-01', '2026-06-30', 'draft', 0, 'MXN'
FROM clients WHERE legal_name ILIKE '%inovaweb%'
RETURNING id;"
```

Insertar los items de consumo (usa los valores reales obtenidos en 5.1 y 5.2):
```bash
docker compose exec -T postgres psql -U caf -d admin_financiera -c "
INSERT INTO invoice_items (invoice_id, description, quantity, unit_price_cents, total_cents, sat_key, unit_sat_key)
VALUES
  (<INVOICE_ID>, 'Consumo IA — operaciones LLM', <TOTAL_OPS>, <PRECIO_POR_OP_CENTS>, <TOTAL_COST_CENTS>, '81161700', 'E48'),
  (<INVOICE_ID>, 'Mensajes de correo electrónico', 4, 100, 400, '81161700', 'E48');"
```

Precio por email: 100 centavos = $1 MXN (punto 7 del requerimiento).
Precio por operación IA: según tabla pública de LLMs (ver nota abajo).

**Nota sobre precios LLM (punto 6):**
Busca en el CAF si existe una tabla `llm_prices` o similar. Si no existe, usa estos precios base de mercado como referencia y créalos como constantes en el insert:
- Claude Haiku: ~$0.0025 USD por 1K tokens ≈ 0.05 centavos MXN por operación
- Claude Sonnet: ~$0.015 USD por 1K tokens ≈ 0.30 centavos MXN por operación
- GPT-4o: ~$0.01 USD por 1K tokens ≈ 0.20 centavos MXN por operación
Si el Medidor ya devuelve `total_cost_cents` calculado, usa ese valor directamente.

---

## FASE 6 — VERIFICACIÓN FINAL

Reporta todo lo siguiente con datos reales (sin inventar ningún valor):

1. **Deploy:** salida del `curl /health` del CAF.
2. **Cliente creado:** `client_id`, `wallet_id`, `user_id` reales.
3. **Link Scraping:** `caf_client_id` y `medidor_wallet_id` en la BD de Scraping.
4. **Saldo:** balance real en el Medidor post-carga ($500).
5. **4 emails:** `message_id` de cada disparo + status de entrega.
6. **Consumo IA:** total ops y costo en centavos del periodo.
7. **Consumo mensajes:** count y amount_cents registrados.
8. **invoice_items:** SELECT final de los items insertados con sus montos.
9. **audit_log:** las últimas 10 entradas del CAF.

Si algún paso falla, detente, reporta el error exacto y no inventes el resultado.
