# Runbook operacional — inovaweb-admin-financiera

Procedimientos para diagnóstico y mitigación de incidentes en el CAF.
Cada componente sigue el formato: **Síntoma → Diagnóstico → Fix → Verificación**.

VPS: `root@89.116.25.222` · puerto host del CAF: `8006` → contenedor `8001`.

---

## 0. Comandos de orientación rápida

```bash
# entrar al VPS
ssh root@89.116.25.222

# estado de los contenedores del CAF
docker ps --filter "name=caf_" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

# logs en vivo del backend (últimas 200 líneas + follow)
docker logs --tail 200 -f caf_app

# logs en vivo de Postgres
docker logs --tail 200 -f caf_postgres

# salud externa
curl -s https://admin.inovaweb.com.mx/health | jq
curl -s https://app.inovaweb.com.mx/health | jq
```

---

## 1. Backend FastAPI (`caf_app`)

### 1.1 Síntoma: el contenedor reinicia en loop

**Diagnóstico:**
```bash
docker logs --tail 100 caf_app
```
Causas habituales:
- Variable de entorno obligatoria faltante o vacía →
  `pydantic_core._pydantic_core.ValidationError`.
- `JWT_SECRET` o `AES_KEY` con menos de 32 bytes.
- Postgres no levantado todavía (cuando se hace `up` por primera vez).

**Fix:**
```bash
cd /opt/inovaweb-admin-financiera
# revisar/corregir .env
nano .env
docker compose up -d --build admin_financiera
```

**Verificación:**
```bash
docker logs --tail 30 caf_app | grep caf_startup
curl -s http://localhost:8006/health
```
Debe imprimir `{"status":"ok"}`.

---

### 1.2 Síntoma: `/health` OK pero `/health/db` devuelve 503

**Diagnóstico:**
```bash
docker exec -it caf_app python -c "
import asyncio
from app.core.database import engine
async def f():
    async with engine.connect() as c:
        await c.execute('SELECT 1')
asyncio.run(f())
"
```
Si arroja `OperationalError` → red entre `caf_app` y `caf_postgres` rota o
Postgres no aceptando conexiones.

**Fix:**
```bash
docker compose restart postgres
sleep 5
docker compose restart admin_financiera
```

**Verificación:**
```bash
curl -s http://localhost:8006/health/db
```

---

### 1.3 Síntoma: 502 Bad Gateway desde admin.inovaweb.com.mx o app.inovaweb.com.mx

**Diagnóstico:**
- Caddy del stack n8n no encuentra el backend.
```bash
# verificar que el contenedor está en la red n8n_default
docker network inspect n8n_default | grep caf_app
```

**Fix:**
```bash
docker network connect n8n_default caf_app
# o bien recrear con la red declarada en docker-compose.yml
docker compose up -d --force-recreate admin_financiera
```

**Verificación:**
```bash
curl -sI https://admin.inovaweb.com.mx/health
curl -sI https://app.inovaweb.com.mx/health
```

---

### 1.4 Síntoma: login devuelve 401 en cuentas válidas

**Diagnóstico:**
- Posible cambio de `JWT_SECRET` invalidó todos los tokens previos (esperado
  tras rotación).
- Argon2 verifica mal porque la fila tiene hash legacy: `psql` y revisar
  `users.password_hash`.

**Fix:**
- Si fue rotación de `JWT_SECRET`: usuarios deben re-loguearse, no hay fix
  (es comportamiento correcto).
- Si es hash legacy: forzar reset desde UI super-admin
  (`/admin/users/{id}/reset-password`).

**Verificación:**
- Login manual en `https://admin.inovaweb.com.mx/login` con credenciales
  de prueba.

---

## 2. PostgreSQL (`caf_postgres`)

### 2.1 Síntoma: `caf_postgres` unhealthy

**Diagnóstico:**
```bash
docker logs --tail 100 caf_postgres
docker exec -it caf_postgres pg_isready -U caf -d admin_financiera
```

**Fix:**
```bash
docker compose restart postgres
# si persiste, inspeccionar el volumen
docker volume inspect inovaweb-admin-financiera_caf_pgdata
df -h  # confirmar que no se llenó el disco
```

**Verificación:**
```bash
docker exec -it caf_postgres psql -U caf -d admin_financiera -c "SELECT now();"
```

---

### 2.2 Síntoma: `RAISE EXCEPTION append-only: UPDATE on payments no permitido`

**Diagnóstico:** alguien (humano o bug) intentó modificar una tabla
financiera. Esto es **comportamiento esperado** (ADR-003). El trigger está
defendiendo la integridad.

**Fix:**
- NO desactivar el trigger.
- Para corregir un payment, crear un `adjustment` con motivo:
```sql
INSERT INTO adjustments (invoice_id, amount_cents, reason, actor_user_id)
VALUES (...);
```
- Para una factura mal emitida, emitir nota de crédito (CFDI tipo `E`)
  desde `/admin/billing/invoices/{id}/credit-note`.

**Verificación:**
```sql
SELECT * FROM audit_log ORDER BY id DESC LIMIT 10;
```

---

### 2.3 Síntoma: tabla `audit_log` crece descontroladamente

**Diagnóstico:**
```sql
SELECT pg_size_pretty(pg_total_relation_size('audit_log'));
SELECT count(*) FROM audit_log WHERE created_at < now() - interval '90 days';
```

