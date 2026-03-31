---
skill_id: "21-backhaul-planning"
name: "Backhaul Planning"
version: "1.0.0"
phase: 3
priority: high
trigger: "Immediately when a load is booked AND delivery city is known. Runs in parallel with in-transit monitoring."
inputs:
  - delivery_city: "string"
  - delivery_state: "string"
  - estimated_delivery_datetime: "ISO 8601"
  - carrier_profile: "lanes, rate floor, equipment, constraints"
  - driver_hos_after_delivery: "estimated hours remaining after delivery"
outputs:
  - backhaul_candidates: "array of ranked loads available from delivery area"
  - best_reload_market: "city with highest freight density near delivery"
  - positioning_recommendation: "where to dead-head if delivery area has no freight"
integrations:
  - DAT API
  - Truckstop API
  - Google Maps API
  - TMS
depends_on: ["10-load-booking", "15-in-transit-monitoring"]
triggers_next: ["05-load-board-search (at delivery)"]
tags: [backhaul, reload, deadhead, positioning, efficiency, next-load]
---

# SKILL: Backhaul Planning
## Cortex Bot — Truck Dispatch Automation

**Trigger**: Load booked and delivery city known. Runs in background while current load is in transit.
**Phase**: 3–4 (concurrent)
**Priority**: HIGH

---

## Purpose

Find the driver's next load before they finish the current one. A truck sitting idle after delivery earns zero revenue. The best dispatch operations have the next load ready the moment the driver goes empty.

---

## Why Backhaul Planning Matters

- Average driver idle time after delivery without planning: **4–8 hours**
- Revenue lost per idle hour at 60 mph, $2.50/mile: **~$150/hour**
- At 100 loads/month, 4 hours idle per load: **$60,000/month in lost capacity**

---

## Execution Steps

### Step 1 — Delivery Market Analysis (At Time of Booking)

Immediately after current load is booked:

1. Check DAT freight heat map for delivery city
2. Pull load-to-truck ratio for delivery area
3. Identify: Is this a **freight-rich** or **freight-poor** market?

```
freight_rich_markets = [
  "Los Angeles, CA", "Chicago, IL", "Dallas, TX", "Atlanta, GA",
  "Houston, TX", "Memphis, TN", "Columbus, OH", "Louisville, KY",
  "Laredo, TX", "Philadelphia, PA"
]

freight_poor_markets = [
  "Las Vegas, NV", "Boise, ID", "Albuquerque, NM", "Jackson, MS",
  "Bangor, ME", "Billings, MT"
]
```

If delivery is to a freight-poor market:
- Flag immediately at time of booking
- Negotiate higher rate on inbound load (premium for being stranded)
- Begin backhaul search earlier — 48 hrs before delivery

### Step 2 — Define Search Window

```
available_datetime = estimated_delivery_time + timedelta(hours=1.5)
# Add 1.5 hrs for unloading, paperwork, driver break

search_origin = delivery_city + ", " + delivery_state
search_radius = 75  # miles
search_window_start = available_datetime
search_window_end = available_datetime + timedelta(hours=24)
```

### Step 3 — Lane Strategy from Carrier Profile

Pull carrier's preferred outbound lanes from delivery city. Prioritize:
1. Loads heading toward carrier's home base
2. Loads heading toward next preferred market
3. High-CPM lanes regardless of direction
4. Loads with strong reload markets at their destination (chain loads)

### Step 4 — Execute Backhaul Search

Run `05-load-board-search` with delivery city as new origin:
- Start search 24 hours before delivery
- Refresh every 30 minutes
- Score and rank per `06-load-triage-eligibility`

### Step 5 — Chain Load Identification

For each backhaul candidate, evaluate reload market:
```
For each load in backhaul_candidates:
    reload_market_score = DAT.load_to_truck_ratio(load.destination)
    load.chain_score = load.profitability_score + (reload_market_score × 0.1)

# Prefer loads that position driver well for the NEXT load, not just this one
```

### Step 6 — Pre-Offer to Carrier During Transit

When a strong backhaul is identified:
- Send preview to driver while still hauling current load:
  > "🔄 Backhaul Preview: [Origin] → [Destination] — $[Rate] — available [Date]. Are you interested? I'll hold it if I can."
- Carrier replies to reserve or pass
- If reserved: begin broker negotiation immediately
- If passed: continue searching

### Step 7 — Delivery Day Execution

When driver is 2 hours from delivery:
- Confirm top backhaul candidates are still available
- Pre-call broker to soft-hold if possible
- Have confirmation loop ready to fire the moment driver goes empty

When driver confirms delivery:
- Immediately trigger `09-carrier-confirmation-loop` for best backhaul candidate
- Goal: driver has next load within 30 minutes of going empty

### Step 8 — Deadhead Positioning (When No Local Freight)

If no suitable backhaul within 75 miles of delivery:

1. Calculate nearest high-freight market
2. Compare: deadhead cost vs. rate improvement from that market
   ```
   deadhead_miles = Google Maps(delivery → nearest_freight_hub)
   deadhead_cost = deadhead_miles × $0.35
   rate_improvement = (hub_market_rate - local_rate) × avg_loaded_miles
   
   if rate_improvement > deadhead_cost × 1.5:
       recommend_reposition(nearest_freight_hub)
   else:
       accept_local_freight(lower_rate)
   ```
3. If repositioning: notify carrier of recommendation with math:
   > "Nearest good market is [city], [X] miles away. Cost to deadhead: ~$[X]. But rates there are $[X.XX]/mile vs $[X.XX] here — you'd recover deadhead in [X] miles loaded. Want to run it?"

---

## Outputs

```json
{
  "carrier_id": "CARR-032026-001",
  "current_load_id": "TMS-2026-0392",
  "delivery_city": "Atlanta",
  "delivery_state": "GA",
  "estimated_available": "2026-03-20T19:30:00Z",
  "freight_market_rating": "RICH",
  "backhaul_candidates": [
    {
      "load_id": "DAT-9934521",
      "origin": "Atlanta, GA",
      "destination": "Nashville, TN",
      "pickup_date": "2026-03-21",
      "pickup_window": "07:00-11:00",
      "estimated_rate": 2.45,
      "miles": 250,
      "chain_score": 8.2,
      "priority": 1
    }
  ],
  "positioning_recommendation": null
}
```

---

## Error Handling

| Situation | Action |
|-----------|--------|
| Freight-poor delivery market | Flag at booking; negotiate higher inbound rate |
| No backhaul within 75 miles | Recommend deadhead to nearest hub |
| Best backhaul books before driver delivers | Move to next candidate |
| Driver needs reset after delivery | Factor reset time into search window |
