---
skill_id: "23-weather-risk-monitoring"
name: "Weather Risk Monitoring"
version: "1.0.0"
phase: 13
priority: high
trigger: "Activated at dispatch for every load. Monitors route continuously until delivery confirmed."
inputs:
  - load_id: "string"
  - route: "origin, destination, waypoints"
  - pickup_datetime: "ISO 8601"
  - delivery_datetime: "ISO 8601"
  - driver_id: "string"
  - commodity: "string (perishables need extra alert threshold)"
outputs:
  - weather_alerts: "array of weather risks along route"
  - route_recommendations: "alternate routes if primary blocked"
  - delay_estimate: "hours of expected delay if weather event"
  - broker_notification_needed: "boolean"
integrations:
  - NOAA / National Weather Service API
  - Weather.gov alerts API
  - Google Maps (alternate routing)
  - FHWA road conditions API
  - TMS
  - WhatsApp/SMS (driver alerts)
  - Email (broker notifications)
depends_on: ["13-driver-dispatch"]
triggers_next: ["15-in-transit-monitoring (feeds weather data)"]
tags: [weather, safety, route, delay, storm, ice, alert, risk]
---

# SKILL: Weather Risk Monitoring
## Cortex Bot — Truck Dispatch Automation

**Trigger**: Activated at dispatch. Monitors route continuously until delivery.
**Phase**: 13 (concurrent with in-transit monitoring)
**Priority**: HIGH

---

## Purpose

Weather is the #1 cause of unexpected delays, missed appointments, and detention claims in trucking. This skill monitors weather along the route proactively — not reactively — so the dispatcher can reroute, notify brokers early, and keep drivers safe before conditions deteriorate.

---

## Weather Risk Categories

| Risk Level | Condition | Driver Impact |
|------------|-----------|---------------|
| WATCH | Rain, light snow, fog | Slow speeds; allow extra time |
| WARNING | Heavy snow, ice, severe thunderstorm | Possible road closures; 30–120 min delays |
| CRITICAL | Blizzard, ice storm, tornado, hurricane, flooding | Route may close; load may need to hold |
| EMERGENCY | Road closed by DOT/State Police | Driver must stop; load delayed indefinitely |

---

## Execution Steps

### Step 1 — Pre-Dispatch Weather Scan

Before dispatch sheet is sent:
1. Pull 72-hour forecast for all points along route (every 100 miles)
2. Check NWS alerts for all counties along route
3. Check FHWA road conditions (closures, chain requirements, weight restrictions)
4. If any WARNING or higher → include weather note in dispatch sheet

### Step 2 — Continuous Route Monitoring

Every 30 minutes while load is in transit:
1. Check current conditions along remaining route
2. Check updated forecasts for next 12 hours ahead of driver's position
3. Check NWS active alerts and FHWA road conditions

### Step 3 — Alert Thresholds & Actions

```
if weather_level == "WATCH":
    # Log only — no immediate action
    add note to TMS
    if commodity is perishable:
        monitor more frequently (every 15 min)

elif weather_level == "WARNING":
    # Notify driver and broker
    alert_driver(safety_guidance)
    notify_broker(delay_possible, estimated_delay)
    log in TMS

elif weather_level == "CRITICAL":
    # Immediate action
    call_driver()
    assess: continue or hold?
    notify_broker(delay_certain, estimated_delay)
    identify safe holding location
    document force_majeure event in TMS

elif weather_level == "EMERGENCY":
    # Road closed
    call_driver()
    direct_driver_to_safe_location()
    call_broker_immediately()
    assess reroute or wait
    document everything for claims
```

### Step 4 — Driver Safety Communications

**WATCH alert:**
> "⚠️ Weather heads up: [rain/light snow/fog] expected near [location] around [time]. Reduce speed and keep extra following distance. Let me know if conditions get worse."

**WARNING alert:**
> "🌨️ Weather Warning: [heavy snow/ice/storm] alert for [counties] along your route. Expected impact: [time window]. Slow down and check road conditions. I've notified the broker. Nearest safe stop if you need to hold: [Truck stop name, address]."

**CRITICAL alert:**
> "🚨 Severe Weather Alert: [condition] near your route. I recommend you find a safe place to hold NOW. Nearest safe location: [Name, address, distance]. I am calling the broker to advise of a potential delay. DO NOT risk driving through this — your safety comes first."

### Step 5 — Broker Notification

When WARNING or higher:

Email to broker:
> Subject: `Weather Alert — Load [Ref#] — Possible Delay`
> Body: "I want to give you proactive notice that there is a [weather event] alert affecting our driver's route near [location]. This may cause a delay of approximately [X hours]. We are monitoring the situation and will update you immediately as conditions develop. Driver is currently [X miles/hours] from delivery. Please advise if the receiver has flexibility on the appointment window."

### Step 6 — Alternate Routing

When primary route is impacted:
1. Pull alternate route from Google Maps avoiding impacted area
2. Calculate: extra miles + extra time vs. waiting for road to clear
3. If alternate is <50 miles longer and opens road: recommend alternate
4. If alternate is impractical: recommend holding at nearest safe location

```
primary_route_blocked = True
alternate = Google Maps.get_route(
    origin=driver_current_location,
    destination=load.destination,
    avoid=[impacted_area]
)

if alternate.extra_miles < 50 and alternate.passable:
    recommend_alternate(alternate)
    update_eta(alternate.arrival_time)
    notify_broker(new_eta)
else:
    recommend_hold(nearest_safe_truck_stop)
    notify_broker(indefinite_delay)
```

### Step 7 — Force Majeure Documentation

When weather causes a delay or load failure:
1. Screenshot all NWS alerts and FHWA road closure notices
2. Save weather data with timestamps in load folder
3. Note in TMS: WEATHER_DELAY with event type, duration, documentation links
4. This documentation protects carrier from claims of negligence

---

## High-Risk Corridors Reference

| Corridor | Season | Risk |
|----------|--------|------|
| I-70 (Eisenhower Tunnel, CO) | Oct–Apr | Blizzard, closures |
| I-90 (Montana, Wyoming) | Oct–Apr | Ice, whiteout |
| I-80 (Donner Pass, CA) | Nov–Mar | Snow, closures |
| I-10 (Texas panhandle) | All year | Dust storms, ice in winter |
| I-95 (Northeast corridor) | Nov–Mar | Ice, snow events |
| I-40 (Oklahoma panhandle) | All year | Tornadoes, ice storms |
| Gulf Coast I-10/I-12 | Jun–Nov | Hurricane season |
| Appalachian mountain passes | Oct–Mar | Ice, snow |

---

## Outputs

```json
{
  "load_id": "TMS-2026-0392",
  "monitoring_active": true,
  "current_risk_level": "WATCH",
  "active_alerts": [
    {
      "type": "Winter Storm Watch",
      "area": "Hamilton County, TN",
      "start": "2026-03-20T22:00:00Z",
      "end": "2026-03-21T12:00:00Z",
      "driver_impact": "Driver will be past this area by 18:00 — no impact",
      "action_taken": "logged_only"
    }
  ],
  "broker_notified": false,
  "route_status": "CLEAR"
}
```

---

## Error Handling

| Situation | Action |
|-----------|--------|
| Weather API offline | Check Weather.gov manually every 30 min; flag for human review |
| Driver ignores severe weather warning | Escalate to owner immediately; document refusal |
| Road closed with no alternate | Hold load; notify broker; document force majeure |
| Perishable load at risk in weather hold | Immediate broker call — commodity may require special handling |
