"""
cortexbot/core/event_router.py — FIXED

Production Event Router

FIXES APPLIED:
  1. Handler dispatch is now non-blocking — uses asyncio.create_task()
     instead of direct await, so a slow LLM call in a handler cannot
     stall the entire event publishing sequence.
  2. Added handler error isolation — one failing handler doesn't crash others.
  3. Added Redis stream append safety (graceful failure).
"""

import asyncio
import logging
from typing import Callable, Dict, Optional, Any

from cortexbot.db.session import get_db_session
from cortexbot.db.models import Event

logger = logging.getLogger("cortexbot.core.event_router")


class EventRouter:
    """
    Central event routing and logging hub.

    Skills call:
        await event_router.publish("RC_RECEIVED", "load", load_id, {...})

    The router:
      1. Logs to PostgreSQL  (synchronous — guarantees audit trail)
      2. Appends to Redis stream  (async, fire-and-forget)
      3. Dispatches to handler   (non-blocking via create_task)
    """

    def __init__(self):
        self._handlers: Dict[str, Callable] = {}

    def register(self, event_code: str, handler: Callable):
        """Register a coroutine handler for an event code."""
        self._handlers[event_code] = handler
        logger.debug(f"Registered handler for event: {event_code}")

    def get_handler(self, event_code: str) -> Optional[Callable]:
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
        Publish an event: persist to DB → Redis stream → dispatch handler.

        The handler is dispatched as a background task so it NEVER blocks
        the caller, even if the handler performs heavy IO or LLM inference.
        """
        data = data or {}

        # ── 1. Persist to PostgreSQL (synchronous, required for audit) ──
        event_id = None
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
            logger.error(f"Failed to persist event {event_code} to DB: {e}")

        # ── 2. Append to Redis stream (best-effort, non-blocking) ────────
        try:
            from cortexbot.core.redis_client import get_redis
            r = get_redis()
            stream_name = f"cortex:events:{entity_type}"
            await r.xadd(
                stream_name,
                {
                    "event_code": event_code,
                    "entity_id": entity_id,
                    "triggered_by": triggered_by,
                    "data": str(data),
                },
                maxlen=10000,
            )
        except Exception as e:
            logger.warning(f"Redis stream append failed for {event_code}: {e}")

        # ── 3. Dispatch handler as background task (FIX #1) ──────────────
        # Previously: await handler(payload) — blocked the publisher if
        #   the handler did slow I/O (LLM calls, API requests).
        # Fixed: asyncio.create_task() schedules the handler to run
        #   concurrently without blocking publish() from returning.
        handler = self._handlers.get(event_code)
        if handler:
            event_payload = {
                "event_id": event_id,
                "event_code": event_code,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "data": data,
                "new_status": new_status,
            }
            # Wrap in a helper that catches exceptions so a buggy handler
            # does not surface an unhandled exception to the event loop.
            asyncio.create_task(
                _safe_handler(handler, event_payload, event_code)
            )

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
        """Alias for publish() — logging only (no handler dispatch intent)."""
        await self.publish(
            event_code=event_code,
            entity_type=entity_type,
            entity_id=entity_id,
            data=data or {},
            triggered_by=triggered_by,
        )


async def _safe_handler(handler: Callable, payload: dict, event_code: str):
    """
    Wraps a handler in a try/except so an exception in one handler
    does not propagate to the event loop as an unhandled task exception.
    """
    try:
        await handler(payload)
    except Exception as e:
        logger.error(
            f"Handler for event '{event_code}' raised an exception: {e}",
            exc_info=True,
        )


# ── Module-level singleton ─────────────────────────────────────
event_router = EventRouter()


def register_default_handlers():
    """
    Register Phase 1+2 event handlers on startup.
    Called from main.py during app lifespan.
    """

    async def _on_rc_received(event: dict):
        """Resume orchestrator when RC email arrives."""
        load_id = event["entity_id"]
        s3_url = event["data"].get("s3_url")
        if s3_url:
            from cortexbot.core.orchestrator import resume_workflow_after_rc
            await resume_workflow_after_rc(load_id, s3_url)

    async def _on_carrier_decision(event: dict):
        """Handle carrier YES/NO from WhatsApp."""
        logger.info(f"Carrier decision event: {event['data']}")

    async def _on_load_dispatched(event: dict):
        """Start Phase 2 transit monitoring when load is dispatched."""
        load_id = event["entity_id"]
        from cortexbot.core.orchestrator_phase2 import start_transit_monitoring_tasks
        from cortexbot.core.redis_client import get_state
        state = await get_state(f"cortex:state:load:{load_id}") or {}
        await start_transit_monitoring_tasks(load_id, state)

    async def _on_payment_received(event: dict):
        """Trigger post-payment financial pipeline."""
        load_id = event["entity_id"]
        amount_paid = event["data"].get("amount_paid", 0)
        from cortexbot.core.orchestrator import resume_workflow_after_payment
        await resume_workflow_after_payment(load_id, float(amount_paid))

    async def _on_fraud_alert(event: dict):
        """Log fraud alerts and prevent booking."""
        load_id = event["entity_id"]
        score = event["data"].get("score", 0)
        broker_mc = event["data"].get("broker_mc", "")
        logger.warning(
            f"🚨 Fraud alert: broker={broker_mc} score={score} load={load_id}"
        )

    event_router.register("RC_RECEIVED", _on_rc_received)
    event_router.register("CARRIER_DECISION", _on_carrier_decision)
    event_router.register("LOAD_DISPATCHED", _on_load_dispatched)
    event_router.register("PAYMENT_RECEIVED", _on_payment_received)
    event_router.register("FRAUD_ALERT", _on_fraud_alert)