**Fix:** **NO TRUNCAR.** El audit_log es append-only por diseño (ADR-003).
Opciones:
1. Particionar por mes (cambio de schema, planeado).
2. Archivar a frío (S3 / Backblaze) snapshots mensuales y mantener solo
   los últimos 12 meses en línea — requiere ADR-010.

**Verificación:** monitoreo de tamaño de tabla en dashboards mensuales.

---

### 2.4 Síntoma: necesito aplicar una migración SQL

**Procedimiento estándar** (desde Windows del operador):
```powershell
# IMPORTANTE: usar Get-Content | ssh, NUNCA <
Get-Content "C:\path\to\003_nueva_migracion.sql" `
  | ssh root@89.116.25.222 "docker exec -i caf_postgres psql -U caf -d admin_financiera"
```

**Verificación:**
```bash
docker exec -it caf_postgres psql -U caf -d admin_financiera -c "\dt"
```

---

## 3. Workers batch (`monthly_closing`, `invoice_retry`, `overdue_notifier`)

### 3.1 Síntoma: cierre mensual no se ejecutó el día 1

**Diagnóstico:**
```bash
# revisar cron del host
crontab -l | grep monthly_closing

# revisar último run
docker logs caf_monthly_closing 2>&1 | tail -50

# y la tabla de runs
docker exec -it caf_postgres psql -U caf -d admin_financiera \
  -c "SELECT * FROM closing_runs ORDER BY id DESC LIMIT 5;"
```

**Fix:**
```bash
# disparar manualmente
cd /opt/inovaweb-admin-financiera
docker compose --profile jobs run --rm monthly_closing

# o forzar desde la UI admin
# POST https://admin.inovaweb.com.mx/admin/billing/run-closing
# (requiere rol super_admin o finanzas)
```

**Verificación:**
```sql
SELECT count(*), sum(total_cents)
FROM invoices
WHERE created_at::date = CURRENT_DATE;
```

---

### 3.2 Síntoma: facturas en estado `stamp_pending` acumulándose

**Diagnóstico:**
```sql
SELECT count(*), min(created_at)
FROM invoices
WHERE status = 'stamp_pending';
```

Si el PAC está caído, el `invoice_retry` worker hará backoff y reintentos.
Probar manualmente:
```bash
docker exec -it caf_app python -c "
import asyncio
from app.core.clients.pac_client import PACClient
async def f():
    c = PACClient()
    print(await c.health())
asyncio.run(f())
"
```

**Fix:**
- Si el PAC responde pero rechaza el XML → revisar `audit_log` con
  `event_type='stamp_failed'` y la respuesta del PAC. Causa típica:
  certificado CSD expirado.
- Si el PAC no responde → esperar; el worker reintenta con backoff.
- Si el cliente es crítico y el PAC sigue caído > 24h: emitir manualmente
  desde el portal del PAC y registrar `payment_method='manual'`.

**Verificación:**
```bash
docker compose --profile jobs run --rm invoice_retry
```

---

### 3.3 Síntoma: clientes en mora no reciben recordatorio

**Diagnóstico:**
```bash
crontab -l | grep overdue_notifier
docker logs caf_overdue_notifier 2>&1 | tail -50
```

**Fix:**
```bash
docker compose --profile jobs run --rm overdue_notifier
```

**Verificación:**
```sql
SELECT * FROM notifications_sent
WHERE template = 'overdue_reminder'
  AND created_at::date = CURRENT_DATE;
```

---

### 3.4 Cron sugerido del host

```cron
# /etc/crontab del VPS
# cierre mensual - día 1 a las 03:00
0 3 1 * * root cd /opt/inovaweb-admin-financiera && docker compose --profile jobs run --rm monthly_closing >> /var/log/caf/monthly_closing.log 2>&1

# reintento de timbrado - cada 15 min
*/15 * * * * root cd /opt/inovaweb-admin-financiera && docker compose --profile jobs run --rm invoice_retry >> /var/log/caf/invoice_retry.log 2>&1

# recordatorios de mora - 9 AM diario
0 9 * * * root cd /opt/inovaweb-admin-financiera && docker compose --profile jobs run --rm overdue_notifier >> /var/log/caf/overdue_notifier.log 2>&1
```

---

## 4. Integración con cores Nivel 1

### 4.1 Síntoma: alta de cliente falla con `provisioning_failed`

**Diagnóstico:**
```sql
SELECT * FROM audit_log
WHERE event_type = 'onboarding_failed'
ORDER BY id DESC LIMIT 5;
```
Buscar en el `event_data` qué core falló y en qué paso.

**Fix:**
1. Si el core estaba caído: reintentar el alta desde
   `/admin/clients/{id}/retry-provisioning` (sólo si el cliente sigue en
   estado `provisioning_failed`).
2. Si la API key del core fue revocada: emitir nueva en el core, actualizar
   `.env` del CAF, redeploy.
3. Si el core devolvió 4xx por datos inválidos: corregir los datos del
   cliente y reintentar.

**Verificación:**
- Cliente pasa a estado `active`.
- Hay 4 entradas en `audit_log` con `event_type='core_provisioned'` (una
  por core).

---

### 4.2 Síntoma: el saldo en portal cliente no coincide con medidor

**Diagnóstico:** El CAF nunca duplica saldo; siempre pide al medidor. Si
discrepan, es porque el medidor está reportando algo distinto:
```bash
docker exec -it caf_app python -c "
import asyncio
from app.core.clients.medidor_client import MedidorClient
async def f():
    c = MedidorClient()
    print(await c.get_balance('client-XXXX'))
