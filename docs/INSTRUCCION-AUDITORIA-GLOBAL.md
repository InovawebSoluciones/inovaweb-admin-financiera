# INSTRUCCION PARA CLAUDE CODE — Auditoría Global Inovaweb

**Fecha de emisión:** 2026-06-06  
**Emitido por:** VPM (asistente principal Cowork)  
**Ejecuta:** Claude Code (tú)  
**Alcance:** 6 proyectos — 4 cores Nivel 1 + 1 servicio Nivel 2 + 1 app Nivel 3

---

## INSTRUCCION PRINCIPAL

Lee este archivo completo antes de ejecutar cualquier cosa.
Luego ejecuta los pasos en el orden indicado, sin saltarte ninguno.
Ante cualquier duda sobre un proyecto, lee su CLAUDE.md antes de actuar.

---

## 1. ARQUITECTURA DE LA PLATAFORMA INOVAWEB (contexto obligatorio)

La plataforma tiene 3 niveles. Debes entender la jerarquía antes de auditar.

```
NIVEL 3 — Apps Cliente (consumen los cores directamente para IA)
  └── scraping-universidades   Búsqueda semántica con LLM. Piloto activo.

NIVEL 2 — Servicios (orquestan los 4 cores, tienen UI y auth humano)
  └── inovaweb-admin-financiera (CAF)   Centro de Administración Financiera.
                                         Incorpora clientes, gestiona planes,
                                         cobranza, portal cliente, tablero operador.

NIVEL 1 — Cores (API-only, sin UI, sin auth humano, solo API keys)
  ├── medidor_ia           Wallet prepago en centavos MXN. Mide cada llamada LLM.
  ├── inovaweb-hub-pasarelas   Gateway de pagos reales (Conekta). Cobra con tarjeta.
  ├── inovaweb-finanzas-core   Libro contable (ledger de asientos contables MXN).
  └── inovaweb-centro-mensajes  Notificaciones (email + WhatsApp por plantillas).
```

### Principios de diseño que debes tener presentes al auditar

- **Dinero siempre en centavos BIGINT.** Nunca floats. En todos los proyectos.
- **Append-only en tablas financieras.** invoices, payments, adjustments, audit_log,
  wallet_transactions NUNCA se modifican. Correcciones generan nuevas entradas.
- **Auditoría inmutable obligatoria.** Cada escritura financiera registra actor,
  IP, timestamp, valor anterior, valor nuevo. Normalmente enforced por triggers SQL.
- **El Medidor es la fuente única de saldo.** Ningún otro proyecto calcula ni
  duplica el saldo del cliente. Solo leen del Medidor.
- **Idempotencia por request_id.** Las operaciones de crédito y los webhooks de pago
  deben ser idempotentes. Un pago duplicado no debe acreditar dos veces.
- **Scopes de API key separados.** El CAF usa scope ADMIN en el Medidor (puede crear
  wallets y acreditar). Scraping usa scope CLIENT (solo authorize/finish/release).

---

## 2. RUTAS DE TODOS LOS PROYECTOS

```
CAF (Nivel 2):
  C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-admin-financiera

Medidor IA (Core Nivel 1):
  C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\medidor_ia

Hub-Pasarelas (Core Nivel 1):
  C:\Users\conra\inovaweb-hub-pasarelas

Finanzas-Core (Core Nivel 1):
  C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-finanzas-core

Centro-Mensajes (Core Nivel 1):
  C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\inovaweb-centro-mensajes

Scraping Universidades (App Nivel 3):
  C:\Users\conra\OneDrive - Inovaweb\webescolar\Implementacion WebEscolar\scraping_comercial
```

---

## 3. INTERCONEXIONES ENTRE PROYECTOS

### 3.1 Quién llama a quién

```
CAF ──ADMIN──► Medidor:  POST /v1/wallets          (crea wallet del cliente)
                          POST /v1/wallets/{id}/credit  (acredita saldo tras pago)
                          GET  /v1/wallets/{id}/balance (consulta saldo para portal)
                          GET  /v1/usage             (consumo para tablero)
                          POST /v1/wallets/{id}/suspend
                          POST /v1/wallets/{id}/unsuspend

CAF ──────────► Hub:     POST /v1/charges            (inicia cargo en Conekta)
Hub ──webhook──► CAF:    POST /webhooks/hub-payment-paid  (confirma pago realizado)

CAF ──────────► Finanzas: POST /v1/entries           (registra asiento contable)
                           GET  /v1/balance            (lee ingresos para tablero)

CAF ──────────► Mensajes: send_email (template)       (activación, pago, vencimiento)
                           send_whatsapp (template)    (OTP activación, alertas)

CAF ──────────► Scraping: POST /companies/{id}/link-caf  (onboarding: liga wallet)

Scraping ─CLIENT─► Medidor: POST /operations/authorize  (reserva HOLD de crédito)
                             POST /operations/finish      (liquida HOLD con costo real)
                             POST /operations/release     (cancela HOLD sin cargo)
                             POST /operations/quote       (consulta precio estimado)
                             GET  /v1/wallets/{id}/balance (consulta saldo cliente)

Scraping ──────► Finanzas: POST /v1/entries (source_slug=scraping_*)  (consumo IA)
Scraping ──────► Mensajes: notificaciones a usuarios finales
```

