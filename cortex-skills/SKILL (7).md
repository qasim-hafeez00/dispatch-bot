---
skill_id: "17-pod-invoicing-factoring"
name: "POD Collection, Invoicing & Factoring Submission"
version: "1.0.0"
phase: 15
priority: critical
trigger: "Driver confirms delivery complete (load status = DELIVERED)"
inputs:
  - load_id: "string"
  - driver_contact: "phone/WhatsApp"
  - factoring_company: "string or null"
  - broker_ap_email: "string"
  - approved_accessorials: "detention hours, lumper amount, etc."
outputs:
  - pod_documents: "signed BOL, lumper receipts filed"
  - invoice: "PDF with all line items"
  - submission_confirmation: "factoring or broker receipt"
  - payment_tracking_record: "in TMS"
integrations:
  - WhatsApp/SMS/Email
  - OCR/PDF tool
  - Invoicing engine
  - Factoring company portal/API
  - TMS
  - Email
depends_on: ["15-in-transit-monitoring", "16-detention-layover-management"]
triggers_next: ["19-payment-reconciliation"]
tags: [pod, bol, invoice, factoring, payment, documents]
---
# SKILL: POD Collection, Invoicing & Factoring Submission
## Cortex Bot — Truck Dispatch Automation

**Trigger**: Driver confirms delivery complete (load status = DELIVERED).  
**Phase**: 15–16 (Document Collection + Invoicing & Factoring)  
**Priority**: CRITICAL  

---

## Purpose

Collect all proof-of-delivery documents, generate the invoice, and submit to broker or factoring company the same day as delivery for fastest possible payment.

---

## Time Sensitivity

Most factoring companies require same-day submission for advances.  
Brokers may have invoice submission deadlines (some require within 72 hours).  
**Every hour of delay = delayed cash flow for the carrier.**

---

## Phase 1: Document Collection

### Step 1 — Driver Document Request (Immediately After Delivery Confirmed)

Send driver:
> "🎉 Load delivered! Now I need documents ASAP to get you paid fast.
> Please send me clear photos/scans of:
> 1. Signed BOL (both pickup AND delivery copies)
> 2. All pages of BOL with in/out timestamps visible
> 3. Lumper receipt (if applicable)
> 4. Any additional paperwork the receiver gave you
> Send photos RIGHT NOW — don't wait until morning!"

### Step 2 — Document Quality Check

When documents received:
- [ ] BOL is signed by receiver (not just driver signature)
- [ ] BOL has delivery date visible
- [ ] In/out times are written or stamped (if required by RC)
- [ ] All pages are readable (no blurry, cut-off, dark photos)
- [ ] Piece count on BOL matches RC
- [ ] Lumper receipt legible and shows amount paid (if applicable)
- [ ] No "subject to count" or "exceptions" noted without documentation

If quality issues:
- Request retake from driver immediately
- If driver has already left: contact receiver to get stamped copy faxed/emailed

### Step 3 — File Documents

Save with standardized names in load folder `loads/[LoadRef]/`:
- `BOL-[LoadRef]-[Pickup]-SIGNED.pdf`
- `BOL-[LoadRef]-[Delivery]-SIGNED.pdf`
- `LUMPER-[LoadRef]-[Amount].pdf`
- Any additional docs

### Step 4 — Update TMS

- Load status: `DELIVERED — DOCS RECEIVED`
- Log delivery timestamp
- Log any exceptions or notes (damaged freight, short shipment, etc.)
- Calculate final accessorials owed:
  - Detention at pickup: [hours] × [rate] = $[X]
  - Detention at delivery: [hours] × [rate] = $[X]
  - Lumper reimbursement: $[X]
  - Extra stop payments: $[X]

---

## Phase 2: Invoice Generation

### Step 1 — Build Invoice

Auto-generate invoice with:

```
INVOICE

From: [Carrier Company Name]
       [Address]
       MC#: [X] | EIN/SSN: [from W-9]

To:   [Broker Company Name]
      [Broker Address]

Invoice #: INV-[LoadRef]-[Date]
Invoice Date: [Today]
Due Date: [Today + Net Days per RC]

LOAD DETAILS:
Origin: [Pickup City, State]
Destination: [Delivery City, State]
Pickup Date: [Date]
Delivery Date: [Date]
Reference #: [Broker Load Ref]
Commodity: [X]

LINE ITEMS:
Line Haul:                          $[Rate]
Detention - Pickup ([X] hrs):        $[Amount]
Detention - Delivery ([X] hrs):      $[Amount]
Lumper Reimbursement:               $[Amount]
Extra Stop ([stop]):                 $[Amount]
Driver Assist:                       $[Amount]
TONU (if applicable):               $[Amount]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TOTAL DUE:                          $[TOTAL]

Payment terms: Net [X] days
Quick-pay option: [X]% discount for payment within [X] days

ATTACHMENTS:
✓ Rate Confirmation (signed)
✓ Bill of Lading (signed)
✓ Lumper Receipt (if applicable)
```

### Step 2 — Review Invoice

- [ ] All line items match RC + actual events
- [ ] Total is correct
- [ ] All attachments present
- [ ] Invoice number is unique
- [ ] Due date correctly calculated from payment terms

---

## Phase 3: Submission

### If Carrier Uses Factoring Company

1. Log into factoring portal OR send to factoring company's submission email
2. Submit package:
   - Invoice (PDF)
   - Signed RC
   - Signed BOL
   - Lumper receipt (if applicable)
3. Note submission timestamp in TMS
4. Await factoring advance confirmation (usually same business day)
5. Track: factoring company will collect from broker

**Common factoring companies and submission methods:**
- OTR Capital: portal submission
- RTS Financial: portal + email
- Triumph Business Capital: portal
- Apex Capital: portal + email
- TCI: email submission

### If Carrier Does NOT Use Factoring (Direct Pay)

1. Email invoice package directly to broker's AP department
2. Subject line: `INVOICE — [Load Ref] — [Origin] to [Destination] — [Delivery Date]`
3. Body: brief note confirming delivery + invoice attached
4. CC the broker contact from the load
5. Note submission timestamp in TMS
6. Set payment follow-up reminder per payment terms

---

## Phase 4: Payment Tracking (Trigger `payment-reconciliation` skill)

1. Log expected payment date in TMS
2. Set automated follow-up reminders:
   - 3 days before due: verify no issues with broker
   - On due date: confirm payment received or in transit
   - 3 days past due: send polite reminder
   - 7 days past due: firm follow-up
   - 14 days past due: escalate to collections process

---

## Outputs

- All documents collected and filed ✅
- Invoice generated and submitted ✅
- TMS load closed with all financial details ✅
- Payment tracking activated ✅
- Load status: `INVOICED — AWAITING PAYMENT` ✅

---

## Integration Requirements

- **WhatsApp/SMS/Email**: Driver document request and receipt
- **OCR/PDF tool**: Document quality check, data extraction
- **Invoicing engine**: Auto-generate invoice from TMS data
- **Factoring company API/portal**: Submission
- **TMS**: Full financial tracking
- **Email**: Direct broker submission
- **Calendar/reminder system**: Payment follow-up scheduling

---

## Error Handling

| Situation | Action |
|-----------|--------|
| Driver lost BOL | Request from broker or receiver; note in file |
| BOL not signed by receiver | Contact receiver for signed copy ASAP |
| Broker disputes detention | Provide timestamp proof from geo-fence + BOL |
| Factoring rejects for missing doc | Locate and resubmit within 24 hours |
| Invoice not paid at due date | Trigger payment-reconciliation skill immediately |
| Broker claims short delivery | Pull BOL weight, request proof, dispute if incorrect |
