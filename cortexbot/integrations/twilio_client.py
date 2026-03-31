"""
cortexbot/integrations/twilio_client.py
Send WhatsApp and SMS via Twilio.
"""
import logging
from cortexbot.config import settings

logger = logging.getLogger("cortexbot.integrations.twilio")


async def send_whatsapp(to_phone: str, message: str) -> bool:
    """Send a WhatsApp message via Twilio."""
    if not to_phone:
        logger.warning("send_whatsapp: empty phone number")
        return False
    try:
        from twilio.rest import Client
        import asyncio

        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        from_  = f"whatsapp:{settings.twilio_whatsapp_number}"
        to_    = f"whatsapp:{to_phone}" if not to_phone.startswith("whatsapp:") else to_phone

        loop = asyncio.get_event_loop()
        msg = await loop.run_in_executor(
            None,
            lambda: client.messages.create(from_=from_, to=to_, body=message[:1600])
        )
        logger.info(f"✅ WhatsApp sent to {to_phone}: SID={msg.sid}")
        return True
    except Exception as e:
        logger.error(f"❌ WhatsApp failed to {to_phone}: {e}")
        return False


async def send_sms(to_phone: str, message: str) -> bool:
    """Send an SMS via Twilio."""
    if not to_phone:
        return False
    try:
        from twilio.rest import Client
        import asyncio

        client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        loop   = asyncio.get_event_loop()
        msg = await loop.run_in_executor(
            None,
            lambda: client.messages.create(
                from_=settings.twilio_sms_number,
                to=to_phone,
                body=message[:1600],
            )
        )
        logger.info(f"✅ SMS sent to {to_phone}: SID={msg.sid}")
        return True
    except Exception as e:
        logger.error(f"❌ SMS failed to {to_phone}: {e}")
        return False
