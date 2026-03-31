---
skill_id: "20-tms-crm-logging"
name: "TMS & CRM Logging"
version: "1.0.0"
phase: 0
priority: critical
trigger: "Called by every other skill after every significant event. Runs as a background service across all phases."
inputs:
  - event_type: "string — standardized event code"
  - entity_type: "enum: carrier | load | broker | driver | invoice"
  - entity_id: "string"
  - event_data: "object — varies by event type"
  - triggered_by_skill: "string — which skill generated this log"
outputs:
  - log_entry_id: "string — unique audit trail ID"
  - entity_updated: "boolean"
  - alerts_triggered: "array of any alerts fired based on this event"
integrations:
  - TMS (primary — LoadStop, Axon, ProTransport, custom)
  - CRM (HubSpot, Salesforce, custom)
  - Document storage (S3, Google Drive, Dropbox)
  - Notification system (email, SMS, Slack)
depends_on: []
triggers_next: []
tags: [logging, audit, tms, crm, data, records, compliance]
---

# SKILL: TMS & CRM Logging
## Cortex Bot — Truck Dispatch Automation

**Trigger**: Called by every other skill after every significant event.
**Phase**: All phases (background service)
**Priority**: CRITICAL

---

## Purpose

Create a complete, timestamped audit trail of every action taken across the entire dispatch lifecycle. The TMS is the single source of truth. Every decision, document, call, message, rate, timestamp, and status change must be logged. If it's not logged, it didn't happen — and you can't prove it in a dispute.

---

## Standard Event Codes

### Carrier Events
| Code | Description |
|------|-------------|
| CARRIER_PROSPECT_ADDED | New carrier added to CRM pipeline |
| CARRIER_ONBOARDING_STARTED | Onboarding initiated |
| CARRIER_DOCS_RECEIVED | Document batch received |
| CARRIER_FMCSA_VERIFIED | FMCSA verification completed |
| CARRIER_INSURANCE_VERIFIED | Insurance confirmed with agent |
| CARRIER_ACTIVATED | Carrier set to ACTIVE status |
| CARRIER_SUSPENDED | Carrier suspended (compliance issue) |
| CARRIER_PROFILE_UPDATED | Any profile field changed |
| CARRIER_INSURANCE_EXPIRY_ALERT | Insurance approaching expiry |
| CARRIER_REJECTED | Carrier failed onboarding |

### Load Events
| Code | Description |
|------|-------------|
| LOAD_SEARCH_RUN | Load board search executed |
| LOAD_ELIGIBILITY_SCREEN | Load screened against eligibility gate |
| LOAD_RATE_RESEARCHED | Market rate pulled for lane |
| BROKER_CONTACTED | Broker call or email initiated |
| BROKER_DETAILS_GATHERED | Full load details captured |
| RATE_NEGOTIATED | Rate agreed with broker |
| CARRIER_CONFIRMATION_SENT | Load summary sent to carrier |
| CARRIER_CONFIRMED | Carrier approved the load |
| CARRIER_REJECTED_LOAD | Carrier declined + reason |
| LOAD_BOOKED | Load formally booked with broker |
| PACKET_RECEIVED | Carrier packet received from broker |
| PACKET_SUBMITTED | Carrier packet returned to broker |
| RC_RECEIVED | Rate Confirmation received |
| RC_REVIEWED | RC field-by-field review completed |
| RC_DISCREPANCY | RC field did not match verbal agreement |
| RC_SIGNED | Rate Confirmation signed and returned |
| LOAD_DISPATCHED | Driver dispatch sheet sent |
| DRIVER_ACKNOWLEDGED | Driver confirmed dispatch |

### Transit Events
| Code | Description |
|------|-------------|
| DRIVER_DEPARTED_EMPTY | Driver left previous location |
| DRIVER_ARRIVED_PICKUP | Geo-fence arrival at pickup |
| DRIVER_LOADED | Driver confirmed loaded and departed pickup |
| CHECK_CALL_COMPLETED | Scheduled check-call logged |
| CHECK_CALL_MISSED | Driver did not respond to check-call |
| DELAY_DETECTED | ETA exceeds appointment window |
| BROKER_DELAY_NOTIFIED | Broker informed of delay |
| DRIVER_ARRIVED_DELIVERY | Geo-fence arrival at delivery |
| DRIVER_DELIVERED | Driver confirmed delivery complete |
| DRIVER_DEPARTED_DELIVERY | Driver left delivery facility |

