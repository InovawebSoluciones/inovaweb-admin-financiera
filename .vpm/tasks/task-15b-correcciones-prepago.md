# TASK-15b — Correcciones de QA/Seguridad sobre TASK-15 (prepago CAF)

**Rol:** Ejecutor. **Repo:** CAF. Corrige SOLO estos puntos. **Owns:** app/services/prepago.py, app/routers/webhooks_router.py, app/routers/portal_router.py, app/core/config.py, database/ (nueva migración), tests/. NO toques api_router.py ni admin_router.py (los edita #8 en paralelo). NO commit/push.

## BLOQUEANTE
### FIX-1 — Idempotencia a nivel BD (P0/A1)
- Nueva migración `database/004_payments_idempotency.sql`: índice ÚNICO PARCIAL `CREATE UNIQUE INDEX uq_payments_hub ON payments(hub_payment_id) WHERE hub_payment_id IS NOT NULL;` (es un índice, no viola append-only).
- En `prepago.process_paid_event`: que la garantía de no-duplicado viva en la BD, no en el SELECT. Implementa: intentar INSERT del payment como "reclamo" idempotente (con `ON CONFLICT (hub_payment_id) DO NOTHING` o capturando `IntegrityError`); si ya existía → tratar como replay (`duplicate_ignored`, 200) y NO reenviar correo/asiento/credit. El credit del Medidor ya es idempotente por request_id; el objetivo aquí es no duplicar fila en payments, ni correo, ni asiento en Finanzas.
- Maneja el caso de fallo del credit tras reclamar el payment sin dejar estado inconsistente (documenta la decisión; si reclamas antes del credit y el credit falla, el reintento del Hub debe poder completar el credit — usa el request_id determinista para que sea seguro reintentar).
- Test: dos webhooks concurrentes/repetidos con el mismo hub_transaction_id → `medidor.credit` y `messages.send_email` se llaman UNA sola vez.

### FIX-2 — Validar purpose/amount contra el intento local (M3)
- `initiate_charge` ya deja `recharge.initiated` en audit_log con el recharge_id. Antes de acreditar, correlacionar el evento del webhook con ese registro por recharge_id y validar que `purpose` y `amount_cents` coincidan con lo iniciado. Si no hay intento previo o no coincide → rechazar (no acreditar), audit `hub.paid.rejected`.

## ALTA/MEDIA (de seguridad)
### FIX-3 — Secreto de webhook obligatorio en prod (M1)
- Validator en `Settings`: si `ENV/app_env == production` y `HUB_WEBHOOK_SECRET` no está definido → fallar el arranque. No usar `HUB_API_KEY` como fallback en prod.
### FIX-4 — Exigir timestamp en prod (M2)
- En prod, si el webhook no trae timestamp firmado → 401 (no caer al modo sin ventana anti-replay).
### FIX-5 — No filtrar errores crudos al cliente del portal (B1)
- En portal_router, no `raise HTTPException(502, str(e))`; loguear el detalle server-side y devolver mensaje genérico ("error al procesar el pago, intenta de nuevo").
### FIX-6 — Tope superior de monto de recarga (B2)
- En `start_recharge`, validar `5000 <= amount_cents <= MAX_RECARGA` (config, p.ej. 50_000_000). El monto de compra de plan ya viene del catálogo (correcto).
### FIX-7 — Parseo robusto de amount (B3)
- En `extract_event`, envolver `int(payload["amount"])`; si falta o no es numérico → `PrepagoError` (no 500 sin controlar).

## Reglas
- Centavos BIGINT. Mantener HMAC tiempo-constante y validación-antes-de-I/O (ya correctos).
- Verificar con Read (mount OneDrive trunca). Correr compileall/pytest sobre copia /tmp si hace falta.

## Verificación (ejecútala, reporta salida)
1. `python -m compileall -f app` rc=0; `python -c "import app.main"` OK.
2. Aplicar 001+002+003+004 en Postgres limpio: rc=0; confirmar el índice único parcial existe.
3. `pytest -q --basetemp=/tmp/pt15b` verde, incluyendo: replay/concurrencia → credit+email una vez; fallo de credit → 502 + audit `hub.paid.failed`; purpose/amount no coincide → rechazado.
4. Reporta archivos tocados + salida de cada verificación.
