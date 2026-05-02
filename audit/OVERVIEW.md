# CortexBot Dispatch System — Audit Overview

**Snapshot date:** 2026-05-02  
**Branch:** `feature/phase-3e`  
**Last commit:** `d6f2380` — "Phase 3E: ELD webhook hardening and schema optimizations"

---

## What the System Is

CortexBot is an autonomous freight dispatch platform. It takes a trucking carrier (owner-operator or small fleet), finds loads on the spot market, negotiates rates with brokers via AI voice calls (Bland AI), books the load, handles paperwork (RC, BOL, COI), dispatches the driver, monitors the load through delivery, and processes payments and settlements — with zero manual dispatcher intervention on the happy path.

**Target automation:** ≥ 80% end-to-end (no human needed for clean loads)

---

## System Architecture

```
FastAPI app (cortexbot/main.py)
  ├── REST API            /api/carriers, /api/loads
  ├── Webhooks            /webhooks/bland, /webhooks/eld, /webhooks/sendgrid, /webhooks/twilio, /webhooks/docusign
  └── Background tasks    workers/background_worker.py + queue_manager.py

LangGraph StateGraph (orchestrator.py)
  Phase 1: SEARCHING → DISPATCHED
    search_loads → triage_eligibility → fraud_check → rate_intelligence
    → hos_precheck → voice_broker_call [SUSPEND] → carrier_confirmation
    → compliance_check → book_load → complete_packet → review_rc [SUSPEND]
    → dispatch_driver → start_transit_monitoring
  Phase 2: DISPATCHED → SETTLED
    transit_monitoring [SUSPEND] → collect_pod → generate_invoice
    → track_payment [SUSPEND] → collect_dispatcher_fee → driver_settlement
    → quickbooks_sync

Redis (cache + distributed locks + event streams)
PostgreSQL (persistent state, audit trail, financial records)
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| Framework | FastAPI + LangGraph (Python 3.12+) |
| State machine | LangGraph `StateGraph` + TypedDict (`LoadState`) |
| Voice AI | Bland AI (async, webhook-resumed) |
| Document signing | DocuSign / local-sign fallback → S3 |
| Load boards | DAT (primary) + Truckstop (automatic fallback) |
| ELD integration | Samsara + Motive (dual provider) |
| Notifications | Twilio WhatsApp/SMS + SendGrid email |
| Storage | AWS S3 (pre-signed URLs for broker email links) |
| Database | PostgreSQL (SQLAlchemy async) |
| Cache | Redis 7 (fakeredis in tests) |
| Payments | Stripe + QuickBooks |
| Factoring | Comdata / EFS |
| Tests | pytest-asyncio (asyncio_mode=auto), fakeredis, unittest.mock |

---

## Phase Completion Status

| Phase | Description | Status |
|---|---|---|
| 3A | Core dispatch flow fixes | ✅ Complete |
| 3B | Freight Claims, Expense Tracking, 1099 | ✅ Complete |
| 3C | Escalations + Rebrokering | ✅ Complete |
| 3D | Background agents + routes | ✅ Complete |
| 3E | ELD webhook hardening + schema optimizations | ✅ Complete |

---

## Recent Bug Fixes (Phase 3E session)

| ID | File | Fix |
|---|---|---|
| BUG-9 | `docusign_client.py` | RC email S3 URL → 7-day pre-signed HTTPS URL |
| BUG-10 | `s06_load_triage.py` | Empty `equipment_type` on load now rejected when carrier specifies one |
| BUG-11 | `document_ocr.py` | Multi-page PDF OCR — all pages (up to 3) sent to Claude Vision |
| BUG-12 | `orchestrator.py` | Backhaul planning now triggers 8h before delivery, not at dispatch |
| BUG-13 | `s13_driver_dispatch.py` | `awaiting=DRIVER_ACK` now set after dispatch |
| BUG-14 | `orchestrator.py` | Carrier-level dedup lock (was load-level, now carrier-level) |

---

## New Guard Nodes (WORKFLOW-1 through WORKFLOW-4)

| Node | Position in graph | Blocks when |
|---|---|---|
| `fraud_check` | After triage, before rate intel | `fraud_recommendation == "DO_NOT_BOOK"` |
| `hos_precheck` | After rate intel, before voice call | cached HOS drive < 3h |
| *Active-load gate* | Inside `start_dispatch_workflow` lock | carrier has existing active load |
| `compliance_check` | After carrier confirm, before book | COI/insurance expired or carrier inactive |

---

## Test Coverage Summary

| File | Tests | Scope |
|---|---|---|
| `tests/test_routing.py` | 40 | All 15 routing functions, edge cases, END conditions |
| `tests/test_triage.py` | 10 | S06 equipment/weight/hazmat/state/score filters |
| `tests/test_webhooks.py` | 8 | Bland AI, SendGrid, ELD geofence, RC event routing |
| `tests/test_e2e_workflow.py` | 13 | Full routing chain + skill node integration |
| **Total** | **71** | **71/71 passing** |

---

## File Count by Module

| Module | Python files | Purpose |
|---|---|---|
| `cortexbot/core/` | 6 | Orchestrator, Redis, API gateway, event router, queue |
| `cortexbot/skills/` | 25 | Skill nodes (S05–SX) |
| `cortexbot/agents/` | 11 | Specialized agents (voice, OCR, escalation, etc.) |
| `cortexbot/webhooks/` | 8 | Inbound webhook handlers |
| `cortexbot/integrations/` | 10 | Third-party API clients |
| `cortexbot/db/` | 6 | Models, session, migrations (5 versions) |
| `cortexbot/mocks/` | 8 | Test mock layer (USE_MOCKS=true) |
| `cortexbot/api/` | 3 | REST API routes |
| `cortexbot/schemas/` | 3 | Pydantic output schemas |
| `tests/` | 5 | Test suite |
| `workers/` | 2 | Background worker + health check |
| `scripts/` | 4 | Dev utilities (seeding, offline test, syntax check) |
