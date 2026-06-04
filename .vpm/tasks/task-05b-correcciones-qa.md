# TASK-05b — Correcciones de QA sobre TASK-05

**Rol que ejecuta:** Ejecutor (Claude Code).
**Origen:** QA rechazó TASK-05. Corregir SOLO estos puntos. No cambiar nada más.
**Repo (bash VM):** `/sessions/stoic-festive-cray/mnt/inovaweb-admin-financiera/`

---

## Correcciones obligatorias (bloqueantes)

### FIX-1 — Bytes NUL en `app/core/clients/messages_client.py` (ALTA, bloqueante)
El archivo termina con ~14 bytes NUL (`\x00`) tras `await self.c.close()`. Esto
rompe `compile()`/`compileall -f` en un clone limpio o build Docker (hoy queda
enmascarado por un `.pyc` viejo en `__pycache__`).
- Quitar todos los bytes NUL finales; el resto del archivo es UTF-8 válido y se
  conserva igual.
- Invalidar `.pyc` viejos: borrar `app/core/clients/__pycache__/` antes de verificar.
- Verificar: `python -c "compile(open('app/core/clients/messages_client.py','rb').read(),'m','exec')"` sin error, y `python -m compileall -f app` rc=0.

### FIX-2 — El audit `onboard_failed` no persiste (ALTA, correctitud)
En `app/services/onboarding.py`, en las ramas de fallo se hace `await db.rollback()`
y *luego* `await write_event(...)` (INSERT sin commit). Al propagar el error, el
dependency `get_db` (`app/core/database.py`) hace rollback y descarta ese INSERT.
Resultado: la falla nunca queda en `audit_log`, violando la auditoría obligatoria.
- Persistir el evento de fallo en una transacción independiente que SÍ se confirme
  (p.ej. abrir una sesión/engine aparte solo para el audit de fallo y hacer commit
  explícito; o `db.commit()` tras `write_event` en una sesión limpia post-rollback).
- No cambiar el happy path (ese commit lo hace `get_db`).
- Mantener que el password temporal NUNCA se escribe en el audit (ya es así).

### FIX-3 — Llamador roto en `app/services/billing.py:128` (MEDIA)
`MedidorClient.get_usage` ahora es keyword-only `(wallet_id, *, from_ts, to_ts)`,
pero billing sigue llamando posicional. Actualizar a:
`medidor.get_usage(sub["medidor_account_id"], from_ts=period_start.isoformat(), to_ts=period_end.isoformat())`.
Revisar que no queden otros llamadores con la firma vieja (`grep -rn "get_usage(" app/`).

### FIX-4 — Test del saga real (recomendado, inclúyelo)
`tests/test_onboarding.py` solo prueba `_compensate` aislado. Agregar al menos un
test de `onboard_client` para el **path de fallo tras crear la wallet**: que dispare
compensación (`delete_wallet` llamado), rollback local, y que el audit `onboard_failed`
quede persistido (validando FIX-2). Mockear los clientes de cores y la DB.

---

## Reglas
- No tocar `_base.py`, esquema SQL, ni config.
- No hacer commit ni push.
- Centavos BIGINT, idempotencia determinística: intactos.

## Verificación final (ejecútala de verdad y reporta salida)
1. `rm -rf app/**/__pycache__` y luego `python -m compileall -f app tests` → rc=0.
2. `python -c "compile(open('app/core/clients/messages_client.py','rb').read(),'m','exec')"` sin error.
3. `python -c "import app.main"` sin ImportError.
4. `grep -rn "get_usage(" app/` → ningún llamador con firma posicional vieja.
5. `pytest -q --basetemp=/tmp/pt` → todo verde, incluido el nuevo test del saga.

Reporta: archivos tocados, salida de cada verificación, y confirmación de que FIX-2
realmente persiste el audit (mostrando el test que lo prueba).
