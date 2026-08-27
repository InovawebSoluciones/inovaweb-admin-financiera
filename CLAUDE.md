# inovaweb-admin-financiera

Centro de Administracion Financiera (CAF) - Nivel 2 de la plataforma Inovaweb.
Convierte la infraestructura tecnica de los 4 cores Nivel 1 en un producto
comercial completo: incorporacion automatica de clientes, catalogos de planes
y precios, cobranza automatica con factura electronica fiscal, portal del
cliente, tableros directivos y operacion 100% sin SQL manual.

**Estado:** en construccion (sprint 1, scaffolding inicial).

---

## 1. Arquitectura Inovaweb (3 niveles)

```
NIVEL 1 - APIs core (infraestructura)
├─ medidor             (operativo)  medidor.inovaweb.com.mx
├─ hub-pasarelas       (operativo)  hub.inovaweb.com.mx
├─ finanzas-core       (operativo)  finanzas.inovaweb.com.mx
└─ centro-mensajes     (operativo)  mensajes.inovaweb.com.mx

NIVEL 2 - servicios
└─ admin-financiera    (ESTE PROYECTO)
                       admin.inovaweb.com.mx   <- operador interno
                       app.inovaweb.com.mx     <- portal cliente externo

NIVEL 3 - apps cliente
├─ WebEscolar          (single-tenant, ERP escolar)
├─ MicroFichas         (multi-tenant, video IA)
├─ Scraping            (n8n)
└─ Ecofile             (factura electronica, planeado)
```

**Que SI hace este modulo:**
- Incorporacion atomica de clientes: alta en 1 formulario que ejecuta INSERT
  coordinado en los 4 cores Nivel 1 + emite 4 API keys + asigna plan inicial.
- Catalogo administrable de productos, servicios cobrables, planes y precios.
- CRUD de clientes (alta, baja, suspension, reactivacion) con auditoria.
- Cobranza automatica mensual: cierre nocturno, calculo del consumo,
  aplicacion de plan + descuentos + promociones, emision de factura.
- Facturacion electronica CFDI 4.0 via PAC certificado (Facturama / Soluc.
  Factible / Edicom).
- Sistema completo de promociones: cupones, descuentos por temporada,
  descuentos por volumen, referidos, planes Free con limites.
- Recargas asistidas y autoservicio (cliente paga via hub-pasarelas).
- Portal del cliente: saldo, consumo, facturas, recargas autonomas.
- Tablero interno consolidado: ingresos por periodo/producto/cliente/concepto.
- Notificaciones al cliente via centro-mensajes.
- Auditoria inmutable de cada operacion financiera (actor + timestamp + diff).

**Que NO hace (fuera de alcance):**
- ERP completo (cuentas por pagar, nomina, activos fijos). Solo ingresos.
- ML / analytics avanzado de churn.
- App movil nativa (web responsive cubre).
- Conciliacion bancaria automatica.
- CRM completo de pipeline de ventas. Solo cliente ya cerrado.
- Multi-moneda real (todo MXN; USD opcional fase posterior).
- Multi-idioma (solo espanol).
- White-label (branding Inovaweb fijo).

---

## 2. Stack tecnico

- **Python 3.12** + **FastAPI** + **uvicorn**
- **SQLAlchemy 2 async** + **psycopg 3 binary**
- **PostgreSQL 16** (auto-contenido en docker-compose)
- **httpx async** para llamadas a los 4 cores Nivel 1 y al PAC
- **Jinja2 + HTMX + Tailwind CSS** para UI server-side (sin SPA, sin build Node)
- **JWT + bcrypt/Argon2** para auth con usuario/password (NO solo API keys)
- **cryptography** para AES-256-GCM (sellos CFDI, secretos de PAC)
- **Docker** + Nginx (TLS — reemplazó a Caddy; verificar config real en VPS antes de documentar)
- **VPS Contabo** 89.116.25.222, puerto host 8006 (los 8000-8005 ocupados)

**Diferencia clave respecto a los 4 cores Nivel 1:** este modulo TIENE UI
gráfica y autenticacion con humanos. Los cores son API-only.

---

## 3. Endpoints previstos

### Publicos (sin auth)
- `GET  /health`              liveness
- `GET  /health/db`           readiness
- `GET  /docs` `/redoc` `/openapi.json` (solo dev; ocultos en prod)
- `GET  /login`               pagina de login (HTML)
- `POST /login`               procesa login, emite session cookie
- `GET  /signup-request`      formulario publico de solicitud de acceso

