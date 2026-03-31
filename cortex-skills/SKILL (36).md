---
skill_id: "27-accessorials-tracking"
name: "Accessorials Tracking"
version: "1.0.0"
phase: 14
priority: high
trigger: "Activated at load booking. Runs through delivery and invoicing to ensure every accessorial is claimed."
inputs:
  - load_id: "string"
  - rc_accessorials: "all accessorials locked in rate confirmation"
  - detention_data: "from 16-detention-layover-management"
  - lumper_receipts: "from driver"
  - extra_stop_data: "timestamps and confirmation"
outputs:
  - accessorials_claim_summary: "total of all billable extras"
  - invoice_line_items: "formatted for invoicing skill"
  - missing_documentation: "any claims lacking proof"
integrations:
  - TMS
  - WhatsApp/SMS (driver document requests)
  - 16-detention-layover-management
  - 17-pod-invoicing-factoring
depends_on: ["12-rate-confirmation-review", "16-detention-layover-management"]
triggers_next: ["17-pod-invoicing-factoring"]
tags: [accessorials, detention, lumper, tonu, extra-stops, claims, billing]
---

# SKILL: Accessorials Tracking
## Cortex Bot — Truck Dispatch Automation

**Trigger**: Activated at booking. Runs through full invoice cycle.
**Phase**: 14 (concurrent with transit + detention)
**Priority**: HIGH

---

## Purpose

Every dollar of accessorial revenue must be captured, documented, and invoiced. Industry data shows dispatchers miss 15–25% of accessorial claims due to poor tracking. This skill ensures nothing is left on the table.

---

## Accessorial Types Reference

| Accessorial | When Applies | Typical Rate | Documentation Needed |
|-------------|-------------|-------------|---------------------|
| Detention — Pickup | Dwell >2 hrs at shipper | $25–75/hr | Arrival/departure timestamps, BOL times |
| Detention — Delivery | Dwell >2 hrs at receiver | $25–75/hr | Arrival/departure timestamps, BOL times |
| Layover | Driver holds overnight | $150–400/night | Timestamps, location proof |
| TONU | Load cancelled after dispatch | $150–300 flat | Dispatch confirmation, cancellation notice |
| Extra Stop | More than 1 pickup or delivery | $50–150/stop | Signed stop BOL, address |
| Driver Assist | Driver helps load/unload | $50–150 flat | RC clause, driver confirmation |
| Lumper | Third-party unloaders | Actual cost | Lumper receipt |
| Fuel Surcharge | If separate from rate | Varies | Per RC terms |
| NYC Surcharge | Delivery in NYC metro | $150–300 flat | If agreed in RC |
| Overweight | Load exceeds standard weight | $100–300 | Weight ticket |
| Reefer Breakdown | Reefer unit fails on temp load | Varies | Maintenance record, temp logs |
| Truck Ordered Not Used (TONU) | Load cancelled | $150–300 | Per RC |

---

## Execution Steps

### Step 1 — RC Accessorial Extraction (At Booking)

Parse signed Rate Confirmation to extract all agreed accessorials:

```json
{
  "load_id": "TMS-2026-0392",
  "rc_accessorials": {
    "detention": {
      "free_hours": 2,
      "rate_per_hour": 50,
      "applies_to": ["pickup", "delivery"]
    },
    "tonu": {
      "amount": 150,
      "trigger": "cancelled_after_dispatch"
    },
    "driver_assist": {
      "amount": 75,
      "confirmed": false
    },
    "lumper": {
      "payer": "broker",
      "auth_process": "call_broker_for_code"
    },
    "extra_stops": null,
    "nyc_surcharge": null
  }
}
```

Create accessorial checklist in TMS. Every item on this list must be monitored and claimed if triggered.

### Step 2 — Live Tracking During Transit

Monitor each accessorial trigger in real time:

**Detention triggers:**
- Geo-fence arrival → start detention clock (handled by skill 16)
- Receive timestamps from skill 16 every hour
- Update TMS running total

**Lumper triggers:**
- Driver messages "lumper at receiver" → immediately:
  1. Request lumper receipt amount
  2. If broker-paid: call broker for auth code before lumper starts
  3. If carrier-paid: instruct driver to get receipt with amount + company name

**Extra stop triggers:**
- GPS confirms truck at additional address not on original RC
- Request driver confirmation and stop BOL
- Log extra stop in TMS

