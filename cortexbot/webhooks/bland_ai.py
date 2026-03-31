"""
cortexbot/webhooks/bland_ai.py
Receives call completion events from Bland AI.
"""
import logging

logger = logging.getLogger("cortexbot.webhooks.bland_ai")


async def handle_call_complete(payload: dict):
    """Entry point from main.py — delegates to voice calling agent."""
    from cortexbot.agents.voice_calling import handle_call_complete as agent_handle
    await agent_handle(payload)
