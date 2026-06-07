#!/bin/bash
set -e
echo "=== DEPLOY CAF + SCRAPING ==="

# 1. Pull ambos repos
cd /opt/inovaweb-admin-financiera
git pull

# 2. Aplicar migraciones nuevas (idempotentes, IF NOT EXISTS)
docker compose exec -T postgres psql -U caf -d admin_financiera \
  -f /docker-entrypoint-initdb.d/005_activation_tokens.sql
docker compose exec -T postgres psql -U caf -d admin_financiera \
  -f /docker-entrypoint-initdb.d/006_idempotencia.sql

# 3. Rebuild y levantar CAF
docker compose up -d --build

# 4. Verificar que levantó
sleep 5
curl -sf http://localhost:8006/health && echo "CAF OK" || echo "CAF FALLO"

# 5. Pytest en Docker
docker compose run --rm admin_financiera sh -c \
  "pip install pytest pytest-asyncio httpx --quiet && python -m pytest tests/ -v --tb=short"

# Deploy Scraping (fix D1 — alembic 0005)
# NOTA: ruta y servicio ajustados a los valores REALES del repo Scraping
#   - ruta VPS: /root/scraping-universidades (ver scraping_comercial/CLAUDE.md;
#     la plantilla original decia /opt/scraping-universidades — confirmar cual existe)
#   - servicio: "backend" (container scraping-backend), NO "scraping"
echo "=== DEPLOY SCRAPING ==="
cd /root/scraping-universidades
git pull
# aplica 0005_caf_client_id_bigint (UUID -> BIGINT). exec si el backend esta vivo;
# si no, run --rm levanta un contenedor efimero con el mismo entorno.
docker compose exec -T backend alembic upgrade head || \
  docker compose run --rm backend alembic upgrade head
docker compose up -d --build backend
echo "Scraping OK"
