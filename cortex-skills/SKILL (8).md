---
skill_id: "01-carrier-prospecting"
name: "Carrier Prospecting & Recruitment"
version: "1.0.0"
phase: 0
priority: medium
trigger: "Daily automated outreach campaign OR manual trigger when truck inventory is low"
inputs:
  - target_region: "string — geographic focus area"
  - equipment_types_needed: "array — dry van / reefer / flatbed"
outputs:
  - qualified_prospects: "array — carrier leads ready for onboarding"
  - crm_records: "updated prospect pipeline"
integrations:
  - Facebook Groups API
  - DAT Load Board (posted trucks)
  - LinkedIn
  - Email/SMS gateway
  - CRM
depends_on: []
triggers_next: ["02-carrier-onboarding"]
tags: [acquisition, outreach, recruitment]
---

# SKILL: Carrier Prospecting & Recruitment
## Cortex Bot — Truck Dispatch Automation

**Trigger**: Daily automated outreach campaign + manual trigger when truck inventory is low.  
**Phase**: 0 (Pre-Onboarding)  
**Priority**: MEDIUM  

---

## Purpose

Actively recruit new owner-operators and small fleets to expand the carrier base. A larger, quality carrier base means more truck options, better lane coverage, and less scrambling when a carrier is unavailable.

---

## Target Carrier Profile

**Ideal candidate:**
- Owner-operator or 1–5 truck fleet
- MC authority 90+ days old (some brokers require this)
- Safety rating: Satisfactory or None (no Conditional/Unsatisfactory)
- Clean CSA scores (no flagged BASICs)
- Equipment: 53' dry van, reefer, or flatbed (highest freight volume)
- Based in or running major freight lanes (Southeast, Midwest, Texas, California)

---

## Outreach Channels

### 1. Facebook Groups (Trucking Owner-Operator Communities)
- Search: "Owner Operators", "Truck Dispatchers", "Dry Van Freight", "Reefer Freight"
- Post in relevant groups:
  > "🚛 Looking for owner-operators and small fleets in [region]! We're a dispatch service that books loads, handles broker paperwork, rate negotiations, and invoicing — you just drive. No long-term contracts. Earning $[X]+ CPM on average. DM me or call [phone] to learn more."
- Respond to posts from drivers looking for loads or dispatchers

### 2. DAT / Truckstop Posted Trucks
- Search for trucks posted without a dispatcher in target lanes
- Call carriers directly: "Hi, I saw you have a truck posted in [city]. I'm a dispatcher — have you found your next load? I may have something for you."

### 3. LinkedIn (Fleet Owners and Operations Managers)
- Search: Owner-operator, Fleet Manager, Small Carrier
- Send connection request + brief intro message

### 4. Email/SMS Campaign (if carrier contact list available)
Template:
> Subject: Free yourself from the load board — we handle dispatch
> 
> Hi [Name],
> 
> Running your own truck is hard enough without spending 13 hours a week on load boards and broker calls. We're a full-service dispatch team that handles:
> • Finding and negotiating your loads on DAT/Truckstop
> • All broker paperwork (carrier packets, rate cons)
> • Invoicing and factoring coordination
> • 24/7 in-transit support
> 
> Our carriers average $X.XX+ CPM. No contracts. Pay only when you haul.
> 
> Interested? Let's talk: [phone] or reply to this email.
> 
> [Dispatcher Name]

### 5. Referral Program
- Active carriers refer other carriers → earn $[X] credit per successful referral

---

## Qualification Call (after carrier responds)

Questions to ask:
1. "What equipment do you run? How many trucks?"
2. "What's your MC authority date? How long have you been authorized?"
3. "What lanes do you prefer? Where are you based?"
4. "Are you currently working with a dispatcher? What's your biggest frustration?"
5. "What rate are you typically looking for per mile?"
6. "Do you use a factoring company? Which one?"
7. "Any special certifications — hazmat, TWIC, Oversize/OW?"

**Disqualify if:**
- MC authority inactive or <30 days old
- Safety rating Unsatisfactory
- Unwilling to sign dispatch agreement
- Using factoring company banned by major brokers

---

## CRM Tracking

Track each prospect in CRM:
- Contact info + equipment + lanes + rate expectations
- Date of first contact
- Follow-up schedule (3 touchpoints before marking cold)
- Status: `Prospect → Qualified → Onboarding → Active`

---

## Output
- Qualified prospects fed into `carrier-onboarding` skill ✅
- CRM updated with all prospect data ✅
- Weekly new carrier acquisition metric reported ✅
