---
skill_id: "14-hos-compliance"
name: "Hours of Service Compliance Monitor"
version: "1.0.0"
phase: 12
priority: critical
trigger: "Runs continuously for every active driver from the moment they are dispatched until they complete a 34-hour reset"
inputs:
  - driver_id: "string"
  - eld_feed: "real-time ELD data stream"
  - current_load: "load details including pickup and delivery requirements"
outputs:
  - hos_status: "object with all current hour buckets"
  - compliance_alerts: "array of upcoming violations or risks"
  - reset_recommendation: "when and where driver should take 34-hr reset"
integrations:
  - ELD API (KeepTruckin/Motive, Samsara, Omnitracs, PeopleNet)
  - TMS
  - Google Maps API
  - WhatsApp/SMS (driver alerts)
depends_on: ["13-driver-dispatch"]
triggers_next: ["15-in-transit-monitoring"]
tags: [hos, compliance, eld, hours-of-service, dot, violations]
---

# SKILL: Hours of Service Compliance Monitor
## Cortex Bot — Truck Dispatch Automation

**Trigger**: Continuously active from driver dispatch until 34-hour reset complete.
**Phase**: 12–13 (concurrent with dispatch and transit monitoring)
**Priority**: CRITICAL

---

## Purpose

Ensure no driver ever violates Hours of Service regulations. An HOS violation is a federal offense that creates liability for the carrier and dispatcher, can result in out-of-service orders, and may void cargo insurance coverage. Prevent violations proactively — never reactively.

---

## HOS Rules Reference (Property-Carrying Drivers)

| Rule | Limit | Notes |
|------|-------|-------|
| Daily drive time | 11 hours | After 10 consecutive hrs off duty |
| Daily on-duty window | 14 hours | Non-extendable; includes all on-duty time |
| Mandatory break | 30 min | Required after 8 hours driving |
| Weekly limit (7-day) | 60 hours on-duty | Resets with 34-hr restart |
| Weekly limit (8-day) | 70 hours on-duty | Resets with 34-hr restart |
| Off-duty reset | 10 consecutive hours | Resets daily 11-hr and 14-hr clock |
| 34-hour restart | 34 consecutive hours off-duty | Resets 60/70-hr weekly clock |
| Adverse conditions | +2 hours driving | Weather/road emergency only |
| Short-haul exemption | 150 air-mile radius, return to home base, max 14-hr window | Eliminates 30-min break |
| Split sleeper berth | Various combinations | Complex — see FMCSA guidance |

---

## Execution Steps

### Step 1 — ELD Data Pull (Every 15 Minutes)

From ELD API, retrieve:
```json
{
  "driver_id": "DRV-001",
  "current_status": "driving",
  "current_duty_status_start": "2026-03-20T06:00:00Z",
  "driving_today": 4.5,
  "on_duty_today": 5.2,
  "off_duty_last_reset": 10.25,
  "weekly_on_duty_hours": 38.5,
  "time_remaining_driving": 6.5,
  "time_remaining_window": 8.8,
  "break_taken_today": false,
  "break_hours_since_last": 3.2,
  "eld_provider": "Samsara",
  "last_updated": "2026-03-20T10:30:00Z"
}
```

### Step 2 — Feasibility Check Against Current Load

```
# Can driver reach pickup?
drive_to_pickup = Google Maps ETA(truck_location → pickup_address)
if drive_to_pickup > driver.time_remaining_driving:
    ALERT — "HOS insufficient to reach pickup"
    
# Can driver complete load in current window?
total_trip_time = drive_to_pickup + pickup_dwell_est(2hrs) + drive_to_delivery
if total_trip_time > driver.time_remaining_window:
    ALERT — "Load cannot be completed in current 14-hr window — reset needed"
    plan_reset(between_pickup_and_delivery=True)

# Break requirement check
if driver.driving_today >= 8 and not driver.break_taken_today:
    ALERT — "30-minute break required before driving more than 8 hours"
    recommend_break_location(near=truck_location)
```

### Step 3 — 60/70-Hour Weekly Limit Tracking

```
if driver.weekly_on_duty_hours >= 55:
    ALERT — "Driver approaching weekly hour limit (55+ of 60/70)"
    plan_34hr_restart_location()
    
if driver.weekly_on_duty_hours >= 58:
    CRITICAL_ALERT — "Driver within 2 hours of weekly limit — plan reset NOW"
    
if driver.weekly_on_duty_hours >= 60:  # or 70
    EMERGENCY — "Driver has reached weekly limit — must not drive"
    notify_broker_immediately()
    arrange_alternative_carrier()
```

### Step 4 — Proactive Reset Planning

When weekly hours are at 50+, begin planning the 34-hour reset:

1. Identify best reset location:
   - Truck stop with parking availability
   - Near delivery or pickup (minimize deadhead after reset)
   - Safety — avoid high-crime areas
   - Amenities — shower, food, laundry (driver welfare)

2. Calculate optimal reset timing:
   - If reset taken in [city X] at [time Y], driver will have full hours available by [time Z]
   - Match reset window to freight availability in that market

3. Pre-search loads available from reset location at reset completion time
   - Pass results to `21-backhaul-planning` skill

### Step 5 — Alert Routing

| Alert Level | Condition | Recipient | Channel |
|-------------|-----------|-----------|---------|
| INFO | <2 hrs remaining driving | Driver | WhatsApp |
| WARNING | <1 hr remaining driving | Driver + Dispatcher | WhatsApp + TMS |
| CRITICAL | <30 min remaining | Driver + Owner + Broker | Call + WhatsApp |
| EMERGENCY | At limit — cannot drive | All parties | All channels |

### Step 6 — Violation Prevention Communications

**When driver is within 1 hour of limit:**
> "⚠️ HOS Alert: You have approximately 60 minutes of drive time remaining today. You MUST stop and go off-duty within 60 minutes. Nearest truck stops: [List 3 options with distance]. Reply with which one you're heading to."

**When driver must stop immediately:**
> "🛑 STOP: You have reached your HOS limit. Pull off at the next safe location and go off-duty immediately. Do NOT continue driving. I am notifying the broker of the delay."

---

## Reset Planning Output

```json
{
  "driver_id": "DRV-001",
  "reset_needed": true,
  "earliest_reset_start": "2026-03-20T22:00:00Z",
  "earliest_available_after_reset": "2026-03-22T08:00:00Z",
  "recommended_reset_locations": [
    {
      "name": "Pilot Travel Center #1234",
      "address": "1234 I-75, Chattanooga, TN",
      "parking_availability": "high",
      "amenities": ["shower", "restaurant", "laundry"],
      "distance_from_delivery": "12 miles"
    }
  ],
  "loads_available_after_reset": "pass to 21-backhaul-planning"
}
```

---

## Error Handling

| Situation | Action |
|-----------|--------|
| ELD API offline | Switch to manual check-call every 2 hours; estimate hours conservatively |
| Driver disputes ELD data | Document dispute; use ELD data as official record |
| Adverse conditions exemption needed | Document weather event; extend limit per FMCSA rules; log in TMS |
| Driver refuses to stop at HOS limit | Document refusal in writing; notify owner; contact broker |
