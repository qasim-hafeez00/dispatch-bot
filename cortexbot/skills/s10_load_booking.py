"""
cortexbot/skills/s10_load_booking.py

Skill 10 — Load Booking: creates the TMS record when carrier confirms.
"""
import logging
from datetime import datetime, timezone
from cortexbot.db.session import get_db_session
from cortexbot.db.models import Load, Broker, BrokerContact, Event
from cortexbot.integrations.sendgrid_client import send_email

logger = logging.getLogger("cortexbot.skills.s10")


async def skill_10_load_booking(state: dict) -> dict:
    load_id    = state["load_id"]
    details    = state.get("load_details_extracted", {})
    agreed_cpm = state.get("agreed_rate_cpm")
    access     = state.get("locked_accessorials", {})

    logger.info(f"📝 [S10] Booking load {load_id}")

    async with get_db_session() as db:
        from sqlalchemy import update as sa_update, select

        # Upsert broker record
        broker_mc = state.get("broker_mc", "")
        if broker_mc:
            r = await db.execute(select(Broker).where(Broker.mc_number == broker_mc))
            broker = r.scalar_one_or_none()
            if not broker:
                broker = Broker(
                    mc_number=broker_mc,
                    company_name=state.get("broker_company", "Unknown Broker"),
                )
                db.add(broker)
                await db.flush()

            # Upsert contact
            contact_name  = details.get("broker_contact_name") or state.get("broker_contact_name")
            contact_email = details.get("broker_rc_email") or state.get("broker_email")
            contact_phone = state.get("broker_phone")

            if contact_name or contact_email:
                contact = BrokerContact(
                    broker_id=broker.broker_id,
                    name=contact_name,
                    email=contact_email,
                    phone=contact_phone,
                )
                db.add(contact)
                await db.flush()
                broker_contact_id = contact.contact_id
            else:
                broker_contact_id = None

            broker_id = broker.broker_id
        else:
            broker_id = None
            broker_contact_id = None

        # Update the load record with all negotiated details
        await db.execute(
            sa_update(Load).where(Load.load_id == load_id).values(
                broker_id=broker_id,
                broker_contact_id=broker_contact_id,
                broker_load_ref=details.get("load_reference"),
                status="BOOKED",
                booked_at=datetime.now(timezone.utc),
                agreed_rate_cpm=agreed_cpm,
                detention_free_hrs=access.get("detention_free_hrs", 2),
                detention_rate_hr=access.get("detention_rate"),
                tonu_amount=access.get("tonu_amount"),
                lumper_payer=access.get("lumper_payer"),
                commodity=details.get("commodity"),
                weight_lbs=details.get("weight_lbs"),
                load_type=details.get("load_type"),
                tracking_method=details.get("tracking_requirement"),
                payment_terms_days=_parse_net_days(details.get("payment_terms", "")),
                factoring_allowed=details.get("factoring_allowed", True),
            )
        )

        db.add(Event(
            event_code="LOAD_BOOKED",
            entity_type="load",
            entity_id=load_id,
            triggered_by="s10_load_booking",
            data={"agreed_rate_cpm": float(agreed_cpm) if agreed_cpm else None,
                  "broker_mc": broker_mc},
            new_status="BOOKED",
        ))
        await db.commit()

    # Notify carrier of booking confirmation
    carrier_wa = state.get("carrier_whatsapp")
    if carrier_wa:
        from cortexbot.integrations.twilio_client import send_whatsapp
        origin = state.get("origin_city", "?")
        dest   = state.get("destination_city", "?")
        await send_whatsapp(
            carrier_wa,
            f"✅ BOOKED — {origin} → {dest}\n"
            f"Rate: ${agreed_cpm:.2f}/mi\n"
            f"Broker: {state.get('broker_company', '?')}\n"
            f"Dispatch sheet coming shortly. Watch for it!"
        )

    return {**state, "status": "BOOKED", "broker_id": str(broker_id) if broker_id else None}


def _parse_net_days(terms: str) -> int:
    """Extract number from 'Net 30', 'Net 15', etc."""
    import re
    match = re.search(r"\d+", (terms or ""))
    return int(match.group()) if match else 30
