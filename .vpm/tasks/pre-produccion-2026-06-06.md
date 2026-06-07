# PRE-PRODUCCIÓN — Pasos para llevar el CAF a prod (2026-06-06)

**Repo CAF:** `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera\`
**Repo Scraping:** `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\scraping_comercial\scraping-universidades\`
**VPS:** 89.116.25.222 — Docker Compose, servicio `admin_financiera`, puerto 8006.

Ejecuta en orden. Las tareas de código las haces tú (Claude Code). Las que dicen "USUARIO EN VPS" las hace Conrado manualmente.

---

## PASO 1 — Corregir D1: mismatch de tipo `caf_client_id` (BIGINT vs UUID)

**Problema:** `clients.id` en CAF es `BIGINT`. Las columnas `companies.caf_client_id` y `companies.medidor_wallet_id` en Scraping son `UUID`. El endpoint `POST /companies/{id}/link-caf` recibe un BIGINT y lo intenta guardar en UUID → da 400.

**Qué hacer:**

1. Leer el schema real de Scraping:
   - `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\scraping_comercial\scraping-universidades\`
   - Buscar el modelo `Company` y el tipo real de `caf_client_id` y `medidor_wallet_id`.

2. Si `caf_client_id` es UUID en Scraping:
   - Cambiar el tipo en el endpoint `POST /companies/{id}/link-caf` (en Scraping) para que acepte `caf_client_id: int` (no UUID).
   - Actualizar la columna si es necesario (migración Alembic o SQL directo en Scraping).

3. Si `medidor_wallet_id` es UUID pero el Medidor devuelve un string UUID → no hay problema, es correcto.

4. Verificar con `py_compile` los archivos tocados en Scraping.

---

## PASO 2 — Verificar ruta `suspend_wallet` del Medidor (C1 parcial)

**Problema:** `medidor_client.suspend_wallet` usa `POST /admin/v1/wallets/{id}/suspend`. Confirmar si esa ruta existe en el Medidor real.

**Qué hacer:**

1. Leer `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera\docs\01-admin-financiera-integracion-cores.md` y `docs\ARQUITECTURA-GLOBAL.md` — buscar la ruta real de suspend en el Medidor.

2. Si la ruta correcta es diferente a `/admin/v1/wallets/{id}/suspend`:
   - Corregir en `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera\app\core\clients\medidor_client.py`
   - Actualizar el docstring del método `suspend_wallet`.

3. `py_compile` del archivo.

---

## PASO 3 — Verificar endpoint `GET /v1/usage` del Centro de Mensajes

**Problema:** `billing.py` llama a `messages_client.get_usage(external_user_id, from_ts, to_ts)`. Confirmar si ese endpoint existe en el Centro de Mensajes o si el contrato real es diferente.

**Qué hacer:**

1. Leer `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera\docs\01-admin-financiera-integracion-cores.md` — buscar el contrato real del Centro de Mensajes para consulta de uso/consumo.

2. Si el endpoint no existe o tiene otra ruta/parámetros:
   - Corregir `get_usage` en `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera\app\core\clients\messages_client.py`.
   - Si el endpoint definitivamente no existe: hacer que `get_usage` retorne `{"total_messages": 0, "total_cents": 0}` con un warning en log (degradación graceful — no rompe el billing).

3. `py_compile` del archivo.

---

## PASO 4 — Actualizar `.env.example` con las variables nuevas

Agregar al archivo `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera\.env.example` las variables que faltan:

```
# Integración Scraping (requerida; app no arranca sin esta)
SCRAPING_BASE_URL=https://scraping.inovaweb.com.mx
SCRAPING_ADMIN_KEY=

# Seguridad webhooks (requerida en prod; debe ser ≠ HUB_API_KEY)
HUB_WEBHOOK_SECRET=

