# TASK-07 — Seed del catálogo de planes Scraping (modelo PREPAGO)

**Rol que ejecuta:** Ejecutor (Claude Code).
**Repo (bash VM):** `/sessions/stoic-festive-cray/mnt/inovaweb-admin-financiera/`
**Esquema de referencia:** `database/001_initial_schema.sql` (tablas `plans`, `plan_items`, `services`).
**Decisiones de negocio:** ver `project_caf_piloto_scraping` (modelo prepago).

---

## Parte A — Verificación previa (tareas #3 y #4, ya scaffoldeadas)
Antes de sembrar, confirma que la base está sana (solo verificar, NO reconstruir):
1. `database/001_initial_schema.sql` y `002_security_constraints.sql` aplican sin error
   en un Postgres limpio. Levanta uno efímero (docker o el de docker-compose) y corre
   ambos scripts. Reporta rc y cualquier error.
2. Los módulos del núcleo importan: `python -c "import app.core.config, app.core.database, app.core.jwt_auth, app.core.password, app.core.audit"` (con env mínimo si hace falta). Reporta resultado.
Si algo falla aquí, NO sigas con la Parte B: repórtalo y detente.

## Parte B — Seed de planes (lo nuevo)

Crear `database/003_seed_scraping_plans.sql` con los planes del piloto. Modelo
**PREPAGO**: el `monthly_fee_cents` de cada plan = el saldo (crédito) que se acredita
a la wallet del Medidor al contratar. NO hay cuotas fijas por servicio todavía
(el desglose tokens/créditos/mensajes lo definirá el usuario después), así que
`plan_items` se deja vacío por ahora.

Planes a insertar (centavos MXN, idempotente con `ON CONFLICT (code) DO UPDATE`):

| code     | name             | monthly_fee_cents | is_free | is_active |
|----------|------------------|-------------------|---------|-----------|
| free     | Free / Entrada   | 10000             | false   | true      |
| basico   | Básico           | 9900              | false   | true      |
| medio    | Medio            | 20000             | false   | true      |
| premium  | Premium          | 40000             | false   | true      |

Notas a incluir como comentario SQL al inicio del archivo:
- Cifras de REFERENCIA, se ajustarán después.
- "Free/Entrada" tiene precio (no es gratis); `is_free=false` a propósito. El nombre
  es comercial.
- Planes especiales = contacto directo, NO se siembran (sin precio público).
- Modelo prepago: precio del plan = crédito a la wallet; sin excedente; al agotar saldo
  se recontrata.
- `description` de cada plan: una línea breve indicando "crédito prepago de $X MXN".

Reglas:
- Idempotente: correrlo 2 veces no duplica ni rompe (usa `ON CONFLICT (code)`).
- Centavos BIGINT. Nada de floats.
- No tocar 001/002 ni código de app/.
- No commit ni push.

## Verificación (ejecútala de verdad y reporta salida)
1. Aplicar 001 + 002 + 003 en un Postgres limpio: rc=0 sin errores.
2. `SELECT code, name, monthly_fee_cents, is_active FROM plans ORDER BY monthly_fee_cents;`
   → muestra los 4 planes con los montos correctos.
3. Correr 003 una segunda vez → sin error, sigue habiendo 4 filas (idempotencia).
4. Reporta la salida concreta de los 3 puntos.

Entrega: reporte conciso con archivos creados, resultado de Parte A (verificación
#3/#4), y la salida de las 3 verificaciones de la Parte B.