### Operador interno (auth JWT con rol admin/finanzas/lectura)
**UI HTML server-side (Jinja2 + HTMX):**
- `GET  /admin/dashboard`              tablero ingresos consolidado
- `GET  /admin/clients`                listado clientes
- `GET  /admin/clients/{id}`           detalle cliente
- `POST /admin/clients`                alta atomica (4 cores)
- `PATCH /admin/clients/{id}`          editar
- `POST /admin/clients/{id}/suspend`   suspender por mora
- `GET  /admin/catalog/products`       CRUD productos
- `GET  /admin/catalog/services`       CRUD servicios cobrables
- `GET  /admin/catalog/plans`          CRUD planes
- `GET  /admin/catalog/promotions`     CRUD promociones
- `GET  /admin/billing/invoices`       listado facturas
- `POST /admin/billing/run-closing`    forzar cierre mensual
- `GET  /admin/audit-log`              audit log inmutable

### Cliente externo (auth JWT con rol client)
**Portal en app.inovaweb.com.mx:**
- `GET  /portal/dashboard`             saldo + consumo del mes
- `GET  /portal/usage`                 historial detallado
- `GET  /portal/invoices`              mis facturas
- `GET  /portal/invoices/{id}.pdf`     descarga PDF
- `GET  /portal/invoices/{id}.xml`     descarga CFDI XML
- `POST /portal/recharge`              inicia flujo Hub-Pasarelas
- `GET  /portal/account`               datos comerciales propios

### API JSON (autenticada con JWT o API key admin del CAF)
- `POST /api/v2/clients`               alta programatica (mismo que UI)
- `GET  /api/v2/clients/{id}/balance`  saldo consolidado
- `GET  /api/v2/reports/income`        agregados de ingreso
- `POST /api/v2/billing/run-closing`   trigger manual de cierre

### Webhooks
- `POST /webhooks/pac`                 eventos del PAC (timbrado exitoso, fallo)
- `POST /webhooks/hub-payment-paid`    confirma recarga via hub-pasarelas

---

## 4. Convenciones firmes

- **Centavos enteros BIGINT.** Nunca floats.
- **Append-only en operaciones financieras:** invoices, payments, adjustments,
  audit_log NUNCA se modifican; correcciones generan nuevas entradas (notas
  de credito, ajustes con motivo).
- **Auditoria obligatoria:** cada accion de escritura registra actor_user_id,
  IP, timestamp, valor_anterior, valor_nuevo. Triggers en BD lo enforcen.
- **Multi-rol estricto:** super-admin, finanzas, lectura, cliente-titular,
  cliente-usuario. Cada endpoint declara su rol minimo requerido.
- **2 dominios:** admin.inovaweb.com.mx (operador) + app.inovaweb.com.mx
  (cliente). Mismo backend, distinto routing por host header.
- **Onboarding atomico:** patron Saga. Si falla la creacion en uno de los
  4 cores, se revierten los anteriores y se registra la falla.
- **CFDI 4.0:** todas las facturas se timbran via PAC en cuanto se emiten.
  Cola interna con reintento si el PAC esta caido (mismo patron que
  ledger_retry del centro-mensajes).
- **Passwords:** Argon2id para hashing. NUNCA almacenar plaintext.
- **Sesiones JWT:** httpOnly cookie, SameSite=Strict, 15 min de access token
  + 30 dias de refresh token con rotacion.

---

## 5. Estructura prevista

```
inovaweb-admin-financiera/
├── app/
│   ├── main.py                       FastAPI + wiring
│   ├── core/
│   │   ├── config.py                 pydantic-settings
│   │   ├── database.py               AsyncEngine + get_db
│   │   ├── jwt_auth.py               JWT + cookies + login flow
│   │   ├── password.py               Argon2 hashing
│   │   ├── audit.py                  audit log writer
│   │   ├── observability.py          JSON logging + request-id
│   │   └── clients/                  HTTP clients a los 4 cores Nivel 1
│   │       ├── medidor_client.py
│   │       ├── hub_client.py
│   │       ├── messages_client.py
│   │       ├── finanzas_client.py
│   │       └── pac_client.py         cliente al PAC (Facturama, etc.)
│   ├── routers/
│   │   ├── health_router.py
│   │   ├── auth_router.py            /login /logout /signup-request
│   │   ├── admin_router.py           /admin/*  (UI operador)
│   │   ├── portal_router.py          /portal/* (UI cliente)
│   │   ├── api_router.py             /api/v2/* (JSON)
│   │   └── webhooks_router.py        /webhooks/{pac|hub-payment-paid}
│   ├── services/
│   │   ├── onboarding.py             Saga de alta atomica cross-core
│   │   ├── billing.py                cierre mensual + calculo de cargos
│   │   ├── promotions.py             aplicacion de descuentos/cupones
│   │   └── invoicing.py              emision de factura + timbrado PAC
│   ├── workers/
│   │   ├── monthly_closing.py        job nocturno del 1 de cada mes
│   │   ├── invoice_retry.py          reintento de timbrado fallido
│   │   └── overdue_notifier.py       recordatorios de vencimiento
│   └── templates/                    Jinja2 templates (UI HTMX)
│       ├── base.html
│       ├── admin/
│       └── portal/
├── database/
│   ├── 001_initial_schema.sql        users, roles, clients, products,
│   │                                 services, plans, subscriptions,
│   │                                 promotions, invoices, payments,
│   │                                 adjustments, audit_log
│   └── 002_security_constraints.sql  triggers append-only + auditoria
├── static/                           CSS Tailwind compilado + JS minimo
├── tests/
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
├── Caddyfile (referencia)
├── .env.example
├── .gitignore
├── CLAUDE.md
└── SECURITY.md
```

