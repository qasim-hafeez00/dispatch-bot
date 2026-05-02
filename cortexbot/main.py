"""
cortexbot/main.py — PHASE 3A FIXED

PHASE 3A FIXES (GAP-02 + GAP-12):

GAP-02 — 5 broken /internal/* route handlers:
  1. /internal/transit-monitor called skill_15_gps_check(load_id) — didn't exist.
     Fix: now calls skill_15_gps_check from updated s15 module.

  2. /internal/hos-check called skill_14_hos_check(driver_id, load_id) — didn't exist.
     Fix: added _skill_14_hos_check_wrapper() that builds state and calls
     skill_14_hos_compliance(state).

  3. /internal/weather-check called skill_23_weather_check(load_id) — didn't exist.
     Fix: s23_weather_monitoring.py already has skill_23_weather_check(load_id);
     import corrected.

  4. /internal/payment-followup called run_followup_step(invoice_id, step) — didn't exist.
     Fix: function added to s19; import corrected.

  5. /internal/rate-data called get_rate_brief(...) — didn't exist in s07.
     Fix: function added to s07; import corrected.

GAP-12 — ELD webhook routes entirely missing:
  Samsara and Motive push GPS, geo-fence, and HOS events via webhook.
  Without routes, geo-fence arrivals never fire → detention clock never
  starts automatically.
  Fix: Added POST /webhooks/samsara and POST /webhooks/motive.
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

    from cortexbot.core.redis_client import init_redis
    await init_redis()

    from cortexbot.core.event_router import register_default_handlers
    register_default_handlers()

    # SCALE-2: build graph eagerly so concurrent requests never race on lazy-init
    import cortexbot.core.orchestrator as _orch
    _orch._graph = _orch.build_phase2_graph()
    app.state.dispatch_graph = _orch._graph
    logger.info("✅ Dispatch graph compiled and cached")

    # Phase 3D — start background tasks
    from cortexbot.agents.system_health import run_health_monitor
    from cortexbot.agents.disaster_recovery import run_disaster_recovery_tasks

    asyncio.create_task(run_health_monitor(), name="health_monitor")
    await run_disaster_recovery_tasks()

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
            "error":   "Internal server error",
            "message": str(exc) if settings.is_development else "Something went wrong",
        },
    )


# ============================================================
# HEALTH
# ============================================================

@app.get("/health", tags=["System"])
async def health_check():
    return {
        "status":      "ok",
        "version":     settings.app_version,
        "environment": settings.environment,
    }


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


# ── GAP-12 FIX: ELD webhook routes ───────────────────────────
@app.post("/webhooks/samsara", tags=["Webhooks"])
async def samsara_webhook(request: Request):
    """
    GAP-12 FIX: Samsara ELD webhook — GPS, geo-fence, HOS events.
    Without this route geo-fence arrivals never reach the system
    and the detention clock never starts automatically.
    PHASE 3E: Read raw body first so HMAC-SHA256 signature can be verified.
    """
    from cortexbot.webhooks.eld_webhooks import handle_samsara_webhook
    import json as _json
    raw_body  = await request.body()
    payload   = _json.loads(raw_body)
    signature = request.headers.get("X-Samsara-Signature", "")
    event_type = payload.get("eventType", "unknown")
    logger.info(f"🛰️ Samsara webhook: {event_type}")
    asyncio.create_task(handle_samsara_webhook(payload, signature, raw_body))
    return {"received": True}


@app.post("/webhooks/motive", tags=["Webhooks"])
async def motive_webhook(request: Request):
    """
    GAP-12 FIX: Motive (KeepTruckin) ELD webhook — GPS, geo-fence, HOS events.
    PHASE 3E: Read raw body first so HMAC-SHA256 signature can be verified.
    """
    from cortexbot.webhooks.eld_webhooks import handle_motive_webhook
    import json as _json
    raw_body   = await request.body()
    payload    = _json.loads(raw_body)
    signature  = request.headers.get("X-Motive-Signature", "")
    event_type = payload.get("event_type", "unknown")
    logger.info(f"🛰️ Motive webhook: {event_type}")
    asyncio.create_task(handle_motive_webhook(payload, signature, raw_body))
    return {"received": True}


# ============================================================
# INTERNAL ROUTES — Bland AI mid-call data injection
# ============================================================

@app.post("/internal/rate-data", tags=["Internal"])
async def get_live_rate_data(request: Request):
    """
    GAP-02 FIX: Called BY Bland AI during a live broker call.
    get_rate_brief now exists in s07_rate_intelligence.
    """
    from cortexbot.skills.s07_rate_intelligence import get_rate_brief
    payload = await request.json()
    return await get_rate_brief(
        origin_city=payload.get("origin_city", ""),
        dest_city=payload.get("dest_city", ""),
        equipment=payload.get("equipment", "53_dry_van"),
        origin_state=payload.get("origin_state", ""),
        dest_state=payload.get("dest_state", ""),
    )


# ============================================================
# INTERNAL ROUTES — BullMQ Worker triggers
# ============================================================

@app.post("/internal/transit-monitor", tags=["Internal"])
async def internal_transit_monitor(request: Request):
    """
    GAP-02 FIX: skill_15_gps_check now exists in s15.
    Trigger a GPS/ETA check for an active load.
    Called by BullMQ transit_monitor queue every 15 minutes.
    """
    payload = await request.json()
    load_id = payload.get("load_id")
    if not load_id:
        return JSONResponse(status_code=422, content={"error": "load_id required"})

    from cortexbot.skills.s15_in_transit_monitoring import skill_15_gps_check
    asyncio.create_task(skill_15_gps_check(load_id))
    return {"load_id": load_id, "check_initiated": True}


@app.post("/internal/hos-check", tags=["Internal"])
async def internal_hos_check(request: Request):
    """
    GAP-02 FIX: skill_14_hos_check did not exist.
    Added _skill_14_hos_check_wrapper() below which builds a minimal
    state dict and calls skill_14_hos_compliance(state).
    """
    payload   = await request.json()
    driver_id = payload.get("driver_id")
    load_id   = payload.get("load_id")
    if not driver_id or not load_id:
        return JSONResponse(status_code=422, content={"error": "driver_id and load_id required"})

    asyncio.create_task(_skill_14_hos_check_wrapper(driver_id, load_id))
    return {"driver_id": driver_id, "load_id": load_id, "check_initiated": True}


async def _skill_14_hos_check_wrapper(driver_id: str, load_id: str):
    """
    GAP-02 FIX: Builds a minimal state dict from Redis and runs skill_14.
    skill_14_hos_compliance requires a full state dict, not just driver_id.
    """
    from cortexbot.core.redis_client import get_state, set_state
    from cortexbot.skills.s14_hos_compliance import skill_14_hos_compliance

    state = await get_state(f"cortex:state:load:{load_id}") or {}
    state.setdefault("load_id",    load_id)
    state.setdefault("carrier_id", driver_id)
    state.setdefault("driver_id",  driver_id)

    try:
        updated = await skill_14_hos_compliance(state)
        await set_state(f"cortex:state:load:{load_id}", updated)
    except Exception as e:
        logger.error(f"HOS check failed for driver={driver_id} load={load_id}: {e}")


@app.post("/internal/weather-check", tags=["Internal"])
async def internal_weather_check(request: Request):
    """
    GAP-02 FIX: skill_23_weather_check now exists in s23_weather_monitoring.py.
    Run weather route scan for an active load.
    Called by BullMQ weather_check queue every 30 minutes.
    """
    payload = await request.json()
    load_id = payload.get("load_id")
    if not load_id:
        return JSONResponse(status_code=422, content={"error": "load_id required"})

    from cortexbot.skills.s23_weather_monitoring import skill_23_weather_check
    asyncio.create_task(skill_23_weather_check(load_id))
    return {"load_id": load_id, "check_initiated": True}


@app.post("/internal/payment-followup", tags=["Internal"])
async def internal_payment_followup(request: Request):
    """
    GAP-02 FIX: run_followup_step now exists in s19.
    Advance payment follow-up sequence for an invoice.
    Called by BullMQ payment_followup queue on scheduled dates.
    """
    payload    = await request.json()
    invoice_id = payload.get("invoice_id")
    step       = payload.get("step")

    # Also support legacy load_id + amount_paid signature used in seed script
    load_id    = payload.get("load_id")
    amount_paid = payload.get("amount_paid")

    if amount_paid and load_id:
        # Payment received — resume settlement pipeline
        from cortexbot.core.orchestrator import resume_workflow_after_payment
        asyncio.create_task(resume_workflow_after_payment(load_id, float(amount_paid)))
        return {"load_id": load_id, "amount_paid": amount_paid, "pipeline_started": True}

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
    GAP-04 now fixed: sy_freight_claims.py exists.
    Check freight claim deadlines daily.
    """
    from cortexbot.skills.sy_freight_claims import skill_y_daily_deadline_check
    result = await skill_y_daily_deadline_check()
    return result