### 3.2 Mapeo de identidad entre proyectos

Este es el vínculo que une al cliente en todos los sistemas:

```
CAF: clients.id = "caf-c1a2b3"
  └─ clients.medidor_account_id = "wlt-9f8e7d6c"  ← wallet en Medidor

Scraping: companies.id = "scraping-co-001"         ← external_user_id en Medidor
  └─ companies.caf_client_id = "caf-c1a2b3"
  └─ companies.medidor_wallet_id = "wlt-9f8e7d6c"

Medidor: wallets.external_user_id = "scraping-co-001"
         wallets.tenant_id = "inovaweb"
         wallets.id = "wlt-9f8e7d6c"
```

La identidad fluye así: CAF crea la wallet en el Medidor usando el Company.id de
Scraping como external_user_id. Luego CAF llama a Scraping para persistir la
ligadura. Así los tres sistemas saben que son el mismo cliente.

### 3.3 Flujo de pago de punta a punta (el más crítico)

```
1. Cliente elige plan en portal CAF (app.inovaweb.com.mx)
2. CAF → Hub: POST /v1/charges {amount_cents, source=token_conekta}
3. Hub → Conekta: procesa el cargo bancario
4. Hub → CAF: POST /webhooks/hub-payment-paid {event:payment.paid, transaction_id}
5. CAF valida: HMAC + timestamp anti-replay + correlación purpose/amount
6. CAF → Medidor: POST /v1/wallets/{id}/credit {amount_cents, request_id=caf-recharge-{id}}
7. CAF → Finanzas: POST /v1/entries {source_ref=caf-recharge-{id}, amount_cents}
8. CAF → Mensajes: send_email template=caf-pago-confirmado
```

### 3.4 Flujo de consumo IA de punta a punta

```
1. Usuario hace búsqueda en Scraping
2. Scraping → Medidor: POST /operations/authorize {wallet_id, estimated_cost_cents}
   └─ Medidor crea HOLD, verifica saldo disponible, rechaza si insuficiente
3. Scraping → LLM (OpenAI / Bedrock): ejecuta la búsqueda
4. Scraping → Medidor: POST /operations/finish {hold_id, real_cost_cents}
   └─ Medidor liquida HOLD, debita wallet, registra en ledger append-only
5. Scraping → Finanzas: POST /v1/entries {source_slug=scraping_search, amount_cents}
6. Usuario recibe resultados
```

### 3.5 Flujo de onboarding (liga todos los sistemas)

```
1. CAF recibe alta del cliente (operador o signup-request)
2. CAF → BD local: INSERT clients (status=pending_wallet)
3. CAF → Medidor: POST /v1/wallets {external_user_id=Company.id, tenant=inovaweb}
   └─ guarda wallet_id en clients.medidor_account_id
4. CAF → Scraping: POST /companies/{id}/link-caf {caf_client_id, medidor_wallet_id}
5. CAF → Mensajes: send_email template=caf-activacion-correo (token 1-uso, exp 24h)
6. (opcional) CAF → Mensajes: send_whatsapp template=caf-activacion-otp
```

---

## 4. QUE DEBES AUDITAR EN CADA PROYECTO

Para cada proyecto (ejecutar en el orden de la sección 5):

### A. Revisión de código

**Correctitud funcional:**
- Lógica de negocio de los flujos críticos del proyecto
- Manejo de errores y casos borde (¿qué pasa si el core externo falla?)
- Condiciones de carrera o estados inconsistentes posibles
- Retry logic y timeouts en llamadas HTTP externas
- Idempotencia: ¿los endpoints que reciben webhooks o comandos repetibles son idempotentes?

**Consistencia de contratos entre proyectos:**
- Verifica que lo que CAF envía al Medidor coincida con lo que el Medidor espera
- Verifica que lo que Scraping envía al Medidor coincida con el contrato real
- Verifica que los source_ref y source_slug usados en Finanzas sean consistentes
- Verifica que los template_id usados en CAF existan (o estén esperados) en Mensajes

**Calidad del código:**
- Toda función pública debe tener docstring. Falta = hallazgo.
- Type hints en firmas de funciones públicas
- Comentarios en lógica no obvia

Hallazgos:
- CRITICO: bug que puede corromper datos o estado financiero. Bloquea commit.
- MEJORA: docstring faltante, type hint ausente. Queda como deuda documentada.

