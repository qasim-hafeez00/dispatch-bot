---
skill_id: "16-detention-layover-management"
name: "Detention & Layover Management"
version: "1.0.0"
phase: 14
priority: high
trigger: "Driver arrives at any facility (pickup or delivery) — geo-fence entry detected"
inputs:
  - driver_id: "string"
  - facility_type: "enum: pickup | delivery"
  - load_id: "string"
  - rc_detention_terms: "free hours, rate per hour"
  - arrival_timestamp: "ISO 8601 — from geo-fence trigger"
outputs:
  - detention_hours: "float — billable hours accrued"
  - detention_claim_amount: "float — dollars to invoice"
  - layover_claim_amount: "float — if overnight required"
  - tonu_claim_amount: "float — if load cancelled after dispatch"
  - all_timestamps_documented: "arrival, loading start, departure"
integrations:
  - Telematics/GPS API (geo-fence)
  - WhatsApp/SMS (driver)
  - Email/VOIP (broker alerts)
  - TMS
depends_on: ["15-in-transit-monitoring"]
triggers_next: ["17-pod-invoicing-factoring", "27-accessorials-tracking"]
tags: [detention, layover, tonu, accessorials, claims, timestamps, geo-fence]
---

# SKILL: Detention & Layover Management
## Cortex Bot — Truck Dispatch Automation

**Trigger**: Driver enters facility geo-fence (0.5-mile radius) at pickup or delivery.
**Phase**: 14
**Priority**: HIGH

---

## Purpose

Automatically track every minute a driver spends waiting at a facility and convert that time into billable detention revenue. Most dispatchers miss 60–80% of detention claims due to poor timestamp tracking. This skill makes it impossible to miss.

---

## Industry Context

- Standard detention: **2 hours free**, then **$25–75/hour** after
- Average detention per load: **1.8 hours** beyond the free window
- Average missed detention per load (without automation): **$45–90**
- At 100 loads/month: **$4,500–9,000/month in unclaimed revenue**

---

## Detention Execution Steps

### Step 1 — Geo-Fence Arrival Detection

When driver's GPS enters 0.5-mile radius of facility:
1. Auto-timestamp: `ARRIVED_FACILITY = [exact time]`
2. Log in TMS immediately
3. Notify dispatcher: "Driver arrived at [Pickup/Delivery] facility at [time]. Detention clock starts at [time + free hours]."
4. Send driver check-in message:
   > "📍 You've arrived at [facility name]. Please reply with your exact arrival time if different. Do you have an appointment or is it FCFS?"

### Step 2 — Driver Confirmation

Driver replies with:
- Arrival time confirmation (override if different from geo-fence — sometimes driver parks at gate, not dock)
- Queue status: "In line", "At dock", "Waiting for lumper", etc.
- Any issues: "Receiver says no appointment on file", "Dock is full", etc.

Log confirmed arrival time in TMS. This is the official clock start.

### Step 3 — Detention Countdown Timer

```
free_hours = rc_detention_terms.free_hours  # usually 2.0
detention_start = arrival_time + timedelta(hours=free_hours)

# Set alerts:
alert_at_1h45m = arrival_time + timedelta(hours=1, minutes=45)  # 15 min before detention
alert_at_2h00m = detention_start  # detention begins
alert_every_1h = detention_start + timedelta(hours=1), +2, +3...  # hourly billing
```

### Step 4 — 15-Minute Pre-Detention Alert to Broker

At `alert_at_1h45m`:

> "Hi [Broker Name], this is [Dispatcher] with [Carrier Company]. I wanted to give you a heads up — driver [Name] arrived at [facility] at [arrival time]. Per the rate confirmation, detention begins at 2 hours free, which starts at **[detention_start time]**. Driver is currently in [queue/dock]. Wanted to alert you so you can try to get the facility to speed things up. Let me know if you need anything."

**Why 15 minutes early**: Gives broker time to call the facility and potentially avoid detention — which builds goodwill. Also establishes the timeline in writing before the clock hits.

### Step 5 — Detention Start Notification

At `detention_start`:

> "Detention is now accruing on load **[Ref#]**. Driver **[Name]** arrived at **[arrival time]**, detention per RC starts at **[detention_start]** at **$[rate]/hour**. Driver is still at facility waiting. Please note this on the BOL if possible and have the facility note the delay."

### Step 6 — Hourly Billing Tracker

