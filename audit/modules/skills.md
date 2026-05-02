# cortexbot/skills/ — Skill Nodes

25 Python files implementing the individual work units of the dispatch pipeline. Each skill is a pure `async def skill_XX(state: dict) -> dict` function that reads from state, does its work, and returns an updated state.

---

## Skill Index

| File | Skill ID | Name | Phase | Graph Position |
|---|---|---|---|---|
| `s05_load_search.py` | S05 | Load Board Search | 1 | Node: `search_loads` |
| `s06_load_triage.py` | S06 | Load Triage & Scoring | 1 | Node: `triage_eligibility` |
| `s07_rate_intelligence.py` | S07 | Rate Intelligence | 1 | Node: `rate_intelligence` |
| `s09_carrier_confirm.py` | S09 | Carrier Confirmation | 1 | Node: `carrier_confirmation` |
| `s10_load_booking.py` | S10 | Load Booking (TMS) | 1 | Node: `book_load` |
| `s11_carrier_packet.py` | S11 | Carrier Packet | 1 | Node: `complete_packet` |
| `s12_rc_review.py` | S12 | RC Review | 1 | Node: `review_rc` |
| `s13_driver_dispatch.py` | S13 | Driver Dispatch | 1 | Node: `dispatch_driver` |
| `s14_hos_compliance.py` | S14 | HOS Compliance | 1+2 | Background loop |
| `s15_in_transit_monitoring.py` | S15 | Transit Monitoring | 2 | Node: `start_transit_monitoring` |
| `s16_detention_layover.py` | S16 | Detention & Layover | 2 | ELD webhook triggered |
| `s17_pod_invoicing.py` | S17 | POD & Invoicing | 2 | Node: `generate_invoice` |
| `s19_payment_reconciliation.py` | S19 | Payment Reconciliation | 2 | Node: `track_payment` |
| `s21_backhaul_planning.py` | S21 | Backhaul Planning | 2 | Background task, T-8h before delivery |
| `s21_s22_s23_ops.py` | — | Ops bundle (Backhaul/Fuel/Weather) | 2 | Background |
| `s22_fuel_optimization.py` | S22 | Fuel Optimization | 2 | Background |
| `s23_weather_monitoring.py` | S23 | Weather Monitoring | 2 | Background |
| `s24_s25_relationship_scoring.py` | S24-S25 | Broker + Carrier Scoring | 2 | Post-settlement |
| `s26_s27_compliance_accessorials.py` | S26-S27 | Compliance + Accessorials | 2 | Background |
| `sq_dispatcher_fee.py` | SQ | Dispatcher Fee Collection | 2 | Node: `collect_dispatcher_fee` |
| `sq_sr_ss_st_financial.py` | — | Financial bundle | 2 | Nodes: fee/settlement/sync |
| `sr_driver_settlement.py` | SR | Driver Settlement | 2 | Node: `driver_settlement` |
| `ss_driver_advance.py` | SS | Driver Advance | 2 | Internal endpoint |
| `st_quickbooks_sync.py` | ST | QuickBooks Sync | 2 | Node: `quickbooks_sync` |
| `su_sv_expenses_1099.py` | SU-SV | Expenses + 1099 | 3B | Background |
| `sx_fraud_detection.py` | SX | Fraud Detection | 1 | Node: `fraud_check` |
| `sy_freight_claims.py` | SY | Freight Claims | 3B | Internal endpoint |

---

## Key Skill Details

### S05 — Load Board Search (`s05_load_search.py`)

