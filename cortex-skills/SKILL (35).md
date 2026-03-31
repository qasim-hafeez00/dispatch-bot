---
skill_id: "26-compliance-monitoring"
name: "Compliance Monitoring"
version: "1.0.0"
phase: 0
priority: high
trigger: "Daily automated check at 06:00 AM for all active carriers. Also triggered by any document expiry event."
inputs:
  - all_active_carriers: "array of carrier IDs"
  - document_vault: "all carrier documents with expiry dates"
  - eld_feed: "daily HOS summary per driver"
outputs:
  - compliance_dashboard: "status of all carriers"
  - expiry_alerts: "documents expiring within alert windows"
  - suspended_carriers: "carriers who must not be dispatched"
  - ifta_reports: "quarterly fuel tax summaries"
integrations:
  - TMS/document vault
  - FMCSA SAFER API (monthly recheck)
  - ELD API
  - Email/SMS (carrier alerts)
  - Calendar/alert system
depends_on: []
triggers_next: ["02-carrier-onboarding (renewal)", "20-tms-crm-logging"]
tags: [compliance, coi, cdl, ifta, eld, dot, expiry, suspension]
---

# SKILL: Compliance Monitoring
## Cortex Bot — Truck Dispatch Automation

**Trigger**: Daily at 06:00 AM. Also triggered by any expiry event.
**Phase**: All (always running)
**Priority**: HIGH

---

## Purpose

A carrier with lapsed insurance, expired CDL, or revoked authority cannot legally haul freight. Dispatching such a carrier creates criminal and civil liability for the dispatcher. This skill ensures every carrier is always in full compliance before every single load.

---

## Document Expiry Monitoring

### Alert Windows Per Document

| Document | 90-Day Alert | 30-Day Alert | 7-Day Alert | Day-Of Hard Stop |
|----------|-------------|-------------|-------------|-----------------|
| COI — Auto Liability | — | ✅ | ✅ | ✅ Suspend carrier |
| COI — Cargo | — | ✅ | ✅ | ✅ Suspend carrier |
| COI — General Liability | — | ✅ | ✅ | ✅ Suspend carrier |
| CDL | ✅ | ✅ | ✅ | ✅ Suspend driver |
| Medical Certificate | ✅ | ✅ | ✅ | ✅ Suspend driver |
| Hazmat Endorsement | ✅ | ✅ | ✅ | ✅ Remove hazmat flag |
| TWIC Card | ✅ | ✅ | ✅ | ✅ Remove TWIC flag |
| DOT Registration | — | ✅ | ✅ | ✅ Flag |
| IFTA License | — | ✅ | ✅ | ✅ Flag |
| Annual DOT Inspection | ✅ | ✅ | ✅ | ✅ Suspend vehicle |
| MC Authority | Monthly FMCSA check | — | — | ✅ Suspend carrier |

---

## Execution Steps

### Step 1 — Daily Compliance Sweep (06:00 AM)

For every active carrier:
```python
for carrier in active_carriers:
    for document in carrier.documents:
        days_until_expiry = (document.expiry_date - today).days
        
        if days_until_expiry <= 0:
            suspend_carrier(carrier, document)
            alert_CRITICAL(carrier, document)
            
        elif days_until_expiry <= 7:
            alert_URGENT(carrier, document)
            
        elif days_until_expiry <= 30:
            alert_WARNING(carrier, document)
            
        elif days_until_expiry <= 90 and document.type in HIGH_RISK_DOCS:
            alert_INFO(carrier, document)
```

### Step 2 — Alert Communications

**90-Day Alert (INFO):**
Message to carrier:
> "📋 Compliance Reminder: Your [CDL/Medical Certificate/Hazmat Endorsement] expires in 90 days on [date]. Please schedule your renewal now to avoid any disruption to dispatching."

**30-Day Alert (WARNING):**
> "⚠️ Document Expiring Soon: Your [document] expires in 30 days on [date]. Please provide the updated document as soon as it's renewed. Without it, we cannot dispatch loads after [expiry date]."

**7-Day Alert (URGENT):**
> "🚨 URGENT — Document Expiring: Your [document] expires in 7 days on [date]. We MUST have the updated document before [expiry date]. Please send your renewal immediately to [email]. If you haven't renewed yet, please do so TODAY."

