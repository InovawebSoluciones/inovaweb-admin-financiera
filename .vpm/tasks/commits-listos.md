# Commits listos para copiar — Pre-producción CAF (2026-06-06)

> Generado por Claude Code. NO se ejecutó git; copia y pega tú.
> El push del CAF es HTTPS; si el remoto local no tiene credenciales, hazlo
> desde el VPS o configura el PAT antes.

---

## CAF

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

## Scraping

```
cd "C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\scraping_comercial\scraping-universidades"
git add .
git commit -m "feat(caf-integration): endpoint POST /companies/{id}/link-caf

Nuevo endpoint admin para ligar una Company con el CAF:
persiste caf_client_id y medidor_wallet_id, idempotente.
Auth: Bearer SCRAPING_ADMIN_KEY.

Fix D1: caf_client_id UUID -> BIGINT (modelo + migración alembic 0005).

Co-authored-by: Claude Code <claude@anthropic.com>"
git push origin main
```

---

## ANTES DE CORRER EL DEPLOY (USUARIO EN VPS — Conrado)

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

## OJO — el deploy-vps.sh NO cubre Scraping (hallazgo)

El script `deploy-vps.sh` (PASO 6) solo despliega el CAF y aplica las
migraciones 005/006 del CAF. El endpoint `POST /companies/{id}/link-caf` y el
fix D1 (migración alembic **0005** de Scraping) viven en el repo Scraping, que
se despliega aparte. En el VPS de Scraping, además del pull + rebuild, hay que
correr la migración alembic:

```
# En el host de Scraping (ver scraping_comercial/CLAUDE.md, repo en VPS):
cd /root/scraping-universidades
git pull
docker compose exec backend alembic upgrade head   # aplica 0005_caf_client_id_bigint
docker compose up -d --build backend
```

---

## Commit adicional — CLAUDE.md actualizado

```bash
cd "C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera"
git add CLAUDE.md
git commit -m "docs(claude): actualizar estado sesion 2026-06-07 — grupo3 completo + pre-prod"
git push origin main
```
