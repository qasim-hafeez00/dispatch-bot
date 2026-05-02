# cortexbot/db/, mocks/, tests/ — Database, Mocks, and Tests

---

## db/ — Database Layer

### models.py (~833 lines, 20+ ORM classes)

All models use:
- `BIGSERIAL` / `BigInteger` + `UUID` external key pattern
- `as_uuid=True` on UUID columns — requires proper `uuid.UUID` objects (not strings)
- Indexed foreign keys and frequently-filtered columns
- JSONB for flexible fields (`data`, `extracted_call_data`, `conversation_json`)

**Model inventory:**

| Model | Table | Key fields |
|---|---|---|
| `Carrier` | `carriers` | mc_number (unique), dot_number, equipment_type, eld_provider, eld_vehicle_id, eld_driver_id, stripe_account_id |
| `Broker` | `brokers` | mc_number (unique), blacklisted, blacklist_reason, dat_credit_score, avg_days_to_pay, relationship_tier |
| `BrokerContact` | `broker_contacts` | broker FK, phone, email, best_lanes |
| `Load` | `loads` | tms_ref (unique), carrier FK, broker FK, status (19 states), pickup/delivery dates, cpm/flat rate, RC/POD/BOL URLs, amount_paid, detention tracking |
| `Event` | `events` | event_code, entity_type, entity_id, triggered_by, data (JSONB), new_status |
| `LoadCheckpoint` | `load_checkpoints` | load_id (unique), state_json (JSONB), current_skill, checkpoint_seq |
| `InboundEmail` | `inbound_emails` | from_email, subject, body, category, processed, load_id FK |
| `CallLog` | `call_logs` | bland_ai_call_id (unique), load_id, outcome, agreed_rate_cpm, transcript |
| `WhatsAppContext` | `whatsapp_context` | phone (unique), carrier_id FK, current_load_id, awaiting, conversation_json |
| `LoadExpense` | `load_expenses` | load_id FK, expense_type, amount, receipt_url |
| `TransitEvent` | `transit_events` | load_id FK, event_type, lat, lng, speed, HOS data, ELD raw data |
| `CheckCall` | `check_calls` | load_id FK, sequence, status, driver response, ETA |
| `DetentionRecord` | `detention_records` | load_id FK, stop_type, arrival/departure ts, free/billable hours, hourly_rate, amount |
| `Invoice` | `invoices` | load_id FK (unique), linehaul, detention, lumper, TONU, total, status, amount_paid |
| `InvoiceLineItem` | `invoice_line_items` | invoice FK, item_type, quantity, unit_rate, amount |
| `Payment` | `payments` | invoice FK, amount, method, reference, status |
| `DriverSettlement` | `driver_settlements` | load_id FK, gross_revenue, dispatch_fee, net_settlement, stripe_transfer_id |
| `DriverAdvance` | `driver_advances` | carrier FK, network (comdata/efs), amount, check_code, redeemed |
| `WeatherAlert` | `weather_alerts` | load_id FK, severity, alert_type, affected_area, force_majeure_doc |
| `QuickbooksSyncLog` | `quickbooks_sync_logs` | entity_type, qbo_entity_id, status, error |
| `BrokerScore` | `broker_scores` | broker FK, composite score, metrics JSON |
| `CarrierScore` | `carrier_scores` | carrier FK, composite score, metrics JSON |

**Phase 3B additions (promoted to models.py from score_models.py):**
- `BrokerScore`, `CarrierScore`
- `Carrier.stripe_account_id` (canonical column)
- `Load.amount_paid`, `Load.payment_received_date`
- `Carrier.eld_driver_id`
- `Load.detention_pickup_hours`, `Load.detention_delivery_hours` (with `_hrs` aliases via `@property`)

### migrations/

5 Alembic migration versions:

| Version | File | Description |
|---|---|---|
| 001 | `versions/001_initial_schema.py` | Initial schema |
| 002 | `versions/002_phase2_schema.py` | Phase 2 additions (transit, POD, invoice, settlement) |
| 003 | `versions/003_phase3a_fixes.py` | Phase 3A column fixes |
| 004 | `versions/004_phase3b_additions.py` | BrokerScore, CarrierScore, stripe fields |
| 005 | `versions/005_phase3e_eld_hardening.py` | ELD webhook idempotency, geofence dedup columns |

**Gap**: No Alembic `env.py` configured for auto-generate or online migration — `migrations/env.py` exists but may need production configuration.

### session.py

- Async SQLAlchemy engine
- `get_db_session()` — async context manager, auto-commit/rollback
- `init_db()` — creates minimal SQLite DDL for mock testing (simplified schema without UUID/JSONB)
- **Known issue**: SQLite mock schema has simplified types — JSONB → TEXT, UUID → TEXT. Tests that use `get_db_session()` against the real SQLite session see full SQLAlchemy types which fail with string UUIDs.

---

## mocks/ — Test Mock Layer

8 files, activated by `USE_MOCKS=true` environment variable.

