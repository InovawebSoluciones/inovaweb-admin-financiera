"""Cliente al PAC (CFDI 4.0). Abstrae proveedor (Facturama default).

Fase 4 del roadmap. La interfaz queda definida ahora; la implementacion real
de timbrado se completa cuando se firme el PAC.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.core.config import get_settings


@dataclass(slots=True)
class CfdiResult:
    uuid: str
    xml: bytes
    pdf: bytes
    stamped_at: str


class PacClient(Protocol):
    async def stamp(self, cfdi_data: dict[str, Any]) -> CfdiResult: ...
    async def cancel(self, uuid: str, *, reason_code: str) -> None: ...
    async def download_xml(self, uuid: str) -> bytes: ...
    async def download_pdf(self, uuid: str) -> bytes: ...
    async def close(self) -> None: ...


class FacturamaClient:
    """Implementacion para Facturama. Endpoints reales se ajustan al firmar."""

    def __init__(self) -> None:
        s = get_settings()
        self._http = httpx.AsyncClient(
            base_url=s.PAC_BASE_URL,
            auth=(s.PAC_API_KEY.get_secret_value(), s.PAC_API_SECRET.get_secret_value()),
            timeout=httpx.Timeout(30.0),
            headers={"Accept": "application/json"},
        )

    async def stamp(self, cfdi_data: dict[str, Any]) -> CfdiResult:
        r = await self._http.post("/3/cfdis", json=cfdi_data)
        r.raise_for_status()
        data = r.json()
        return CfdiResult(
            uuid=data["Complement"]["TaxStamp"]["Uuid"],
            xml=(await self.download_xml(data["Complement"]["TaxStamp"]["Uuid"])),
            pdf=(await self.download_pdf(data["Complement"]["TaxStamp"]["Uuid"])),
            stamped_at=data["Complement"]["TaxStamp"]["Date"],
        )

    async def cancel(self, uuid: str, *, reason_code: str) -> None:
        r = await self._http.delete(f"/cfdi/{uuid}", params={"motive": reason_code})
        r.raise_for_status()

    async def download_xml(self, uuid: str) -> bytes:
        r = await self._http.get(f"/cfdi/xml/issuedLite/{uuid}")
        r.raise_for_status()
        return r.content

    async def download_pdf(self, uuid: str) -> bytes:
        r = await self._http.get(f"/cfdi/pdf/issuedLite/{uuid}")
        r.raise_for_status()
        return r.content

    async def close(self) -> None:
        await self._http.aclose()


def make() -> PacClient:
    s = get_settings()
    if s.PAC_PROVIDER == "facturama":
        return FacturamaClient()
    raise NotImplementedError(f"PAC '{s.PAC_PROVIDER}' aun no implementado")
