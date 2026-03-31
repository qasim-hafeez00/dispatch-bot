---
skill_id: "19-payment-reconciliation"
name: "Payment Reconciliation"
version: "1.0.0"
phase: 17
priority: high
trigger: "Invoice submitted (from 17-pod-invoicing-factoring). Runs follow-up loop until payment confirmed received."
inputs:
  - invoice_id: "string"
  - invoice_amount: "float"
  - submission_date: "ISO 8601 date"
  - payment_terms: "Net 15 | Net 30 | Net 45 | Quick-pay N days"
  - broker_ap_contact: "email and phone"
  - factoring_used: "boolean"
  - factoring_company: "string or null"
outputs:
  - payment_status: "enum: PENDING | PAID | OVERDUE | DISPUTED | IN_COLLECTIONS"
  - payment_received_date: "ISO 8601 or null"
  - amount_paid: "float"
  - variance: "float — difference from invoiced amount"
  - dispute_log: "string or null"
integrations:
  - TMS
  - Email/VOIP
  - Factoring company portal
  - Collections platform (if needed)
depends_on: ["17-pod-invoicing-factoring"]
triggers_next: ["25-carrier-performance-scoring (after payment confirmed)"]
tags: [payment, reconciliation, collections, overdue, factoring, invoice-tracking]
---

# SKILL: Payment Reconciliation
## Cortex Bot — Truck Dispatch Automation

**Trigger**: Invoice submitted. Runs until payment is confirmed received in full.
**Phase**: 17
**Priority**: HIGH

---

## Purpose

Track every invoice from submission to full payment. No invoice falls through the cracks. Overdue accounts are escalated systematically. Disputes are resolved with documentation. Cash flow is protected.

---

## Follow-Up Sequence

### Day 0 — Invoice Submitted
- Log: `invoice_submitted_at`, `expected_pay_date` (submission + Net days)
- Set all follow-up reminders in TMS
- If factored: monitor factoring portal for advance confirmation (expect same business day)

### Day -3 Before Due Date — Pre-Due Check
Send email to broker AP:
> Subject: `Payment Check-In — Invoice [INV#] — Due [Date]`
> Body: "Hi, just checking in ahead of payment due date for Invoice [INV#] in the amount of $[X] for load [Ref#] delivered [date]. Please let me know if there are any questions or missing documents. Thank you."

Purpose: Catch any issues (missing BOL, wrong remit-to address) before the due date.

### Due Date — Payment Confirmation
- Check bank account / factoring portal for payment received
- If paid: log `PAID`, amount, date, record any variance
- If not paid: begin escalation sequence

### Due +3 — First Reminder
Email to broker AP (CC broker contact):
> Subject: `REMINDER — Invoice [INV#] — Payment Due [Date] — $[X]`
> Body: "This is a friendly reminder that Invoice [INV#] for $[X] was due on [date]. Please process payment at your earliest convenience or let us know if there are any issues."

### Due +7 — Firm Follow-Up
Email + phone call to broker AP:
> Subject: `OVERDUE — Invoice [INV#] — 7 Days Past Due`
> Body: "Invoice [INV#] for $[X] is now 7 days past due. Please remit payment immediately or contact us to discuss. Supporting documents (RC, signed BOL, POD) are attached for reference."

Call script:
> "Hi, I'm calling about Invoice [INV#] for [Carrier Company] — it was due on [date] and we haven't received payment. Can you tell me the status? Is there an ETA on the check/ACH?"

### Due +14 — Operations Manager Escalation
Email to broker operations manager (skip AP):
> Subject: `URGENT — 14-Day Overdue Invoice — [Carrier Company] / [Load Ref]`
> Body: "We have an invoice that is now 14 days past due. This is for load [Ref#] delivered [date] from [origin] to [destination]. Invoice [INV#] for $[X]. We have submitted all required documentation. Please escalate this for immediate payment or contact me directly at [phone]."

### Due +21 — Formal Demand
Send formal demand letter (PDF on letterhead) via email and certified mail if applicable:
> "DEMAND FOR PAYMENT: This letter serves as formal demand for payment of $[X] owed to [Carrier Company] for freight services rendered on [date]. Payment was due [date]. If payment is not received within 7 business days, we will pursue all available remedies including referral to collections and reporting to TIA and DAT."

### Due +30 — Collections Referral
Options:
1. **Freight broker bond claim**: File against broker's BMC-84 or BMC-85 bond (FMCSA requires brokers to maintain $75,000 bond). File at: https://li-public.fmcsa.dot.gov
2. **TIA Arbitration**: Transportation Intermediaries Association dispute resolution
3. **Collections agency**: Freight-specific collections (BlueGrace, TriumphPay, TAFS)
4. **Small claims court**: For amounts under state threshold (~$5,000–25,000)
5. **DAT bad debt reporting**: Flag broker on DAT to warn other carriers

---

## Dispute Handling

When broker disputes the invoice amount:

### Step 1 — Get Written Dispute
Request broker's dispute reason in writing (email). Common disputes:
- "Short delivery" — driver delivered less than billed
- "Damaged freight" — cargo claim pending
- "Wrong rate" — broker claims different rate than RC
- "Missing documentation" — BOL not received
- "Detention not approved" — broker claims detention wasn't authorized

### Step 2 — Pull Documentation
Match dispute against evidence:
| Dispute | Our Evidence |
|---------|-------------|
| Short delivery | Signed BOL with piece count |
| Wrong rate | Signed Rate Confirmation |
| Missing BOL | Resend BOL + timestamp showing original send |
| Detention not approved | RC detention terms + geo-fence timestamps |
| Damaged freight | Clean BOL (if no noted damage at delivery) |

### Step 3 — Counter-Response
Send formal dispute response with all supporting documents.
Allow broker 5 business days to review and respond.

### Step 4 — Partial Payment Acceptance
If broker offers partial payment:
- If dispute has merit: accept partial, document concession in TMS
- If dispute is unfounded: reject partial, escalate to collections
- Never accept partial payment without written confirmation the balance is resolved

---

## Variance Tracking

When payment received differs from invoice:

```
variance = amount_paid - invoice_amount

if variance == 0:
    status = "PAID IN FULL"
    
elif variance < 0 and abs(variance) < 5:
    status = "PAID — MINOR SHORT (rounding)"
    log and close
    
elif variance < 0:
    status = "SHORT PAID"
    calculate what was deducted
    request explanation from broker
    if quick_pay_discount_applied:
        verify quick_pay_rate matches RC
    else:
        dispute the shortage
        
elif variance > 0:
    status = "OVERPAID"
    notify broker, offer credit or refund
```

---

## Outputs

```json
{
  "invoice_id": "INV-2026-001",
  "load_ref": "ECHO-123456",
  "invoiced_amount": 787.50,
  "payment_status": "PAID",
  "payment_received_date": "2026-04-04",
  "amount_paid": 787.50,
  "variance": 0,
  "days_to_pay": 15,
  "follow_up_actions_taken": ["due-3 check", "due-date confirmation"],
  "notes": null
}
```

---

## Error Handling

| Situation | Action |
|-----------|--------|
| Broker ignores all communications | File bond claim + collections referral |
| Broker went out of business | File FMCSA bond claim immediately |
| Factoring company won't advance — missing doc | Locate document, resubmit within 24 hours |
| Payment sent to wrong account | Trace with broker ACH/wire, request correction |
| Broker claims never received BOL | Resend + provide email timestamp proof |