---

## 6. Variables de entorno previstas

| variable                | obligatoria | default                |
|-------------------------|-------------|------------------------|
| DATABASE_URL            | si          | -                      |
| AES_KEY                 | si          | -                      |
| JWT_SECRET              | si          | -                      |
| POSTGRES_USER           | no          | caf                    |
| POSTGRES_PASSWORD       | si          | -                      |
| POSTGRES_DB             | no          | admin_financiera       |
| PORT                    | no          | 8001 (en contenedor)   |
| ENV                     | si          | -                      |
| LOG_LEVEL               | no          | INFO                   |
| MEDIDOR_BASE_URL        | si          | https://medidor.inovaweb.com.mx |
| MEDIDOR_API_KEY         | si          | (admin key)            |
| HUB_BASE_URL            | si          | https://hub.inovaweb.com.mx |
| HUB_API_KEY             | si          | (admin key)            |
| MESSAGES_BASE_URL       | si          | https://mensajes.inovaweb.com.mx |
| MESSAGES_API_KEY        | si          | (admin master)         |
| FINANZAS_BASE_URL       | si          | https://finanzas.inovaweb.com.mx |
| FINANZAS_API_KEY        | si          | (admin master)         |
| PAC_PROVIDER            | si          | facturama              |
| PAC_API_KEY             | si          | -                      |
| PAC_API_SECRET          | si          | -                      |
| RFC_EMISOR              | si          | (RFC de Inovaweb)      |
| CER_PATH                | si          | /secrets/csd.cer       |
| KEY_PATH                | si          | /secrets/csd.key       |
| KEY_PASSWORD            | si          | -                      |
| ADMIN_DOMAIN            | no          | admin.inovaweb.com.mx  |
| PORTAL_DOMAIN           | no          | app.inovaweb.com.mx    |

---

## 7. Despliegue

### VPS Contabo (puerto host 8006)
```
cd /opt/inovaweb-admin-financiera
git pull
docker compose up -d --build
```

Nginx (reemplazó a Caddy) enruta **dos dominios distintos al MISMO backend**:
- `https://admin.inovaweb.com.mx` -> `admin_financiera:8001` (operador)
- `https://app.inovaweb.com.mx` -> `admin_financiera:8001` (cliente)

El propio backend distingue por `Host` header y aplica el routing correcto
(routers admin vs portal).

**OJO:** el `Caddyfile` en la raiz del repo es solo referencia historica.
La config real del reverse proxy esta en el VPS bajo Nginx. Claude Code debe
leer la config real del VPS antes de documentar el deploy.

---

## 8. Documentos clave

- `docs/inovaweb-admin-financiera-proyecto-tecnico.md` - documento tecnico
  completo del proyecto, con diagramas mermaid y secciones para stakeholders.
- `docs/01-admin-financiera-integracion-cores.md` - contrato de integracion
  con los 4 cores Nivel 1 (medidor, hub, finanzas, mensajes).
- `SECURITY.md` - modelo de amenazas y controles.
- Repositorio GitHub (planeado): https://github.com/InovawebSoluciones/inovaweb-admin-financiera

---

## 9. Roadmap por fases (aprobado por direccion)

| Fase | Que se entrega | Tiempo |
|---|---|---|
| 1 | Backend + onboarding atomico + catalogos (sin UI). | 3 sem |
| 2 | UI interna operativa para equipo financiero. | 2 sem |
| 3 | Portal del cliente externo con recarga via hub-pasarelas. | 2 sem |
| 4 | Cierre mensual automatico + factura CFDI 4.0 via PAC. | 3-4 sem |
| 5 | Promociones avanzadas + reportes ejecutivos. | 1-2 sem |

Decision directiva: aprobar fases 1+2+3 como bloque (7 semanas = operacion
end-to-end). Fase 4 (CFDI) diferida hasta seleccion de PAC. Fase 5
pospuesta hasta demanda real.

