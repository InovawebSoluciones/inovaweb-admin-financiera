"""Emision y timbrado de facturas CFDI 4.0 via PAC."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clients.pac_client import make as make_pac
from app.core.config import get_settings

log = logging.getLogger("invoicing")

INVOICES_DIR = Path(os.environ.get("INVOICES_DIR", "/var/lib/caf/invoices"))


async def stamp_invoice(db: AsyncSession, invoice_id: int) -> dict:
    """Construye CFDI desde invoice + items y lo timbra. Idempotente."""
    inv = (await db.execute(text("""
        SELECT i.*, c.rfc, c.legal_name, c.cfdi_use, c.tax_regime, c.zip_code
        FROM invoices i JOIN clients c ON c.id = i.client_id
        WHERE i.id = :id
    """), {"id": invoice_id})).mappings().first()
    if not inv:
        raise ValueError(f"invoice {invoice_id} no existe")
    if inv["status"] not in {"pending_stamp", "failed"}:
        return {"status": inv["status"], "uuid": str(inv["uuid_cfdi"]) if inv["uuid_cfdi"] else None}

    items = (await db.execute(text("""
        SELECT description, quantity, unit_price_cents, amount_cents,
               sat_key, unit_sat_key
        FROM invoice_items WHERE invoice_id = :i ORDER BY id
    """), {"i": invoice_id})).mappings().all()

    cfdi = _build_cfdi_payload(inv, items)
    pac = make_pac()
    try:
        result = await pac.stamp(cfdi)
    finally:
        await pac.close()

    INVOICES_DIR.mkdir(parents=True, exist_ok=True)
    xml_path = INVOICES_DIR / f"{result.uuid}.xml"
    pdf_path = INVOICES_DIR / f"{result.uuid}.pdf"
    xml_path.write_bytes(result.xml)
    pdf_path.write_bytes(result.pdf)

    await db.execute(text("""
        UPDATE invoices SET status='stamped', uuid_cfdi=:u,
          xml_path=:xp, pdf_path=:pp, stamped_at=now(),
          pac_provider=:prov
        WHERE id=:id
    """), {
        "id": invoice_id, "u": result.uuid,
        "xp": str(xml_path), "pp": str(pdf_path),
        "prov": get_settings().PAC_PROVIDER,
    })

    return {"status": "stamped", "uuid": result.uuid}


def _build_cfdi_payload(inv: dict, items: list) -> dict:
    s = get_settings()
    sub = inv["subtotal_cents"] / 100
    desc = inv["discount_cents"] / 100
    return {
        "NameId": "1",
        "Folio": str(inv["id"]),
        "CfdiType": "I",
        "PaymentForm": "99",
        "PaymentMethod": "PPD",
        "ExpeditionPlace": "00000",  # CP de Inovaweb (override en config si aplica)
        "Issuer": {"FiscalRegime": "601", "Rfc": s.RFC_EMISOR, "Name": "Inovaweb"},
        "Receiver": {
            "Rfc": inv["rfc"], "Name": inv["legal_name"],
            "CfdiUse": inv["cfdi_use"], "FiscalRegime": inv["tax_regime"],
            "TaxZipCode": inv["zip_code"],
        },
        "Items": [
            {
                "ProductCode": it["sat_key"],
                "Description": it["description"],
                "UnitCode": it["unit_sat_key"],
                "Quantity": float(it["quantity"]),
                "UnitPrice": float(it["unit_price_cents"]) / 100,
                "Subtotal": float(it["amount_cents"]) / 100,
                "Taxes": [{
                    "Total": float(it["amount_cents"]) / 100 * 0.16,
                    "Name": "IVA", "Base": float(it["amount_cents"]) / 100,
                    "Rate": 0.16, "IsRetention": False,
                }],
                "Total": float(it["amount_cents"]) / 100 * 1.16,
            }
            for it in items
        ],
        "Subtotal": sub,
        "Discount": desc,
        "Total": inv["total_cents"] / 100,
    }