asyncio.run(f())
"
```

**Fix:** corregir en el medidor, no en el CAF. El CAF es solo lector.

**Verificación:** comparar `GET /api/v2/clients/{id}/balance` con el
endpoint directo del medidor.

---

### 4.3 Síntoma: cargo de IA en la factura del cliente parece equivocado

**Contexto:** el medidor IA es la fuente única del costo de IA (ADR-009). El
CAF nunca recalcula tokens → pesos; solo agrega los eventos
`source=medidor, direction=debit` que el finanzas-core ya tiene.

**Diagnóstico:**

1. ¿El monto en la factura coincide con la suma de eventos del medidor?
```sql
-- en la BD del CAF, ver qué cargo IA puso la factura
SELECT id, total_cents, ai_charge_cents
FROM invoices
WHERE id = '<invoice_id>';

-- en finanzas-core, ver los eventos del medidor del período
SELECT sum(amount_cents)
FROM ledger_entries
WHERE source_slug = 'medidor'
  AND direction  = 'debit'
  AND tenant_id  = '<tenant_uuid>'
  AND occurred_at >= '<inicio_periodo>'
  AND occurred_at <  '<fin_periodo>';
```
Si los números difieren → bug en `app/services/billing.py` (agregación
incorrecta).

2. Si la suma del finanzas-core no se parece a lo que el cliente esperaba
   ver consumido → el problema está en el medidor (tarifa mal cargada, o
   tokens reportados incorrectamente por la IA proxy).

**Fix:**
- Discrepancia entre CAF y finanzas-core: corregir agregación en CAF y
  re-correr el cierre del período afectado (`POST /api/v2/billing/run-closing`
  con `period=YYYY-MM` y `force=true`).
- Discrepancia entre medidor y expectativa del cliente: NO tocar en CAF.
  Reportar al equipo del medidor; el medidor maneja tarifas, modelos
  habilitados y conversión tokens→pesos.

**Verificación:**
```bash
# que el agregado del CAF y del finanzas coincidan exactamente
docker exec -it caf_app python -c "
import asyncio
from app.services.billing import _ai_charge_for_period
asyncio.run(_ai_charge_for_period('<client_id>', '2026-05'))
"
```

---

### 4.4 Síntoma: cliente no recibe email de activación al darse de alta

**Contexto:** el onboarding (paso 5b) genera un token SHA-256 en `activation_tokens`
y llama al Centro de Mensajes con la plantilla `caf-activacion-correo`. Si el
email no llega, revisar en este orden:

**Diagnóstico:**
```sql
-- ¿se generó el token?
SELECT id, user_id, expires_at, used_at
FROM activation_tokens
WHERE user_id = (SELECT id FROM users WHERE email = 'cliente@ejemplo.com')
ORDER BY created_at DESC LIMIT 1;
```

```bash
# ¿el Centro de Mensajes recibió la solicitud?
ssh root@89.116.25.222 "docker logs inovaweb-centro-mensajes 2>&1 | grep 'caf-activacion-correo' | tail -10"

# ¿hay proveedor de email configurado en el Centro?
ssh root@89.116.25.222 "docker exec inovaweb-centro-mensajes env | grep -E 'RESEND|SMTP|SENDGRID'"
```

**Fix:**
- Token no generado: revisar logs del CAF — onboarding.py paso 5b.
- Centro sin proveedor de email: configurar `RESEND_API_KEY` o credenciales SMTP en `.env` del Centro de Mensajes y reiniciar.
- Plantilla `caf-activacion-correo` sin sembrar: correr el SQL del `seed-mensajes.md`.

**Verificación:**
```bash
# reenviar email manualmente (endpoint admin)
curl -X POST https://admin.inovaweb.com.mx/admin/clients/<id>/resend-activation \
  -H "Authorization: Bearer <jwt>"
```

---

### 4.5 Síntoma: concepto de factura falta en el cierre mensual (`pricing_missing`)

**Contexto:** `billing.py` llama a `pricing.price_quantity()` para cada canal de
consumo. Si no hay precio activo en `price_catalog` para esa unidad en el
periodo, el concepto se omite con nivel WARNING (no rompe el cierre).

**Diagnóstico:**
```bash
# buscar en logs los canales omitidos por falta de precio
docker logs caf_app 2>&1 | grep "pricing_missing" | tail -20
```

```sql
-- ver precios activos hoy
SELECT meter, unit_code, amount_cents, valid_from, valid_to
FROM price_catalog
WHERE (valid_to IS NULL OR valid_to >= CURRENT_DATE)
ORDER BY meter, unit_code;
```

**Fix:**
```sql
-- insertar precio faltante (ejemplo: email no tenía precio)
INSERT INTO price_catalog (meter, unit_code, amount_cents, valid_from)
VALUES ('message', 'email', 100, CURRENT_DATE);  -- $1.00 MXN por email
```

**Verificación:** re-correr el cierre del periodo afectado:
```bash
curl -X POST https://admin.inovaweb.com.mx/api/v2/billing/run-closing \
  -H "Authorization: Bearer <jwt>" \
  -d '{"period": "YYYY-MM", "force": true}'
