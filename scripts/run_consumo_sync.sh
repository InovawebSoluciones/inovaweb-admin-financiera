#!/bin/bash
# Sync automático de consumo (IA + mensajería) -> Finanzas. Cron cada 5 min.
set -e
docker cp /opt/inovaweb-admin-financiera/scripts/sync_consumption.py caf_app:/tmp/sync_consumption.py
docker exec -e PYTHONPATH=/app caf_app python /tmp/sync_consumption.py 25116fe8-8b3e-4ca9-8415-a81c13fd061b 5 client-5
