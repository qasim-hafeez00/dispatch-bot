---
skill_id: "15-in-transit-monitoring"
name: "In-Transit Monitoring & Exception Handling"
version: "1.0.0"
phase: 13
priority: critical
trigger: "Driver confirmed rolling (dispatch acknowledged). Runs until delivery confirmed."
inputs:
  - load_id: "string"
  - driver_id: "string"
  - gps_feed: "real-time position from telematics"
  - check_call_schedule: "from driver-dispatch skill"
outputs:
  - milestone_log: "all events timestamped in TMS"
  - broker_updates: "proactive status messages"
  - exception_reports: "delays, breakdowns, appointment misses"
integrations:
  - Telematics/ELD API
  - Weather API
  - Google Maps / HERE Maps
  - WhatsApp/SMS
  - VOIP/Phone
  - TMS
depends_on: ["13-driver-dispatch"]
triggers_next: ["16-detention-layover-management", "17-pod-invoicing-factoring"]
tags: [monitoring, gps, check-calls, exceptions, tracking]
---
# SKILL: In-Transit Monitoring & Detention Management
## Cortex Bot — Truck Dispatch Automation

**Trigger**: Driver dispatched and confirmed rolling. Runs continuously until delivery confirmed.  
**Phase**: 13–14 (In-Transit Monitoring + Detention Management)  
**Priority**: CRITICAL  

---

## Purpose

Maintain real-time visibility of the load, proactively communicate with brokers about milestones and delays, manage the detention clock, and resolve exceptions before they become problems.

---

## Monitoring Loop (runs every 15 minutes automatically)

### Step 1 — GPS Position Check
1. Pull GPS/ELD position from telematics API
2. Calculate:
   - Distance to next stop (pickup or delivery)
   - Estimated arrival time (ETA) based on current speed + traffic
   - HOS remaining for driver
3. Compare ETA against appointment time
4. If ETA > appointment time: trigger delay alert (see Step 5)

### Step 2 — Scheduled Check-Call Processing

At each scheduled check-call time:
1. Auto-send check-in prompt to driver via WhatsApp/SMS:
   > "📍 Check-in: Where are you now? ETA to [next stop]? Any issues?"
2. Wait 10 minutes for response
3. If no response: auto-call driver's cell
4. If still no response: escalate to owner's cell
5. Log check-call time and response in TMS

**Required check-calls:**
- Depart pickup (loaded)
- Every 2 hours in transit
- 1 hour before delivery
- Arrived at delivery
- Delivered + empty

### Step 3 — Milestone Logging

Log each event with timestamp in TMS:
- `DEPARTED_PU` — time driver left pickup
- `IN_TRANSIT` — en route updates
- `ARRIVED_DEL` — time arrived at delivery
- `UNLOADING` — time unloading started
- `DELIVERED` — time unloading complete + driver departed

### Step 4 — Broker Milestone Updates

Send proactive status updates to broker:

**At departure from pickup:**
> "Driver [Name] is loaded and rolling from [pickup city]. ETA [delivery city]: [date/time]. Tracking active at [link]."

**If running on time:**
> (no update needed — tracking is live)

**At 1 hour from delivery:**
> "Driver [Name] is approximately 1 hour out from [delivery city]. ETA [time]. Appointment is at [time]. [On schedule / Slightly behind — will arrive X min late]."

**At delivery:**
> "Driver [Name] has arrived at [delivery address] at [arrival time]. Unloading in progress."

### Step 5 — Delay Detection & Management

If ETA exceeds appointment window:
1. Immediately notify broker:
   > "Heads up — we're tracking driver [Name] and showing ETA of [new ETA] vs the [appointment time] appointment. [Reason if known: traffic, weather, breakdown]. Are there any schedule accommodations available? We want to give you as much notice as possible."
2. Ask broker to notify receiver
3. Log delay reason in TMS
4. If reason is breakdown: trigger emergency re-broking or roadside assistance

---

## Detention Management (Critical — Most Missed Revenue)

