---
skill_id: "12-rate-confirmation-review"
name: "Rate Confirmation Review"
version: "1.0.0"
phase: 11
priority: critical
trigger: "Rate Confirmation PDF received from broker via email after load booking"
inputs:
  - rate_confirmation_pdf: "PDF from broker"
  - tms_negotiation_record: "what was verbally agreed"
  - carrier_profile: "MC#, equipment, requirements"
outputs:
  - review_result: "enum: APPROVED | DISCREPANCY_FOUND"
  - signed_rc: "PDF signed and returned"
  - discrepancy_log: "any fields that did not match"
integrations:
  - Document OCR/parser
  - TMS
  - Email
  - E-signature tool (DocuSign)
  - Cloud storage
depends_on: ["10-load-booking", "11-carrier-packet-completion"]
triggers_next: ["13-driver-dispatch"]
tags: [rate-confirmation, verification, signing, paperwork]
---
# SKILL: Rate Confirmation Review
## Cortex Bot — Truck Dispatch Automation

**Trigger**: Rate Confirmation (RC) document received from broker via email.  
**Phase**: 11 (Rate Confirmation Review)  
**Priority**: CRITICAL  

---

## Purpose

Systematically verify every field on the Rate Confirmation against what was verbally agreed before signing. Catching errors here prevents disputes, short payments, and liability issues. **NEVER sign an RC without completing this full review.**

---

## Inputs

- Rate Confirmation PDF from broker
- TMS record from negotiation (what was verbally agreed)
- Carrier profile (equipment, requirements)

---

## Execution Steps

### Step 1 — Auto-Parse the RC

Use document OCR to extract:
- Shipper/broker company name and MC#
- Load reference number(s)
- Pickup address, date, time, appointment window
- Delivery address, date, time, appointment window
- Commodity description
- Weight and piece count
- Equipment type required
- Carrier name and MC number
- Rate (per mile OR flat)
- Accessorials listed (detention, TONU, extra stops, driver assist, lumper)
- Tracking requirements
- Payment terms (Net days, quick-pay option)
- Factoring assignment clause
- Special instructions

### Step 2 — Field-by-Field Verification

Run each field against the TMS record from negotiation:

#### Identity Checks
- [ ] **Our MC#**: Correct MC number for this carrier (not someone else's)
- [ ] **Broker MC#**: Matches the broker we negotiated with
- [ ] **Carrier legal name**: Matches carrier's FMCSA registration exactly

#### Location & Timing Checks
- [ ] **Pickup address**: Full address matches what broker stated on call
- [ ] **Pickup date**: Correct date
- [ ] **Pickup appointment time**: Matches agreed window (FCFS or appointment)
- [ ] **Delivery address**: Full address matches
- [ ] **Delivery date**: Correct date
- [ ] **Delivery appointment time**: Matches agreed window

#### Load Specification Checks
- [ ] **Commodity**: Matches exactly — discrepancies = wrong load or liability exposure
- [ ] **Weight**: Within carrier's legal limit AND matches what broker quoted
- [ ] **Piece count**: Matches
- [ ] **Equipment type**: Correct (53' van, reefer, flatbed, etc.)
- [ ] **Temperature requirement** (reefer): Min/max temp matches verbally agreed

#### Rate Checks
- [ ] **Line-haul rate**: Matches agreed rate exactly ($/mile × miles OR flat amount)
- [ ] **Rate total**: Calculate manually — does rate × miles = quoted total?
- [ ] **Fuel surcharge**: Is it included in rate or separate? Matches what was agreed?
- [ ] **No unauthorized deductions**: Watch for broker fees buried in rate section

#### Accessorial Checks (CRITICAL — most common source of disputes)
- [ ] **Detention**: Start time (after 2 hrs free?), rate per hour — matches verbal agreement
- [ ] **TONU (Truck Order Not Used)**: Amount listed? Trigger conditions clear?
- [ ] **Layover**: Rate per night if applicable
- [ ] **Extra stops**: Payment per stop listed if multi-stop load
- [ ] **Driver assist**: Payment amount listed if required
- [ ] **Lumper**: Who pays? How to authorize? Contact number?

#### Compliance & Administrative Checks
- [ ] **Tracking requirement**: Macropoint, FourKites, etc. — is our carrier enrolled?
- [ ] **BOL in/out time requirement**: Is driver required to get timestamps on BOL?
- [ ] **POD requirement**: When and how must POD be submitted?
- [ ] **Invoice instructions**: Where to send invoice? What attachments required?
- [ ] **Payment terms**: Net days matches agreed? Quick-pay % listed?
- [ ] **Factoring**: Is factoring assignment clause included? Factoring company listed correctly?

#### Red Flags (Stop — Do Not Sign)
- [ ] ⛔ MC# on RC doesn't match carrier's MC# → wrong carrier on RC
- [ ] ⛔ Rate lower than verbally agreed → call broker to correct before signing
- [ ] ⛔ Accessorials missing entirely → call broker — must be added before signing
- [ ] ⛔ "All-in" language removing accessorial rights → do not sign as-is
- [ ] ⛔ Double brokering language or unusual assignment clauses → legal review
- [ ] ⛔ Payment terms longer than agreed → call broker

### Step 3 — Discrepancy Resolution

If any field doesn't match:
1. Call broker immediately: "I'm reviewing the rate con and I'm seeing [X] instead of the [Y] we agreed on. Can you have an updated RC sent?"
2. Document the discrepancy in TMS
3. **Do not sign until corrected** unless it is a minor typo (e.g., address has extra space but GPS confirms same location)

### Step 4 — Sign and Return

Once all fields verified:
1. Apply electronic signature
2. Return to broker via reply email (reply to same thread for documentation)
3. Note: "RC [load#] signed and returned — confirming [PU date/time] pickup and [$X] all-in rate with [detention terms]."
4. Save signed copy as: `RC-[BrokerName]-[LoadRef]-[Date]-SIGNED.pdf`
5. File in load folder: `loads/[LoadRef]/`

### Step 5 — TMS Update

Update load record with:
- RC received and signed timestamp
- All verified fields
- Load status: `RC SIGNED — DISPATCHING`
- Trigger `driver-dispatch` skill

---

## Common Broker RC Errors to Watch For

| Error | How It Appears | Risk |
|-------|---------------|------|
| Wrong rate | Lower $/mile or wrong flat total | Short payment |
| Missing detention terms | No detention clause at all | Can't claim detention |
| Wrong appointment time | AM vs PM error | Carrier misses appointment |
| Missing TONU | Not listed after verbal agreement | Unpaid if load cancelled |
| Wrong commodity | Different description | Wrong equipment / liability |
| Wrong pickup address | Similar but different city | Carrier goes to wrong location |
| Factoring not assigned | Factoring clause missing | Payment goes to broker, not factor |

---

## Outputs

- RC verified and signed ✅
- Filed in load folder ✅
- TMS load record updated ✅
- `driver-dispatch` skill triggered ✅
- OR: Discrepancy flagged and broker contacted for correction ✅

---

## Integration Requirements

- **Document OCR/parser**: Extract all fields from PDF
- **TMS**: Compare against negotiation record
- **Email**: Receive and send RC documents
- **E-signature tool**: DocuSign or equivalent
- **Cloud storage**: File signed RC in load folder
