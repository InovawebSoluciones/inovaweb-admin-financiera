# Decisiones de Arquitectura (ADR)

Registro inmutable de decisiones que dan forma al Centro de Administración
Financiera. Cada ADR se mantiene aunque la decisión cambie después: se añade
una nueva entrada que supersede a la anterior, nunca se borra.

---

## ADR-001: Un solo backend FastAPI para los dos dominios

**Fecha:** 2026-05-26
**Estado:** Aprobado

### Contexto
El CAF expone dos audiencias muy distintas: el operador interno
(`admin.inovaweb.com.mx`) y el cliente externo (`app.inovaweb.com.mx`). La
disyuntiva inicial fue desplegar dos backends FastAPI separados (uno por
dominio) o uno solo que sirva ambos.

### Decisión
Un único backend en el contenedor `caf_app`, expuesto en el host por el puerto
`8006`. El middleware `HostEnforcementMiddleware` (`app/main.py`) decide qué
rutas son válidas para cada `Host`:
- `/admin/*` solo se sirve si `Host == ADMIN_DOMAIN`
- `/portal/*` solo se sirve si `Host == PORTAL_DOMAIN`
- `/health`, `/login`, `/api/*`, `/webhooks/*` viven en ambos.

### Alternativas consideradas
- **Dos contenedores separados:** descartado por duplicación de imagen,
  duplicación de conexiones a Postgres, doble ciclo de deploy y mayor costo
  de operación para un equipo pequeño. La ganancia (aislamiento) no compensa.
- **Reverse proxy con prefix rewriting:** descartado porque obliga a Caddy a
  conocer el dominio interno y complica el cambio de dominios.

### Consecuencias
- ✅ Una sola imagen, un solo deploy, un solo set de variables de entorno.
- ✅ El share de código (auth, audit, clientes de cores) es trivial.
- ⚠️ Un bug en el routing del middleware puede exponer rutas internas al
  cliente. Mitigación: tests unitarios obligatorios de `HostEnforcementMiddleware`
  y redirect 308 al dominio correcto en lugar de servir contenido.

---

## ADR-002: Saga de onboarding atómico cross-core

**Fecha:** 2026-05-26
**Estado:** Aprobado

### Contexto
Dar de alta a un cliente requiere 4 escrituras en cores Nivel 1 distintos
(medidor → wallet, hub → cuenta de cobro, finanzas → cuenta ledger, mensajes
→ identidad de notificación) más la fila en la BD del CAF. Cada core es un
servicio HTTP independiente; no existe transacción distribuida. Si la
escritura 3 falla, las dos primeras quedan huérfanas.

### Decisión
Patrón Saga con compensación en `app/services/onboarding.py`. Cada paso
declara una operación de compensación (`DELETE wallet`, `DELETE cuenta hub`,
etc.). Si el paso N falla, se ejecutan las compensaciones de 1..N-1 en orden
inverso y se registra el fallo en `audit_log` con la traza completa. El
cliente queda en estado `provisioning_failed` para revisión manual.

### Alternativas consideradas
- **Two-phase commit distribuido:** descartado por costo de implementación
  en 4 cores ya en producción que no lo soportan.
- **Eventual consistency con outbox + retries:** descartado para el alta
  porque queremos respuesta síncrona al operador; no podemos decirle "tu
  cliente eventualmente quedará dado de alta".
- **Crear todo primero en CAF y push asíncrono a los cores:** descartado
  porque medidor y hub deben emitir IDs que el CAF necesita en la respuesta.

### Consecuencias
- ✅ Atomicidad observable: el operador ve éxito o fallo, no estado parcial.
- ✅ Cada core mantiene su API independiente.
- ⚠️ Si la compensación falla (p.ej. core caído al momento del rollback)
  queda huérfano un recurso. Mitigación: el fallo de compensación se
  registra en `audit_log` con flag `requires_manual_cleanup` y dispara
  alerta vía centro-mensajes al super-admin.
- ⚠️ Latencia del alta = suma de latencias de 4 cores. Mitigación: timeout
  agresivo por paso (10s) y respuesta clara al operador si se rebasa.

---

## ADR-003: Auditoría enforced por triggers de Postgres, no por la app

**Fecha:** 2026-05-26
**Estado:** Aprobado

### Contexto
La auditoría inmutable es una obligación regulatoria (modelo de seguridad
descrito en `SECURITY.md`). Se podía implementar a nivel aplicación
(SQLAlchemy events) o a nivel base de datos (triggers PL/pgSQL).

### Decisión
Triggers PL/pgSQL en `database/002_security_constraints.sql`:
- `audit_log`, `payments`: bloqueo total de UPDATE y DELETE.
- `invoices`, `adjustments`: DELETE bloqueado; UPDATE solo a campos no
  financieros (lista blanca: `status`, paths, stamp data).
- Triggers `AFTER INSERT/UPDATE/DELETE` en entidades sensibles que escriben
  a `audit_log` con el diff completo.

La app no puede ni siquiera intentar saltarse esto; la BD rechaza la
operación con `RAISE EXCEPTION` y la transacción aborta.

### Alternativas consideradas
- **Auditoría a nivel aplicación:** descartada porque un bug, un script
  manual, o un acceso directo con `psql` la rompen. La BD es la última
  línea de defensa.
- **Auditoría en log externo (Loki / S3):** descartada por costo y porque
  no resuelve el riesgo de un UPDATE directo en `payments`.

### Consecuencias
- ✅ Cumplimiento independiente del comportamiento de la app.
- ✅ Funciona incluso para acceso humano a la BD durante incidentes.
- ⚠️ Migraciones futuras deben respetar el modelo append-only. No se puede
  "limpiar" una tabla afectada por estos triggers; correcciones se hacen
  con entradas nuevas (notas de crédito, ajustes con motivo).
- ⚠️ Trigger en cada INSERT añade overhead. Mitigación: el diff se calcula
  con `to_jsonb(NEW) - to_jsonb(OLD)`, costo aceptable para el volumen
  esperado (<100k facturas/mes en horizonte de 3 años).

---

## ADR-004: JWT con cookie httpOnly + rotación de refresh, en vez de tokens en localStorage

**Fecha:** 2026-05-26
**Estado:** Aprobado

### Contexto
El CAF tiene UI server-side con HTMX. Hay que decidir cómo se persiste la
sesión entre requests sin SPA.

### Decisión
- Access token JWT en cookie `caf_at`: `HttpOnly`, `Secure`, `SameSite=Strict`,
  TTL 15 min.
- Refresh token JWT en cookie `caf_rt`: mismas flags, TTL 30 días, **rotación
  obligatoria** en cada uso (se invalida el viejo, se emite uno nuevo,
  ambos se registran en `refresh_tokens` con `replaced_by`).
- Tabla `refresh_tokens` en BD con flag `revoked` y `replaced_by`. Si llega
  un refresh con un token ya marcado `revoked` (= reuse attempt), se invalida
  toda la cadena de refresh del usuario y se obliga login nuevo.

### Alternativas consideradas
- **Tokens en `localStorage`:** descartado por riesgo XSS. HTMX no nos obliga
  a SPA, y queremos cookies httpOnly.
- **Sesiones server-side en Redis:** descartado por no tener Redis en el
  stack y por no querer agregarlo solo para esto. JWT con rotación es
  suficiente para el volumen esperado.

### Consecuencias
- ✅ Inmune a XSS robando tokens.
- ✅ Logout efectivo: revoca el refresh y los nuevos access tokens caducan
  en ≤ 15 min.
- ⚠️ La cookie viaja en cada request. Cookies son chicas (~1KB), aceptable.
- ⚠️ La rotación añade complejidad. Documentada en `app/core/jwt_auth.py`.

---

## ADR-005: PAC adapter pattern (Facturama default, swap a Factible/Edicom sin tocar `invoicing`)

**Fecha:** 2026-05-26
**Estado:** Aprobado

### Contexto
CFDI 4.0 requiere un PAC certificado. En México hay tres viables (Facturama,
Solución Factible, Edicom) con APIs distintas. Cambiar de PAC durante la vida
del proyecto es posible (precios, downtime, soporte). No queremos amarrarnos.

### Decisión
- Cliente del PAC en `app/core/clients/pac_client.py` expone interfaz
  estable (`timbrar(xml: bytes) -> StampResult`, `cancelar(uuid: str)`).
- `PAC_PROVIDER` en `.env` (`facturama` / `factible` / `edicom`) selecciona
  la implementación. El servicio `app/services/invoicing.py` solo conoce la
  interfaz, nunca el provider concreto.
- El XML CFDI 4.0 se genera localmente con `lxml` (no delegado al PAC) para
  que el cambio de PAC no requiera regenerar templates.

### Alternativas consideradas
- **Cablear Facturama directo:** descartado por lock-in.
- **Librería tercero `cfdi-python`:** evaluada pero abandonada por bajo
  mantenimiento (último commit > 1 año).

