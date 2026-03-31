"""
cortexbot/core/queue_manager.py

Redis-backed Job Queue Manager

Produces jobs for the BullMQ workers (Node.js side).
Skills call enqueue_job() and the worker picks them up.

Queue architecture:
  Python (FastAPI) → Redis queue → Node.js (BullMQ worker) → back to Python API

This pattern gives us:
- Automatic retry with exponential backoff
- Job prioritization
- Rate limiting
- Visual dashboard (Bull Board at :3001)
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("cortexbot.core.queue_manager")

# Queue name constants — must match workers/index.js
QUEUE_BROKER_CALLS    = "cortex:queue:broker_calls"
QUEUE_CARRIER_CONFIRM = "cortex:queue:carrier_confirm"
QUEUE_EMAIL_PROCESS   = "cortex:queue:email_process"
QUEUE_DOCUMENT_OCR    = "cortex:queue:document_ocr"
QUEUE_LOAD_SEARCH     = "cortex:queue:load_search"
QUEUE_DISPATCH_SHEET  = "cortex:queue:dispatch_sheet"


class QueueManager:
    """
    Enqueues jobs into BullMQ-compatible Redis queues.

    BullMQ uses a specific Redis key format:
    - bull:<queue_name>:waiting  (sorted set)
    - bull:<queue_name>:id       (counter)
    - bull:<queue_name>:<job_id> (hash with job data)
    """

    async def enqueue_job(
        self,
        queue_name: str,
        job_data: Dict[str, Any],
        priority: int = 0,
        delay_ms: int = 0,
        attempts: int = 3,
        job_name: str = "default",
    ) -> Optional[str]:
        """
        Enqueue a job for the BullMQ worker.

        Args:
            queue_name: One of the QUEUE_* constants above
            job_data: JSON-serializable job payload
            priority: Lower = higher priority (0 is highest)
            delay_ms: Delay before processing (milliseconds)
            attempts: Number of retry attempts
            job_name: Human-readable job name for dashboard

        Returns:
            Job ID string, or None on failure
        """
        try:
            from cortexbot.core.redis_client import get_redis

            redis = await get_redis()
            if not redis:
                logger.error("Redis not available — cannot enqueue job")
                return None

            # Get next job ID
            job_id = await redis.incr(f"bull:{queue_name}:id")
            job_id_str = str(job_id)

            # Build BullMQ-compatible job hash
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            job_hash = {
                "name": job_name,
                "data": json.dumps(job_data),
                "opts": json.dumps({
                    "attempts": attempts,
                    "delay": delay_ms,
                    "priority": priority,
                    "backoff": {"type": "exponential", "delay": 5000},
                    "removeOnComplete": 100,
                    "removeOnFail": 50,
                }),
                "timestamp": now_ms,
                "delay": delay_ms,
                "priority": priority,
                "processedOn": 0,
                "finishedOn": 0,
            }

            # Store job data
            await redis.hset(f"bull:{queue_name}:{job_id_str}", mapping=job_hash)

            # Add to waiting list
            if delay_ms > 0:
                # Delayed job — add to delayed set
                process_at = now_ms + delay_ms
                await redis.zadd(f"bull:{queue_name}:delayed", {job_id_str: process_at})
            else:
                # Immediate job — add to waiting list with priority
                score = priority * 1e13 + now_ms  # Priority + timestamp for ordering
                await redis.zadd(f"bull:{queue_name}:waiting", {job_id_str: score})

            # Notify workers via pub/sub
            await redis.publish(f"bull:{queue_name}:waiting", job_id_str)

            logger.info(
                f"📋 Job enqueued: {queue_name}/{job_name} "
                f"(id={job_id_str}, priority={priority})"
            )
            return job_id_str

        except Exception as e:
            logger.error(f"Failed to enqueue job on {queue_name}: {e}")
            return None

    async def enqueue_load_search(
        self,
        carrier_id: str,
        current_city: str = None,
        current_state: str = None,
        trigger: str = "manual",
    ) -> Optional[str]:
        """Convenience: enqueue a load search job."""
        return await self.enqueue_job(
            QUEUE_LOAD_SEARCH,
            {
                "carrier_id": carrier_id,
                "current_city": current_city,
                "current_state": current_state,
                "trigger": trigger,
            },
            job_name="load_search",
        )

    async def enqueue_email_processing(self, email_id: str) -> Optional[str]:
        """Convenience: enqueue an email for processing."""
        return await self.enqueue_job(
            QUEUE_EMAIL_PROCESS,
            {"email_id": email_id},
            job_name="email_process",
        )

    async def enqueue_ocr(self, load_id: str, s3_url: str) -> Optional[str]:
        """Convenience: enqueue a document for OCR."""
        return await self.enqueue_job(
            QUEUE_DOCUMENT_OCR,
            {"load_id": load_id, "s3_url": s3_url},
            job_name="document_ocr",
        )


# Module-level singleton
queue_manager = QueueManager()