```

---

## 5. Webhooks

### 5.1 Síntoma: PAC dice que envió webhook pero la factura sigue en
`stamp_pending`

**Diagnóstico:**
```bash
# logs de webhooks recibidos
docker logs caf_app 2>&1 | grep "webhook_received" | tail -20

# verificar firma del webhook
# si el PAC firma con HMAC y el secret cambió, el webhook se rechaza
grep PAC_WEBHOOK_SECRET /opt/inovaweb-admin-financiera/.env
```

**Fix:**
- Si la firma falla: validar `PAC_WEBHOOK_SECRET` contra dashboard del PAC.
- Si el webhook no llega: probar el endpoint manualmente desde el dashboard
  del PAC (reentregar evento).

**Verificación:**
- Factura pasa a `stamped` con `stamp_uuid` poblado.

---

### 5.2 Síntoma: recarga / compra de plan queda colgada (Hub-Pasarelas)

**Contexto:** el flujo prepago (`app/services/prepago.py`) abre el cargo en el
Hub con `initiate_charge` (deja `recharge.initiated` en `audit_log` con
`recharge_id`/`purpose`/`amount_cents`). Al pagar, el Hub manda
`POST /webhooks/hub-payment-paid`; `process_paid_event` reclama el pago con
idempotencia BD (`uq_payments_hub`), valida purpose/amount contra el intento y
acredita la wallet del cliente en el Medidor (`credit`, idempotente por
`request_id = caf-recharge-{recharge_id}`).

**Diagnóstico:**
```sql
-- intento abierto sin pago confirmado
SELECT * FROM audit_log
WHERE event_type = 'recharge.initiated'
  AND created_at < now() - interval '15 min'
ORDER BY id DESC LIMIT 10;

-- ¿llegó el webhook? (pago reclamado en payments)
SELECT id, hub_payment_id, amount_cents, created_at
FROM payments
WHERE hub_payment_id IS NOT NULL
ORDER BY id DESC LIMIT 10;

-- ¿fue rechazado por purpose/amount distinto al intento? (FIX-2)
SELECT * FROM audit_log
WHERE event_type IN ('hub.paid.rejected','hub.paid.failed')
ORDER BY id DESC LIMIT 10;
```

**Fix:**
- Verificar en el Hub si la transacción fue capturada (puede haber sido
  abandonada por el cliente).
- Si el Hub la confirma pero el webhook no llegó: reentregar el evento desde el
  Hub. El reintento es seguro: la idempotencia (BD + `request_id` del Medidor)
  evita doble cargo / doble crédito / doble correo.
- Si el webhook fue **rechazado** (`hub.paid.rejected`): el `purpose`/`amount`
  no coincidió con el intento local → revisar que el metadata enviado al Hub en
  `initiate_charge` no se haya alterado.
- Si la firma HMAC o el timestamp fallan (401): validar `HUB_WEBHOOK_SECRET`
  contra el dashboard del Hub y el reloj del VPS (NTP).

**Verificación:**
```sql
-- el pago quedó reclamado una sola vez
SELECT count(*) FROM payments WHERE hub_payment_id = '<hub_txn_id>';
```
Debe ser exactamente 1, y la wallet del cliente en el Medidor debe reflejar el
crédito (consultar `GET /v1/wallets/{id}/balance` del Medidor).

---

## 6. Auth / sesiones

### 6.1 Síntoma: usuario bloqueado por `failed_attempts`

**Diagnóstico:**
```sql
SELECT email, failed_attempts, locked_until
FROM users
WHERE email = 'usuario@cliente.com';
```

**Fix (super-admin):**
```sql
UPDATE users
SET failed_attempts = 0, locked_until = NULL
WHERE email = 'usuario@cliente.com';
-- la auditoría se dispara automáticamente
```

**Verificación:** el usuario puede loguearse de nuevo.

---

### 6.2 Síntoma: sospecha de robo de refresh token

**Diagnóstico:** en `refresh_tokens`, buscar entradas con
`revoked_reason='reuse_attempt'`:
```sql
SELECT * FROM refresh_tokens
WHERE revoked_reason = 'reuse_attempt'
ORDER BY id DESC LIMIT 20;
```

**Fix:** la rotación ya invalidó la cadena completa del usuario afectado;
no se requiere acción adicional. Notificar al usuario para que confirme
y considere cambio de password.

**Verificación:**
- Usuario fuerza login nuevo.
- Toda actividad post-incidente queda registrada con nuevo `session_id`.

---

## 7. Seguridad — incidentes

### 7.1 Sospecha de credencial filtrada (DB / API keys cores / PAC)

1. **Rotar inmediatamente.** Generar nueva, actualizar `.env`, redeploy.
2. **Revocar la vieja** en el sistema que la emite (Postgres, cada core,
   PAC).
3. **Auditar** desde `audit_log` qué se hizo con esa credencial mientras
   estuvo activa.
4. **Incidente formal** según `SECURITY.md` (postmortem + comunicación
   a clientes si hubo exposición de datos).

### 7.2 Pérdida del CSD (certificado de sello digital)

1. Solicitar nuevo CSD al SAT (proceso oficial).
2. Mientras tanto, todas las facturas quedan en `stamp_pending`; el cliente
   externo NO debe poder cobrar (suspender altas que requieran factura).
3. Una vez emitido el nuevo CSD: colocar en `/opt/inovaweb-admin-financiera/secrets/`,
   actualizar `KEY_PASSWORD` en `.env`, redeploy y disparar `invoice_retry`.

---

## 8. Backups

`[TODO: completar tras ADR-010]`. Por ahora:
- Snapshot manual antes de cualquier deploy con migración:
```bash
docker exec caf_postgres pg_dump -U caf -d admin_financiera \
  | gzip > /backups/caf-$(date +%Y%m%d-%H%M%S).sql.gz
