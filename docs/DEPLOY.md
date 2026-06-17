# Guía de deploy — inovaweb-admin-financiera

Procedimiento operado por **operador con acceso SSH al VPS Contabo**. Sin
CI/CD aún (planeado para sprint posterior); por ahora todo deploy es
manual. La guía cubre primer arranque y deploys incrementales.

VPS: `root@89.116.25.222` · directorio: `/opt/inovaweb-admin-financiera`.

---

## 1. Pre-requisitos

| Recurso | Cómo verificar | Cómo obtener |
|---|---|---|
| Acceso SSH al VPS | `ssh root@89.116.25.222` | Solicitar a super-admin |
| `git` instalado en VPS | `which git` | `apt install git` |
| Docker 24+ en VPS | `docker --version` | Ya instalado en el VPS |
| Docker Compose v2 | `docker compose version` | Ya instalado en el VPS |
| Red Docker `n8n_default` existente | `docker network ls \| grep n8n_default` | Stack n8n del VPS la crea |
| Caddy del stack n8n corriendo | `docker ps \| grep caddy` | Stack n8n |
| Archivo `.env` completo en VPS | `cat /opt/inovaweb-admin-financiera/.env` | Generar a partir de `.env.example` |
| CSD del SAT (sólo prod) | `ls /opt/inovaweb-admin-financiera/secrets/` | Tramitar con contador |
| Llaves admin de los 4 cores Nivel 1 (medidor IA, hub-pasarelas, finanzas-core, centro-mensajes) | Variables `MEDIDOR_API_KEY` / `HUB_API_KEY` / `FINANZAS_API_KEY` / `MESSAGES_API_KEY` en `.env` | Emitir vía SQL en cada core (ver `docs/01-admin-financiera-integracion-cores.md`). El medidor IA es la fuente única del costo de IA por cliente (ADR-009); sin su API key el CAF no puede leer saldo ni consumo |
| Llaves del PAC | `PAC_API_KEY`, `PAC_API_SECRET` en `.env` | Dashboard del PAC contratado |

---

## 2. Primer arranque (bootstrap del VPS)

```bash
ssh root@89.116.25.222

# 1. clonar
cd /opt
git clone https://github.com/InovawebSoluciones/inovaweb-admin-financiera.git
cd inovaweb-admin-financiera

# 2. crear .env (a partir del .env.example)
cp .env.example .env
nano .env
# - ENV=prod
# - JWT_SECRET / AES_KEY: 32 bytes URL-safe (generar con secrets.token_urlsafe(32))
# - POSTGRES_PASSWORD: fuerte
# - URLs y API keys de los 4 cores Nivel 1
#   (MEDIDOR_API_KEY con scope ADMIN, label core-admin-financiera; emitir con vps/04 del Medidor)
# - HUB_WEBHOOK_SECRET: secreto HMAC dedicado del webhook del Hub (OBLIGATORIO en prod)
# - credenciales del PAC (diferidas mientras el piloto sea prepago)
# - RFC_EMISOR

# 3. colocar CSD (sólo prod)
mkdir -p secrets
# copiar csd.cer y csd.key vía scp desde Windows del operador
chmod 600 secrets/csd.*

# 4. logs persistentes para workers (referenciados por cron)
mkdir -p /var/log/caf
touch /var/log/caf/{monthly_closing,invoice_retry,overdue_notifier}.log

# 5. levantar el stack
docker compose up -d --build

# 6. esperar a que postgres y la app estén healthy (~30s)
watch -n2 'docker ps --filter "name=caf_" --format "{{.Names}}\t{{.Status}}"'

# 7. verificar
curl -s http://localhost:8006/health
curl -s http://localhost:8006/health/db

# 8. instalar cron de los workers
crontab -e
# pegar la tabla de cron del RUNBOOK §3.4

# 9. confirmar que Caddy del stack n8n enruta los dos dominios
#    (Caddyfile de referencia en raíz del repo)
curl -sI https://admin.inovaweb.com.mx/health
curl -sI https://app.inovaweb.com.mx/health
```

---

## 3. Deploy incremental de backend

### 3.1 Procedimiento estándar (sin migración)

