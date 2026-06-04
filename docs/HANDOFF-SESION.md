# Handoff de sesión — CAF (inovaweb-admin-financiera)

**Fecha de cierre:** 2026-06-04
**Objetivo del sprint:** piloto **PREPAGO** de Scraping Universidades sobre el CAF.

Este documento permite que **otro chat** retome el trabajo sin contexto previo.
Léelo completo antes de tocar código. El CAF no tiene `HISTORIAL_SESIONES.md`;
la fuente de verdad de continuidad es este archivo + `CLAUDE.md` + `CHANGELOG.md`.

---

## 1. El objetivo en una frase

El cliente elige un plan → paga en Conekta sandbox (vía Hub-Pasarelas) → el CAF
**acredita saldo** en la wallet del cliente en el **Medidor** → el consumo de IA
de Scraping descuenta ese saldo en vivo → al agotarse, el Medidor bloquea el
consumo. **No hay cierre mensual ni CFDI en el piloto** (diferidos, ADR-010).

---

## 2. Decisiones firmes (no re-litigar)

- **El Medidor core (Nivel 1) es la wallet prepago autoritativa.** Ya implementa
  prepago completo: `POST /v1/operations/authorize` (pre-check + HOLD),
  `/finish` (DEBIT), `POST /v1/wallets/{id}/credit` (recarga, idempotente por
  `request_id`), `GET /v1/wallets/{id}/balance`. **No se construye débito nuevo
  en el CAF.** (ADR-009, ADR-010, ADR-011 en `docs/ADR.md` — ya escritos, no
  duplicar.)
- **Reparto por scope:** CAF usa scope `ADMIN` (crea wallet + `credit` al
  confirmarse el pago); Scraping usa scope `CLIENT` (`authorize → finish`). El
  bloqueo por saldo insuficiente lo impone `authorize` del Medidor, no el CAF.
- **Mapeo de identidad cross-core:**
  `CAF clients.id` ↔ `Company.caf_client_id` (BD Scraping) ↔ `Company.id`
  (= `company_id`) ↔ wallet `external_user_id` bajo `tenant_id="inovaweb"`.
- **Activación de cuenta del cliente:** link por correo (obligatorio) + OTP
  WhatsApp (opcional) vía Centro de Mensajes. SMS fuera de alcance.

---

## 3. Qué quedó HECHO y aceptado (en el árbol del CAF, working tree)

> ⚠️ **Nada está commiteado.** Ver §7.

- Clientes HTTP a los 4 cores reescritos al **contrato real** (`finanzas_client`,
  `messages_client`, `hub_client`, `medidor_client`). Se eliminaron métodos
  inventados (`create_account`/`issue_api_key`/`send_sms`).
- Onboarding (`app/services/onboarding.py`) reescrito a modelo **prepago**: el
  alta solo crea la wallet del cliente en el Medidor y guarda su id; compensación
  `delete_wallet` ante fallo; auditoría de fallo persistida en transacción
  propia.
- Seed de planes del piloto (`database/003_seed_scraping_plans.sql`): free
  `10000` / básico `9900` / medio `20000` / premium `40000` centavos.
- `docs/01-admin-financiera-integracion-cores.md` §3 actualizado al API real del
  Medidor.
- Integración Scraping ↔ Medidor (en **repo Scraping**, no aquí):
  `medidor_client.py` + `authorize`/`finish` en `semantic_search`, migración
  `0004` con `Company.medidor_wallet_id` y
  `search_sessions.medidor_hold_id`/`medidor_status`.
- Script `vps/04` (en **repo Medidor**) para emitir la API key scope `ADMIN` que
  el CAF necesita.

---

## 4. Qué está EN CORRECCIÓN: #15b (flujo prepago del CAF)

Archivos del flujo prepago presentes en el working tree:
`app/services/prepago.py`, `app/routers/webhooks_router.py`,
`app/routers/portal_router.py`, `app/core/config.py`,
`database/004_payments_idempotency.sql`, `tests/test_hub_webhook.py`.

**Historia:** QA **rechazó** la primera versión de TASK-15 por faltar:
(a) UNIQUE parcial en `payments.hub_payment_id`, y (b) validar `purpose`/`amount`
contra el intento local. La spec de corrección está en
`.vpm/tasks/task-15b-correcciones-prepago.md`.

**Estado real observado en el árbol (2026-06-04):** los archivos **ya contienen**
las correcciones FIX-1 … FIX-7:
- FIX-1 — `INSERT ... ON CONFLICT (hub_payment_id) DO NOTHING` + migración `004`.
- FIX-2 — correlación por `recharge_id` + audit `hub.paid.rejected`.
- FIX-3/FIX-4 — `HUB_WEBHOOK_SECRET` obligatorio en prod + timestamp exigido.
- FIX-5 — el portal no propaga errores crudos del core.
- FIX-6 — `MAX_RECARGA_CENTS` (5000 ≤ monto ≤ tope).
- FIX-7 — parseo defensivo de `amount` en `extract_event`.

