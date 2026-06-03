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

## Pendientes de ADR (placeholder)

- **ADR-009: Selección concreta de PAC** — diferida hasta sprint 4. Decisión
  entre Facturama, Solución Factible, Edicom. Variables a comparar: precio
  por timbre, SLA, soporte en español, calidad de la API.
- **ADR-010: Backups y RPO/RTO del CAF** — `[TODO: completar]`. Necesita
  decisión sobre destino (S3 / Backblaze / OneDrive corporativo) y
  frecuencia.
- **ADR-011: 2FA para super-admin** — mencionado en CLAUDE.md y SECURITY.md
  como requisito; tecnología concreta (TOTP / WebAuthn / push) `[TODO:
  completar]`.
