# TASK-16 EJECUTOR — Sesión 2026-06-06 (CAF + Scraping)

**Repo CAF:** `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera\`
**Repo Scraping:** `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\scraping_comercial\scraping-universidades\`
**GitHub Scraping:** `https://github.com/InovawebSoluciones/scraping-inovaweb`
**Nota:** Tienes acceso completo a ambos repos. Implementar los dos lados: CAF y Scraping.

## Qué implementar en esta sesión (CAF)

### 1. `app/core/clients/scraping_client.py` (nuevo)
Cliente HTTP async al endpoint interno de Scraping:
- `POST /companies/{company_id}/link-caf` con `{caf_client_id, medidor_wallet_id}`
- Leer `SCRAPING_BASE_URL` y `SCRAPING_ADMIN_KEY` de `app/core/config.py` (agregarlos si no existen)
- Usar el mismo patrón que `_base.py` / `CoreClient`
- Docstring completo, type hints

### 2. `app/core/clients/messages_client.py` — revisar/extender
Verificar que existe y funciona `send_email(template, to, variables)`.
Si falta, implementarlo. Plantillas que usará el onboarding:
- `caf-activacion-correo` — link de verificación
- `caf-pago-confirmado` — confirmación de alta

### 3. `app/services/onboarding.py` — extender flujo post-wallet
Después del paso 2 (wallet creada), agregar:
- **Paso 2b:** llamar `scraping_client.link_caf(company_id, caf_client_id, medidor_wallet_id)`
  - `company_id` viene del payload (campo nuevo: `scraping_company_id: int | None`)
  - Si `scraping_company_id` es None, saltar este paso (no todos los clientes son de Scraping)
  - Si falla: compensar (suspend wallet) + registrar en audit_log, igual que hoy
- **Paso 5b:** generar token de activación de un solo uso (32 bytes, hash SHA-256 en BD, expiración 24h)
  - INSERT en tabla `activation_tokens` (ver schema abajo)
  - Llamar `messages_client.send_email('caf-activacion-correo', titular_email, {token_url})`
  - Fallos de mensajería: loguear, NO abortar el onboarding (el token ya existe en BD)

### 4. Migración SQL nueva: `database/003_activation_tokens.sql`
```sql
CREATE TABLE IF NOT EXISTS activation_tokens (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id),
    token_hash  TEXT NOT NULL UNIQUE,   -- SHA-256 hex del token original
    expires_at  TIMESTAMPTZ NOT NULL DEFAULT now() + interval '24 hours',
    used_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_activation_tokens_user ON activation_tokens(user_id);
```

### 5. `app/core/config.py` — agregar si faltan:
```python
SCRAPING_BASE_URL: str = "https://scraping.inovaweb.com.mx"
SCRAPING_ADMIN_KEY: SecretStr
```

## Reglas firmes
- Idempotente: re-ejecutar no duplica wallet (ya protegido en Medidor) ni tokens
- Saga: fallos externos (Scraping, Mensajes) tienen compensación o son non-fatal
- Tokens de activación: hash SHA-256, expiración 24h, un solo uso
- Centavos BIGINT. Auditoría obligatoria en cada paso externo nuevo
- NO hacer commit, NO hacer push, NO levantar Docker

## Verificación obligatoria
1. `python -m py_compile` en todos los archivos tocados
2. Agregar/actualizar tests en `tests/test_onboarding.py`:
   - Mock de `scraping_client.link_caf` — verifica que se llama con los parámetros correctos
   - Mock de `messages_client.send_email` — verifica que se llama con plantilla correcta
   - Test de compensación: fallo en link-caf → suspend_wallet + audit_log
   - Test de flujo sin scraping_company_id (campo None → skip link-caf)
3. Reportar: archivos creados/modificados, líneas cambiadas, output de py_compile

## Archivos de referencia (leer antes de empezar)

### CAF
- `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera\app\services\onboarding.py`
- `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera\app\core\clients\medidor_client.py`
- `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera\app\core\clients\_base.py`
- `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera\app\core\config.py`
- `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera\app\core\clients\messages_client.py`
- `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera\tests\test_onboarding.py`

### Scraping (leer estructura antes de tocar)
- `C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\scraping_comercial\scraping-universidades\` — explorar para entender modelos, endpoints y estructura antes de escribir código