```bash
ssh root@89.116.25.222
cd /opt/inovaweb-admin-financiera

# 1. ver qué se va a aplicar
git fetch origin
git log --oneline HEAD..origin/main

# 2. backup express de la BD (antes de cualquier deploy)
docker exec caf_postgres pg_dump -U caf -d admin_financiera \
  | gzip > /backups/caf-$(date +%Y%m%d-%H%M%S).sql.gz

# 3. aplicar
git pull --ff-only
docker compose up -d --build admin_financiera

# 4. esperar healthy y verificar
sleep 5
docker logs --tail 30 caf_app | grep -E "caf_startup|ERROR"
curl -s http://localhost:8006/health
curl -s http://localhost:8006/health/db
```

### 3.2 Verificación post-deploy

Checklist mínimo, todos deben pasar:

| # | Check | Comando |
|---|---|---|
| 1 | `caf_app` healthy | `docker ps \| grep caf_app \| grep healthy` |
| 2 | `caf_postgres` healthy | `docker ps \| grep caf_postgres \| grep healthy` |
| 3 | `/health` 200 | `curl -fsS http://localhost:8006/health` |
| 4 | `/health/db` 200 | `curl -fsS http://localhost:8006/health/db` |
| 5 | Caddy enruta admin | `curl -sI https://admin.inovaweb.com.mx/health \| head -1` |
| 6 | Caddy enruta portal | `curl -sI https://app.inovaweb.com.mx/health \| head -1` |
| 7 | Login admin funcional | Manual en `https://admin.inovaweb.com.mx/login` |
| 8 | Logs sin ERROR recientes | `docker logs --tail 200 caf_app \| grep ERROR` |
| 9 | Sin facturas atascadas nuevas | `SELECT count(*) FROM invoices WHERE status='stamp_pending' AND created_at > now()-interval '1 hour';` |
| 10 | Audit log activo | `SELECT max(created_at) FROM audit_log;` |

Si falla cualquier check 1–6, hacer rollback (§6).
Si falla 7–10, evaluar caso por caso; pueden no requerir rollback.

---

## 4. Migraciones SQL

### 4.0 Migraciones acumuladas (003 → 007)

Aplicar en orden tras `001`+`002`. Todas son idempotentes o append-only.

| Archivo | Qué hace | Sesión |
|---------|----------|--------|
| `003_seed_scraping_plans.sql` | Seed planes Free/Básico/Medio/Premium Scraping | 2026-06-04 |
| `004_payments_idempotency.sql` | Índice UNIQUE `uq_payments_hub` en `payments` | 2026-06-04 |
| `005_activation_tokens.sql` | Tabla `activation_tokens` — tokens SHA-256 activación email | 2026-06-07 |
| `006_idempotencia.sql` | Índice UNIQUE parcial `clients(request_id)` | 2026-06-07 |
| `007_price_catalog.sql` | Tabla `price_catalog` + seed precios públicos por canal | 2026-06-07 |

Aplicar las migraciones 005-007 desde PowerShell (si aún no están aplicadas):
```powershell
# backup previo
ssh root@89.116.25.222 "docker exec caf_postgres pg_dump -U caf -d admin_financiera | gzip > /backups/caf-pre-grupo3-$(date +%Y%m%d-%H%M%S).sql.gz"

foreach ($f in @("005_activation_tokens.sql","006_idempotencia.sql","007_price_catalog.sql")) {
  Get-Content ".\database\$f" `
    | ssh root@89.116.25.222 "docker exec -i caf_postgres psql -U caf -d admin_financiera -v ON_ERROR_STOP=1"
  Write-Host "$f OK"
}
```

Verificar:
```bash
docker exec caf_postgres psql -U caf -d admin_financiera \
  -c "\dt" | grep -E "activation_tokens|price_catalog"
# debe listar ambas tablas nuevas
```

### 4.1 Regla crítica

**Desde PowerShell (Windows del operador) SIEMPRE usar `Get-Content | ssh`,
NUNCA el operador `<`.** El operador `<` en PowerShell tiene comportamiento
distinto a Bash y no redirige stdin correctamente.

### 4.2 Procedimiento

```powershell
# 1. backup ANTES de migrar
ssh root@89.116.25.222 "docker exec caf_postgres pg_dump -U caf -d admin_financiera | gzip > /backups/caf-pre-migrate-$(date +%Y%m%d-%H%M%S).sql.gz"

