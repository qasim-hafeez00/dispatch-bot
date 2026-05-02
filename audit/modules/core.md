# cortexbot/core/ — Core Infrastructure

Six files that form the backbone of the system.

---

## orchestrator.py

**The largest file in the codebase (~1100+ lines).**

Owns the LangGraph `StateGraph` definition, all skill-node wrappers, routing functions, and workflow lifecycle management.

### Key exports

| Symbol | Type | Purpose |
|---|---|---|
| `LoadState` | TypedDict | Complete workflow state (~80 fields) |
| `build_phase2_graph()` | function | Constructs and compiles the full LangGraph |
| `get_graph()` | function | Thread-safe singleton accessor (module-level `_graph`) |
| `start_dispatch_workflow()` | async function | Entry point — creates load, acquires carrier lock, starts graph |
| `resume_workflow_after_call()` | async function | Resumes graph after Bland AI webhook |
| `resume_workflow_after_rc()` | async function | Resumes graph after RC email arrives |
| `resume_workflow_after_payment()` | async function | Resumes after payment confirmed |
| `run_fraud_check()` | async function | WORKFLOW-1 node wrapper |
| `run_hos_precheck()` | async function | WORKFLOW-2 node wrapper |
| `run_compliance_check()` | async function | WORKFLOW-4 node wrapper |
| `route_after_*` | 15 functions | Routing functions (pure, testable) |

### LoadState fields (key additions in Phase 3E)

- `awaiting: Optional[str]` — "DRIVER_ACK", "RC", "PAYMENT" suspension signals
- `fraud_risk_score`, `fraud_recommendation`, `fraud_flags`, `fraud_detected`
- `hos_drive_remaining`, `hos_blocks_dispatch`
- `compliance_blocked`, `compliance_issues`

### Graph topology (Phase 2 full graph)

```
search_loads
  ↓ LOADS_FOUND → triage_eligibility
  ↓ NO_LOADS / retry → search_loads (max 3 retries → minimal_escalation)

triage_eligibility
  ↓ ELIGIBLE → fraud_check
  ↓ queue fallback → fraud_check (pops queue)
  ↓ no loads → minimal_escalation

fraud_check (WORKFLOW-1)
  ↓ BOOK/CAUTION → rate_intelligence
  ↓ DO_NOT_BOOK/EMERGENCY → minimal_escalation

rate_intelligence
  ↓ always → hos_precheck

hos_precheck (WORKFLOW-2)
  ↓ ok → voice_broker_call
  ↓ <3h drive → minimal_escalation

voice_broker_call
  ↓ CALLING (async) → END (suspend until Bland AI webhook)
  ↓ BOOKED → carrier_confirmation
  ↓ VOICEMAIL/NO_ANSWER + queue → rate_intelligence
  ↓ VOICEMAIL/NO_ANSWER no queue → search_loads

carrier_confirmation
  ↓ CONFIRMED → compliance_check
  ↓ REJECTED + queue → rate_intelligence
  ↓ REJECTED no queue → search_loads

compliance_check (WORKFLOW-4)
  ↓ pass → book_load
  ↓ blocked → minimal_escalation

book_load → complete_packet → review_rc
  ↓ signed → dispatch_driver
  ↓ discrepancy → minimal_escalation
  ↓ waiting → END (suspend until RC webhook)

dispatch_driver
  ↓ sent → start_transit_monitoring
  ↓ failed → minimal_escalation

[Phase 2]
transit_monitoring → END (suspend) or collect_pod
collect_pod → generate_invoice → track_payment → END (suspend) or collect_dispatcher_fee
collect_dispatcher_fee → driver_settlement → quickbooks_sync
```

### Critical patterns

- **Carrier lock**: `cortex:carrier_lock:{carrier_id}` (Redis, 900s timeout) prevents duplicate load creation at process level; DB active-load check inside the lock is the true dedup gate.
- **Checkpoint persistence**: `_save_checkpoint()` writes `LoadCheckpoint` to DB after each node. UUID columns require proper UUIDs — string `carrier_id` causes SQLAlchemy UUID type error (known test issue, patched in tests).
- **Backhaul timing**: `_run_backhaul_planning()` sleeps until 8h before delivery date, runs as `asyncio.create_task` so it doesn't block dispatch.
- **SCALE-2**: `_graph` module variable + `asyncio.Lock` prevents race in `get_graph()`. Graph is pre-built in FastAPI `lifespan` startup.