---

## 10. Pendientes diferidos

- Conciliacion bancaria automatica (manual en MVP).
- Multi-moneda real (USD).
- Multi-idioma.
- White-label / marca por cliente.
- Aplicacion movil nativa.
- Analytics predictivo / ML de churn.

---

## 11. Modelo de trabajo (roles)

- **VPM** (asistente principal): planea, escribe specs en `.vpm/tasks/`, maneja el
  taskbar, acepta/rechaza entregables. NO escribe codigo de produccion.
- **Ejecutor** (subagente / Claude Code): implementa segun el spec.
- **QA, Ciberseguridad, Documentador** (subagentes): revisan cada entrega.
- Flujo por tarea: spec -> ejecutor -> QA + seguridad -> documentador -> VPM acepta.
- Instrucciones .txt/.md para el ejecutor: guardarlas en `.vpm/tasks/`, no pegar
  prompts largos en el chat.

---

## 12. ESTADO ACTUAL Y PENDIENTES (actualizar al cerrar cada sesion)

**Sesion al: 2026-08-27. Foco: módulo de distribuidores — códigos de vendedor (mig 040) + operación completa (mig 041) + 4 fixes de seguridad/dinero + docs formales.**

### ✅ Sesion 2026-08-27 (2) — Operación completa del módulo de distribuidores (ADR-033, commit 77616da + cierre)
Se cerraron los 12 huecos que hacían inoperable el módulo sin bajar a SQL:
- **Editar y desactivar** distribuidores y códigos de vendedor. Inactivo = deja de captar clientes, pero **conserva y permite pagar** sus comisiones devengadas.
- **Cambiar el % NO reescribe comisiones ya devengadas** (cada fila guarda su propio `commission_pct`).
- **Liquidación masiva** de comisiones con una sola referencia de transferencia.
- **Lista de clientes referidos** (antes solo el número), buscador (nombre/código/vendedor) + paginación, export CSV, tarjeta de comisiones pendientes en el dashboard, `external_ref` en uso, badges de estado.
- **Migración 041:** triggers de auditoría en las 3 tablas del módulo (**ninguna los tenía**, incumpliendo CLAUDE.md §4) + `updated_at` en `distributor_codes`.

**🔴 4 hallazgos de seguridad/dinero, todos corregidos y desplegados:**
1. `toggle_distributor_code` sin chequeo de `organization_id` (IDOR cross-tenant).
2. `mark_commission_paid` con el **mismo** hueco. **Tercera aparición del patrón** (la 1ª fue `6faaeb5`). Causa raíz: el chequeo se copiaba a mano en cada endpoint → resuelto con `_assert_distributor_in_org()`, que ahora usan **todas** las mutaciones del módulo.
3. **Liquidación masiva confirmaba un monto menor al que liquidaba** (total sumado sobre las 200 filas listadas, pero el endpoint paga todas las pendientes).
4. **Inyección de fórmulas en el CSV** vía `clients.trade_name`, que viene del alta self-service (input externo).

**Refutado en la revisión (no se cambió nada):** se sospechó que el `except` sin `rollback()` rompería los mensajes de error; probado contra Postgres real, SQLAlchemy lo maneja y el `commit()` de `get_db` no falla.

**🔴 DEUDA ABIERTA:** auditar el CRUD de servicios/productos que se desplegó directo en el VPS sin pasar por git (detectado en la sesión anterior). Sigue sin revisar.

**Lección de proceso:** el `code-review` se corrió DESPUÉS del primer deploy y por eso el IDOR llegó a producción. En este proyecto va **antes** de desplegar.

