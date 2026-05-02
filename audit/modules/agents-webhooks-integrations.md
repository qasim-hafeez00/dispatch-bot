# cortexbot/agents/, /webhooks/, /integrations/ — Agents, Webhooks, Integrations

---

## agents/ — Specialized Processing Agents

11 files. Agents are heavier modules called by skills or orchestrated independently.

| File | Purpose | Key function |
|---|---|---|
| `voice_calling.py` | Bland AI call orchestration (Stage 1–5: open, gather, negotiate, hold, close) | `agent_g_voice_call(state)` |
| `document_ocr.py` | Claude Vision OCR for RC/BOL/POD documents | `extract_document_fields(pdf_bytes, doc_type)` |
| `email_parser.py` | Email classification + field extraction | `parse_inbound_email(from, subject, body, attachments)` |
| `escalation.py` | Human escalation dispatch via phone/SMS/email | `run_minimal_escalation(state)` |
| `emergency_rebroker.py` | Emergency rebrokering when carrier drops load | `run_emergency_rebrokering(state)` |
| `cargo_theft.py` | Cargo theft incident response | `handle_cargo_theft(state)` |
| `disaster_recovery.py` | Load rescue after system failure | `recover_loads()` |
| `gdpr_ccpa.py` | Data deletion / privacy compliance | `process_deletion_request(carrier_id)` |
| `system_health.py` | System health snapshot | `get_health_snapshot()` |
| `service_agreement.py` | Dispatcher service agreement generation | `generate_service_agreement(carrier)` |

### Key agent notes

**document_ocr.py** (BUG-11 fix):
- Uses PyMuPDF (`fitz`) to render PDF pages to JPEG
- Sends up to 3 pages as separate base64-encoded image content blocks to Claude Vision (Anthropic SDK)
- `RC_EXTRACT_PROMPT` tells Claude to extract: rate, pickup/delivery dates, commodity, weight, equipment, broker reference

**voice_calling.py**:
- 5-stage call flow with Bland AI
- Returns immediately with `status=CALLING` (async Bland AI calls webhook on completion)
- `agent_g_voice_call(state)` is the node function
- `handle_call_complete(payload)` processes Bland AI webhook (delegates to `bland_ai.py`)

**escalation.py**:
- `run_minimal_escalation(state)` is called by all failure routing paths
- Sends SMS + email + WhatsApp to on-call dispatcher
- Returns `status=ESCALATED`

---

## webhooks/ — Inbound Webhook Handlers

8 files. All receive HTTP POST from external services.

| File | Handler function | External service |
|---|---|---|
| `bland.py` | `handle_bland_webhook(payload)` | Bland AI (call completion) |
| `bland_ai.py` | `handle_call_complete(payload)` | Bland AI (legacy, delegates to `voice_calling.py`) |
| `eld_webhooks.py` | `handle_samsara_webhook(payload, sig, body)`, `handle_motive_webhook(...)` | Samsara / Motive ELD |
| `sendgrid.py` | processes inbound email events | SendGrid Inbound Parse |
| `twilio.py` | processes WhatsApp/SMS messages | Twilio |
| `docusign.py` | processes signature complete events | DocuSign |
| `others.py` | miscellaneous webhook handlers | Various |

### ELD webhook details (`eld_webhooks.py`)

**Phase 3E hardening:**
- HMAC-SHA256 signature verification (accepts if secret not configured)
- Redis dedup: `sha256(provider+event_type+vehicle_id+minute_bucket)` with 5-min NX TTL
- Samsara event types handled: `VehicleLocation`, `AddressArrival`, `AddressDeparture`, `DriverHosStatusChanged`, `VehicleBreakdown`, `DVIRDefectReported`
- Motive event types: `location_update`, `geofence_arrival`, `geofence_departure`, `hos_event`, `vehicle_breakdown`
- Geofence naming convention: `CortexBot-{TMS_REF}:PICKUP` and `CortexBot-{TMS_REF}:DELIVERY`
- Arrival → `start_detention_clock()`, updates DB `DetentionRecord`
- Departure → `stop_detention_clock()`, calculates billable hours, updates Load

**Gap**: Test sends `eventType: "geofenceEntry"` (incorrect). Fixed to send `"AddressArrival"` in test. Production ELD events must use Samsara/Motive native event names.

### Bland webhook details (`bland.py`)

**New in Phase 3E session** (created alongside `bland_ai.py`):
- Top-level import of `resume_workflow_after_call` allows test mocking
- Extracts `call_outcome`, `agreed_rate_cpm`, `broker_contact_name` from Bland AI payload
- Calls `resume_workflow_after_call(load_id, updated_state)`

**Note**: Two bland webhook files now exist:
- `bland.py` — used by tests and future route wiring
- `bland_ai.py` — legacy, delegates to `voice_calling.py`  
`main.py` likely uses `bland_ai.py`; should be unified.

### SendGrid webhook details (`sendgrid.py`)

- `_classify_email(from_email, subject, body)` returns `(category, confidence)`
- Categories: `RC` (Rate Confirmation), `PAYMENT` (Remittance), `CARRIER_PACKET`
- Keyword-based classification with confidence scoring
- Triggers `dispatch_event("RC_RECEIVED", {...})` on RC emails

---

## integrations/ — Third-Party API Clients

10 files. Each wraps one or more external APIs with auth, retry, and error handling.

| File | Service | Key function |
|---|---|---|
| `docusign_client.py` | DocuSign + S3 | `sign_document(pdf_bytes, signer_email)` → pre-signed URL |
| `eld_adapter.py` | Samsara / Motive (unified) | `get_hos(carrier)`, `get_location(carrier)`, `register_geofence(carrier, load)` |
| `sendgrid_client.py` | SendGrid email | `send_email(to, subject, body, attachments)` |
| `twilio_client.py` | Twilio WhatsApp/SMS | `send_whatsapp(to, body)`, `send_sms(to, body)` |
| `stripe_client.py` | Stripe Issuing | `collect_dispatcher_fee(carrier, amount)`, `transfer_settlement(carrier, amount)` |
| `quickbooks_client.py` | QuickBooks Online | `create_invoice(load)`, `sync_payment(invoice_id, amount)` |
| `factoring_client.py` | OTR Capital / RTS Financial | `submit_invoice_for_factoring(invoice)` |
| `comdata_efs_client.py` | Comdata / EFS fuel cards | `issue_advance(carrier, amount)`, `get_transaction_history(carrier)` |
| `weather_client.py` | NOAA + weather APIs | `get_route_weather(origin, destination, pickup_date)` |
| `placeholders.py` | Stub clients for unimplemented integrations | Various no-op stubs |

### docusign_client.py (BUG-9 fix)

- `_sign_locally()` fallback generates real PDF with reportlab
- Uploads to S3 at `loads/{load_id}/RC_signed_{timestamp}.pdf`
- Returns a 7-day **pre-signed HTTPS URL** (not `s3://` URI) so brokers can download from email links
- Mock (`docusign_mock.py`) returns `http://localhost/mock-s3/{key}` to match production format

### eld_adapter.py

- Unified interface over Samsara and Motive
- Provider selection from `carrier.eld_provider` (defaults to `settings.default_eld_provider`)
- Translates provider-specific responses into unified format