### B. Auditoría OWASP

Para cada proyecto verificar:
- SQL Injection
- XSS (si tiene templates o UI)
- CSRF (si tiene UI o endpoints que usan cookies)
- Secrets hardcodeados en código
- Gestión de sesiones (JWT, cookies, tokens)
- Endpoints sin autenticación

Un FAIL bloquea el commit del proyecto afectado.

### C. Documentación a generar

Para cada proyecto generar o actualizar:

**Docs técnicos formales** (en el repo de cada proyecto):
- README.md (descripción, arquitectura, stack, vars de entorno, cómo correr, URLs)
- docs/ADR.md (decisiones arquitectónicas, usar formato ADR-NNN)
- docs/RUNBOOK.md (síntoma → diagnóstico → fix → verificación por componente)
- docs/DEPLOY.md (pre-requisitos, deploy, migraciones, rollback, checklist)
- CHANGELOG.md (derivar del CLAUDE.md o HISTORIAL_SESIONES.md del proyecto)
- docs/OWASP.md (reporte de auditoría de seguridad)

**Docs para desarrolladores** (en el repo de cada proyecto):
- docs/GUIA-DESARROLLADOR.md:
  - Onboarding técnico desde cero
  - Mapa del código (qué hace cada módulo)
  - Contratos de integración (qué consume este proyecto y qué expone)
  - Flujos principales paso a paso
  - Convenciones del proyecto

**Docs para humanos** (solo en el CAF como orquestador principal, y en Scraping):
- docs/GUIA-USUARIO-OPERADOR.md (equipo Inovaweb que opera el sistema)
- docs/GUIA-USUARIO-CLIENTE.md (cliente final que usa el producto)
- docs/RESUMEN-EJECUTIVO.md (dirección/stakeholders, máximo 1 página)

**Documento adicional de arquitectura global** (solo en el CAF):
- docs/ARQUITECTURA-GLOBAL.md: documento que describe toda la plataforma,
  cómo se interconectan los 6 proyectos, los flujos de datos de punta a punta,
  los contratos entre capas, y el mapa de identidad entre sistemas.
  Este documento es la referencia principal para cualquier desarrollador nuevo
  que quiera entender la plataforma completa.

---

## 5. ORDEN DE EJECUCION

Ejecutar en este orden exacto:

### FASE 1 — Exploración (leer, no escribir nada todavía)

1. Lee el CLAUDE.md de cada proyecto para entender su estado actual.
2. Lee la estructura de archivos de cada proyecto (app/, src/, database/, tests/, docs/).
3. Lee los contratos HTTP clave de cada core: rutas, autenticación, modelos de request/response.
4. Identifica qué cliente HTTP usa cada proyecto para llamar a los otros
   (busca los archivos *_client.py o equivalentes).
5. Construye mentalmente el mapa completo de llamadas entre proyectos
   (verifica que lo que encontraste coincide con la sección 3 de este documento).
   Si hay discrepancias, anótalas — son hallazgos importantes.

### FASE 2 — Revisión de código cruzada

1. Audita el código de cada proyecto en busca de hallazgos CRITICO y MEJORA.
2. Presta especial atención a los puntos de integración:
   - ¿El cliente HTTP del CAF hacia el Medidor envía los campos correctos?
   - ¿El webhook del Hub valida HMAC y anti-replay?
   - ¿El authorize/finish de Scraping maneja correctamente el caso de saldo insuficiente?
   - ¿Los source_ref y request_id son determinísticos para garantizar idempotencia?
3. Verifica que ningún proyecto almacene dinero en floats.
4. Verifica que las tablas financieras tengan protección append-only (triggers o equivalente).
5. Reporta hallazgos por proyecto con archivo y línea cuando sea posible.

### FASE 3 — Auditoría OWASP

Ejecutar la auditoría OWASP para cada proyecto por separado.
Generar docs/OWASP.md en cada proyecto.
Si hay un FAIL, registrarlo claramente — no continúa el commit de ese proyecto.

### FASE 4 — Generación de documentación

Generar o actualizar los documentos listados en la sección 4C para cada proyecto.

Orden recomendado de proyectos (de más bajo a más alto nivel):
1. medidor_ia          ← YA TIENE AUDITORÍA RECIENTE (ver nota abajo)
2. inovaweb-hub-pasarelas
3. inovaweb-finanzas-core  ← YA TIENE AUDITORÍA RECIENTE (ver nota abajo)
4. inovaweb-centro-mensajes
5. inovaweb-admin-financiera (CAF) — incluye ARQUITECTURA-GLOBAL.md
6. scraping_comercial

**NOTA — medidor_ia e inovaweb-finanzas-core:**
Estos dos proyectos ya cuentan con una auditoría reciente equivalente a la
que se solicita aquí. Para ellos NO hacer auditoría desde cero. En su lugar:

