# Modelo de cobro Inovaweb — hoja para revisión del contador

> Propósito: que un contador/financiero valide el modelo ("bien o mal") sin
> leer código. Estado: PROPUESTA en implementación (2026-06-07). Los **precios
> son placeholders** — el contador/negocio debe fijarlos.

---

## 1. En cristiano (cómo cobramos)

El cliente **prepaga** un saldo (como un monedero/tiempo aire). Ese saldo se va
**descontando conforme consume** dos cosas, que son **distintas y se miden
distinto**:

1. **Inteligencia Artificial (IA)** — se mide en **tokens** (fracciones muy
   pequeñas). Lo mide el **Medidor**.
2. **Mensajería** — se cuenta en **mensajes enteros** (1 email, 1 WhatsApp, 1
   SMS, 1 Instagram, 1 Messenger…). Cada canal cuesta distinto. Lo cuenta el
   **Centro de Mensajes**.

El **precio al público** de cada cosa vive en una **tabla de precios** (en el
CAF). El sistema toma *cuánto consumió* (cantidad) × *precio público* = **pesos**,
y eso le baja al saldo. Cuando el saldo se acaba, se bloquea hasta que recargue.

**Importante (contable):**
- El dinero que entra en una recarga **NO es ingreso todavía**: es un **anticipo
  del cliente** (un pasivo, dinero que le debemos en forma de servicio).
- Se vuelve **ingreso** poco a poco, **a medida que consume**.
- Guardamos también **cuánto nos costó** a nosotros (el proveedor: OpenAI,
  Perplexity, el canal de mensajería, la comisión de la pasarela) para saber si
  **ganamos o perdemos** en cada cosa (margen).

---

## 2. Quién hace qué (las capas — ninguna se brinca)

| Capa | Quién | Su único trabajo |
|---|---|---|
| Medir IA | **Medidor** (core) | Contar **tokens**. No sabe precios ni contabiliza. |
| Contar mensajes | **Centro de Mensajes** (core) | Contar **mensajes por canal**. No sabe precios ni contabiliza. |
| Cobrar el pago | **Hub** (core) | Procesar la tarjeta/OXXO/SPEI (Conekta, etc.). No contabiliza. |
| **Precios + saldo + factura** | **CAF** (nivel 2) | Tener la **tabla de precios**, convertir cantidad→pesos, **descontar saldo**, facturar, tableros. |
| **Libro** | **Finanzas** (core) | Guardar los **asientos** (los hechos en pesos, ya con precio aplicado). No decide precios. |

Regla de oro: **el precio se decide en el CAF; Finanzas solo registra montos ya
calculados; los cores solo miden/cuentan/cobran.**

---

## 3. El flujo completo (ejemplo: $500)

1. Cliente paga $500 → el **Hub** procesa el cobro (Conekta).
2. El **CAF** registra ese pago como **anticipo** (pasivo) y **acredita $500 de
   saldo**. Asienta en **Finanzas** (`source=hub`, es un anticipo).
3. Cliente consume IA → **Medidor** cuenta los tokens.
4. El **CAF** toma esos tokens × **precio público IA** = pesos; **baja el saldo**;
   reconoce **ingreso** por esa parte; asienta en **Finanzas** (`source=medidor`).
5. Cliente manda mensajes → **Centro de Mensajes** cuenta por canal.
6. El **CAF** toma los mensajes × **precio público del canal** = pesos; baja el
   saldo; reconoce ingreso; asienta en **Finanzas** (`source=messages`).
7. El cliente ve su **saldo actualizado**. Si se agota → bloqueo → recarga (vuelve a 1).
8. Cada consumo guarda también el **costo** (proveedor) → **margen = precio − costo**.

---

## 4. Decisiones que necesito del contador (placeholders hoy)

1. **Precios al público**: por 1,000 tokens de IA; y por cada canal (email,
   WhatsApp, SMS, IG, Messenger). Hoy van valores de ejemplo.
2. **¿Los precios llevan IVA incluido o se suma aparte?**
3. **CFDI/SAT**: ¿se timbra al **recargar** (anticipo) o al **consumir**
   (devengado)? (Monedero electrónico tiene reglas propias.)
4. **¿Finanzas debe ser contabilidad de partida doble** (para estados
   financieros formales) o basta un **libro de movimientos** y la contabilidad
   fiscal formal se lleva aparte (p. ej. Ecofile)?

---

## 5. Mapeo técnico (para desarrollo)

- **Tabla de precios** = `price_catalog` (CAF, migración `007`):
  - `meter` ∈ {`ia`, `message`}; `unit_code` (`token` | `email`|`whatsapp`|`sms`|`instagram`|`messenger`).
  - `public_price_micros` BIGINT — precio público por unidad en **micro-pesos**
    (1 peso = 1,000,000 micros) para soportar **sub-centavo** (un token vale
    fracciones de centavo).
  - `cost_price_micros` BIGINT — costo del proveedor por unidad (para margen/COGS).
  - Versionado/append-only (`valid_from`, `is_active`).
- **Servicio de tarificación** = `app/services/pricing.py`:
  - `price(meter, unit_code, quantity) -> {amount_cents, cost_cents, margin_cents}`.
  - Agrega a precisión de **micros** y **redondea una sola vez** a centavos
    (evita la sub-facturación por redondear cada operación a 0 — lección del
    hallazgo A04 de Scraping).
- **Saldo (única fuente de verdad)**: el **wallet del Medidor**, pero el gate y
  el descuento deben ser en **pesos al precio público** (no en costo crudo de
  tokens). El CAF/Finanzas son espejo, no el gate.
- **Anticipo vs ingreso**: el pago entra como pasivo (anticipo); el ingreso se
  reconoce en el asiento de consumo. (Pendiente de modelar en Finanzas según
  decisión #4.)
- **Asientos en Finanzas**: `source_slug` ∈ {hub, medidor, messages, invoice,
  subscription, manual}; idempotente por `(tenant, source_slug, source_ref)`;
  **solo el CAF** los emite (se retira el auto-reporte del Centro y el
  doble-crédito del Hub — discrepancia D2).
