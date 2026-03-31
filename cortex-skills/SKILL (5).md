---
skill_id: "13-driver-dispatch"
name: "Driver Dispatch"
version: "1.0.0"
phase: 12
priority: critical
trigger: "Rate Confirmation signed and verified; load ready to be assigned to driver"
inputs:
  - load_record: "all details from signed RC"
  - carrier_profile: "driver name, phone, communication preference"
  - hos_data: "current hours of service remaining"
outputs:
  - dispatch_sheet: "formatted load instructions sent to driver"
  - check_call_schedule: "automated milestone reminders"
  - broker_notification: "driver assignment confirmation"
integrations:
  - TMS
  - ELD/Telematics API
  - WhatsApp Business API / SMS gateway
  - Google Maps API
  - Email
depends_on: ["12-rate-confirmation-review"]
triggers_next: ["15-in-transit-monitoring", "14-hos-compliance"]
tags: [dispatch, driver, check-calls, communication]
---
# SKILL: Driver Dispatch
## Cortex Bot — Truck Dispatch Automation

**Trigger**: Rate Confirmation signed and verified; load ready to be assigned to driver.  
**Phase**: 12 (Driver Dispatch)  
**Priority**: CRITICAL  

---

## Purpose

Generate a complete, clear dispatch sheet and deliver it to the driver with all information needed to execute the load without needing to call the dispatcher. Set up the check-call schedule and confirm driver readiness.

---

## Inputs Required

From TMS (load record):
- All load details from signed RC
- Broker contact info
- Commodity, weight, temperature requirements
- Special equipment requirements
- Facility notes

From carrier profile:
- Driver name and cell phone
- Owner name and cell phone (if different)
- Communication preference (call/text/WhatsApp/email)

---

## Execution Steps

### Step 1 — Pre-Dispatch HOS Verification

Before dispatching, confirm driver has enough hours:
1. Check ELD/hours of service remaining
2. Calculate: drive time to pickup + pickup dwell estimate + drive time to delivery
3. If driver has insufficient HOS:
   - Notify broker of delay immediately
   - Reschedule appointment if possible
   - DO NOT dispatch if driver will violate HOS

**HOS Reference:**
- Property-carrying: 11 hours driving, 14-hour window, 10 hours off-duty reset
- 30-minute break required after 8 hours of driving
- 60/70 hour limit in 7/8 days

### Step 2 — Generate Dispatch Sheet

**Format: Plain text, bullet points, mobile-optimized (readable on phone)**