```
- Probar restauración en un Postgres temporal cada 30 días.

---

## 9. Escalamiento

| Severidad | Quién | Cuándo |
|---|---|---|
| Sev1: PAC caído + cierre mensual en curso | super-admin + dirección | inmediato |
| Sev1: pérdida de CSD | super-admin + contador | inmediato |
| Sev2: backend `caf_app` caído > 5 min | super-admin | inmediato |
| Sev2: workers detenidos > 2 días | super-admin | mismo día |
| Sev3: usuario bloqueado | finanzas | mismo día |
| Sev3: factura mal emitida | finanzas + cliente | siguiente día hábil |


---

## 10. Saldo prepago nativo del CAF + apps consumidoras (Bearer)

Desde 2026-06-11 el saldo monetario vive en el CAF (`prepaid_ledger` + `v_client_balance`), no en el
Medidor (ADR-015). Las apps (LiaForge/Scraping, Swigg) cobran vía Bearer (`SCRAPING_ADMIN_KEY` /
`SWIGG_ADMIN_KEY`).

### 10.1 Síntoma: una app reporta `402 saldo_insuficiente` al cobrar
**Diagnóstico** — es el comportamiento esperado de `POST /clients/{id}/charge` cuando
`v_client_balance < unit_price_cents * units`. Verificar el saldo real:
```bash
docker compose exec -T postgres psql -U caf -d admin_financiera \
  -c "SELECT * FROM v_client_balance WHERE client_id=<ID>;"
# últimos movimientos
docker compose exec -T postgres psql -U caf -d admin_financiera \
  -c "SELECT created_at,kind,service_code,units,amount_cents,source FROM prepaid_ledger WHERE client_id=<ID> ORDER BY created_at DESC LIMIT 20;"
```
**Fix** — el cliente debe recargar (flujo Hub-Pasarelas; la recarga acredita el `prepaid_ledger`).
NO acreditar a mano salvo corrección auditada (append-only: insertar un `credit` con `source` y motivo,
nunca UPDATE).
**Verificar** — reintentar el `charge`; debe devolver 200 con `balance_cents` actualizado.

### 10.2 Síntoma: un `charge` se duplicó (doble cobro)
**Diagnóstico** — no debería ocurrir: el cobro es idempotente por `(client_id, idempotency_key)` y hay
`pg_advisory_xact_lock(client_id)`. Si se ve doble débito, la app mandó **dos `idempotency_key`
distintas** para la misma operación. Confirmar:
```bash
docker compose exec -T postgres psql -U caf -d admin_financiera \
  -c "SELECT idempotency_key,count(*) FROM prepaid_ledger WHERE client_id=<ID> AND kind='debit' GROUP BY 1 HAVING count(*)>1;"
```
**Fix** — corregir la app para reusar `idempotency_key = projectId+runId+callIndex`. La reversión de un
cobro erróneo se hace con un `credit` compensatorio auditado, no con DELETE.

### 10.3 Dar de alta una **app consumidora nueva** (ej. ConductorPlay)
Todo es **additive** salvo un append de código. Orden:
```bash
# 1) sembrar servicios y planes de la app (additive; verificar columnas con \d services / \d plans)
docker compose exec -T postgres psql -U caf -d admin_financiera \
  -c "INSERT INTO services (code,name,unit,unit_price_cents,is_active) VALUES ('<code>','<n>','<unit>',<cents>,true);"
docker compose exec -T postgres psql -U caf -d admin_financiera \
  -c "INSERT INTO plans (code,name,monthly_credit_cents) VALUES ('<app>_<plan>','<n>',<cents_o_0>);"