@app.post("/internal/carrier-performance", tags=["Internal"])
async def internal_carrier_performance(request: Request):
    payload    = await request.json()
    carrier_id = payload.get("carrier_id")
    from cortexbot.skills.s24_s25_relationship_scoring import skill_25_carrier_performance_scoring
    result = await skill_25_carrier_performance_scoring(carrier_id)
    return result


@app.post("/internal/broker-scoring", tags=["Internal"])
async def internal_broker_scoring(request: Request):
    payload   = await request.json()
    broker_id = payload.get("broker_id")
    from cortexbot.skills.s24_s25_relationship_scoring import skill_24_broker_relationship_management
    result = await skill_24_broker_relationship_management(broker_id)
    return result


@app.post("/internal/backhaul-search", tags=["Internal"])
async def internal_backhaul_search(request: Request):
    payload    = await request.json()
    load_id    = payload.get("load_id")
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
        result = await db.execute(
            select(InboundEmail).where(InboundEmail.email_id == email_id)
        )
        email = result.scalar_one_or_none()

    if not email:
        return JSONResponse(status_code=404, content={"error": f"Email {email_id} not found"})

    from cortexbot.webhooks.sendgrid import handle_inbound_email
    asyncio.create_task(handle_inbound_email({
        "from":    email.from_email,
        "subject": email.subject,
        "text":    email.body_text or "",
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
    payload = await request.json()
    from cortexbot.skills.s26_s27_compliance_accessorials import upsert_compliance_doc
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


@app.post("/api/carriers/{carrier_id}/send-agreement", tags=["Carriers"])
async def send_service_agreement(carrier_id: str):
    """
    PHASE 3C — Agent AA
    Generate and send the Dispatcher Service Agreement to a carrier via DocuSign.
    Call this after carrier onboarding is complete but before first dispatch.
    """
    from cortexbot.agents.service_agreement import skill_aa_generate_agreement
    result = await skill_aa_generate_agreement(carrier_id)
    if not result.get("success"):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=422, content=result)
    return result


@app.post("/webhooks/docusign/service-agreement", tags=["Webhooks"])
async def docusign_service_agreement_signed(request: Request):
    """
    PHASE 3C — Agent AA webhook
    Receives DocuSign 'envelope-completed' events for carrier service agreements.
    Activates the carrier in the DB and sends the welcome message.

    DocuSign must be configured to POST to:
      {base_url}/webhooks/docusign/service-agreement
    with Connect trigger: envelope-completed
    """
    from cortexbot.agents.service_agreement import skill_aa_process_signature

    payload     = await request.json()
    envelope_id = (
        payload.get("envelopeId")
        or payload.get("data", {}).get("envelopeId")
        or payload.get("envelope_id", "")
    )

    if not envelope_id:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=422, content={"error": "envelopeId missing"})

    status = payload.get("status") or payload.get("data", {}).get("envelopeSummary", {}).get("status", "")
    if status.lower() not in ("completed", "signed"):
        # Not a completed event — acknowledge but do nothing
        return {"received": True, "action": "no_action", "status": status}

    asyncio.create_task(skill_aa_process_signature(envelope_id, payload))
    return {"received": True, "envelope_id": envelope_id}


@app.post("/internal/emergency-rebroker", tags=["Internal"])
async def internal_emergency_rebroker(request: Request):
    """
    PHASE 3C — Agent CC
    Manually trigger emergency rebrokering for a load.
    Also called by BullMQ when GPS-dark or breakdown events fire.

    Body: { load_id, trigger_reason, [carrier_whatsapp, broker_email, ...] }
    trigger_reason: GPS_DARK | BREAKDOWN | NO_SHOW | CARRIER_QUIT
    """
    from cortexbot.agents.emergency_rebroker import skill_cc_emergency_rebroker
    from cortexbot.core.redis_client import get_state

    payload        = await request.json()
    load_id        = payload.get("load_id")
    trigger_reason = payload.get("trigger_reason", "BREAKDOWN")

    if not load_id:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=422, content={"error": "load_id required"})

    # Load state from Redis
    state = await get_state(f"cortex:state:load:{load_id}") or {"load_id": load_id}
    state.update({k: v for k, v in payload.items() if k not in ("load_id", "trigger_reason")})

    asyncio.create_task(
        skill_cc_emergency_rebroker(
            load_id=load_id,
            trigger_reason=trigger_reason,
            state=state,
        ),
        name=f"cc_{load_id}",
    )

    return {
        "load_id":        load_id,
        "trigger_reason": trigger_reason,
        "rebroker_initiated": True,
    }


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
    payload    = await request.json()
    invoice_id = payload.get("invoice_id")
    amount_paid = payload.get("amount_paid")

    if not invoice_id or not amount_paid:
        return JSONResponse(status_code=422, content={"error": "invoice_id and amount_paid required"})

    from cortexbot.skills.s19_payment_reconciliation import record_payment_received
    from cortexbot.core.redis_client import get_state
    state = await get_state(f"cortex:state:load:{load_id}") or {"load_id": load_id}
    result = await record_payment_received(load_id, float(amount_paid), state)
    return result


