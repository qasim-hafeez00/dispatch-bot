#!/usr/bin/env python3
"""
scripts/seed_test_carrier.py

Seeds the database with a complete test carrier, broker, and an
in-progress load so you can immediately test Phase 2 features.

Usage:
    docker compose exec cortexbot-api python scripts/seed_test_carrier.py

What it creates:
  1. Test carrier — ABC Trucking LLC (MC-999001) with full profile
  2. Test broker   — Echo Global Logistics (MC-888001)
  3. Test broker contact — Sarah Jones
  4. Active load   — Nashville TN → Atlanta GA, $2.82/mi, status=DISPATCHED
  5. Simulated events for the full Phase 1 timeline

After running:
  - GET /api/carriers           → see test carrier
  - GET /api/loads              → see active load
  - POST /debug/simulate/carrier-yes/{load_id} → advance workflow
  - WhatsApp "DELIVERED" to carrier number → trigger Phase 2
"""

import asyncio
import uuid
import sys
import os
from datetime import datetime, timezone, timedelta, date

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import text
import json

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://cortex:cortex@localhost:5432/cortexbot"
)

engine  = create_async_engine(DATABASE_URL, echo=False)
Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def seed_all():
    async with Session() as db:
        print("🌱 Seeding CortexBot Phase 2 test data...")

        # ── 1. Test Carrier ──────────────────────────────────
        carrier_id = str(uuid.uuid4())
        load_id    = str(uuid.uuid4())
        broker_id  = str(uuid.uuid4())

        now = datetime.now(timezone.utc)

        # Check if test carrier already exists
        existing = await db.execute(
            text("SELECT carrier_id FROM carriers WHERE mc_number = 'MC-999001'")
        )
        row = existing.fetchone()
        if row:
            carrier_id = str(row[0])
            print(f"✅ Test carrier already exists: {carrier_id}")
        else:
            await db.execute(text("""
                INSERT INTO carriers (
                    carrier_id, mc_number, dot_number, company_name, owner_name,
                    owner_email, owner_phone, driver_phone, whatsapp_phone,
                    language_pref, equipment_type, max_weight_lbs,
                    home_base_city, home_base_state,
                    preferred_dest_states, avoid_states,
                    rate_floor_cpm, max_deadhead_mi,
                    no_touch_only, hazmat_cert, twic_card,
                    status, dispatch_fee_pct, created_at, updated_at
                ) VALUES (
                    :carrier_id, 'MC-999001', 'DOT-12345678', 'ABC Trucking LLC', 'John Smith',
                    'john@abctrucking.com', '+15551000001', '+15551000001', '+15551000001',
                    'en', '53_dry_van', 44000,
                    'Nashville', 'TN',
                    ARRAY['GA','FL','SC','NC','VA','TN'], ARRAY['NY','NJ'],
                    2.25, 100,
                    false, false, false,
                    'ACTIVE', 0.060, :now, :now
                )
            """), {"carrier_id": carrier_id, "now": now})
            print(f"✅ Created test carrier: {carrier_id}")

        # ── 2. Test Broker ───────────────────────────────────
        existing_broker = await db.execute(
            text("SELECT broker_id FROM brokers WHERE mc_number = 'MC-888001'")
        )
        row = existing_broker.fetchone()
        if row:
            broker_id = str(row[0])
            print(f"✅ Test broker already exists: {broker_id}")
        else:
            await db.execute(text("""
                INSERT INTO brokers (
                    broker_id, mc_number, company_name,
                    dat_credit_score, avg_days_to_pay,
                    relationship_tier, blacklisted, loads_booked,
                    created_at, updated_at
                ) VALUES (
                    :broker_id, 'MC-888001', 'Echo Global Logistics',
                    85, 18,
                    'PREFERRED', false, 47,
                    :now, :now
                )
            """), {"broker_id": broker_id, "now": now})

            # Test broker contact
            contact_id = str(uuid.uuid4())
            await db.execute(text("""
                INSERT INTO broker_contacts (
                    contact_id, broker_id, name, phone, email,
                    best_lanes, equipment_focus, notes, created_at
                ) VALUES (
                    :contact_id, :broker_id, 'Sarah Jones', '+15559000001',
                    'sarah.jones@echo.com',
                    ARRAY['Southeast', 'Midwest'], 'dry_van',
                    'Quick to approve rate increases on tight market days.',
                    :now
                )
            """), {"contact_id": contact_id, "broker_id": broker_id, "now": now})
            print(f"✅ Created test broker: {broker_id}")

        # ── 3. Test Load (DISPATCHED) ─────────────────────────
        existing_load = await db.execute(
            text("SELECT load_id FROM loads WHERE tms_ref = 'TMS-2026-0001'")
        )
        row = existing_load.fetchone()

        if row:
            load_id = str(row[0])
            print(f"✅ Test load already exists: {load_id}")
        else:
            pickup_date   = (now + timedelta(days=1)).date()
            delivery_date = (now + timedelta(days=1)).date()

            await db.execute(text("""
                INSERT INTO loads (
                    load_id, tms_ref, carrier_id, broker_id,
                    status, broker_load_ref, bland_call_id,
                    origin_address, origin_city, origin_state, origin_zip,
                    destination_address, destination_city, destination_state, destination_zip,
                    loaded_miles, deadhead_miles,
                    pickup_date, pickup_appt_type, pickup_appt_time,
                    delivery_date, delivery_appt_type, delivery_appt_time,
                    commodity, weight_lbs, piece_count,
                    equipment_type, load_type, unload_type,
                    agreed_rate_cpm, agreed_rate_flat,
                    detention_free_hrs, detention_rate_hr, tonu_amount,
                    tracking_method, payment_terms_days,
                    factoring_allowed,
                    market_rate_cpm, anchor_rate_cpm,
                    searched_at, broker_called_at, rate_agreed_at,
                    carrier_confirmed_at, booked_at, rc_received_at,
                    rc_signed_at, dispatched_at,
                    created_at, updated_at
                ) VALUES (
                    :load_id, 'TMS-2026-0001', :carrier_id, :broker_id,
                    'DISPATCHED', 'ECHO-123456', 'BLAND-TEST-CALL-001',
                    '123 Shipper Way, Nashville, TN 37201', 'Nashville', 'TN', '37201',
                    '456 Receiver Blvd, Atlanta, GA 30301', 'Atlanta', 'GA', '30301',
                    248, 12,
                    :pickup_date, 'appointment', '09:00',
                    :delivery_date, 'appointment', '17:00',
                    'Dry Goods (Pallets)', 42000, 26,
                    '53_dry_van', 'live', 'live_unload',
                    2.820, 700.00,
                    2, 50.00, 150.00,
                    'Macropoint', 30,
                    true,
                    2.45, 2.82,
                    :now, :now, :now,
                    :now, :now, :now,
                    :now, :now,
                    :now, :now
                )
            """), {
                "load_id": load_id, "carrier_id": carrier_id, "broker_id": broker_id,
                "pickup_date": pickup_date, "delivery_date": delivery_date, "now": now,
            })
            print(f"✅ Created test load: {load_id} (TMS-2026-0001)")

        # ── 4. Seed events (full Phase 1 timeline) ────────────
        existing_events = await db.execute(
            text("SELECT COUNT(*) FROM events WHERE entity_id = :load_id"),
            {"load_id": load_id}
        )
        event_count = existing_events.scalar()

        if event_count == 0:
            event_timeline = [
                ("LOAD_SEARCH_STARTED",      now - timedelta(hours=3),  "SEARCHING",        {}),
                ("LOAD_SEARCH_RUN",           now - timedelta(hours=3),  "SEARCHING",        {"loads_found": 8}),
                ("BROKER_CONTACTED",          now - timedelta(hours=2, minutes=45), "CALLING", {"broker_phone": "+15559000001"}),
                ("BROKER_CALL_COMPLETED",     now - timedelta(hours=2, minutes=30), "RATE_AGREED", {
                    "outcome": "BOOKED", "agreed_rate_cpm": 2.82, "duration": 312
                }),
                ("CARRIER_CONFIRMATION_SENT", now - timedelta(hours=2, minutes=25), "CARRIER_CONFIRMING", {}),
                ("CARRIER_DECISION",          now - timedelta(hours=2, minutes=23), "CONFIRMED",          {"decision": "CONFIRMED"}),
                ("LOAD_BOOKED",               now - timedelta(hours=2, minutes=20), "BOOKED",             {"agreed_rate_cpm": 2.82}),
                ("PACKET_SUBMITTED",          now - timedelta(hours=2, minutes=5),  "PACKET_SENT",        {}),
                ("RC_RECEIVED",               now - timedelta(hours=1, minutes=45), "RC_RECEIVED",        {}),
                ("RC_SIGNED",                 now - timedelta(hours=1, minutes=30), "RC_SIGNED",          {}),
                ("LOAD_DISPATCHED",           now - timedelta(hours=1),             "DISPATCHED",         {}),
            ]

            for code, ts, new_status, data in event_timeline:
                event_id = str(uuid.uuid4())
                await db.execute(text("""
                    INSERT INTO events (
                        event_id, event_code, entity_type, entity_id,
                        triggered_by, actor, data, new_status, created_at
                    ) VALUES (
                        :event_id, :code, 'load', :load_id,
                        'seed_script', 'system', :data, :status, :ts
                    )
                """), {
                    "event_id": event_id, "code": code, "load_id": load_id,
                    "data": json.dumps(data), "status": new_status, "ts": ts,
                })

            print(f"✅ Created {len(event_timeline)} timeline events for load {load_id}")

        # ── 5. WhatsApp context for test carrier ─────────────
        await db.execute(text("""
            INSERT INTO whatsapp_context (phone, carrier_id, current_load_id, awaiting, language, updated_at)
            VALUES ('+15551000001', :carrier_id, :load_id, NULL, 'en', :now)
            ON CONFLICT (phone) DO UPDATE SET
                carrier_id = :carrier_id,
                current_load_id = :load_id,
                awaiting = NULL,
                updated_at = :now
        """), {"carrier_id": carrier_id, "load_id": load_id, "now": now})
        print(f"✅ WhatsApp context set for +15551000001")

        await db.commit()

        print("\n" + "="*60)
        print("🚛 CORTEXBOT PHASE 2 — TEST DATA READY")
        print("="*60)
        print(f"\nCarrier ID:  {carrier_id}")
        print(f"MC Number:   MC-999001")
        print(f"Load ID:     {load_id}")
        print(f"TMS Ref:     TMS-2026-0001")
        print(f"Status:      DISPATCHED")
        print(f"Route:       Nashville TN → Atlanta GA")
        print(f"Rate:        $2.82/mile ($700 flat)")
        print("\nTest commands:")
        print(f"  curl http://localhost:8000/api/loads/{load_id}")
        print(f"  curl -X POST http://localhost:8000/debug/simulate/whatsapp \\")
        print(f"       -H 'Content-Type: application/json' \\")
        print(f"       -d '{{\"from\": \"+15551000001\", \"body\": \"DELIVERED\"}}'")
        print(f"\n  # Trigger payment pipeline manually:")
        print(f"  curl -X POST http://localhost:8000/internal/payment-followup \\")
        print(f"       -d '{{\"load_id\": \"{load_id}\", \"amount_paid\": 787.50}}'")
        print("="*60)


if __name__ == "__main__":
    asyncio.run(seed_all())