| File | Mocks | Key behavior |
|---|---|---|
| `__init__.py` | `MOCKS_ENABLED` flag | `bool(os.getenv("USE_MOCKS", "false").lower() == "true")` |
| `redis_mock.py` | `get_fake_redis()` | Returns `fakeredis.aio.FakeRedis` singleton — **do not use singleton in tests; conftest creates fresh instance** |
| `dat_mock.py` | DAT load board API | Returns `fixtures/dat_loads.json` — real-looking mock load data |
| `bland_mock.py` | Bland AI call API | Returns mock call initiation and completion responses |
| `docusign_mock.py` | DocuSign signing | Returns `http://localhost/mock-s3/{key}` (BUG-9 fix — HTTP URL not s3://) |
| `s3_mock.py` | AWS S3 | In-memory file store |
| `ocr_mock.py` | Claude Vision OCR | Returns pre-canned RC extraction result |
| `twilio_mock.py` | Twilio SMS/WhatsApp | No-op send, logs to stdout |

### Mock activation pattern

```python
# In api_gateway.py:
if MOCKS_ENABLED:
    return await _mock_api_call(api_name, endpoint, payload)

# In redis_client.py:
if MOCKS_ENABLED:
    from cortexbot.mocks.redis_mock import get_fake_redis
    _redis = await get_fake_redis()
```

### Key mock gap

`redis_mock.py` has a module-level `_instance` singleton. Tests must create a fresh `FakeRedis` per test (bypassing the singleton) to avoid "bound to different event loop" errors. The `conftest.py` `mock_redis` fixture handles this correctly.

---

## tests/ — Test Suite

**71 tests, 71 passing** as of Phase 3E audit session.

### conftest.py

**Fixtures:**
- `mock_redis` (autouse) — fresh `FakeRedis` per test, patches `cortexbot.core.redis_client._redis`; resets `redis_mock._instance` singleton
- `base_state` — complete `LoadState` dict (~80 fields) for routing tests
- `load_with_candidates` — state with one DRY_VAN load in `raw_loads`
- `booked_state` — state ready for RC review / dispatch

**Environment setup (before all imports):**
```python
os.environ.setdefault("USE_MOCKS", "true")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
# + ANTHROPIC_API_KEY, AWS_*, BLAND_AI_API_KEY, TWILIO_*, SENDGRID_*, ONCALL_PHONE
```

### test_routing.py (40 tests)

Pure unit tests for all 15 `route_after_*` functions. No I/O, no fixtures beyond `base_state`. Covers:
- Happy path, retry loops (with retry count), `END` conditions
- Queue-pop behavior (triage, call, confirm)
- All new routing functions: `route_after_fraud`, `route_after_hos_precheck`, `route_after_compliance`

### test_triage.py (10 tests)

Tests for `skill_06_load_triage`:
- Equipment match / mismatch / empty (BUG-10 regression)
- Weight filtering (overweight, at-max)
- Hazmat (rejected without cert, accepted with cert)
- Avoid states
- Score ordering (preferred destination loads rank first)

### test_webhooks.py (8 tests)

Contract tests for webhook entry points:
- Bland AI: BOOKED + VOICEMAIL outcomes; patch target: `cortexbot.webhooks.bland.resume_workflow_after_call`
- SendGrid: RC / PAYMENT / CARRIER_PACKET email classification
- ELD: `AddressArrival` event routes to `_handle_geofence_event(event="arrival")`; geofence dedup returns True/False
- Event router: `dispatch_event("RC_RECEIVED", ...)` calls `resume_workflow_after_rc`; patch target: `cortexbot.core.orchestrator.resume_workflow_after_rc`

### test_e2e_workflow.py (13 tests)

Integration tests with DB mocking:
- `test_search_loads_finds_candidates` — mocks DB session, checks status in ("LOADS_FOUND", "NO_LOADS_FOUND", "NO_LOADS")
- `test_triage_filters_to_eligible` — skill returns ELIGIBLE for DRY_VAN loads
- `test_fraud_check_clean_broker` — mocks DB, checks recommendation in (BOOK/CAUTION/DO_NOT_BOOK)
- `test_hos_precheck_passes_when_no_data` — no cache → defaults to 11h, not blocked
- `test_hos_precheck_blocks_when_low` — caches `eld_driver_id` with 1.5h remaining, confirms blocked
- `test_dispatch_sets_driver_ack_awaiting` — mocks all external calls + DB; checks DISPATCHED/DRIVER_ACK
- `test_full_phase1_routing_chain` — walks all 9 routing functions in sequence (happy path)
- `test_fraud_block_short_circuits_workflow` — DO_NOT_BOOK → minimal_escalation
- `test_compliance_block_prevents_booking` — blocked → minimal_escalation
- `test_route_after_dispatch_reaches_transit` — dispatch_sent=True → start_transit_monitoring

---

## Coverage Gaps in Tests

| Gap | Priority | Description |
|---|---|---|
| Phase 2 skill tests | HIGH | No tests for S15-S19 (transit, POD, invoice, payment) |
| Bland AI voice call | HIGH | No test for `agent_g_voice_call()` initiating a call |
| ELD full flow | MEDIUM | Geofence → detention clock → billing cycle not tested end-to-end |
| Compliance check | MEDIUM | `run_compliance_check` only tested via routing; no test for expired COI blocking |
| QuickBooks sync | MEDIUM | ST skill has no tests |
| Settlement + fee | LOW | SR/SQ skills have no tests |
| Error paths | HIGH | `minimal_escalation` node is tested for routing but not for actual alert delivery |
| Checkpoint recovery | HIGH | No test for workflow resumption after Redis loss (Postgres fallback) |
| Carrier lock race | HIGH | No test for concurrent `start_dispatch_workflow` calls (BUG-14 dedup) |