### Consecuencias
- ✅ Cambiar de PAC = 1 archivo nuevo en `clients/` + flip de variable.
- ⚠️ Tres implementaciones que mantener. Mitigación: solo se implementa
  Facturama en sprint 4; los otros dos quedan como stubs hasta que haya
  demanda real.
- ⚠️ Generar XML localmente con `lxml` exige mantener el namespace y el
  esquema actualizados. Tarea documentada en `RUNBOOK.md`.

---

## ADR-006: Workers como contenedores `restart: no` con profile `jobs`, no daemons

**Fecha:** 2026-05-26
**Estado:** Aprobado

### Contexto
El CAF tiene 3 jobs batch (`monthly_closing`, `invoice_retry`,
`overdue_notifier`). Disyuntiva: daemons internos con `apscheduler` dentro
del contenedor `caf_app`, o contenedores separados disparados por cron del
host.

### Decisión
Cada worker es un servicio Docker Compose con `restart: "no"` y
`profiles: ["jobs"]`. Se invocan vía:
```bash
docker compose --profile jobs run --rm monthly_closing
```
El cron del host (no del contenedor de la app) los dispara. Tabla de cron
sugerida en `docs/RUNBOOK.md`.

### Alternativas consideradas
- **APScheduler dentro de `caf_app`:** descartado porque el job pesado
  (cierre mensual) podría afectar el SLA del backend HTTP, y porque si el
  cierre falla, queda en estado raro dentro del proceso web.
- **Kubernetes CronJob:** descartado por overhead. No vamos a desplegar k8s
  para 3 jobs.

### Consecuencias
- ✅ Aislamiento total: un cierre mensual con error no tira el backend HTTP.
- ✅ Observabilidad: cada run tiene su contenedor con logs separados.
- ⚠️ Cron del host es un punto que documentar (en `DEPLOY.md` y `RUNBOOK.md`).
  Si el host muere, los jobs no corren. Mitigación: alerta de
  `overdue_notifier` ausente > 2 días.

---

## ADR-007: Catálogos editables (productos / servicios / planes / promociones) versus precios cableados

**Fecha:** 2026-05-26
**Estado:** Aprobado

### Contexto
Hoy los precios viven en código en los 4 cores Nivel 1. Cambiar el precio
de un correo de 50¢ a 70¢ requiere redespliegue del centro-mensajes. Esto
fue una de las motivaciones explícitas del CAF.

### Decisión
Los precios son filas de BD en el CAF. Los cores Nivel 1 dejan de conocer
precios y solo reportan consumo crudo. El cierre mensual del CAF
(`app/services/billing.py`) lee el consumo del finanzas-core, lo cruza con
el plan vigente del cliente en la BD del CAF, aplica promociones activas y
emite la factura.

### Alternativas consideradas
- **Precios federados (cada core sigue conociendo sus precios):** descartado
  porque hace imposible un plan unificado que mezcle conceptos de varios
  cores (p.ej. "Plan Pro: 10k correos + 5k consultas IA + storage").
- **Precios en archivo YAML versionado en git:** descartado porque obliga
  a un deploy para cambiar un precio. La UI tiene que poder modificarlos.

### Consecuencias
- ✅ Cambio de precio = update a fila + nueva entrada en `audit_log`. Sin
  deploy.
- ✅ Promociones, descuentos por volumen y planes son combinables.
- ⚠️ La BD del CAF se vuelve fuente de verdad de pricing. Si se pierde, el
  cierre mensual no se puede ejecutar. Mitigación: backup nocturno con
  prueba mensual de restauración (en `RUNBOOK.md`).
- ⚠️ Riesgo de cambiar un precio activo con facturas en vuelo. Mitigación:
  los precios se versionan (tabla `price_versions` con `valid_from` /
  `valid_to`), y el cierre mensual congela el precio a la fecha de consumo,
  no a la fecha de cierre.

---

## ADR-008: Sin Redis ni Celery — colas implícitas en Postgres con `SELECT ... FOR UPDATE SKIP LOCKED`

**Fecha:** 2026-05-26
**Estado:** Aprobado

### Contexto
Hay tareas asíncronas (reintento de timbrado PAC, envío de notificaciones).
La opción canónica es Celery + Redis. Pregunta: ¿vale la pena el operacional?

### Decisión
Tabla `invoice_retry_queue` (y similares) en Postgres. Los workers hacen
`SELECT ... FROM queue WHERE next_attempt_at <= now() ORDER BY id FOR UPDATE
SKIP LOCKED LIMIT N`, procesan, actualizan estado. Mismo patrón que
`ledger_retry` del centro-mensajes.

### Alternativas consideradas
- **Celery + Redis:** descartado por sumar dos componentes (broker + result
  backend) para un volumen pequeño. Postgres ya está, sabe hacer locks.
- **RabbitMQ / SQS / Kafka:** descartado por sobreingeniería.

### Consecuencias
- ✅ Un componente menos que operar.
- ✅ Visibilidad nativa: `SELECT * FROM invoice_retry_queue` muestra el
  backlog desde `psql`.
- ⚠️ Postgres no es ideal a partir de ~10k jobs/min. Para el horizonte del
  CAF (<1k jobs/día) sobra. Si el volumen crece, migrar a Redis es una
  decisión futura, documentada como pendiente.

---

## ADR-009: El medidor IA es la fuente única del costo de consumo de IA por cliente

**Fecha:** 2026-06-03
**Estado:** Aprobado

### Contexto
Cada llamada a un LLM (DeepSeek, OpenAI, Claude, etc.) reporta el consumo de
tokens (entrada + salida). Ese consumo hay que convertirlo a costo monetario
y cobrarlo al cliente. Dónde vive esa lógica define la arquitectura
financiera del consumo IA en toda la plataforma.

Opciones discutidas:
- Que cada app Nivel 3 calcule su propio costo y reporte el monto al CAF.
- Que el CAF lea tokens crudos y los multiplique por tarifa configurada.
- Que el medidor IA reciba el reporte de tokens, calcule el costo en pesos
  mexicanos (centavos enteros BIGINT), debite el wallet del cliente y emita
  un evento `direction=debit, source=medidor` al finanzas-core.

### Decisión
**El medidor IA (Nivel 1) es la única pieza autorizada para convertir tokens
a pesos.** Toda llamada a LLM en cualquier app Nivel 3 pasa por el medidor
(o por su proxy de LLM), que:

1. Recibe el reporte de tokens de la IA (`tokens_in`, `tokens_out`, modelo).
2. Aplica la tarifa vigente del modelo (en centavos por mil tokens).
3. Debita el wallet del cliente en centavos MXN.
4. POST al finanzas-core con `source_slug=medidor`, `direction=debit`,
   `amount_cents=<pesos en centavos>`.

El CAF jamás duplica saldos ni recalcula costos de IA. Para mostrar saldo y
consumo en el portal cliente, el CAF consume:
- `GET /v1/wallets/{wallet_id}/balance` del medidor.
- `GET /v1/usage?from_ts=...&to_ts=...&project_id=<wallet_id>` del medidor.

Para el cierre mensual, el CAF agrega del finanzas-core los eventos
`source=medidor` del cliente y los presenta en la factura como "consumo IA"
sin recalcular.

### Alternativas consideradas
- **CAF recalcula costo de IA con tarifa propia:** descartado. Habría dos
  fuentes de verdad (tarifa en CAF y tarifa en medidor) y inevitablemente
  divergirían. Además, el medidor ya debita el wallet en vivo; si CAF
  recalculara distinto, el cliente vería saldos contradictorios.
- **Cada app Nivel 3 calcula su costo de IA:** descartado. Implica
  reimplementar tarifas en cada app, y abre la puerta a fraude (la app
  podría reportar menos consumo del real).
- **Tarifa cableada en código del medidor:** descartado dentro del propio
  medidor (debe ser configurable en BD del medidor), pero el medidor sigue
  siendo el único responsable de aplicarla.

### Consecuencias
- ✅ Una sola fuente del costo IA en pesos para toda la plataforma.
- ✅ El cliente ve el mismo número en el wallet del medidor, en el portal
  CAF y en la factura mensual.
- ✅ Cambiar la tarifa de un modelo (ej. baja de precio de DeepSeek) se
  hace en el medidor y todos los cores se enteran sin redeploy.
- ⚠️ Caída del medidor = no se puede consumir IA en ninguna app Nivel 3.
  Mitigación: SLO estricto del medidor (>99.9%), monitoreo dedicado, y el
  medidor es el core más viejo y estable del stack.
- ⚠️ Cambio de tarifa con consumo en vuelo requiere versionado de tarifa
  con `valid_from` para evitar recalcular consumo histórico. Documentado
  como deuda del medidor (no del CAF).

### Cómo se ve en el CAF
- `app/core/clients/medidor_client.py` solo expone lectura
  (`get_balance`, `get_usage_summary`, `get_usage_events`) y acreditación
  por recargas confirmadas (`credit_after_recharge`). **No tiene método
  para debitar ni para recalcular costo de IA.**
- El cierre mensual (`app/services/billing.py`) lee eventos del
  finanzas-core con `source=medidor` y los presenta como-son. No
  multiplica ni convierte.