# 2. transferir el SQL al VPS (opcional, también se puede pipe directo)
scp "C:\path\local\003_nueva_migracion.sql" root@89.116.25.222:/tmp/

# 3. aplicar
Get-Content "C:\path\local\003_nueva_migracion.sql" `
  | ssh root@89.116.25.222 "docker exec -i caf_postgres psql -U caf -d admin_financiera -v ON_ERROR_STOP=1"

# 4. verificar tablas/columnas afectadas
ssh root@89.116.25.222 "docker exec caf_postgres psql -U caf -d admin_financiera -c '\d nueva_tabla'"

# 5. si la migración renombra/cambia algo que la app vieja todavía usa,
#    desplegar la nueva versión del backend INMEDIATAMENTE después
ssh root@89.116.25.222 "cd /opt/inovaweb-admin-financiera && git pull && docker compose up -d --build admin_financiera"
```

### 4.3 Migraciones que requieren downtime

Si la migración:
- añade `NOT NULL` sin default a tabla grande,
- crea índice sin `CONCURRENTLY`,
- elimina columna en uso por la app activa,

→ entonces requiere ventana de mantenimiento:
```bash
docker compose stop admin_financiera
# aplicar migración
docker compose start admin_financiera
```

Anunciar la ventana en `centro-mensajes` 24h antes a clientes con
notificaciones activas.

---

## 5. Deploy de templates HTML (sin restart)

Los templates Jinja2 viven dentro de la imagen Docker (`COPY app ./app`).
Para refresh de templates **sin rebuild**:

```bash
# 1. transferir templates modificados
scp -r "C:\path\local\inovaweb-admin-financiera\app\templates\" `
  root@89.116.25.222:/opt/inovaweb-admin-financiera/app/templates/

# 2. copiar dentro del contenedor (sobreescribe los del build)
ssh root@89.116.25.222 "docker cp /opt/inovaweb-admin-financiera/app/templates/. caf_app:/app/app/templates/"

# 3. Jinja2 con auto_reload solo en dev; en prod hay que reiniciar
ssh root@89.116.25.222 "docker compose restart admin_financiera"
```

**Recomendado:** evitar este shortcut en prod; commitear y hacer deploy
completo (§3) para mantener trazabilidad git.

---

## 6. Rollback

### 6.1 Rollback de código (sin migración)

```bash
ssh root@89.116.25.222
cd /opt/inovaweb-admin-financiera

# encontrar el commit previo estable
git log --oneline -10

# checkout y rebuild
git checkout <sha-anterior>
docker compose up -d --build admin_financiera

