---
skill_id: "24-broker-relationship-management"
name: "Broker Relationship Management"
version: "1.0.0"
phase: 18
priority: high
trigger: "Weekly automated review. Also triggered after every booked load and every payment received."
inputs:
  - broker_id: "string"
  - recent_loads: "array of loads booked with this broker (last 90 days)"
  - payment_history: "array of invoices and payment dates"
  - negotiation_outcomes: "array of agreed rates vs market"
outputs:
  - broker_score: "integer 0–100"
  - relationship_tier: "enum: PREFERRED | ACTIVE | CAUTION | BLACKLIST"
  - recommended_actions: "array of relationship-building or avoidance actions"
  - top_contacts: "best contacts at this broker for this equipment type"
integrations:
  - TMS/CRM
  - DAT Broker Credit API
  - TIA watchlist API
  - Email
depends_on: ["19-payment-reconciliation", "08-broker-negotiation"]
triggers_next: []
tags: [broker, relationship, crm, credit, preferred, blacklist, scoring]
---

# SKILL: Broker Relationship Management
## Cortex Bot — Truck Dispatch Automation

**Trigger**: Weekly automated review for all active brokers. Also triggered after each load completion.
**Phase**: 18 (Continuous Improvement)
**Priority**: HIGH

---

## Purpose

Not all brokers are equal. PREFERRED brokers pay fast, offer fair rates, and have good loads. CAUTION brokers slow pay, dispute accessorials, or lowball rates. BLACKLIST brokers are a net loss. This skill tracks every broker interaction and gives each a score so the dispatch system prioritizes the best relationships and avoids the bad ones.

---

## Broker Scoring Model

### Score Components (100 points total)

| Category | Weight | Metrics |
|----------|--------|---------|
| Payment speed | 30 pts | Days to pay vs agreed terms |
| Rate quality | 25 pts | Agreed rate vs DAT market |
| Load quality | 20 pts | Volume, consistency, commodities |
| Communication | 15 pts | Responsiveness, accuracy |
| Dispute rate | 10 pts | % of invoices disputed |

### Payment Speed Scoring (30 pts)
```
if avg_days_to_pay <= net_terms:
    payment_score = 30
elif avg_days_to_pay <= net_terms + 5:
    payment_score = 25
elif avg_days_to_pay <= net_terms + 15:
    payment_score = 15
elif avg_days_to_pay <= net_terms + 30:
    payment_score = 5
else:
    payment_score = 0
```

### Rate Quality Scoring (25 pts)
```
rate_vs_market = avg(agreed_rate_cpm) / avg(dat_market_rate_cpm)

if rate_vs_market >= 1.10:
    rate_score = 25  # pays 10%+ above market
elif rate_vs_market >= 1.05:
    rate_score = 20
elif rate_vs_market >= 1.00:
    rate_score = 15  # pays at market
elif rate_vs_market >= 0.95:
    rate_score = 8   # slightly below market
else:
    rate_score = 0   # consistently below market
```

### Load Quality Scoring (20 pts)
```
load_score = 0

if loads_per_month >= 4: load_score += 8  # consistent volume
if avg_miles_per_load >= 400: load_score += 5  # good long hauls
if commodity_mix == "favorable": load_score += 4  # no hazmat/difficulty
if appointment_accuracy >= 90%: load_score += 3  # appointments are real
```

### Communication Scoring (15 pts)
```
comm_score = 0
if avg_rc_delivery_time <= 30:  load_score += 5  # RC within 30 min
if rc_accuracy >= 95%: load_score += 5  # rare discrepancies
if broker_responds_to_delays: load_score += 5  # good partnership behavior
```

### Dispute Rate Scoring (10 pts)
```
dispute_rate = disputes / total_invoices

if dispute_rate == 0: dispute_score = 10
elif dispute_rate <= 0.05: dispute_score = 8
elif dispute_rate <= 0.10: dispute_score = 5
elif dispute_rate <= 0.20: dispute_score = 2
else: dispute_score = 0
```

---

## Relationship Tiers

