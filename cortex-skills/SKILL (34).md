---
skill_id: "25-carrier-performance-scoring"
name: "Carrier Performance Scoring"
version: "1.0.0"
phase: 18
priority: high
trigger: "Weekly automated run for all active carriers. Also triggered after each delivered load and payment confirmed."
inputs:
  - carrier_id: "string"
  - loads_data: "array of all loads in last 90 days"
  - payment_data: "revenue per load, total invoiced"
  - compliance_data: "check-call rate, document submission times, HOS events"
outputs:
  - performance_report: "weekly KPI summary"
  - carrier_score: "integer 0–100"
  - lane_recommendations: "suggested lane adjustments"
  - alerts: "underperformance flags requiring action"
integrations:
  - TMS
  - ELD API
  - WhatsApp/Email (report delivery)
depends_on: ["19-payment-reconciliation", "15-in-transit-monitoring"]
triggers_next: ["04-carrier-profile-management (if profile update recommended)"]
tags: [performance, kpi, scoring, weekly-report, optimization, carrier]
---

# SKILL: Carrier Performance Scoring
## Cortex Bot — Truck Dispatch Automation

**Trigger**: Weekly automated review for all active carriers.
**Phase**: 18 (Continuous Improvement)
**Priority**: HIGH

---

## Purpose

Track every carrier against key performance metrics to identify top performers, detect declining trends early, optimize lane assignments, and ensure every carrier is earning at their potential. Data replaces gut feel.

---

## KPI Framework

### Core KPIs (Tracked Weekly)

| Metric | Formula | Target | Alert |
|--------|---------|--------|-------|
| Weekly miles | sum(loaded_miles + deadhead_miles) | >2,500 mi | <2,000 mi |
| Loaded miles percentage | loaded / total × 100 | >88% | <80% |
| Average RPM (revenue per mile) | total_revenue / loaded_miles | >$2.50 | <$2.00 |
| Gross revenue | sum(all_load_payments) | >$6,000/wk | <$4,000/wk |
| On-time pickup rate | on_time_pu / total_pu × 100 | >95% | <85% |
| On-time delivery rate | on_time_del / total_del × 100 | >95% | <85% |
| Load acceptance rate | accepted / offered × 100 | >80% | <60% |
| Check-call compliance | on_time_calls / required_calls × 100 | >95% | <80% |
| Document submission speed | avg hours from delivery to POD | <2 hrs | >12 hrs |
| Detention events per week | count(detention_events) | <2 | >5 |
| Detention revenue | sum(detention_claimed) | Track | Not claimed = missed $ |
| HOS violations | count(hos_violations) | 0 | >0 |

---

## Carrier Scoring Model (100 pts)

| Category | Weight | Metric |
|----------|--------|--------|
| Revenue efficiency | 30 pts | RPM vs target |
| Reliability | 25 pts | On-time pickup + delivery |
| Utilization | 20 pts | Miles driven vs capacity |
| Compliance | 15 pts | Check-calls, documents, HOS |
| Responsiveness | 10 pts | Load acceptance rate |

```python
def calculate_carrier_score(carrier):
    # Revenue efficiency (30 pts)
    rpm_score = min(30, (carrier.avg_rpm / 2.50) * 30)
    
    # Reliability (25 pts)
    reliability = (carrier.on_time_pickup + carrier.on_time_delivery) / 2
    reliability_score = (reliability / 100) * 25
    
    # Utilization (20 pts)
    loaded_pct = carrier.loaded_miles / carrier.total_miles
    utilization_score = min(20, loaded_pct * 20 / 0.88)
    
    # Compliance (15 pts)
    compliance = (carrier.check_call_rate + carrier.doc_submission_on_time) / 2
    compliance_score = (compliance / 100) * 15
    if carrier.hos_violations > 0:
        compliance_score *= 0.5  # Half score for any HOS violation
    
    # Responsiveness (10 pts)
    responsiveness_score = (carrier.load_acceptance_rate / 100) * 10
    
    return rpm_score + reliability_score + utilization_score + compliance_score + responsiveness_score
```

---

## Weekly Performance Report (Sent to Carrier)

