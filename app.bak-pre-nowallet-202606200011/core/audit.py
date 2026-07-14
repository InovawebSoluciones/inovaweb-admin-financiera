"""Writer del audit_log y contexto de auditoria por request.

Patron: el middleware setea variables de sesion en postgres antes de cada
transaccion. Los triggers de 002_security_constraints.sql leen estas
variables y completan el audit_log automaticamente.

Para acciones que NO mutan tablas (login, logout, lectura sensible) se usa
write_event() para inyectar manualmente.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def bind_actor(
    db: AsyncSession,
    *,
    actor_user_id: int | None,
    actor_ip: str | None,
    request_id: str | None,
) -> None:
    """Setea variables de sesion para que los triggers las recojan.

    Debe llamarse al inicio de cada request autenticado, dentro de la misma
    transaccion en que ocurriran las mutaciones.
    """
    await db.execute(
        text("SELECT set_config('app.actor_user_id', :v, true)"),
        {"v": str(actor_user_id) if actor_user_id is not None else ""},
    )
    await db.execute(
        text("SELECT set_config('app.actor_ip', :v, true)"),
        {"v": actor_ip or ""},
    )
    await db.execute(
        text("SELECT set_config('app.request_id', :v, true)"),
        {"v": request_id or ""},
    )


async def write_event(
    db: AsyncSession,
    *,
    actor_user_id: int | None,
    actor_ip: str | None,
    entity_type: str,
    entity_id: int | None,
    action: str,
    old_values: dict[str, Any] | None = None,
    new_values: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> None:
    """Inserta un evento manual (login, logout, export, etc.)."""
    await db.execute(
        text("""
            INSERT INTO audit_log
              (actor_user_id, actor_ip, entity_type, entity_id, action,
               old_values, new_values, request_id)
            VALUES
              (:uid, NULLIF(:ip,'')::INET, :etype, :eid, :action,
               CAST(:old AS JSONB), CAST(:new AS JSONB), :req)
        """),
        {
            "uid": actor_user_id,
            "ip": actor_ip or "",
            "etype": entity_type,
            "eid": entity_id,
            "action": action,
            "old": None if old_values is None else _jsonify(old_values),
            "new": None if new_values is None else _jsonify(new_values),
            "req": request_id,
        },
    )


def _jsonify(d: dict[str, Any]) -> str:
    import json
    return json.dumps(d, default=str, ensure_ascii=False)