@app.post("/api/dispatch/open-claim/{load_id}", tags=["Dispatch"])
async def open_freight_claim(load_id: str, request: Request):
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
    from sqlalchemy import text as sa_text
    from cortexbot.db.session import get_db_session
    async with get_db_session() as db:
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
    payload = await request.json()
    from cortexbot.skills.sq_sr_ss_st_financial import skill_s_driver_advance
    result = await skill_s_driver_advance(
        carrier_id=payload["carrier_id"],
        load_id=payload.get("load_id", ""),
        advance_type=payload.get("advance_type", "FUEL"),
        amount_requested=float(payload.get("amount", 200)),
        state={"carrier_whatsapp": payload.get("carrier_whatsapp", "")},
    )
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
        "From":     f"whatsapp:{payload['from']}",
        "Body":     payload["body"],
        "NumMedia": str(len(payload.get("media_urls", []))),
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
    s3_url  = payload.get("s3_url", "s3://cortexbot-docs/samples/sample_rc.pdf")
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
    if not settings.is_development:
        return JSONResponse(status_code=404, content={"error": "Not found"})
    from cortexbot.core.orchestrator_phase2 import handle_delivery_confirmed
    asyncio.create_task(handle_delivery_confirmed(load_id))
    return {"load_id": load_id, "delivery_simulated": True}