| Score | Tier | Treatment |
|-------|------|-----------|
| 80–100 | PREFERRED | Priority calls, negotiate less aggressively (relationship value), offer consistent capacity |
| 60–79 | ACTIVE | Standard engagement — book loads at full negotiated rate |
| 40–59 | CAUTION | Require quick-pay or factoring. Watch carefully. |
| 20–39 | RESTRICTED | Only book if rate is significantly above market. Get quick-pay up front. |
| 0–19 | BLACKLIST | Do not book. Flag on DAT. Report to TIA if payment defaults. |

---

## Execution Steps

### Step 1 — Weekly Broker Review

For every broker with ≥1 load in last 90 days:
1. Pull all loads, rates, payment dates, disputes from TMS
2. Calculate score per model above
3. Update tier in CRM
4. Flag any tier changes (especially downgrades)

### Step 2 — Preferred Broker Nurturing

For PREFERRED brokers:
- Send monthly capacity update:
  > "Hi [Name], wanted to touch base — we have [X] trucks available in [region] this week. If you have any [equipment] loads, we'd love to keep working together. We've really appreciated the consistent business and quick payments."
- Offer rate flexibility (within reason) to retain relationship
- Be first to call when truck is available in their strongest lanes
- Note personal details (name, preferences) in CRM

### Step 3 — Caution/Restricted Broker Protocols

For CAUTION brokers:
- Require quick-pay option on every load
- Add to factoring company "verify before advancing" list
- Set TMS flag: "CAUTION — verify payment before next load"
- Track trend: is score improving or declining?

For RESTRICTED brokers:
- Only accept loads when rate is ≥15% above market
- Require quick-pay or do not book
- Document every interaction

### Step 4 — Blacklist Actions

When broker score drops to BLACKLIST:
1. Flag in CRM: DO NOT BOOK
2. Notify all active dispatchers/carriers
3. Report to DAT credit watchlist (if payment default)
4. File with TIA if applicable
5. Attempt bond claim (FMCSA BMC-84) if unpaid invoices outstanding

### Step 5 — New Broker Onboarding

When calling a broker for the first time:
1. Look up MC# on FMCSA (from `03-fmcsa-verification`)
2. Check DAT credit score and days-to-pay
3. Check TIA watchlist
4. Set initial tier: ACTIVE (neutral start)
5. First load: prefer quick-pay option
6. Monitor payment — grade after first 3 loads

---

## Contact Management

For each broker, maintain:
```json
{
  "broker_id": "ECHO-001",
  "company": "Echo Global Logistics",
  "mc_number": "MC-YYYYYY",
  "dat_credit_score": 82,
  "avg_days_to_pay": 18,
  "relationship_tier": "PREFERRED",
  "broker_score": 85,
  "top_contacts": [
    {
      "name": "Sarah Jones",
      "title": "Load Coordinator",
      "phone": "+15551234567",
      "email": "sjones@echo.com",
      "best_lanes": ["Southeast", "Midwest"],
      "equipment_focus": "dry_van",
      "notes": "Quick to approve rate increases on tight market days. Responds best in mornings.",
      "relationship_quality": "strong"
    }
  ],
  "loads_last_90_days": 12,
  "avg_rate_vs_market": 1.08,
  "dispute_count": 0,
  "last_contacted": "2026-03-18"
}
```

---

## Outputs

```json
{
  "broker_id": "ECHO-001",
  "score": 85,
  "tier": "PREFERRED",
  "score_trend": "STABLE",
  "recommended_actions": [
    "Reach out with weekly capacity update",
    "Prioritize their loads in Southeast corridor",
    "Offer 3-truck commitment for Q2 if rates hold"
  ],
  "alerts": []
}
```

---

## Error Handling

| Situation | Action |
|-----------|--------|
| Broker score drops 20+ points in one week | Immediate review — check for payment issues |
| Broker appears on TIA watchlist | Immediate RESTRICTED status; notify carriers |
| New broker with no DAT data | Start CAUTION tier; require quick-pay on first 3 loads |
| PREFERRED broker misses payment | Score recalculation; may drop to ACTIVE or CAUTION |