# verificar
curl -fsS http://localhost:8006/health
```

### 6.2 Rollback con migración

Las migraciones son **append-only** por filosofía del proyecto (ADR-003).
NO se hace `DROP COLUMN` ni `DROP TABLE` para revertir. Si la migración
introdujo una columna que el código nuevo usa:

1. Hacer rollback del **código** (§6.1).
2. La columna nueva queda en BD, sin uso → benigno.
3. Si la columna es estrictamente incompatible con el código viejo
   (cambió tipo de una columna existente):
   - Restaurar desde el backup pre-migrate:
   ```bash
   gunzip -c /backups/caf-pre-migrate-YYYYMMDD-HHMMSS.sql.gz \
     | docker exec -i caf_postgres psql -U caf -d admin_financiera
   ```
   - **Pérdida de datos** entre el backup y ahora. Antes de hacer esto,
     evaluar si vale la pena vs. corregir hacia adelante.

### 6.3 Triggers de rollback (definidos por adelantado)

| Disparador | Acción |
|---|---|
| `/health` o `/health/db` 5xx > 2 min | Rollback de código inmediato |
| Errores 500 > 10% del tráfico durante 5 min | Rollback de código |
| Saga de onboarding falla en pasos previamente OK | Rollback de código |
| Audit log deja de escribir > 5 min | Rollback de código + investigación |
| Filas en `audit_log` con `event_type='trigger_bypass_attempted'` | Rollback + incidente Sev1 |

---

## 7. Deploy de workers (`monthly_closing`, etc.)

Los workers usan la **misma imagen** que `admin_financiera`. Al hacer
`docker compose build admin_financiera`, los workers heredan los cambios
en su próximo run (cron los lanza con `--rm`, así que siempre toman la
imagen vigente).

Para forzar un build dedicado:
```bash
docker compose build monthly_closing invoice_retry overdue_notifier
```

Para correr ad-hoc:
```bash
docker compose --profile jobs run --rm monthly_closing
docker compose --profile jobs run --rm invoice_retry
docker compose --profile jobs run --rm overdue_notifier
```

---

## 8. Cambio de PAC

Cambio de provider (Facturama → Factible, por ejemplo):

1. Implementar adaptador en `app/core/clients/pac_client.py` para el nuevo
   provider (o validar que el stub existente está completo).
2. Probar en `ENV=staging` con un timbrado de prueba.
3. Actualizar `.env` en prod:
   ```env
   PAC_PROVIDER=factible
   PAC_BASE_URL=https://...
   PAC_API_KEY=...
   PAC_API_SECRET=...
   ```
4. Deploy (§3).
5. Monitorear cola de timbrado por 24h.

Las facturas ya timbradas con el PAC anterior siguen siendo válidas (el SAT
las acepta; el PAC es solo el certificador, no el emisor).

---

## 9. Checklist consolidado de pre-deploy

Imprimir y marcar antes de cada deploy:

- [ ] `git status` limpio en VPS (sin cambios sin commit)
- [ ] `git log --oneline HEAD..origin/main` revisado (sé qué se aplica)
- [ ] Backup express ejecutado (`pg_dump | gzip`)
- [ ] `.env` no ha cambiado, o cambios documentados y aplicados
- [ ] Si hay migración: aprobada por revisor + backup pre-migrate hecho
- [ ] Si hay migración: comando PowerShell preparado (con `Get-Content | ssh`)
- [ ] Ventana de mantenimiento anunciada (sólo si requiere downtime)
- [ ] Plan de rollback claro y comunicado
- [ ] Operador disponible siguientes 30 min para vigilar logs


---

## 10. Repo del VPS = fuente de verdad + push a GitHub (alta 2026-06-14)

**Regla:** el repo desplegado en `/opt/inovaweb-admin-financiera` (lo que corre) es la **fuente de
verdad**. Las copias en OneDrive o en mounts de sesiones de agente son checkouts viejos: **no desplegar
ni documentar desde ellas** (riesgo de drift).

### 10.1 Push a GitHub (resuelto)
El remoto venía HTTPS sin credenciales (pendiente histórico). Ya está cableado vía SSH:
```bash
# remoto -> alias SSH dedicado (una sola vez, ya hecho)
git -C /opt/inovaweb-admin-financiera remote get-url origin
# git@github-caf:InovawebSoluciones/inovaweb-admin-financiera.git
# alias en ~/.ssh/config:  Host github-caf -> IdentityFile /root/.ssh/id_ed25519
git -C /opt/inovaweb-admin-financiera push origin main   # funciona directo
```
La llave `/root/.ssh/id_ed25519` es la personal (cuenta InovawebSoluciones, acceso a todos los repos).
Los deploy-keys por-repo del VPS NO sirven para el CAF (no había uno).

### 10.2 Reconciliar drift VPS↔GitHub (si vuelve a divergir)
```bash
cd /opt/inovaweb-admin-financiera
git tag -f backup/pre-reconcile HEAD        # red de seguridad
git status -s                               # commitear lo del VPS primero (es la verdad)
git fetch origin
git rev-list --left-right --count origin/main...HEAD   # left=GitHub-only  right=VPS-only
git merge -X ours --no-edit origin/main     # el VPS gana en conflictos; conserva commits de docs
# resolver add/add a favor del VPS: git checkout --ours <f> && git add <f>; git commit --no-edit
git push origin main
git rev-list --left-right --count origin/main...HEAD   # debe ser  0   0
```

### 10.3 Migración del saldo prepago nativo
La capa saldo-B usa `migrations/030_prepaid_ledger.sql` (`prepaid_ledger` + `v_client_balance`). Aplicar
con la regla §4 (PowerShell `Get-Content | ssh`, nunca `<`). Sembrar `plans`/`services` de cada app es
**additive** (ver RUNBOOK §10.3). Backend que toca código: `docker compose up -d --build admin_financiera`.

---

## 11. Multi-tenancy + SaaS Billing Engine (sesión 2026-06-16)

Sesión que convirtió el CAF en **motor de medición y cobro multi-organización (multi-tenant)**
con tarifa propia del SaaS, proveedores de correo por org/cliente, enlace org→cliente para el
meta-cobro, distribuidores con cupones de descuento, cron de cierre mensual del SaaS y nuevas
variables de entorno (Hub admin + AES real + TTL de sesión).

### 11.1 Migraciones nuevas (031 → 036) — orden estricto

Aplicar **en este orden** tras `030`. Todas son **additivas, idempotentes y transaccionales**
(envueltas en `BEGIN; … COMMIT;`). Aplicación por **psql directo, NO alembic**.

| # | Archivo | Qué hace |
|---|---------|----------|
| 031 | `031_organizations_tenancy.sql` | F0 de tenancy. Crea tabla `organizations` (id, slug único, name, status active/suspended/cancelled) y siembra **org #1 = Inovaweb**. Agrega columna `organization_id BIGINT NOT NULL DEFAULT 1` + índice + FK a `organizations(id)` a 13 tablas de primer nivel (`clients, users, services, plans, products, promotions, api_keys, subscriptions, invoices, payments, adjustments, price_catalog, prepaid_ledger`). El `DEFAULT 1` evita romper el código vivo que aún no conoce la columna; se retira en F1 cuando el código setee `organization_id` del contexto. NO toca lógica de cobro. |
| 032 | `032_catalog_unique_por_org.sql` | Cataloga multi-tenant: cambia el UNIQUE de `code` de **global a por organización** en `services`, `plans`, `products`, `promotions` (`DROP CONSTRAINT *_code_key` → `ADD CONSTRAINT *_org_code_key UNIQUE (organization_id, code)`). Permite que cada org reutilice sus propios `code`. Seguro: ninguna FK referencia por `code` (todas por id); los codes actuales viven en org 1 → siguen únicos dentro de ella. |
| 033 | `033_email_providers.sql` | Crea tabla canónica `email_providers`: config de envío de email por org (`client_id` NULL) o por cliente específico (`client_id` con valor). Soporta `microsoft`, `gmail`, `smtp`. El secreto (api_key/app_password/refresh_token/client_secret) viaja SIEMPRE cifrado en `secret_encrypted` vía `app.core.crypto.encrypt_secret`, NUNCA en claro. Solo CREATE TABLE + 2 índices (org, client); sin seeds. **Requiere `AES_KEY` real** (ver §11.2). |
| 034 | `034_seed_saas_tariff.sql` | Seed (idempotente, `ON CONFLICT … DO UPDATE`) de la **tarifa del propio SaaS del CAF** en la org plataforma (organization_id=1). Modelo híbrido: servicio `saas_transaccion` = $0.99 (99¢) por transacción facturable + plan `caf_saas` = $99/mes (9900¢, app_slug `caf`). Ajustar montos aquí (centavos BIGINT) si cambia el precio. |
| 035 | `035_org_platform_client.sql` | Agrega `organizations.platform_client_id` (FK nullable a `clients(id)`) + índice. Liga cada organización con su fila "cliente" dentro de la org plataforma (org 1): vehículo del **meta-cobro** del SaaS (la plataforma factura a cada org como un cliente más dentro de la org 1). Nullable a propósito: orgs sin cliente plataforma quedan en NULL. Solo ADD COLUMN IF NOT EXISTS + índice. |
| 036 | `036_distributors.sql` | Crea tabla `distributors` (id, organization_id DEFAULT 1 + FK, name, external_ref futuro, is_active) + índice; y agrega `promotions.distributor_id` (FK a `distributors(id)`) + índice. Cada código de promoción se asocia a un distribuidor; el código lleva descuento en % (`promotions.discount_pct`) que se aplica al contratar (self-service). Por ahora del distribuidor solo se da de alta el nombre. |
| 037 | `037_services_app_slug.sql` | Agrega columna `app_slug TEXT` a tabla `services`. Backfill automático en la misma migración: asigna `app_slug` a los servicios de LiaForge, Swigg y CAF según su `code`. Permite identificar a qué producto pertenece cada servicio cobrable (usado en la vista de reportes de consumo, filtro "App"). |

### Migración 037 — app_slug en services (2026-06-17)

```powershell
Get-Content "037_services_app_slug.sql" | ssh root@89.116.25.222 "docker exec -i caf_postgres psql -U caf -d admin_financiera"
```

Backfill automático en la migración: services de LiaForge, Swigg y CAF quedan etiquetados.

**Comando de aplicación** (psql directo dentro del contenedor, parar al primer error):
```bash
docker compose exec -T postgres psql -U caf -d admin_financiera -v ON_ERROR_STOP=1 < 0NN_*.sql
```

**Regla crítica desde PowerShell (Windows del operador)** — usar `Get-Content | ssh`, **NUNCA el
operador `<`** (ver §4.1):
```powershell
# backup ANTES de migrar
ssh root@89.116.25.222 "docker compose -f /opt/inovaweb-admin-financiera/docker-compose.yml exec -T postgres pg_dump -U caf -d admin_financiera | gzip > /backups/caf-pre-031-036-$(date +%Y%m%d-%H%M%S).sql.gz"

