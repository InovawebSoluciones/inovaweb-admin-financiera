"""conftest.py — parchea psycopg y SQLAlchemy engine ANTES de importar app.

El sandbox de CI no tiene libpq. Se reemplaza create_async_engine con un stub
que devuelve un MagicMock, lo que permite importar app.* y construir el
TestClient sin levantar una BD real. Los tests de TASK-08 usan dependency
overrides para inyectar un AsyncSession mock, asi que nunca llegan al engine.
"""
from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

# ── 1. stub para psycopg (evita ImportError de libpq) ───────────────────────
psycopg_stub = MagicMock()
sys.modules.setdefault("psycopg", psycopg_stub)
sys.modules.setdefault("psycopg.rows", psycopg_stub)
sys.modules.setdefault("psycopg_binary", psycopg_stub)
sys.modules.setdefault("psycopg_c", psycopg_stub)

# ── 2. stub para create_async_engine (evita validacion del dialect) ──────────
import sqlalchemy.ext.asyncio as _asa

_fake_engine = MagicMock()
_fake_engine.dispose = AsyncMock()

_orig_cae = _asa.create_async_engine


def _patched_cae(url, **kw):
    """Devuelve un engine falso si el dialect es psycopg (sandbox sin libpq)."""
    url_str = str(url)
    if "psycopg" in url_str:
        return _fake_engine
    return _orig_cae(url, **kw)


_asa.create_async_engine = _patched_cae

# ── 3. también parchear en sqlalchemy.ext.asyncio directo ───────────────────
import sqlalchemy.ext.asyncio
sqlalchemy.ext.asyncio.create_async_engine = _patched_cae

# ── 4. stub SessionLocal para que database.py no falle ──────────────────────
# Se importa aqui para forzar la ejecucion del modulo ya parchado.
import importlib, os

os.environ.setdefault("ENV", "dev")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://caf:caf@localhost/test")
os.environ.setdefault("POSTGRES_PASSWORD", "caf")
os.environ.setdefault("AES_KEY", "dGVzdGtleXRlc3RrZXl0ZXN0a2V5dGVzdA==")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-at-least-32-bytes-long!")
os.environ.setdefault("MEDIDOR_API_KEY", "test")
os.environ.setdefault("HUB_API_KEY", "test")
os.environ.setdefault("MESSAGES_API_KEY", "test")
os.environ.setdefault("FINANZAS_API_KEY", "test")
os.environ.setdefault("SCRAPING_ADMIN_KEY", "test")
os.environ.setdefault("PAC_API_KEY", "test")
os.environ.setdefault("PAC_API_SECRET", "test")
os.environ.setdefault("RFC_EMISOR", "TST000000AAA")
os.environ.setdefault("KEY_PASSWORD", "test")

# Invalidar cache de settings si ya fue construido con valores distintos
from app.core import config as _config_mod
_config_mod.get_settings.cache_clear()
