"""
cortexbot/skills/ss_driver_advance.py

Skill S — Driver Advance / Comchek Issuance

FIX: This file was completely empty.
"""

import logging
from datetime import datetime, timezone

from cortexbot.db.session import get_db_session
from cortexbot.db.models import Event

logger = logging.getLogger("cortexbot.skills.ss")

ADVANCE_LIMITS = {
    "FUEL": 400.0,
    "LUMPER": 300.0,
    "EMERGENCY": 500.0,
    "TOLL": 100.0,
    "REPAIR": 400.0,
}


async def skill_s_driver_advance(state: dict) -> dict:
    """
    Issue an EFS or Comdata advance code to the driver.
    Called when driver requests fuel, lumper, or emergency funds.
    """
    # Support both dict-style (from orchestrator) and keyword-style calls
    carrier_id = state.get("carrier_id", "")
    load_id = state.get("load_id", "")
    advance_type = state.get("advance_type", "FUEL").upper()
    amount_requested = float(state.get("amount_requested") or 200.0)
    carrier_wa = state.get("carrier_whatsapp", "")
    tms_ref = state.get("tms_ref", load_id)

    max_amt = ADVANCE_LIMITS.get(advance_type, 200.0)
    approved_amt = min(amount_requested, max_amt)

    logger.info(
        f"[S] Advance: type={advance_type} requested=${amount_requested:.2f}"
        f" approved=${approved_amt:.2f} carrier={carrier_id}"
    )

    # Issue code via EFS (falls back to Comdata)
    from cortexbot.integrations.comdata_efs_client import issue_fuel_advance
    fuel_code = await issue_fuel_advance(
        amount=approved_amt,
        advance_type=advance_type.lower(),
        carrier_id=carrier_id,
    )

    code_data = fuel_code.to_dict() if fuel_code else {
        "code": "CONTACT-DISPATCH",
        "network": "Manual",
        "expires_at": "N/A",
    }

    # Persist advance record
    async with get_db_session() as db:
        db.add(Event(
            event_code="DRIVER_ADVANCE_ISSUED",
            entity_type="load",
            entity_id=load_id,
            triggered_by="ss_driver_advance",
            data={
                "advance_type": advance_type,
                "amount": approved_amt,
                "code": code_data.get("code", ""),
                "network": code_data.get("network", "EFS"),
                "issued_at": datetime.now(timezone.utc).isoformat(),
            },
        ))

    # Send code to driver
    if carrier_wa:
        from cortexbot.integrations.twilio_client import send_whatsapp
        if fuel_code:
            msg = fuel_code.driver_instructions
        else:
            msg = (
                f"💳 ADVANCE — Load {tms_ref}\n"
                f"Type: {advance_type.title()} | Amount: ${approved_amt:.2f}\n"
                f"Contact dispatch for code: {__import__('cortexbot.config', fromlist=['settings']).settings.oncall_phone}"
            )
        await send_whatsapp(carrier_wa, msg)

    return {
        **state,
        "advance_issued": True,
        "advance_type": advance_type,
        "advance_amount": approved_amt,
        "advance_code": code_data.get("code"),
        "advance_network": code_data.get("network"),
    }