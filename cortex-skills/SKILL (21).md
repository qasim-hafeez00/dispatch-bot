---
skill_id: "11-carrier-packet-completion"
name: "Carrier Packet Completion"
version: "1.0.0"
phase: 10
priority: critical
trigger: "Carrier setup packet received from broker via email after load booking"
inputs:
  - broker_packet_email: "raw email with PDF or web form link"
  - carrier_profile: "all carrier details from TMS"
  - carrier_documents: "MC auth, W-9, COI, NOA — pre-stored in vault"
outputs:
  - completed_packet: "filled and returned to broker"
  - submission_confirmation: "broker confirms receipt"
  - rate_con_request: "broker sends Rate Confirmation"
integrations:
  - Email
  - Document OCR/parser
  - PDF form-filler
  - Cloud document vault
  - TMS
depends_on: ["10-load-booking"]
triggers_next: ["12-rate-confirmation-review"]
tags: [carrier-packet, setup, documents, broker, onboarding-per-load]
---

# SKILL: Carrier Packet Completion
## Cortex Bot — Truck Dispatch Automation

**Trigger**: Carrier setup packet received from broker via email.
**Phase**: 10 (Carrier Packet & Setup)
**Priority**: CRITICAL

---

## Purpose

Complete and return the broker's carrier setup packet as fast as possible — ideally within 15 minutes of receipt. Slow packet return delays the Rate Confirmation, which delays dispatch, which delays revenue.

---

## What Is a Carrier Packet?

Every broker requires carriers to complete a setup packet before the first load (and sometimes every load with new brokers). It typically includes:

- Carrier contact and MC/DOT information form
- W-9 (or W-9 request)
- Certificate of Insurance (COI) — often specific endorsements required
- NOA (Notice of Assignment) if carrier uses factoring
- Signature on broker's terms and conditions
- Payment method / remit-to address

Some brokers use PDF forms. Others use web portals (Carrier411, MyCarrierPackets, SaferWatch, Highway, Carrier Source).

---

## Execution Steps

### Step 1 — Detect Packet Type

When email arrives:
1. Check if email contains a PDF attachment → use PDF form-filler
2. Check if email contains a web link (MyCarrierPackets, Highway, etc.) → use web form automation
3. Check if email is just a text request → compose reply with documents attached

### Step 2 — Auto-Fill Standard Fields

Pull from carrier profile in TMS and auto-populate:

| Field | Source |
|-------|--------|
| Legal company name | Carrier TMS record |
| DBA name | Carrier TMS record |
| MC number | Carrier TMS record |
| DOT number | Carrier TMS record |
| Physical address | Carrier TMS record |
| Mailing/remit-to address | Carrier TMS record (or factoring company address) |
| Federal Tax ID (EIN) | From W-9 on file |
| Owner name | Carrier TMS record |
| Owner email | Carrier TMS record |
| Owner phone | Carrier TMS record |
| Dispatcher contact | Dispatch service contact info |
| Equipment type | Carrier profile |
| Insurance company | From COI on file |
| Insurance policy number | From COI on file |
| Insurance agent name | Carrier TMS record |
| Insurance agent phone | Carrier TMS record |
| Factoring company name | NOA on file (if applicable) |
| Factoring remit-to address | NOA on file |
| Payment terms preference | Carrier profile |

### Step 3 — Attach Required Documents

From the carrier's document vault, retrieve and attach:

- [ ] **W-9**: Current year, signed
- [ ] **COI**: Most recent certificate showing all required coverages
  - Verify COI is not expired before attaching
  - Verify broker is listed as certificate holder (some brokers require this)
- [ ] **NOA**: If carrier uses factoring — include factoring company's remit-to letter
- [ ] **MC Authority letter**: Some brokers require the operating authority printout

### Step 4 — Terms & Conditions Review

Some brokers include T&C language that must be reviewed before signing:

**Flag and DO NOT sign without review if T&C contains:**
- "All-in rate" language that waives right to claim accessorials
- Claim filing deadlines shorter than 30 days
- Mandatory arbitration in unusual jurisdictions
- Cargo liability waiver language
- Double brokering definitions that could misclassify the transaction

**Standard T&C (safe to sign):**
- Standard payment terms (Net 15/30/45)
- Standard cargo liability per the RC
- Standard check-in and check-call requirements
- Standard POD submission requirements

### Step 5 — Return Packet

**Via email:**
Subject: `Carrier Packet — [Carrier Company] — MC [MC#] — Load [Ref#]`
Body: "Please find our completed carrier packet attached. Please send the Rate Confirmation to [email] as soon as possible so we can dispatch."

**Via web portal:**
- Complete all fields, upload documents, submit
- Screenshot confirmation page
- Save confirmation number in TMS

### Step 6 — Confirm Receipt and Request RC

After sending:
- Wait 10 minutes
- If no RC received → reply: "Just following up — did you receive our carrier packet? We're ready to dispatch and just need the rate confirmation."
- If still no response after 20 minutes → call broker directly

### Step 7 — Log in TMS

```
packet_sent_at: [timestamp]
packet_method: email | portal
documents_attached: [W-9, COI, NOA]
broker_portal: [portal name if applicable]
submission_confirmation: [yes/no + confirmation number]
rc_requested: [yes]
```

---

## Common Broker Portals

| Portal | URL | Notes |
|--------|-----|-------|
| MyCarrierPackets | mycarrierpackets.com | Most common — full automation possible |
| Highway | usehighway.com | Identity verification focus |
| Carrier411 | carrier411.com | Safety + setup combined |
| SaferWatch | saferwatch.com | Insurance verification focus |
| Rmis | rmis.com | Used by large brokers |
| Assure Assist | assureassist.com | |

For each portal: maintain pre-loaded carrier profiles so fields auto-populate on submission.

---

## Outputs

- Broker packet completed and returned within 15 minutes ✅
- All required documents attached ✅
- RC requested ✅
- TMS updated with submission details ✅
- `12-rate-confirmation-review` skill on standby for incoming RC ✅

---

## Error Handling

| Situation | Action |
|-----------|--------|
| COI expired before packet sent | Emergency — contact carrier's insurance agent for updated COI NOW |
| Broker portal won't accept our COI limits | Call broker — may need updated COI with higher limits |
| W-9 missing from vault | Call carrier for new W-9 immediately — cannot pay without it |
| Broker T&C has waiver of accessorials | Do NOT sign — call broker to negotiate or walk away |
| Portal account not set up | Create account, then complete packet |
| Broker requires owner's personal signature | 3-way loop with owner — get signature electronically |
