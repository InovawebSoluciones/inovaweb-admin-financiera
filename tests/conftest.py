"""Fixtures comunes de tests."""

import os

# valores dummy para que pydantic-settings no falle al cargar app
os.environ.setdefault("ENV", "dev")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost/test")
os.environ.setdefault("POSTGRES_PASSWORD", "test")
os.environ.setdefault("AES_KEY", "dummy_aes_key_for_tests_only_32bytes_ok")
os.environ.setdefault("JWT_SECRET", "dummy_jwt_secret_for_tests_only_32bytes")
os.environ.setdefault("MEDIDOR_API_KEY", "dummy")
os.environ.setdefault("HUB_API_KEY", "dummy")
os.environ.setdefault("MESSAGES_API_KEY", "dummy")
os.environ.setdefault("FINANZAS_API_KEY", "dummy")
os.environ.setdefault("PAC_API_KEY", "dummy")
os.environ.setdefault("PAC_API_SECRET", "dummy")
os.environ.setdefault("RFC_EMISOR", "AAA010101AAA")
os.environ.setdefault("KEY_PASSWORD", "dummy")
