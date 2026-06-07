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

**Sesion al: 2026-06-07 (v2). Foco: CABLEAR CORES + FLUJO DE PAGO FUNCIONANDO E2E.**

Modelo: cliente paga -> Hub (pasarela) cobra -> webhook -> CAF acredita saldo en
wallet del Medidor -> consumo IA/mensajes descuenta -> cuadre en Finanzas.
Identidad: CAF clients.id <-> external_user_id "client-{id}" en cada core.

### ✅ FUNCIONA EN PROD (verificado E2E esta sesion)
- **Flujo de pago completo, sin saltarse cores:** CAF /portal/recharge -> Hub
  /hub/v1/charge (gateway mock async=pending) -> webhook paid -> Hub notifica al
  CAF (D2, HMAC) -> CAF acredita Medidor + asienta Finanzas. Verificado:
  **saldo Medidor $500**, **payments CAF $500 (hub_card)**, **Finanzas
  source=hub credit 50000 (1 asiento)**. Idempotente.
- **Onboarding real:** cliente Inovaweb **id=5**, wallet Medidor
  `25116fe8-8b3e-4ca9-8415-a81c13fd061b`, link a company Scraping `inovaweb`
  (`bb21b463-...`, caf_client_id=5).
- **4 claves de cores ACUÑADAS y cableadas en .env del CAF** (estaban CHANGE_ME):
  Medidor(admin/HMAC), Hub(payments:write), Centro(*), Finanzas(ledger:write).
  Verificadas vs cada core vivo. VALORES: en .env del VPS / gestor de secretos
  (NO en git). Admin CAF: usuario conrado.torres@inovaweb.com.mx (clave fuera de git).
- **D2 corregido y desplegado:** Hub notifica al CAF (no acredita Medidor directo,
  flag CAF_WEBHOOK_URL + secreto compartido HUB_WEBHOOK_SECRET); Centro ya NO
  auto-reporta a Finanzas (REPORT_TO_FINANZAS=false). **El CAF es el unico contador.**
- **Tarificacion (rating) viva:** `007_price_catalog.sql` (IA/token + mensajes por
  canal + costo/margen, micro-pesos), `pricing.py`, `billing.py` factura a PRECIO
  PUBLICO y asienta consumo (source=invoice debit) en Finanzas. Doc: `docs/MODELO-COBRO.md`.
- **Migraciones aplicadas a BD viva CAF:** 003-007. Scraping: esquema corregido
  por DDL directo (caf_client_id BIGINT + medidor_wallet_id) — alembic quedo en
  0001 (cadena rota en imagen), deuda.

### Estado de repos
- CAF: todo commiteado+pusheado a GitHub (varios commits, ultimo ~7746b37).
- Centro: pusheado (6ebb0bc, D2). Scraping: pusheado (08882da, link-caf+usage+BIGINT).
- **Hub: cambios D2 commiteados SOLO en repo del VPS (/opt/inovaweb-hub-pasarelas),
  push a GitHub fallo por credenciales del remoto -> PENDIENTE pushear.**

### Pendientes (orden)
| # | Pendiente | Estado |
|---|---|---|
| P1 | **Endpoint `POST /v1/messages/record` en Centro + nodo n8n** (registrar envio email/whatsapp/sms con destinatario+sent_at+client_id; tenant via key; idempotente por source_ref). Opcion B. | DISEÑADO, no implementado (diferido por usuario) |
| P2 | **Smoke real del CONSUMO** (cuanto se cobro): IA real (Scraping authorize/finish) + mensajes -> CAF tarifica -> debito Finanzas -> saldo baja -> cuadre = pagado-cobrado | pendiente |
| P3 | **Proveedor de email en Centro** (`tenant_channel_credentials` VACIO -> sin entrega real). Resend o SMTP M365. Necesario para correos reales y para Opcion A. | pendiente usuario (credencial) |
| P4 | Push del Hub a GitHub (hoy solo en VPS) | pendiente |
| P5 | Limpieza: 1-2 tx 'paid' huerfanas en Hub (de depuracion), backups .bak-* en VPS | menor |
| 1 | DNS/TLS admin/app.inovaweb.com.mx | pendiente usuario |
| F | CFDI 4.0 via Ecofile | diferido |

GIT: VPS usa SSH; Windows HTTPS con credential cache. pytest diferido a Docker/VPS.
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