- El portal cliente (`/portal/dashboard`, `/portal/usage`) hace pass-through
  de los números del medidor.

---

## ADR-010: El modelo del piloto Scraping es PREPAGO (recarga de wallet); la facturación mensual + CFDI se difiere

**Fecha:** 2026-06-03
**Estado:** Aprobado

### Contexto
El primer cliente real del CAF es la app Nivel 3 **Scraping**. El diseño
original del CAF (CLAUDE.md, fases 4 y 5) asume cobranza **pospago**: cierre
mensual nocturno que mide el consumo del periodo, aplica plan + promociones y
emite una factura CFDI 4.0 timbrada vía PAC. Ese flujo exige tener un PAC
seleccionado y certificados CSD operativos, decisión que aún no se toma (la selección
de PAC vive como pendiente al final de este documento).

Para arrancar el piloto sin bloquearlo en la selección de PAC, hay que decidir
el modelo de cobro del piloto.

### Decisión
**El piloto Scraping opera en modelo PREPAGO.** El cliente **recarga su wallet
en el Medidor** (vía Hub-Pasarelas, Conekta sandbox) y el consumo de IA se
debita de ese saldo en vivo por el propio Medidor (ver ADR-009). No hay cierre
mensual ni emisión de factura en el piloto.

Consecuencias en el onboarding y los clientes de cores:
- El alta atómica (`app/services/onboarding.py`) solo crea la **wallet** del
  cliente en el Medidor; no factura ni programa cobranza mensual.
- Los clientes HTTP a Finanzas-Core y Centro de Mensajes son de **lectura /
  emisión** según el contrato real (`docs/01-admin-financiera-integracion-cores.md`),
  sin creación de cuentas ni emisión de llaves por cliente.
- El cierre mensual (`app/services/billing.py`), el timbrado CFDI
  (`app/services/invoicing.py`) y el adapter de PAC permanecen en el código
  pero **no se ejercitan en el piloto**.

### Alternativas consideradas
- **Pospago con CFDI desde el día 1:** descartado para el piloto porque
  bloquea el arranque hasta seleccionar PAC, certificar CSD y validar
  timbrado. Demasiado riesgo para un primer cliente.
- **Pospago sin CFDI (factura informal):** descartado porque mezcla el flujo
  pospago a medias y no reduce el trabajo respecto a esperar al PAC.

### Consecuencias
- ✅ El piloto arranca sin depender de la selección de PAC ni de certificados
  fiscales.
- ✅ Riesgo de impago nulo: el cliente paga antes de consumir; al agotarse el
  saldo se bloquea el consumo (tarea de control de consumo del piloto).
- ✅ Reutiliza ADR-009 (el Medidor es la fuente única del costo de IA y ya
  debita el wallet en vivo).
- ⚠️ La facturación mensual + CFDI 4.0 queda **diferida**. Cuando se active
  pospago para clientes que lo requieran, se retoman las fases 4–5 y se
  selecciona el PAC (pendiente más abajo). Esta decisión no elimina ese
  código, solo no lo ejercita en el piloto.

---

## ADR-011: El Medidor core es la wallet prepago autoritativa (authorize/finish/credit) y mapeo de identidad del piloto Scraping

**Fecha:** 2026-06-03
**Estado:** Aprobado

### Contexto
Confirmado leyendo el código real del Medidor (no la descripción previa del
contrato del CAF, que estaba desactualizada): el Medidor (Nivel 1) ya implementa
prepago completo. Expone wallets con identidad `(tenant_id, external_user_id)`
UNIQUE, un par de operaciones `authorize → finish` para reservar y cobrar saldo,
`credit` para recargar, un ledger append-only (`wallet_transactions`), balance
materializado con locking optimista e idempotencia por `UNIQUE(wallet_id,
request_id)`. La autenticación es por API key con dos scopes: `CLIENT`
(`authorize`/`finish`/`track`) y `ADMIN` (`create`/`credit`/`suspend`).

El piloto del CAF es la app Nivel 3 **Scraping**, que **no tenía integración
con el Medidor**. Había que decidir (a) qué pieza es la autoridad del saldo y
del cobro, y (b) cómo se mapea la identidad del cliente entre CAF, Scraping y la
wallet del Medidor, sin lo cual el alta atómica y la recarga no pueden apuntar
a la wallet correcta.

### Decisión
**El Medidor core es la wallet prepago autoritativa.** El saldo, su validación
y su débito viven en el Medidor; ninguna otra pieza recalcula ni duplica saldo.
El reparto de responsabilidades por scope es:

- **CAF (scope `ADMIN`):** crea la wallet en el alta del cliente y la **acredita**
  (`credit`) cuando el Hub confirma una recarga. No consume ni cobra.
- **Scraping (scope `CLIENT`):** ejecuta `authorize → finish` por operación.
  `authorize` crea el HOLD y valida el saldo; **el bloqueo por saldo insuficiente
  lo impone `authorize` al rechazar**, no el CAF. `finish` captura el hold y
  descuenta el saldo.

**Mapeo de identidad (cross-core):**

```
CAF clients.id
   │  (FK lógica)
   ▼
Company.caf_client_id      (en la BD de Scraping)
   │
   ▼
Company.id  (= company_id)  ──►  wallet external_user_id
                                  bajo tenant_id = "inovaweb", proyecto "scraping"
```

Es decir, la wallet del cliente se identifica por `(tenant_id="inovaweb",
external_user_id=Company.id)`. El CAF guarda el `id` de wallet devuelto por el
Medidor en `clients.medidor_account_id`.

**Scraping se wirea al Medidor en TASK-21** (pre-check de saldo con `authorize`
y reporte de costo con `finish`). Esta decisión define el contrato; el cableado
del consumidor es trabajo aparte.

### Alternativas consideradas
- **CAF como autoridad del saldo (wallet local en la BD del CAF):** descartado.
  Duplicaría el saldo que el Medidor ya mantiene y debita en vivo (ADR-009),
  produciendo saldos contradictorios entre el wallet del Medidor y el del CAF.
- **`external_user_id = clients.id` del CAF directamente:** descartado. El
  consumidor que cobra es Scraping, que razona en términos de su propio
  `Company.id`; usar el id del CAF obligaría a Scraping a conocer la identidad
  interna del CAF en cada `authorize`. El mapeo indirecto vía
  `Company.caf_client_id` mantiene a cada sistema con su propia clave primaria.
- **Una wallet por proyecto en lugar de por cliente:** descartado; impide
  saldo y bloqueo por cliente individual, que es justo lo que el piloto necesita.

### Consecuencias
- ✅ Una sola autoridad de saldo y cobro: el Medidor. Cero divergencia de saldo.
- ✅ El bloqueo por saldo agotado es automático y vive donde debe (en
  `authorize` del Medidor), no en lógica frágil del CAF o de Scraping.
- ✅ Idempotencia garantizada en recarga (`credit`) y cobro (`finish`) por
  `UNIQUE(wallet_id, request_id)`; un webhook o reintento duplicado no
  duplica dinero.
- ✅ Cada sistema conserva su clave primaria; el mapeo es explícito y auditable.
- ⚠️ El CAF depende de que Scraping ejecute `authorize/finish` correctamente.
  Si Scraping consume sin `authorize`, no hay débito. Mitigación: el cableado
  de TASK-21 hace el `authorize` obligatorio antes de cada operación cobrable.
- ⚠️ El alta del cliente debe completarse (wallet creada + `caf_client_id`
  seteado en `Company`) antes de que Scraping pueda cobrar. Mitigación: la
  saga de onboarding (ADR-002) crea la wallet y el orden lo cubren las tareas
  de piloto.

### Cómo se ve en el CAF
- `app/core/clients/medidor_client.py` expone, con scope `ADMIN`, creación de
  wallet, `credit` por recarga confirmada y lecturas de balance/usage. **No
  expone `authorize`, `finish`, `release` ni `track`** — esas son del consumidor
  (scope `CLIENT`).
- El contrato real (endpoints, scopes, identidad, flujo prepago) está
  documentado en `docs/01-admin-financiera-integracion-cores.md` §3.

---

## ADR-012: Facturación CFDI 4.0 vía Ecofile (app propia) en lugar de PAC externo
**Fecha:** 2026-06-07
**Estado:** Aprobado (diferido a sprint 4)

### Contexto
El CAF necesita emitir CFDIs 4.0 timbrados para que las facturas sean fiscalmente válidas en México.
La decisión original (ADR-005) eligió un adaptador PAC intercambiable (Facturama/Factible/Edicom).
Inovaweb está desarrollando su propia aplicación de facturación electrónica llamada **Ecofile**
que implementa el ciclo completo CFDI 4.0 + timbrado + descarga PDF/XML.

### Decisión
El CAF consumirá Ecofile vía API HTTP (contrato interno Inovaweb) en lugar de integrar directamente
con un PAC externo. `pac_client.py` se reemplazará por `ecofile_client.py` cuando Ecofile exponga
su API. La lógica de `invoicing.py` permanece aislada detrás del adaptador (ADR-005 sigue vigente).

