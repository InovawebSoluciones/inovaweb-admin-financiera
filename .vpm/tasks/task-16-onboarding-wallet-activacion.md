# TASK-16 — Onboarding: crear wallet en Medidor + ligar Scraping + activación de cuenta

**Rol:** Ejecutor (Claude Code). Cross-repo (CAF + Scraping). Es el PEGAMENTO de la integración.
**Repos:** CAF `/sessions/stoic-festive-cray/mnt/inovaweb-admin-financiera/` y Scraping `/sessions/stoic-festive-cray/mnt/scraping_comercial/scraping-universidades/`.
**Modelo:** prepago. Mapeo: CAF clients.id ↔ Company.caf_client_id ↔ Company.id ↔ wallet external_user_id (tenant inovaweb).

## Flujo objetivo (al contratar / confirmar pago del primer plan)
1. **CAF crea la wallet en el Medidor** (key ADMIN, #18): `POST /v1/wallets` con `external_user_id = <Company.id de Scraping>`, metadata {caf_client_id, razon_social}. Guardar el `wallet_id` (UUID) devuelto en `clients.medidor_account_id` (CAF).
2. **CAF liga la wallet en Scraping**: NO escribir directo a la BD de Scraping. Scraping expone un endpoint interno (admin, autenticado) `POST /companies/{company_id}/link-caf` que recibe `{caf_client_id, medidor_wallet_id}` y los persiste en la fila `companies` (campos ya existentes: `caf_client_id`, `medidor_wallet_id` de TASK-21). El CAF lo llama tras crear la wallet.
   - Si prefieres acoplar menos: el CAF pasa el `medidor_wallet_id` y Scraping ya lo lee en authorize/finish (TASK-21 `_resolve_wallet_id`).
3. **Alta de cuenta de acceso en Scraping**: crear el usuario titular de la company para que entre a Scraping (multi-tenant, BD única). Puede ser parte del mismo endpoint de provisión o uno dedicado.
4. **Activación de cuenta** (decisión ya tomada): link de verificación por correo (OBLIGATORIO) + OTP WhatsApp (opcional), AMBOS vía Centro de Mensajes. Plantillas nuevas: `caf-activacion-correo`, `caf-activacion-otp`. SMS fuera del piloto. Generar token de verificación de un solo uso con expiración; NO mandar el token por log.

## Alcance por repo
- **CAF**: extender `app/services/onboarding.py` (o el flujo post-pago de `prepago.py`) para los pasos 1-2 y disparar la activación (paso 4). Cliente HTTP al endpoint de Scraping (paso 2).
- **Scraping**: nuevo endpoint `POST /companies/{company_id}/link-caf` (auth admin/Bearer) que persiste caf_client_id + medidor_wallet_id; y endpoint/lógica de alta de usuario titular + activación si no existe.

## Reglas firmes
- Idempotente: re-ejecutar no duplica wallet (UNIQUE tenant+external_user_id en el Medidor ya protege) ni usuarios.
- Saga: si falla la liga en Scraping tras crear la wallet, compensar/registrar (no dejar estado inconsistente).
- Centavos BIGINT. Auditoría obligatoria. Tokens de activación con hash, expiración, un solo uso.
- No deploy, no commit, no push.
- Depende de #18 (key ADMIN) en runtime y de TASK-21 (columnas medidor_wallet_id/caf_client_id en Company) — ya creadas.

## Verificación
- CAF: import + compileall + pytest (test del onboarding que crea wallet [mock medidor], guarda medidor_account_id, llama al link de Scraping [mock], y dispara activación).
- Scraping: compileall + test del endpoint link-caf (persiste campos) + test de activación (token un-solo-uso, expira).
- Reporta archivos por repo, migraciones si las hay, y salida de verificaciones.
