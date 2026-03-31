---
skill_id: "06-load-triage-eligibility"
name: "Load Triage & Eligibility Gate"
version: "1.0.0"
phase: 4
priority: critical
trigger: "List of loads returned by load-board-search; runs before any broker contact is made"
inputs:
  - loads_list: "array of loads from load-board-search"
  - carrier_profile: "full profile from carrier-profile-management"
  - carrier_hos: "current hours of service remaining from ELD"
  - truck_location: "current GPS position"
outputs:
  - eligible_loads: "array of loads passing all gates"
  - rejected_loads: "array with rejection reason per load"
  - profitability_ranking: "eligible loads sorted by net CPM score"
integrations:
  - TMS
  - ELD/telematics API
  - Google Maps API (drive time calculation)
  - DAT Rate Analytics (lane rate reference)
depends_on: ["05-load-board-search", "04-carrier-profile-management"]
triggers_next: ["07-rate-market-intelligence", "08-broker-negotiation"]
tags: [eligibility, triage, gate, screening, profitability]
---

# SKILL: Load Triage & Eligibility Gate
## Cortex Bot — Truck Dispatch Automation

**Trigger**: Load list returned by `load-board-search`; runs before any broker contact is made.
**Phase**: 4–5 (Load Triage + Eligibility Gate)
**Priority**: CRITICAL

---

## Purpose

Screen every load against a strict eligibility gate before wasting time calling a broker. A failed eligibility check means the load cannot legally, physically, or financially work for this carrier. Calling anyway wastes time and erodes broker relationships.

---

## Gate 1 — Hard Disqualifiers (Auto-Reject, No Review)

Fail any of these → immediately remove from list, no call made:

| Check | How to Verify |
|-------|--------------|
| Equipment type mismatch | Load requires reefer, carrier has dry van → SKIP |
| Weight exceeds carrier legal limit | Load weight > carrier max payload → SKIP |
| Hazmat required, carrier not certified | Load comments say "hazmat" + carrier has no endorsement → SKIP |
| TWIC required, carrier no TWIC | Load requires TWIC + carrier not credentialed → SKIP |
| Team driver required, single driver | Load says "team only" + carrier is solo → SKIP |
| Commodity on carrier's exclusion list | Carrier profile says "no live animals" + load is livestock → SKIP |
| State/region carrier refuses | Load delivers to NYC + carrier marked "no NYC" → SKIP |
| Canada crossing required, not authorized | Load crosses border + carrier has no PAPS/FAST → SKIP |
| MC age requirement not met | Load comments: "MC must be 90+ days" + carrier MC age = 45 days → SKIP |
| Broker bans carrier's factoring company | Load comments: "No [FactorName]" + carrier uses that factor → SKIP |

---

## Gate 2 — HOS & Timing Feasibility

For each load that passes Gate 1:

### Step 1 — Calculate Drive Time to Pickup
```
drive_time = Google Maps ETA(truck_location → pickup_address, departure=NOW)
add 15% buffer for traffic variability
```

### Step 2 — Check HOS vs Drive Time
```
if drive_time > carrier_hos_remaining:
    REJECT — "Insufficient HOS to reach pickup"
if (drive_time + pickup_dwell_estimate + loaded_drive_time) > (carrier_hos + next_reset_hours):
    FLAG — "May need reset before delivery — confirm with carrier"
```

### Step 3 — Check Appointment Feasibility
```
earliest_arrival = NOW + drive_time
if load_has_appointment:
    if earliest_arrival > appointment_time:
        REJECT — "Cannot make appointment window"
    if earliest_arrival < (appointment_time - 4hrs):
        FLAG — "Very early — may need to wait"
```

---

## Gate 3 — Rate Floor Screening

```
if load_posted_rate is not None:
    estimated_cpm = load_posted_rate / loaded_miles
    if estimated_cpm < carrier_rate_floor:
        REJECT — "Posted rate below carrier floor — not worth calling unless market says negotiable"
        
if load_posted_rate is None (rate negotiable):
    check DAT market rate for this lane
    if market_rate < carrier_rate_floor × 0.85:
        REJECT — "Lane pays below floor even at market top"
    else:
        FLAG as "Negotiate needed — market rate borderline"
```

---

## Gate 4 — Broker Creditworthiness

```
broker_mc = load.broker_mc_number
run 03-fmcsa-verification(broker_mc, check_type="broker")

if broker_operating_status != "ACTIVE":
    REJECT — "Broker authority not active"
    
if broker_dat_credit_score < 60:
    REJECT — "Poor broker credit score"
    
if broker_days_to_pay > 45 AND carrier_not_factored:
    FLAG — "Slow pay broker — require quick-pay or factor"
    
if broker_on_factoring_ban_list:
    REJECT — "Carrier's factoring company banned by this broker"
```

---

## Profitability Scoring (for all loads passing Gates 1–4)

Calculate net CPM score for each eligible load:

```
# Base metrics
loaded_miles = Google Maps distance(pickup → delivery)
deadhead_miles = Google Maps distance(truck_location → pickup)
posted_rate = load.rate OR market_rate_estimate

# Cost deductions
deadhead_cost = deadhead_miles × $0.35  # estimated fuel cost
estimated_fuel_surcharge = loaded_miles × $0.08  # if not included in rate

# Adjusted rate
net_rate = posted_rate - deadhead_cost

# CPM calculation
net_cpm = net_rate / loaded_miles

# Adjustment factors
if load.multi_stop: net_cpm -= 0.05
if load.driver_assist_required: net_cpm -= 0.03
if load.nyc_delivery: net_rate += 200; net_cpm = net_rate / loaded_miles
if load.lumper_likely: net_rate -= 150; net_cpm = net_rate / loaded_miles
if load.high_dwell_risk: net_cpm -= 0.04  # grocery, food grade
if load.good_reload_market: net_cpm += 0.05  # bonus for position value
if load.quick_pay_available: net_cpm += 0.02
if load.drop_and_hook: net_cpm += 0.03  # no dwell risk

# Final score
load.profitability_score = net_cpm
```

---

## Output Structure

```json
{
  "carrier_id": "CARR-032026-001",
  "screened_at": "2026-03-20T07:15:00Z",
  "total_loads_reviewed": 47,
  "hard_rejected": 31,
  "timing_rejected": 4,
  "rate_rejected": 6,
  "credit_rejected": 2,
  "eligible_count": 4,
  "eligible_loads": [
    {
      "load_id": "DAT-8821234",
      "broker": "Echo Global",
      "origin": "Nashville, TN",
      "destination": "Atlanta, GA",
      "pickup_date": "2026-03-20",
      "pickup_window": "07:00-10:00",
      "commodity": "Dry goods",
      "weight_lbs": 42000,
      "loaded_miles": 248,
      "deadhead_miles": 12,
      "posted_rate": null,
      "market_rate_estimate": 2.45,
      "net_cpm_score": 2.31,
      "flags": ["rate_negotiable", "quick_pay_available"],
      "call_priority": 1
    }
  ]
}
```

---

## Error Handling

| Situation | Action |
|-----------|--------|
| HOS data unavailable | Flag load as "Verify HOS manually" — do not auto-reject |
| Google Maps timeout | Use carrier's stated home-base distance estimate |
| Load already covered when calling | Mark expired in TMS, move to next |
| Broker MC# not found in FMCSA | Do not book — broker authority unverified |
