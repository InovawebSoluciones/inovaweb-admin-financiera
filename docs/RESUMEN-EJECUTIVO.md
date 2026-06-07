# Resumen Ejecutivo — Centro de Administración Financiera (CAF)

**Fecha:** 2026-06-07 · **Audiencia:** dirección y stakeholders · **1 página**

## Qué es

El CAF convierte la infraestructura técnica de Inovaweb (los 4 cores: medición de
consumo IA, pagos, contabilidad y notificaciones) en un **producto comercial
operable**: alta de clientes, cobranza, portal del cliente y tableros directivos,
sin necesidad de tocar bases de datos manualmente.

## Estado actual (2026-06-07)

- **Flujo de pago verificado E2E en producción:** recarga → cobro en pasarela (Hub) → notificación webhook → acreditación de saldo (Medidor) → asiento contable (Finanzas). Idempotente: un pago duplicado no duplica saldo.
- **Onboarding completo:** alta de cliente en un formulario que activa los 4 cores + envía email de activación con enlace seguro.
- **Facturación por consumo:** el cierre mensual calcula automáticamente la cuota del plan + el consumo de IA + los mensajes enviados por canal, y genera el borrador de factura.
- **Portal del cliente y tablero operador:** operativos en frontend (acceso vía URL del VPS; DNS público pendiente de configurar).
- **Diferido por decisión de dirección:** facturación electrónica CFDI 4.0 (Sprint 4, vía Ecofile) y promociones avanzadas (Sprint 5).

## Fortalezas (verificadas)

- **Integridad financiera:** todo el dinero en centavos enteros; las tablas de
  pagos, ajustes y auditoría son **inmutables** (no se pueden borrar ni alterar).
- **Trazabilidad total:** cada operación queda registrada con usuario, IP, fecha y
  el detalle del cambio.
- **Seguridad sólida:** contraseñas con cifrado de grado bancario (Argon2id),
  sesiones protegidas, control de acceso por rol, validación de pagos con firma.

## Riesgos a vigilar

1. **Recarga (bloqueante):** corregir la ruta de acreditación y re-verificar QA.
2. **Notificaciones:** las plantillas de correo deben cargarse en el core de
   mensajes; WhatsApp aún no está habilitado.
3. **Trabajo sin versionar:** hay cambios funcionales en el repositorio que aún no
   se han confirmado (commit) ni pasado QA final.

## Próximos pasos

1. Resolver el bloqueante de recarga y cerrar QA del flujo de pago end-to-end.
2. Sembrar plantillas de correo y completar el onboarding (wallet + correo de
   activación).
3. Configurar DNS/TLS de los dominios en el VPS (tarea de infraestructura).
4. Avanzar a operación multi-cliente una vez cerrado el hardening pendiente.

## Métricas clave (cuando esté en operación)

Ingresos por periodo/producto/cliente · saldo y consumo por cliente · facturas
emitidas y cobradas · tasa de éxito de cobros.

*Resumen ejecutivo — auditoría global Inovaweb 2026-06-06.*