```
📊 WEEKLY PERFORMANCE REPORT
Carrier: [Name] | MC#: [X]
Week: March 16–22, 2026

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🚛 LOADS & MILES
Loads hauled: 5
Total miles: 3,240  (loaded: 2,890 | deadhead: 350)
Deadhead %: 10.8% ✅
Loaded mile efficiency: 89.2% ✅

💰 REVENUE
Gross revenue: $7,450
Average RPM: $2.58 ✅
Detention earned: $175
Fuel cost estimate: $920
Net to carrier (est.): $6,530

⏱️ RELIABILITY
On-time pickup: 5/5 (100%) ✅
On-time delivery: 4/5 (80%) ⚠️
  └─ Late delivery: Echo load on Wed — 45 min due to traffic

📋 COMPLIANCE
Check-call compliance: 19/20 (95%) ✅
Document submission: Avg 1.4 hrs after delivery ✅
HOS violations: 0 ✅

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📈 PERFORMANCE SCORE: 88/100  ↑ +3 from last week

🗺️ BEST LANES THIS WEEK:
1. Nashville → Atlanta — $2.82/mi
2. Atlanta → Charlotte — $2.65/mi
3. Memphis → Nashville — $2.45/mi

💡 RECOMMENDATIONS:
• Great week overall! One late delivery this week — traffic on I-75.
• Consider I-285 bypass around Atlanta on Wed afternoons.
• Dallas → Houston showing $2.90/mi this week if you want to position there.
• Home time request received — blocking Fri–Sun next week. ✅

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Questions? Reply to this message or call [dispatcher phone].
```

---

## Execution Steps

### Step 1 — Data Pull (Every Sunday night for prior week)

Pull from TMS for each active carrier:
- All loads with pickup/delivery timestamps
- All check-calls (scheduled vs actual)
- All document submissions with timestamps
- HOS data from ELD API
- All revenue and accessorials
- All detention claims submitted vs accrued

### Step 2 — Calculate KPIs

Run all formulas above. Flag any metric below alert threshold.

### Step 3 — Generate Lane Recommendations

```
# Identify best-performing lanes (last 90 days)
best_lanes = TMS.query(
    carrier=carrier_id,
    order_by=agreed_rate_cpm DESC,
    limit=5
)

# Identify underperforming lanes
worst_lanes = TMS.query(
    carrier=carrier_id,
    order_by=agreed_rate_cpm ASC,
    limit=3
)

# Cross-reference with current market rates
for lane in worst_lanes:
    if dat_market_rate(lane) > carrier.rate_floor × 1.1:
        recommend: "Market improved — try this lane again"
    else:
        recommend: "Avoid this lane — market remains weak"
```

### Step 4 — Trend Analysis

Compare this week vs last 4 weeks:
- Is RPM trending up or down?
- Is deadhead increasing (carrier drifting from good markets)?
- Is acceptance rate declining (rate floor too high for current market)?
- Is on-time rate declining (carrier taking on too many tight loads)?

### Step 5 — Send Report

Deliver via WhatsApp (mobile-friendly) + email (full version) every Monday morning at 07:00.

### Step 6 — Profile Adjustments (If Needed)

Based on performance data, recommend profile updates:
- Rate floor adjustment if consistently below or above market
- Lane preference update if new corridors are outperforming
- Constraint review if acceptance rate is too low
- Home time adjustment if pattern shows driver fatigue

Trigger `04-carrier-profile-management` with recommendations.

---

## Outputs

```json
{
  "carrier_id": "CARR-032026-001",
  "week_ending": "2026-03-22",
  "carrier_score": 88,
  "score_trend": "+3",
  "weekly_miles": 3240,
  "loaded_pct": 89.2,
  "avg_rpm": 2.58,
  "gross_revenue": 7450,
  "on_time_pickup_pct": 100,
  "on_time_delivery_pct": 80,
  "check_call_compliance": 95,
  "hos_violations": 0,
  "alerts": ["on_time_delivery below target — 80% vs 95% target"],
  "report_sent": true
}
```

---

## Error Handling

| Situation | Action |
|-----------|--------|
| ELD data unavailable | Use manual check-call logs for compliance estimate |
| Carrier has 0 loads in week | Send availability check instead of performance report |
| Score drops >15 points in one week | Flag for dispatcher review — investigate cause |
| Acceptance rate <50% | Urgent — carrier may have equipment issue or rate mismatch |
