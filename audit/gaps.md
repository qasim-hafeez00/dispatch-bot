# CortexBot — Identified Gaps and Risks

**Snapshot:** 2026-05-02 (Phase 3E complete)

Gap severity: 🔴 Critical | 🟠 High | 🟡 Medium | 🟢 Low

---

## Architecture Gaps

### 🔴 GAP-A1: Webhook routing split (bland.py vs bland_ai.py)

`main.py` routes Bland AI webhooks through `bland_ai.py → voice_calling.py`. Tests use `bland.py`. Two parallel webhook paths exist — only one will receive production events.

**Risk**: If `main.py` doesn't wire up `bland.py`, the test contract is disconnected from production behavior.  
**Fix**: Verify `main.py` uses `bland.py`'s `handle_bland_webhook`, or consolidate the two files.

---

### 🔴 GAP-A2: UUID type enforcement

All DB models use `UUID(as_uuid=True)` columns. Any code that passes a string like `"test-carrier-001"` as `carrier_id` crashes at SQLAlchemy's type processor with `AttributeError: 'str' object has no attribute 'hex'`. This affects:
- `start_dispatch_workflow()` (called with carrier_id from HTTP request)
- Any test that doesn't use real UUIDs

**Risk**: Unhandled exception in production if carrier_id validation is missing at API gateway.  
**Fix**: Add UUID validation in `/api/dispatch/start` route handler; use `str(uuid.uuid4())` in test fixtures.

---

### 🔴 GAP-A3: Checkpoint save always fails in tests

`_save_checkpoint()` tries to write `LoadCheckpoint(load_id=state["load_id"], ...)` to the DB. Since `load_id = "test-load-001"` (a string, not UUID), the write fails with SQLAlchemy error. It's caught and logged as WARNING, so tests don't break — but the checkpoint is never written.

**Risk**: In production, if the load_id format is wrong, workflow state is never persisted to Postgres. Crash recovery via DB fallback would fail.  
**Fix**: Ensure `load_id` is always a valid UUID string (`str(uuid.uuid4())`); add assertion in `_save_checkpoint`.

---

### 🟠 GAP-A4: background_worker.py calls non-existent method

`workers/background_worker.py` calls `queue_manager.process_tasks()` which does not exist in `queue_manager.py`. The actual BullMQ processing is in Node.js (`workers/index.js`).

**Risk**: Running `python workers/background_worker.py` directly would crash.  
**Fix**: Either implement `process_tasks()` in Python or document that this file is a placeholder.

---

### 🟠 GAP-A5: In-memory circuit breakers reset on restart

`api_gateway.py`'s `CircuitBreaker` state is in-process only. A deploy resets all circuit states — including a breaker that was OPEN to protect a failing dependency.

**Risk**: Post-deploy, the first batch of requests to a failing API will all fail before the circuit opens again.  
**Fix**: Persist circuit state to Redis with TTL.

---

### 🟡 GAP-A6: No dead-letter queue for event handler failures

`event_router.py` uses `asyncio.create_task(_safe_handler(...))`. If a handler fails (even with _safe_handler catching), there's no retry or dead-letter mechanism.

**Risk**: `RC_RECEIVED` event processing failure means the load stays suspended indefinitely.  
**Fix**: Add retry logic or push failed events to a Redis dead-letter list.

---

## Security Gaps

### 🟠 GAP-S1: ELD webhook signature verification only soft-fails

If `SAMSARA_WEBHOOK_SECRET` or `MOTIVE_WEBHOOK_SECRET` is not configured, signature verification is skipped (accepts all requests). This is good for development but dangerous if secrets are forgotten in production.

**Risk**: Unauthenticated ELD events could trigger detention clock manipulation or GPS spoofing.  
**Fix**: Hard-fail signature check in non-development environments.

---

### 🟡 GAP-S2: Pre-signed URL 7-day expiry is long for high-value documents

RC documents are pre-signed for 7 days. RC PDFs contain sensitive financial data (rate, carrier identity, load details).

**Risk**: Forwarded email links could expose RC data to unauthorized parties for up to 7 days.  
**Fix**: Reduce to 24-48h; or add IP/token binding at the CDN layer.

---

### 🟡 GAP-S3: `dispatch_event` is a public test helper

`dispatch_event()` in `event_router.py` is designed for tests but is exported from a production module. It bypasses DB audit logging and Redis stream recording.

**Risk**: If called in production code accidentally, events won't be persisted.  
**Fix**: Move to `tests/` or add a clear guard (e.g., `if settings.is_development: raise RuntimeError`).

---

## Test Coverage Gaps

### 🔴 GAP-T1: No tests for Phase 2 skills (S15-S19)

Transit monitoring, POD collection, invoice generation, payment reconciliation, and settlement have zero test coverage.

**Risk**: Bugs in the settlement pipeline (wrong amounts, missed POD, payment reconciliation errors) are not caught before production.  
**Priority skills to test**: S17 (POD + invoice), S19 (payment reconciliation), SR (driver settlement).

---

### 🔴 GAP-T2: No test for carrier lock / dedup (BUG-14)

The `start_dispatch_workflow` carrier-level lock and active-load check (WORKFLOW-3) have no test. The code was written and reviewed, but never exercised by a test that actually calls it concurrently.

**Fix**: Add test that calls `start_dispatch_workflow` twice concurrently (or sequentially with an active load in DB state) and asserts the second call returns `{"error": "carrier_already_active"}`.

---

### 🔴 GAP-T3: No checkpoint recovery test

There is no test that simulates Redis loss and verifies that `_load_state()` falls back to the DB checkpoint and resumes the workflow correctly.

---

### 🟠 GAP-T4: Compliance check only tested via routing

`run_compliance_check` in orchestrator queries `CarrierDocument` model — but there's no test that verifies an expired COI actually blocks booking (only the routing function is tested with a pre-set `compliance_blocked=True` flag).

---

### ✅ GAP-T5: CarrierDocument model — FIXED (Phase 3E audit session)

`run_compliance_check` referenced `CarrierDocument` which was missing from `models.py`. The model has been added with fields: `document_type`, `expiry_date`, `s3_url`, `verified`, and a back-populate relationship to `Carrier.documents`.

A migration (`006_carrier_documents.py`) should be written to create the `carrier_documents` table in production.

---

## Operational Gaps

### 🟡 GAP-O1: No observability on circuit breaker trips

API gateway circuit breakers have no Prometheus/Datadog counters. Operations team has no alerting when a dependency enters OPEN state.

---

### 🟡 GAP-O2: Backhaul task is not cancellable

`_run_backhaul_planning` is launched as `asyncio.create_task()`. If the load is cancelled or reassigned, the sleeping background task has no cancellation hook.

---

### 🟡 GAP-O3: HOS precheck only reads cached data

If the ELD cache has expired (>5 min TTL), `run_hos_precheck` defaults to 11h (assumes safe). A driver could be over-hours if the cache is stale.

**Fix**: Trigger a live ELD pull if cache is stale before making the dispatch decision.

---

### 🟢 GAP-O4: Skills S08, S18, S20 are missing

Skill numbering has gaps at S08, S18, and S20. These were likely planned but not implemented.

---

## Configuration Gaps

### 🟡 GAP-C1: Alembic env.py may not be production-configured

`db/migrations/env.py` exists but was not audited. Online migrations in production require proper Alembic configuration.

---

### 🟡 GAP-C2: `s3_key_prefix` for RC documents

Pre-signed URLs use `loads/{load_id}/RC_signed_{timestamp}.pdf` but there's no per-environment key prefix. Test and production buckets share the same key structure.
