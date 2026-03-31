"""
cortexbot/core/event_router.py

Production Event Router

Maps event codes to handler functions and provides utility
methods for publishing events across the system.

Every event is:
1. Logged to the PostgreSQL `events` table
2. Appended to the Redis event stream for real-time consumers
3. Routed to the appropriate handler function (if registered)
"""

import logging
from typing import Callable, Dict, Optional, Any
from datetime import datetime, timezone

from cortexbot.db.session import get_db_session
from cortexbot.db.models import Event

logger = logging.getLogger("cortexbot.core.event_router")


class EventRouter:
    """
    Central event routing and logging hub.

    Skills and webhooks call:
        await event_router.publish("RC_RECEIVED", "load", load_id, {...})

    The router logs the event and dispatches to any registered handler.
    """

    def __init__(self):
        self._handlers: Dict[str, Callable] = {}

    def register(self, event_code: str, handler: Callable):
        """Register a handler for an event code."""
        self._handlers[event_code] = handler
        logger.debug(f"Registered handler for event: {event_code}")

    def get_handler(self, event_code: str) -> Optional[Callable]:
        """Get the handler for an event code, or None."""
        return self._handlers.get(event_code)

    async def publish(
        self,
        event_code: str,
        entity_type: str,
        entity_id: str,
        data: Dict[str, Any] = None,
        triggered_by: str = "system",
        new_status: str = None,
    ):
        """
        Publish an event: log to DB + Redis stream + dispatch to handler.

        Args:
            event_code: E.g. "LOAD_BOOKED", "RC_RECEIVED", "CARRIER_DECISION"
            entity_type: "load" | "carrier" | "broker"
            entity_id: UUID string of the entity
            data: Arbitrary JSON-serializable event payload
            triggered_by: Which skill/agent triggered this
            new_status: Optional new status for the entity
        """
        data = data or {}

        # 1. Log to PostgreSQL
        try:
            async with get_db_session() as db:
                event = Event(
                    event_code=event_code,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    triggered_by=triggered_by,
                    data=data,
                    new_status=new_status,
                )
                db.add(event)
                await db.commit()
                event_id = str(event.event_id)
        except Exception as e:
            logger.error(f"Failed to log event {event_code} to DB: {e}")
            event_id = None

        # 2. Append to Redis event stream
        try:
            from cortexbot.core.redis_client import append_event_stream

            stream_name = f"cortex:events:{entity_type}"
            await append_event_stream(stream_name, {
                "event_code": event_code,
                "entity_id": entity_id,
                "triggered_by": triggered_by,
                "data": str(data),
            })
        except Exception as e:
            logger.warning(f"Failed to append event {event_code} to Redis stream: {e}")

        # 3. Dispatch to registered handler
        handler = self._handlers.get(event_code)
        if handler:
            try:
                await handler({
                    "event_id": event_id,
                    "event_code": event_code,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "data": data,
                    "new_status": new_status,
                })
            except Exception as e:
                logger.error(f"Handler for {event_code} failed: {e}")

        logger.info(
            f"📰 Event published: {event_code} | {entity_type}:{entity_id}"
            f"{' → ' + new_status if new_status else ''}"
        )

    async def log_event(
        self,
        event_code: str,
        entity_type: str,
        entity_id: str,
        data: Dict[str, Any] = None,
        triggered_by: str = "system",
    ):
        """Convenience alias for publish() without routing (logging only)."""
        await self.publish(
            event_code=event_code,
            entity_type=entity_type,
            entity_id=entity_id,
            data=data or {},
            triggered_by=triggered_by,
        )


# ── Module-level singleton ─────────────────────────────────────
event_router = EventRouter()


def register_default_handlers():
    """
    Register Phase 1 event handlers on startup.
    Called from main.py during app lifespan.
    """

    async def _on_rc_received(event: dict):
        """When an RC email arrives, resume the orchestrator."""
        load_id = event["entity_id"]
        s3_url = event["data"].get("s3_url")
        if s3_url:
            from cortexbot.core.orchestrator import resume_workflow_after_rc
            await resume_workflow_after_rc(load_id, s3_url)

    async def _on_carrier_decision(event: dict):
        """When carrier confirms/rejects via WhatsApp."""
        logger.info(f"Carrier decision event: {event['data']}")

    event_router.register("RC_RECEIVED", _on_rc_received)
    event_router.register("CARRIER_DECISION", _on_carrier_decision)
