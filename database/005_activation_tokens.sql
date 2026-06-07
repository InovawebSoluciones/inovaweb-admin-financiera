-- 005_activation_tokens.sql
-- TASK-16 (#16 Onboarding): token de activacion de un solo uso para el titular.
--
-- En el alta se emite un token de 32 bytes (token_hex(32)) que se envia por
-- correo via el Centro de Mensajes. En BD se almacena SOLO el hash SHA-256 del
-- token (nunca el token en claro) con expiracion de 24h. Al activar se marca
-- `used_at` para impedir la reutilizacion (un solo uso).

CREATE TABLE IF NOT EXISTS activation_tokens (
    id          BIGSERIAL PRIMARY KEY,
    user_id     BIGINT NOT NULL REFERENCES users(id),
    token_hash  TEXT NOT NULL UNIQUE,
    expires_at  TIMESTAMPTZ NOT NULL DEFAULT now() + interval '24 hours',
    used_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_activation_tokens_user ON activation_tokens(user_id);
