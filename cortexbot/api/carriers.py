"""
cortexbot/api/carriers.py
Carrier CRUD API handlers.

GAP FIX — onboard_carrier_handler:
  Added a complete Step 2 onboarding handler that:
  1. Creates the carrier record with ALL profile fields (equipment, lanes,
     constraints, communication preferences) from the onboarding form.
  2. Uploads documents (W-9, COI, NOA, CDL) to S3 from base64 payloads.
  3. Creates CarrierDocument records for each uploaded doc with expiry dates.
  4. Triggers Agent AA (service agreement via DocuSign).
  5. Sends a welcome SMS/WhatsApp to confirm next steps.

  create_carrier_handler updated to accept all new Carrier model fields
  (tarp_capable, reefer_temp, team_capable, avoid_nyc, canada_ok, etc.)
"""
import base64
import io
import logging
from uuid import UUID

import boto3

from cortexbot.config import settings
from cortexbot.db.session import get_db_session
from cortexbot.db.models import Carrier, CarrierDocument, Event
from cortexbot.integrations.twilio_client import send_whatsapp, send_sms

logger = logging.getLogger("cortexbot.api.carriers")


async def create_carrier_handler(payload: dict) -> dict:
    """Basic carrier create — accepts all profile fields including new ones from migration 006."""
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
            # ── New fields from migration 006 ──────────────────
            tarp_capable=payload.get("tarp_capable", False),
            straps_count=payload.get("straps_count"),
            load_locks_qty=payload.get("load_locks_qty"),
            team_capable=payload.get("team_capable", False),
            reefer_temp_min_f=payload.get("reefer_temp_min_f"),
            reefer_temp_max_f=payload.get("reefer_temp_max_f"),
            max_loaded_length_ft=payload.get("max_loaded_length_ft", 53),
            commodity_exclusions=payload.get("commodity_exclusions", []),
            avoid_nyc=payload.get("avoid_nyc", False),
            avoid_ports=payload.get("avoid_ports", False),
            canada_ok=payload.get("canada_ok", False),
            preferred_home_time_days=payload.get("preferred_home_time_days"),
            comm_start_hour=payload.get("comm_start_hour"),
            comm_end_hour=payload.get("comm_end_hour"),
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