**⚠️ PENDIENTE PARA EL PRÓXIMO CHAT — cerrar #15b formalmente:**
Re-correr la verificación de QA sobre **este** árbol y reportar salida:
1. `python -m compileall -f app` (rc=0) y `python -c "import app.main"` OK.
2. Aplicar `001`+`002`+`003`+`004` en un Postgres **limpio** (rc=0) y confirmar
   que el índice `uq_payments_hub` existe.
3. `pytest -q` verde, incluyendo: replay/concurrencia → `credit`+`send_email`
   una sola vez; fallo de `credit` → audit `hub.paid.failed`; purpose/amount no
   coincide → rechazado.

> Nota de método: `git diff`/`git status` sobre el mount OneDrive es poco
> fiable (la stat-cache no detecta todos los cambios). NO confiar en `git diff`
> para juzgar qué cambió; verificar leyendo los archivos (`Read`) y corriendo los
> tests sobre una copia en `/tmp`.

---

## 5. Specs LISTOS sin ejecutar (orden sugerido)

| Tarea | Spec | Qué falta |
|---|---|---|
| **#15b** | `.vpm/tasks/task-15b-correcciones-prepago.md` | Solo **verificar** (§4). El código parece ya aplicado. |
| **#8** | `.vpm/tasks/task-08-crud-clientes-api.md` | CRUD de clientes + API JSON `/api/v2`. Toca `api_router.py`/`admin_router.py` (no los toca #15b). |
| **#16** | `.vpm/tasks/task-16-onboarding-wallet-activacion.md` | Onboarding que crea wallet + liga `Company.caf_client_id` en Scraping + dispara activación (email obligatorio + OTP WhatsApp). |

---

## 6. Backlog de hardening antes de multi-cliente (#19, #22)

- Idempotencia a nivel BD (cubierta por FIX-1 — verificar que quedó bien).
- Retry de captura si `finish` falla a mitad.
- Filtro explícito de `company_id` en queries de Scraping (hoy cubierto por RLS).
- Tope de monto de recarga (FIX-6 — verificar).
- `MEDIDOR_API_KEY` obligatoria en prod.
- No propagar errores crudos del core al cliente (FIX-5 — verificar).

---

## 7. Acciones que dependen del USUARIO (en el VPS / decisiones)

1. Correr `vps/04` (repo Medidor) para emitir la key scope `ADMIN` del Medidor y
   pegarla como `MEDIDOR_API_KEY` en el `.env` del CAF.
2. DNS + TLS de `admin.inovaweb.com.mx` y `app.inovaweb.com.mx` (bloques Caddy
   del stack n8n apuntando a `caf_app:8001`).
3. Credenciales Conekta **sandbox** + `HUB_WEBHOOK_SECRET` en el `.env`.
4. Decidir si se hace commit/push (ver §8).

---

## 8. Estado de git (3 repos, nada commiteado)

Working tree del **CAF** con cambios sin commit:
- Modificados: `CHANGELOG.md`, `README.md`, `docs/ADR.md`,
  `app/core/clients/{finanzas,hub,medidor,messages}_client.py`,
  `app/routers/{api,portal}_router.py`,
  `app/services/{billing,onboarding}.py`, `app/workers/overdue_notifier.py`,
  `tests/test_onboarding.py`.
- Sin trackear: `app/services/prepago.py`, `database/003_seed_scraping_plans.sql`,
  `database/004_payments_idempotency.sql`, `tests/test_hub_webhook.py`, `.vpm/`,
  más los docs de esta sesión (`docs/RUNBOOK.md`, `docs/DEPLOY.md`,
  `docs/HANDOFF-SESION.md` y demás cambios de documentación).

> `app/core/config.py` y `app/routers/webhooks_router.py` contienen el código
> nuevo del flujo prepago aunque `git status` sobre el mount OneDrive pueda no
> listarlos como modificados (stat-cache poco fiable, ver §4). En el VPS / en un
> clone limpio sí aparecerán. Hacer `git add -A` para no dejar nada fuera.

Repo: `https://github.com/InovawebSoluciones/inovaweb-admin-financiera`

Los repos **Medidor** y **Scraping** también tienen cambios sin commit
(integración cross-core). Se commitean por separado en sus propios repos.

---

## 9. Punto de entrada para el próximo chat

1. Leer este archivo + `CLAUDE.md` + `CHANGELOG.md` ([0.3.0]).
2. **Cerrar #15b**: correr la verificación QA del §4 sobre el árbol actual.
3. Si QA pasa → ejecutar #8 y #16 (specs en §5).
4. Confirmar con el usuario las acciones de VPS del §7 antes de probar
   end-to-end.