# aplicar 031 -> 036 en orden
foreach ($f in @(
  "031_organizations_tenancy.sql","032_catalog_unique_por_org.sql","033_email_providers.sql",
  "034_seed_saas_tariff.sql","035_org_platform_client.sql","036_distributors.sql"
)) {
  Get-Content $f `
    | ssh root@89.116.25.222 "docker compose -f /opt/inovaweb-admin-financiera/docker-compose.yml exec -T postgres psql -U caf -d admin_financiera -v ON_ERROR_STOP=1"
  Write-Host "$f OK"
}
```
(El `docker compose exec -T postgres` lee el .sql de stdin; el `-f .../docker-compose.yml` se necesita
porque el `ssh` no entra al directorio del proyecto.)

### 11.2 Variables de entorno NUEVAS en el `.env` del VPS (NO en git)

Agregar al `.env` de `/opt/inovaweb-admin-financiera` (los secretos viven SOLO en el VPS, nunca en
git ni en CLAUDE.md):

| Variable | Valor / cómo obtener | Para qué |
|---|---|---|
| `HUB_ADMIN_KEY` | Llave admin del Hub con scope `admin:gateways` | Que el CAF **configure pasarelas de pago** en el Hub (distinta de `HUB_API_KEY`, que es payments:write) |
| `HUB_COMPANY_ID` | `b5237689-...` (tenant del CAF dentro del Hub) | Identifica el tenant/empresa del CAF en el Hub al administrar pasarelas |
| `AES_KEY` | **base64 de 32 bytes REAL** (no placeholder). Generar con `openssl rand -base64 32` | Cifrado AES de secretos; el **feature de email (`email_providers`, mig 033) la requiere** para `encrypt_secret`. Un placeholder rompe encrypt/decrypt |
| `JWT_ACCESS_TTL_MIN` | `720` | TTL del access token = **sesión de 12 horas** |

Generar `AES_KEY`:
```bash
openssl rand -base64 32
```

Tras cambiar el `.env`, **recrear el contenedor** (no basta restart) para que tome las nuevas variables:
```bash
docker compose up -d admin_financiera
```

### 11.3 Endpoints de reportes de consumo (sesión 2026-06-17)

Dos endpoints nuevos bajo `/admin/reports/consumption`:

| Endpoint | Descripción | Requiere rebuild |
|---|---|---|
| `GET /admin/reports/consumption` | Página HTML de reportes (Jinja2 + Chart.js). Baked en la imagen Docker (`COPY app ./app`). | **Sí** — cambios en el template requieren `docker compose up -d --build admin_financiera` |
| `GET /admin/reports/consumption/data` | JSON con métricas de consumo. Lógica en Python puro, no en template. | **No** — basta con SCP del archivo Python al VPS + `docker compose restart admin_financiera` |

**Cuando solo cambia la lógica del endpoint `/data`** (sin tocar el template):
```bash
# 1. transferir solo el router/servicio modificado
scp "C:\path\local\app\routers\reports_router.py" root@89.116.25.222:/opt/inovaweb-admin-financiera/app/routers/

# 2. reiniciar (sin rebuild)
ssh root@89.116.25.222 "docker compose restart admin_financiera"
```

**Cuando cambia el template** (`admin/reports/consumption.html`):
```bash
# rebuild completo
ssh root@89.116.25.222 "cd /opt/inovaweb-admin-financiera && git pull && docker compose up -d --build admin_financiera"
```

### 11.4 Cron nuevo — cierre mensual del SaaS

Agregar al crontab del VPS (junto a los workers del RUNBOOK §3.4) el script de cierre mensual del SaaS:
```cron
0 6 1 * *  /opt/inovaweb-admin-financiera/scripts/run_saas_monthly_billing.sh
```
Corre **a las 06:00 del día 1 de cada mes**: cierre/facturación mensual del SaaS (servicio
`saas_transaccion` + plan `caf_saas` sembrados en mig 034).

### 11.5 Deploy de backend (recordatorio)

Sin cambios respecto a §3: el deploy de backend sigue siendo
```bash
docker compose up -d --build admin_financiera
```
Y todo deploy = **scp/edit al VPS Y commit+push** a GitHub (remoto alias `github-caf`, llave
`/root/.ssh/id_ed25519`; ver §10.1). El repo del VPS es la fuente de verdad.

### 11.6 Checklist post-deploy (sesión 2026-06-16 + 2026-06-17)

Además del checklist §3.2, verificar:

- [ ] `/health` y `/health/db` 200 (`curl -fsS http://localhost:8006/health`)
- [ ] Migraciones **031–036 aplicadas**: tablas `organizations`, `email_providers`, `distributors` existen; `organization_id` presente en las 13 tablas; constraints `*_org_code_key` activos; `organizations.platform_client_id` y `promotions.distributor_id` existen
      ```bash
      docker compose exec -T postgres psql -U caf -d admin_financiera -c "\dt" | grep -E "organizations|email_providers|distributors"
      docker compose exec -T postgres psql -U caf -d admin_financiera -c "SELECT code FROM services WHERE code='saas_transaccion'; SELECT code FROM plans WHERE code='caf_saas';"
      ```
- [ ] `/admin/payment-gateways` carga (configuración de pasarelas vía Hub; requiere `HUB_ADMIN_KEY` + `HUB_COMPANY_ID`)
- [ ] `/api/v2/orgs` responde con **JWT de plataforma** (capa multi-tenant viva)
- [ ] **Crypto round-trip OK** (`AES_KEY` real): encrypt → decrypt dentro del contenedor devuelve el texto original
      ```bash
      docker compose exec -T admin_financiera python -c "from app.core.crypto import encrypt_secret, decrypt_secret; assert decrypt_secret(encrypt_secret('ok')) == 'ok'; print('crypto round-trip OK')"
      ```
- [ ] `JWT_ACCESS_TTL_MIN=720` tomado (sesión de 12h; el contenedor fue **recreado**, no solo reiniciado)
- [ ] Cron `run_saas_monthly_billing.sh` presente en `crontab -l` (`0 6 1 * *`)
- [ ] Migración **037 aplicada**: columna `app_slug` existe en `services` con backfill correcto
      ```bash
      docker compose exec -T postgres psql -U caf -d admin_financiera -c "SELECT code, app_slug FROM services WHERE app_slug IS NOT NULL LIMIT 10;"
      ```
- [ ] `GET /admin/reports/consumption` retorna 200 tras login (requiere rebuild si el template cambió)
      ```bash
      # verificar desde el VPS con cookie de sesión válida
      curl -fsS -b "session=<token>" https://admin.inovaweb.com.mx/admin/reports/consumption -o /dev/null -w "%{http_code}"
      ```
- [ ] `GET /admin/reports/consumption/data` retorna JSON con `metrics.tx_count >= 0`
      ```bash
      curl -fsS -b "session=<token>" \
        'https://admin.inovaweb.com.mx/admin/reports/consumption/data?date_from=YYYY-MM-01&date_to=YYYY-MM-DD' \
        | python3 -c "import sys,json; d=json.load(sys.stdin); assert d['metrics']['tx_count'] >= 0; print('OK tx_count=', d['metrics']['tx_count'])"
      ```

