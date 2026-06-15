#!/usr/bin/env bash
set -uo pipefail

# =============================================================================
# verify_catalog_isolation.sh
#
# Verificacion de AISLAMIENTO MULTI-TENANT del Bloque 1 (Catalogo) del CAF.
#
# Objetivo: probar que los endpoints de catalogo bajo /admin/catalog respetan
# el aislamiento por organizacion (org), es decir:
#   - El SUPER TENANT (super_admin de la org 1) puede operar en cualquier org,
#     incluyendo el bypass de plataforma via ?org=<id>.
#   - Un super_admin "scoped" a OTRA org (p.ej. org 2) NO puede tocar recursos
#     de una org ajena: debe recibir 403 o 404.
#
# IMPORTANTE: este script esta pensado para correr DENTRO del VPS (el backend
# escucha en http://127.0.0.1:8006). No toca produccion de forma destructiva
# mas alla de crear recursos de catalogo de prueba (con sufijo aleatorio).
#
# Uso (desde el VPS):
#   bash verify_catalog_isolation.sh
#
# Requisitos: curl, docker compose disponible (para emitir los JWT).
# =============================================================================

# ----------------------------------------------------------------------------
# Configuracion
# ----------------------------------------------------------------------------
BASE="http://127.0.0.1:8006"          # Backend del CAF (desde el propio VPS)
CAF_DIR="/opt/inovaweb-admin-financiera"  # Donde vive el docker compose del CAF

# Sufijo aleatorio para no chocar con corridas previas (idempotente-friendly)
RND="$RANDOM"

# Contadores globales del resumen
PASS_COUNT=0
FAIL_COUNT=0

# ----------------------------------------------------------------------------
# Helpers de salida (PASS / FAIL)
# ----------------------------------------------------------------------------

# print_result <descripcion> <esperado> <obtenido>
#   Compara el codigo HTTP esperado (puede ser una lista separada por '|',
#   p.ej. "403|404") contra el obtenido e imprime PASS/FAIL.
print_result() {
  local desc="$1"
  local expected="$2"
  local got="$3"
  # Coincidencia: el codigo obtenido aparece dentro de la lista esperada.
  if [[ "|$expected|" == *"|$got|"* ]]; then
    PASS_COUNT=$((PASS_COUNT + 1))
    printf '  [PASS] %-55s esperado=%-9s obtenido=%s\n' "$desc" "$expected" "$got"
  else
    FAIL_COUNT=$((FAIL_COUNT + 1))
    printf '  [FAIL] %-55s esperado=%-9s obtenido=%s\n' "$desc" "$expected" "$got"
  fi
}

# section <titulo>  -> imprime un encabezado de seccion
section() {
  printf '\n=== %s ===\n' "$1"
}

# print_present <descripcion> <valor>
#   PASS si <valor> es no vacio (se creo el recurso y devolvio id), si no FAIL.
print_present() {
  local desc="$1"; local val="$2"
  if [[ -n "$val" ]]; then
    PASS_COUNT=$((PASS_COUNT + 1)); printf '  [PASS] %-55s id=%s\n' "$desc" "$val"
  else
    FAIL_COUNT=$((FAIL_COUNT + 1)); printf '  [FAIL] %-55s (sin id en la respuesta)\n' "$desc"
  fi
}

# ----------------------------------------------------------------------------
# Helper: emision de JWT
# ----------------------------------------------------------------------------

# issue_jwt <USER_ID> <ORG_ID>
#   Emite un JWT de super_admin scoped a la org indicada, usando la utilidad
#   issue_access del backend dentro del contenedor docker.
#   Nota: el tercer argumento de issue_access (tenant/None) va como None.
issue_jwt() {
  local user_id="$1"
  local org_id="$2"
  docker compose exec -T admin_financiera python -c \
    "from app.core.jwt_auth import issue_access; print(issue_access(${user_id}, ['super_admin'], None, ${org_id}))"
}

# ----------------------------------------------------------------------------
# Helpers HTTP: devuelven SOLO el codigo HTTP (para comparar facil)
# ----------------------------------------------------------------------------

# http_code <jwt> <metodo> <ruta> [json_body]
#   Ejecuta la peticion y devuelve unicamente el codigo HTTP.
http_code() {
  local jwt="$1"
  local method="$2"
  local path="$3"
  local body="${4:-}"
  if [[ -n "$body" ]]; then
    curl -s -o /dev/null -w '%{http_code}' \
      -X "$method" "${BASE}${path}" \
      -H "Authorization: Bearer ${jwt}" \
      -H "Content-Type: application/json" \
      -d "$body"
  else
    curl -s -o /dev/null -w '%{http_code}' \
      -X "$method" "${BASE}${path}" \
      -H "Authorization: Bearer ${jwt}"
  fi
}