### ✅ Sesion 2026-08-27 — Códigos de vendedor por distribuidor (ADR-032, commit 1c85d35)
- **Migración 040:** tabla `distributor_codes` — códigos hijos de un distribuidor (uno por vendedor), sin tabla de comisión propia. La comisión sigue siendo 100% por `distributors.commission_pct`; explícitamente NO se calcula por vendedor (pedido así por Conrado).
- **`api_router.py`:** `POST /apps/onboard` resuelve `referral_code` en cascada — primero `distributors.referral_code`, si no matchea entonces `distributor_codes.code` (activo) → mismo `distributor_id`.
- **Panel `/admin/distributors/{id}`:** sección "Códigos de vendedor" (alta + activar/desactivar). Sin portal de autoservicio para el distribuidor — pedido explícitamente fuera de esta iteración ("no, se hace por la noche... no vamos a hacer portal").
- **🔴 Hallazgo de seguridad (encontrado y corregido el mismo día):** `toggle_distributor_code` salió a producción sin chequeo de `organization_id` — IDOR cross-tenant, mismo patrón que `6faaeb5`. Detectado en `code-review` posterior al primer deploy, corregido y redesplegado.
- **Sincronizado desde el VPS:** mientras se trabajaba esta sesión, alguien desplegó directo en el servidor (sin git) un CRUD de servicios/productos/precios (`edit_service`, `toggle_service`, `create_product`, `edit_product`). Se trajo al repo para no perderlo — **queda pendiente auditar ese código con `code-review`**, no se revisó en esta sesión (no era el foco).
- **Validación:** 2 corridas contra Postgres desechable (esquema sintético + volcado `--schema-only` real de prod) antes de cada uno de los 2 despliegues.
- **Docs:** ADR-032 + CHANGELOG 0.11.0 + addendum OWASP 2026-08-27.
- **Fuera de alcance (a pedido explícito):** portal de autoservicio del distribuidor, trazabilidad por vendedor (`referral_sales_rep_id`), comisión por vendedor.
- **Aparte, en esta sesión:** se reseteó la contraseña del admin `conrado.torres@inovaweb.com.mx` en prod (Argon2, generado y verificado dentro del contenedor real) — no es código, no quedó en git a propósito.

**Anterior — Sesion al: 2026-06-19. Foco: módulo de referidos de distribuidores (mig 038 + accrual + panel admin) + docs formales.**

> **ACTUALIZACION 2026-06-08 (sesion LiaForge) — leer esto primero:**
> - **P2 (consumo E2E) HECHO y EN PROD:** IA Perplexity via proxy nuevo del Medidor (/llm/perplexity, mide+debita wallet client-5; PROXY_DEFAULT_WALLETS factura al cliente) + email via endpoint nuevo Centro POST /v1/messages/record (Scraping send_email lo reporta) -> sync CAF->Finanzas (source=medidor/messages) por cron */5. Verificado: $500 -> $497.69. Key DeepSeek corregida.
> - **Rebrand LiaForge:** paleta indigo/ambar + logo + Space Grotesk en la app de Scraping y en el front del CAF (admin+portal). Landing publica https://liaforge.inovaweb.com.mx (+ /producto /precios /casos /contacto /registro), HTTPS. CTAs -> /registro y WhatsApp +522226184898 / ventas@inovaweb.com.mx.
> - **Estimador de campana** (costo/creditos/costo-por-lead) en la Bandeja de Scraping.
> - **TODO pusheado a GitHub** (medidor_ia da08b0c, centro 71631ad, CAF 147042f, scraping 10da5a5). Respaldos en VPS /root/liaforge_prompts_backup_20260608/.
> - **NUEVA TAREA PRIORITARIA:** agregar un **CONTADOR/medicion COBRABLE por proceso** -> (a) scraping de sitios, (b) envio de email, (c) descubrimiento IA, (d) validacion de pagina web. Pipeline: medir en Medidor -> tarificar en CAF (price_catalog: nuevos meter/unit_code) -> asentar en Finanzas (igual que IA y mensajeria ya operativas).
> - **PENDIENTE de usuario:** correo remitente de validacion de cuenta (para cablear el alta real de /registro y /contacto).


> ⚠️ **CORRECCION DE DESFASE:** una version previa de esta §12 ("v3 — traslada formal")
> decia que commit/push/deploy estaban "PENDIENTE USUARIO" y que se corrio el `traslada`
> (`inovaweb-documentacion`). **ESO ERA FALSO.** Realidad verificada en prod (2026-06-08):
> TODO esta commiteado, pusheado y **DESPLEGADO**, y el flujo de pago corre **E2E**. El
> `traslada` formal **NO se ha corrido** -> los docs formales estan ATRASADOS (ver abajo).
> **Fuente de verdad = esta §12.**

Modelo: cliente paga -> Hub (pasarela) cobra -> webhook -> CAF acredita wallet Medidor +
asienta Finanzas -> consumo IA/mensajes descuenta -> cuadre. Identidad:
CAF clients.id <-> external_user_id "client-{id}" en cada core.

### ✅ EN PROD, VERIFICADO (no pendiente)
- **Saldo prepago NATIVO del CAF (jun 9-11):** prepaid_ledger + v_client_balance; POST /clients/{id}/charge (pay-per-use, 402 saldo_insuficiente, idempotente+advisory lock), /prepaid-balance, /ledger, /services, /clients/{id}/plan-limits; POST /apps/onboard (self-service Bearer por app: LiaForge=SCRAPING_ADMIN_KEY, Swigg=SWIGG_ADMIN_KEY). El Medidor solo mide (ADR-015/016/017). Detalle vivo en memoria project_caf_auditoria_global.md.
- **Flujo de pago E2E funcionando:** CAF /portal/recharge -> Hub /hub/v1/charge (gateway
  mock async=pending) -> webhook paid -> Hub notifica al CAF (D2, HMAC) -> CAF acredita
  Medidor + asienta Finanzas. Verificado: saldo $500, CAF payments $500, Finanzas
  source=hub credit 50000. Idempotente.