**Dependencies:** DAT API (via `api_gateway`), PostgreSQL (carrier lookup)  
**DB query:** `SELECT * FROM carriers WHERE carrier_id = ?` — fails with string `carrier_id` (must be UUID)  
**Key fix:** Removed manual Truckstop fallback call (was duplicating the gateway's automatic fallback)  
**Output statuses:** `LOADS_FOUND`, `NO_LOADS`

**Gap:** `carrier_id` must be a valid UUID in production; string IDs from test fixtures cause `AttributeError: 'str' object has no attribute 'hex'`

---

### S06 — Load Triage (`s06_load_triage.py`)

**Purpose:** Filters `raw_loads` by equipment type, weight, hazmat, avoid-states; scores by preferred destinations and rate.  
**BUG-10 fix:** Empty `equipment_type` on load is now rejected when carrier specifies one.  
**Output:** `status=ELIGIBLE` with `load_queue` sorted by score, `current_load` set to top candidate; or `status=NO_ELIGIBLE_LOADS`  
**Tests:** Full coverage in `tests/test_triage.py` (10 tests)

---

### S07 — Rate Intelligence (`s07_rate_intelligence.py`)

**Purpose:** Fetches DAT market rates for the lane, computes anchor rate, counter rate, walk-away rate.  
**Phase 3A fix:** Added to internal routes in `main.py` (was broken).  
**Output:** `market_rate_cpm`, `anchor_rate_cpm`, `counter_rate_cpm`, `walk_away_rate_cpm`, `rate_brief`

---

### S12 — RC Review (`s12_rc_review.py`)

**Purpose:** Downloads RC from S3, runs Claude Vision OCR, checks for discrepancies against booked terms.  
**OCR:** Uses `document_ocr.py` agent — BUG-11 fixed (multi-page PDF, up to 3 pages)  
**Output:** `rc_extracted_fields`, `rc_discrepancy_found`, `rc_discrepancies`  
**Suspends:** Returns END if `rc_signed_url` is None (waiting for broker to sign)

---

### S13 — Driver Dispatch (`s13_driver_dispatch.py`)

**Purpose:** Sends dispatch packet to driver via WhatsApp + SMS + email, registers geofences on ELD.  
**BUG-13 fix:** Now sets `awaiting=DRIVER_ACK` after dispatch.  
**DB:** Looks up carrier + load from DB; also writes Event record.  
**Output:** `status=DISPATCHED`, `dispatch_sent=True`, `awaiting=DRIVER_ACK`

---

### S14 — HOS Compliance (`s14_hos_compliance.py`)

**Purpose:** Polls ELD for HOS data, caches in Redis, alerts if violations imminent.  
**Federal limits:** 11h drive, 14h window, 30-min break after 8h, 60h/7-day or 70h/8-day  
**Cache key:** `cortex:hos:{driver_id}` (5-min TTL)  
**WORKFLOW-2:** `run_hos_precheck` in orchestrator reads from this cache key — blocks dispatch if < 3h remaining

---

### SX — Fraud Detection (`sx_fraud_detection.py`)

**Purpose:** 4-layer fraud check: internal blacklist → Highway.com → FMCSA SAFER → DAT Credit  
**Input:** `broker_mc`, optional `load_id`  
**DB:** Queries `brokers` table for blacklist flag  
**Output:** `fraud_risk_score` (0-100), `fraud_recommendation` (BOOK/CAUTION/DO_NOT_BOOK/EMERGENCY), `fraud_flags`  
**Alert:** High-risk triggers Twilio SMS to dispatcher

---

## Notable Gaps Across Skills

1. **S05**: No retry on carrier DB lookup failure — returns FAILED immediately
2. **S07**: Rate brief is stored in state but no validation that `market_rate_cpm > 0` before proceeding
3. **S13**: `db.add(Event(...))` in mock context issues RuntimeWarning — `db.add()` is sync but called inside async mock
4. **SX**: Fraud check queries `brokers` table without creating it in test fixtures — requires DB mock
5. **S15**: In-transit monitoring background loop interval not configurable — hardcoded sleep
6. **S16**: Detention billing is only triggered by ELD geofence events — no fallback for carriers without ELD
7. **S17**: POD collection has no timeout/escalation if driver doesn't upload POD
8. **S19**: Payment reconciliation doesn't handle partial payments (pays-in-full assumption)
9. **S21**: Backhaul planning uses `asyncio.sleep` which is not cancellation-safe in all Python 3.12 scenarios
10. **Skills missing from graph**: S08 (Checkout?), S18, S20 — numbering gaps suggest planned but not implemented skills
