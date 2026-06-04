# TASK-05 — Reescribir clientes HTTP + onboarding al contrato REAL de los cores

**Rol que ejecuta:** Ejecutor (Claude Code).
**Tarea en taskbar:** #5 (clientes) + parte coherente de #6 (onboarding).
**Fuente de verdad del contrato:** `docs/01-admin-financiera-integracion-cores.md`.
**Modelo de negocio:** PREPAGO (recarga de wallet en Medidor). NO facturación mensual.

---

## Contexto / problema

El scaffolding original asumió una API genérica inventada (`POST /v1/accounts`,
`/keys`) que los cores reales NO exponen. Hay que alinear los 4 clientes HTTP y
el onboarding al contrato real. Las llaves del CAF hacia los cores son de
**bootstrap (SQL)**: NO existe `create_account` ni `issue_api_key` por cliente.

Estado actual (ya reescritos, úsalos como referencia de estilo):
- `app/core/clients/medidor_client.py` — HECHO
- `app/core/clients/hub_client.py` — HECHO

Pendiente en esta tarea:
- `app/core/clients/finanzas_client.py`
- `app/core/clients/messages_client.py`
- `app/services/onboarding.py` (alinear a la nueva API de los clientes + prepago)
- Cualquier otro llamador que rompa al quitar métodos viejos.

No cambiar `app/core/clients/_base.py` (el wrapper httpx sirve tal cual).

---

## 1. `finanzas_client.py` — contrato real (§5)

Finanzas-Core resuelve el `tenant_id` desde la API key (el CAF tiene key admin
master). Para vista por cliente se filtra por `external_user_id` en `meta` JSONB.
NO hay creación de cuenta ni emisión de llaves.

Métodos requeridos:
- `get_balance(*, as_of: str, external_user_id: str | None = None) -> dict`
  → `GET /v1/ledger/balance?as_of=...`
- `get_totals(*, from_ts: str, to_ts: str) -> dict`
  → `GET /v1/ledger/totals?from_ts=...&to_ts=...`
- `list_entries(*, source: str | None, direction: str | None, limit: int = 100, offset: int = 0) -> dict`
  → `GET /v1/ledger/entries?source=&direction=&limit=&offset=`
- `post_entry(*, source_slug, source_ref, direction, amount_cents, occurred_at, description, meta: dict) -> dict`
  → `POST /v1/ledger/entries` con body:
  `{source_slug, source_ref, direction, amount_cents, currency:"MXN", occurred_at, description, meta}`

Convención de `source_ref` (§5.3) — documentar en docstring:
- Pago de factura: `caf-invoice-<invoice_id>-payment`
- Recarga acreditada: `caf-recharge-<rch_id>`
- Ajuste manual: `caf-manual-adj-<adj_id>`
- Cuota suscripción: `caf-sub-<client>-<yyyymm>`
- Reversión: `<original>-reversal`

Quitar: `create_account`, `delete_account`, `issue_api_key`, `post_charge`/`post_credit` viejos.

## 2. `messages_client.py` — contrato real (§6)

Emisor de notificaciones. Devuelve 202 con `message_id` (persistir para
correlación). NO crea cuenta ni emite llaves.

Métodos requeridos:
- `send_email(*, client_id, service_id, template_id, to: dict, variables: dict, from_: dict | None = None, meta: dict | None = None, app_id: str = "admin-financiera") -> dict`
  → `POST /v1/messages/email` con body:
  `{app_id, client_id, service_id, origin_kind:"template", template_id, from, to, variables, meta}`
  `from` default `{"email":"facturacion@inovaweb.com.mx","name":"Inovaweb"}`.
- `send_whatsapp(*, client_id, service_id, template_id, to: dict, variables: dict, meta: dict | None = None, app_id: str = "admin-financiera") -> dict`
  → análogo a email pero canal WhatsApp. **El contrato solo documenta el endpoint
  de email**; usar `POST /v1/messages/whatsapp` como supuesto y marcar con
  comentario `# TODO confirmar endpoint real de WhatsApp con el Centro de Mensajes`.

Quitar: `create_account`, `delete_account`, `issue_api_key`, `send_sms` (SMS fuera del piloto).

## 3. `onboarding.py` — alta PREPAGO + contrato real

El alta ya NO crea "cuentas" en hub/finanzas/messages (finanzas y mensajes son
multi-tenant resueltos por la llave; hub se configura por SQL). El único core que
se toca en el alta es el **Medidor**, para crear la wallet del cliente.

Reescribir `onboard_client` a estos pasos (mantener patrón Saga + compensación):
1. Validar que el plan existe y está activo.
2. INSERT en `clients` (local).
3. Crear wallet en Medidor: `medidor.create_wallet(external_user_id=f"client-{client_id}", metadata={"caf_client_id": client_id, "razon_social": legal_name})`. Guardar el `id` de la wallet en `clients.medidor_account_id`.
4. (Opcional) guardar `external_user_id` en `clients.hub_account_id/finanzas_account_id/messages_account_id` o dejarlos NULL — son el mismo `external_user_id` lógico; documentar la decisión en docstring.
5. INSERT en `subscriptions` (plan elegido, status 'active').
6. Crear `users` titular con password temporal + rol `cliente_titular`.

Compensación si algo falla tras crear la wallet: `medidor.delete_wallet(...)` via
`_safe` (best-effort, ya implementado), rollback local, audit `onboard_failed`.

Quitar todas las llamadas a `create_account` / `issue_api_key` / `delete_account`
de hub, finanzas y messages. `OnboardResult.api_keys` ya no aplica: reemplazar por
`wallet_id` (o eliminar el campo y ajustar llamadores).

NOTA de alcance: el reordenamiento del disparo (que el alta ocurra DESPUÉS del
pago confirmado) pertenece a las tareas #15/#16; en esta tarea `onboard_client`
sigue siendo una función invocable. No implementar el flujo de cobro aquí.

---

## Reglas firmes (no romper)
- Centavos enteros BIGINT. Nunca floats.
- Idempotencia: `request_id`/`source_ref` determinístico en todo POST cross-core.
- No inventar endpoints fuera del contrato salvo los marcados con `# TODO confirmar`.
- No tocar `_base.py`, ni el esquema SQL, ni la config.

## Criterios de aceptación / verificación
1. `python -c "import app.main"` (o import del paquete) corre sin ImportError.
2. `python -m compileall app` sin errores de sintaxis.
3. `grep -rn "create_account\|issue_api_key\|/v1/accounts" app/` no devuelve nada
   en finanzas/messages/hub/medidor ni en onboarding.
4. Ejecutar la suite si existe: `pytest -q` (no debe romper por estos cambios;
   si hay tests que asumían la API vieja, actualizarlos al nuevo contrato).
5. Dejar un resumen corto de los archivos tocados al terminar.

## Entorno
- Repo: raíz del proyecto (cwd). Usar `.venv` si existe.
- NO hacer commit ni push (eso es tarea #2, la decide el VPM). Solo dejar los
  cambios en el working tree.