**Driver assist triggers:**
- Driver messages "had to help unload" → log immediately with timestamp
- Request confirmation message from broker that assist was required

**TONU triggers:**
- Broker calls/emails to cancel load
- Immediately log: TONU_TRIGGERED, timestamp, driver location
- Confirm TONU amount per RC

### Step 3 — Documentation Collection

For each triggered accessorial, collect proof:

| Accessorial | Required Proof |
|-------------|---------------|
| Detention | Geo-fence timestamps + BOL in/out times |
| Lumper | Signed lumper receipt with amount, date, company |
| Extra stop | Signed stop BOL from additional location |
| Driver assist | Broker email/message confirming or RC clause |
| TONU | Broker cancellation message/email + driver location proof |
| Layover | Timestamped driver log + location proof |
| Overweight | Weight ticket from certified scale |

If documentation is missing, request immediately:
> "Driver — I need the lumper receipt photo RIGHT NOW to bill it back. Send me a clear photo of the receipt showing the amount and company name."

### Step 4 — Claim Calculation

After delivery (before invoice generation):

```python
total_accessorials = 0

# Detention
if detention_pickup.billable_hours > 0:
    detention_pu_amount = detention_pickup.billable_hours × rc.detention_rate
    total_accessorials += detention_pu_amount
    
if detention_delivery.billable_hours > 0:
    detention_del_amount = detention_delivery.billable_hours × rc.detention_rate
    total_accessorials += detention_del_amount

# Lumper
if lumper_receipt_received:
    lumper_amount = parse_receipt(lumper_receipt)
    if rc.lumper_payer == "broker":
        total_accessorials += lumper_amount
    # If carrier-paid: it's already accounted for as carrier cost

# TONU
if tonu_triggered:
    total_accessorials += rc.tonu_amount

# Extra stops
for stop in extra_stops:
    if stop.confirmed and rc.extra_stop_rate:
        total_accessorials += rc.extra_stop_rate

# Driver assist
if driver_assist_confirmed:
    total_accessorials += rc.driver_assist_amount

# Layover
if layover_triggered:
    total_accessorials += rc.layover_rate × layover_nights
```

### Step 5 — Pre-Invoice Verification

Before submitting to invoicing skill:
- [ ] All triggered accessorials have documentation
- [ ] All amounts match RC terms exactly
- [ ] No undocumented claims included
- [ ] TONU documented with broker cancellation proof
- [ ] Lumper receipt is legible and shows correct amount

Flag any claim without full documentation as `NEEDS_DOCUMENTATION` — do not invoice undocumented claims.

### Step 6 — Pass to Invoicing

Send complete accessorial package to `17-pod-invoicing-factoring`:

```json
{
  "load_id": "TMS-2026-0392",
  "linehaul_rate": 700.00,
  "accessorials": [
    {
      "type": "detention_pickup",
      "hours": 1.25,
      "rate": 50,
      "amount": 62.50,
      "documented": true,
      "proof": "geo-fence-timestamps + BOL-scan"
    },
    {
      "type": "detention_delivery",
      "hours": 0.5,
      "rate": 50,
      "amount": 25.00,
      "documented": true,
      "proof": "geo-fence-timestamps"
    },
    {
      "type": "lumper",
      "amount": 75.00,
      "documented": true,
      "proof": "lumper-receipt-photo"
    }
  ],
  "total_accessorials": 162.50,
  "total_invoice": 862.50,
  "missing_documentation": []
}
```

---

## Accessorial Dispute Prevention

Best practices built into this skill:
1. **Pre-approve in writing** — all accessorials must be in RC before dispatch
2. **Notify broker in advance** — detention alerts give broker chance to respond
3. **Timestamp everything** — geo-fence is objective proof
4. **Get BOL documentation** — facility-stamped times are strongest evidence
5. **Save all communications** — broker emails/texts agreeing to accessorials

---

## Error Handling

| Situation | Action |
|-----------|--------|
| Lumper receipt lost | Contact lumper company for copy; note in file |
| Broker denies detention was authorized | Provide RC + timestamps; escalate if refused |
| Driver didn't get BOL times stamped | Use geo-fence + text message proof as backup |
| RC has no accessorial clause | Attempt claim citing industry standard; note for future |
| Broker says accessorial not in budget | Accessorials in RC are contractual obligations — insist |
