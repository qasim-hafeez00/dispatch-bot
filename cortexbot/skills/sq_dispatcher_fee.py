"""
cortexbot/skills/sq_dispatcher_fee.py

Skill Q — Dispatcher Fee Collection

FIX: This file was completely empty. Populated from sq_sr_ss_st_financial.py.
     Also adds the skill_q_collect_fee() entry point that the orchestrator
     imports but was missing, causing ImportError at runtime.
"""

import logging
from datetime import datetime, timezone

from cortexbot.db.session import get_db_session
from cortexbot.db.models import Carrier, Event

logger = logging.getLogger("cortexbot.skills.sq")


async def skill_q_dispatcher_fee(state: dict) -> dict:
    """
    Calculate the dispatch service fee for a completed load.
    Called after payment is received from the broker.
    """
    load_id = state["load_id"]
    carrier_id = state["carrier_id"]
    gross_revenue = float(
        state.get("invoice_amount") or state.get("amount_paid") or 0
    )

    async with get_db_session() as db:
        from sqlalchemy import select
        r = await db.execute(select(Carrier).where(Carrier.carrier_id == carrier_id))
        carrier = r.scalar_one_or_none()

    if not carrier:
        return {**state, "dispatch_fee": 0.0}

    fee_pct = float(carrier.dispatch_fee_pct or 0.06)
    fee_amount = round(gross_revenue * fee_pct, 2)
    net_carrier = round(gross_revenue - fee_amount, 2)
    tms_ref = state.get("tms_ref", load_id)

    logger.info(
        f"[Q] Fee: gross=${gross_revenue:.2f} fee={fee_pct*100:.0f}% "
        f"fee_amt=${fee_amount:.2f} net=${net_carrier:.2f}"
    )

    async with get_db_session() as db:
        db.add(Event(
            event_code="DISPATCH_FEE_CALCULATED",
            entity_type="load",
            entity_id=load_id,
            triggered_by="sq_dispatcher_fee",
            data={
                "gross_revenue": gross_revenue,
                "fee_pct": fee_pct,
                "fee_amount": fee_amount,
                "net_carrier": net_carrier,
                "calculated_at": datetime.now(timezone.utc).isoformat(),
            },
        ))

    return {
        **state,
        "dispatch_fee": fee_amount,
        "dispatch_fee_pct": fee_pct,
        "gross_revenue": gross_revenue,
        "net_carrier": net_carrier,
        "dispatch_fee_collected": True,
    }


# Alias used by orchestrator.py import
skill_q_collect_fee = skill_q_dispatcher_fee