**Day-Of Hard Stop:**
> "🛑 DISPATCHING SUSPENDED: Your [document] has expired as of today. We cannot dispatch any loads until you provide proof of renewal. Please send updated [document] to [email] immediately. No loads will be booked until compliance is restored."

Simultaneously:
- Set carrier status to SUSPENDED in TMS
- Cancel any pending load searches for this carrier
- Notify any brokers with pending loads that carrier is temporarily unavailable

### Step 3 — FMCSA Monthly Recheck

On the 1st of every month, re-run `03-fmcsa-verification` for all active carriers:
- Confirm MC authority still ACTIVE
- Check for new safety rating changes
- Check for new CSA score flags
- Check for insurance filing updates

If any status has changed:
- Log change in TMS with timestamp
- If negative change (Inactive authority, Unsatisfactory rating) → suspend immediately

### Step 4 — ELD Compliance Daily Check

Pull from ELD API daily:
- Any HOS violations from prior day
- Any ELD malfunctions or data gaps
- Any unassigned driving segments

For each violation:
1. Log in TMS: HOS_VIOLATION event
2. Notify carrier and owner
3. Review if violation was due to dispatch assignment — if yes, adjust future planning
4. Repeated violations → formal warning + profile flag

### Step 5 — IFTA Quarterly Reporting

Run quarterly (Jan 31, Apr 30, Jul 31, Oct 31):

1. Pull all state-by-state mileage from ELD/GPS for the quarter
2. Pull all fuel purchases from fuel card data
3. Calculate per-state tax owed or refund due
4. Generate IFTA report PDF
5. Send to carrier with filing instructions 30 days before deadline
6. Confirm carrier filed by deadline

```
IFTA SUMMARY — Q1 2026 (Jan–Mar)

Carrier: ABC Trucking | IFTA License: TN-XXXXXX

State Mileage Summary:
  Tennessee: 8,420 miles
  Georgia: 4,250 miles
  Alabama: 1,890 miles
  Florida: 2,100 miles
  North Carolina: 1,340 miles
  Total: 18,000 miles

Fuel Purchased by State:
  Tennessee: 450 gallons @ avg $3.72 = $1,674
  Georgia: 180 gallons @ avg $3.58 = $644
  
IFTA Tax Calculation:
  [State-by-state breakdown]
  Net tax owed: $284.50
  Filing due: April 30, 2026
```

### Step 6 — Annual DOT Inspection Tracking

Monitor each vehicle's annual inspection date:
- Pull from carrier profile (last inspection date + 12 months = due date)
- 90 days out: remind carrier to schedule inspection
- 30 days out: confirm inspection scheduled
- 7 days out: confirm inspection completed and sticker obtained
- Day-of expiry without renewal: flag vehicle — cannot dispatch until inspected

---

## Compliance Dashboard Output

```
DAILY COMPLIANCE REPORT — March 20, 2026

✅ FULLY COMPLIANT: 14 carriers
⚠️  ACTION NEEDED: 3 carriers
🛑 SUSPENDED: 1 carrier

ACTION NEEDED:
• Smith Trucking (MC-111222): COI expires in 18 days (Apr 7)
• Jones Freight (MC-333444): CDL expires in 45 days (May 4)
• Williams Transport (MC-555666): Annual inspection due in 25 days (Apr 14)

SUSPENDED:
• Garcia Logistics (MC-777888): COI expired March 18 — awaiting renewal
  [Last contacted: March 19, 2026 — carrier says renewal in progress]

ACTION REQUIRED: Obtain updated COI from Garcia before resuming dispatch.
```

---

## Error Handling

| Situation | Action |
|-----------|--------|
| Carrier provides renewal doc with errors | Do not accept — request correct document |
| FMCSA API unavailable for monthly recheck | Retry in 24 hours; flag for manual check |
| Carrier disputes suspension | Explain compliance requirement; provide extension path only with proof of in-progress renewal |
| ELD malfunctions (not carrier fault) | Document; continue with paper log per FMCSA rules |
| Carrier renews insurance with lower limits | Reject — must meet minimum requirements |
