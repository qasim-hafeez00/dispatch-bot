---
skill_id: "03-fmcsa-verification"
name: "FMCSA & Safety Verification"
version: "1.0.0"
phase: 1
priority: critical
trigger: "Called during carrier onboarding OR when checking a broker MC# before booking OR monthly recheck"
inputs:
  - mc_number: "string — carrier or broker MC#"
  - dot_number: "string — optional"
  - check_type: "enum: carrier | broker"
outputs:
  - verification_report: "JSON with all FMCSA fields"
  - overall_status: "enum: APPROVED | CONDITIONAL | REJECTED"
  - risk_flags: "array of identified issues"
integrations:
  - FMCSA SAFER API
  - FMCSA Licensing & Insurance portal
  - CSA BASIC scores API
  - Carrier411
  - DAT Broker Credit
depends_on: ["02-carrier-onboarding"]
triggers_next: ["02-carrier-onboarding (result)", "10-load-booking (broker check)"]
tags: [compliance, fmcsa, safety, verification, insurance]
---

# SKILL: FMCSA & Safety Verification
## Cortex Bot — Truck Dispatch Automation

**Trigger**: Called during carrier onboarding OR when checking a broker's MC# before booking.  
**Phase**: 1, 6 (used in both carrier onboarding and pre-booking checks)  
**Priority**: CRITICAL  

---

## Purpose

Verify that a carrier (or broker) is legally authorized to operate, has active insurance filings with FMCSA, and does not have safety violations that would disqualify them for loads. Also used to verify broker authority before submitting a carrier packet.

---

## When to Run

1. **Always** during carrier onboarding (new carrier)
2. **Before booking** when broker MC age or credit score is unknown
3. **Monthly** for all active carriers (recheck for lapses)
4. **Before dispatching** any carrier who has been inactive >30 days
5. **When a broker requests** COI or authority verification

---

## Data Sources

| Source | URL | What it provides |
|--------|-----|-----------------|
| FMCSA SAFER | https://safer.fmcsa.dot.gov/query.asp | Authority, safety rating, inspections |
| FMCSA Licensing & Insurance | https://li-public.fmcsa.dot.gov | Insurance filings, bond, broker authority |
| CSA BASIC scores | https://ai.fmcsa.dot.gov/sms | Driver/vehicle safety violation percentiles |
| Carrier411 | https://www.carrier411.com | Cargo theft history, blacklists |
| DAT Broker Credit | https://www.dat.com | Broker payment scores, days to pay |

---

## Execution Steps

### Step 1 — FMCSA SAFER Lookup
1. Go to FMCSA SAFER: `https://safer.fmcsa.dot.gov/query.asp`
2. Search by MC# or DOT#
3. Record:
   - Entity Name
   - Operating Status → must be **ACTIVE**
   - USDOT Number
   - MC/MX Number
   - Authority type (Common Carrier, Contract Carrier, Broker)
   - Authority Granted Date → calculate MC age in days
   - Physical address

### Step 2 — Safety Rating Check
1. Note Safety Rating:
   - **Satisfactory** = ✅ Good
   - **None / Not Rated** = ✅ Acceptable (new carrier)
   - **Conditional** = ⚠️ Proceed with caution — check CSA scores
   - **Unsatisfactory** = ❌ REJECT — do not use

### Step 3 — Inspection & Violation History
1. Check number of inspections (past 24 months)
2. Check out-of-service rate (OOS%) for:
   - Driver OOS% (national average ~5.5% — alert if >10%)
   - Vehicle OOS% (national average ~20% — alert if >30%)
3. Check crash rate per million miles

### Step 4 — CSA BASIC Scores
Go to: `https://ai.fmcsa.dot.gov/sms`
Check percentile scores for each BASIC:
- Unsafe Driving (alert >65th percentile)
- HOS Compliance (alert >65th percentile)
- Driver Fitness (alert >80th percentile)
- Controlled Substances/Alcohol (alert >50th percentile)
- Vehicle Maintenance (alert >75th percentile)
- Hazmat Compliance (if applicable)

### Step 5 — Insurance Filing Verification
1. Go to FMCSA L&I portal: `https://li-public.fmcsa.dot.gov`
2. Search by MC/USDOT number
3. Verify:
   - Insurance filing status = Active
   - Form BMC-91 or BMC-91X on file (auto liability)
   - Form BMC-34 if cargo insurance filed with FMCSA
   - Effective and expiration dates
4. Note insurer name — cross-reference with carrier's COI

### Step 6 — Cargo Theft / Blacklist Check
1. Check Carrier411 for:
   - Cargo theft complaints
   - Double brokering history
   - Load abandonment reports
2. Flag any carrier with cargo theft or double brokering history — **DO NOT USE**

### Step 7 — Broker Credit Check (when booking loads)
1. Check broker MC# on FMCSA SAFER → must be licensed as **Property Broker**
2. Check broker credit score on DAT:
   - Days to Pay (DTP): flag if >45 days
   - Credit score: flag if <70
3. Check TIA (Transportation Intermediaries Association) watchlist
4. Flag brokers on factoring company ban lists

---

## Outputs

Returns a structured verification report:

```json
{
  "mc_number": "MC-XXXXXX",
  "dot_number": "XXXXXXXX",
  "entity_name": "ABC Trucking LLC",
  "operating_status": "ACTIVE",
  "mc_age_days": 450,
  "safety_rating": "Satisfactory",
  "driver_oos_pct": 4.2,
  "vehicle_oos_pct": 18.5,
  "csa_unsafe_driving": 42,
  "csa_hos_compliance": 28,
  "insurance_active": true,
  "insurance_expiry": "2026-09-15",
  "cargo_theft_flag": false,
  "overall_status": "APPROVED",
  "risk_flags": [],
  "verified_at": "2026-03-19T14:30:00Z"
}
```

---

## Pass/Fail Criteria

| Check | Pass | Fail |
|-------|------|------|
| Operating Status | ACTIVE | Anything else |
| Safety Rating | Satisfactory / None | Unsatisfactory |
| Driver OOS% | <10% | ≥10% |
| Vehicle OOS% | <30% | ≥30% |
| CSA Unsafe Driving | <65th pct | ≥65th pct |
| Insurance filing | Active | Lapsed / Missing |
| Cargo theft history | None | Any complaint |

---

## Error Handling

| Error | Action |
|-------|--------|
| MC# not found | Verify MC# with carrier — may be DOT-only |
| Status = Inactive | Do not onboard. Carrier must reinstate authority. |
| Status = Revoked | Hard reject. Never use. |
| Insurance lapsed | Hold all loads. Notify carrier immediately. |
| High CSA scores | Flag for limited load types. No hazmat, no NYC. |
