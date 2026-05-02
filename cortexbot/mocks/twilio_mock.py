"""
cortexbot/mocks/twilio_mock.py

Prints WhatsApp and SMS messages to the console instead of
calling Twilio. Every send returns True so callers behave normally.
"""
import logging

logger = logging.getLogger("mock.twilio")


async def mock_send_whatsapp(to: str, body: str) -> bool:
    logger.info(
        "\n%s\n[MOCK WhatsApp → %s]\n%s\n%s",
        "─" * 60, to, body, "─" * 60,
    )
    return True


async def mock_send_sms(to: str, body: str) -> bool:
    logger.info("[MOCK SMS → %s] %s", to, body[:120])
    return True
