# TASK-20 — Actualizar contrato del Medidor (docs) + ADR del modelo prepago

**Rol:** Documentador. **Repo:** CAF (`/sessions/stoic-festive-cray/mnt/inovaweb-admin-financiera/`).
**Solo toca `docs/`** (NO app/, NO tests). NO commit ni push.

## Contexto (confirmado leyendo el código real del Medidor)
El Medidor (core Nivel 1) ya implementa prepago completo. El contrato del CAF estaba desactualizado. API real:
- `POST /v1/wallets` (crear wallet; identidad (tenant_id, external_user_id) UNIQUE; scope ADMIN).
- `GET /v1/wallets/{id}/balance` (balance_cents, holds_total, disponible = balance - holds).
- `POST /v1/wallets/{id}/credit` (recarga; scope ADMIN; idempotente por request_id).
- `POST /v1/operations/authorize` (pre-check: crea HOLD y VALIDA saldo; rechaza si insuficiente; scope CLIENT).
- `POST /v1/operations/finish` (captura hold → DEBIT, descuenta saldo; idempotente por request_id; scope CLIENT).
- `POST /v1/operations/release`, `POST /v1/operations/quote`.
- `POST /v1/events/track` (telemetría, NO cobra), `GET /v1/usage`, `POST /v1/events/refund`.
- Admin: suspend/unsuspend wallet, refund_audit.
- Ledger append-only `wallet_transactions`; balance materializado; locking optimista; idempotencia UNIQUE(wallet_id, request_id).
- Auth: API key `X-Api-Key`/Bearer; scopes CLIENT (authorize/finish/track) y ADMIN (credit/create/suspend).
- Tenant del piloto = `inovaweb`, proyecto `scraping`. Identidad de wallet del cliente = (tenant inovaweb, external_user_id = Company.id de Scraping).

## Tareas
1. **`docs/01-admin-financiera-integracion-cores.md` §3 (Integración con Medidor IA):** reemplaza la descripción desactualizada por el API real de arriba. Documenta el flujo prepago: CAF (key ADMIN) crea wallet + recarga; el consumidor (Scraping, key CLIENT) hace authorize→finish; el bloqueo lo impone authorize al rechazar por saldo insuficiente. Conserva el estilo/formato del documento.
2. **`docs/ADR.md`:** agrega un ADR (sigue numeración y formato existentes) que registre: "Decisión: el modelo del piloto es PREPAGO con el Medidor core como wallet autoritativa (authorize/finish/credit). Mapeo de identidad: CAF clients.id ↔ Company.caf_client_id ↔ Company.id (=company_id) ↔ wallet external_user_id bajo tenant inovaweb. Scraping no tenía integración con el Medidor; se wirea en TASK-21." Incluye Contexto/Decisión/Alternativas/Consecuencias.
3. Si hace falta, una línea en `CHANGELOG.md` (no committear).

## Entrega
Reporte conciso: documentos tocados + 1 línea por cada uno. NADA de código. NO commit.
