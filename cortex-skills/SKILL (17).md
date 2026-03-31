---
skill_id: "07-rate-market-intelligence"
name: "Rate Market Intelligence"
version: "1.0.0"
phase: 5
priority: critical
trigger: "Called before every broker negotiation to arm the dispatcher with current market data"
inputs:
  - origin_city: "string"
  - origin_state: "string"
  - destination_city: "string"
  - destination_state: "string"
  - equipment_type: "string"
  - pickup_date: "ISO 8601 date"
outputs:
  - market_rate_per_mile: "float — DAT average for lane"
  - load_to_truck_ratio: "float — market tightness indicator"
  - anchor_rate: "float — recommended opening offer"
  - target_rate: "float — minimum acceptable"
  - rate_context: "narrative justification string for use on call"
integrations:
  - DAT Rate Analytics API
  - Truckstop rate API
  - Greenscreens.ai (optional — AI rate prediction)
depends_on: ["06-load-triage-eligibility"]
triggers_next: ["08-broker-negotiation"]
tags: [rate, market, dat, intelligence, anchor, negotiation-prep]
---

# SKILL: Rate Market Intelligence
## Cortex Bot — Truck Dispatch Automation

**Trigger**: Called before every broker negotiation to arm the dispatcher with current market data.
**Phase**: 5 (Pre-Call Intelligence)
**Priority**: CRITICAL

---

## Purpose

Pull real-time lane rate data so every negotiation is anchored to the actual market — never guessing, never leaving money on the table. The dispatcher should know the rate before the broker does.

---

## Data Sources

| Source | Data Point | Use |
|--------|-----------|-----|
| DAT RateView | 7-day and 30-day lane avg | Primary rate benchmark |
| DAT Load-to-Truck Ratio | Market tightness by area | Leverage indicator |
| Truckstop Rate Analyzer | Secondary lane confirmation | Cross-reference |
| Greenscreens.ai | AI-predicted rate | Optional premium data |
| Internal TMS history | Previous loads in same lane | Proprietary benchmark |

---

## Execution Steps

### Step 1 — Lane Rate Lookup (DAT)

Query DAT RateView API for:
- **Origin → Destination** (city-pair or state-pair)
- Equipment type
- Date range: last 7 days + last 30 days

Capture:
```
lane_7day_avg_cpm      # Average rate per mile, last 7 days
lane_30day_avg_cpm     # Average rate per mile, last 30 days
lane_7day_high         # High end of range
lane_7day_low          # Low end of range
posted_loads_count     # How many loads posted this lane
available_trucks_count # How many trucks available this lane
load_to_truck_ratio    # posted_loads / available_trucks
```

### Step 2 — Market Tightness Assessment

```
if load_to_truck_ratio > 3.0:
    market = "VERY TIGHT — strong carrier leverage"
    rate_multiplier = 1.20  # anchor 20% above market
elif load_to_truck_ratio > 2.0:
    market = "TIGHT — moderate leverage"
    rate_multiplier = 1.15
elif load_to_truck_ratio > 1.0:
    market = "BALANCED"
    rate_multiplier = 1.10
else:
    market = "LOOSE — broker has leverage"
    rate_multiplier = 1.05
```

### Step 3 — Trend Analysis

```
if lane_7day_avg_cpm > lane_30day_avg_cpm × 1.05:
    trend = "RISING — momentum in our favor"
elif lane_7day_avg_cpm < lane_30day_avg_cpm × 0.95:
    trend = "FALLING — broker may use this"
else:
    trend = "STABLE"
```

### Step 4 — Seasonal & Event Adjustments

Check for:
- Produce season (spring/summer Florida, California)
- Holiday freight surges (Thanksgiving, Christmas build-up)
- Weather events causing supply shortage
- Regulatory changes (ELD mandate renewals, FMCSA rule changes)
- Major shipper disruptions (strikes, port closures)

Apply manual adjustment if any seasonal event active: +$0.10–0.30/mile.

### Step 5 — Calculate Negotiation Targets

```
base_rate = lane_7day_avg_cpm

anchor_rate = base_rate × rate_multiplier
# Round up to nearest $0.05/mile for clean numbers

target_rate = max(carrier_rate_floor, base_rate × 0.98)
# Never go below carrier floor — this is the walk-away point

counter_rate = (anchor_rate + target_rate) / 2
# Midpoint for when broker pushes back on anchor
```

### Step 6 — Build Call Brief

Generate a rate context summary for the negotiation call:

```
RATE BRIEF — Nashville, TN → Atlanta, GA | 53' Dry Van | 2026-03-20

Market rate (7-day avg):  $2.45/mile
Market rate (30-day avg): $2.38/mile
Load-to-truck ratio:      2.8 (TIGHT market — carrier advantage)
Trend:                    RISING (+3% week-over-week)

Negotiation targets:
  Anchor (open with):     $2.82/mile ($700 flat for 248 mi)
  Counter (if pushed):    $2.65/mile ($657 flat)
  Walk-away (minimum):    $2.25/mile ($558 flat)

Talking points:
  → "DAT is showing this lane averaging $2.45 this week — market is tight"
  → "I have 12 miles of deadhead from current position — that adds cost"
  → "Load-to-truck ratio in Nashville is 2.8 right now — trucks are moving"
  → "With current diesel at $3.80/gal, fuel is a factor in this lane"
```

### Step 7 — Internal History Check

Query TMS for previous loads in the same lane:
```
SELECT avg(agreed_rate_cpm), max(agreed_rate_cpm), count(*)
FROM loads
WHERE origin_state = 'TN' AND dest_state = 'GA'
  AND equipment = 'dry_van'
  AND pickup_date > NOW() - INTERVAL 90 DAYS
```

If we've run this lane before:
- Note the highest rate previously achieved
- Note the broker who paid it
- Use this as internal benchmark in negotiation

---

## Output

```json
{
  "lane": "Nashville, TN → Atlanta, GA",
  "equipment": "53_dry_van",
  "pickup_date": "2026-03-20",
  "market_rate_7day": 2.45,
  "market_rate_30day": 2.38,
  "load_to_truck_ratio": 2.8,
  "market_condition": "TIGHT",
  "trend": "RISING",
  "anchor_rate_cpm": 2.82,
  "counter_rate_cpm": 2.65,
  "walk_away_rate_cpm": 2.25,
  "rate_context_narrative": "DAT showing $2.45/mile average this week...",
  "internal_best_rate_cpm": 2.55,
  "internal_best_broker": "Echo Global",
  "generated_at": "2026-03-20T07:30:00Z"
}
```

---

## Error Handling

| Situation | Action |
|-----------|--------|
| DAT API unavailable | Use Truckstop data as fallback |
| Lane has <5 data points | Flag as "thin lane — use national average as proxy" |
| No internal history | Note "first time in this lane" |
| Market rate below carrier floor | Flag before calling — this lane may not be viable today |
