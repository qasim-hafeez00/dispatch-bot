"""
cortexbot/api/loads.py — Phase 2 Complete
Load status API with financial summary, filtering, and event history.
"""
import logging
from typing import Optional
from cortexbot.db.session import get_db_session
from cortexbot.db.models import Load, Event

logger = logging.getLogger("cortexbot.api.loads")


async def list_loads_handler(status: Optional[str] = None, carrier_id: Optional[str] = None) -> dict:
    from sqlalchemy import select
    async with get_db_session() as db:
        query = select(Load).order_by(Load.created_at.desc()).limit(100)
        if status:
            query = query.where(Load.status == status)
        if carrier_id:
            query = query.where(Load.carrier_id == carrier_id)

        result = await db.execute(query)
        loads = result.scalars().all()

    return {
        "loads": [
            {
                "load_id":     str(l.load_id),
                "tms_ref":     l.tms_ref,
                "status":      l.status,
                "carrier_id":  str(l.carrier_id) if l.carrier_id else None,
                "origin":      f"{l.origin_city}, {l.origin_state}" if l.origin_city else None,
                "destination": f"{l.destination_city}, {l.destination_state}" if l.destination_city else None,
                "pickup_date": str(l.pickup_date) if l.pickup_date else None,
                "commodity":   l.commodity,
                "weight_lbs":  l.weight_lbs,
                "agreed_rate": float(l.agreed_rate_cpm or 0),
                "loaded_miles": l.loaded_miles,
                "gross_revenue": round(float(l.agreed_rate_cpm or 0) * float(l.loaded_miles or 0), 2),
                "amount_paid": float(l.amount_paid or 0) if l.amount_paid else None,
                "created_at":  l.created_at.isoformat() if l.created_at else None,
                "dispatched_at": l.dispatched_at.isoformat() if l.dispatched_at else None,
                "delivered_at": l.delivered_at.isoformat() if hasattr(l, 'delivered_at') and l.delivered_at else None,
            }
            for l in loads
        ],
        "total": len(loads),
        "filters": {"status": status, "carrier_id": carrier_id},
    }


async def get_load_handler(load_id: str) -> dict:
    from sqlalchemy import select
    async with get_db_session() as db:
        result = await db.execute(select(Load).where(Load.load_id == load_id))
        load = result.scalar_one_or_none()
        if not load:
            return {"error": "Load not found"}

        events_result = await db.execute(
            select(Event)
            .where(Event.entity_id == load_id)
            .order_by(Event.created_at)
        )
        events = events_result.scalars().all()

    # Calculate financial summary
    gross_revenue = round(
        float(load.agreed_rate_cpm or 0) * float(load.loaded_miles or 0), 2
    )
    dispatch_fee  = round(gross_revenue * 0.06, 2)
    net_to_carrier = round(gross_revenue - dispatch_fee, 2)

    # Extract key events for timeline display
    event_map = {}
    for e in events:
        event_map[e.event_code] = {
            "timestamp": e.created_at.isoformat(),
            "data":      e.data,
        }

    return {
        "load_id":        str(load.load_id),
        "tms_ref":        load.tms_ref,
        "status":         load.status,
        "carrier_id":     str(load.carrier_id) if load.carrier_id else None,
        "broker_id":      str(load.broker_id)  if load.broker_id  else None,

        # Route
        "origin":         f"{load.origin_city}, {load.origin_state}" if load.origin_city else None,
        "destination":    f"{load.destination_city}, {load.destination_state}" if load.destination_city else None,
        "origin_address": load.origin_address,
        "destination_address": load.destination_address,
        "loaded_miles":   load.loaded_miles,
        "deadhead_miles": load.deadhead_miles,

        # Load specs
        "pickup_date":    str(load.pickup_date)   if load.pickup_date   else None,
        "delivery_date":  str(load.delivery_date) if load.delivery_date else None,
        "commodity":      load.commodity,
        "weight_lbs":     load.weight_lbs,
        "equipment_type": load.equipment_type,

        # Rate
        "agreed_rate_cpm":   float(load.agreed_rate_cpm or 0),
        "detention_free_hrs": load.detention_free_hrs,
        "detention_rate_hr":  float(load.detention_rate_hr or 0) if load.detention_rate_hr else None,
        "tonu_amount":        float(load.tonu_amount or 0) if load.tonu_amount else None,

        # Financial summary
        "financial": {
            "gross_revenue":    gross_revenue,
            "dispatch_fee_6pct": dispatch_fee,
            "net_to_carrier":   net_to_carrier,
            "amount_paid":      float(load.amount_paid or 0) if load.amount_paid else None,
            "payment_received": str(load.payment_received_date) if load.payment_received_date else None,
        },

        # Documents
        "documents": {
            "rc_url":        load.rc_url,
            "rc_signed_url": load.rc_signed_url,
            "pod_url":       load.pod_url if hasattr(load, "pod_url") else None,
        },

        # Timeline
        "timeline": {
            "searched":      load.searched_at.isoformat()         if load.searched_at         else None,
            "broker_called": load.broker_called_at.isoformat()    if load.broker_called_at    else None,
            "rate_agreed":   load.rate_agreed_at.isoformat()      if load.rate_agreed_at      else None,
            "confirmed":     load.carrier_confirmed_at.isoformat() if load.carrier_confirmed_at else None,
            "booked":        load.booked_at.isoformat()            if load.booked_at           else None,
            "rc_signed":     load.rc_signed_at.isoformat()         if load.rc_signed_at        else None,
            "dispatched":    load.dispatched_at.isoformat()        if load.dispatched_at       else None,
            "arrived_pickup": load.arrived_pickup_at.isoformat()   if hasattr(load, "arrived_pickup_at") and load.arrived_pickup_at else None,
            "delivered":     load.delivered_at.isoformat()         if hasattr(load, "delivered_at") and load.delivered_at else None,
        },

        # Full audit trail
        "events": [
            {
                "code":      e.event_code,
                "timestamp": e.created_at.isoformat(),
                "skill":     e.triggered_by,
                "data":      e.data,
                "status_change": f"{e.previous_status} → {e.new_status}" if e.previous_status and e.new_status else None,
            }
            for e in events
        ],
    }
