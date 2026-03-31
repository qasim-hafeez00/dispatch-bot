---
skill_id: "10-load-booking"
name: "Load Booking"
version: "1.0.0"
phase: 9
priority: critical
trigger: "Carrier confirms load via carrier-confirmation-loop skill"
inputs:
  - carrier_mc_number: "string"
  - agreed_rate: "float"
  - locked_accessorials: "object — detention, TONU, layover, lumper"
  - broker_contact: "name, phone, email"
  - carrier_email: "string — for RC delivery"
  - driver_name: "string"
  - driver_phone: "string"
outputs:
  - booking_confirmation: "verbal or written confirmation from broker"
  - carrier_packet_request: "broker sends setup packet to carrier email"
  - load_status: "BOOKED"
integrations:
  - VOIP/phone system
  - TMS
  - Email
depends_on: ["09-carrier-confirmation-loop"]
triggers_next: ["11-carrier-packet-completion"]
tags: [booking, mc-number, broker, confirmation, load-status]
---

# SKILL: Load Booking
## Cortex Bot — Truck Dispatch Automation

**Trigger**: Carrier confirms load via `carrier-confirmation-loop` skill.
**Phase**: 9 (Load Booking)
**Priority**: CRITICAL

---

## Purpose

Formally book the load with the broker, provide all required carrier information, and initiate the paperwork chain. This is the point of no return — once booked, the carrier is committed.

---

## Pre-Booking Checklist

Before saying "Book it" to the broker, verify:

- [ ] Carrier confirmed YES (from `09-carrier-confirmation-loop`)
- [ ] Agreed rate locked (verbal confirmation on record)
- [ ] All accessorials verbally confirmed (detention, TONU, lumper)
- [ ] Carrier MC# on hand
- [ ] Carrier email address for RC delivery confirmed
- [ ] Driver name and cell number ready to provide

---

## Execution Steps

### Step 1 — Formal Booking Statement

Return to broker (resuming from hold):

> "We're good to go — book it. Our MC is **[MC#]**. Please send the carrier packet and rate confirmation to **[carrier email]**. My driver's name is **[Driver First Name]**, cell **[phone number]**. We're confirmed for the **[pickup date/time]** pickup."

**Never book without saying the MC# out loud** — brokers need it to set up the carrier in their system.

### Step 2 — Broker Requires Owner Verification (Edge Case)

Some brokers (especially for new carriers or high-value loads) require:
- Owner's email address to send RC
- A direct call with the owner/carrier principal
- Owner to reply to rate con from their own email

If this happens:
1. Tell broker: "No problem — give me 2 minutes to get my carrier on the line."
2. 3-way call the owner immediately
3. Introduce: "I have [Broker Name] from [Company] on the line — they need to verify your info before sending the rate con."
4. Let owner confirm their details directly
5. Rejoin and confirm booking

### Step 3 — Confirm Paperwork Delivery

Before hanging up:
> "Just to confirm — you'll be sending the carrier packet and rate confirmation to **[email]** within the next **[X] minutes**? And we're looking at a **[pickup date]** pickup at **[time]**. Great — we'll get everything back to you quickly."

Note the time broker says they will send paperwork — set a 20-minute alert if not received.

### Step 4 — TMS Load Record Creation

Create a complete load record:

```json
{
  "load_id": "TMS-[auto-generated]",
  "status": "BOOKED",
  "booked_at": "2026-03-20T08:22:00Z",
  "carrier_id": "CARR-032026-001",
  "carrier_mc": "MC-XXXXXX",
  "driver_name": "John Smith",
  "driver_phone": "+15551234567",
  "broker_company": "Echo Global Logistics",
  "broker_mc": "MC-YYYYYY",
  "broker_contact_name": "Sarah Jones",
  "broker_phone": "+15559876543",
  "broker_email": "sjones@echo.com",
  "rc_delivery_email": "carrier@abctrucking.com",
  "origin_city": "Nashville",
  "origin_state": "TN",
  "origin_address": "123 Shipper Way, Nashville, TN 37201",
  "pickup_date": "2026-03-20",
  "pickup_appointment": "09:00",
  "destination_city": "Atlanta",
  "destination_state": "GA",
  "destination_address": "456 Receiver Blvd, Atlanta, GA 30301",
  "delivery_date": "2026-03-20",
  "delivery_appointment": "17:00",
  "commodity": "Dry goods",
  "weight_lbs": 42000,
  "agreed_rate": 700.00,
  "rate_per_mile": 2.82,
  "detention_free_hours": 2,
  "detention_rate_per_hour": 50,
  "tonu_amount": 150,
  "lumper": "broker_paid",
  "tracking_method": "Macropoint",
  "payment_terms": "Net 30",
  "quick_pay_option": "2% discount for 3-day pay"
}
```

### Step 5 — Notify Carrier

Send booking confirmation to carrier:

```
✅ BOOKED — [Origin City] → [Destination City]

Load #: [TMS Load ID]
Broker: [Broker Company]
PU: [Date] | [Time] | [Address]
DEL: [Date] | [Time] | [Address]
Rate: $[X] all-in

📋 Watch for dispatch sheet shortly.
Keep your phone on — the broker may call to verify.
```

### Step 6 — Set Paperwork Timer

Set alert: if carrier packet not received in email within 20 minutes → call broker to follow up.

---

## Outputs

- Load record created in TMS with status `BOOKED` ✅
- Broker notified of MC# and driver details ✅
- Carrier notified of booking confirmation ✅
- Paperwork receipt timer set ✅
- `11-carrier-packet-completion` skill triggered ✅

---

## Error Handling

| Situation | Action |
|-----------|--------|
| Broker says "load just got covered" | Apologize, log as "lost — covered before booking", move on |
| Broker requires owner call — owner unavailable | Ask broker for 30-min hold; call owner urgently |
| Carrier changes mind after "book it" | Immediate escalation — must find replacement carrier or TONU applies |
| Broker sends RC to wrong email | Call broker immediately, resend to correct address |
| RC not received in 20 min | Call broker AP or load coordinator |
