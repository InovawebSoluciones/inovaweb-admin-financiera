# Guía del Operador — Centro de Administración Financiera (CAF)

Manual para el equipo interno de Inovaweb que opera el sistema. No requiere
conocimientos técnicos. Acceso en **https://admin.inovaweb.com.mx**.

> **Nota de estado (2026-06-06):** el sistema está en piloto. El flujo de recarga
> automática tiene un ajuste técnico pendiente (ver con el equipo de desarrollo
> antes de procesar pagos reales). Las funciones de consulta y alta ya operan.

---

## 1. Entrar al sistema

1. Abre **https://admin.inovaweb.com.mx**.
2. Ingresa tu correo y contraseña.
3. Tras 5 intentos fallidos la cuenta se bloquea 15 minutos (seguridad).
4. Tu sesión expira por inactividad; vuelve a entrar si te la pide.

Roles y qué puede hacer cada uno:
- **Super admin:** todo, incluida administración de usuarios y catálogos.
- **Finanzas:** alta de clientes, cobranza, facturas, catálogos.
- **Lectura:** solo consultar tableros y listados (no modifica nada).

---

## 2. Tablero de ingresos

`Dashboard` muestra los ingresos consolidados por periodo, producto, cliente y
concepto. Los montos se leen del libro contable (Finanzas) y del Medidor; el CAF
no recalcula saldos.

---

## 3. Dar de alta un cliente

1. Ve a **Clientes → Nuevo**.
2. Captura: razón social, RFC, uso de CFDI, correo de facturación, plan inicial,
   y el nombre y correo del titular.
3. Al guardar, el sistema (de forma automática y atómica):
   - crea el cliente,
   - le abre una wallet de saldo en el Medidor,
   - lo suscribe al plan,
   - crea el usuario titular con una contraseña temporal.
4. Si algún paso falla, **se revierte todo** y queda registrado en la auditoría.
   Vuelve a intentarlo o avisa a soporte.

> El correo de activación al titular depende de que las plantillas de correo estén
> cargadas en el Centro de Mensajes. Si el cliente no recibe correo, repórtalo.

---

## 4. Editar, suspender o reactivar un cliente

- **Editar:** Clientes → (cliente) → Editar.
- **Suspender por mora:** Clientes → (cliente) → Suspender. El cliente pierde
  acceso al portal hasta reactivarlo.
- Toda acción queda en la auditoría con tu usuario, fecha y hora.

---

## 5. Catálogos

En **Catálogo** administras: Productos, Servicios cobrables, Planes y Promociones
(cupones, descuentos). Los precios se capturan en pesos pero el sistema los guarda
en centavos para evitar errores de redondeo.

---

## 6. Cobranza y facturas

- **Facturas:** lista de facturas emitidas, su estatus y descargas.
- **Forzar cierre mensual:** Cobranza → Ejecutar cierre. Normalmente corre solo el
  día 1 de cada mes; úsalo solo si necesitas adelantarlo.
- El timbrado fiscal CFDI 4.0 (Fase 4) está diferido hasta seleccionar el PAC.

---

## 7. Auditoría

**Audit log** registra cada operación de escritura (quién, desde qué IP, cuándo, y
qué cambió). Es de solo lectura e inmutable: nadie —ni un super admin— puede
borrarlo o alterarlo. Úsalo para investigar cualquier discrepancia.

---

## 8. ¿Algo falla?

- Si una acción no responde o ves un error, **no la repitas a ciegas**: revisa el
  audit log y avisa a soporte técnico con la hora aproximada.
- Para problemas de pago/recarga, escala a desarrollo (hay un ajuste técnico
  pendiente en esa ruta).

*Guía del operador — 2026-06-06.*
