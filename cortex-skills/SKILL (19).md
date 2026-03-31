---
skill_id: "09-carrier-confirmation-loop"
name: "Carrier Confirmation Loop"
version: "1.0.0"
phase: 8
priority: critical
trigger: "Broker has verbally agreed to a rate; carrier must confirm before booking is finalized"
inputs:
  - load_summary: "origin, destination, rate, commodity, weight, times, special requirements"
  - carrier_contact: "phone, WhatsApp, preferred channel"
  - broker_hold_start: "timestamp when broker was put on hold"
outputs:
  - carrier_decision: "enum: CONFIRMED | REJECTED | TIMEOUT"
  - rejection_reason: "string if rejected"
  - confirmation_timestamp: "ISO 8601"
integrations:
  - WhatsApp Business API
  - SMS gateway
  - VOIP/phone system
  - TMS
depends_on: ["08-broker-negotiation"]
triggers_next: ["10-load-booking (if CONFIRMED)", "08-broker-negotiation (retry next load if REJECTED)"]
tags: [confirmation, carrier, approval, 90-second-loop, real-time]
---

# SKILL: Carrier Confirmation Loop
## Cortex Bot — Truck Dispatch Automation

**Trigger**: Broker verbally agrees to a rate; need carrier confirmation before finalizing the booking.
**Phase**: 8
**Priority**: CRITICAL

---

## Purpose

Get the carrier's fast YES or NO while the broker is on hold. This is the most time-critical step in the entire dispatch process. The broker expects an answer in under 90 seconds. A slow loop burns broker goodwill and loses loads to competing dispatchers.

---

## The 90-Second Rule

**Maximum time broker stays on hold: 90 seconds.**

After 90 seconds, either:
- Tell broker "We're confirmed — book it." (carrier said yes)
- Tell broker "Let me keep searching — I need to check one more thing." (buy time)
- Release the load entirely and move to next

---

## Execution Steps

### Step 1 — Compose Load Summary (Pre-written before calling broker)

Prepare this message BEFORE calling the broker, so it's ready to send instantly:

```
LOAD OFFER — Reply YES or NO:

🚛 PU: [City, State] | [Date] [Time window]
📦 DEL: [City, State] | [Date] [Time window]
📏 Miles: [X] loaded | [X] deadhead
💰 Rate: $[X] all-in ($[X.XX]/mi)
📦 Commodity: [X]
⚖️ Weight: [X] lbs
🌡️ Temp: [X°F] (reefer only)
⚠️ Notes: [any special reqs — touch, driver assist, tracking, etc.]
🏦 Detention: 2 hrs free then $[X]/hr

REPLY NOW — broker waiting ⏱️
```

### Step 2 — Trigger Immediately When Broker Confirms Rate

The moment broker says "I can do $X":
1. Say: "Perfect, give me 60 seconds to confirm with my driver."
2. Immediately send prepared load summary to carrier via:
   - **WhatsApp** (primary — fastest read receipts)
   - **SMS** (simultaneously as backup)
3. Start 90-second timer

### Step 3 — Monitor for Response

**Seconds 0–45:**
- Watch for carrier response via WhatsApp/SMS
- If carrier replies "YES" or "confirmed" → immediately go to Step 4 (book it)
- If carrier replies "NO" → go to Step 5 (rejection handling)

**Seconds 45–75 (no response yet):**
- Auto-call carrier cell phone
- Ring 3 times
- If answers → quickly confirm: "Did you see my message about the [origin → destination] load at $X? Can you run it?"
- If voicemail → leave 10-second message: "It's [dispatcher], calling about a [origin → destination] load at $X. Reply yes or no NOW — broker on hold."

**Seconds 75–90 (still no response):**
- Tell broker: "My driver just sent me a quick message — give me 30 more seconds."
- Attempt one more call/missed call to carrier
- Also call owner (if different from driver)

**After 90 seconds (no response):**
- Apologize to broker, ask if they can hold 2 more minutes
- If broker releases → log the load as "lost — carrier non-responsive"
- Update carrier profile: +1 non-responsive event (affects reliability score)

### Step 4 — Carrier Confirmed

1. Tell broker: "We're confirmed! Book it. Our MC is [MC#]. Please send the carrier packet and rate confirmation to [email]. Driver is [Name], cell [phone]."
2. Log in TMS: CARRIER_CONFIRMED, timestamp, rate, all load details
3. Trigger `10-load-booking` skill
4. Send carrier confirmation:
   > "✅ BOOKED! Load is confirmed. Dispatch sheet coming shortly — watch for it."

### Step 5 — Carrier Rejection Handling

When carrier says NO:

**Step 5a — Capture Rejection Reason**

| Rejection Type | Code | Action |
|---------------|------|--------|
| Rate too low | RATE | Try to negotiate higher with broker; if floor hit → release |
| Timing doesn't work | TIMING | Try to adjust appointment; if inflexible → release |
| Commodity concern | COMMODITY | Cannot fix — release immediately |
| Equipment issue | EQUIPMENT | Cannot fix — release; check other carriers |
| Personal/availability | PERSONAL | Cannot fix — release; note in profile |
| No reason given | UNKNOWN | Release; note in profile |

**Step 5b — Quick Solve Attempt (max 30 seconds)**

If RATE rejection:
- Tell broker: "My carrier needs $[+$0.10/mile or +$50 flat] to make this work. Can you do that?"
- If broker agrees → loop carrier again
- If broker refuses → release load

If TIMING rejection:
- Ask broker: "Can the appointment be moved to [2 hours later]?"
- If yes → loop carrier again
- If no → release

**Step 5c — Release Protocol**

> "Thanks for your time — I need to keep working other loads but I'll circle back if anything changes on our end. Good luck getting it covered."

Log: LOAD_RELEASED, reason, timestamp

### Step 6 — Rejection Pattern Tracking

After 3 rejections from same carrier:
- Flag for profile review
- Is rate floor too high for current market?
- Are they only available for very specific lanes?
- Update carrier reliability score

---

## Response Keywords (Auto-Detect)

Configure WhatsApp/SMS to auto-detect:
- **YES**: "yes", "confirmed", "ok", "go", "good", "approved", "yep", "yeah", "do it", "sounds good"
- **NO**: "no", "cant", "can't", "won't", "not gonna", "too low", "pass", "skip", "negative"
- **QUESTION**: "what", "when", "where", "how", "?" (pause — driver wants clarification)

If QUESTION detected → immediately call driver to answer verbally (don't text back and forth — too slow).

---

## Outputs

```json
{
  "carrier_id": "CARR-032026-001",
  "load_id": "DAT-8821234",
  "decision": "CONFIRMED",
  "confirmed_at": "2026-03-20T08:15:43Z",
  "response_time_seconds": 38,
  "channel_used": "WhatsApp",
  "rejection_reason": null
}
```

---

## Error Handling

| Situation | Action |
|-----------|--------|
| WhatsApp fails to deliver | Fall back to SMS immediately |
| Both message channels fail | Call immediately |
| All contact attempts fail | Release load; mark carrier non-responsive |
| Carrier asks question during hold | Call to answer verbally; no texting back and forth |