# Límite de recarga (opcional; default $500,000 MXN)
MAX_RECARGA_CENTS=50000000
```

---

## PASO 5 — Preparar commits de ambos repos

Genera los comandos de commit listos para copiar. NO ejecutes git — solo escribe los comandos.

### CAF
```
cd "C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera"
git add .
git commit -m "feat(grupo3): onboarding+scraping+activación, hardening H1-H5, frontend Jinja2+HTMX, billing consumo IA+emails

- TAREA A: scraping_client.py, 005_activation_tokens.sql, paso 2b link-caf, paso 5b token activación SHA-256
- TAREA B: 006_idempotencia.sql, retry backoff medidor.credit, fail-closed prod, tope recarga, H3 filtro client_id
- TAREA C: templates admin/ y portal/, endpoints HTML, recharge form
- TAREA D: billing.py conceptos IA+mensajes, get_usage_summary, get_usage mensajes

Fix: D1 caf_client_id type, suspend_wallet ruta, messages get_usage degradación graceful
Docs: .env.example actualizado

Tests: test_onboarding.py (4 tests), test_hardening.py (11 tests), test_billing.py nuevos
Verificado: py_compile OK. pytest pendiente en Docker/VPS.

Co-authored-by: Claude Code <claude@anthropic.com>"
git push origin main
```

### Scraping
```
cd "C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\scraping_comercial\scraping-universidades"
git add .
git commit -m "feat(caf-integration): endpoint POST /companies/{id}/link-caf

Nuevo endpoint admin para ligar una Company con el CAF:
persiste caf_client_id y medidor_wallet_id, idempotente.
Auth: Bearer SCRAPING_ADMIN_KEY.

Co-authored-by: Claude Code <claude@anthropic.com>"
git push origin main
```

Escribe estos comandos en un archivo `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera\.vpm\tasks\commits-listos.md` para que Conrado los copie.

---

## PASO 6 — Generar script de deploy para el VPS

Crea el archivo `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera\.vpm\tasks\deploy-vps.sh` con el siguiente contenido exacto (Conrado lo copia y corre en el VPS):

```bash
#!/bin/bash
set -e
echo "=== DEPLOY CAF + SCRAPING ==="

# 1. Pull ambos repos
cd /opt/inovaweb-admin-financiera
git pull

# 2. Aplicar migraciones nuevas (idempotentes, IF NOT EXISTS)
docker compose exec -T postgres psql -U caf -d admin_financiera \
  -f /docker-entrypoint-initdb.d/005_activation_tokens.sql
docker compose exec -T postgres psql -U caf -d admin_financiera \
  -f /docker-entrypoint-initdb.d/006_idempotencia.sql

# 3. Rebuild y levantar CAF
docker compose up -d --build

# 4. Verificar que levantó
sleep 5
curl -sf http://localhost:8006/health && echo "CAF OK" || echo "CAF FALLO"

# 5. Pytest en Docker
docker compose run --rm admin_financiera sh -c \
  "pip install pytest pytest-asyncio httpx --quiet && python -m pytest tests/ -v --tb=short"
```

---

## PASO 7 — Nota para Conrado (USUARIO EN VPS — NO LO HACE CLAUDE CODE)

Escribe esto al final del archivo `commits-listos.md`:

```
ANTES DE CORRER EL DEPLOY:

1. Editar /opt/inovaweb-admin-financiera/.env y agregar:
   SCRAPING_ADMIN_KEY=<pedir a Conrado>
   HUB_WEBHOOK_SECRET=<generar: openssl rand -hex 32>

2. Sembrar plantilla en Centro de Mensajes:
   slug: caf-activacion-correo
   variables: {{nombre}}, {{token_url}}, {{expiracion_horas}}

3. Correr el script de deploy:
   bash /opt/inovaweb-admin-financiera/.vpm/tasks/deploy-vps.sh
```

---

## Verificación final

Al terminar todos los pasos, reporta:
- Archivos modificados en cada paso
- `py_compile` OK en todos
- Contenido de `commits-listos.md` y `deploy-vps.sh` generados
- Cualquier hallazgo nuevo