```python
detention_hours = 0
current_time = detention_start

while driver_status != "DEPARTED_FACILITY":
    wait 1 hour
    detention_hours += 1
    detention_amount = detention_hours * rc_detention_rate
    
    # Update TMS
    log(f"Detention hour {detention_hours} accrued. Total: ${detention_amount}")
    
    # Notify broker at each hour if dwell is extended (>4 hours total)
    if detention_hours >= 2:
        notify_broker(f"Driver still at facility — {detention_hours} hours of detention accrued (${detention_amount})")
```

### Step 7 — Departure Timestamp

When driver departs facility (geo-fence exit OR driver message):
1. Auto-timestamp: `DEPARTED_FACILITY = [exact time]`
2. Calculate final detention:
   ```
   total_dwell = DEPARTED - ARRIVED
   billable_detention = max(0, total_dwell - free_hours)
   detention_amount = billable_detention × hourly_rate
   ```
3. Instruct driver: "Make sure your in/out times are written on the BOL — exact times, signed by the facility if possible."

### Step 8 — BOL Documentation Requirement

Message to driver:
> "Before you leave — make sure the BOL shows:
> ✅ Your ARRIVAL time: **[time]**
> ✅ Your DEPARTURE time: **[time]**
> If the facility won't write it, take a photo of your ELD or phone showing the time at the dock."

---

## Layover Management

**Layover** = driver must hold overnight because delivery appointment is the next day.

### Layover Trigger Conditions
- Driver delivered or completed pickup today
- Next appointment is tomorrow (no same-day continuation possible)
- HOS prevents reaching next facility today

### Layover Rate Reference
- Standard layover: **$150–400/night** (varies by broker — must be in RC)
- Must be agreed in RC before dispatch — never assume

### Layover Steps
1. Identify layover situation at time of booking — lock rate in RC
2. When layover occurs: document in TMS with timestamp
3. Find safe truck stop with parking for driver
4. Notify broker: "Driver will be laying over tonight at [location] per the layover terms in the rate confirmation. Layover charge of $[X] will be added to invoice."
5. Add layover to invoice in `17-pod-invoicing-factoring`

---

## TONU (Truck Order Not Used)

**TONU** = carrier dispatched and en route, broker cancels the load.

### TONU Trigger
- Driver has been dispatched (received dispatch sheet)
- Broker calls/emails to cancel the load
- Driver has not yet loaded freight

### TONU Steps
1. Immediately timestamp: `TONU_NOTIFIED = [time]`
2. Verify driver's location — are they en route? Already at pickup?
3. Confirm TONU amount from RC (typically $150–250)
4. Notify carrier: "Load [Ref#] has been cancelled. You're entitled to a TONU of $[X]. Please confirm your current location."
5. Add TONU line item to invoice
6. Log: TONU_CLAIMED, amount, timestamp, driver location at time of cancel

### TONU Documentation
- Screenshot or save broker's cancellation email/text
- Note driver location from GPS at time of cancel
- Document any fuel or time cost to driver

---

## Claim Summary Output (passed to 27-accessorials-tracking)

```json
{
  "load_id": "TMS-2026-001",
  "pickup_detention": {
    "arrival": "2026-03-20T09:00:00Z",
    "departure": "2026-03-20T12:15:00Z",
    "total_hours": 3.25,
    "free_hours": 2.0,
    "billable_hours": 1.25,
    "rate_per_hour": 50,
    "amount": 62.50,
    "bol_documented": true
  },
  "delivery_detention": {
    "arrival": "2026-03-20T17:00:00Z",
    "departure": "2026-03-20T19:30:00Z",
    "total_hours": 2.5,
    "free_hours": 2.0,
    "billable_hours": 0.5,
    "rate_per_hour": 50,
    "amount": 25.00,
    "bol_documented": true
  },
  "layover": null,
  "tonu": null,
  "total_accessorial_claim": 87.50
}
```

---

## Error Handling

| Situation | Action |
|-----------|--------|
| Facility refuses to write times on BOL | Driver photos ELD + text/email timestamp as backup |
| Broker disputes detention timeline | Provide geo-fence timestamps + driver photos |
| RC has no detention clause | Attempt claim anyway — cite industry standard; note for future |
| Driver doesn't report departure | Use geo-fence exit as departure timestamp |
| TONU amount not in RC | Negotiate with broker — minimum $150 industry standard |