@app.post("/debug/simulate/fraud-check", tags=["Debug"])
async def simulate_fraud_check(request: Request):
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
        },
    }


# ============================================================
# PHASE 3D — ROUTES TO APPEND TO cortexbot/main.py
# Paste everything below this comment into main.py,
# before the final closing lines.
# ============================================================

# ── Agent E: System Health ─────────────────────────────────

@app.get("/health/agents", tags=["System"])
async def agent_health():
    """
    Phase 3D — Agent E
    Per-agent health status: APIs, queues, circuit breakers, DB, Redis.
    """
    from cortexbot.agents.system_health import get_agent_health
    return await get_agent_health()


@app.post("/internal/health/snapshot", tags=["System"])
async def trigger_health_snapshot():
    """Force an immediate health snapshot (internal use / debugging)."""
    from cortexbot.agents.system_health import collect_health_snapshot, _store_snapshot
    snapshot = await collect_health_snapshot()
    await _store_snapshot(snapshot)
    return snapshot


# ── Agent Z: Cargo Theft ────────────────────────────────────

@app.post("/internal/cargo-theft/assess/{load_id}", tags=["Internal"])
async def assess_cargo_theft_risk(load_id: str, request: Request):
    """
    Phase 3D — Agent Z
    Score the theft risk for an active load.
    Body: { gps_dark_minutes, cc_driver_responded, ... } (optional overrides)
    """
    from cortexbot.agents.cargo_theft import skill_z_detect_theft_risk
    from cortexbot.core.redis_client import get_state

    state   = await get_state(f"cortex:state:load:{load_id}") or {"load_id": load_id}
    body    = {}
    try:
        body = await request.json()
    except Exception:
        pass
    state.update(body)

    result = await skill_z_detect_theft_risk(load_id, state)
    return {
        "load_id":           load_id,
        "theft_risk_score":  result.get("theft_risk_score"),
        "recommendation":    result.get("theft_recommendation"),
        "factors":           result.get("theft_risk_factors", []),
    }


@app.post("/internal/cargo-theft/activate/{load_id}", tags=["Internal"])
async def activate_cargo_theft_response(load_id: str, request: Request):
    """
    Phase 3D — Agent Z
    Activate the full cargo theft response protocol for a load.
    Should only be called after theft_risk_score >= 60.
    """
    from cortexbot.agents.cargo_theft import skill_z_activate_response
    from cortexbot.core.redis_client import get_state

    state = await get_state(f"cortex:state:load:{load_id}") or {"load_id": load_id}
    body  = {}
    try:
        body = await request.json()
    except Exception:
        pass
    state.update(body)

    asyncio.create_task(
        skill_z_activate_response(load_id, state),
        name=f"theft_{load_id}",
    )
    return {
        "load_id":   load_id,
        "activated": True,
        "message":   "Cargo theft response protocol activated. NTC, NICB, and insurance will be notified.",
    }


