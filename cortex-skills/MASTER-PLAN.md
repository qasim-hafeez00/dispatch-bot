# 🚛 Autonomous Truck Dispatching — Master Plan & Skill Index
## Cortex Bot Automation System

---

## Research Summary

Based on comprehensive research across the truck dispatching industry, a dispatcher today handles:
- **13+ hours/week** of manual load board searching, calling, and negotiating
- **5–15 broker calls** per load before booking
- **20–40 documents** per carrier onboarded
- **Check calls every 2 hours** per active truck
- **Same-day invoicing** with POD collection after delivery

Modern AI-powered TMS platforms (DataTruck, DispatchMVP, LoadStop) have automated 40–60% of these tasks but still require human dispatchers for negotiation, carrier relations, and exception handling. Our goal is full autonomy — 100% automated from carrier recruitment to payment collection.

---

## Enhanced Workflow (Research-Improved, 18 Phases)

### PHASE 0 — CARRIER ACQUISITION
> Added based on research: dispatchers spend significant time recruiting quality carriers

**0.1** Run outbound prospecting (email/SMS/social) for owner-operators and small fleets  
**0.2** Qualify inbound leads against minimum standards (MC age, safety score, equipment)  
**0.3** Initiate automated onboarding sequence upon qualification

---

### PHASE 1 — CARRIER ONBOARDING
**1.1** Collect all compliance documents (MC/DOT, W-9, COI, NOA/Factoring, CDL, truck/trailer info)  
**1.2** Verify FMCSA authority status live (Active, not Revoked/Suspended)  
**1.3** Verify insurance live with agent — get COI-on-demand email  
**1.4** Run safety score check (CSA scores, inspections, violations, crash history)  
**1.5** Sign service agreement + payment terms (detention policy, invoice schedule, dispatcher fee)  
**1.6** Set communication channels (phone, WhatsApp, email) and response time SLA

---

