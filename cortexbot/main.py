"""
cortexbot/main.py  — PHASE 2 COMPLETE

FastAPI application entry point.
Phase 2 adds:
  - Transit monitoring internal routes
  - Payment follow-up routes
  - Compliance + claims routes
  - Fraud check route
  - Updated orchestrator wiring at DISPATCHED state
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from cortexbot.config import settings
from cortexbot.db.session import init_db, close_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("cortexbot")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"🚀 Starting CortexBot {settings.app_version} ({settings.environment})")
    await init_db()

    from cortexbot.core.redis_client import init_redis, close_redis
    await init_redis()

    logger.info("✅ CortexBot ready — all systems online")
    yield

    logger.info("🛑 Shutting down CortexBot...")
    await close_db()
    from cortexbot.core.redis_client import close_redis
    await close_redis()
    logger.info("✅ Shutdown complete")


app = FastAPI(
    title="CortexBot — Autonomous Truck Dispatch",
    description="AI-powered truck dispatch system — Phase 2: full lifecycle from search to payment.",
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.is_development else [settings.base_url],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "message": str(exc) if settings.is_development else "Something went wrong",
        }
    )


# ============================================================
# HEALTH
# ============================================================

@app.get("/health", tags=["System"])
async def health_check():
    return {"status": "ok", "version": settings.app_version, "environment": settings.environment}


@app.get("/health/deep", tags=["System"])
async def deep_health_check():
    results = {"status": "ok", "checks": {}}
    try:
        from cortexbot.db.session import engine
        from sqlalchemy import text
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        results["checks"]["database"] = "ok"
    except Exception as e:
        results["checks"]["database"] = f"error: {e}"
        results["status"] = "degraded"

    try:
        from cortexbot.core.redis_client import get_redis
        await get_redis().ping()
        results["checks"]["redis"] = "ok"
    except Exception as e:
        results["checks"]["redis"] = f"error: {e}"
        results["status"] = "degraded"

    api_keys = {
        "anthropic": bool(settings.anthropic_api_key),
        "bland_ai":  bool(settings.bland_ai_api_key),
        "twilio":    bool(settings.twilio_account_sid),
        "sendgrid":  bool(settings.sendgrid_api_key),
        "dat":       bool(settings.dat_client_id),
        "aws":       bool(settings.aws_access_key_id),
    }
    results["checks"]["api_keys"] = api_keys
    return results


# ============================================================
# WEBHOOKS — Bland AI, Twilio, SendGrid, DocuSign
# ============================================================

@app.post("/webhooks/bland/call-complete", tags=["Webhooks"])
async def bland_call_complete(request: Request):
    from cortexbot.webhooks.bland_ai import handle_call_complete
    payload = await request.json()
    logger.info(f"📞 Bland AI webhook: call_id={payload.get('call_id')}")
    asyncio.create_task(handle_call_complete(payload))
    return {"received": True}


@app.post("/webhooks/twilio/whatsapp", tags=["Webhooks"])
async def twilio_whatsapp_inbound(request: Request):
    from cortexbot.webhooks.twilio import handle_whatsapp_inbound
    form_data = await request.form()
    payload = dict(form_data)
    from_number = payload.get("From", "").replace("whatsapp:", "")
    body = payload.get("Body", "")
    logger.info(f"💬 WhatsApp from {from_number}: '{body[:50]}'")
    asyncio.create_task(handle_whatsapp_inbound(payload))
    return JSONResponse(content={"message": "received"})


@app.post("/webhooks/sendgrid/inbound", tags=["Webhooks"])
async def sendgrid_inbound_email(request: Request):
    from cortexbot.webhooks.sendgrid import handle_inbound_email
    form_data = await request.form()
    payload = dict(form_data)
    logger.info(f"📧 Email from {payload.get('from', '')}: '{payload.get('subject', '')[:50]}'")
    asyncio.create_task(handle_inbound_email(payload))
    return {"received": True}


@app.post("/webhooks/docusign/complete", tags=["Webhooks"])
async def docusign_signature_complete(request: Request):
    from cortexbot.webhooks.docusign import handle_signature_complete
    payload = await request.json()
    asyncio.create_task(handle_signature_complete(payload))
    return {"received": True}


# ============================================================
# INTERNAL ROUTES — Bland AI mid-call data injection
# ============================================================

@app.post("/internal/rate-data", tags=["Internal"])
async def get_live_rate_data(request: Request):
    """Called BY Bland AI during a live broker call for live DAT rates."""
    from cortexbot.skills.s07_rate_intelligence import get_rate_brief
    payload = await request.json()
    return await get_rate_brief(
        payload.get("origin_city", ""),
        payload.get("dest_city", ""),
        payload.get("equipment", "53_dry_van"),
    )


# ============================================================
# INTERNAL ROUTES — BullMQ Worker triggers
# ============================================================

@app.post("/internal/transit-monitor", tags=["Internal"])
async def internal_transit_monitor(request: Request):
    """
    Trigger a GPS/ETA check for an active load.
    Called by BullMQ transit_monitor queue every 15 minutes.
    """
    payload  = await request.json()
    load_id  = payload.get("load_id")
    if not load_id:
        return JSONResponse(status_code=422, content={"error": "load_id required"})

    from cortexbot.skills.s15_in_transit_monitoring import skill_15_gps_check
    asyncio.create_task(skill_15_gps_check(load_id))
    return {"load_id": load_id, "check_initiated": True}


@app.post("/internal/hos-check", tags=["Internal"])
async def internal_hos_check(request: Request):
    """
    Check HOS compliance for a driver.
    Called by BullMQ hos_check queue every 15 minutes per active load.
    """
    payload    = await request.json()
    driver_id  = payload.get("driver_id")
    load_id    = payload.get("load_id")
    if not driver_id or not load_id:
        return JSONResponse(status_code=422, content={"error": "driver_id and load_id required"})

    from cortexbot.skills.s14_hos_compliance import skill_14_hos_check
    asyncio.create_task(skill_14_hos_check(driver_id, load_id))
    return {"driver_id": driver_id, "load_id": load_id, "check_initiated": True}


@app.post("/internal/weather-check", tags=["Internal"])
async def internal_weather_check(request: Request):
    """
    Run weather route scan for an active load.
    Called by BullMQ weather_check queue every 30 minutes.
    """
    payload = await request.json()
    load_id = payload.get("load_id")
    if not load_id:
        return JSONResponse(status_code=422, content={"error": "load_id required"})

    from cortexbot.skills.s21_s22_s23_ops import skill_23_weather_check
    asyncio.create_task(skill_23_weather_check(load_id))
    return {"load_id": load_id, "check_initiated": True}


@app.post("/internal/payment-followup", tags=["Internal"])
async def internal_payment_followup(request: Request):
    """
    Advance payment follow-up sequence for an invoice.
    Called by BullMQ payment_followup queue on scheduled dates.
    """
    payload    = await request.json()
    invoice_id = payload.get("invoice_id")
    step       = payload.get("step")
    if not invoice_id:
        return JSONResponse(status_code=422, content={"error": "invoice_id required"})

    from cortexbot.skills.s19_payment_reconciliation import run_followup_step
    asyncio.create_task(run_followup_step(invoice_id, step))
    return {"invoice_id": invoice_id, "step": step, "initiated": True}


@app.post("/internal/compliance-sweep", tags=["Internal"])
async def internal_compliance_sweep():
    """
    Run daily compliance sweep for all carriers.
    Called by BullMQ compliance_sweep queue at 06:00 UTC daily.
    """
    from cortexbot.skills.s26_s27_compliance_accessorials import skill_26_daily_compliance_sweep
    result = await skill_26_daily_compliance_sweep()
    return result


@app.post("/internal/claims-deadline-check", tags=["Internal"])
async def internal_claims_deadline_check():
    """
    Check freight claim deadlines daily.
    Called by BullMQ compliance_sweep queue (piggybacks on compliance run).
    """
    from cortexbot.skills.sy_freight_claims import skill_y_daily_deadline_check
    result = await skill_y_daily_deadline_check()
    return result


@app.post("/internal/carrier-performance", tags=["Internal"])
async def internal_carrier_performance(request: Request):
    """
    Run weekly carrier performance scoring.
    Called by BullMQ carrier_performance queue every Monday at 07:00.
    """
    payload    = await request.json()
    carrier_id = payload.get("carrier_id")  # None = score all
    from cortexbot.skills.s24_s25_relationship_scoring import skill_25_carrier_performance_scoring
    result = await skill_25_carrier_performance_scoring(carrier_id)
    return result


@app.post("/internal/broker-scoring", tags=["Internal"])
async def internal_broker_scoring(request: Request):
    """
    Run weekly broker relationship scoring.
    Called by BullMQ weekly cron.
    """
    payload   = await request.json()
    broker_id = payload.get("broker_id")  # None = score all
    from cortexbot.skills.s24_s25_relationship_scoring import skill_24_broker_relationship_management
    result = await skill_24_broker_relationship_management(broker_id)
    return result


@app.post("/internal/backhaul-search", tags=["Internal"])
async def internal_backhaul_search(request: Request):
    """
    Search for backhaul load from delivery city.
    Called by BullMQ backhaul_search queue at booking time.
    """
    payload   = await request.json()
    load_id   = payload.get("load_id")
    carrier_id = payload.get("carrier_id")

    from cortexbot.skills.s21_s22_s23_ops import skill_21_backhaul_planning
    state = {"load_id": load_id, "carrier_id": carrier_id, **payload}
    asyncio.create_task(skill_21_backhaul_planning(state))
    return {"load_id": load_id, "initiated": True}


@app.post("/internal/process-email/{email_id}", tags=["Internal"])
async def internal_process_email(email_id: str):
    from cortexbot.db.session import get_db_session
    from cortexbot.db.models import InboundEmail
    from sqlalchemy import select

    async with get_db_session() as db:
        result = await db.execute(select(InboundEmail).where(InboundEmail.email_id == email_id))
        email = result.scalar_one_or_none()

    if not email:
        return JSONResponse(status_code=404, content={"error": f"Email {email_id} not found"})

    from cortexbot.webhooks.sendgrid import handle_inbound_email
    asyncio.create_task(handle_inbound_email({
        "from": email.from_email,
        "subject": email.subject,
        "text": email.body_text or "",
    }))
    return {"email_id": email_id, "reprocessing": True}


@app.post("/internal/process-ocr", tags=["Internal"])
async def internal_process_ocr(request: Request):
    payload = await request.json()
    load_id = payload.get("load_id")
    s3_url  = payload.get("s3_url")

    if not load_id or not s3_url:
        return JSONResponse(status_code=422, content={"error": "load_id and s3_url required"})

    from cortexbot.core.orchestrator import resume_workflow_after_rc
    asyncio.create_task(resume_workflow_after_rc(load_id, s3_url))
    return {"load_id": load_id, "s3_url": s3_url, "ocr_started": True}


@app.post("/internal/fraud-check", tags=["Internal"])
async def internal_fraud_check(request: Request):
    """
    Run fraud detection on a broker before booking.
    Called by orchestrator before skill 10 (load booking).
    """
    payload    = await request.json()
    broker_mc  = payload.get("broker_mc")
    load_id    = payload.get("load_id")
    carrier_mc = payload.get("carrier_mc")

    if not broker_mc:
        return JSONResponse(status_code=422, content={"error": "broker_mc required"})

    from cortexbot.skills.sx_fraud_detection import skill_x_fraud_detection
    result = await skill_x_fraud_detection(broker_mc, load_id, carrier_mc)
    return result


# ============================================================
# CARRIER MANAGEMENT API
# ============================================================

@app.post("/api/carriers", tags=["Carriers"])
async def create_carrier(request: Request):
    from cortexbot.api.carriers import create_carrier_handler
    payload = await request.json()
    return await create_carrier_handler(payload)


@app.get("/api/carriers", tags=["Carriers"])
async def list_carriers():
    from cortexbot.api.carriers import list_carriers_handler
    return await list_carriers_handler()


@app.get("/api/carriers/{carrier_id}", tags=["Carriers"])
async def get_carrier(carrier_id: str):
    from cortexbot.api.carriers import get_carrier_handler
    return await get_carrier_handler(carrier_id)


@app.post("/api/carriers/{carrier_id}/compliance-doc", tags=["Carriers"])
async def add_compliance_doc(carrier_id: str, request: Request):
    """Add or update a carrier compliance document."""
    payload = await request.json()
    from cortexbot.skills.s26_s27_compliance_accessorials import upsert_compliance_doc
    from datetime import date as date_type
    import datetime

    expiry_str = payload.get("expiry_date")
    expiry = datetime.date.fromisoformat(expiry_str) if expiry_str else None

    success = await upsert_compliance_doc(
        carrier_id=carrier_id,
        doc_type=payload["doc_type"],
        expiry_date=expiry,
        doc_url=payload.get("doc_url"),
        issuer=payload.get("issuer"),
    )
    return {"success": success, "carrier_id": carrier_id, "doc_type": payload["doc_type"]}


# ============================================================
# DISPATCH TRIGGERS
# ============================================================

@app.post("/api/dispatch/start/{carrier_id}", tags=["Dispatch"])
async def start_dispatch(carrier_id: str, request: Request):
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass

    from cortexbot.core.orchestrator import start_dispatch_workflow
    result = await start_dispatch_workflow(
        carrier_id=carrier_id,
        current_city=body.get("current_city"),
        current_state=body.get("current_state"),
    )
    return result


@app.post("/api/dispatch/record-payment/{load_id}", tags=["Dispatch"])
async def record_payment(load_id: str, request: Request):
    """Record payment received for a load. Triggers settlement pipeline."""
    payload = await request.json()
    invoice_id = payload.get("invoice_id")
    amount_paid = payload.get("amount_paid")

    if not invoice_id or not amount_paid:
        return JSONResponse(status_code=422, content={"error": "invoice_id and amount_paid required"})

    from cortexbot.skills.s19_payment_reconciliation import record_payment_received
    result = await record_payment_received(invoice_id, float(amount_paid))
    return result


@app.post("/api/dispatch/open-claim/{load_id}", tags=["Dispatch"])
async def open_freight_claim(load_id: str, request: Request):
    """Open a freight claim for a load."""
    payload = await request.json()
    from cortexbot.skills.sy_freight_claims import skill_y_open_freight_claim
    result = await skill_y_open_freight_claim(
        load_id=load_id,
        claim_type=payload["claim_type"],
        claimed_by=payload.get("claimed_by", "broker"),
        claimed_amount=float(payload["claimed_amount"]),
        reported_description=payload.get("description", ""),
    )
    return result


# ============================================================
# LOADS API
# ============================================================

@app.get("/api/loads", tags=["Loads"])
async def list_loads(status: str = None, carrier_id: str = None):
    from cortexbot.api.loads import list_loads_handler
    return await list_loads_handler(status=status, carrier_id=carrier_id)


@app.get("/api/loads/{load_id}", tags=["Loads"])
async def get_load(load_id: str):
    from cortexbot.api.loads import get_load_handler
    return await get_load_handler(load_id)


@app.get("/api/loads/{load_id}/invoice", tags=["Loads"])
async def get_load_invoice(load_id: str):
    """Get invoice details for a load."""
    from sqlalchemy import select, text as sa_text
    async with __import__("cortexbot.db.session", fromlist=["get_db_session"]).get_db_session() as db:
        result = await db.execute(sa_text("""
            SELECT invoice_id, invoice_number, total_amount, status,
                   due_date, amount_paid, payment_received_date, days_to_pay
            FROM invoices WHERE load_id = :lid LIMIT 1
        """), {"lid": load_id})
        row = result.fetchone()

    if not row:
        return JSONResponse(status_code=404, content={"error": "No invoice found for this load"})

    return {
        "invoice_id":     str(row[0]),
        "invoice_number": row[1],
        "total_amount":   float(row[2] or 0),
        "status":         row[3],
        "due_date":       str(row[4]) if row[4] else None,
        "amount_paid":    float(row[5]) if row[5] else None,
        "payment_date":   str(row[6]) if row[6] else None,
        "days_to_pay":    row[7],
    }


# ============================================================
# DRIVER ADVANCES
# ============================================================

@app.post("/api/advances/request", tags=["Advances"])
async def request_driver_advance(request: Request):
    """
    Issue a fuel/lumper/emergency advance to a driver.
    Returns EFS or Comdata check code.
    """
    payload = await request.json()
    from cortexbot.skills.sq_sr_ss_st_financial import skill_s_driver_advance
    result = await skill_s_driver_advance({
        "carrier_id":       payload["carrier_id"],
        "load_id":          payload.get("load_id"),
        "advance_type":     payload.get("advance_type", "FUEL"),
        "amount_requested": float(payload.get("amount", 200)),
        "carrier_whatsapp": payload.get("carrier_whatsapp", ""),
    })
    return result


# ============================================================
# DEBUG ROUTES (Development only)
# ============================================================

@app.post("/debug/simulate/whatsapp", tags=["Debug"])
async def simulate_whatsapp(request: Request):
    if not settings.is_development:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    payload = await request.json()
    from cortexbot.webhooks.twilio import handle_whatsapp_inbound
    await handle_whatsapp_inbound({
        "From":      f"whatsapp:{payload['from']}",
        "Body":      payload["body"],
        "NumMedia":  str(len(payload.get("media_urls", []))),
    })
    return {"simulated": True}


@app.post("/debug/simulate/email", tags=["Debug"])
async def simulate_email(request: Request):
    if not settings.is_development:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    payload = await request.json()
    from cortexbot.webhooks.sendgrid import handle_inbound_email
    await handle_inbound_email(payload)
    return {"simulated": True}


@app.post("/debug/start-workflow", tags=["Debug"])
async def debug_start_workflow(request: Request):
    if not settings.is_development:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    payload = await request.json()
    from cortexbot.core.orchestrator import start_dispatch_workflow
    return await start_dispatch_workflow(
        carrier_id=payload["carrier_id"],
        current_city=payload.get("current_city"),
        current_state=payload.get("current_state"),
    )


@app.get("/debug/load/{load_id}/state", tags=["Debug"])
async def debug_load_state(load_id: str):
    if not settings.is_development:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    from cortexbot.core.redis_client import get_state
    state = await get_state(f"cortex:state:load:{load_id}")
    if not state:
        return JSONResponse(status_code=404, content={"error": "No state found"})
    return state


@app.post("/debug/load/{load_id}/inject-rc", tags=["Debug"])
async def debug_inject_rc(load_id: str, request: Request):
    if not settings.is_development:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    payload = await request.json()
    s3_url = payload.get("s3_url", "s3://cortexbot-docs/samples/sample_rc.pdf")
    from cortexbot.core.orchestrator import resume_workflow_after_rc
    await resume_workflow_after_rc(load_id, s3_url)
    return {"load_id": load_id, "rc_injected": True, "s3_url": s3_url}


@app.post("/debug/simulate/carrier-yes/{load_id}", tags=["Debug"])
async def simulate_carrier_yes(load_id: str):
    if not settings.is_development:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    from cortexbot.core.redis_client import publish_carrier_decision
    await publish_carrier_decision(load_id, "CONFIRMED", "YES (simulated)")
    return {"load_id": load_id, "decision": "CONFIRMED", "simulated": True}


@app.post("/debug/simulate/carrier-no/{load_id}", tags=["Debug"])
async def simulate_carrier_no(load_id: str):
    if not settings.is_development:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    from cortexbot.core.redis_client import publish_carrier_decision
    await publish_carrier_decision(load_id, "REJECTED", "NO (simulated)")
    return {"load_id": load_id, "decision": "REJECTED", "simulated": True}


@app.post("/debug/simulate/delivery/{load_id}", tags=["Debug"])
async def simulate_delivery(load_id: str):
    """Simulate driver confirming delivery — triggers POD + invoice pipeline."""
    if not settings.is_development:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    from cortexbot.core.orchestrator_phase2 import handle_delivery_confirmed
    asyncio.create_task(handle_delivery_confirmed(load_id))
    return {"load_id": load_id, "delivery_simulated": True}


@app.post("/debug/simulate/fraud-check", tags=["Debug"])
async def simulate_fraud_check(request: Request):
    """Test fraud detection on a broker MC."""
    if not settings.is_development:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    payload = await request.json()
    from cortexbot.skills.sx_fraud_detection import skill_x_fraud_detection
    return await skill_x_fraud_detection(payload["broker_mc"])


@app.get("/debug/queue/depths", tags=["Debug"])
async def debug_queue_depths():
    if not settings.is_development:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    from cortexbot.core.redis_client import get_redis
    r = get_redis()
    keys = await r.keys("cortex:state:load:*")
    return {
        "active_load_states": len(keys),
        "queues": {
            "dispatch_workflows": await r.llen("bull:cortex:dispatch_workflows:wait"),
            "email_parse":        await r.llen("bull:cortex:email_parse:wait"),
            "doc_ocr":            await r.llen("bull:cortex:doc_ocr:wait"),
            "transit_monitor":    await r.llen("bull:cortex:transit_monitor:wait"),
            "payment_followup":   await r.llen("bull:cortex:payment_followup:wait"),
            "compliance_sweep":   await r.llen("bull:cortex:compliance_sweep:wait"),
        }
    }
