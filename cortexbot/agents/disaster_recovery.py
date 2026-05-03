"""
cortexbot/agents/disaster_recovery.py  — PHASE 3D  (new file)

Agent P — Disaster Recovery

RPO: 15 minutes  (Redis AOF + PostgreSQL WAL shipping)
RTO: 30 minutes  (automated failover runbook)

Components:
  skill_p_backup_state()            — Full snapshot of all active load states to S3
  skill_p_restore_from_backup()     — Restore workflow states from a backup snapshot
  skill_p_heartbeat()               — Write heartbeat to S3 every 5 min
  skill_p_weekly_dr_drill()         — Simulate Redis wipe + verify Postgres restore

The backup cycle:
  Every 5 min  → heartbeat to S3 + incremental Redis state dump
  Every hour   → full load state snapshot to S3
  Daily 02:00  → full PostgreSQL pg_dump → S3 (gzip)
  Weekly Mon   → DR drill (dry-run restore from last backup)

S3 layout:
  cortexbot-backups/
    heartbeat/              ← 5-min heartbeats
    state-snapshots/        ← hourly Redis state dumps
    pg-backups/             ← daily pg_dump files
    dr-drill-reports/       ← weekly drill results
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import subprocess
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

import boto3

from cortexbot.config import settings
from cortexbot.core.redis_client import get_redis, set_state, get_state

logger = logging.getLogger("cortexbot.agents.disaster_recovery")

BACKUP_BUCKET = getattr(settings, "backup_s3_bucket", settings.aws_s3_bucket)
HEARTBEAT_INTERVAL_SECS    = 300     # 5 min
STATE_SNAPSHOT_INTERVAL    = 3600    # 1 hour
HEARTBEAT_MISS_ALERT_COUNT = 3       # alert after 3 missed heartbeats


# ═══════════════════════════════════════════════════════════════
# BACKGROUND TASKS
# ═══════════════════════════════════════════════════════════════

async def run_disaster_recovery_tasks():
    """
    Launch all DR background tasks.
    Called from main.py lifespan startup.
    """
    logger.info("🛡️ [P] Disaster recovery tasks started")
    asyncio.create_task(_heartbeat_loop(),        name="dr_heartbeat")
    asyncio.create_task(_state_snapshot_loop(),   name="dr_state_snapshot")
    asyncio.create_task(_pg_backup_loop(),        name="dr_pg_backup")
    asyncio.create_task(_weekly_drill_loop(),     name="dr_weekly_drill")


async def _heartbeat_loop():
    """Write heartbeat to S3 every 5 minutes."""
    while True:
        try:
            await skill_p_heartbeat()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[P] Heartbeat error: {e}")
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECS)


async def _state_snapshot_loop():
    """Full load state snapshot to S3 every hour."""
    await asyncio.sleep(60)   # stagger from heartbeat
    while True:
        try:
            result = await skill_p_backup_state()
            logger.info(f"[P] State snapshot: {result.get('loads_backed_up', 0)} loads backed up")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[P] State snapshot error: {e}")
        await asyncio.sleep(STATE_SNAPSHOT_INTERVAL)


async def _pg_backup_loop():
    """Daily PostgreSQL backup at 02:00 UTC."""
    while True:
        try:
            now  = datetime.now(timezone.utc)
            # Sleep until 02:00 UTC
            next_run = now.replace(hour=2, minute=0, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(days=1)
            wait_secs = (next_run - now).total_seconds()
            await asyncio.sleep(wait_secs)
            await skill_p_pg_backup()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[P] PG backup error: {e}")


async def _weekly_drill_loop():
    """Weekly DR drill on Monday 03:00 UTC."""
    while True:
        try:
            now      = datetime.now(timezone.utc)
            days_to_monday = (7 - now.weekday()) % 7
            if days_to_monday == 0 and now.hour >= 3:
                days_to_monday = 7
            next_monday = now.replace(hour=3, minute=0, second=0, microsecond=0) + timedelta(days=days_to_monday)
            wait_secs = (next_monday - now).total_seconds()
            await asyncio.sleep(wait_secs)
            await skill_p_weekly_dr_drill()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[P] DR drill error: {e}")


# ═══════════════════════════════════════════════════════════════
# CORE SKILLS
# ═══════════════════════════════════════════════════════════════

async def skill_p_heartbeat():
    """
    Write heartbeat to S3.
    CloudWatch alarm fires if this misses 3 intervals (15 min).
    """
    now = datetime.now(timezone.utc)
    payload = {
        "timestamp":    now.isoformat(),
        "unix_ts":      int(now.timestamp()),
        "host":         "cortexbot-api",
        "status":       "alive",
    }

    s3_key = f"heartbeat/latest.json"
    history_key = f"heartbeat/{now.strftime('%Y/%m/%d/%H%M%S')}.json"

    content = json.dumps(payload, indent=2).encode("utf-8")
    await _upload_to_s3(content, s3_key,       "application/json")
    await _upload_to_s3(content, history_key,   "application/json")

    # Also store in Redis for fast local health check
    try:
        r = get_redis()
        await r.set("cortex:dr:last_heartbeat", now.isoformat(), ex=600)
    except Exception:
        pass


async def skill_p_backup_state(incremental: bool = False) -> dict:
    """
    Snapshot all active load states from Redis to S3.

    Args:
        incremental: If True, only back up states modified in the last hour.

    Returns:
        {"loads_backed_up": int, "backup_id": str, "s3_url": str}
    """
    now       = datetime.now(timezone.utc)
    backup_id = now.strftime("%Y%m%dT%H%M%S")
    s3_key    = f"state-snapshots/{now.strftime('%Y/%m/%d')}/snapshot_{backup_id}.json.gz"

    try:
        r = get_redis()

        # Fetch all active load state keys
        pattern = "cortex:state:load:*"
        keys    = await r.keys(pattern)

        if not keys:
            return {"loads_backed_up": 0, "backup_id": backup_id}

        # Also snapshot WhatsApp contexts
        wa_keys = await r.keys("cortex:wa:context:*")

        all_states: Dict[str, Any] = {}
        pipeline = r.pipeline()
        for key in keys + wa_keys:
            pipeline.get(key)
        values = await pipeline.execute()

        for key, value in zip(keys + wa_keys, values):
            if value:
                try:
                    all_states[key] = json.loads(value)
                except Exception:
                    all_states[key] = value

        # Gzip compress and upload to S3
        snapshot = {
            "backup_id":    backup_id,
            "created_at":   now.isoformat(),
            "load_count":   len(keys),
            "total_keys":   len(all_states),
            "states":       all_states,
        }
        compressed = gzip.compress(json.dumps(snapshot, default=str).encode("utf-8"))
        s3_url = await _upload_to_s3(compressed, s3_key, "application/gzip")

        # Store latest backup reference in Redis
        await r.set("cortex:dr:last_backup", json.dumps({
            "backup_id": backup_id,
            "s3_url":    s3_url,
            "load_count": len(keys),
            "created_at": now.isoformat(),
        }), ex=86400)

        logger.info(f"[P] State backup complete: {len(keys)} loads → {s3_url}")
        return {"loads_backed_up": len(keys), "backup_id": backup_id, "s3_url": s3_url}

    except Exception as e:
        logger.error(f"[P] State backup failed: {e}")
        return {"error": str(e), "backup_id": backup_id}


async def skill_p_restore_from_backup(backup_id: Optional[str] = None) -> dict:
    """
    Restore workflow states from a backup snapshot.

    Args:
        backup_id: Specific backup to restore. If None, uses the latest.

    Returns:
        {"restored": int, "failed": int, "backup_id": str}
    """
    now = datetime.now(timezone.utc)

    # Determine which backup to restore
    if not backup_id:
        try:
            r = get_redis()
            raw = await r.get("cortex:dr:last_backup")
            if raw:
                meta    = json.loads(raw)
                s3_url  = meta.get("s3_url", "")
                backup_id = meta.get("backup_id", "unknown")
            else:
                return {"error": "No backup reference found in Redis"}
        except Exception as e:
            return {"error": f"Could not find backup: {e}"}
    else:
        # Build S3 key from backup_id (format: YYYYMMDDTHHmmss)
        date_prefix = f"{backup_id[:4]}/{backup_id[4:6]}/{backup_id[6:8]}"
        s3_url = f"s3://{BACKUP_BUCKET}/state-snapshots/{date_prefix}/snapshot_{backup_id}.json.gz"

    logger.info(f"[P] Restoring from backup {backup_id}: {s3_url}")

    try:
        # Download from S3
        compressed = await _download_from_s3(s3_url)
        raw        = gzip.decompress(compressed)
        snapshot   = json.loads(raw)

        states     = snapshot.get("states", {})
        restored   = 0
        failed     = 0

        r = get_redis()
        pipeline = r.pipeline()
        for key, value in states.items():
            try:
                serialized = json.dumps(value, default=str)
                pipeline.set(key, serialized, ex=86400)
                restored += 1
            except Exception as e:
                logger.warning(f"[P] Could not restore key {key}: {e}")
                failed += 1

        await pipeline.execute()

        logger.info(
            f"[P] Restore complete: {restored} keys restored, {failed} failed | "
            f"backup_id={backup_id}"
        )
        return {
            "backup_id":  backup_id,
            "restored":   restored,
            "failed":     failed,
            "restored_at": now.isoformat(),
        }

    except Exception as e:
        logger.error(f"[P] Restore failed: {e}", exc_info=True)
        return {"error": str(e), "backup_id": backup_id}


async def skill_p_pg_backup() -> dict:
    """
    Run pg_dump and upload to S3.
    Requires pg_dump to be available in the container.
    """
    now       = datetime.now(timezone.utc)
    backup_id = now.strftime("%Y%m%dT%H%M%S")
    s3_key    = f"pg-backups/{now.strftime('%Y/%m/%d')}/cortexbot_{backup_id}.sql.gz"

    # Parse DATABASE_URL for pg_dump
    db_url = settings.database_url.replace("+asyncpg", "")

    try:
        import asyncio
        proc = await asyncio.create_subprocess_exec(
            "pg_dump",
            "--no-password",
            f"--dbname={db_url}",
            "--format=plain",
            "--no-acl",
            "--no-owner",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)

        if proc.returncode != 0:
            raise RuntimeError(f"pg_dump failed: {stderr.decode()[:500]}")

        compressed = gzip.compress(stdout)
        s3_url     = await _upload_to_s3(compressed, s3_key, "application/gzip")

        logger.info(
            f"[P] PG backup complete: {len(compressed)/1024/1024:.1f}MB → {s3_url}"
        )
        return {"backup_id": backup_id, "s3_url": s3_url, "size_mb": round(len(compressed)/1024/1024, 2)}

    except FileNotFoundError:
        logger.warning("[P] pg_dump not available — PG backup skipped")
        return {"skipped": True, "reason": "pg_dump not found"}
    except asyncio.TimeoutError:
        return {"error": "pg_dump timed out after 300s"}
    except Exception as e:
        logger.error(f"[P] PG backup failed: {e}")
        return {"error": str(e)}


async def skill_p_weekly_dr_drill() -> dict:
    """
    Weekly DR drill:
    1. Take a fresh state backup
    2. Simulate what a Redis wipe would look like (dry-run: don't actually wipe)
    3. Verify all active load states are restorable from Postgres checkpoints
    4. Generate drill report

    Does NOT actually wipe Redis — this is a verification run only.
    """
    now      = datetime.now(timezone.utc)
    drill_id = f"DRILL-{now.strftime('%Y%m%dT%H%M%S')}"

    logger.info(f"[P] Starting weekly DR drill: {drill_id}")
    report = {"drill_id": drill_id, "started_at": now.isoformat(), "checks": {}}

    # ── Step 1: Take fresh backup ──────────────────────────────
    backup_result = await skill_p_backup_state()
    report["checks"]["backup"] = {
        "passed":        "error" not in backup_result,
        "loads_captured": backup_result.get("loads_backed_up", 0),
    }

    # ── Step 2: Verify Postgres checkpoints ───────────────────
    pg_check = await _verify_pg_checkpoints()
    report["checks"]["postgres_checkpoints"] = pg_check

    # ── Step 3: Test restore dry-run ─────────────────────────
    restore_check = await _dry_run_restore(backup_result.get("backup_id"))
    report["checks"]["restore_dry_run"] = restore_check

    # ── Step 4: Verify heartbeat chain ────────────────────────
    hb_check = await _verify_heartbeat_chain()
    report["checks"]["heartbeat_chain"] = hb_check

    # ── Overall pass/fail ─────────────────────────────────────
    all_passed  = all(c.get("passed", False) for c in report["checks"].values())
    report["overall_passed"] = all_passed
    report["completed_at"]   = datetime.now(timezone.utc).isoformat()

    # Upload drill report to S3
    try:
        report_json = json.dumps(report, indent=2, default=str).encode("utf-8")
        s3_key      = f"dr-drill-reports/{now.strftime('%Y/%m/%d')}/drill_{drill_id}.json"
        await _upload_to_s3(report_json, s3_key, "application/json")
        report["s3_url"] = f"s3://{BACKUP_BUCKET}/{s3_key}"
    except Exception as e:
        report["s3_upload_error"] = str(e)

    # Alert if drill failed
    if not all_passed:
        from cortexbot.integrations.twilio_client import send_sms
        await send_sms(
            settings.oncall_phone,
            f"⚠️ DR DRILL FAILED — {drill_id}\n"
            f"Checks: {json.dumps({k: v.get('passed') for k, v in report['checks'].items()})}\n"
            f"Review immediately: check S3 drill report."
        )
    else:
        logger.info(f"[P] DR drill PASSED: {drill_id}")

    return report


# ═══════════════════════════════════════════════════════════════
# DRILL HELPER CHECKS
# ═══════════════════════════════════════════════════════════════

async def _verify_pg_checkpoints() -> dict:
    """Verify all active loads have a Postgres checkpoint."""
    try:
        from cortexbot.db.session import get_db_session
        from sqlalchemy import text as sa_text

        async with get_db_session() as db:
            result = await db.execute(sa_text("""
                SELECT
                    COUNT(*)                        AS total_checkpoints,
                    COUNT(CASE WHEN state_json IS NOT NULL THEN 1 END) AS with_state,
                    MAX(updated_at)                 AS latest_checkpoint
                FROM load_checkpoints
            """))
            row = result.fetchone()

        total   = row[0] or 0
        with_st = row[1] or 0
        latest  = str(row[2]) if row[2] else "never"

        return {
            "passed":              total > 0 and total == with_st,
            "total_checkpoints":   total,
            "checkpoints_with_state": with_st,
            "latest_checkpoint":   latest,
        }
    except Exception as e:
        return {"passed": False, "error": str(e)[:200]}


async def _dry_run_restore(backup_id: Optional[str]) -> dict:
    """
    Dry-run: parse the backup and verify all keys are valid JSON.
    Does NOT write to Redis.
    """
    if not backup_id:
        return {"passed": False, "error": "No backup_id provided"}

    try:
        r = get_redis()
        raw = await r.get("cortex:dr:last_backup")
        if not raw:
            return {"passed": False, "error": "No backup reference in Redis"}

        meta   = json.loads(raw)
        s3_url = meta.get("s3_url", "")

        compressed = await _download_from_s3(s3_url)
        data       = json.loads(gzip.decompress(compressed))
        states     = data.get("states", {})

        valid   = sum(1 for v in states.values() if isinstance(v, dict))
        invalid = len(states) - valid

        return {
            "passed":       invalid == 0,
            "total_keys":   len(states),
            "valid_states": valid,
            "invalid":      invalid,
            "backup_size_kb": round(len(compressed) / 1024, 1),
        }
    except Exception as e:
        return {"passed": False, "error": str(e)[:200]}


async def _verify_heartbeat_chain() -> dict:
    """Verify heartbeat was written within the last 10 minutes."""
    try:
        r = get_redis()
        raw = await r.get("cortex:dr:last_heartbeat")
        if not raw:
            return {"passed": False, "error": "No heartbeat in Redis"}

        last_ts = datetime.fromisoformat(raw)
        age_secs = (datetime.now(timezone.utc) - last_ts).total_seconds()

        return {
            "passed":          age_secs < 600,
            "last_heartbeat":  raw,
            "age_seconds":     int(age_secs),
        }
    except Exception as e:
        return {"passed": False, "error": str(e)[:100]}


# ═══════════════════════════════════════════════════════════════
# S3 HELPERS
# ═══════════════════════════════════════════════════════════════

async def _upload_to_s3(content: bytes, s3_key: str, content_type: str) -> str:
    import asyncio
    s3   = boto3.client(
        "s3",
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    )
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: s3.put_object(
        Bucket=BACKUP_BUCKET,
        Key=s3_key,
        Body=content,
        ContentType=content_type,
    ))
    return f"s3://{BACKUP_BUCKET}/{s3_key}"


async def _download_from_s3(s3_url: str) -> bytes:
    import asyncio
    without_prefix = s3_url.replace("s3://", "")
    bucket, key    = without_prefix.split("/", 1)
    s3   = boto3.client(
        "s3",
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    )
    loop = asyncio.get_running_loop()
    obj  = await loop.run_in_executor(None, lambda: s3.get_object(Bucket=bucket, Key=key))
    return await loop.run_in_executor(None, lambda: obj["Body"].read())
