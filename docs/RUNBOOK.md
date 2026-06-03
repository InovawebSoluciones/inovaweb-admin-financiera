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

### 5.2 Síntoma: recarga del cliente queda colgada (Hub-Pasarelas)

**Diagnóstico:**
```sql
SELECT * FROM recharge_intents
WHERE status = 'pending'
  AND created_at < now() - interval '15 min'
ORDER BY id DESC LIMIT 10;
```

**Fix:**
- Verificar en el Hub si la transacción fue capturada (puede haber sido
  abandonada por el cliente).
- Si el Hub la confirma pero el webhook no llegó: reintegrar desde el Hub.

**Verificación:**
```sql
SELECT status, amount_cents, hub_transaction_id
FROM recharge_intents
WHERE id = XXX;
```
Debe quedar en `confirmed` y el medidor con crédito aplicado.

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