- **Onboarding real:** cliente Inovaweb id=5, wallet `25116fe8-8b3e-4ca9-8415-a81c13fd061b`,
  company Scraping 'inovaweb' (bb21b463) ligada con caf_client_id=5.
- **4 claves de cores acuñadas + cableadas** en .env del VPS del CAF (Medidor admin/HMAC,
  Hub payments:write, Centro *, Finanzas ledger:write). Verificadas vs cores vivos.
  Valores SOLO en .env VPS. Admin CAF: conrado.torres@inovaweb.com.mx (clave fuera de git).
- **D2 resuelto+desplegado** (Hub notifica al CAF, no acredita directo; Centro no
  auto-reporta a Finanzas; CAF = unico contador). **D1 resuelto** (link-caf + BIGINT).
- **Tarificacion viva:** 007_price_catalog.sql + pricing.py + billing.py + docs/MODELO-COBRO.md.
- **Migraciones CAF 001-007** aplicadas a BD viva.
- **Scraping corregido (2026-06-08):** alembic reparado (creado `0001_base` -> cadena
  resuelve -> `stamp head` = **0005**), `medidor_client.py` (prepago) en VPS, endpoints
  link-caf + /usage + /email-usage vivos, esquema caf_client_id BIGINT. Backend healthy.

### Estado de repos / METODOS DE DEPLOY (para no repetir errores)
- **CAF:** /opt/inovaweb-admin-financiera en el VPS **SI es git** -> deploy = `git pull` +
  rebuild. Todo commiteado+pusheado+desplegado.
- **Scraping:** /root/scraping-universidades en el VPS **NO es git y NO tiene acceso a
  GitHub** (su llave SSH es deploy key de *microfichas*, no de scraping-inovaweb). Deploy =
  **scp desde el repo local** (NO `git pull`). Codigo alineado a origin/main (`761484b`).
  app/ esta bind-mounted (--reload); alembic NO (requiere rebuild para hornear).
- **Hub:** cambios D2 commiteados **SOLO en el repo del VPS**; push a GitHub PENDIENTE.
- **Centro:** D2 pusheado. **Medidor/Finanzas:** sin cambios de codigo (solo api_keys).

### ✅ Sesion 2026-06-16 — Motor SaaS multi-tenant (commits 8960434, d9c5334, 1063ced en CAF; 1619dae en Hub)
- **Multi-tenancy:** migraciones 031-036 aplicadas. `organizations` table + `organization_id` en 13 tablas. org 1 = plataforma Inovaweb (ve todo). Cada org tiene su catálogo, llaves, consumo y admin delegado.
- **API keys self-service:** orgs acuñan sus propias llaves via `/api/v2/auth/api-keys` (scope system). ADR-021.
- **Meta-cobro SaaS:** cada org es `client` de org 1; $0.99/tx + $99/mes (ADR-022). Cron `meta_billing` registra.
- **AES-256-GCM para email_providers:** secretos cifrados en reposo por org/cliente (ADR-023).
- **Pasarelas dinámicas:** CAF lee default gateway del Hub, front `/admin/payment-gateways` para configurar sk_/whsec_ sin SQL (ADR-024). HubAdminClient en hub_client.py.
- **Distribuidores+promociones:** tabla `distributors`, códigos de descuento como bono de crédito (ADR-025).
- **Stripe en Hub (commit 1619dae):** Checkout Session, firma HMAC `Stripe-Signature`, fail-closed. Activo en catálogo (`active_count=4`).
- **Hub admin endpoint (commit 1063ced):** `POST /admin/hub/v1/gateway-config` (scope `admin:gateways`), cifra creds con AES + upsert.
- **ADR-020/025** añadidos al docs/ADR.md.
- **OWASP 2026-06-16:** PASS CON OBSERVACIONES (CSRF SameSite mitiga, JWT TTL 12h ventana ampliada, audit actor_user_id=1 en self-service onboard).

