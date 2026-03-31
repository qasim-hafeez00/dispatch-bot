---
skill_id: "08-broker-negotiation"
name: "Broker Negotiation & Rate Locking"
version: "1.0.0"
phase: 6
priority: critical
trigger: "Eligible load identified by load-triage-eligibility; carrier confirmed available; ready to contact broker"
inputs:
  - load_details: "origin, destination, commodity, weight, dates"
  - carrier_profile: "rate floor, constraints"
  - market_rate: "from rate-market-intelligence skill"
  - anchor_rate: "calculated pre-call"
outputs:
  - agreed_rate: "dollars per mile or flat"
  - locked_accessorials: "detention terms, TONU, layover, lumper"
  - broker_confirmation: "verbal booking confirmed"
  - negotiation_log: "full call record in TMS"
integrations:
  - DAT Rate Analytics API
  - Phone/VOIP (call recording)
  - WhatsApp/SMS API
  - TMS
depends_on: ["06-load-triage-eligibility", "07-rate-market-intelligence"]
triggers_next: ["09-carrier-confirmation-loop"]
tags: [negotiation, rate, broker, accessorials, call-script]
---

# SKILL: Broker Negotiation & Rate Locking
## Cortex Bot — Truck Dispatch Automation

**Trigger**: Eligible load identified by `load-board-search` skill; ready to contact broker.  
**Phase**: 6–7 (Broker Contact + Rate Negotiation)  
**Priority**: CRITICAL  

---

## Purpose

Contact the freight broker by phone or email to gather complete load details and negotiate the best possible all-in rate on behalf of the carrier. Lock all accessorials in writing before committing.

---

## Pre-Call Checklist (run before every call)

- [ ] Carrier confirmed available (HOS + location + appointment window)
- [ ] Current lane rate researched (DAT rate index for this lane)
- [ ] Anchor rate calculated (above market)
- [ ] Target rate (minimum acceptable = carrier rate floor + dispatch margin)
- [ ] Counter-offer prepared
- [ ] All carrier constraints in mind (no touch, no hazmat, etc.)

---

## Rate Research (Do Before Calling)

### Step 1 — Lane Rate Lookup
1. On DAT: look up [origin → destination] average rate (last 7 days, last 30 days)
2. Note: load-to-truck ratio for origin market (tighter = negotiate higher)
3. Note: fuel surcharge per broker (if separate from linehaul)
4. Note: any surge events (holidays, seasonal peaks, weather disruptions)

### Step 2 — Calculate Anchor Rate
```
Market Rate (DAT avg) = $X.XX/mile
Deadhead estimate = [miles] × $0.35/mile
Total deadhead cost = $Y
Loaded miles = [miles]
Market rate total = Market Rate × Loaded miles

Anchor Rate = Market Rate × 1.15 (anchor 15% above market)
Minimum Rate = Carrier rate floor + Dispatch fee
Negotiation range = Minimum Rate → Anchor Rate
```

### Step 3 — Identify Accessorials to Lock
- Detention: when does clock start? (usually 2 hrs free) Rate per hour?
- TONU (Truck Order Not Used): if carrier is sent to PU and load is cancelled
- Layover: if carrier must hold overnight (usually $250–400/day)
- Extra stops: rate per additional stop
- Lumper: is it broker-paid or carrier-paid? Get lumper company name.
- Driver assist: additional pay if carrier must help load/unload

---

## Execution Steps

### Step 1 — Opening the Call

Script:
> "Hi, this is [Dispatcher Name] calling for [Carrier Company] — we have a [equipment type] available in [origin city] on [date]. I'm calling about the [origin → destination] [commodity] load posted on DAT/Truckstop, reference number [REF#]. Do you have a few minutes to go over details?"

### Step 2 — Gather Complete Load Details

