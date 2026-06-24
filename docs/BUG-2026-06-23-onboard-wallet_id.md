# ✅ RESUELTO — `/api/v2/apps/onboard` revienta (wallet_id None). Bloqueaba TODOS los registros nuevos.

**Reportado:** 2026-06-23 · **Resuelto:** 2026-06-23 · **Severidad:** CRÍTICA · **Estado:** ✅ RESUELTO

## Qué pasaba
El refactor `0650a83 "eliminar wallet del Medidor — balance a prepaid_ledger"` quitó el wallet del
Medidor, pero dejó `AppOnboardResponse.wallet_id` como **`str` obligatorio**. Tras el refactor ese campo
llega `None` → `ValidationError (string_type)` en `api_router.py:574 (api_app_onboard)` → el CAF respondía
**500** → LiaForge `POST /auth/register` lo traducía a **502 "error interno"** → ningún cliente nuevo podía
registrarse.

## Qué se hizo (FIX aplicado)
`app/routers/api_router.py`, línea **470**:
```python
wallet_id: str            #  ANTES (obligatorio → reventaba con None)
wallet_id: str | None = None   #  AHORA (coherente con el ledger nativo, sin wallet del Medidor)
```
- Aplicado al código fuente + desplegado a `caf_app` (`docker cp` + `docker restart`).
- **Verificado:** `AppOnboardResponse(client_id=1, wallet_id=None, plan_code="liaforge_free", granted_cents=0)`
  ya **NO** lanza (devuelve `wallet_id=None`). Antes reventaba.

## Verificación de datos
- **Sin clientes huérfanos:** CAF `clients` con tepz/miguel = 0; LiaForge `companies`/`users` = 0. Los
  rollbacks (CAF y LiaForge) limpiaron los intentos fallidos. Nada que purgar.
- **LiaForge:** su `register` (`auth.py`) ya toleraba la ausencia de `wallet_id` (`try/except`) → no requirió
  cambios. En cuanto el CAF dejó de reventar, el alta vuelve a funcionar.

## Pendientes menores (no bloquean el registro)
- `medidor-jobs` está `unhealthy` — revisar.
- ~~`activation_email_send_failed`~~ — descartado: el correo de activación SÍ llega (confirmado tras el fix; el error era del onboard mientras reventaba).
- ✅ Imagen del CAF reconstruida y `caf_app` recreado: el fix queda HORNEADO en la imagen (permanente, no se pierde al recrear el contenedor).
  lo deja permanente en el repo).