# http_json <jwt> <metodo> <ruta> [json_body]
#   Igual que http_code pero devuelve el CUERPO de la respuesta (para extraer
#   ids de los recursos recien creados).
http_json() {
  local jwt="$1"
  local method="$2"
  local path="$3"
  local body="${4:-}"
  if [[ -n "$body" ]]; then
    curl -s \
      -X "$method" "${BASE}${path}" \
      -H "Authorization: Bearer ${jwt}" \
      -H "Content-Type: application/json" \
      -d "$body"
  else
    curl -s \
      -X "$method" "${BASE}${path}" \
      -H "Authorization: Bearer ${jwt}"
  fi
}

# extract_id <json>
#   Extrae el campo "id" de una respuesta JSON sin depender de jq
#   (toma el primer "id": <numero>).
extract_id() {
  printf '%s' "$1" | grep -o '"id"[[:space:]]*:[[:space:]]*[0-9]\+' | head -n1 | grep -o '[0-9]\+'
}

# ----------------------------------------------------------------------------
# Preparacion: emitir los JWT necesarios
# ----------------------------------------------------------------------------
# Para emitir los JWT necesitamos estar en el directorio del compose del CAF.
cd "$CAF_DIR" || { echo "No se pudo entrar a $CAF_DIR"; exit 1; }

section "Emitiendo JWT"
# Super tenant: super_admin de la org 1 (puede operar en cualquier org).
JWT_ORG1="$(issue_jwt 1 1)"
# Admin "ajeno": super_admin scoped a la org 2 (NO debe tocar recursos de org1).
JWT_ORG2="$(issue_jwt 1 2)"

if [[ -z "${JWT_ORG1:-}" || -z "${JWT_ORG2:-}" ]]; then
  echo "ERROR: no se pudieron emitir los JWT. Abortando."
  exit 1
fi
echo "  JWT org1 (super tenant): emitido"
echo "  JWT org2 (admin ajeno):  emitido"

# =============================================================================
# CASO 1: El super tenant (org1) crea/edita/togglea un servicio en SU org
# =============================================================================
section "Caso 1: super tenant opera en su propia org (org1)"

SVC1_CODE="svc_org1_${RND}"
SVC1_BODY="{\"code\":\"${SVC1_CODE}\",\"name\":\"Servicio Org1 ${RND}\",\"source_core\":\"internal\",\"unit\":\"email\",\"unit_price_cents\":100}"

# 1a. Crear servicio en org1 -> 200/201
SVC1_RESP="$(http_json "$JWT_ORG1" POST "/admin/catalog/services" "$SVC1_BODY")"
SVC1_ID="$(extract_id "$SVC1_RESP")"
print_present "Crear servicio en org1 (devuelve id)" "$SVC1_ID"

if [[ -z "${SVC1_ID:-}" ]]; then
  echo "  AVISO: no se pudo extraer el id del servicio de org1; los casos 2 y 5 podrian fallar."
fi

# 1b. Editar (PATCH) el precio del servicio de org1 -> 200
PATCH1_CODE="$(http_code "$JWT_ORG1" PATCH "/admin/catalog/services/${SVC1_ID:-0}" '{"unit_price_cents":150}')"
print_result "Editar precio servicio org1 (super tenant)" "200" "$PATCH1_CODE"

# 1c. Toggle del servicio de org1 -> 200
TOGGLE1_CODE="$(http_code "$JWT_ORG1" POST "/admin/catalog/services/${SVC1_ID:-0}/toggle")"
print_result "Toggle servicio org1 (super tenant)" "200" "$TOGGLE1_CODE"

# =============================================================================
# CASO 2 (CLAVE): Admin de org2 intenta tocar el servicio de org1 -> 403/404
# =============================================================================
section "Caso 2 (AISLAMIENTO): org2 intenta modificar servicio de org1"

# 2a. PATCH cruzado (org2 sobre recurso de org1) -> 403 o 404
XPATCH_CODE="$(http_code "$JWT_ORG2" PATCH "/admin/catalog/services/${SVC1_ID:-0}" '{"unit_price_cents":999}')"
print_result "org2 PATCH servicio de org1 (debe bloquearse)" "403|404" "$XPATCH_CODE"

