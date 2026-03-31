---
skill_id: "02-carrier-onboarding"
name: "Carrier Onboarding"
version: "1.0.0"
phase: 1
priority: critical
trigger: "New carrier submits interest form OR dispatcher manually initiates onboarding for a prospect"
inputs:
  - carrier_contact_info: "name, email, phone, MC#"
  - submitted_documents: "array of uploaded files"
outputs:
  - carrier_record: "TMS/CRM entry"
  - document_vault: "all verified docs filed"
  - carrier_status: "ACTIVE or REJECTED"
integrations:
  - FMCSA SAFER API
  - Document OCR tool
  - TMS/CRM
  - Email/SMS gateway
  - Cloud storage
  - Calendar/alert system
depends_on: ["01-carrier-prospecting"]
triggers_next: ["03-fmcsa-verification", "04-carrier-profile-management"]
tags: [onboarding, compliance, documents, insurance]
---

# SKILL: Carrier Onboarding
## Cortex Bot — Truck Dispatch Automation

**Trigger**: New carrier submits interest form OR dispatcher manually initiates onboarding for a prospect.  
**Phase**: 1 (Carrier Onboarding)  
**Priority**: CRITICAL  

---

## Purpose

Collect all required documents, verify legal compliance, and fully onboard a carrier into the dispatch system so they are ready to be assigned loads. A carrier cannot be dispatched until ALL required documents are verified and stored.

---

## Required Documents Checklist

| Document | Required | Notes |
|----------|----------|-------|
| MC/DOT Authority letter | ✅ Yes | Must be Active on FMCSA |
| W-9 (signed, current year) | ✅ Yes | Required for payment |
| Certificate of Insurance (COI) | ✅ Yes | See limits below |
| Notice of Assignment (NOA) / Factoring letter | If factored | Required if using factoring company |
| CDL copy (driver) | ✅ Yes | Must be valid, correct class |
| Truck registration | ✅ Yes | VIN, year, make, model |
| Trailer registration | If owned | VIN, year, type |
| Signed Dispatch Service Agreement | ✅ Yes | Before first load |
| Payment terms acknowledgement | ✅ Yes | Detention policy, invoice schedule |

### COI Minimum Requirements
- **Auto liability**: $1,000,000 minimum
- **Cargo insurance**: $100,000 minimum (reefer: $100,000 with reefer breakdown)
- **General liability**: $1,000,000 minimum
- **Endorsements**: Motor Truck Cargo, hired/non-owned auto
- Dispatch company MUST be listed as **Certificate Holder**

---

## Execution Steps

### Step 1 — Initial Contact
1. Send welcome email/SMS with secure document upload link
2. Include checklist of required documents
3. Set 48-hour deadline for document submission
4. Assign carrier a temp ID: `CARR-[MMYYYY]-[3-digit seq]`

### Step 2 — Document Collection
1. Monitor upload portal for incoming documents
2. Auto-acknowledge each document received
3. Check document quality (readable, complete, not expired)
4. Flag missing or low-quality documents immediately
5. Send reminder at 24 hours if incomplete

### Step 3 — FMCSA Verification (call FMCSA-VERIFICATION skill)
1. Look up MC number on FMCSA SAFER system: https://safer.fmcsa.dot.gov
2. Verify: Operating Status = ACTIVE
3. Verify: Authority type (Common/Contract/Broker)
4. Check MC age (days since operating authority granted)
5. Check safety rating (Satisfactory / None / Conditional — NOT Unsatisfactory)
6. Check CSA scores (BASICs violations, out-of-service rate)
7. Verify insurance filings on file with FMCSA

### Step 4 — Insurance Verification
1. Call insurance agent directly using phone on COI
2. Confirm policy is active and in force
3. Confirm coverage limits meet requirements
4. Request COI-on-demand email capability (for future broker requests)
5. Note policy expiration date — set 30-day renewal reminder

### Step 5 — Document Storage
1. Create carrier folder: `carriers/[MC#]/`
2. Store all documents with standardized file names:
   - `MC-Authority.pdf`
   - `W9-[Year].pdf`
   - `COI-[InsuranceCarrier]-[ExpDate].pdf`
   - `NOA-[FactoringCompany].pdf`
   - `CDL-[DriverLastName].pdf`
   - `TruckReg-[VIN].pdf`
   - `TrailerReg-[VIN].pdf`
   - `DispatchAgreement-Signed.pdf`
3. Log all expiration dates in compliance calendar

### Step 6 — Profile Creation
1. Create carrier record in TMS/CRM
2. Enter all basic info (MC#, DOT#, company name, owner name, phone, email)
3. Set insurance expiration alerts (30 days, 7 days, 1 day before)
4. Set CDL expiration alerts
5. Trigger `carrier-profile-management` skill to capture preferences

### Step 7 — Communication Setup
1. Confirm preferred communication channels (call/text/WhatsApp/email)
2. Set expected response time window (e.g., within 10 min during business hours)
3. Add driver and owner contact numbers separately
4. Set up dispatch notification preferences

### Step 8 — Welcome & Activation
1. Send welcome confirmation with:
   - Their assigned dispatcher contact
   - How to submit availability each day
   - How loads will be sent for approval
   - Payment timeline expectations
2. Mark carrier status: `ACTIVE`
3. Log activation date

---

## Outputs

- Carrier record created in TMS ✅
- All documents stored and indexed ✅
- Compliance calendar updated ✅
- FMCSA/insurance verified ✅
- Service agreement signed ✅
- Carrier marked ACTIVE ✅

---

## Integration Requirements

- **FMCSA SAFER API**: Real-time authority verification
- **Document OCR tool**: Auto-parse PDF fields (MC#, DOT#, expiry dates)
- **TMS/CRM**: Carrier record storage
- **Email/SMS gateway**: Welcome sequences, reminders
- **Cloud storage**: Document vault per carrier
- **Calendar/alert system**: Expiration reminders

---

## Error Handling

| Error | Action |
|-------|--------|
| MC authority Inactive/Revoked | Reject carrier — do NOT proceed. Notify and explain. |
| Safety rating Unsatisfactory | Reject carrier — do NOT proceed. |
| COI limits below minimum | Request updated COI before proceeding. |
| W-9 missing or unsigned | Cannot process payment — hold activation. |
| Factoring company on broker ban list | Notify carrier — may limit load options. |
| Document expired | Request renewal before activation. |

---

## Quality Gate

**DO NOT activate carrier until ALL of the following are confirmed:**
- [ ] MC Authority = ACTIVE on FMCSA
- [ ] Safety rating ≠ Unsatisfactory
- [ ] COI meets minimum limits
- [ ] All required documents received and readable
- [ ] Dispatch agreement signed
- [ ] W-9 on file
