# TASK-08 — CRUD de clientes + API JSON /api/v2

**Rol:** Ejecutor. **Repo:** CAF. **Owns:** app/routers/api_router.py, app/routers/admin_router.py, y SUS tests. NO toques prepago.py, webhooks_router.py, portal_router.py, config.py, database/ (los edita #15b en paralelo). NO commit/push.
Buena parte ya está scaffoldeada: verifica y completa, no reconstruyas.

## Alcance
1. **CRUD de clientes** (operador interno, vía admin_router o api_router):
   - Alta (ya existe vía onboarding/prepago — reutiliza, no dupliques la Saga), edición (PATCH datos comerciales), baja, **suspensión** (`POST /admin/clients/{id}/suspend` con motivo → clients.status='suspended', suspended_at, suspended_reason) y **reactivación**. Todo con auditoría (write_event) y rol mínimo.
2. **API JSON /api/v2** (autenticada JWT o API key admin):
   - `POST /api/v2/clients` (alta programática; mismo flujo que UI).
   - `GET /api/v2/clients/{id}/balance` (saldo consolidado: leer del Medidor vía medidor.get_balance con clients.medidor_account_id).
   - `GET /api/v2/reports/income` (agregados de ingreso; leer Finanzas get_totals / o payments locales).
   - `POST /api/v2/billing/run-closing` (trigger manual; para el piloto prepago puede ser stub que responde 501/no-op documentado, ya que no hay cierre mensual).
3. **Multi-rol estricto**: cada endpoint declara su rol mínimo (super_admin/finanzas/lectura). Lectura no escribe.

## Reglas
- Centavos BIGINT. Auditoría en toda escritura (actor, ip, old/new). Append-only respetado.
- No reimplementar el credit/webhook (es de #15/#15b). Aquí solo lectura de balance + CRUD + reportes.
- Verificar con Read (mount OneDrive trunca).

## Verificación (ejecútala, reporta salida)
1. `python -m compileall -f app` rc=0; `python -c "import app.main"` OK.
2. `pytest -q --basetemp=/tmp/pt08` verde; agrega tests: suspend/reactivate cambia status + audita; GET balance llama medidor.get_balance (mock); authz (rol lectura no puede suspender → 403).
3. Reporta archivos tocados + salida de verificaciones + endpoints expuestos.