# 2b. TOGGLE cruzado (org2 sobre recurso de org1) -> 403 o 404
XTOGGLE_CODE="$(http_code "$JWT_ORG2" POST "/admin/catalog/services/${SVC1_ID:-0}/toggle")"
print_result "org2 TOGGLE servicio de org1 (debe bloquearse)" "403|404" "$XTOGGLE_CODE"

# =============================================================================
# CASO 3: El super tenant usa ?org=2 (bypass de plataforma) para crear en org2
# =============================================================================
section "Caso 3: super tenant crea servicio en org2 via ?org=2 (bypass)"

SVC2_CODE="svc_org2_${RND}"
SVC2_BODY="{\"code\":\"${SVC2_CODE}\",\"name\":\"Servicio Org2 ${RND}\",\"source_core\":\"internal\",\"unit\":\"email\",\"unit_price_cents\":200}"

# 3a. Crear servicio en org2 desde el super tenant -> 200/201
SVC2_RESP="$(http_json "$JWT_ORG1" POST "/admin/catalog/services?org=2" "$SVC2_BODY")"
SVC2_ID="$(extract_id "$SVC2_RESP")"
print_present "Crear servicio en org2 via ?org=2 (super tenant)" "$SVC2_ID"

if [[ -z "${SVC2_ID:-}" ]]; then
  echo "  AVISO: no se pudo extraer el id del servicio de org2; el caso 4 podria fallar."
fi

# =============================================================================
# CASO 4: Admin de org2 edita SU PROPIO servicio (el de org2) -> 200
# =============================================================================
section "Caso 4: org2 edita su propio servicio (debe permitirse)"

# 4a. PATCH del admin de org2 sobre el servicio de org2 -> 200
OWN_PATCH_CODE="$(http_code "$JWT_ORG2" PATCH "/admin/catalog/services/${SVC2_ID:-0}" '{"unit_price_cents":250}')"
print_result "org2 PATCH su propio servicio org2" "200" "$OWN_PATCH_CODE"

# =============================================================================
# CASO 5: super tenant crea plan en org1; org2 intenta editarlo -> 403/404
# =============================================================================
section "Caso 5 (AISLAMIENTO): org2 intenta editar plan de org1"

PLAN1_CODE="plan_org1_${RND}"
PLAN1_BODY="{\"code\":\"${PLAN1_CODE}\",\"name\":\"Plan Org1 ${RND}\",\"monthly_fee_cents\":50000}"

# 5a. Crear plan en org1 (super tenant) -> 200/201
PLAN1_RESP="$(http_json "$JWT_ORG1" POST "/admin/catalog/plans" "$PLAN1_BODY")"
PLAN1_ID="$(extract_id "$PLAN1_RESP")"
print_present "Crear plan en org1 (super tenant)" "$PLAN1_ID"

if [[ -z "${PLAN1_ID:-}" ]]; then
  echo "  AVISO: no se pudo extraer el id del plan de org1; el cruce podria fallar."
fi

# 5b. org2 intenta PATCH del plan de org1 -> 403 o 404 (AISLAMIENTO)
XPLAN_PATCH_CODE="$(http_code "$JWT_ORG2" PATCH "/admin/catalog/plans/${PLAN1_ID:-0}" '{"name":"hackeado"}')"
print_result "org2 PATCH plan de org1 (debe bloquearse)" "403|404" "$XPLAN_PATCH_CODE"

# 5c. (extra) org2 intenta TOGGLE del plan de org1 -> 403 o 404
XPLAN_TOGGLE_CODE="$(http_code "$JWT_ORG2" POST "/admin/catalog/plans/${PLAN1_ID:-0}/toggle")"
print_result "org2 TOGGLE plan de org1 (debe bloquearse)" "403|404" "$XPLAN_TOGGLE_CODE"

# =============================================================================
# Resumen final
# =============================================================================
section "RESUMEN"
TOTAL=$((PASS_COUNT + FAIL_COUNT))
printf '  Total casos : %d\n' "$TOTAL"
printf '  PASS        : %d\n' "$PASS_COUNT"
printf '  FAIL        : %d\n' "$FAIL_COUNT"

if [[ "$FAIL_COUNT" -eq 0 ]]; then
  printf '\n  RESULTADO GLOBAL: PASS (aislamiento multi-tenant correcto)\n'
  exit 0
else
  printf '\n  RESULTADO GLOBAL: FAIL (revisar casos marcados [FAIL])\n'
  exit 1
fi