### Known gaps

- `run_compliance_check` references `CarrierDocument` model — verify this model exists in `models.py`
- Checkpoint save always fails in tests with string `carrier_id` (logged as WARNING, not fatal)
- No retry logic on `resume_workflow_after_*` functions if the state can't be loaded from Redis

---

## redis_client.py

Central Redis wrapper. Provides typed helpers on top of raw `aioredis`.

### Key exports

| Function | Purpose |
|---|---|
| `get_redis()` | Returns module-level singleton Redis client |
| `set_state(key, state)` | JSON-serialise and SET |
| `get_state(key)` | GET and JSON-deserialise |
| `cache_hos(driver_id, data)` | `cortex:hos:{driver_id}` TTL=300s |
| `get_cached_hos(driver_id)` | Read HOS cache |
| `cache_hos_status(carrier_id, data)` | `cortex:hos_status:{carrier_id}` |
| `cache_gps_position(carrier_id, data)` | `cortex:gps:{carrier_id}` |
| `mark_geofence_triggered(load_id, stop_type, event)` | SET NX for idempotency |
| `start_detention_clock(load_id, stop_type, arrival_ts)` | Redis hash for detention tracking |
| `get_detention_clock(load_id, stop_type)` | Read detention state |

### Key naming conventions

```
cortex:state:load:{load_id}       — full workflow state
cortex:carrier_lock:{carrier_id}  — distributed dispatch lock
cortex:hos:{driver_id}            — HOS cache (300s TTL)
cortex:hos_status:{carrier_id}    — HOS status cache
cortex:gps:{carrier_id}           — GPS position cache
cortex:geofence:{load_id}:{stop}:{event} — idempotency key
cortex:detention:{load_id}:{stop} — detention clock hash
cortex:events:{entity_type}       — Redis stream for event audit
```

### Known gaps

- No exponential backoff on Redis connection failure
- `get_redis()` returns `None` if `_redis` is not set — callers get `AttributeError` on `None.get(...)`
- `mark_geofence_triggered` TTL is not documented — unclear if it expires and allows re-trigger

---

## event_router.py

Publish-subscribe hub for internal events. Persists to DB + Redis stream, dispatches handlers non-blocking via `asyncio.create_task`.

### Key exports

| Symbol | Purpose |
|---|---|
| `event_router` | Module-level `EventRouter` singleton |
| `register_default_handlers()` | Wire up Phase 1+2 event handlers (called from `main.py` lifespan) |
| `dispatch_event(event_code, data)` | Test-friendly direct handler invocation (no DB/Redis) |

### Registered events

| Event | Handler |
|---|---|
| `RC_RECEIVED` | `resume_workflow_after_rc(load_id, s3_url)` |
| `CARRIER_DECISION` | Log only |
| `LOAD_DISPATCHED` | `start_transit_monitoring_tasks(load_id, state)` |
| `PAYMENT_RECEIVED` | `resume_workflow_after_payment(load_id, amount)` |
| `FRAUD_ALERT` | Log only |

### Known gaps

- `FRAUD_ALERT` handler only logs — no automated response (block broker, notify ops)
- No dead-letter queue for failed handler tasks
- `dispatch_event` is a test helper but could be misused in production code

---

## api_gateway.py

Circuit-breaker API gateway for all outbound load-board and rate calls.

### Key exports

- `api_call(provider, endpoint, payload)` — single entry point with retry, fallback, and circuit breaker
- `APIError` — raised on non-retriable errors

### Known gaps

- Circuit-breaker state is in-process only — resets on every deploy/restart
- No observability (no Prometheus/Datadog counters on circuit trips)

---

## queue_manager.py

Redis-backed job queue for background skill execution.

### Known gaps

- Not deeply reviewed — confirm whether it integrates with `background_worker.py` or is standalone

---

## orchestrator_phase2.py

Phase 2 skill orchestration (transit → settlement). Separate file from `orchestrator.py`.

- `start_transit_monitoring_tasks(load_id, state)` — called by `LOAD_DISPATCHED` event handler
