# TASK-15 — Flujo prepago CAF: contratar plan → cargo Conekta → recarga wallet

**Rol:** Ejecutor (Claude Code). **Repo:** raíz del CAF (`/sessions/stoic-festive-cray/mnt/inovaweb-admin-financiera/`).
**Modelo:** PREPAGO. Contratar plan = cargo en Hub (gateway conekta, sandbox) → webhook → acreditar saldo en la wallet del Medidor.
**Contrato:** `docs/01-admin-financiera-integracion-cores.md` §4 (Hub) y §3 (Medidor). Mucho scaffolding YA existe (webhooks_router, portal_router); verifica y completa, no reconstruyas.

## Alcance
1. **Iniciar compra de plan** (CAF → Hub): `hub.charge(external_user_id, amount_cents=plan.monthly_fee_cents, description, metadata={purpose:"plan_purchase", caf_client_id, plan_code, recharge_id})`, gateway conekta. external_user_id = el de la wallet (usar `clients.hub_account_id`/`medidor_account_id` según corresponda). Persistir intento en tabla `payments` (o tabla de recargas) con estado pendiente, idempotencia por recharge_id.
2. **Webhook** `POST /webhooks/hub-payment-paid` (`app/routers/webhooks_router.py`):
   - Validar firma HMAC del Hub ANTES de procesar (rechazo 401 si inválida o timestamp fuera de ventana). Si hoy no está, impleméntalo.
   - Idempotencia por `hub_transaction_id` (UNIQUE en `payments`). Replay = 200 sin reprocesar.
   - Si `metadata.purpose` ∈ {plan_purchase, wallet_recharge}: acreditar saldo en el Medidor con `medidor.credit(wallet_id=clients.medidor_account_id, amount_cents, request_id=f"caf-recharge-{recharge_id}", reason, metadata)`. El `request_id` es determinístico (idempotencia en el Medidor).
   - Registrar entrada en Finanzas: `finanzas.post_entry(source_slug="hub", source_ref=f"caf-recharge-{recharge_id}", direction="credit", amount_cents, occurred_at, description, meta={caf_client_id, hub_transaction_id})`.
   - Marcar el pago `paid` en `payments`. Notificar al cliente vía `messages.send_email(template_id="caf-pago-confirmado", ...)`. Registrar en audit_log.
   - Si `purpose=invoice_payment`: marcar factura `paid` (fuera del piloto; deja el branch pero sin prioridad).
3. **Política de fallo**: si el `credit` al Medidor falla, encolar/retry (backoff) y dejar estado recuperable; NO perder el pago. Audit del fallo en transacción propia (mismo patrón que onboarding `_persist_failure_audit`).

## Reglas firmes
- Centavos BIGINT. Idempotencia determinística (`request_id`/`source_ref`/`hub_transaction_id`).
- Append-only en payments. Validar firma del webhook antes de cualquier I/O.
- NO tocar `docs/` (eso es TASK-20). NO commit ni push.
- Las credenciales reales (admin key Medidor, Conekta sandbox) son de runtime (#18); el código debe funcionar cuando estén; no las hardcodees.

## Verificación (ejecútala y reporta salida)
1. `python -c "import app.main"` sin ImportError (con env mínimo).
2. `python -m compileall -f app` rc=0.
3. `pytest -q --basetemp=/tmp/pt15` verde. Agrega test del webhook: firma inválida → 401; replay del mismo hub_transaction_id → no doble credit (mockea medidor.credit y verifica assert_awaited_once).
4. Reporta archivos tocados + salida de cada verificación + supuestos/TODO.