# 2) llave Bearer propia de la app: generar y poner en .env del CAF
openssl rand -hex 32   # -> <APP>_ADMIN_KEY en .env
# 3) UNICO cambio de codigo: append en app/routers/api_router.py::_verify_app_key
#    if getattr(s,'<APP>_ADMIN_KEY',None) is not None: keys.append(s.<APP>_ADMIN_KEY.get_secret_value())
#    + declarar <APP>_ADMIN_KEY en app/core/config.py
docker compose up -d --build admin_financiera
```
**Regla** — NO tocar ni `SCRAPING_ADMIN_KEY` ni `SWIGG_ADMIN_KEY` ni la lógica de cobro: solo
*agregar*. Verificar que LiaForge/Swigg siguen autenticando con su Bearer de siempre.

### 10.4 Síntoma: el saldo del portal (Medidor) no coincide con `v_client_balance`
Durante la transición la recarga hace **dual-write** (CAF + Medidor). La **fuente del saldo es el CAF**
(`v_client_balance`). Si divergen, confiar en el CAF y revisar `prepago.py` (idempotencia por `req_id`).
Ver también §4.2 (saldo Medidor) que ahora es secundario.

---

## 11. Componentes de la sesión 2026-06-16 (SaaS multi-tenant)

> Comandos contra la BD vía: `ssh root@89.116.25.222` y luego
> `cd /opt/inovaweb-admin-financiera && docker compose exec -T postgres psql -U caf -d admin_financiera`.
> Comandos contra el backend: contenedor de servicio `admin_financiera` (host 8006).

### 11.1 Aislamiento multi-tenant (organization_id)

**Síntoma:** una organización ve o modifica datos (clientes, saldos, proveedores,
ajustes, reportes) de otra organización.

**Diagnóstico:** el tenant SIEMPRE sale de la API key o del JWT, **NUNCA del body**
(regla rector 7). Cada endpoint debe acotar por `organization_id` usando uno de los
tres helpers, según su tipo de auth:
- Endpoints **app-facing** (Bearer API key, `app/routers/api_router.py`): deben usar
  `resolve_app_org(request, db)` (`app/core/tenancy.py`) para obtener el `organization_id`
  dueño de la llave, y `assert_client_in_org(db, client_id, org_id)` antes de operar
  sobre cualquier `client_id` recibido (404 si el cliente es de otra org).
- Endpoints **JWT** del panel/portal (`admin_router.py`, `email_providers_router.py`,
  `client_account_router.py`, `adjustments_router.py`, `reports_router.py`): deben usar
  `_org_scope(user)` (o el `_resolve_org(user, org)` del router de email), que toma
  `user.organization_id` y solo respeta `?org=<id>` cuando `user.is_platform`.

Para auditar un endpoint sospechoso, confirmar que aparece uno de esos helpers y que
ningún `WHERE` omite `organization_id`:
```bash
docker compose exec -T admin_financiera grep -nE "resolve_app_org|assert_client_in_org|_org_scope|_resolve_org" app/routers/<router>.py
```
Verificación cruzada de fugas a nivel datos:
```sql
-- ¿algún cliente quedó sin org? (no debería existir)
SELECT id, legal_name FROM clients WHERE organization_id IS NULL;
-- ¿qué org posee una API key concreta?
SELECT organization_id, revoked_at, last_used_at FROM api_keys WHERE key_hash = encode(digest('<token>','sha256'),'hex');
```

**Fix:** corregir el endpoint para que resuelva el tenant del token (helper correcto)
y filtre por `organization_id`. NUNCA aceptar `organization_id` desde el cuerpo de la
petición. Las llaves legacy `SCRAPING_ADMIN_KEY`/`SWIGG_ADMIN_KEY` caen a la org
plataforma (`PLATFORM_ORG_ID = 1`) por diseño — esto es esperado, no una fuga.

**Verificación:** repetir la operación con la llave/JWT de la org B sobre un recurso de
la org A → debe responder 404 (`assert_client_in_org`) o 403 (`_resolve_org`/`_load_provider_scoped`).
Existe un script de regresión: `scripts/verify_catalog_isolation.sh`.

---

### 11.2 Pasarelas de pago (front del CAF → Hub-Pasarelas)

**Síntoma:** el panel `/admin/payment-gateways` "no guarda credenciales".

**Diagnóstico:** el front del CAF NO persiste credenciales; las reenvía al Hub, que
las cifra y guarda. Requiere `HUB_ADMIN_KEY` (scope `admin:gateways`) en el `.env` y
que el endpoint admin del Hub responda. Sin `HUB_ADMIN_KEY` el guardado redirige a
`?saved=error_sin_admin_key`.
```bash
# ¿está la llave admin del Hub?
grep -E "^HUB_ADMIN_KEY=" /opt/inovaweb-admin-financiera/.env
# ¿responde el endpoint admin del Hub para el tenant del CAF (HUB_COMPANY_ID)?
HUB_ADMIN_KEY=$(grep -E "^HUB_ADMIN_KEY=" /opt/inovaweb-admin-financiera/.env | cut -d= -f2-)
HUB_COMPANY_ID=$(grep -E "^HUB_COMPANY_ID=" /opt/inovaweb-admin-financiera/.env | cut -d= -f2-)
HUB_BASE_URL=$(grep -E "^HUB_BASE_URL=" /opt/inovaweb-admin-financiera/.env | cut -d= -f2-)
curl -s "${HUB_BASE_URL}/admin/hub/v1/gateway-config?company_id=${HUB_COMPANY_ID:-b5237689-c2b3-48b5-8faa-595fc41dc0c7}" \
  -H "Authorization: Bearer ${HUB_ADMIN_KEY}" | jq