### ✅ Sesion 2026-06-17 — app_slug + Stripe E2E + reportes de consumo (commits b12a719, b8dcfba en CAF; 5d53a50 en Hub)
- **Migración 037:** `app_slug TEXT` en tabla `services`; backfill: liaforge/swigg/caf por `code`. Aplica al identificar a qué producto pertenece cada servicio cobrable.
- **Templates admin:** columna "App" en `/admin/catalog/services` y `/admin/catalog/plans`.
- **`POST /api/v2/clients/{id}/recharge` (commit d7b12c1):** endpoint app-facing Bearer. Reusa `initiate_charge`. LiaForge lo consume para recargar saldo de sus clientes programáticamente.
- **Stripe E2E verificado:** llaves test (sk_test_/whsec_YEDt) cargadas via front pasarelas. Webhook `we_1TjAXWIz` registrado en Stripe test dashboard. Flujo: LiaForge → CAF /recharge → Hub cs_test_ → 4242 → webhook → CAF acredita. Saldo 4 080→9 080 cr ✅.
- **Hub fix `success_url` (commit 5d53a50):** gateway buscaba solo `success_url`; CAF manda `return_url`. Fix: `metadata.get('success_url') or metadata.get('return_url') or fallback`.
- **Reportes de consumo:** `GET /admin/reports/consumption` (página Jinja2 + Chart.js) + `GET /admin/reports/consumption/data` (JSON). Query sobre `prepaid_ledger JOIN services` con filtros: fecha desde/hasta, multiselect App y Core. Gráficas: donut por app, barras top 5 servicios. Tabla detalle: cliente/app/core/servicio/uds/fecha/monto. Fix: `clients.name` → `COALESCE(c.trade_name, c.legal_name)`.
- **Tarea pendiente #1:** webhook Stripe LIVE antes de salir a producción.
- **Tarea pendiente #2:** "Error al cargar datos" en reportes — investigar 500 residual.

### ✅ Sesion 2026-06-19 (2) — Catálogo LiaForge: imagen_ia + precio validacion_pagina (commit pendiente)
- **`imagen_ia` (nuevo):** $12 MXN / imagen, `source_core=internal`, `unit=imagen`, `app_slug=liaforge`. Débito desde módulo Brief con idempotency `imagen:{image_id}`. Ya en BD (aplicado por LiaForge).
- **`validacion_pagina`:** precio bajó 50→30 cents ($0.50→$0.30). Ya en BD.
- **`validacion_email` — nuevo flujo Enriquecer:** mismo servicio ($0.18), idempotency `valemail:enriquecer:{session_id}`. Solo cambia quién llama; no hay cambio en el CAF.
- **Sin nuevos endpoints ni migraciones estructurales:** todo usa `/charge` existente. Migración `039` = solo datos, idempotente.
- **Decisión pendiente negocio:** ¿`validacion_email` ($0.18) o nuevo `validacion_dns` ($0.05) para flujo Enriquecer?

### ✅ Sesion 2026-06-19 — Módulo de referidos de distribuidores (commit 6fa7043)
- **Migración 038:** `referral_code` + `commission_pct` en `distributors`; `referral_distributor_id FK` en `clients`; tabla append-only `distributor_commissions` (idempotente por `UNIQUE(distributor_id, payment_hub_txn)`).
- **`api_router.py`:** `AppOnboardBody.referral_code` opcional; lógica que vincula el cliente al distribuidor tras el onboard.
- **`prepago.py`:** `_maybe_accrue_commission` — best-effort, solo en primera recarga (`COUNT(payments)==1`), `ON CONFLICT DO NOTHING`.
- **Panel admin distribuidores:** `GET/POST /admin/distributors`, `GET /admin/distributors/{id}`, `POST .../commissions/{cid}/pay`. Templates `distributors.html` + `distributor_detail.html`. Menú "Referidos" en `_layout.html`.
- **Norma Silva (id=3):** `NYM`, `25%` configurada en BD.
- **Docs:** ADR-031 + CHANGELOG v0.10.0 + OWASP addendum 2026-06-19. ADR-028/029/030 (generados en sesión anterior pero nunca llegaron al VPS) incluidos en este commit.
- **Commit:** `6fa7043` (VPS == GitHub).

### ✅ Sesion 2026-06-18 — Gráfica por core + formularios catálogo + fix seguridad + promo NYM (commits af45cfe, 6faaeb5, 82d758d, 35e63b7, d5853b3)
- **Gráfica pastel por core:** Chart.js pie añadida a `/admin/reports/consumption` con colores por core. `by_core` en response JSON.
- **Formularios alta servicios/planes:** `POST /admin/catalog/services` y `POST /admin/catalog/plans` con org isolation, validación enum, PRG flash.
- **CRÍTICO FIX (6faaeb5):** aislamiento multi-tenant en `/reports/consumption/data` — `oc` calculado pero nunca añadido al WHERE. Verificado con org5 (acmecorp).
- **Fix promo plataforma (d5853b3):** `WHERE organization_id IN (:org, 1)` — promos de org 1 aplican a todos los tenants.
- **Backfill NYM:** bonos retroactivos aplicados a clientes 20 y 21 (+62 500 cr c/u, saldo final 312 500 cr).
- **ADR-028/029/030** añadidos. OWASP addendum 2026-06-18.

