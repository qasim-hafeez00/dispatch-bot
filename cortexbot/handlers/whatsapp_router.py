import logging
import re
from cortexbot.core.redis_client import (
    get_whatsapp_context,
    update_whatsapp_context,
    publish_carrier_decision
)
from cortexbot.db.session import get_db_session
from cortexbot.db.models import Event
from cortexbot.integrations.twilio_client import send_whatsapp
from cortexbot.core.api_gateway import api_call

logger = logging.getLogger("cortexbot.handlers.whatsapp_router")

async def route_inbound_whatsapp(phone: str, body: str, media_urls: list = None):
    """
    Routes inbound WhatsApp based on what we're awaiting.
    """
    ctx = await get_whatsapp_context(phone)
    if not ctx:
        # Unknown sender — log and ignore
        logger.info(f"💬 Message from unknown sender {phone}: '{body[:30]}'")
        return

    # Update last message timestamp
    import time
    await update_whatsapp_context(phone, {"last_message_at": time.time()})

    awaiting = ctx.get("awaiting")
    load_id  = ctx.get("current_load_id")
    last_msg = ctx.get("last_message_at")

    # FIX-07: Expire awaiting state if > 2 hours
    if awaiting and last_msg:
        import time
        if time.time() - last_msg > 7200:
            logger.info(f"⌛ WhatsApp awaiting state '{awaiting}' expired for {phone}")
            awaiting = None
            await update_whatsapp_context(phone, {"awaiting": None})

    if awaiting == "LOAD_CONFIRMATION" and load_id:
        await _handle_confirmation_response(phone, body, load_id, ctx)

    elif awaiting == "DRIVER_ACK" and load_id:
        await _handle_driver_ack(phone, body, load_id)

    elif media_urls:
        # FIX-08: POD photo validation via Claude Vision
        logger.info(f"📸 Media received from {phone}: {len(media_urls)} file(s)")
        for url in media_urls:
            await _process_pod_media(phone, url, load_id)

async def _classify_with_claude(body: str) -> str:
    """Classify YES/NO/COUNTER_OFFER using Claude LLM."""
    try:
        response = await api_call(
            "anthropic",
            "/v1/messages",
            method="POST",
            payload={
                "model": "claude-3-haiku-20240307",
                "max_tokens": 10,
                "system": "You are classifying a truck driver's response to a load offer. Reply with exactly one word: YES, NO, COUNTER, or UNCLEAR. A counter-offer includes a specific rate they want.",
                "messages": [{"role": "user", "content": body}]
            }
        )
        text = response.get("content", [{}])[0].get("text", "").strip().upper()
        if text in ["YES", "NO", "COUNTER"]:
            return text
    except Exception as e:
        logger.warning(f"Claude classification failed: {e}")
        
    return "UNCLEAR"

async def _handle_confirmation_response(phone: str, body: str, load_id: str, ctx: dict):
    """Process YES/NO/COUNTER confirmation from carrier."""
    decision_class = await _classify_with_claude(body)
    
    if decision_class == "YES":
        decision = "CONFIRMED"
    elif decision_class == "NO":
        decision = "REJECTED"
    elif decision_class == "COUNTER":
        # Extract rate
        rate_match = re.search(r'\$?(\d+\.?\d*)\s*(per mile|/mi|cpm|flat)', body, re.IGNORECASE)
        if rate_match:
            counter_rate = float(rate_match.group(1))
            await publish_carrier_decision(load_id, "COUNTER_OFFER", body, 
                                           extra={"counter_rate_cpm": counter_rate})
            await send_whatsapp(phone, f"Got it — let me check with the broker on {counter_rate:.2f} and get back to you shortly.")
            await update_whatsapp_context(phone, {"awaiting": None})
            return
        else:
            decision = "COUNTER_OFFER"
            await publish_carrier_decision(load_id, decision, body)
            await send_whatsapp(phone, "Got it — let me check with the broker on your counter offer and get back to you shortly.")
            await update_whatsapp_context(phone, {"awaiting": None})
            return
    else:
        # Ambiguous — ask again
        await send_whatsapp(phone, "I need a YES, NO, or a specific counter-offer rate — do you want this load?")
        return

    # Publish so wait_for_carrier_decision() wakes up
    await publish_carrier_decision(load_id, decision, body)

    # Clear awaiting
    await update_whatsapp_context(phone, {"awaiting": None})

async def _handle_driver_ack(phone: str, body: str, load_id: str):
    """Process driver acknowledgement of dispatch sheet."""
    text = body.lower().strip()
    positive = {"confirmed", "confirm", "got it", "ok", "received", "on my way", "yes"}

    if any(kw in text for kw in positive):
        async with get_db_session() as db:
            db.add(Event(
                event_code="DRIVER_ACKNOWLEDGED",
                entity_type="load",
                entity_id=load_id,
                triggered_by="whatsapp_router",
                data={"message": body},
            ))
            await db.commit()
        await update_whatsapp_context(phone, {"awaiting": None})
        logger.info(f"✅ Driver acknowledged dispatch for load {load_id}")


async def _process_pod_media(phone: str, media_url: str, load_id: str):
    """
    Validate if the photo is a valid POD and extract receiver name.
    """
    try:
        # 1. Download image
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(media_url)
            resp.raise_for_status()
            img_bytes = resp.content

        import base64
        img_b64 = base64.b64encode(img_bytes).decode()

        # 2. Analyze with Claude Vision
        from cortexbot.core.api_gateway import api_call
        response = await api_call(
            "anthropic",
            "/v1/messages",
            method="POST",
            payload={
                "model": "claude-3-haiku-20240307",
                "max_tokens": 100,
                "messages": [{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": img_b64
                            }
                        },
                        {
                            "type": "text",
                            "text": (
                                "Is this a Proof of Delivery (POD) or Bill of Lading (BOL)? "
                                "If yes, extract the Receiver Name (who signed it) and the Date. "
                                "Reply in JSON: {\"is_pod\": bool, \"receiver_name\": string, \"date\": string}"
                            )
                        }
                    ]
                }]
            }
        )

        import json
        text = response.get("content", [{}])[0].get("text", "{}")
        # Find JSON block
        match = re.search(r'\{.*\}', text, re.DOTALL)
        data = json.loads(match.group(0)) if match else {}

        if data.get("is_pod"):
            logger.info(f"✅ Valid POD received from {phone} for load {load_id}. Receiver: {data.get('receiver_name')}")
            
            # Save to DB
            async with get_db_session() as db:
                from sqlalchemy import update as sa_update
                from cortexbot.db.models import Load
                await db.execute(
                    sa_update(Load).where(Load.load_id == load_id).values(
                        pod_url=media_url,
                        status="DELIVERED"
                    )
                )
                db.add(Event(
                    event_code="POD_RECEIVED",
                    entity_type="load",
                    entity_id=load_id,
                    triggered_by="whatsapp_router",
                    data=data,
                    new_status="DELIVERED"
                ))
                await db.commit()
            
            await send_whatsapp(phone, f"Thank you! POD received and verified. Signed by {data.get('receiver_name')}. Great job!")
        else:
            logger.warning(f"❌ Received media from {phone} that doesn't look like a POD.")
            await send_whatsapp(phone, "Hmm, that photo doesn't look like a clear POD or BOL. Could you please send a clear photo of the signed delivery document?")

    except Exception as e:
        logger.error(f"Failed to process POD media: {e}")
        await send_whatsapp(phone, "Received your photo, but I had trouble verifying it. I'll have a human dispatcher take a look!")
