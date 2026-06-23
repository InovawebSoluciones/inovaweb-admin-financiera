# 🔴 BUG CRÍTICO — `/api/v2/apps/onboard` revienta (wallet_id None). Bloquea TODOS los registros nuevos.

**Reportado:** 2026-06-23 · **Por:** equipo LiaForge (scraping-universidades) · **Severidad:** CRÍTICA
**Estado:** ABIERTO — pendiente de arreglo por el CAF.

## Síntoma
Cualquier alta de cliente nuevo desde LiaForge (`https://liaforge.inovaweb.com.mx/registro/`) falla con
**"error interno"**. Ejemplo real: TepZ Global / miguel@tepz.global.

## Cadena del fallo
1. LiaForge `POST /auth/register` → llama al CAF `POST /api/v2/apps/onboard`.
2. El CAF crea/actualiza el cliente y acredita el grant en `prepaid_ledger` (OK).
3. Al construir la respuesta (`api_router.py:574 api_app_onboard → AppOnboardResponse(...)`) el campo
   **`wallet_id` llega `None`** y el modelo lo exige como `str` → **ValidationError (`string_type`)**.
4. El CAF responde **500** → LiaForge lo traduce a **502 "no se pudo crear el cliente en el CAF"** →
   el formulario muestra "error interno".

## Causa raíz
El refactor **`0650a83 "refactor: eliminar wallet del Medidor — balance a prepaid_ledger"`** eliminó el
wallet del Medidor (el balance ahora vive en `prepaid_ledger`), pero **dejó `wallet_id` como campo
obligatorio** en la respuesta del onboard. Hoy `medidor_account_id` (de donde sale `r.wallet_id`) queda
`None` → el modelo revienta. Refactor incompleto.

## Fix (lado CAF)
`app/routers/api_router.py`, línea **470**:
```python
class AppOnboardResponse(BaseModel):
    client_id: int
    wallet_id: str            # ← CAMBIAR
```
→
```python
    wallet_id: str | None = None   # el wallet del Medidor se eliminó (ledger nativo)
```
(Opcional, más limpio: eliminar `wallet_id` del modelo y del `return AppOnboardResponse(...)` de la línea 574,
ya que el concepto de wallet del Medidor desapareció.)

## Efectos colaterales a limpiar (lado CAF)
- **Clientes huérfanos:** cada intento fallido crea/actualiza el cliente en `clients` pero la respuesta
  revienta, así que LiaForge nunca recibe el `client_id` ni enlaza. Revisar y limpiar los clientes de
  prueba creados (TepZ Global / miguel@tepz.global) antes de re-probar el alta.
- **Correo de activación:** en el mismo onboard se registra `activation_email_send_failed` — revisar el envío.
- **Infra:** el contenedor `medidor-jobs` está `unhealthy` (no bloquea este bug, pero revisar).

## Lado LiaForge (NO requiere cambio)
`app/routers/auth.py` (register) ya tolera la ausencia de `wallet_id`:
```python
try:
    company.medidor_wallet_id = _uuid.UUID(str(data["wallet_id"]))
except (ValueError, KeyError, TypeError):
    pass
```
En cuanto el CAF deje de reventar (devuelva la respuesta con `wallet_id` opcional/ausente), el registro de
LiaForge funcionará sin cambios.