### Alternativas consideradas
- **PAC externo (Facturama, Factible, Edicom):** descartado como primera opción porque Ecofile
  brindará la misma funcionalidad con costo controlado y sin dependencia de terceros. Queda como
  fallback si Ecofile se retrasa.
- **Timbre manual (operador descarga XML, timbra en portal del PAC):** descartado; no escala y
  rompe el flujo automatizado de `invoice_retry`.

### Consecuencias
- ✅ Sin costo por timbre externo una vez Ecofile esté operativo.
- ✅ El CAF y Ecofile comparten el ecosistema Inovaweb; la integración es más directa.
- ⚠️ El contrato de API de Ecofile aún no está definido. `invoicing.py` usa placeholders hasta
  que se firme el contrato.
- ⚠️ La facturación CFDI queda diferida hasta sprint 4 (Ecofile debe estar listo primero).
- Las facturas en el MVP quedan en estado `draft` / `pending_stamp`; el operador ve el importe
  pero no puede entregar XML/PDF firmados al cliente hasta sprint 4.

---

## ADR-013: Tarificación a precio público vía `price_catalog`, no al costo COGS del Medidor
**Fecha:** 2026-06-07
**Estado:** Aprobado

### Contexto
El Medidor reporta el **costo crudo** de las operaciones de IA (lo que Inovaweb paga al proveedor LLM).
Este costo no puede facturarse directamente al cliente: incluye el margen de Inovaweb, varía por modelo
y no coincide con el precio de lista pactado en el contrato del cliente.

### Decisión
El CAF mantiene una tabla `price_catalog(meter, unit_code, amount_cents, valid_from, valid_to)`
con el **precio público** por unidad de consumo (IA/token, email, whatsapp, sms).
`billing.py` llama a `pricing.price_quantity()` para calcular el cargo a facturar;
el costo COGS del Medidor se usa únicamente para calcular margen interno, no se asienta
en `invoice_items`.

### Alternativas consideradas
- **Facturar el costo crudo del Medidor más un porcentaje de markup fijo:** descartado; el markup
  varía por cliente (plan, descuento), y el precio del LLM puede fluctuar sin aviso.
- **Precio fijo por "paquete" (ilimitado dentro del plan):** posible en planes futuros, pero el
  piloto requiere facturación por consumo real.

### Consecuencias
- ✅ El precio de lista es administrable desde la UI (`/admin/catalog`) sin tocar código.
- ✅ El margen queda implícito: `price_catalog.amount_cents - medidor.cost_cents` por unidad.
- ✅ Múltiples canales con precio distinto (email ≠ whatsapp ≠ token IA) en la misma tabla.
- ⚠️ Requiere mantener `price_catalog` actualizado cuando cambien los precios. El cierre mensual
  falla si no hay precio activo para una unidad consumida (degradación graceful: se omite el concepto,
  se loguea `pricing_missing`).

---

## ADR-014: Hardening H1-H5 — controles de resiliencia para el MVP prepago
**Fecha:** 2026-06-07
**Estado:** Aprobado

### Contexto
El flujo prepago (recarga → webhook → crédito Medidor) tiene cinco puntos de falla que pueden
producir: duplicación de saldo, cargos sin acreditación, acceso cruzado de clientes, o onboarding
parcial sin rollback. Se requieren controles mínimos antes de la primera recarga real.

### Decisión
Cinco controles (`H1`-`H5`) implementados en esta sesión:

| Control | Descripción | Archivo |
|---------|-------------|---------|
| H1 | Idempotencia onboarding: índice UNIQUE parcial en `clients(request_id)` | `database/006_idempotencia.sql` |
| H2 | Retry con backoff exponencial en `CoreClient` para 429 y 5xx | `app/core/clients/_base.py` |
| H3 | Fail-closed en `ENV=production`: falla abierta lanza excepción; dev/staging degrada | `app/services/onboarding.py` |
| H4 | Tope de recarga `MAX_RECARGA_CENTS` (default 50 M centavos = $500,000 MXN) | `app/core/config.py` |
| H5 | Webhook de pago valida `client_id` del JWT — no puede acreditar saldo ajeno | `app/routers/webhooks_router.py` |

### Alternativas consideradas
- **H1 alternativa — lock optimista con `version`:** más flexible para actualizaciones parciales,
  pero innecesario para onboarding que es un INSERT único.
- **H3 alternativa — mismo fail-closed en todos los ambientes:** descartado; en dev bloquearía la
  iteración sin que el developer tenga todos los cores corriendo localmente.

### Consecuencias
- ✅ Un webhook duplicado o un reintento de recarga no duplica saldo.
- ✅ Un onboarding parcial (fallo en step N) puede reintentarse con el mismo `request_id` sin
  crear un cliente duplicado.
- ✅ Un cliente no puede abusar del endpoint de pago para acreditar saldo de otro cliente.
- ⚠️ `MAX_RECARGA_CENTS` es un tope duro configurable; si un cliente legítimamente necesita más,
  el operador debe ajustar la variable de entorno o mover el tope.

---

## ADR-015: Saldo prepago NATIVO del CAF (`prepaid_ledger`); el Medidor queda como medidor puro

**Fecha:** 2026-06-11
**Estado:** Aprobado (supersede parcialmente ADR-011 para el saldo monetario)