```
(El default de `HUB_COMPANY_ID` en `config.py` es `b5237689-c2b3-48b5-8faa-595fc41dc0c7`.)

**Fix:** colocar/corregir `HUB_ADMIN_KEY` en el `.env` del CAF y `docker compose up -d
admin_financiera`. Si el Hub responde 401, revalidar la llave contra el Hub. El cliente
de admin del Hub está en `app/core/clients/hub_client.py` (`HubAdminClient`:
`list_gateways` / `save_gateway` GET·POST `/admin/hub/v1/gateway-config`, `set_default`
POST `/admin/hub/v1/gateway-default`).

---

**Síntoma:** "no cobra por la pasarela elegida" (recarga sale por otra pasarela).

**Diagnóstico:** el flujo prepago (`prepago.py::_resolve_gateway`) cobra con la pasarela
**default ACTIVA del Hub** (la que el operador elige en `/admin/payment-gateways`). Si
no puede resolverla (sin `HUB_ADMIN_KEY` o el Hub falla), cae al fallback `HUB_GATEWAY`
del `.env` (default `"mock"` en `config.py`).
```sql
-- ¿qué pasarela default tiene el panel? (selector); el valor real vive en el Hub
```
```bash
# default real según el Hub (mismo curl de arriba): buscar is_default=true AND is_active=true
curl -s "${HUB_BASE_URL}/admin/hub/v1/gateway-config?company_id=${HUB_COMPANY_ID}" \
  -H "Authorization: Bearer ${HUB_ADMIN_KEY}" | jq '.configured[] | select(.is_default and .is_active)'
# fallback configurado en el CAF
grep -E "^HUB_GATEWAY=" /opt/inovaweb-admin-financiera/.env
```

**Fix:** fijar la pasarela correcta como default desde el selector de
`/admin/payment-gateways` (POST `/admin/payment-gateways/default`, llama a
`HubAdminClient.set_default`). Asegurar que esa pasarela esté `is_active`. Si se está
cayendo al fallback no deseado, corregir `HUB_ADMIN_KEY`/conexión al Hub para que
`_resolve_gateway` resuelva el default real.

**Verificación:** iniciar una recarga de prueba y confirmar en `audit_log`
(`recharge.initiated`) y en el Hub que la transacción salió por la pasarela esperada.

---

### 11.3 Email por proveedor (crypto AES-256-GCM)

**Síntoma:** "secreto ilegible / no envía"; el test SMTP en `/admin/email-providers`
devuelve `secreto inválido o ilegible (revisar AES_KEY)`.

**Diagnóstico:** las credenciales de email se guardan cifradas con AES-256-GCM
(`app/core/crypto.py`, columna `email_providers.secret_encrypted`, tokens con prefijo
`v1.`). El error casi siempre es `AES_KEY` mal: debe ser base64 que decodifique a
EXACTAMENTE 32 bytes; si no, `crypto._key()` aborta con `ValueError`. Round-trip de
cordura del cifrado (debe imprimir `True`):
```bash
docker compose exec -T admin_financiera python -c "from app.core.crypto import encrypt_secret,decrypt_secret; t=encrypt_secret('x'); print(decrypt_secret(t)=='x')"
```
Si imprime `True` pero un proveedor concreto sigue ilegible, ese secreto se cifró con
OTRA `AES_KEY` (rotación previa sin re-cifrar). Identificar proveedores afectados:
```sql
SELECT id, organization_id, client_id, provider, left(secret_encrypted, 3) AS pref
FROM email_providers WHERE secret_encrypted IS NOT NULL;
```

**Fix:**
- `AES_KEY` mala → corregir el valor en el `.env` (base64 → 32 bytes) y
  `docker compose up -d admin_financiera`.
- **NO rotar `AES_KEY`** sin re-cifrar primero todos los `email_providers.secret_encrypted`
  con la llave nueva (descifrar con la vieja → cifrar con la nueva). Rotar a secas deja
  todos los secretos ilegibles.
- Si un secreto quedó huérfano, re-capturarlo desde el panel
  (PATCH `/admin/email-providers/{id}` con `secret`, que lo re-cifra).

**Verificación:** `POST /admin/email-providers/{id}/test` → `{"ok": true, ...}` (connect +
login SMTP). El endpoint nunca devuelve el secreto en claro.

---

### 11.4 Meta-cobro SaaS (el CAF se cobra a sí mismo)

**Contexto:** la org plataforma (`PLATFORM_ORG_ID = 1`) cobra a cada org cliente su
consumo del motor. Cada org se representa como un "cliente de plataforma"
(`organizations.platform_client_id` → `clients.id`, org 1). El consumo se acumula como
DÉBITOS POSTPAGO en `prepaid_ledger` (puede dejar saldo negativo).
`app/services/saas_billing.py`.

**Síntoma:** "no acumula" o "duplica" el consumo SaaS de una org.

**Diagnóstico:** el accrual por transacción (`accrue_transaction`) es **best-effort**:
corre en su propia sesión, jamás propaga excepción, e idempotente por
`idempotency_key = saas-tx-{source_ref}`. Si no acumula, buscar sus logs (no rompe el
flujo de negocio que lo dispara):
```bash
docker compose logs --tail 300 admin_financiera | grep -E "saas_accrue_(no_platform_client|no_service|transaction_failed)"
```
- `saas_accrue_no_platform_client` → la org no tiene `platform_client_id` (no se corrió
  `register_org_as_platform_client`; requiere migración 035 con la columna).
- `saas_accrue_no_service` → falta el servicio `saas_transaccion` en la org 1.
- "duplica" → revisar que `source_ref` sea estable; el `ON CONFLICT` por
  `idempotency_key` evita el doble débito si el `source_ref` se repite.
```sql
-- débitos SaaS acumulados de una org (vía su cliente de plataforma)
SELECT pl.created_at, pl.source, pl.amount_cents, pl.idempotency_key
FROM prepaid_ledger pl
JOIN organizations o ON o.platform_client_id = pl.client_id
WHERE o.id = <ORG_ID> AND pl.source IN ('saas_usage','saas_fee')
ORDER BY pl.created_at DESC LIMIT 30;
```

**Fix:**
- Sembrar lo que falte: servicio `saas_transaccion` (99c) y plan `caf_saas` (9900/mes)
  en la org 1; correr `register_org_as_platform_client` para poblar `platform_client_id`.
- El **cierre mensual** (cuota del plan) se corre con el script idempotente (cron día 1):
  ```bash
  /opt/inovaweb-admin-financiera/scripts/run_saas_monthly_billing.sh
  # equivale a run_saas_monthly_billing(db, 'YYYY-MM'); correr de más NO duplica
  # (idempotency_key = saas-fee-{platform_client_id}-{period})
  ```

**Verificación:** estado de cuenta SaaS de la org (saldo negativo = debe; consumo del mes):
```bash
docker compose exec -T admin_financiera python -c "
import asyncio
from app.core.database import SessionLocal
from app.services.saas_billing import get_saas_account
async def main():
    async with SessionLocal() as db:
        print(await get_saas_account(db, <ORG_ID>))
