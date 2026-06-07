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

## Pendientes de ADR (placeholder)

- **ADR-015: Backups y RPO/RTO del CAF** — `[TODO: completar]`. Necesita
  decisión sobre destino (S3 / Backblaze / OneDrive corporativo) y
  frecuencia.
- **ADR-016: 2FA para super-admin** — mencionado en CLAUDE.md y SECURITY.md
  como requisito; tecnología concreta (TOTP / WebAuthn / push) `[TODO:
  completar]`.