### Contexto
ADR-009/010/011 pusieron el saldo prepago en el **Medidor** (wallet autoritativa con
`authorize/finish/credit`). Funciona para IA, pero acopla el saldo **monetario** del cliente a un core
cuya razón de ser es **medir consumo de IA**. Cuando el CAF empieza a cobrar servicios que NO son IA
(email, scraping, descubrimiento, validación de páginas — ver tarea de "contador cobrable por
proceso"), tener el saldo en el Medidor obliga a un ida-y-vuelta por cada cobro y mezcla dos conceptos
distintos: "cuánta IA consumiste" vs. "cuánto dinero te queda".

### Decisión
El **CAF** mantiene su propio libro prepago nativo: `prepaid_ledger` (append-only) + vista de saldo
`v_client_balance` (`migrations/030_prepaid_ledger.sql`). El **saldo monetario del cliente vive en el
CAF**. El Medidor vuelve a ser **medidor puro** (mide IA, lleva holds) y deja de ser la fuente del saldo.
Durante la transición, la recarga hace **dual-write** (acredita el `prepaid_ledger` del CAF *y* el
wallet del Medidor, idempotente por `req_id`) hasta retirar el saldo del Medidor.

Regla de negocio canonizada (ver `docs/MODELO-COBRO.md`): **"el Medidor mide, el CAF tarifica y cobra"**.

### Alternativas consideradas
- **Seguir con la wallet del Medidor como saldo (ADR-011):** descartado. Acopla el saldo monetario a un
  core de medición; cobrar servicios no-IA requería meter precios y débitos arbitrarios en el Medidor,
  desvirtuándolo.
- **Doble contabilidad sin fuente única:** descartado; garantiza divergencia de saldos.

### Consecuencias
- ✅ El CAF cobra **cualquier** servicio (IA, email, scraping, validación) contra un **saldo único**.
- ✅ `prepaid_ledger` es append-only con idempotencia (consistente con ADR-003).
- ⚠️ Dual-write transitorio con el Medidor (idempotente por `req_id`) hasta retirar el saldo del
  Medidor. La medición de IA del Medidor (ADR-009) **sigue vigente**: este ADR mueve el **saldo**, no la
  **medición**.
- ⚠️ ADR-010/011 (saldo en el Medidor) quedan **superseded** en lo que toca al saldo monetario.

---

## ADR-016: Cobro pay-per-use vía `POST /charge` (tarifica + valida + debita + 402), idempotente con advisory lock

**Fecha:** 2026-06-11
**Estado:** Aprobado

### Contexto
Las apps consumidoras (LiaForge, Swigg) necesitan cobrar **por operación** (una consulta SEO, un email,
un descubrimiento) de forma síncrona, a precio de catálogo y a prueba de doble-gasto.

### Decisión
`POST /api/v2/clients/{id}/charge {service_code, units, idempotency_key, meta}` (app-facing, Bearer):
tarifica con `services.unit_price_cents`, valida `v_client_balance`, debita `prepaid_ledger` y devuelve
**402 `saldo_insuficiente`** (`{balance_cents, required_cents}`) si no alcanza. **Idempotente** por
`(client_id, idempotency_key)` con replay; `pg_advisory_xact_lock(client_id)` serializa los cobros
concurrentes del mismo cliente.

### Alternativas consideradas
- **Enforcement por `plan-limits`** (contar en tablas de la app + tope del CAF): es para **límites de
  plan**, no para cobro monetario. Se mantiene en paralelo (`GET /plan-limits`), no reemplaza al cobro.
- **Holds del Medidor (`authorize/finish`):** apropiado para IA con costo incierto previo; excesivo para
  un servicio de datos con precio fijo conocido.

### Consecuencias
- ✅ Pay-per-use puro: una llamada cobra, con 402 claro para bloquear por saldo.
- ✅ Doble-gasto imposible (advisory lock + idempotencia).
- ⚠️ El precio vive en `services` del CAF (no en la app); cambiar precio = update de fila + auditoría.

---

## ADR-017: Onboarding app-facing self-service + modelo multi-app por Bearer (sin discriminador duro)

**Fecha:** 2026-06-11
**Estado:** Aprobado

### Contexto
Además del alta por operador (saga ADR-002, con JWT y datos fiscales), las apps necesitan dar de alta
clientes **self-service** desde su propio registro público, sin JWT ni datos fiscales completos. Y ya
hay más de una app consumidora (LiaForge/Scraping, Swigg, y futuras como ConductorPlay).

### Decisión
`POST /api/v2/apps/onboard {trade_name, billing_email, plan_code, external_ref}` autenticado por **Bearer
de app** (`_verify_app_key`): crea cliente + wallet Medidor + suscripción + grant del plan al
`prepaid_ledger`; datos fiscales **placeholder** (se completan al facturar). Cada app usa **su propio
Bearer** (`SCRAPING_ADMIN_KEY` = LiaForge, `SWIGG_ADMIN_KEY` = Swigg). **No** hay discriminador de app por
fila: los clientes se distinguen por `plan_code` (prefijo por app: `liaforge_*`, `swigg_*`) + `external_ref`.

### Alternativas consideradas
- **Solo alta por operador:** descartado; no escala para registro self-service.
- **Columna `app`/`tenant` por cliente con aislamiento duro:** descartado *por ahora*. El reporting por
  `product` (`GET /reports/income?group_by=product`) ya cubre la segmentación; se reconsiderará si se
  requiere aislamiento estricto entre apps.

### Consecuencias
- ✅ Self-service sin JWT ni datos fiscales; un formulario de registro da de alta y deja saldo de plan.
- ✅ Multi-app con llave por app (separación de credenciales).
- ⚠️ Sin aislamiento duro entre apps: la separación es por convención (`plan_code` + `external_ref`).
- ⚠️ Dar de alta una **app nueva** = sembrar sus `plans`/`services` (additive, seguro) + **append** de
  su llave en `_verify_app_key` (único cambio de código). Caso de uso vivo: alta de **ConductorPlay** como
  3ª app (planes `cp_*` sin grant por ser BYO-tokens).

---

## ADR-020: Multi-tenancy por `organization_id` (single-DB, scoping por query), no schema-por-tenant ni RLS

**Fecha:** 2026-06-16
**Estado:** Aprobado

### Contexto
El CAF deja de ser el motor de cobro de una sola empresa (Inovaweb) para
convertirse en un **SaaS multi-organización**: varias empresas-cliente
("organizations") usan el mismo motor de medición y cobro, cada una con su
propio catálogo, sus clientes y su saldo, sin verse entre sí. Antes de esta
sesión el código vivo no conocía el concepto de organización; todo era
implícitamente de Inovaweb. Había que introducir el tenant sin romper el
código en producción (LiaForge/Swigg ya cobrando) y decidir el modelo de
aislamiento.

### Decisión
**Tenant lógico por columna `organization_id` en una sola base de datos**, con
aislamiento aplicado a nivel de query en la app:

- Migración `031_organizations_tenancy.sql`: tabla `organizations`
  (`031_organizations_tenancy.sql:9`) + `organization_id BIGINT NOT NULL
  DEFAULT 1` agregado a **13 tablas** de primer nivel (clients, users,
  services, plans, products, promotions, api_keys, subscriptions, invoices,
  payments, adjustments, price_catalog, prepaid_ledger —
  `031_organizations_tenancy.sql:29-34`), cada una con índice por
  `organization_id` y FK a `organizations(id)`. El `DEFAULT 1` (org Inovaweb)
  es una **red de seguridad** para el código vivo que aún no setea la columna;
  se retira cuando el código siempre la provea desde el contexto
  (`031_organizations_tenancy.sql:2-5`).
- El tenant se resuelve **SIEMPRE de la credencial, nunca del body** (regla
  rectora 7):
  - **App-facing (Bearer de app):** `app/core/tenancy.py:resolve_app_org`
    (`app/core/tenancy.py:34`) busca el hash SHA-256 de la API key en
    `api_keys` (no revocada) → su `organization_id`; con **fallback legacy**
    a las llaves de `.env` (SCRAPING_ADMIN_KEY/SWIGG_ADMIN_KEY → org 1) para
    no romper a LiaForge/Swigg durante la migración (`app/core/tenancy.py:57-66`).
  - **Operador (JWT):** `CurrentUser` lleva `organization_id` (claim `oid`) y
    `is_platform` (super_admin de la org 1 Inovaweb), que ve y opera sobre
    **todas** las orgs vía `?org=<id>` (patrón repetido en
    `catalog_*_router`, `adjustments_router`, `client_account_router`,
    `email_providers_router`).
- **Aislamiento por query:** todo SELECT/INSERT acota por `organization_id`, y
  el cruce entre orgs se bloquea con `assert_client_in_org`
  (`app/core/tenancy.py:69`), que devuelve 404 si el `client_id` no pertenece
  a la org de la llave (impide que la org A opere sobre un cliente de la org B).

### Alternativas consideradas
- **Schema-por-tenant (un schema Postgres por organización):** descartado.
  Multiplica DDL y migraciones por N tenants, complica los reportes
  cross-org de la plataforma (`/orgs`, consumo agregado) y no aporta
  aislamiento real frente a un bug de la app que ya tiene la conexión.
- **Row-Level Security (RLS) de Postgres:** descartado por ahora. Habría que
  fijar un `SET app.current_org` por transacción en una capa async con pool
  compartido (frágil con SQLAlchemy async) y el beneficio sobre el scoping
  explícito por query es marginal para el tamaño actual. Queda como
  endurecimiento futuro si se quiere defensa en profundidad.

### Consecuencias
- ✅ Una sola BD, una sola imagen, un solo set de migraciones: el SaaS escala
  agregando filas, no schemas.
- ✅ El código vivo no se rompe: `DEFAULT 1` deja a Inovaweb funcionando
  mientras el resto del código adopta `organization_id`.
- ✅ El tenant nunca es spoofeable desde el cliente: sale de la llave (hash en
  `api_keys`) o del JWT, jamás del body.
- ⚠️ El aislamiento depende de que **cada query** filtre por
  `organization_id`. Una consulta que lo olvide fuga datos cross-tenant.
  Mitigación: helpers centralizados (`resolve_app_org`,
  `assert_client_in_org`, `_org_scope`) y revisión de seguridad obligatoria
  de cualquier router nuevo.
- ⚠️ El `DEFAULT 1` es transitorio; mientras exista, una fila insertada sin
  org explícita cae en Inovaweb por descuido. Se retira en cuanto el código
  garantice la columna.

---

## ADR-021: API keys self-service por organización (hash en `api_keys`), reemplazan el hardcode de llaves en `.env`

**Fecha:** 2026-06-16
**Estado:** Aprobado

### Contexto
Hasta esta sesión, cada app consumidora se autenticaba con una llave **cableada
en el `.env`** del CAF (`SCRAPING_ADMIN_KEY` = LiaForge, `SWIGG_ADMIN_KEY` =
Swigg — ver ADR-017). Dar de alta una app nueva exigía editar `.env` y
redesplegar, y todas las llaves vivían fuera de la BD, sin pertenencia a una
organización. Con el SaaS multi-org (ADR-020) cada organización necesita acuñar
y revocar sus propias llaves sin pasar por el operador ni por un deploy.

### Decisión
Cada organización **acuña sus propias API keys self-service** desde
`orgs_router` (`app/routers/orgs_router.py`):

- `POST /api/v2/orgs/{org_id}/api-keys` (`app/routers/orgs_router.py:181`):
  genera una llave `cafk_<token>`, guarda **solo su hash SHA-256** en
  `api_keys` (con `organization_id`, scope admin|readonly, `created_by_user_id`)
  y devuelve el texto plano **una sola vez** (`orgs_router.py:200-207`). La
  lectura (`GET .../api-keys`) jamás re-expone el plano.
- `POST /api/v2/api-keys/{key_id}/revoke` (`orgs_router.py:226`): revocación
  *soft* (marca `revoked_at`, nunca borra).
- Permisos: un `super_admin` administra **solo su org**; el operador de
  plataforma (`is_platform`) cualquiera (`_can_manage_org`,
  `orgs_router.py:43`).
- `resolve_app_org` (ADR-020) valida estas llaves por hash. El **fallback
  legacy** a `SCRAPING_ADMIN_KEY`/`SWIGG_ADMIN_KEY` se conserva como puente
  para LiaForge/Swigg, no como vía permanente.
- Para que el catálogo sea por-org, la migración `032_catalog_unique_por_org.sql`
  cambia el `UNIQUE(code)` global a **`UNIQUE(organization_id, code)`** en
  services/plans/products/promotions (`032_catalog_unique_por_org.sql:9-23`):
  dos orgs pueden tener su propio `code` 'email' sin colisión.

### Alternativas consideradas
- **Seguir con llaves en `.env`:** descartado. No escala (deploy por app
  nueva), no pertenece a una org, y mezcla secretos de tenants distintos en
  un solo archivo.
- **Guardar la llave en claro o reversible en BD:** descartado. Solo se
  persiste el hash; un dump de BD no revela llaves usables.
- **`UNIQUE(code)` global con prefijo por app (`liaforge_*`):** era el apaño de
  ADR-017; se reemplaza por `UNIQUE(organization_id, code)`, que es el modelo
  correcto multi-tenant y libera los nombres de código por org.

### Consecuencias
- ✅ Alta de app/credencial **sin deploy ni edición de `.env`**: la org acuña
  su llave por API.
- ✅ Cada llave pertenece a una org y es auditable (creador, último uso,
  revocación); revocar es inmediato y reversible-trazable (soft delete).
- ✅ Catálogo realmente por-org: nombres de `code` libres entre tenants.
- ⚠️ El texto plano se muestra **una sola vez**; si se pierde, hay que acuñar
  otra. Es intencional (no se puede recuperar de un hash).
- ⚠️ El fallback legacy sigue vivo: mientras LiaForge/Swigg no migren a llaves
  de BD, esas dos llaves de `.env` siguen siendo válidas para la org 1.

---

## ADR-022: Meta-cobro del SaaS — cada org es un `client` de la org plataforma, postpago a $0.99/tx + $99/mes

**Fecha:** 2026-06-16
**Estado:** Aprobado

### Contexto
El CAF ahora es un producto vendible a otras empresas (ADR-020). Hay que
**cobrar el uso del propio motor** a cada organización cliente: el SaaS debe
facturarse a sí mismo. La pregunta es dónde y cómo se contabiliza ese
meta-cobro sin inventar un segundo sistema de facturación paralelo y sin que el
cobro del SaaS se dispare a sí mismo en bucle.

### Decisión
**Cada organización cliente se representa como UN `client` de la organización
plataforma (org 1 Inovaweb)** y se le cobra con el mismo `prepaid_ledger` que
todo lo demás (`app/services/saas_billing.py`):

- Al crear una org (`POST /api/v2/orgs`), `register_org_as_platform_client`
  (`saas_billing.py:48`) inserta una fila `clients` en la **org 1** con datos
  fiscales placeholder (patrón app-onboard) y suscripción al plan `caf_saas`;
  el vínculo se guarda en `organizations.platform_client_id` (migración
  `035_org_platform_client.sql:12`). Idempotente por
  `clients.request_id = "saas-org-{org_id}"`.
- **Accrual por transacción ($0.99 = 99¢):** tras cada cobro real del motor,
  el endpoint `/charge` llama `accrue_transaction(org, "charge-{ledger_id}")`
  (`app/routers/api_router.py:386`). Es **post-charge, best-effort** (corre en
  su propia sesión, try/except que nunca propaga) e **idempotente** por
  `idempotency_key = "saas-tx-{source_ref}"` (`saas_billing.py:117-162`).
  **Sin recursión:** si `org == 1` (la plataforma) se omite
  (`saas_billing.py:126-127`) — el cobro del SaaS no debe disparar otro cobro
  del SaaS.
- **Cuota mensual ($99 = 9900¢):** `run_saas_monthly_billing`
  (`saas_billing.py:170`) acumula la cuota del plan `caf_saas` a cada org
  activa (cron mensual), idempotente por
  `"saas-fee-{platform_client_id}-{period}"`.
- Tarifas sembradas en el catálogo de la org 1: servicio `saas_transaccion`
  @ 99¢ y plan `caf_saas` @ 9900/mes (`034_seed_saas_tariff.sql`).
- **POSTPAGO:** los accrual son débitos puros que **pueden dejar saldo
  negativo** (la org debe; se liquida mensualmente). No se valida saldo, no se
  devuelve 402, y **no** se usa el endpoint `/charge` (se inserta directo en
  `prepaid_ledger`) para evitar la recursión (`saas_billing.py:13-16`).

### Alternativas consideradas
- **Un módulo de facturación separado para el SaaS:** descartado. Duplicaría
  ledger, idempotencia y reportes; reusar `prepaid_ledger` + el modelo
  `client` da consistencia (mismas invariantes append-only de ADR-003).
- **Prepago para el meta-cobro (bloquear org sin saldo):** descartado. Cortar
  el motor a una empresa-cliente por saldo es desproporcionado; el SaaS es
  postpago con liquidación mensual.
- **Cobrar dentro del propio `/charge` (sin sesión aparte):** descartado.
  Acoplaría el cobro del cliente final al meta-cobro; si el accrual fallara
  rompería el cobro real. Por eso es best-effort en sesión propia y nunca
  propaga.

### Consecuencias
- ✅ El SaaS se factura con la misma maquinaria (ledger append-only,
  idempotencia, reportes) que cualquier cobro: una sola fuente de verdad.
- ✅ Doble accrual imposible (idempotencia por tx y por periodo).
- ✅ Sin bucle: la org 1 se excluye del accrual.
- ⚠️ Best-effort: si el accrual falla, se loguea pero **no** se reintenta en
  línea; el cobro real del cliente no se ve afectado, pero un fallo silencioso
  sub-factura el SaaS. Mitigación: revisión periódica de logs
  `saas_accrue_transaction_failed` (futuro: reconciliación tx vs accrual).
- ⚠️ Saldo negativo permitido: una org morosa acumula deuda hasta el cierre
  mensual. Es intencional (postpago), pero exige cobranza fuera de banda.

---

## ADR-023: Credenciales de terceros cifradas en reposo con AES-256-GCM (proveedores de email por org/cliente)

**Fecha:** 2026-06-16
**Estado:** Aprobado

### Contexto
El SaaS multi-org necesita que cada organización (y opcionalmente cada cliente)
configure su **propio remitente de email** (Microsoft 365, Gmail, SMTP). Eso
implica guardar secretos de terceros (app-password, refresh_token,
client_secret, password SMTP) en la BD del CAF. Guardarlos en claro es
inaceptable; el modelo de seguridad del CAF ya exige AES-256-GCM para sellos
CFDI y secretos de PAC (mismo patrón con que el Hub de Pasarelas cifra las
credenciales de pasarela por tenant).

### Decisión
Los secretos de proveedores de email se guardan **cifrados con AES-256-GCM**
(AEAD: confidencialidad + integridad) vía `app/core/crypto.py`:

- `encrypt_secret`/`decrypt_secret` (`app/core/crypto.py:65`,
  `app/core/crypto.py:82`) producen un token `"v1." +
  base64url(nonce[12] || ciphertext_con_tag)`.
- **Nonce aleatorio de 12 bytes por cada cifrado** (`os.urandom`), jamás
  reusado (reusar nonce en GCM rompe confidencialidad e integridad —
  `app/core/crypto.py:21-23`, `crypto.py:76`).
- La **versión del esquema** (`v1`) se liga criptográficamente como
  **AAD** (`_AAD = b"caf-secret-v1"`, `crypto.py:37`), impidiendo
  confusión/downgrade entre esquemas futuros.
- La llave se deriva de `AES_KEY` del `.env` (SecretStr base64), validada en
  cada uso a **exactamente 32 bytes** (AES-256, fail-fast con ValueError —
  `crypto.py:45-62`). No se usa Fernet (es AES-128-CBC+HMAC, no cumple el
  spec — `crypto.py:5-6`).
- La tabla `email_providers` (`033_email_providers.sql:11`) guarda el secreto
  **solo** en `secret_encrypted` (`033_email_providers.sql:22`); el router
  `email_providers_router` cifra al escribir y **nunca devuelve el secreto en
  claro ni en logs** — las lecturas solo exponen `secret_set`
  (`app/routers/email_providers_router.py:13-15`). La config puede ser a nivel
  org (`client_id NULL`) o a nivel cliente (pisa a la de la org).

### Alternativas consideradas
- **Guardar el secreto en claro / ofuscado:** descartado. Un dump de BD o un
  log lo expondría; AEAD da confidencialidad e integridad verificable.
- **Fernet (`cryptography`):** descartado. Es AES-128-CBC+HMAC; el spec del
  campo `AES_KEY` exige GCM (AES-256) (`crypto.py:5-6`).
- **KMS / Vault externo:** descartado por overhead operativo para el tamaño
  actual; `AES_KEY` en `.env` del VPS es suficiente y consistente con el
  resto de secretos del CAF. Migrar a KMS queda como endurecimiento futuro.

### Consecuencias
- ✅ Secretos de email en reposo cifrados y autenticados; manipulación
  detectable (InvalidTag → ValueError).
- ✅ Mismo helper reutilizable para cualquier secreto de tercero (consistente
  con sellos CFDI / PAC).
- ✅ El prefijo de versión permite rotar el esquema sin ambiguar tokens viejos.
- ⚠️ La seguridad colapsa a la custodia de `AES_KEY`: si se filtra, todos los
  secretos son descifrables. Mitigación: `AES_KEY` solo en `.env` del VPS,
  nunca en git; rotación = re-cifrar con nuevo `v2.` (soportado por el diseño).

---

## ADR-024: Selección dinámica de pasarela desde el panel del CAF (default del Hub), con fail-safe a `HUB_GATEWAY`

**Fecha:** 2026-06-16
**Estado:** Aprobado

### Contexto
El Hub de Pasarelas soporta varias pasarelas (Conekta, Stripe, …) por tenant.
Hasta ahora el CAF cobraba con una pasarela fija de `.env`. Se quiere que el
**operador elija la pasarela activa desde el panel del CAF** (y administre las
credenciales de pasarela del tenant) sin tocar SQL ni redesplegar, y que el
cambio surta efecto en el siguiente cobro.

### Decisión
La pasarela con la que cobra el CAF es la **default activa del tenant en el
Hub**, elegida por el operador en el panel del CAF:

- El CAF administra la config de pasarela del tenant **en el Hub** vía
  `HubAdminClient` (scope `admin:gateways`, `app/core/clients/hub_client.py:86`):
  `save_gateway` guarda credenciales + flags (`hub_client.py:99`),
  `set_default` fija la pasarela default sin re-enviar credenciales
  (`hub_client.py:113`), `default_gateway` lee el slug default activo
  (`hub_client.py:120`). **El Hub cifra y persiste las credenciales; el CAF
  nunca las guarda ni las repite en sus respuestas** (`hub_client.py:87-89`).
- Al iniciar un cobro, `prepago._resolve_gateway`
  (`app/services/prepago.py:664`) lee la default del Hub para
  `HUB_COMPANY_ID`; **fail-safe**: ante cualquier fallo (o si no hay
  `HUB_ADMIN_KEY`) cae a `HUB_GATEWAY` del `.env` y **nunca bloquea el cobro**
  (`prepago.py:664-680`, invocado en `prepago.py:166-168`).

### Alternativas consideradas
- **Pasarela fija en `.env` (estado previo):** descartado. Cambiarla exige
  editar `.env` y redesplegar; no es operable por el equipo financiero.
- **Que el CAF persista y cifre las credenciales de pasarela:** descartado.
  El cifrado y la custodia de credenciales de pasarela son responsabilidad
  del Hub (separación de responsabilidades); el CAF solo las **manda** al Hub
  y lee el default.
- **Resolver la pasarela en cada request del cliente final:** innecesario; la
  default por tenant cubre el caso y el fail-safe evita acoplar el cobro a la
  disponibilidad del endpoint admin del Hub.

### Consecuencias
- ✅ El operador cambia de pasarela desde el panel; el siguiente cobro la usa.
- ✅ El CAF administra credenciales de pasarela del tenant sin SQL y sin
  custodiarlas (las cifra el Hub).
- ✅ Resolver la pasarela nunca tira un cobro: fail-safe a `HUB_GATEWAY`.
- ⚠️ **Límite:** el front de `/registro` es de la **app dueña**, no del CAF; el
  CAF expone la administración de pasarela y la resolución, pero la captura del
  alta vive en la app cliente.
- ⚠️ Si el default del Hub queda mal configurado, el fail-safe cobra con
  `HUB_GATEWAY` (puede no ser la deseada). Mitigación: el panel muestra la
  default vigente; verificar tras cambiarla.

---

## ADR-025: Promociones por distribuidor — código con % de descuento aplicado como bono de crédito al contratar

**Fecha:** 2026-06-16
**Estado:** Aprobado

### Contexto
Inovaweb capta clientes a través de **distribuidores**. Cada distribuidor
reparte un **código de promoción** que debe traducirse en un descuento al
contratar (self-service). Hay que decidir quién valida el código, dónde se
aplica el descuento y cómo se evita el doble conteo ante reintentos del alta.

### Decisión
Un **código de promoción se asocia a un distribuidor y lleva un `discount_pct`**;
el CAF lo valida y lo aplica como **bono de crédito** sobre el grant del plan, al
contratar:

- Migración `036_distributors.sql`: tabla `distributors`
  (`036_distributors.sql:9`, por ahora solo `name`) + columna
  `promotions.distributor_id` y `promotions.discount_pct` (referida en
  `036_distributors.sql:20-21`; el `%` vive en la promo). Cada promo →
  distribuidor + % de descuento.
- El **código viaja en el alta self-service** `POST /api/v2/apps/onboard`
  (campo `promo_code`, `app/routers/api_router.py:401`): **la app dueña lo
  manda**; el CAF solo valida y aplica (`api_router.py:452-455`).
- Validación (`api_router.py:460-480`): la promo debe ser de la **org de la
  llave**, activa, vigente (`now() BETWEEN valid_from AND valid_to`), con
  `discount_pct` no nulo y dentro de `max_uses`. El **uso se cuenta de forma
  atómica** (`UPDATE ... WHERE uses_count < max_uses RETURNING id`,
  `api_router.py:470-473`) para respetar el tope ante carreras.
- El bono = `round(granted * discount_pct / 100)` se suma al grant del plan y
  se asienta en `prepaid_ledger` con `source='grant_plan'` e
  **idempotencia** por `idempotency_key = "grant-{client_id}-{plan_code}"`
  (`api_router.py:440`, `482-492`): un reintento del alta **no** duplica grant
  ni bono ni uso (si el grant ya existe, retorna temprano —
  `api_router.py:444-450`).

### Alternativas consideradas
- **Que la app dueña calcule y aplique el descuento:** descartado. Abre la
  puerta a fraude y duplica la lógica de promociones en cada app; **el CAF
  define y aplica**, la app solo pide el código.
- **Descuento como rebaja del precio de catálogo en vez de bono de crédito:**
  descartado para el alta; el bono de crédito al `prepaid_ledger` es directo,
  auditable y consistente con el modelo prepago (el cliente recibe más saldo).
- **Conteo de uso no atómico (leer-luego-incrementar):** descartado; permitiría
  exceder `max_uses` bajo concurrencia. Se usa `UPDATE ... RETURNING`.

### Consecuencias
- ✅ El descuento por distribuidor se aplica una sola vez, atómicamente, como
  saldo extra; trazable en el ledger (meta con `promo_code`, `distributor_id`,
  `discount_pct`, `bonus_cents`).
- ✅ `max_uses` se respeta bajo concurrencia; los reintentos del alta son
  idempotentes (no doble bono ni doble conteo).
- ✅ Atribución a distribuidor disponible para reportes (vía `distributor_id`).
- ⚠️ El conteo de uso se incrementa **antes** de asentar el bono; si el INSERT
  del ledger fallara tras contar el uso, se "gastaría" un uso sin acreditar el
  bono. Riesgo bajo (misma transacción); a vigilar.
- ⚠️ De `distributors` solo se captura el nombre por ahora
  (`036_distributors.sql:13` deja `external_ref` para el futuro); la
  liquidación de comisiones al distribuidor es trabajo posterior.

---

## Pendientes de ADR (placeholder)

- **ADR-018: Backups y RPO/RTO del CAF** — `[TODO: completar]`. Necesita
  decisión sobre destino (S3 / Backblaze / OneDrive corporativo) y
  frecuencia.
- **ADR-019: 2FA para super-admin** — mencionado en CLAUDE.md y SECURITY.md
  como requisito; tecnología concreta (TOTP / WebAuthn / push) `[TODO:
  completar]`.

---

## ADR-026: Columna `app_slug` en `services` — identificación de producto por servicio cobrable

**Fecha:** 2026-06-17
**Estado:** Aprobado

### Contexto
La tabla `services` contenía servicios de tres productos distintos (LiaForge, Swigg, CAF) sin ninguna columna que los distinguiera. Los reportes de consumo y los paneles de administración no podían filtrar ni agrupar por producto, lo que dificultaba la operación multi-app.

### Decisión
Añadir columna `app_slug TEXT` a `services` (migración `037_services_app_slug.sql`) y hacer backfill con los valores conocidos: `liaforge` para los servicios de scraping/email/IA, `swigg` para video, `caf` para la transacción SaaS. Filas futuras sin app conocida quedan con `NULL` (visible como `—` en UI).

### Alternativas consideradas
- **Columna en `prepaid_ledger`**: copiaría el slug en cada débito → redundancia, riesgo de inconsistencia. Descartado.
- **Tabla de relación `service_apps`**: más flexible pero innecesaria para el caso de uso actual (un servicio pertenece a una app). Descartado por complejidad.

### Consecuencias
- El endpoint de reportes puede filtrar por `s.app_slug` sin JOIN adicional.
- El panel de Servicios y Planes muestra la columna "App" directamente.
- Nuevos servicios deben tener `app_slug` establecido al insertar (convención, no constraint).

---

## ADR-027: Reportes de consumo sobre `prepaid_ledger` con filtros dinámicos en el panel admin

**Fecha:** 2026-06-17
**Estado:** Aprobado

### Contexto
El dashboard existente mostraba solo el consumo del mes actual agregado por core (cuatro barras, sin desglose). Los operadores necesitaban poder analizar el consumo por período arbitrario, producto (app) y core, con detalle por cliente y servicio.

### Decisión
Añadir al panel admin la página `GET /admin/reports/consumption` (Jinja2 + Chart.js) y un endpoint JSON `GET /admin/reports/consumption/data` que consulta `prepaid_ledger JOIN services` con tres filtros combinados: rango de fechas (desde/hasta), multiselección de `app_slug` y multiselección de `source_core`. La respuesta incluye métricas agregadas + top 5 servicios + filas de detalle (límite 500). Los filtros se construyen dinámicamente con placeholders nominales para evitar inyección SQL.

### Alternativas consideradas
- **OLAP / warehouse externo**: demasiado complejo para el volumen actual (< 10 000 filas/mes). Descartado.
- **Consulta directa al Medidor**: el Medidor no tiene visibilidad de `app_slug` ni del nombre del cliente CAF. Descartado.
- **Exportar CSV y analizar offline**: sin interactividad en tiempo real. Descartado como solución principal (puede añadirse como complemento).

### Consecuencias
- Los reportes reflejan el libro `prepaid_ledger` (fuente de verdad del CAF), coherente con los saldos y el cierre mensual.
- El filtro de app requiere `app_slug` poblado en `services` (dependencia de ADR-026).
- El límite de 500 filas por consulta es suficiente para el MVP; paginación o exportación CSV pendiente.

---

## ADR-028: Alta self-service de servicios y planes por tenant
**Fecha:** 2026-06-18
**Estado:** Aprobado

### Contexto
Los tenants que usan el CAF como motor SaaS multi-tenant no tenían UI para crear sus propios servicios y planes. El flujo previo requería acceso SQL directo al servidor, lo que bloqueaba la autonomía operativa de cada organización.

### Decisión
Agregar `POST /admin/catalog/services` y `POST /admin/catalog/plans` con formularios Jinja2 inline en la misma página del catálogo existente. El `organization_id` se fija desde la sesión del usuario autenticado y nunca proviene del body del request. Validaciones aplicadas al insertar:

- `source_core` validado contra enum fijo `{medidor, hub, messages, finanzas, internal}`.
- `unit_price_mxn` convertido a centavos enteros al persistir.
- Flash message vía redirect GET (patrón PRG): `?saved=ok | error_dup | error_core | error_precio`.

### Alternativas consideradas
- **API JSON separada**: dos endpoints REST independientes en lugar de form + página integrada. Descartada porque duplica superficie sin beneficio para el caso de uso MVP.
- **Modal HTMX**: componente dinámico sin recarga de página. Descartado por complejidad innecesaria en esta fase; el PRG cubre todos los casos de error/éxito con código mínimo.

### Consecuencias
- Cada organización puede gestionar su propio catálogo de servicios y planes sin intervención de DBA.
- El `organization_id` nunca viaja desde el cliente, eliminando el vector de escape de tenant por body tampering.
- El enum `source_core` debe mantenerse sincronizado con los sistemas reales de la plataforma.

---

## ADR-029: Promociones de plataforma aplicables a todos los tenants
**Fecha:** 2026-06-18
**Estado:** Aprobado

### Contexto
Las promociones de tipo `referral` creadas por Inovaweb (org 1) no podían ser usadas por tenants hijos porque el query de validación filtraba `WHERE organization_id = :org`. Un tenant con su propia org key nunca encontraba la promo de plataforma, haciendo inoperante el programa de referidos.

### Decisión
Cambiar el filtro de validación de promociones en `api_router.py` a:

```sql
WHERE organization_id IN (:org, 1)
ORDER BY organization_id DESC
LIMIT 1
```

El `ORDER BY organization_id DESC` garantiza que, si existe un código con el mismo nombre tanto en org 1 como en la org del tenant, se usa la versión tenant-específica.

### Alternativas consideradas
- **Copiar la promo a cada org al crearla**: proliferación de filas, riesgo de inconsistencia. Descartada.
- **Flag `is_global` en la tabla `promotions`**: sobreingeniería para el caso de uso actual. Descartada.

### Consecuencias
- Inovaweb puede crear códigos referral/distribuidor en org 1 que aplican automáticamente a toda la plataforma.
- Si un tenant crea un código con el mismo nombre, su versión tiene precedencia (comportamiento explícito por el `DESC`).

---

## ADR-030: Metodología de auditoría de independencia de tenants
**Fecha:** 2026-06-18
**Estado:** Aprobado

### Contexto
Al agregar soporte multi-tenant completo al CAF, era necesario verificar de forma sistemática que ningún tenant pudiera leer ni modificar datos de otro, tanto a nivel de SQL como a través de HTTP real con credenciales válidas de una org distinta.

### Decisión
Protocolo de auditoría en dos capas obligatorias:

1. **SQL ground-truth**: `SELECT count(*) FROM <tabla> WHERE organization_id = :nueva_org` sobre las 15 tablas con `organization_id` para confirmar aislamiento en base de datos.
2. **HTTP con JWT real**: autenticar con credenciales de la nueva org y ejercer todos los endpoints del panel y la API; comparar respuestas con las de org 1 para verificar que no hay fuga cruzada.

Hallazgo crítico detectado por este protocolo: `/admin/reports/consumption/data` calculaba el filtro `oc` de `_org_scope()` pero nunca lo añadía al string de conditions (`append()` omitido). El bug era silencioso. Corregido en commit `6faaeb5`.

### Alternativas consideradas
- **Solo tests automatizados**: insuficiente para lógica de filtros dinámica por concatenación de strings; el bug de `append()` silencioso habría pasado asserts de cobertura de ramas.
- **Solo revisión de código estática**: el bug requería trazar la variable `oc` a través de varias líneas de construcción de query; la ejecución HTTP real lo expuso en segundos.

### Consecuencias
- El protocolo de dos capas debe ejecutarse ante cada nueva tabla o endpoint que maneje datos con `organization_id`.
- Se mantiene `org5` (acmecorp) como tenant de prueba permanente para estos checks; no debe usarse para datos de producción.
- Los bugs de filtros dinámicos por concatenación de strings son la clase de error más probable en este patrón; considerar migrar a query builders en endpoints de reporte.

---

## ADR-031: Módulo de comisiones de referidos de distribuidores

**Fecha:** 2026-06-19
**Estado:** Aprobado

### Contexto
Inovaweb opera con distribuidores que reclutan clientes para los productos SaaS (LiaForge, Swigg, etc.). Cuando un distribuidor comparte su código y el cliente lo usa al registrarse, el distribuidor debe recibir una comisión económica en dinero real (no créditos). Se necesitaba un módulo que rastreara el vínculo distribuidor→cliente y acumulara la comisión en la primera recarga confirmada del cliente, dejando registro para pago manual posterior.

### Decisión
- **Nuevo campo `referral_code` en `distributors`** (UNIQUE case-insensitive) + `commission_pct NUMERIC(5,2)`.
- **Nuevo campo `referral_distributor_id` en `clients`** (FK a `distributors`): se puebla en el onboard si el campo `referral_code` viene en `POST /api/v2/apps/onboard`.
- **Tabla `distributor_commissions`** append-only: una fila por (distribuidor, pago); nunca se modifica, solo se añade `status='paid'` via `UPDATE` restringido al rol `_WRITE`.
- **Accrual en primera recarga únicamente**: `_maybe_accrue_commission` en `prepago.py` verifica `COUNT(payments WHERE client_id) == 1` antes de insertar la comisión.
- **Best-effort**: el accrual está envuelto en `try/except`; si falla, el pago al cliente ya está confirmado y se registra un `log.warning` para revisión manual.
- **Idempotencia**: `UNIQUE INDEX uq_distributor_commissions_txn ON (distributor_id, payment_hub_txn)` con `ON CONFLICT DO NOTHING`.
- **Pago manual**: Conrado realiza la transferencia bancaria y marca la comisión como pagada desde `/admin/distributors/{id}` con nota de referencia de la transferencia.

### Alternativas consideradas
- **Comisión en créditos al distribuidor (no dinero)**: no aplica, los distribuidores son personas externas sin cuenta en el sistema.
- **Comisión en todas las recargas, no solo la primera**: aumenta la complejidad sin acuerdo comercial con todos los distribuidores; la primera recarga como trigger es el acuerdo inicial.
- **Pago automático vía Hub al distribuidor**: requiere que los distribuidores tengan cuenta bancaria registrada en el sistema (CLABE/SPEI); fuera de alcance en MVP.

### Consecuencias
- El flujo de pago principal (Hub → CAF → créditos al cliente) no se ve afectado si el accrual de comisión falla.
- El estado `pending` es la única fuente de verdad de lo que se debe pagar; no hay conciliación automática.
- Si un cliente se registra sin código de referido, `referral_distributor_id` queda NULL y no se genera comisión — correcto.
- No hay beneficio para el cliente: el código es del distribuidor, no un cupón de descuento (ver ADR-025/029 para el flujo de promos con bono al cliente).