asyncio.run(main())
"
```

---

### 11.5 Sesión / login (panel y portal)

**Contexto:** auth por JWT en cookie httpOnly. El TTL del access token lo gobierna
`JWT_ACCESS_TTL_MIN` del `.env` (operativamente ~12h; el default de `config.py` es 15
min y se sobrescribe en prod) y el refresh `JWT_REFRESH_TTL_DAYS`. Verificar el valor
real:
```bash
grep -E "^JWT_ACCESS_TTL_MIN=|^JWT_REFRESH_TTL_DAYS=" /opt/inovaweb-admin-financiera/.env
```

**Síntoma:** "me sacó de la sesión" en el panel.

**Diagnóstico/Fix:** comportamiento esperado al expirar el token → re-loguear. En
páginas HTML del panel/portal, un 401 NO devuelve JSON: el handler de
`StarletteHTTPException` en `app/main.py` redirige a `/login` con 303 (sesión caducada).
Si el 401 persiste tras re-login válido, revisar rotación de `JWT_SECRET` (invalida
todos los tokens previos — esperado) y §1.4.

**Síntoma:** resetear el password de un usuario (super-admin/operación).
```sql
UPDATE users
SET password_hash = hash_password('<nueva_contraseña>'),
    failed_attempts = 0,
    locked_until = NULL
WHERE email = '<usuario@cliente.com>';
```
Ejecutar vía el contenedor para tener `hash_password` (Argon2) disponible:
```bash
docker compose exec -T admin_financiera python -c "
import asyncio
from sqlalchemy import text
from app.core.database import SessionLocal
from app.core.password import hash_password
async def main():
    async with SessionLocal() as db:
        await db.execute(text(\"UPDATE users SET password_hash=:h, failed_attempts=0, locked_until=NULL WHERE email=:e\"),
                         {'h': hash_password('<nueva_contraseña>'), 'e': '<usuario@cliente.com>'})
        await db.commit()
asyncio.run(main())
"
```
(El hash de Argon2 NO se puede generar en SQL puro; por eso el `UPDATE` con
`hash_password` corre dentro del contenedor.)

**Verificación:** el usuario inicia sesión en `https://admin.inovaweb.com.mx/login`
(o `app.inovaweb.com.mx`) con la nueva contraseña; `failed_attempts=0` y
`locked_until=NULL`.

---

### 11.6 Promociones / código de distribuidor

**Contexto:** los códigos de promoción viven en la tabla `promotions` (por
`organization_id`), con `max_uses` y `uses_count`. El código de distribuidor se aplica
en el self-service de apps (`POST /apps/onboard`, `app/routers/api_router.py`): si el
código es válido y `uses_count < max_uses`, incrementa `uses_count` y aplica su
`discount_pct` al crédito otorgado. También hay cupones en `app/services/promotions.py`.

**Síntoma:** un código "ya no aplica" o se quiere auditar su uso.

**Diagnóstico:** revisar el contador y el tope del código:
```sql
SELECT id, code, kind, discount_pct, discount_cents,
       max_uses, uses_count, is_active, distributor_id
FROM promotions
WHERE code = '<CODIGO_EN_MAYUSCULAS>';
```
- `is_active = false` o `max_uses` alcanzado (`uses_count >= max_uses`) → el código deja
  de aplicar (esperado). El onboard normaliza el código a MAYÚSCULAS y `.strip()` antes
  de buscarlo.

**Fix:** subir `max_uses` o reactivar el código con un UPDATE auditado en `promotions`
(la tabla de promociones es administrable, no es append-only financiero):
```sql
UPDATE promotions SET max_uses = <nuevo_tope>, is_active = true
WHERE code = '<CODIGO>' AND organization_id = <ORG_ID>;
```
NO decrementar `uses_count` a mano salvo corrección de un uso erróneo plenamente
justificado.

**Verificación:** repetir `POST /apps/onboard` con el `promo_code` → la respuesta trae
`promo_applied: true` y el `granted_cents` refleja el bono; `uses_count` sube en 1.

