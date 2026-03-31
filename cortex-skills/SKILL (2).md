---
skill_id: "05-load-board-search"
name: "Load Board Search & Triage"
version: "1.0.0"
phase: 3
priority: critical
trigger: "Carrier submits availability (location + datetime) OR daily at 05:00 AM per active carrier"
inputs:
  - carrier_id: "string"
  - truck_location: "city, state, zip"
  - available_datetime: "ISO 8601 datetime"
  - carrier_profile: "equipment, lanes, rate floor, constraints"
outputs:
  - ranked_loads: "array sorted by profitability score"
  - call_briefs: "top-5 priority loads with negotiation prep"
  - status: "enum: LOADS_FOUND | NO_LOADS_POSTING_TRUCK"
integrations:
  - DAT API
  - Truckstop API
  - Google Maps / HERE Maps API
  - Rate calculation engine
  - TMS
depends_on: ["04-carrier-profile-management"]
triggers_next: ["06-load-triage-eligibility", "07-rate-market-intelligence"]
tags: [load-board, dat, truckstop, search, triage, profitability]
---

# SKILL: Load Board Search & Triage
## Cortex Bot — Truck Dispatch Automation

**Trigger**: Carrier submits availability (location + date/time available) OR daily at 5:00 AM for each active carrier.  
**Phase**: 3–4 (Load Board Configuration + Search & Triage)  
**Priority**: CRITICAL  

---

## Purpose

Systematically search DAT, Truckstop, and other load boards for loads that match the carrier's equipment, lanes, rate floors, and availability — then rank results by profitability before calling brokers.

---

## Inputs Required

From carrier profile:
- Current truck location (city, state, zip)
- Equipment type (53' dry van / reefer / flatbed / step-deck / power-only)
- Available date and time
- Preferred delivery lanes / states
- Maximum deadhead miles willing to accept
- Minimum rate floor (CPM or flat)
- Special certifications (hazmat, TWIC, team)
- HOS hours remaining
- Special constraints (no NYC, no Canada, no touch, no multi-stop, etc.)

---

## Execution Steps

### Step 1 — Configure Search Filters

**DAT Load Board Filters:**
- Origin: [Current city/state] + radius [carrier max deadhead] miles
- Equipment: [carrier equipment type]
- Available date: [carrier available date]
- Weight: [carrier weight capacity max]
- Length: [carrier trailer length]
- Rate floor: [carrier minimum rate floor]
- Exclude commodities: [per carrier restrictions]

**Additional filters to enable:**
- Credit/quick-pay filter: only show brokers with DAT credit score ≥65
- Days to Pay filter: only show brokers who pay within 35 days (or factoring allowed)
- Show load comments: Yes (to catch hidden requirements)

**Repeat equivalent setup for Truckstop.com**

### Step 2 — Execute Search

1. Run search on DAT → pull all results
2. Run search on Truckstop → pull all results
3. De-duplicate loads that appear on both boards
4. Sort by: newest posted first

### Step 3 — Initial Eligibility Screening

For each load, run automated eligibility check:

**Hard Pass/Fail (automatic disqualify if failed):**
- [ ] Equipment match (van load on reefer carrier = SKIP)
- [ ] Weight within carrier's legal limit (80,000 lbs max standard)
- [ ] Pickup date/time achievable given current location + HOS
- [ ] Driver does not require team if single driver
- [ ] Hazmat — does carrier have hazmat endorsement?
- [ ] TWIC required — does carrier have TWIC card?
- [ ] MC age meets broker requirement (check load comments)
- [ ] Factoring company not banned by broker

**Soft Checks (score, don't disqualify):**
- [ ] Deadhead miles vs carrier preference
- [ ] Multi-stop (lower profitability score)
- [ ] Driver assist required (lower score if no-touch carrier)
- [ ] NYC delivery (add NYC surcharge estimate)
- [ ] Lumper required (add lumper cost estimate)

### Step 4 — Profitability Scoring

For each eligible load, calculate **Load Score**:

```
Base Score = (posted rate OR estimated market rate) / total trip miles
Deadhead penalty = deadhead miles × $0.30/mile (estimated fuel cost)
Net CPM = (rate - deadhead penalty) / loaded miles

Adjustments:
  - Multi-stop: -$0.05/CPM
  - Driver assist: -$0.03/CPM  
  - NYC: +$200 estimated surcharge → add to rate
  - Lumper likely: -$150 estimated cost
  - High dwell risk (food grade, grocery): -$0.04/CPM
  - Good reload market at delivery: +$0.05/CPM bonus
  - Quick pay available: +$0.02/CPM bonus

Final Score = Net CPM with all adjustments
```

### Step 5 — Rank & Present

1. Sort all eligible loads by Final Score (highest first)
2. Flag top 5 loads as "Priority Calls"
3. For each priority load, prepare call brief:
   - Broker name, phone number, load reference
   - Origin → Destination
   - Estimated rate and CPM
   - Key requirements
   - Suggested opening rate (anchor high)
   - Target rate (minimum acceptable)
   - Counter-offer script (pass to `broker-negotiation` skill)

### Step 6 — Fallback Actions (No Suitable Loads Found)

If no loads pass eligibility after screening:
1. **Widen radius** by 25 miles and re-run search
2. **Adjust time window** by ±4 hours
3. **Check reload markets**: where does the carrier want to end up? Search loads FROM delivery cities back toward home.
4. **Post truck**: publish truck availability post on DAT and Truckstop
   - Include: equipment, location, available date, preferred lanes, phone number
   - Post every 30 minutes to keep it fresh
5. **Proactively call** top 10 brokers in target lanes from relationship database
6. **Check partner carriers**: any carrier going the opposite way that needs a swap?

### Step 7 — Continuous Monitoring

- Refresh search every 3–5 minutes for new loads
- Alert immediately when a load posts above [carrier rate floor × 1.15] (premium load)
- At 60 minutes without a booked load: escalate to widen search radius and call backup brokers

---

## Outputs

- Ranked list of eligible loads with profitability scores
- Call brief for each top-5 priority load
- Status: `LOADS_FOUND` or `NO_LOADS_POSTING_TRUCK`
- Truck posting confirmation if no loads found

---

## Integration Requirements

- **DAT API**: Load search, rate data, broker credit scores
- **Truckstop API**: Load search, market rates
- **Google Maps / HERE Maps API**: Deadhead distance calculation, traffic ETA
- **Rate calculation engine**: CPM, profitability scoring
- **TMS**: Carrier profile read, load tracking write

---

## Load Board Platforms

| Platform | Best For | Notes |
|----------|---------|-------|
| DAT One | Largest volume, rate analytics | Primary board |
| Truckstop.com | Strong broker relationships | Secondary board |
| Landstar | If carrier is Landstar agent | Agent-only |
| Echo Global | Direct broker relationship | Spot market |
| Coyote | Direct broker relationship | Spot market |
| 123Loadboard | Budget board | Supplement only |

---

## Error Handling

| Error | Action |
|-------|--------|
| No loads within radius | Widen radius 25 miles, re-search |
| All loads below rate floor | Post truck, call brokers directly |
| Board API timeout | Retry 3x then search manually via web |
| Load already covered | Mark expired, move to next |
| Carrier HOS insufficient | Flag — do not book until next available reset |
