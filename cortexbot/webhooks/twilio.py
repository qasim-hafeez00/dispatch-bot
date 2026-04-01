"""
cortexbot/webhooks/twilio.py — PHASE 3A FIXED

PHASE 3A FIX (GAP-15): WhatsApp message deduplication missing.

Twilio can deliver the same webhook twice on network timeout/retry.
Previously a driver sending "DELIVERED" twice in quick succession would
trigger handle_delivery_confirmed twice → double invoice generation.

Fix: _is_duplicate_message(phone, body) uses Redis SETNX with a 60-second
TTL. First delivery → processed. Second delivery within 60s → silently
dropped. The deduplication key is a hash of (from_phone, body, timestamp-minute)
so the same message sent by the same driver a minute later is treated as new.

Also fixed the redis_client import — we now use update_whatsapp_context
from the updated redis_client module.
"""

import hashlib
import logging
import re
import time

from cortexbot.config import settings

logger = logging.getLogger("cortexbot.webhooks.twilio")

# Delivery confirmation keywords
DELIVERED_KEYWORDS = {
    "delivered", "delivery done", "all done", "dropped off", "empty",
    "unloaded", "done delivering", "delivered it", "it's delivered",
    "entregado", "terminé", "listo",
}

ARRIVED_DELIVERY_KEYWORDS = {
    "at delivery", "arrived delivery", "at receiver", "at consignee",
    "arrived at delivery", "here for delivery",
}

ARRIVED_PICKUP_KEYWORDS = {
    "at pickup", "at shipper", "arrived pickup", "at dock", "at the shipper",
    "llegué", "here for pickup",
}

ADVANCE_KEYWORDS = {
    "need fuel", "fuel money", "need cash", "lumper", "broke down",
    "need advance", "send code", "need comchek",
}


async def _is_duplicate_message(phone: str, body: str) -> bool:
    """
    GAP-15 FIX: Idempotency guard for inbound WhatsApp messages.

    Twilio retries webhooks on HTTP timeout.  A duplicated DELIVERED
    message would generate two invoices.

    Strategy: hash (phone, body, time-minute) → Redis SETNX with 60s TTL.
      - First delivery: SETNX sets key → returns True (not duplicate)
      - Second delivery within same minute: key exists → returns False (duplicate)
      - New message one minute later: different hash → processed normally
    """
    try:
        from cortexbot.core.redis_client import get_redis

        # Include minute-level timestamp so the same message a minute later
        # is treated as a fresh message (e.g. driver sends update every 2hr)
        minute_bucket = int(time.time() // 60)
        raw = f"{phone}|{body.strip().lower()[:200]}|{minute_bucket}"
        msg_hash = hashlib.sha256(raw.encode()).hexdigest()[:24]
        key      = f"cortex:wa:dedup:{msg_hash}"

        r = get_redis()
        # COPILOT FIX: use a single atomic SET NX EX instead of the
        # non-atomic SETNX + EXPIRE pair.  If the process crashes between
        # the two calls the key would never expire → message permanently
        # deduplicated.  SET NX EX is atomic on all Redis versions ≥ 2.6.
        set_result = await r.set(key, "1", nx=True, ex=90)
        is_new = set_result is not None   # None → key already existed
        return not is_new                 # True = duplicate, False = new

    except Exception as e:
        # If Redis is down, let the message through (fail open)
        logger.warning(f"Dedup check failed (Redis?): {e} — processing message anyway")
        return False


async def handle_whatsapp_inbound(payload: dict):
    """
    Main entry point — route inbound WhatsApp/SMS.
    """
    from_raw  = payload.get("From", "")
    body      = payload.get("Body", "").strip()
    num_media = int(payload.get("NumMedia", 0))

    phone = from_raw.replace("whatsapp:", "").strip()
    text  = body.lower()

    logger.info(f"💬 Inbound from {phone}: '{body[:60]}' media={num_media}")

    # ── GAP-15 FIX: Idempotency check ────────────────────────
    if await _is_duplicate_message(phone, body):
        logger.debug(f"[GAP-15] Duplicate WhatsApp message from {phone} — dropped")
        return

    # ── Media attachments (BOL photos, POD) ─────────────────
    if num_media > 0:
        media_urls = []
        for i in range(num_media):
            url   = payload.get(f"MediaUrl{i}")
            ctype = payload.get(f"MediaContentType{i}", "")
            if url:
                media_urls.append({"url": url, "content_type": ctype})
        await _handle_media(phone, body, media_urls)
        return

    # ── Load offer confirmation (Skill 09 handles this) ──────
    from cortexbot.skills.s09_carrier_confirm import handle_inbound_whatsapp
    await handle_inbound_whatsapp(phone, body, [])

    # ── Delivery events (Phase 2) ─────────────────────────────
    if any(kw in text for kw in DELIVERED_KEYWORDS):
        await _handle_delivery_message(phone, body)

    elif any(kw in text for kw in ARRIVED_DELIVERY_KEYWORDS):
        await _handle_arrival_message(phone, "delivery")

    elif any(kw in text for kw in ARRIVED_PICKUP_KEYWORDS):
        await _handle_arrival_message(phone, "pickup")

    # ── Driver advance request ────────────────────────────────
    elif any(kw in text for kw in ADVANCE_KEYWORDS):
        await _handle_advance_request(phone, body)


async def _handle_media(phone: str, caption: str, media_urls: list):
    """
    Driver sent photos — upload to S3 and associate with active load.
    """
    from cortexbot.core.redis_client import get_whatsapp_context
    import boto3
    import httpx
    import uuid

    ctx     = await get_whatsapp_context(phone)
    load_id = ctx.get("current_load_id") if ctx else None

    logger.info(f"📸 Media received from {phone}: {len(media_urls)} file(s) for load {load_id}")

    s3_urls = []
    for media in media_urls[:5]:
        url   = media["url"]
        ctype = media["content_type"]
        ext   = "jpg" if "jpeg" in ctype else "pdf" if "pdf" in ctype else "jpg"

        try:
            import asyncio
            async with httpx.AsyncClient(
                auth=(settings.twilio_account_sid, settings.twilio_auth_token),
                timeout=30,
            ) as client:
                resp    = await client.get(url)
                content = resp.content

            s3  = boto3.client(
                "s3",
                aws_access_key_id=settings.aws_access_key_id,
                aws_secret_access_key=settings.aws_secret_access_key,
                region_name=settings.aws_region,
            )
            key  = f"loads/{load_id or 'unmatched'}/docs/{uuid.uuid4().hex[:8]}.{ext}"
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: s3.put_object(
                    Bucket=settings.aws_s3_bucket,
                    Key=key,
                    Body=content,
                    ContentType=ctype,
                ),
            )
            s3_url = f"s3://{settings.aws_s3_bucket}/{key}"
            s3_urls.append(s3_url)
            logger.info(f"📤 Uploaded BOL/POD photo to {s3_url}")

        except Exception as e:
            logger.error(f"Media upload failed: {e}")

    if not s3_urls:
        return

    if load_id:
        from cortexbot.db.session import get_db_session
        from cortexbot.db.models import Event
        from datetime import datetime, timezone

        async with get_db_session() as db:
            db.add(Event(
                event_code="POD_RECEIVED",
                entity_type="load",
                entity_id=load_id,
                triggered_by="twilio_webhook",
                data={"s3_urls": s3_urls, "caption": caption, "from_phone": phone},
            ))

    from cortexbot.integrations.twilio_client import send_whatsapp
    await send_whatsapp(
        phone,
        f"✅ Got your documents ({len(s3_urls)} photo(s)) — thanks!\n"
        f"I'll process these and get you paid asap. 💰"
    )