### Detention Clock Tracking

**Standard rule:** 2 hours free at pickup AND at delivery. After 2 hours, detention accrues.

**Step 1 — Set Geo-Fence Arrival Trigger**
- When driver enters geo-fence around facility (0.5-mile radius):
  - Auto-timestamp: `ARRIVED_AT_FACILITY = [time]`
  - Start 2-hour countdown
  - Alert dispatcher: "Driver arrived [facility] at [time]. Detention clock starts at [time + 2 hrs]"

**Step 2 — At 1:45 (15 minutes before detention starts)**
- Send proactive warning to broker:
  > "Driver [Name] arrived at [shipper/receiver] at [arrival time]. Per the rate confirmation, detention begins at 2 hours free, which will start at [detention start time]. Driver is currently waiting. Please notify the facility."

**Step 3 — At Detention Start (2 hrs after arrival)**
- Begin hourly detention billing
- Send detention notification to broker:
  > "Detention is now accruing on load [ref#]. Driver arrived at [arrival time]. Detention started at [detention start time]. Rate is $[X]/hour per the rate confirmation. Please document this on the BOL if possible."

**Step 4 — Track Detention Hours**
- Log each hour of detention
- When driver is finally released:
  - Note: `DETENTION_END_TIME = [time]`
  - Calculate: Total detention hours = (end - start) minus 2 free hours
  - Bill: Total hours × hourly rate = detention claim amount

**Step 5 — At Delivery Detention**
- Same process at delivery facility
- Track separately from pickup detention

**Step 6 — BOL Documentation**
- Instruct driver to get in/out times WRITTEN AND STAMPED on BOL by facility
- If facility refuses: driver photos their phone showing arrival time + any text/messages confirming arrival

---

## Exception Handling

### Breakdown on Road
1. Driver calls: "Truck broke down at [location]"
2. Immediately:
   - Notify broker: "I need to flag an issue — driver has experienced a breakdown at [mile marker/location]. We're arranging repair/towing. Will keep you updated every 30 minutes."
   - Check roadside assistance coverage (insurance or fleet plan)
   - Assess if load can be transferred to another truck
   - If perishable load (reefer): immediate escalation — commodity at risk
3. Every 30 min: update broker until resolved

### Weather Delay
1. Monitor weather along route (weather API)
2. If storm/flood/blizzard blocking route:
   - Proactively notify broker BEFORE delay occurs
   - Suggest alternate route or wait time
   - Document weather event for any force majeure claims

### Appointment Miss
1. Driver will miss delivery appointment by >30 minutes:
   - Call broker immediately
   - Ask broker to call receiver and reschedule
   - Document new appointment time
   - If delivery window missed entirely: assess re-delivery fee — document in TMS

### Load Discrepancy at Pickup
1. Driver arrives — counts are off or commodity is wrong:
   - Do NOT sign BOL for incorrect count/commodity
   - Call dispatcher immediately
   - Call broker to report
   - Document everything with photos
   - Options: accept with notation, refuse load, or wait for correction

---

## Outputs

- All milestones logged with timestamps in TMS ✅
- Broker notified of all updates ✅
- Detention tracked and documented ✅
- Exceptions resolved and logged ✅
- `pod-document-collection` skill triggered at delivery ✅

---

## Integration Requirements

- **Telematics/ELD API**: GPS position, HOS data, geofencing
- **Weather API**: Route weather monitoring
- **Google Maps / HERE Maps**: Traffic, ETA calculation
- **WhatsApp/SMS**: Driver communication
- **VOIP/Phone**: Auto-call on check-call miss
- **TMS**: Full event logging, milestone tracking

---

## Error Handling

| Situation | Action |
|-----------|--------|
| GPS signal lost | Switch to manual check-call every 2 hours |
| Driver not responding | Call owner, then emergency contact |
| Broker unresponsive on delay | Document attempts; proceed best available |
| ELD malfunction | Document, continue with paper log temporarily |
