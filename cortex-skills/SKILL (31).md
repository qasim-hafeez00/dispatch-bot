---
skill_id: "22-fuel-optimization"
name: "Fuel Optimization"
version: "1.0.0"
phase: 12
priority: medium
trigger: "Triggered at dispatch for every load. Also triggered when driver requests fuel guidance or when route deviation is detected."
inputs:
  - route: "origin, destination, waypoints"
  - truck_fuel_level: "gallons remaining (from telematics)"
  - truck_mpg: "float — from carrier profile"
  - fuel_card_network: "Comdata, EFS, TCS, Pilot, Love's, etc."
  - driver_preferences: "preferred truck stop chains"
outputs:
  - fuel_stop_plan: "ordered list of recommended stops with prices"
  - estimated_fuel_cost: "float — total fuel cost for this load"
  - fuel_savings_vs_random: "float — savings vs stopping anywhere"
integrations:
  - GasBuddy API / OPIS fuel price data
  - Google Maps / HERE Maps API
  - Fuel card network APIs (Comdata, EFS, TCS)
  - Telematics API (fuel level)
  - TMS
depends_on: ["13-driver-dispatch"]
triggers_next: []
tags: [fuel, optimization, cost, diesel, fuel-card, route]
---

# SKILL: Fuel Optimization
## Cortex Bot — Truck Dispatch Automation

**Trigger**: Triggered at dispatch for every load.
**Phase**: 12 (concurrent with dispatch)
**Priority**: MEDIUM

---

## Purpose

Fuel is the #1 operating cost for truckers — typically 35–40% of gross revenue. Optimizing fuel stop locations and prices can save $200–600 per load for long-haul runs. This skill finds the cheapest diesel along the route, accounts for fuel card discounts, and plans stops that don't waste time.

---

## Fuel Cost Context

- Average semi gets 6–7 MPG
- At 500 loaded miles: ~75 gallons needed
- At $3.80/gallon (national avg diesel): ~$285 for that run
- With fuel optimization (saving $0.25/gallon avg): ~$19 saved per fill-up
- At 100 loads/month: ~$1,900–3,800/month in fuel savings

---

## Execution Steps

### Step 1 — Route Mapping

```
full_route = Google Maps route(origin → destination)
total_miles = full_route.distance
estimated_gallons = total_miles / truck_mpg
fuel_needed = estimated_gallons + 20  # buffer gallons

waypoints = full_route.waypoints_every_150_miles
# Plan fuel stops every 150–200 miles (safe range for most tanks)
```

### Step 2 — Fuel Price Lookup

For each waypoint (150-mile intervals along route):

1. Query fuel price APIs for diesel within 5 miles of waypoint:
   - OPIS (Oil Price Information Service) — most accurate
   - GasBuddy commercial API
   - Fuel card network's own price feed (most accurate for card users)

2. Pull prices for:
   - Truck stops on driver's fuel card network
   - Non-card stops (for comparison)
   - DEF (Diesel Exhaust Fluid) prices if truck requires it

### Step 3 — Fuel Card Network Discounts

Apply fuel card discounts to raw prices:

| Fuel Card | Typical Discount | Notes |
|-----------|-----------------|-------|
| Comdata | $0.10–0.40/gal | Volume-based |
| EFS (WEX) | $0.08–0.35/gal | Network-dependent |
| TCS Fuel | $0.15–0.50/gal | Strong at Pilot/Flying J |
| Pilot Flying J | In-network only | Loyalty + card combo |
| Love's | In-network only | Loyalty + card combo |
| Fleet One | $0.10–0.30/gal | |

```
for each stop:
    rack_price = api_price
    card_discount = fuel_card_discount_at_this_location
    effective_price = rack_price - card_discount
    
    stop.effective_price = effective_price
    stop.savings_vs_national_avg = national_avg_diesel - effective_price
```

### Step 4 — Optimize Stop Selection

Select 1–3 stops per load using:

```
optimal_stops = []
for waypoint in route_waypoints:
    nearby_stops = get_stops_within_5mi(waypoint)
    best_stop = min(nearby_stops, key=lambda s: s.effective_price)
    
    # Only stop here if price is worthwhile
    # Rule: if best nearby price is within $0.05 of next stop, skip and combine
    if best_stop.effective_price < (cheapest_next_50mi + 0.05):
        optimal_stops.append(best_stop)
```

### Step 5 — Timing Optimization

- Avoid fueling during peak hours at major truck stops (Mon morning, Fri afternoon)
- Plan fuel stops to coincide with mandatory 30-minute break (double-dip time)
- Flag stops with long fuel lines based on historical data
- Prefer stops with reserved truck parking if driver's break timing aligns

### Step 6 — Generate Fuel Plan

```
FUEL PLAN — Nashville, TN → Atlanta, GA

Route: 248 miles | ~40 gallons needed | Tank: 150 gal capacity

Recommended stops:
1. ⛽ Pilot #1423 — Chattanooga, TN (mile 118)
   Diesel: $3.42/gal (card price after Comdata discount)
   DEF: $2.89/gal
   Fill: 40 gallons = $136.80
   Parking: Available
   Amenities: Shower, Subway
   
   Savings vs random stop: $0.28/gal × 40 gal = $11.20 saved

Total estimated fuel cost: $136.80
Savings vs unoptimized: ~$11.20
```

### Step 7 — Send to Driver

Send fuel plan alongside dispatch sheet:
> "Fuel stop recommendation: **Pilot #1423 in Chattanooga** (mile ~118). Best price along route at $3.42/gal after your Comdata discount. Good place for your 30-min break too. DEF available if needed."

---

## Outputs

```json
{
  "load_id": "TMS-2026-0392",
  "total_route_miles": 248,
  "estimated_gallons": 40,
  "fuel_stops": [
    {
      "stop_name": "Pilot Travel Center #1423",
      "address": "7890 Lee Hwy, Chattanooga, TN",
      "mile_marker": 118,
      "diesel_price_rack": 3.70,
      "card_discount": 0.28,
      "effective_price": 3.42,
      "fill_gallons": 40,
      "fill_cost": 136.80,
      "def_available": true,
      "parking": "available",
      "amenities": ["shower", "restaurant", "laundry"]
    }
  ],
  "total_fuel_cost": 136.80,
  "savings_vs_unoptimized": 11.20
}
```

---

## Error Handling

| Situation | Action |
|-----------|--------|
| Fuel price API unavailable | Use GasBuddy as fallback; note prices may be delayed |
| Card not accepted at cheapest stop | Route to next best card-accepting stop |
| Driver deviates from fuel plan | No action — recommendation only, not mandatory |
| Low fuel alert from telematics | Alert driver immediately with nearest stop |