```
=== DISPATCH SHEET ===
Load #: [TMS Load ID] | Broker Ref: [REF#]
Dispatched: [Date/Time]

--- BROKER CONTACT ---
Company: [Broker Name]
Contact: [Broker Rep Name]
Phone: [Direct number]
Email: [Email]
After hours: [24hr line if available]

--- PICKUP ---
Company: [Shipper Name]
Address: [Full address]
City, State ZIP: [X]
Date: [Pickup Date]
Appointment: [Time] [OR FCFS window: X:00 AM – X:00 PM]
Ref #: [PO# or Shipper Ref]
Contact: [Shipper phone if available]
Instructions: [Gate entry, dock#, check-in process, photo ID required, etc.]

--- DELIVERY ---
Company: [Receiver Name]
Address: [Full address]
City, State ZIP: [X]
Date: [Delivery Date]
Appointment: [Time] [OR FCFS window]
Ref #: [Delivery Ref#]
Contact: [Receiver phone if available]
Instructions: [Lumper process, dock#, check-in process, etc.]

--- LOAD DETAILS ---
Commodity: [X]
Weight: [X] lbs
Pieces: [X] [pallets/cases/drums/etc.]
Temperature: [X°F - X°F] (reefer only)
Load type: [Live load / Drop & hook / Pre-loaded]
Unload type: [Live unload / Drop & hook / Lumper]
Lumper: [Broker-paid — call broker for auth code at delivery] OR [Carrier-paid — keep receipt]

--- EQUIPMENT CHECK ---
(Flatbed): Tarps required: [Yes/No] | Straps: [X] | Chains: [X]
(Reefer): Pre-cool trailer to [X°F] before loading | Continuous/cycle-sentry
(Van): Load locks required: [Yes/No]
PPE required: [Safety vest, hard hat, steel toes — Yes/No]

--- TRACKING ---
Method: [Macropoint / FourKites / Samsara / GPS share link]
Setup: [Link or instructions to connect tracking before pickup]
BOL requirement: Record in/out times at EVERY stop — stamped or written

--- PAYMENT INFO ---
Rate: $[X] all-in
Detention: FREE for 2 hours, then $[X]/hour after
TONU: $[X] if load is cancelled after you are dispatched
Lumper: [Broker-paid with auth / Keep receipt for reimbursement]

--- CHECK-IN SCHEDULE ---
✓ Confirm this dispatch received: Reply "CONFIRMED" to this message
✓ Departing to pickup: Call/text when wheels rolling
✓ Arrived at pickup: Text arrival time
✓ Loaded and departed: Text departure time + BOL # 
✓ Every 2 hours in transit: Check-in text with location
✓ 1 hour from delivery: Text ETA
✓ Arrived at delivery: Text arrival time
✓ Delivered/empty: Text departure time + confirm BOL signed + photos sent

--- EMERGENCY CONTACTS ---
Dispatcher: [Name] — [Phone] (available [hours])
Broker after-hours: [Phone]
Breakdown: [Fleet maintenance or roadside service number]

======================
```

### Step 3 — Deliver Dispatch Sheet

1. Send via preferred channel (WhatsApp/SMS/email) — all three if unsure
2. For WhatsApp: send as text message (not document) for easy reading on phone
3. For email: send as body text + attached PDF
4. Confirm receipt: wait for driver acknowledgement

### Step 4 — Equipment Confirmation

Call or text driver to confirm:
- [ ] Driver has required number of straps/tarps/chains (flatbed)
- [ ] Load locks present (van/reefer)
- [ ] Reefer fuel level sufficient
- [ ] Pre-trip inspection complete
- [ ] Tracking app/device activated

### Step 5 — Set Check-Call Schedule in TMS

Create automated reminder schedule:
- T+0: Dispatch sent — await acknowledgement
- T+depart: Trigger when driver confirms rolling
- T+loaded: Trigger when driver confirms loaded
- Every 2 hours: Auto-prompt check-in
- T-1hr delivery: Trigger ETA check
- T+delivery: Trigger delivery confirmation
- T+POD: Trigger document collection (call `pod-document-collection` skill)

### Step 6 — Notify Broker

Send brief confirmation to broker:
> "Driver [First Name] ([phone]) is confirmed for your [origin → destination] load [ref#]. Pickup appointment [date/time]. Tracking will be active via [tracking method]. Please send any facility-specific instructions to this number."

---

## Outputs

- Dispatch sheet sent and acknowledged ✅
- Check-call schedule set in TMS ✅
- Broker notified of driver assignment ✅
- Load status updated: `DISPATCHED — IN TRANSIT` ✅
- `in-transit-monitoring` skill activated ✅

---

## Integration Requirements

- **TMS**: Load record, HOS data, schedule management
- **ELD/Telematics API**: Real-time HOS verification
- **WhatsApp Business API / SMS gateway**: Driver messaging
- **Google Maps API**: Drive time estimates, route
- **Email**: Broker notification

---

## Error Handling

| Situation | Action |
|-----------|--------|
| Driver doesn't acknowledge within 30 min | Call driver directly |
| Driver HOS insufficient | Call broker, reschedule or find backup carrier |
| Driver declines load at last minute | Emergency re-broker (call `load-board-search` skill urgently) |
| Equipment not ready (missing straps, etc.) | Delay departure until corrected, notify broker |
| Tracking won't activate | Contact telematics support; manually call for check-ins |
