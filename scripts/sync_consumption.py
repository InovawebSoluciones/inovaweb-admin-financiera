"""Sync idempotente de CONSUMO -> Finanzas (el CAF es el único contador, D2).

Dos frentes:
  1) IA: lee los débitos `llm_proxy_debit` de la wallet en el Medidor y asienta
     cada uno en Finanzas (source=medidor, source_ref=caf-medidor-<request_id>).
  2) Mensajería: lee los mensajes `sent` del cliente en Centro y por cada uno
     TARIFA con price_catalog, DEBITA la wallet del Medidor y asienta en Finanzas
     (source=messages, source_ref=caf-msg-<message_id>).

Todo idempotente: la UNIQUE de Finanzas y el request_id del Medidor deduplican,
así que re-ejecutar es seguro (apto para cron).

Uso (dentro de caf_app):
    docker exec -e PYTHONPATH=/app caf_app python /tmp/sync_consumption.py <wallet_id> <client_id> <client_external_id>
"""
from __future__ import annotations

import asyncio
import sys

from app.core.clients.medidor_client import MedidorClient
from app.core.clients.messages_client import MessagesClient
from app.core.clients.finanzas_client import FinanzasClient

# Precio público por canal en centavos (espejo de price_catalog meter=message).
MSG_PRICE_CENTS = {
    "email": 100,
    "whatsapp": 150,
    "sms": 80,
    "instagram": 100,
    "messenger": 100,
}


async def sync_ia(med, fin, *, wallet_id: str, client_id: int) -> dict:
    posted = skipped = errors = 0
    resp = await med.c.get(f"/v1/wallets/{wallet_id}/transactions", params={"kind": "debit"})
    for t in (resp or {}).get("items", []) or []:
        if t.get("reason") != "llm_proxy_debit":
            continue
        amount = abs(int(t.get("amount_cents") or 0))
        if amount <= 0:
            continue
        req = str(t.get("request_id"))
        m = t.get("meta") or {}
        try:
            await fin.post_entry(
                source_slug="medidor",
                source_ref=f"caf-medidor-{req}",
                direction="debit",
                amount_cents=amount,
                occurred_at=str(t.get("created_at")),
                description=f"Consumo IA {m.get('model','')} ({m.get('prompt_tokens',0)}+{m.get('completion_tokens',0)} tokens)",
                meta={"caf_client_id": client_id, "medidor_tx_id": t.get("id"), "request_id": req},
            )
            posted += 1
        except Exception as e:  # noqa: BLE001
            if any(s in str(e).lower() for s in ("409", "duplicate", "exists", "conflict", "unique")):
                skipped += 1
            else:
                errors += 1
                print(f"[!] IA ERROR caf-medidor-{req}: {str(e)[:160]}")
    return {"posted": posted, "skipped": skipped, "errors": errors}


async def sync_messages(med, msg, fin, *, wallet_id: str, client_id: int, client_external_id: str) -> dict:
    posted = skipped = errors = 0
    resp = await msg.c.get("/v1/messages", params={"limit": 200})
    items = (resp or {}).get("items", []) or []
    for mrow in items:
        if mrow.get("client_id") != client_external_id:
            continue
        if mrow.get("status") not in ("sent", "delivered"):
            continue
        channel = mrow.get("channel")
        price = MSG_PRICE_CENTS.get(channel)
        if not price:
            continue
        mid = str(mrow.get("id"))
        ref = f"caf-msg-{mid}"
        occurred = str(mrow.get("sent_at") or mrow.get("queued_at"))
        # 1) DEBITA la wallet (idempotente por request_id).
        try:
            await med.c.post(
                f"/v1/wallets/{wallet_id}/debit",
                json={"amount_cents": price, "currency": "MXN", "request_id": ref,
                      "reason": f"Consumo {channel} (Centro msg {mid[:8]})"},
            )
        except Exception as e:  # noqa: BLE001
            # 409/idempotente o saldo insuficiente: continuar al asiento igual si ya existía
            if not any(s in str(e).lower() for s in ("409", "duplicate", "exists", "conflict", "idempot")):
                errors += 1
                print(f"[!] MSG debit ERROR {ref}: {str(e)[:160]}")
        # 2) Asiento Finanzas (idempotente por source_ref).
        try:
            await fin.post_entry(
                source_slug="messages",
                source_ref=ref,
                direction="debit",
                amount_cents=price,
                occurred_at=occurred,
                description=f"Consumo mensajería {channel} -> {mrow.get('to_email') or mrow.get('to_phone')}",
                meta={"caf_client_id": client_id, "centro_message_id": mid, "channel": channel},
            )
            posted += 1
        except Exception as e:  # noqa: BLE001
            if any(s in str(e).lower() for s in ("409", "duplicate", "exists", "conflict", "unique")):
                skipped += 1
            else:
                errors += 1
                print(f"[!] MSG fin ERROR {ref}: {str(e)[:160]}")
    return {"posted": posted, "skipped": skipped, "errors": errors}


async def main(wallet_id: str, client_id: int, client_external_id: str) -> None:
    med = MedidorClient()
    msg = MessagesClient()
    fin = FinanzasClient()
    try:
        ia = await sync_ia(med, fin, wallet_id=wallet_id, client_id=client_id)
        ms = await sync_messages(med, msg, fin, wallet_id=wallet_id, client_id=client_id, client_external_id=client_external_id)
        print(f"IA  -> {ia}")
        print(f"MSG -> {ms}")
    finally:
        await med.close()
        await msg.close()
        await fin.close()


if __name__ == "__main__":
    w = sys.argv[1] if len(sys.argv) > 1 else "25116fe8-8b3e-4ca9-8415-a81c13fd061b"
    c = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    ext = sys.argv[3] if len(sys.argv) > 3 else "client-5"
    asyncio.run(main(w, c, ext))