1. Leer la documentación existente (README, ADR, RUNBOOK, DEPLOY, OWASP, CHANGELOG).
2. Leer el código para verificar que la documentación existente sigue siendo
   precisa (puede haber cambiado desde la última auditoría).
3. Identificar únicamente lo que esté desactualizado o faltante respecto al
   estándar definido en la sección 4C (ej: si falta GUIA-DESARROLLADOR.md,
   generarla; si el RUNBOOK no cubre algún componente nuevo, completarlo).
4. Aplicar solo las actualizaciones necesarias — no reescribir lo que ya está bien.
5. Registrar en el CHANGELOG de cada proyecto qué se actualizó en esta sesión.

Para los 4 proyectos restantes (hub-pasarelas, centro-mensajes, CAF, scraping)
sí ejecutar el flujo completo.

### FASE 5 — Comandos de commit

Al terminar cada proyecto, dar el comando de commit listo para ese proyecto.
Formato:

```bash
cd "C:\Users\conra\...\[nombre-proyecto]"
git add .
git commit -m "docs: auditoria global + documentacion completa 2026-06-06"
git push origin main
```

---

## 6. ENTREGA FINAL

Al terminar todos los proyectos, presentar esta tabla de resumen global:

| Proyecto | Rev. Código | OWASP | README | ADR | RUNBOOK | DEPLOY | CHANGELOG | GUIA-DEV | GUIA-OPER | GUIA-CLIENTE | RESUMEN-EXEC | Commit |
|----------|-------------|-------|--------|-----|---------|--------|-----------|----------|-----------|--------------|--------------|--------|
| medidor_ia *(solo delta)* | | | | | | | | | — | — | — | |
| hub-pasarelas | | | | | | | | | — | — | — | |
| finanzas-core *(solo delta)* | | | | | | | | | — | — | — | |
| centro-mensajes | | | | | | | | | — | — | — | |
| CAF | | | | | | | | | ✓ | ✓ | ✓ | |
| scraping | | | | | | | | | ✓ | ✓ | — | |

Leyenda: OK / WARN / FAIL / — (no aplica) / *(solo delta)* = auditoría previa reciente; solo verificar y actualizar lo que haya cambiado

---

## 7. CAMBIOS DE INFRAESTRUCTURA — LEER ANTES DE DOCUMENTAR EL DEPLOY

**Caddy fue reemplazado por Nginx.** Esto afecta a todos los proyectos.

- El `Caddyfile` que existe en la raíz del CAF y posiblemente en otros repos
  es referencia histórica, NO refleja la configuración real del VPS.
- Antes de escribir docs/DEPLOY.md o docs/RUNBOOK.md de cualquier proyecto,
  leer la configuración real de Nginx en el VPS:
  - Buscar en `/etc/nginx/` o `/opt/nginx/` o dentro del contenedor Nginx
  - Verificar cómo están configurados los virtual hosts para cada dominio
  - Verificar cómo se maneja TLS (Let's Encrypt / certbot / manual)
- No asumir que la red se llama `n8n_default` ni que el stack de n8n sigue siendo
  el que gestiona el reverse proxy — verificar en el VPS antes de documentar.
- Si en algún repo hay referencias a `n8n-caddy-1` o a bloques de Caddyfile,
  son hallazgos de documentación desactualizada — registrarlos y corregirlos.

---

## 8. REGLAS QUE NO PUEDES SALTARTE

1. **Leer antes de escribir.** No generes documentación de un proyecto sin haber
   leído su código fuente. Documentación inventada es peor que documentación ausente.
2. **Un FAIL de código CRITICO o OWASP bloquea el commit de ese proyecto.**
   Reportarlo claramente y esperar instrucciones del usuario.
3. **No modifiques código.** Tu rol aquí es auditor y documentador. Si encuentras
   un bug CRITICO, repórtalo pero no lo corrijas sin autorización explícita.
4. **Centavos BIGINT en todos lados.** Si encuentras dinero en float en cualquier
   proyecto, es un hallazgo CRITICO automático.
5. **Sin inventar contratos.** Si no puedes leer el código de un core para verificar
   un contrato HTTP, dilo explícitamente y deja un [TODO: verificar] en la doc.
6. **Actualiza el CLAUDE.md de cada proyecto** con la fecha y estado al final de
   la auditoría de ese proyecto.

---

## 8. FUENTE DE VERDAD DE CADA PROYECTO

Para entender el estado de cada proyecto, leer en este orden:
1. Su CLAUDE.md (sección de estado/pendientes)
2. Su CHANGELOG.md (si existe)
3. Su HISTORIAL_SESIONES.md (si existe)
4. El código fuente directamente

No hagas suposiciones sobre el estado de un proyecto sin leer sus archivos primero.