### PHASE 2 — CARRIER PROFILE CONFIGURATION
**2.1** Equipment specs (53' dry van / reefer / flatbed / step-deck / power-only / hotshot)  
**2.2** Preferred lanes, home base, max deadhead miles  
**2.3** Rate floor (minimum CPM or flat rate per load)  
**2.4** HOS constraints, pickup/delivery windows, home time requirements  
**2.5** Special constraints (hazmat cert, TWIC card, NYC surcharge, Canada, team driver, touch/no-touch)  
**2.6** Reefer temperature limits, tarp/strap requirements  
**2.7** Driver availability calendar + actual truck location today

---

### PHASE 3 — LOAD BOARD FILTER CONFIGURATION
**3.1** Configure DAT/Truckstop filters (equipment, radius, weight, length, commodity exclusions)  
**3.2** Set rate floors, pickup/delivery date windows, lane filters  
**3.3** Enable credit score / quick-pay filters — avoid brokers with payment issues  
**3.4** Enable comments/notes fields for broker instructions  
**3.5** Save search profiles per truck/equipment type  
**3.6** Set up automated refresh alerts (every 3–5 min)  
**3.7** Configure Landstar/Echo/Coyote direct portals if applicable

---

### PHASE 4 — LOAD SEARCH & TRIAGE
**4.1** Scan newest loads first across all boards  
**4.2** Auto-screen each load against carrier eligibility gate (see Phase 5)  
**4.3** Score and rank loads by profitability (CPM, deadhead, dwell risk, reload market)  
**4.4** Flag "hot" loads — high rate, good market, minimal deadhead  
**4.5** If no suitable loads: widen radius, check reload markets, post truck with clear blurb  
**4.6** Proactively call top brokers in target lanes while waiting

---

### PHASE 5 — ELIGIBILITY GATE (Pre-Call Checklist)
**5.1** Equipment/commodity/weight/length match ✅  
**5.2** Pickup date/time within driver HOS and distance window ✅  
**5.3** Insurance meets broker requirements (COI limits, endorsements) ✅  
**5.4** MC age acceptable to broker (usually 30–90+ days) ✅  
**5.5** Factoring company not on broker's ban list ✅  
**5.6** No hazmat/TWIC/team requirement without carrier cert ✅  
**5.7** Target rate and backup counter-offer prepared ✅

---

### PHASE 6 — BROKER CONTACT & DETAIL GATHERING
**6.1** Call/email broker: "Calling on [PU city > DEL city] [commodity] for [date]."  
**6.2** Gather complete load details: addresses, times, ref#s, weight, pieces, commodity  
**6.3** Gather accessorials: detention terms, TONU, layover, extra stops, lumper process  
**6.4** Gather requirements: temp, tarps/straps, driver assist, FCFS vs appt, tracking method  
**6.5** Gather payment terms: net days, quick-pay option, factoring restrictions  
**6.6** Live-text summary to driver/carrier via WhatsApp/SMS for quick review

---

### PHASE 7 — RATE NEGOTIATION
**7.1** Research lane rate (DAT rate index, Truckstop market data, recent loads in lane)  
**7.2** Calculate true cost: deadhead miles, fuel cost, dwell risk, HOS impact  
**7.3** Anchor high with justification (lane average, deadhead, market tightness)  
**7.4** Lock all accessorials in writing before committing (detention start time, TONU amount, layover rate)  
**7.5** Aim for all-in rate; confirm fuel surcharge handling  
**7.6** Max 90-second hold while confirming with carrier

---

### PHASE 8 — CARRIER CONFIRMATION LOOP
**8.1** Send rate summary to carrier (PU/DEL, miles, rate, commodity, weight, special reqs)  
**8.2** If inactive: call → missed call while broker on hold (90-second max cycle)  
**8.3** If carrier rejects: capture reason, attempt solve (rate, timing, commodity)  
**8.4** If unsolvable: release quickly, move to next eligible load/carrier  
**8.5** Log rejection reason in carrier profile for pattern analysis

---

### PHASE 9 — LOAD BOOKING
**9.1** Confirm booking to broker: "Book it. Our MC is [MC#]. Rate confirmed at $[X]."  
**9.2** Provide carrier email for rate confirmation delivery  
**9.3** If broker requires owner call: 3-way call loop-in immediately  
**9.4** Request carrier packet / rate con be sent immediately

---

### PHASE 10 — CARRIER PACKET COMPLETION
**10.1** Retrieve packet from email (auto-parse PDF fields)  
**10.2** Auto-fill: MC Authority, W-9, NOA/Factoring, insurance/COI, payment info  
**10.3** Return promptly (within 15 minutes); confirm receipt  
**10.4** Request Rate Confirmation document

---

### PHASE 11 — RATE CONFIRMATION REVIEW
**11.1** Verify PU/DEL addresses, appt times, ref numbers  
**11.2** Verify weight/pieces/commodity match what was verbally agreed  
**11.3** Verify all accessorials (detention start, TONU amount, extra stops, driver assist, lumper)  
**11.4** Verify tracking requirements, in/out times on BOL, POD requirements  
**11.5** Verify payment terms, factoring assignment, quick-pay fees  
**11.6** If discrepancies: call broker immediately to correct before signing  
**11.7** Sign and return; file RC in load folder/TMS

---

### PHASE 12 — DRIVER DISPATCH
**12.1** Generate dispatch sheet (load#, broker contact, PU/DEL info, times, commodity, weight)  
**12.2** Include special requirements (straps/tarps, load locks, PPE, temperature settings)  
**12.3** Include facility notes (FCFS vs appt, lumper process, photo ID needed, dock #)  
**12.4** Set check-call schedule: depart PU → loaded → 2-hr updates → arrival DEL → empty  
**12.5** Send tracking link to broker  
**12.6** Confirm driver acknowledgement

---

### PHASE 13 — IN-TRANSIT MONITORING
**13.1** Monitor GPS/ELD position continuously  
**13.2** Auto-generate check calls at scheduled intervals  
**13.3** Detect delays early (traffic, weather, breakdown) — notify broker proactively  
**13.4** Monitor HOS remaining — alert if driver at risk of running out before delivery  
**13.5** Handle exceptions: reschedule, detention clock management, lumper auth  
**13.6** Ensure in/out times are written/stamped on BOL at each stop

---

### PHASE 14 — DETENTION & ACCESSORIAL MANAGEMENT
> Added based on research: detention claims are frequently missed, costing carriers $200–500/load

**14.1** Start detention clock when truck arrives (geo-fence trigger)  
**14.2** Notify broker at 1:45 (15 min before detention typically starts at 2 hrs)  
**14.3** Document timestamps for all dwell: arrival, loaded/unloaded, departure  
**14.4** Submit detention/layover/TONU claims with timestamps immediately after delivery  
**14.5** Track claim status and follow up if unpaid within agreed terms

---

### PHASE 15 — DELIVERY & DOCUMENT COLLECTION
**15.1** Confirm delivery with driver — collect clear POD/BOL photos + PDFs  
**15.2** Collect lumper receipts if applicable  
**15.3** Verify in/out times are on BOL before accepting photos  
**15.4** Submit docs to broker and/or factoring company same day  
**15.5** Log delivery confirmation in TMS

---

### PHASE 16 — INVOICING & PAYMENT
**16.1** Auto-generate invoice: line-haul rate + all approved accessorials  
**16.2** Attach RC + POD + signed BOL + lumper receipts  
**16.3** Submit to factoring company (if enrolled) for same-day advance  
**16.4** Submit directly to broker if non-factored  
**16.5** Confirm payment timeline and set follow-up reminder

---

### PHASE 17 — PAYMENT RECONCILIATION & KPI TRACKING
> Added based on research: unpaid invoices are a major pain point; avg 45+ day collection

**17.1** Track expected vs actual payment dates  
**17.2** Send automated reminder at net-7, net-14 overdue  
**17.3** Escalate to collections/dispute process at net-30+ overdue  
**17.4** Update carrier KPI dashboard (weekly miles, avg RPM, on-time %, revenue)  
**17.5** Flag underperforming lanes; recommend lane adjustments to carrier

---

### PHASE 18 — CONTINUOUS IMPROVEMENT
> Added based on research: best dispatch services continuously optimize lane strategy

**18.1** Weekly performance review per carrier (miles, RPM, deadhead %, on-time %)  
**18.2** Broker relationship scoring (payment speed, load quality, rate negotiation outcome)  
**18.3** Lane opportunity analysis (where is freight densest vs where carrier is positioned)  
**18.4** Driver satisfaction monitoring (reject rate, detention frequency, home time compliance)  
**18.5** Adjust load board filters and lane strategy based on data

---

## Complete Skills Map

| # | Skill Name | Phase | Priority |
|---|-----------|-------|----------|
| 1 | `carrier-prospecting` | 0 | Medium |
| 2 | `carrier-onboarding` | 1 | Critical |
| 3 | `fmcsa-verification` | 1 | Critical |
| 4 | `carrier-profile-management` | 2 | Critical |
| 5 | `load-board-search` | 3–4 | Critical |
| 6 | `load-triage-eligibility` | 4–5 | Critical |
| 7 | `rate-market-intelligence` | 7 | Critical |
| 8 | `broker-negotiation` | 6–7 | Critical |
| 9 | `carrier-confirmation-loop` | 8 | Critical |
| 10 | `load-booking` | 9 | Critical |
| 11 | `carrier-packet-completion` | 10 | Critical |
| 12 | `rate-confirmation-review` | 11 | Critical |
| 13 | `driver-dispatch` | 12 | Critical |
| 14 | `hos-compliance` | 13 | Critical |
| 15 | `in-transit-monitoring` | 13 | Critical |
| 16 | `detention-layover-management` | 14 | High |
| 17 | `pod-document-collection` | 15 | Critical |
| 18 | `invoicing-factoring` | 16 | Critical |
| 19 | `payment-reconciliation` | 17 | High |
| 20 | `tms-crm-logging` | All | Critical |
| 21 | `backhaul-planning` | 3–4 | High |
| 22 | `fuel-optimization` | 12–13 | Medium |
| 23 | `weather-risk-monitoring` | 13 | High |
| 24 | `broker-relationship-management` | 18 | High |
| 25 | `carrier-performance-scoring` | 18 | High |
| 26 | `compliance-monitoring` | All | High |
| 27 | `accessorials-tracking` | 14–16 | High |

---

## Skills Missing from Original Workflow (Research-Added)

1. **`carrier-prospecting`** — Original workflow assumes carriers come to you. Research shows top dispatch services actively recruit quality carriers via cold outreach.
2. **`backhaul-planning`** — Critical for minimizing deadhead and maximizing CPM. Not addressed in original.
3. **`fuel-optimization`** — Fuel is top operating cost. Route-aware fuel stop planning is essential.
4. **`weather-risk-monitoring`** — Weather delays are #1 cause of detention and late delivery penalties.
5. **`broker-relationship-management`** — Building broker networks is key to higher rates over time.
6. **`carrier-performance-scoring`** — Tracking KPIs enables continuous lane optimization.
7. **`payment-reconciliation`** — Original workflow ends at invoice submission. Collection follow-up is critical.
8. **`compliance-monitoring`** — IFTA, ELD, DOT compliance runs continuously and creates liability if missed.
9. **`accessorials-tracking`** — Most dispatchers miss detention claims. Automated tracking captures $200–500+ per load.

---

## Implementation Architecture

```
CORTEX BOT ORCHESTRATOR
         |
    ┌────┴─────┐
    |           |
CARRIER       LOAD
MANAGEMENT    OPERATIONS
    |           |
    |    ┌──────┼──────┐
    |  SEARCH  NEGO  BOOK
    |    └──────┼──────┘
    |           |
    └────┬──────┘
         |
    DISPATCH ENGINE
         |
    ┌────┴─────┐
    |           |
TRANSIT     DOCUMENTS
MONITOR       & PAY
```

Each skill is a self-contained module with:
- **Trigger**: What event activates this skill
- **Inputs**: What data it needs
- **Process**: Step-by-step execution
- **Outputs**: What it produces
- **Integrations**: APIs/tools required
- **Error handling**: What to do when things go wrong