### Estado de repos (2026-06-19)
- **CAF:** VPS == GitHub (6fa7043). Deploy = `git pull` + `docker compose up -d --build`.
- **Hub:** VPS == GitHub (5d53a50/1619dae). Deploy = `docker compose up -d --build hub`.
- **Scraping/LiaForge:** deploy = scp. No git pull en VPS. (auth.py sin cambio — referral_code pendiente de aplicar por equipo LiaForge).
- **Centro/Medidor/Finanzas:** sin cambios de código.

### Pendientes (orden)
| # | Pendiente | Owner |
|---|---|---|
| P1 | Endpoint `POST /v1/messages/record` en Centro + nodo n8n | diferido |
| P3 | Proveedor de email en Centro (Resend o SMTP M365) | usuario (credencial) |
| P4 | Push del Hub a GitHub | pendiente |
| P5 | Deploy key `scraping-inovaweb` en VPS | usuario (GitHub) |
| T1 | Webhook Stripe LIVE antes de producción | pendiente |
| T2 | Fix "Error al cargar datos" reportes (resuelto: alias + GROUP BY; cerrar) | ✅ RESUELTO |
| LF | Agregar campo `referral_code` en form registro LiaForge + enviar al CAF (instrucciones dadas) | equipo LiaForge |
| 1 | DNS/TLS admin/app.inovaweb.com.mx | usuario |
| F | CFDI 4.0 via Ecofile | diferido |

GIT: VPS usa SSH (deploy keys por-repo); Windows HTTPS. pytest diferido a Docker/VPS.
OJO: claves/credenciales reales NUNCA en CLAUDE.md (se commitea); viven en .env del VPS.

---

## 13. ARRANQUE DE SESION (leer al iniciar cada chat de este proyecto)

NO explorar carpetas al arrancar. Leer este CLAUDE.md + la memoria del proyecto
y responder con:
  0. PROYECTO: inovaweb-admin-financiera (CAF) — sesion YYYY-MM-DD
  1. Tabla de pendientes con estado (de la seccion 12, max 8 filas)
  2. Una linea con el siguiente paso inmediato
  3. Que le toca al usuario (VPS) vs que ejecuto yo
  4. ¿Listo para continuar?

PALABRAS CLAVE:
  siguiente = todo bien, dame el siguiente paso y ejecutalo
  duda      = tengo una pregunta antes de continuar
  error     = algo salio mal, ayudame
  pausa     = recuerdame donde quedamos
  traslada  = cierre formal de sesion (ver PROTOCOLO abajo)

PROTOCOLO "traslada" — ejecutar en este orden exacto, sin saltarse pasos:
  (1) Actualizar seccion 12 con el estado real de la sesion.
  (2) Actualizar la fecha de la sesion en el encabezado de la seccion 12.
  (3) Actualizar la memoria persistente del proyecto (project_caf_*.md).
  (4) Revision de codigo con el skill `engineering:code-review`:
      - Correctitud funcional (logica de negocio: saga, prepago, webhooks).
      - Documentacion a nivel de codigo: docstrings, comentarios inline,
        type hints. Cualquier funcion publica sin docstring es un hallazgo.
  (5) Ejecutar el skill `inovaweb-documentacion` para regenerar:
      - Docs tecnicos formales: README, ADR, RUNBOOK, DEPLOY, CHANGELOG.
      - Auditoria OWASP (seguridad). Bloquea commit si hay hallazgo critico.
      - Docs para desarrollador: guia de onboarding tecnico, contratos de
        integracion actualizados, diagramas de flujo en texto.
      NOTA: este skill menciona HISTORIAL_SESIONES.md — en este proyecto
      ese rol lo cumple la seccion 12 del CLAUDE.md; usarla como fuente.
  (6) Generar o actualizar docs de alto nivel y manual de usuario:
      - `docs/GUIA-USUARIO-OPERADOR.md`: manual para el equipo financiero
        interno (como dar de alta clientes, forzar cierre, ver audit log).
        Nivel: usuario de negocio, sin conocimiento tecnico.
      - `docs/GUIA-USUARIO-CLIENTE.md`: manual para el cliente final
        (como entrar al portal, recargar, descargar facturas, ver consumo).
        Nivel: usuario final, lenguaje simple.
      - `docs/RESUMEN-EJECUTIVO.md`: una pagina para direccion/stakeholders
        (que hace el CAF, estado actual, metricas clave, proximos pasos).
        Sin tecnicismos. Maximo 1 pagina.
  (7) Dar el comando de commit/push listo para copiar.
  (8) Cerrar sesion. No seguir avanzando en implementacion.