async def onboard_carrier_handler(payload: dict) -> dict:
    """
    Step 2 automation: full carrier onboarding.

    Accepts the complete onboarding form including document uploads (base64
    encoded), creates the Carrier record, stores documents in S3, records
    CarrierDocument entries, and triggers Agent AA (service agreement).

    Expected payload keys:
      Core identity:  mc_number, dot_number, company_name, owner_name,
                      owner_email, owner_phone, driver_phone, whatsapp_phone
      Equipment:      equipment_type, max_weight_lbs, max_loaded_length_ft,
                      tarp_capable, straps_count, load_locks_qty, team_capable,
                      reefer_temp_min_f, reefer_temp_max_f
      Preferences:    rate_floor_cpm, max_deadhead_mi, home_base_city,
                      home_base_state, preferred_dest_states, avoid_states,
                      commodity_exclusions, no_touch_only, hazmat_cert, twic_card,
                      avoid_nyc, avoid_ports, canada_ok, preferred_home_time_days,
                      comm_start_hour, comm_end_hour
      Financial:      factoring_company, dispatch_fee_pct
      Documents:      w9_b64, coi_auto_b64, coi_cargo_b64, noa_b64, cdl_b64
                      + coi_expiry_date (ISO date string)
    """
    mc_number = payload["mc_number"]
    logger.info(f"[onboard] Starting full onboarding for MC#{mc_number}")

    # ── 1. Create carrier record ──────────────────────────────
    async with get_db_session() as db:
        from sqlalchemy import select
        existing = await db.execute(select(Carrier).where(Carrier.mc_number == mc_number))
        if existing.scalar_one_or_none():
            return {"error": f"Carrier MC#{mc_number} already exists", "code": "DUPLICATE_MC"}

        carrier = Carrier(
            mc_number=mc_number,
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
            tarp_capable=payload.get("tarp_capable", False),
            straps_count=payload.get("straps_count"),
            load_locks_qty=payload.get("load_locks_qty"),
            team_capable=payload.get("team_capable", False),
            reefer_temp_min_f=payload.get("reefer_temp_min_f"),
            reefer_temp_max_f=payload.get("reefer_temp_max_f"),
            max_loaded_length_ft=payload.get("max_loaded_length_ft", 53),
            commodity_exclusions=payload.get("commodity_exclusions", []),
            avoid_nyc=payload.get("avoid_nyc", False),
            avoid_ports=payload.get("avoid_ports", False),
            canada_ok=payload.get("canada_ok", False),
            preferred_home_time_days=payload.get("preferred_home_time_days"),
            comm_start_hour=payload.get("comm_start_hour"),
            comm_end_hour=payload.get("comm_end_hour"),
            status="PENDING_AGREEMENT",
        )
        db.add(carrier)
        await db.flush()  # get carrier_id before commit
        carrier_id = str(carrier.carrier_id)

        # ── 2. Upload documents to S3 + create CarrierDocument records ──
        doc_results = await _upload_onboarding_docs(carrier_id, mc_number, payload)
        for doc in doc_results:
            db.add(CarrierDocument(
                carrier_id=carrier_id,
                document_type=doc["type"],
                s3_url=doc["s3_url"],
                expiry_date=doc.get("expiry_date"),
                verified=False,
            ))

        # Update convenience URL columns on carrier
        for doc in doc_results:
            if doc["type"] == "W9":
                carrier.w9_url = doc["s3_url"]
            elif doc["type"] in ("COI_AUTO", "COI_CARGO"):
                carrier.coi_url = doc["s3_url"]
            elif doc["type"] == "NOA":
                carrier.factoring_noa_url = doc["s3_url"]
            elif doc["type"] == "CDL":
                carrier.cdl_url = doc["s3_url"]

        db.add(Event(
            event_code="CARRIER_ONBOARDED",
            entity_type="carrier",
            entity_id=carrier_id,
            triggered_by="onboard_carrier_handler",
            data={"mc_number": mc_number, "docs_uploaded": [d["type"] for d in doc_results]},
            new_status="PENDING_AGREEMENT",
        ))

    # ── 3. Trigger service agreement (Agent AA) ───────────────
    try:
        from cortexbot.agents.service_agreement import skill_aa_generate_agreement
        agreement_result = await skill_aa_generate_agreement(carrier_id)
        envelope_id = agreement_result.get("envelope_id")
    except Exception as e:
        logger.error(f"[onboard] Agent AA failed for {carrier_id}: {e}")
        envelope_id = None

    # ── 4. Welcome message ────────────────────────────────────
    wa_phone = payload.get("whatsapp_phone") or payload.get("owner_phone")
    if wa_phone:
        await send_whatsapp(
            wa_phone,
            f"👋 Hi {payload['owner_name'].split()[0]}!\n\n"
            f"We received your onboarding information for {payload['company_name']}.\n\n"
            f"Next steps:\n"
            f"✅ Check your email ({payload['owner_email']}) for a Service Agreement to sign\n"
            f"✅ Once signed, we'll start finding loads for you immediately\n\n"
            f"Questions? Reply here or call {settings.oncall_phone}. We're available 24/7. 🚛"
        )

    logger.info(f"[onboard] MC#{mc_number} onboarded. carrier_id={carrier_id} envelope={envelope_id}")
    return {
        "carrier_id":    carrier_id,
        "mc_number":     mc_number,
        "status":        "PENDING_AGREEMENT",
        "docs_uploaded": [d["type"] for d in doc_results],
        "agreement_envelope": envelope_id,
        "message":       "Carrier onboarded. Service agreement sent for signature.",
    }


async def _upload_onboarding_docs(carrier_id: str, mc_number: str, payload: dict) -> list:
    """
    Upload base64-encoded documents from the onboarding form to S3.
    Returns list of {type, s3_url, expiry_date} dicts.
    """
    import asyncio
    from datetime import date

    doc_map = {
        "w9_b64":         ("W9",        None),
        "coi_auto_b64":   ("COI_AUTO",  payload.get("coi_expiry_date")),
        "coi_cargo_b64":  ("COI_CARGO", payload.get("coi_expiry_date")),
        "noa_b64":        ("NOA",       None),
        "cdl_b64":        ("CDL",       payload.get("cdl_expiry_date")),
    }

    results = []
    s3 = boto3.client(
        "s3",
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    )

    loop = asyncio.get_running_loop()

    for field_key, (doc_type, expiry_str) in doc_map.items():
        b64_data = payload.get(field_key)
        if not b64_data:
            continue
        try:
            pdf_bytes = base64.b64decode(b64_data)
            s3_key    = f"carriers/{carrier_id}/{doc_type.lower()}.pdf"
            await loop.run_in_executor(
                None,
                lambda key=s3_key, data=pdf_bytes: s3.put_object(
                    Bucket=settings.aws_s3_bucket,
                    Key=key,
                    Body=data,
                    ContentType="application/pdf",
                ),
            )
            expiry_date = None
            if expiry_str:
                try:
                    expiry_date = date.fromisoformat(expiry_str)
                except Exception:
                    pass
            results.append({
                "type":        doc_type,
                "s3_url":      f"s3://{settings.aws_s3_bucket}/{s3_key}",
                "expiry_date": expiry_date,
            })
            logger.info(f"[onboard] Uploaded {doc_type} for carrier {carrier_id}")
        except Exception as e:
            logger.warning(f"[onboard] Failed to upload {doc_type} for {carrier_id}: {e}")

    return results


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
