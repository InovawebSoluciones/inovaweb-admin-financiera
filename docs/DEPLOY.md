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

### 4.0 Migraciones del piloto prepago (003 + 004)

Al desplegar el flujo prepago por primera vez, aplicar en orden tras `001`+`002`:

- `database/003_seed_scraping_plans.sql` — seed de planes del piloto Scraping
  (free `10000` / básico `9900` / medio `20000` / premium `40000` centavos).
- `database/004_payments_idempotency.sql` — índice ÚNICO PARCIAL
  `uq_payments_hub` (idempotencia del webhook del Hub). Idempotente
  (`IF NOT EXISTS`); no viola append-only.

Verificar el índice tras aplicar:
```bash
docker exec caf_postgres psql -U caf -d admin_financiera \
  -c "\d payments" | grep uq_payments_hub
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
