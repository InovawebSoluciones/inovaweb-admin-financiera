"""Onboarding atomico con mocks de los 4 cores y compensacion."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_compensation_on_finanzas_failure():
    """Si finanzas falla, las cuentas creadas en medidor/hub se borran."""
    from app.services import onboarding

    med = AsyncMock()
    hub = AsyncMock()
    fin = AsyncMock()
    msg = AsyncMock()

    med.create_account.return_value = {"id": "med-1"}
    hub.create_account.return_value = {"id": "hub-1"}
    fin.create_account.side_effect = RuntimeError("PG cae")
    msg.create_account.return_value = {"id": "msg-1"}

    with patch.object(onboarding, "MedidorClient", return_value=med), \
         patch.object(onboarding, "HubClient", return_value=hub), \
         patch.object(onboarding, "FinanzasClient", return_value=fin), \
         patch.object(onboarding, "MessagesClient", return_value=msg):

        # validamos solo la compensacion directa, sin DB:
        created = {"medidor": "med-1", "hub": "hub-1"}
        await onboarding._compensate(
            created, med, hub, fin, msg, client_id=99, error="PG cae",
        )

    med.delete_account.assert_awaited_once_with("med-1")
    hub.delete_account.assert_awaited_once_with("hub-1")
    fin.delete_account.assert_not_called()
    msg.delete_account.assert_not_called()
