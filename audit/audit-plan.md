# CortexBot — Forward Audit Plan

**Goal:** Systematically identify and close all remaining gaps before production deployment.

---

## Audit Methodology

Each sprint cycles through:
1. **Static analysis** — grep for patterns, import errors, type mismatches
2. **Test coverage** — add missing tests, track pass rate
3. **Manual code review** — read each module end-to-end for logic bugs
4. **Integration test** — run `USE_MOCKS=true pytest` + smoke test with seed data

---

## Phase 1: Critical Fixes (Next Session)

Priority: get all 🔴 gaps closed before any production deployment.

### 1.1 Verify CarrierDocument model (GAP-T5)

```bash
grep -n "class CarrierDocument" cortexbot/db/models.py
```

If missing: add model and migration, or update `run_compliance_check` to use the existing carrier compliance fields (COI URLs, status columns on `Carrier`).

### 1.2 Unify bland webhook routing (GAP-A1)

- Check `main.py` route for Bland AI webhook
- If it uses `bland_ai.py`, either:
  a. Change it to use `bland.py` (`handle_bland_webhook`)
  b. Or move `handle_bland_webhook` logic into `bland_ai.py`
- Delete the orphaned file

### 1.3 Add UUID validation at API entry point (GAP-A2, GAP-A3)

In `cortexbot/api/carriers.py` or the dispatch route:
```python
import uuid
try:
    carrier_uuid = uuid.UUID(carrier_id)
except ValueError:
    raise HTTPException(400, "Invalid carrier_id — must be a UUID")
```

### 1.4 Fix background_worker.py (GAP-A4)

Either implement `process_tasks()` as a no-op with a warning, or add a comment that this file is not meant to be run directly.

---

## Phase 2: Test Coverage Sprint

**Target:** Add 30+ tests covering Phase 2 skills and critical paths.

### 2.1 Phase 2 skill tests (GAP-T1)

New file: `tests/test_phase2_skills.py`

| Test | Mocks needed |
|---|---|
| `test_pod_collection_updates_state` | S17 with mocked S3 upload |
| `test_invoice_generation_calculates_total` | S17 with mock DB write |
| `test_payment_reconciliation_marks_paid` | S19 with mock DB |
| `test_dispatcher_fee_deducted_from_settlement` | SQ with mock Stripe |
| `test_driver_settlement_creates_transfer` | SR with mock Stripe |
| `test_quickbooks_sync_creates_invoice` | ST with mock QBO client |

### 2.2 Carrier lock / dedup test (GAP-T2)

In `tests/test_e2e_workflow.py`:
```python
async def test_second_dispatch_rejected_when_active_load(base_state, mock_redis, mock_db_with_active_load):
    """start_dispatch_workflow must reject if carrier already has an active load."""
    result = await start_dispatch_workflow(carrier_id, city, state)
    assert result["error"] == "carrier_already_active"
```

Requires a `mock_db_with_active_load` fixture that returns a mock `Load` object.

### 2.3 Compliance check with real COI data (GAP-T4)

```python
async def test_compliance_check_blocks_on_expired_coi(base_state, mock_redis):
    """run_compliance_check must set compliance_blocked=True for expired COI."""
    # Mock DB to return a carrier with expired coi_cargo date
    ...
    result = await run_compliance_check(base_state)
    assert result["compliance_blocked"] is True
    assert any("COI" in issue for issue in result["compliance_issues"])
```

### 2.4 Webhook signature verification test

```python
async def test_samsara_webhook_rejects_invalid_signature(mock_redis):
    """ELD webhook with wrong HMAC must be rejected when secret is configured."""
    with patch.dict(os.environ, {"SAMSARA_WEBHOOK_SECRET": "real-secret"}):
        await handle_samsara_webhook(valid_payload, signature="sha256=invalidsig", body=b"...")
        # Verify no side effects (no Redis writes, no DB writes)
```

### 2.5 Checkpoint recovery test (GAP-T3)

```python
async def test_load_state_falls_back_to_db_on_redis_miss(mock_redis, mock_db_with_checkpoint):
    """_load_state must return DB checkpoint when Redis is empty."""
    # Don't write to Redis
    # Mock DB to return a checkpoint
    state = await _load_state(load_id)
    assert state is not None
    assert state["load_id"] == load_id
```

---

## Phase 3: Static Analysis Pass

Run these checks and file issues for anything flagged:

### 3.1 Grep for hardcoded secrets
```bash
grep -rn "api_key\s*=\s*['\"]" cortexbot/ --include="*.py" | grep -v "test\|mock\|\.env"
```

### 3.2 Check all `get_redis()` callers handle None
```bash
grep -rn "get_redis()" cortexbot/ --include="*.py"
# Review each: does it handle the case where _redis is None?
```

### 3.3 Verify all lazy imports in node functions don't fail
```bash
# For each skill imported lazily in orchestrator.py node functions:
python -c "from cortexbot.skills.s05_load_search import skill_05_load_search; print('OK')"
# Run for all skill imports
```

### 3.4 Run mypy type check
```bash
mypy cortexbot/core/orchestrator.py --ignore-missing-imports
```

### 3.5 Check for missing `__init__.py` in new modules
```bash
find cortexbot/ -type d | while read d; do
  if [ ! -f "$d/__init__.py" ]; then echo "Missing: $d/__init__.py"; fi
done
```

---

## Phase 4: Integration Test with Seed Data

Use `scripts/seed_test_carrier.py` to create a real carrier, then run `scripts/test_full_loop.py` with `USE_MOCKS=true`.

Expected outcome: full pipeline from SEARCHING → DISPATCHED without errors in logs.

```bash
python scripts/seed_test_carrier.py
USE_MOCKS=true python scripts/test_full_loop.py
```

Check logs for:
- No `AttributeError: 'str' object has no attribute 'hex'`
- No `no such table` errors
- Checkpoint save succeeds
- Graph runs to DISPATCHED status

---

## Phase 5: Ongoing Audit Cadence

| Cadence | Action |
|---|---|
| Every PR | `USE_MOCKS=true pytest tests/ -q` must be green |
| Weekly | Review `audit/gaps.md` — update status of each gap |
| Monthly | Add 5+ tests for uncovered skills |
| Before production deploy | All 🔴 and 🟠 gaps closed; full integration test passing |

---

## Tracking Checklist

- [ ] GAP-A1: Unify bland webhook routing
- [ ] GAP-A2: UUID validation at API entry
- [ ] GAP-A3: Fix test fixtures to use real UUIDs
- [ ] GAP-A4: Fix background_worker.py stub
- [ ] GAP-A5: Persist circuit breaker state to Redis
- [ ] GAP-A6: Add dead-letter queue for event handlers
- [ ] GAP-S1: Hard-fail ELD signature in production
- [ ] GAP-S2: Reduce pre-signed URL expiry to 24-48h
- [ ] GAP-S3: Guard dispatch_event against production misuse
- [ ] GAP-T1: Add Phase 2 skill tests (S15-S19)
- [ ] GAP-T2: Add carrier lock / dedup test
- [ ] GAP-T3: Add checkpoint recovery test
- [ ] GAP-T4: Add compliance check with COI data test
- [ ] GAP-T5: Verify CarrierDocument model exists
- [ ] GAP-O1: Add circuit breaker observability
- [ ] GAP-O2: Add backhaul task cancellation
- [ ] GAP-O3: Live ELD pull on stale HOS cache
- [ ] GAP-C1: Verify Alembic env.py production config
- [ ] GAP-C2: Add per-environment S3 key prefix