**Must get ALL of the following before negotiating:**
- Pickup: Full address, appointment date/time (or FCFS window)
- Delivery: Full address, appointment date/time (or FCFS window)
- Reference numbers (PO#, load#, shipper ref)
- Weight and piece count
- Commodity (exact description)
- Temperature (if reefer): min/max transit temp
- Special requirements: tarps, straps, chains (flatbed), packaging type
- Accessorials: detention policy, TONU amount, layover terms
- Shipper/receiver operating hours
- Load/unload type: drop & hook, live load, live unload, floor-loaded?
- Driver assist required?
- Lumper required? If yes — who pays and how to authorize?
- Tracking requirement: Macropoint, FourKites, Samsara, custom?
- In/out times requirement on BOL?
- POD requirement: immediate photo, signed original, emailed same day?
- Payment terms: Net 15/30/45? Quick-pay option?
- Factoring allowed? Any factoring companies banned?
- MC age requirement?

### Step 3 — Negotiate the Rate

**Opening Move (Anchor High):**
> "Based on the current lane, we're looking at around [$Anchor Rate] all-in for this load. That accounts for current market conditions and the deadhead from [origin]."

**If broker pushes back:**
> "Our carrier has this lane locked in on preferred equipment. What's the best you can do to help us get this covered today?"

**Counter-offer flow:**
1. Start at anchor rate
2. If refused: come down to midpoint between anchor and target
3. If refused: offer target rate (floor)
4. If still refused below floor: **politely decline and move on**

> "I appreciate the offer, but I don't think that's going to work for my carrier given the current market and the deadhead from [origin]. If your rate improves, I'll call you back — but I need to reach out to other loads for now."

**Never go below the carrier's rate floor.** A bad load is worse than no load.

**Rate Anchoring Justifications (use these when anchoring high):**
- "DAT is showing [lane] averaging $X.XX/mile this week"
- "The load-to-truck ratio in [origin] is [tight/high] right now"
- "There's [X] miles of deadhead from our current position"
- "Shipper has a tight appointment window — that's more dwell risk"
- "Reefer fuel adds cost — this lane usually gets a reefer premium"

### Step 4 — Lock Accessorials in Writing

Before confirming: explicitly state and confirm:
> "Let's confirm the accessorials: detention starts at 2 hours free, then $X/hour after. TONU is $X if the load is cancelled after truck is dispatched. Any driver assist is $X extra. All agreed?"

**Get broker to verbally confirm each one — this will be in the Rate Confirmation.**

### Step 5 — Hold and Confirm with Carrier

> "Give me 60 seconds to confirm with my driver."

- Immediately send summary to carrier via WhatsApp/SMS:
  ```
  LOAD OFFER:
  PU: [city], [date/time]
  DEL: [city], [date/time]
  Miles: [X]
  Rate: $[X] all-in
  Commodity: [X]
  Weight: [X] lbs
  Special reqs: [any]
  Detention: starts 2hr, $X/hr
  CONFIRM? Yes/No
  ```
- Wait maximum 90 seconds for response
- If carrier confirms → proceed to booking
- If no response → attempt one call to carrier
- If carrier declines → capture reason, attempt rate adjustment or release

### Step 6 — Close the Booking

> "Great, we're good to go. Book it. Our MC is [MC#]. Please send the carrier packet and rate confirmation to [email]. My driver's name is [Driver Name], cell [phone number]."

---

## Negotiation Tactics Reference

| Tactic | When to Use |
|--------|------------|
| Anchor high (+15%) | Always — start here |
| Lane rate justification | When broker pushes back |
| "Market is tight" | When load-to-truck ratio high |
| Competing load mention | "I have another load I'm evaluating" |
| Time pressure | "My driver needs to roll in X hours" |
| Accessorial leverage | Accept lower rate if detention/TONU is good |
| Walk away | Always be willing to — never desperate |

---

## Email Negotiation (when call not possible)

Subject: `[Equipment] Available [Origin] → [Destination] [Date] — Rate?`

Body:
> We have a [equipment] available in [origin city] on [date] — interested in your [origin → destination] load. Current rate in this lane is running around $X.XX/mile. What's your best rate? Also need to confirm detention terms, TONU, and any driver assist requirements before committing. Please reply or call [phone].

---

## Outputs

- Agreed rate ($/mile or flat)
- All accessorials locked and documented
- Carrier confirmed ✅
- Booking confirmation sent to broker ✅
- Summary logged in TMS ✅

---

## Integration Requirements

- **DAT Rate Analytics API**: Real-time lane rates
- **Phone/VOIP integration**: Call logging, recording
- **WhatsApp/SMS API**: Carrier real-time messaging
- **TMS**: Log negotiation outcome, rate, accessorials

---

## Error Handling

| Situation | Response |
|-----------|---------|
| Broker below rate floor | Politely decline, move to next load |
| Broker won't lock accessorials | Walk away — unwritten accessorials are unenforceable |
| Carrier doesn't respond in 90 sec | Release broker, move to next |
| Broker has low credit score | Require quick-pay or factoring before booking |
| Commodity doesn't match carrier | Stop call immediately — do not accept |