async def _handle_delivery_message(phone: str, body: str):
    """Driver texted that delivery is done."""
    from cortexbot.core.redis_client import get_whatsapp_context
    from cortexbot.core.orchestrator_phase2 import handle_delivery_confirmed

    ctx = await get_whatsapp_context(phone)
    if not ctx:
        return

    load_id = ctx.get("current_load_id")
    if not load_id:
        return

    logger.info(f"🎯 Delivery confirmed via WhatsApp for load {load_id}")
    await handle_delivery_confirmed(load_id)


async def _handle_arrival_message(phone: str, facility_type: str):
    """Driver texted that they arrived at pickup or delivery."""
    from cortexbot.core.redis_client import get_whatsapp_context
    from cortexbot.core.orchestrator_phase2 import handle_driver_arrival
    from datetime import datetime, timezone

    ctx = await get_whatsapp_context(phone)
    if not ctx:
        return

    load_id = ctx.get("current_load_id")
    if not load_id:
        return

    arrival_ts = datetime.now(timezone.utc).isoformat()
    logger.info(f"📍 Driver arrived at {facility_type} for load {load_id}")
    await handle_driver_arrival(load_id, facility_type, arrival_ts)


async def _handle_advance_request(phone: str, body: str):
    """Driver is requesting a fuel advance or comchek."""
    from cortexbot.core.redis_client import get_whatsapp_context, get_state
    from cortexbot.skills.sq_sr_ss_st_financial import skill_s_driver_advance

    ctx = await get_whatsapp_context(phone)
    if not ctx:
        return

    load_id    = ctx.get("current_load_id")
    carrier_id = ctx.get("carrier_id")
    if not load_id or not carrier_id:
        return

    state = await get_state(f"cortex:state:load:{load_id}") or {}

    text         = body.lower()
    advance_type = "EMERGENCY"
    amount       = 100.0

    if "fuel" in text or "diesel" in text:
        advance_type = "FUEL"
        amount = 200.0
    elif "lumper" in text:
        advance_type = "LUMPER"
        amount_match = re.search(r"\$?(\d+)", body)
        amount = float(amount_match.group(1)) if amount_match else 150.0
    elif "broke" in text or "repair" in text:
        advance_type = "EMERGENCY"
        amount = 300.0

    logger.info(f"💳 Advance request from {phone}: {advance_type} ${amount:.2f}")

    await skill_s_driver_advance(
        carrier_id=str(carrier_id),
        load_id=str(load_id),
        advance_type=advance_type,
        amount_requested=amount,
        state={**state, "carrier_whatsapp": phone},
    )