# ── Agent P: Disaster Recovery ──────────────────────────────

@app.post("/internal/dr/backup-state", tags=["Internal"])
async def trigger_state_backup():
    """
    Phase 3D — Agent P
    Manually trigger a full load state backup to S3.
    """
    from cortexbot.agents.disaster_recovery import skill_p_backup_state
    result = await skill_p_backup_state()
    return result


@app.post("/internal/dr/restore/{backup_id}", tags=["Internal"])
async def restore_from_backup(backup_id: str):
    """
    Phase 3D — Agent P
    Restore workflow states from a specific backup snapshot.
    Use backup_id = 'latest' to restore from the most recent backup.
    """
    from cortexbot.agents.disaster_recovery import skill_p_restore_from_backup
    bid = None if backup_id == "latest" else backup_id
    result = await skill_p_restore_from_backup(bid)
    return result


@app.post("/internal/dr/pg-backup", tags=["Internal"])
async def trigger_pg_backup():
    """
    Phase 3D — Agent P
    Manually trigger a PostgreSQL pg_dump backup to S3.
    """
    from cortexbot.agents.disaster_recovery import skill_p_pg_backup
    result = await skill_p_pg_backup()
    return result


@app.post("/internal/dr/drill", tags=["Internal"])
async def trigger_dr_drill():
    """
    Phase 3D — Agent P
    Manually trigger a DR drill (dry-run restore verification).
    """
    from cortexbot.agents.disaster_recovery import skill_p_weekly_dr_drill
    result = await skill_p_weekly_dr_drill()
    return result


@app.get("/internal/dr/status", tags=["Internal"])
async def dr_status():
    """
    Phase 3D — Agent P
    Return DR status: last heartbeat, last backup, last drill.
    """
    from cortexbot.core.redis_client import get_redis
    r = get_redis()

    last_heartbeat = await r.get("cortex:dr:last_heartbeat")
    last_backup_raw = await r.get("cortex:dr:last_backup")
    last_backup = None
    if last_backup_raw:
        try:
            last_backup = json.loads(last_backup_raw)
        except Exception:
            pass

    return {
        "last_heartbeat": last_heartbeat,
        "last_backup":    last_backup,
        "status":         "OK" if last_heartbeat else "NO_HEARTBEAT",
    }


# ── Agent BB: GDPR/CCPA ─────────────────────────────────────

@app.post("/api/carriers/{carrier_id}/data-deletion-request", tags=["Carriers"])
async def request_data_deletion(carrier_id: str, request: Request):
    """
    Phase 3D — Agent BB (GDPR/CCPA)
    Submit a Right to Be Forgotten / data deletion request for a carrier.

    Body: { requester_email: str, reason: str (optional) }

    The carrier's PII is soft-deleted immediately.
    All remaining data is permanently purged after a 30-day grace period.
    Financial records (invoices, settlements) are anonymized but retained
    for 7 years per IRS/FMCSA requirements.
    """
    from cortexbot.agents.gdpr_ccpa import skill_bb_delete_carrier_data

    payload = await request.json()
    requester_email = payload.get("requester_email")

    if not requester_email:
        return JSONResponse(
            status_code=422,
            content={"error": "requester_email is required"}
        )

    result = await skill_bb_delete_carrier_data(
        carrier_id=carrier_id,
        requester_email=requester_email,
        reason=payload.get("reason", "RTBF_REQUEST"),
    )
    return result


@app.get("/api/carriers/{carrier_id}/deletion-request-status", tags=["Carriers"])
async def get_deletion_request_status(carrier_id: str):
    """
    Phase 3D — Agent BB
    Check the status of an existing data deletion request.
    """
    from cortexbot.agents.gdpr_ccpa import _get_deletion_request
    result = await _get_deletion_request(carrier_id)
    if not result:
        return {"status": "NO_REQUEST", "carrier_id": carrier_id}
    return result


@app.post("/internal/gdpr/process-pending-deletions", tags=["Internal"])
async def process_pending_deletions():
    """
    Phase 3D — Agent BB
    Process all data deletion requests that have passed their grace period.
    Called by BullMQ compliance_sweep queue daily at 06:00.
    """
    from cortexbot.agents.gdpr_ccpa import skill_bb_process_pending_deletions
    result = await skill_bb_process_pending_deletions()
    return result
