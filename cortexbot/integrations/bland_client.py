"""
cortexbot/integrations/bland_client.py
Bland AI client for AI voice dispatch calls.
"""

import logging
from typing import Optional, dict

from cortexbot.config import settings
from cortexbot.core.api_gateway import api_call

logger = logging.getLogger("cortexbot.integrations.bland")

class BlandClient:
    """
    Client for Bland AI voice automation.
    Uses the centralized API gateway for auth, retries, and circuit breaking.
    """

    async def initiate_call(
        self, 
        phone_number: str, 
        task_prompt: str, 
        voice_id: str = "nat",
        language: str = "en",
        request_data: Optional[dict] = None
    ) -> Optional[str]:
        """
        Initiate an outbound AI call.
        Returns the Bland call_id if successful.
        """
        try:
            payload = {
                "phone_number": phone_number,
                "task": task_prompt,
                "voice": voice_id,
                "language": language,
                "request_data": request_data or {},
                "webhook": settings.bland_ai_webhook_url,
                "reduce_latency": True,
            }
            
            result = await api_call(
                "bland_ai",
                "/calls",
                method="POST",
                payload=payload
            )
            
            call_id = result.get("call_id")
            if call_id:
                logger.info(f"📞 Outbound call initiated via Bland: {call_id} to {phone_number}")
                return call_id
            
        except Exception as e:
            logger.error(f"Failed to initiate Bland call to {phone_number}: {e}")
            
        return None

    async def get_call_status(self, call_id: str) -> dict:
        """Get the current status of a call."""
        try:
            return await api_call(
                "bland_ai",
                f"/calls/{call_id}",
                method="GET"
            )
        except Exception as e:
            logger.error(f"Failed to get Bland call status for {call_id}: {e}")
            return {}

    async def get_transcript(self, call_id: str) -> Optional[str]:
        """Get the full transcript of a completed call."""
        try:
            status = await self.get_call_status(call_id)
            return status.get("transcripts") or status.get("transcript")
        except Exception as e:
            logger.error(f"Failed to get Bland transcript for {call_id}: {e}")
            return None

    async def record_recording_url(self, call_id: str) -> Optional[str]:
        """Get the recording URL for a completed call."""
        try:
            status = await self.get_call_status(call_id)
            return status.get("recording_url")
        except Exception as e:
            logger.error(f"Failed to get Bland recording URL for {call_id}: {e}")
            return None

bland_client = BlandClient()
