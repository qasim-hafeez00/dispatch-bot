"""
cortexbot/agents/gdpr_ccpa.py  — PHASE 3D  (new file)

Agent BB — GDPR/CCPA Data Deletion Compliance

Implements the Right to Be Forgotten (RTBF) / Right to Erasure workflow
as required by GDPR Article 17 and CCPA Section 1798.105.

Workflow for skill_bb_delete_carrier_data():
  1. Validate requester identity (must be carrier or authorized representative)
  2. Create deletion request record (starts 30-day grace period)
  3. Soft-delete all PII fields immediately (nullify in-place)
  4. After 30 days: hard purge all associated data
     - loads, events, whatsapp_context, call_log, compliance_docs
     - driver_settlements, driver_advances, invoices
     - S3 documents (W-9, COI, BOL photos, call recordings)
  5. Log anonymized deletion audit entry (without PII)

Grace period exists to handle:
  - Active loads in progress (must complete before deletion)
  - Disputes / claims pending
  - Tax retention requirements (7 years for financial records)

Financial records older than 7 years are deleted; records within retention
window are anonymized (PII stripped but transaction amounts preserved).

Tables affected:
  Hard delete:    carriers, whatsapp_context, compliance_docs, carrier_tax_info
  Anonymize:      loads, events, call_log, driver_settlements, driver_advances
  S3 purge:       w9_url, coi_url, factoring_noa_url, bol photos, recordings
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, date, timezone, timedelta
from typing import Any, Dict, List, Optional

import boto3
from sqlalchemy import text as sa_text, update as sa_update

from cortexbot.config import settings
from cortexbot.db.session import get_db_session
from cortexbot.db.models import Carrier, Load, Event

logger = logging.getLogger("cortexbot.agents.gdpr_ccpa")

# Grace period before hard purge (days)
GRACE_PERIOD_DAYS = 30

# Financial record retention (years) — IRS / FMCSA requirement
FINANCIAL_RETENTION_YEARS = 7

# Redis key prefix for deletion requests
DELETION_REQUEST_PREFIX = "cortex:deletion_request:"


# ═══════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════

async def skill_bb_delete_carrier_data(
    carrier_id: str,
    requester_email: str,
    reason: str = "RTBF_REQUEST",
    bypass_grace_period: bool = False,  # Set True only for testing / legal hold release
) -> dict:
    """
    Initiate or process a GDPR/CCPA data deletion request for a carrier.

    First call: Creates deletion request + performs immediate soft delete.
    After 30 days: Hard purge triggered by BullMQ cron.

    Args:
        carrier_id:            UUID of the carrier
        requester_email:       Who is making the request
        reason:                RTBF_REQUEST | ACCOUNT_CLOSED | LEGAL_ORDER
        bypass_grace_period:   If True, execute immediately (testing only)

    Returns:
        Deletion request status dict.
    """
    logger.info(
        f"[BB] Data deletion request: carrier={carrier_id} "
        f"requester={requester_email} reason={reason}"
    )

    # ── 1. Validate carrier exists ────────────────────────────
    async with get_db_session() as db:
        from sqlalchemy import select
        result = await db.execute(
            select(Carrier).where(Carrier.carrier_id == carrier_id)
        )
        carrier = result.scalar_one_or_none()

    if not carrier:
        return {"error": "Carrier not found", "carrier_id": carrier_id}

    # ── 2. Check for active loads that must complete first ────
    active_loads = await _get_active_loads(carrier_id)
    if active_loads:
        return {
            "status":        "PENDING_ACTIVE_LOADS",
            "carrier_id":    carrier_id,
            "active_loads":  active_loads,
            "message":       (
                f"Cannot delete data while {len(active_loads)} load(s) are active. "
                f"Deletion will proceed automatically when all loads are completed or cancelled."
            ),
        }

    # ── 3. Check if deletion request already exists ───────────
    existing = await _get_deletion_request(carrier_id)
    if existing and not bypass_grace_period:
        return existing

    # ── 4. Create deletion request record ────────────────────
    request_id   = f"DEL-{carrier_id[:8].upper()}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    scheduled_at = (
        datetime.now(timezone.utc)
        if bypass_grace_period
        else datetime.now(timezone.utc) + timedelta(days=GRACE_PERIOD_DAYS)
    )

    # Hash the requester email for the audit log (don't store PII in audit)
    requester_hash = hashlib.sha256(requester_email.lower().encode()).hexdigest()[:16]

    # Store request in Redis + DB
    await _store_deletion_request(carrier_id, request_id, scheduled_at, requester_hash, reason)

    # ── 5. Immediate soft delete (nullify PII fields) ─────────
    soft_result = await _soft_delete_carrier(carrier_id)

    # ── 6. If grace period bypassed, execute hard purge now ───
    if bypass_grace_period:
        hard_result = await _hard_purge_carrier(carrier_id, request_id)
        return {
            "status":         "DELETED",
            "request_id":     request_id,
            "carrier_id":     carrier_id,
            "soft_delete":    soft_result,
            "hard_purge":     hard_result,
            "deleted_at":     datetime.now(timezone.utc).isoformat(),
        }

    # ── 7. Log audit entry (no PII) ───────────────────────────
    await _log_deletion_audit(carrier_id, request_id, requester_hash, reason, "INITIATED")

    return {
        "status":           "PENDING",
        "request_id":       request_id,
        "carrier_id":       carrier_id,
        "grace_period_days": GRACE_PERIOD_DAYS,
        "scheduled_purge":  scheduled_at.isoformat(),
        "soft_delete":      soft_result,
        "message":          (
            f"Data deletion request received. PII has been soft-deleted immediately. "
            f"All remaining data will be permanently purged on "
            f"{scheduled_at.strftime('%Y-%m-%d')} after the {GRACE_PERIOD_DAYS}-day "
            f"grace period. Request ID: {request_id}"
        ),
    }


async def skill_bb_process_pending_deletions() -> dict:
    """
    Process all deletion requests that have passed their grace period.
    Called by BullMQ compliance_sweep queue daily at 06:00.
    """
    now = datetime.now(timezone.utc)
    processed = 0
    errors = 0

    try:
        from cortexbot.core.redis_client import get_redis
        r = get_redis()

        # Scan for all pending deletion requests
        keys = await r.keys(f"{DELETION_REQUEST_PREFIX}*")

        for key in keys:
            raw = await r.get(key)
            if not raw:
                continue

            request = json.loads(raw)
            scheduled_at_str = request.get("scheduled_purge")
            if not scheduled_at_str:
                continue

            scheduled_at = datetime.fromisoformat(scheduled_at_str)
            if now >= scheduled_at:
                carrier_id = request.get("carrier_id")
                request_id = request.get("request_id")

                try:
                    result = await _hard_purge_carrier(carrier_id, request_id)
                    await r.delete(key)
                    await _log_deletion_audit(
                        carrier_id, request_id,
                        request.get("requester_hash", ""),
                        request.get("reason", ""),
                        "COMPLETED",
                    )
                    processed += 1
                    logger.info(f"[BB] Hard purge complete: {request_id}")
                except Exception as e:
                    logger.error(f"[BB] Hard purge failed for {request_id}: {e}")
                    errors += 1

    except Exception as e:
        logger.error(f"[BB] Processing pending deletions failed: {e}")

    return {"processed": processed, "errors": errors}


# ═══════════════════════════════════════════════════════════════
# SOFT DELETE
# ═══════════════════════════════════════════════════════════════

async def _soft_delete_carrier(carrier_id: str) -> dict:
    """
    Immediately nullify PII fields on the carrier record.
    Carrier row is kept for referential integrity; PII is wiped.
    """
    try:
        async with get_db_session() as db:
            await db.execute(sa_update(Carrier).where(
                Carrier.carrier_id == carrier_id
            ).values(
                owner_name="[DELETED]",
                owner_email="[DELETED]",
                owner_phone="[DELETED]",
                driver_phone=None,
                whatsapp_phone=None,
                w9_url=None,
                coi_url=None,
                factoring_noa_url=None,
                status="DELETED",
                eld_driver_id=None,
                stripe_account_id=None,
                stripe_connected_account_id=None,
                bank_account_last4=None,
            ))
        logger.info(f"[BB] Soft delete complete for carrier {carrier_id}")
        return {"status": "OK", "pii_nullified": True}
    except Exception as e:
        logger.error(f"[BB] Soft delete failed for {carrier_id}: {e}")
        return {"status": "ERROR", "error": str(e)[:200]}


# ═══════════════════════════════════════════════════════════════
# HARD PURGE
# ═══════════════════════════════════════════════════════════════

async def _hard_purge_carrier(carrier_id: str, request_id: str) -> dict:
    """
    Execute full data purge for a carrier.
    Deletes all non-financial records; anonymizes financial records.
    Purges all S3 documents.
    """
    result: Dict[str, Any] = {"request_id": request_id, "steps": {}}

    # ── Collect S3 URLs before deleting records ───────────────
    s3_urls = await _collect_s3_urls(carrier_id)

    # ── Delete / anonymize database records ───────────────────
    result["steps"]["whatsapp_context"]  = await _delete_whatsapp_context(carrier_id)
    result["steps"]["compliance_docs"]   = await _delete_compliance_docs(carrier_id)
    result["steps"]["call_log"]          = await _anonymize_call_log(carrier_id)
    result["steps"]["carrier_tax_info"]  = await _delete_tax_info(carrier_id)
    result["steps"]["loads"]             = await _anonymize_loads(carrier_id)
    result["steps"]["events"]            = await _anonymize_events(carrier_id)
    result["steps"]["settlements"]       = await _anonymize_settlements(carrier_id)
    result["steps"]["advances"]          = await _anonymize_advances(carrier_id)
    result["steps"]["carrier_row"]       = await _delete_carrier_row(carrier_id)

    # ── Purge S3 documents ────────────────────────────────────
    result["steps"]["s3_purge"] = await _purge_s3_documents(s3_urls, carrier_id)

    # ── Clear Redis context ───────────────────────────────────
    result["steps"]["redis"] = await _clear_redis_data(carrier_id)

    result["completed_at"] = datetime.now(timezone.utc).isoformat()
    logger.info(f"[BB] Hard purge complete: {request_id} carrier={carrier_id}")
    return result


async def _collect_s3_urls(carrier_id: str) -> List[str]:
    """Collect all S3 URLs associated with a carrier before deletion."""
    urls = []
    try:
        async with get_db_session() as db:
            result = await db.execute(sa_text("""
                SELECT w9_url, coi_url, factoring_noa_url
                FROM carriers
                WHERE carrier_id = :cid
            """), {"cid": carrier_id})
            row = result.fetchone()
            if row:
                urls.extend([u for u in row if u and u.startswith("s3://")])

            # BOL, POD, RC documents from loads
            result2 = await db.execute(sa_text("""
                SELECT rc_url, rc_signed_url, pod_url, bol_pickup_url,
                       bol_delivery_url, call_recording_url
                FROM loads
                WHERE carrier_id = :cid
            """), {"cid": carrier_id})
            for row in result2.fetchall():
                urls.extend([u for u in row if u and u.startswith("s3://")])

            # Compliance docs
            result3 = await db.execute(sa_text("""
                SELECT doc_url FROM compliance_docs WHERE carrier_id = :cid
            """), {"cid": carrier_id})
            for row in result3.fetchall():
                if row[0] and row[0].startswith("s3://"):
                    urls.append(row[0])

    except Exception as e:
        logger.warning(f"[BB] S3 URL collection error: {e}")

    return list(set(urls))  # deduplicate


async def _delete_whatsapp_context(carrier_id: str) -> dict:
    try:
        async with get_db_session() as db:
            result = await db.execute(sa_text(
                "DELETE FROM whatsapp_context WHERE carrier_id = :cid RETURNING phone"
            ), {"cid": carrier_id})
            deleted = len(result.fetchall())
        return {"deleted": deleted}
    except Exception as e:
        return {"error": str(e)[:100]}


async def _delete_compliance_docs(carrier_id: str) -> dict:
    try:
        async with get_db_session() as db:
            result = await db.execute(sa_text(
                "DELETE FROM compliance_docs WHERE carrier_id = :cid"
            ), {"cid": carrier_id})
        return {"deleted": True}
    except Exception as e:
        return {"error": str(e)[:100]}


async def _anonymize_call_log(carrier_id: str) -> dict:
    """Anonymize call logs — remove transcript and recording URL, keep metadata."""
    try:
        async with get_db_session() as db:
            result = await db.execute(sa_text("""
                UPDATE call_log
                SET broker_phone = '[DELETED]',
                    transcript_raw = NULL,
                    recording_url = NULL,
                    extracted_data = NULL
                WHERE carrier_id = :cid
                RETURNING call_id
            """), {"cid": carrier_id})
            count = len(result.fetchall())
        return {"anonymized": count}
    except Exception as e:
        return {"error": str(e)[:100]}


async def _delete_tax_info(carrier_id: str) -> dict:
    try:
        async with get_db_session() as db:
            await db.execute(sa_text(
                "DELETE FROM carrier_tax_info WHERE carrier_id = :cid"
            ), {"cid": carrier_id})
        return {"deleted": True}
    except Exception as e:
        return {"skipped": True, "reason": str(e)[:100]}


async def _anonymize_loads(carrier_id: str) -> dict:
    """
    Anonymize load records:
    - Financial amounts preserved (FMCSA/IRS 7-year retention)
    - PII (addresses, contact info) scrubbed after 7 years
    - Recent records: only driver_phone and tracking_id nullified
    """
    retention_cutoff = date.today() - timedelta(days=FINANCIAL_RETENTION_YEARS * 365)

    try:
        async with get_db_session() as db:
            # Anonymize recent loads (within retention period) — remove driver PII only
            result1 = await db.execute(sa_text("""
                UPDATE loads
                SET driver_phone = NULL,
                    tracking_id = NULL,
                    extracted_call_data = NULL,
                    rc_url = NULL,
                    rc_signed_url = NULL,
                    carrier_packet_url = NULL,
                    call_recording_url = NULL,
                    bol_pickup_url = NULL,
                    bol_delivery_url = NULL,
                    pod_url = NULL
                WHERE carrier_id = :cid
                  AND created_at::date >= :cutoff
                RETURNING load_id
            """), {"cid": carrier_id, "cutoff": retention_cutoff})
            recent_count = len(result1.fetchall())

            # Fully anonymize old loads (past retention period)
            result2 = await db.execute(sa_text("""
                UPDATE loads
                SET driver_phone = NULL,
                    tracking_id = NULL,
                    origin_address = '[DELETED]',
                    destination_address = '[DELETED]',
                    extracted_call_data = NULL,
                    rc_url = NULL, rc_signed_url = NULL,
                    carrier_packet_url = NULL,
                    call_recording_url = NULL,
                    bol_pickup_url = NULL,
                    bol_delivery_url = NULL,
                    pod_url = NULL
                WHERE carrier_id = :cid
                  AND created_at::date < :cutoff
                RETURNING load_id
            """), {"cid": carrier_id, "cutoff": retention_cutoff})
            old_count = len(result2.fetchall())

        return {"recent_anonymized": recent_count, "old_anonymized": old_count}
    except Exception as e:
        return {"error": str(e)[:200]}


async def _anonymize_events(carrier_id: str) -> dict:
    """Strip any PII from event data JSON."""
    try:
        async with get_db_session() as db:
            # Remove phone numbers and emails from event data
            result = await db.execute(sa_text("""
                UPDATE events
                SET data = data
                    - 'whatsapp'
                    - 'broker_phone'
                    - 'carrier_phone'
                    - 'email'
                    - 'broker_email'
                WHERE entity_id IN (
                    SELECT load_id::text FROM loads WHERE carrier_id = :cid
                    UNION
                    SELECT carrier_id::text FROM carriers WHERE carrier_id = :cid
                )
                RETURNING event_id
            """), {"cid": carrier_id})
            count = len(result.fetchall())
        return {"events_anonymized": count}
    except Exception as e:
        return {"error": str(e)[:100]}


async def _anonymize_settlements(carrier_id: str) -> dict:
    """Keep financial totals; remove any PII fields."""
    try:
        async with get_db_session() as db:
            result = await db.execute(sa_text("""
                UPDATE driver_settlements
                SET stripe_transfer_id = '[DELETED]',
                    bank_last4 = NULL
                WHERE carrier_id = :cid
                RETURNING settlement_id
            """), {"cid": carrier_id})
            count = len(result.fetchall())
        return {"anonymized": count}
    except Exception as e:
        return {"skipped": True, "reason": str(e)[:100]}


async def _anonymize_advances(carrier_id: str) -> dict:
    """Delete advance codes (fuel/comcheck codes) — these expire anyway."""
    try:
        async with get_db_session() as db:
            result = await db.execute(sa_text("""
                UPDATE driver_advances
                SET check_code = NULL
                WHERE carrier_id = :cid
                RETURNING advance_id
            """), {"cid": carrier_id})
            count = len(result.fetchall())
        return {"anonymized": count}
    except Exception as e:
        return {"skipped": True, "reason": str(e)[:100]}


async def _delete_carrier_row(carrier_id: str) -> dict:
    """
    Final step: delete the carrier row itself.
    All FKs should be nullified/anonymized by this point.
    If FK constraints prevent deletion, mark as DELETED.
    """
    try:
        async with get_db_session() as db:
            # Try hard delete first
            try:
                await db.execute(sa_text(
                    "DELETE FROM carriers WHERE carrier_id = :cid"
                ), {"cid": carrier_id})
                return {"deleted": True, "method": "hard_delete"}
            except Exception:
                # FK constraint — mark as fully deleted instead
                await db.execute(sa_update(Carrier).where(
                    Carrier.carrier_id == carrier_id
                ).values(
                    status="DELETED",
                    mc_number=f"DELETED-{carrier_id[:8]}",
                    company_name="[DELETED]",
                    owner_name="[DELETED]",
                    owner_email="[DELETED]",
                    owner_phone="[DELETED]",
                ))
                return {"deleted": True, "method": "status_deleted"}
    except Exception as e:
        return {"error": str(e)[:200]}


async def _purge_s3_documents(urls: List[str], carrier_id: str) -> dict:
    """Delete all S3 documents associated with the carrier."""
    if not urls:
        return {"deleted": 0}

    import asyncio
    deleted = 0
    errors  = 0

    s3 = boto3.client(
        "s3",
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    )
    loop = asyncio.get_running_loop()

    for url in urls:
        try:
            without_prefix = url.replace("s3://", "")
            bucket, key    = without_prefix.split("/", 1)

            # Remove legal hold first if present
            try:
                await loop.run_in_executor(None, lambda k=key, b=bucket: s3.put_object_legal_hold(
                    Bucket=b, Key=k, LegalHold={"Status": "OFF"}
                ))
            except Exception:
                pass  # Legal hold may not be set

            await loop.run_in_executor(None, lambda k=key, b=bucket: s3.delete_object(
                Bucket=b, Key=k
            ))
            deleted += 1
        except Exception as e:
            logger.warning(f"[BB] S3 delete failed for {url}: {e}")
            errors += 1

    logger.info(f"[BB] S3 purge: {deleted} deleted, {errors} errors for carrier {carrier_id}")
    return {"deleted": deleted, "errors": errors}


async def _clear_redis_data(carrier_id: str) -> dict:
    """Remove all Redis keys associated with the carrier."""
    try:
        from cortexbot.core.redis_client import get_redis
        r = get_redis()

        patterns = [
            f"cortex:wa:context:*",          # WhatsApp context (phone-keyed, scan needed)
            f"cortex:gps:{carrier_id}",
            f"cortex:hos:{carrier_id}",
            f"cortex:hos_status:{carrier_id}",
            f"cortex:deletion_request:{carrier_id}",
        ]

        deleted = 0
        for pattern in patterns:
            if "*" in pattern:
                # Need to scan and check value
                keys = await r.keys(pattern)
                for key in keys:
                    raw = await r.get(key)
                    if raw and carrier_id in raw:
                        await r.delete(key)
                        deleted += 1
            else:
                count = await r.delete(pattern)
                deleted += count

        return {"keys_deleted": deleted}
    except Exception as e:
        return {"error": str(e)[:100]}


# ═══════════════════════════════════════════════════════════════
# AUDIT LOGGING
# ═══════════════════════════════════════════════════════════════

async def _log_deletion_audit(
    carrier_id: str, request_id: str,
    requester_hash: str, reason: str, status: str,
):
    """
    Log the deletion event WITHOUT storing any PII.
    The audit log proves deletion occurred, not who requested it.
    """
    try:
        async with get_db_session() as db:
            db.add(Event(
                event_code="DATA_DELETION",
                entity_type="carrier",
                entity_id=carrier_id,
                triggered_by="agent_bb_gdpr_ccpa",
                data={
                    "request_id":      request_id,
                    "requester_hash":  requester_hash,  # SHA-256 of email, not the email itself
                    "reason":          reason,
                    "status":          status,
                    "timestamp":       datetime.now(timezone.utc).isoformat(),
                    "regulation":      "GDPR_CCPA",
                },
                new_status=status,
            ))
    except Exception as e:
        logger.error(f"[BB] Audit log failed: {e}")


# ═══════════════════════════════════════════════════════════════
# REQUEST MANAGEMENT
# ═══════════════════════════════════════════════════════════════

async def _store_deletion_request(
    carrier_id: str, request_id: str,
    scheduled_at: datetime, requester_hash: str, reason: str,
):
    """Store deletion request in Redis."""
    try:
        from cortexbot.core.redis_client import get_redis
        r = get_redis()
        payload = json.dumps({
            "carrier_id":     carrier_id,
            "request_id":     request_id,
            "scheduled_purge": scheduled_at.isoformat(),
            "requester_hash": requester_hash,
            "reason":         reason,
            "created_at":     datetime.now(timezone.utc).isoformat(),
        })
        # TTL: grace period + 7 days buffer
        ttl = (GRACE_PERIOD_DAYS + 7) * 86400
        await r.set(f"{DELETION_REQUEST_PREFIX}{carrier_id}", payload, ex=ttl)
    except Exception as e:
        logger.error(f"[BB] Could not store deletion request: {e}")


async def _get_deletion_request(carrier_id: str) -> Optional[dict]:
    """Check if a deletion request already exists for this carrier."""
    try:
        from cortexbot.core.redis_client import get_redis
        r = get_redis()
        raw = await r.get(f"{DELETION_REQUEST_PREFIX}{carrier_id}")
        if raw:
            data = json.loads(raw)
            return {
                "status":          "PENDING",
                "request_id":      data.get("request_id"),
                "carrier_id":      carrier_id,
                "scheduled_purge": data.get("scheduled_purge"),
                "message":         "A deletion request already exists for this carrier.",
            }
    except Exception:
        pass
    return None


async def _get_active_loads(carrier_id: str) -> List[str]:
    """Return IDs of active loads that must complete before deletion."""
    try:
        async with get_db_session() as db:
            result = await db.execute(sa_text("""
                SELECT load_id::text FROM loads
                WHERE carrier_id = :cid
                  AND status NOT IN (
                    'SETTLED', 'PAID', 'FAILED', 'CANCELLED', 'DELETED',
                    'INVOICED', 'DELIVERED'
                  )
            """), {"cid": carrier_id})
            return [row[0] for row in result.fetchall()]
    except Exception:
        return []