### Accessorial Events
| Code | Description |
|------|-------------|
| DETENTION_STARTED | 2-hour free window expired — billing begins |
| DETENTION_UPDATED | Hourly detention update logged |
| DETENTION_ENDED | Driver released from facility |
| LAYOVER_STARTED | Driver beginning overnight hold |
| LAYOVER_ENDED | Driver resumed after layover |
| TONU_TRIGGERED | Load cancelled after carrier dispatched |
| LUMPER_PAID | Lumper receipt collected |

### Document Events
| Code | Description |
|------|-------------|
| POD_REQUESTED | Driver asked for delivery documents |
| POD_RECEIVED | POD/BOL photos received |
| INVOICE_GENERATED | Invoice created |
| INVOICE_SUBMITTED | Invoice sent to broker/factoring |
| PAYMENT_RECEIVED | Payment confirmed |
| PAYMENT_SHORT | Payment received but short of invoice |
| DISPUTE_OPENED | Broker disputed invoice |
| DISPUTE_RESOLVED | Dispute settled |

---

## Log Entry Structure

Every log entry must contain:

```json
{
  "log_id": "LOG-20260320-00147",
  "timestamp": "2026-03-20T08:15:43Z",
  "event_code": "RATE_NEGOTIATED",
  "entity_type": "load",
  "entity_id": "TMS-2026-0392",
  "triggered_by_skill": "08-broker-negotiation",
  "actor": "cortex-bot",
  "data": {
    "broker": "Echo Global Logistics",
    "broker_contact": "Sarah Jones",
    "agreed_rate_cpm": 2.82,
    "agreed_rate_flat": 700.00,
    "detention_terms": "2 hrs free, $50/hr",
    "tonu_agreed": 150.00,
    "call_duration_seconds": 312,
    "call_recording_url": "s3://recordings/LOG-20260320-00147.mp3"
  },
  "previous_status": "SEARCHING",
  "new_status": "RATE_AGREED",
  "notes": "Broker started at $2.40, anchored at $2.82, settled at $2.82 — market was tight"
}
```

---

## TMS Load Record Lifecycle

Every load progresses through these statuses — each transition must be logged:

```
SEARCHING → ELIGIBLE → CALLING → RATE_AGREED → CARRIER_CONFIRMING →
BOOKED → PACKET_SENT → RC_REVIEW → DISPATCHED → IN_TRANSIT →
DELIVERED → INVOICED → PAID → CLOSED
```

If a load falls out of the pipeline:
```
SEARCHING → NO_LOADS (truck posted)
CALLING → RATE_FAILED (below floor)
CARRIER_CONFIRMING → CARRIER_REJECTED
BOOKED → TONU (load cancelled)
INVOICED → DISPUTED
```

---

## CRM Records (Carrier & Broker Relationship Data)

### Per Carrier
- Total loads dispatched
- Total revenue generated
- Average CPM achieved
- On-time percentage
- Check-call compliance rate
- Document submission speed
- Load acceptance rate
- Rejection reasons history
- Reliability score (0–100)

### Per Broker
- Total loads booked with this broker
- Average rate vs market (how much above/below market they pay)
- Average days to pay
- Dispute rate
- Credit score history
- Relationship strength score
- Best contacts at broker
- Notes from each interaction

---

## Alerting Rules

Log entries trigger alerts when:
- Carrier compliance document expires → `26-compliance-monitoring` alerted
- Check-call missed × 3 → owner called immediately
- Payment overdue → `19-payment-reconciliation` escalation triggered
- Driver HOS < 2 hours → `14-hos-compliance` alert sent
- Broker credit score drops below 60 → flag for future loads

---

## Error Handling

| Situation | Action |
|-----------|--------|
| TMS API unavailable | Queue logs locally; sync when connection restored |
| Duplicate log entry | Deduplicate by event_code + entity_id + timestamp window |
| Missing required field | Log with error flag; alert for manual completion |
| Log volume spike | Investigate — may indicate runaway loop or system error |
