"""
cortexbot/api/carriers.py
Carrier CRUD API handlers.
"""
import logging
from uuid import UUID
from cortexbot.db.session import get_db_session
from cortexbot.db.models import Carrier

logger = logging.getLogger("cortexbot.api.carriers")


async def create_carrier_handler(payload: dict) -> dict:
    async with get_db_session() as db:
        carrier = Carrier(
            mc_number=payload["mc_number"],
            dot_number=payload.get("dot_number"),
            company_name=payload["company_name"],
            owner_name=payload["owner_name"],
            owner_email=payload["owner_email"],
            owner_phone=payload["owner_phone"],
            driver_phone=payload.get("driver_phone") or payload.get("owner_phone"),
            whatsapp_phone=payload.get("whatsapp_phone") or payload.get("owner_phone"),
            language_pref=payload.get("language_pref", "en"),
            equipment_type=payload["equipment_type"],
            max_weight_lbs=payload.get("max_weight_lbs", 44000),
            home_base_city=payload.get("home_base_city"),
            home_base_state=payload.get("home_base_state"),
            preferred_dest_states=payload.get("preferred_dest_states", []),
            avoid_states=payload.get("avoid_states", []),
            rate_floor_cpm=payload["rate_floor_cpm"],
            max_deadhead_mi=payload.get("max_deadhead_mi", 100),
            no_touch_only=payload.get("no_touch_only", False),
            hazmat_cert=payload.get("hazmat_cert", False),
            twic_card=payload.get("twic_card", False),
            factoring_company=payload.get("factoring_company"),
            dispatch_fee_pct=payload.get("dispatch_fee_pct", 0.06),
            status="ACTIVE",
        )
        db.add(carrier)
        await db.commit()
        return {
            "carrier_id": str(carrier.carrier_id),
            "mc_number": carrier.mc_number,
            "company_name": carrier.company_name,
            "status": carrier.status,
            "message": "Carrier created successfully",
        }


async def list_carriers_handler() -> dict:
    from sqlalchemy import select
    async with get_db_session() as db:
        result = await db.execute(
            select(Carrier).where(Carrier.status == "ACTIVE").order_by(Carrier.created_at.desc())
        )
        carriers = result.scalars().all()
        return {
            "carriers": [
                {
                    "carrier_id":    str(c.carrier_id),
                    "mc_number":     c.mc_number,
                    "company_name":  c.company_name,
                    "equipment_type": c.equipment_type,
                    "home_base":     f"{c.home_base_city}, {c.home_base_state}" if c.home_base_city else None,
                    "rate_floor_cpm": float(c.rate_floor_cpm or 0),
                    "status":        c.status,
                }
                for c in carriers
            ],
            "total": len(carriers),
        }


async def get_carrier_handler(carrier_id: str) -> dict:
    from sqlalchemy import select
    async with get_db_session() as db:
        result = await db.execute(select(Carrier).where(Carrier.carrier_id == carrier_id))
        carrier = result.scalar_one_or_none()
        if not carrier:
            return {"error": "Carrier not found"}
        return {
            "carrier_id":     str(carrier.carrier_id),
            "mc_number":      carrier.mc_number,
            "company_name":   carrier.company_name,
            "owner_name":     carrier.owner_name,
            "equipment_type": carrier.equipment_type,
            "rate_floor_cpm": float(carrier.rate_floor_cpm or 0),
            "status":         carrier.status,
            "home_base_city": carrier.home_base_city,
            "home_base_state": carrier.home_base_state,
        }
