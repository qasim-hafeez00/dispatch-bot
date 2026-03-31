---
skill_id: "04-carrier-profile-management"
name: "Carrier Profile Management"
version: "1.0.0"
phase: 2
priority: critical
trigger: "After carrier onboarding is complete OR when carrier requests profile update"
inputs:
  - carrier_id: "string"
  - equipment_info: "type, year, VIN, dimensions"
  - lane_preferences: "origin/destination states, preferred corridors"
  - rate_expectations: "minimum CPM or flat rate floor"
  - availability_schedule: "HOS pattern, home time, blackout dates"
outputs:
  - carrier_profile: "complete preference record in TMS"
  - load_board_filter_config: "ready to pass to load-board-search skill"
integrations:
  - TMS/CRM
  - Load board APIs (DAT, Truckstop)
depends_on: ["02-carrier-onboarding", "03-fmcsa-verification"]
triggers_next: ["05-load-board-search"]
tags: [profile, preferences, equipment, lanes, rate-floor, hos]
---

# SKILL: Carrier Profile Management
## Cortex Bot — Truck Dispatch Automation

**Trigger**: After carrier onboarding is complete OR when carrier requests a profile update.
**Phase**: 2 (Carrier Profile Configuration)
**Priority**: CRITICAL

---

## Purpose

Capture every operating preference, constraint, and requirement for a carrier so that the load search and dispatch engine can find perfectly matched loads without any human guesswork. An incomplete profile means missed loads, bad matches, and carrier dissatisfaction.

---

## Profile Fields — Complete Reference

### Equipment Specifications

| Field | Options | Notes |
|-------|---------|-------|
| Equipment type | 53' dry van, 53' reefer, flatbed, step-deck, RGN, power-only, hotshot | Primary type |
| Additional equipment types | Same list | If carrier runs multiple units |
| Trailer year | Year | Affects certain broker requirements |
| Trailer length | 28', 48', 53' | Standard is 53' |
| Reefer unit make/model | Carrier, Thermo King, etc. | Reefer only |
| Reefer min temp | °F | Lowest temperature capable |
| Reefer max temp | °F | Highest temperature (frozen vs fresh) |
| Flatbed accessories | Tarps (number/type), straps (count), chains, binders, coil racks | Flatbed only |
| Max payload weight | lbs | Legal limit for their axle config |
| Van interior height | inches | For high-cube loads |
| Lift gate | Yes / No | If equipped |
| Team capable | Yes / No | If second driver available |

### Lane Preferences

| Field | Example | Notes |
|-------|---------|-------|
| Home base city/state | Nashville, TN | Where driver returns |
| Preferred origin states | TX, OK, AR | States driver wants to pick up from |
| Preferred destination states | CA, AZ, NV | States driver wants to deliver to |
| Preferred corridors | SE → NE, Midwest → Southeast | Named lanes |
| States to avoid | NY, NYC metro, NJ | Driver's hard avoids |
| Max trip length | 2,000 miles | Max distance per load |
| Max deadhead miles | 100 miles | Willing to run empty |
| Canada runs | Yes / No | Cross-border capable |
| NYC surcharge required | Yes / No / Amount | If driver will do NYC |
| Port runs (TWIC) | Yes / No | TWIC card on file |

### Rate Expectations

| Field | Notes |
|-------|-------|
| Minimum CPM (loaded) | Minimum cents-per-mile to accept load |
| Minimum flat rate | Minimum dollar amount for short hauls |
| Fuel surcharge expectation | Included in rate or separate |
| Target weekly gross revenue | Used for planning — not a hard floor |
| Dispatch fee | Percentage or flat fee per load |

### Hours of Service & Availability

| Field | Notes |
|-------|-------|
| Operating days | Mon–Fri, 7 days, weekdays only |
| Pickup window start | Earliest time driver will pick up (e.g., 06:00 AM) |
| Pickup window end | Latest time driver will start a pickup |
| Delivery window | Same |
| Home time schedule | Weekly (Fri–Sun home), biweekly, monthly |
| Blackout dates | Holidays, personal dates to avoid |
| Reset location | Where driver typically takes 34-hr reset |

### Special Certifications & Constraints

| Certification | On file | Notes |
|--------------|---------|-------|
| Hazmat endorsement | Yes / No | Class/division if yes |
| TWIC card | Yes / No | Expiry date |
| Tanker endorsement | Yes / No | |
| Oversize/overweight | Yes / No | Permits capability |
| Touch freight | Yes / No | Will driver load/unload? |
| Driver assist | Yes / No | Will driver assist lumper? |
| No-touch only | Yes / No | Hard constraint |
| Drop & hook preferred | Yes / No | Prefer pre-loaded trailers |
| Food grade required | Yes / No | If reefer carrier |
| Live animals | Yes / No | |
| Automotive | Yes / No | |

### Communication Preferences

| Field | Notes |
|-------|-------|
| Primary contact | Phone number |
| Owner contact | Separate if different from driver |
| Preferred channel | Call / WhatsApp / SMS / Email |
| Response time SLA | "Within 10 min during business hours" |
| After-hours contact | Emergency number |
| Language preference | English / Spanish / other |

---

## Execution Steps

### Step 1 — Profile Interview (Automated or Guided)

Send carrier a structured intake form covering all fields above. For high-value carriers, conduct a 15-minute phone intake and capture answers live.

### Step 2 — Validate for Completeness

Required fields before activating profile:
- [ ] Equipment type and capacity
- [ ] Home base location
- [ ] At least one preferred lane or origin state
- [ ] Minimum CPM or flat rate floor
- [ ] HOS pattern (operating days, pickup window)
- [ ] Touch/no-touch preference
- [ ] Communication channel confirmed

### Step 3 — Store in TMS

Create structured profile record. Tag carrier with capability flags:
- `equipment:dry-van`, `equipment:reefer`, etc.
- `certified:hazmat`, `certified:twic`, etc.
- `constraint:no-touch`, `constraint:no-nyc`, etc.
- `lane:southeast`, `lane:midwest`, etc.

### Step 4 — Generate Load Board Filter Config

Output a filter configuration object for `05-load-board-search`:

```json
{
  "carrier_id": "CARR-032026-001",
  "origin_city": "Nashville, TN",
  "origin_radius_miles": 100,
  "equipment": "53_dry_van",
  "max_weight_lbs": 44000,
  "min_rate_cpm": 2.25,
  "preferred_dest_states": ["GA", "FL", "SC", "NC", "VA"],
  "avoid_states": ["NY", "NJ"],
  "no_hazmat": true,
  "no_touch_only": false,
  "driver_assist_ok": true,
  "max_deadhead_miles": 75,
  "available_date": "2026-03-20",
  "available_time": "07:00"
}
```

### Step 5 — Profile Change Management

When carrier requests a change:
- Log previous value + timestamp + reason
- Update profile immediately
- Re-generate load board filter config
- Notify any active load searches to refresh

---

## Outputs

- Complete carrier profile record in TMS ✅
- Load board filter config object ✅
- Capability tags applied ✅
- Profile completeness score (0–100%) ✅

---

## Error Handling

| Situation | Action |
|-----------|--------|
| Required field missing | Block activation, prompt carrier |
| Rate floor below market average | Flag for review — carrier may have unrealistic expectations |
| Contradictory constraints | Flag and clarify (e.g., "no NYC" but "deliver anywhere in NE") |
| No preferred lanes | Default to all US lanes — note as "open to anything" |
