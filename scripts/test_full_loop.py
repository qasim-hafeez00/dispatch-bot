"""
scripts/test_full_loop.py

End-to-end mock integration test.
Drives a load from DAT search → broker call → carrier confirm with
zero paid API calls. Requires only: pip install -r requirements.txt

Usage:
    USE_MOCKS=true python scripts/test_full_loop.py

    # Or on Windows PowerShell:
    $env:USE_MOCKS="true"; python scripts/test_full_loop.py

Expected output (all lines with checkmarks):
    Redis initialized (mock — fakeredis)
    SQLite schema created (minimal mock DDL)
    Database connection established
    Carrier created: <uuid>
    Mock DAT search returned N loads
    Mock WhatsApp logged to console
    Mock Bland AI call initiated: MOCK-CALL-XXXXXXXX
    Mock call-complete webhook fired (2 sec delay)
    Call outcome in Redis: BOOKED at $2.80/mi
    Carrier decision published
    Full loop complete — all mocks working
"""

import asyncio
import logging
import os
import sys
import uuid

# ── Set mock flag BEFORE any cortexbot imports ────────────────
os.environ.setdefault("USE_MOCKS", "true")

# ── Add project root to path ──────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s  %(name)s  %(message)s",
)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

SEP = "-" * 60


async def main():
    print(f"\n{SEP}")
    print("CortexBot  —  Full Mock Loop Test")
    print(f"{SEP}\n")

    # ── 1. Boot infrastructure ────────────────────────────────
    print("Step 1/7 — Booting mock infrastructure...")
    from cortexbot.core.redis_client import init_redis
    from cortexbot.db.session import init_db, engine

    await init_redis()
    await init_db()
    print()

    # ── 2. Create test carrier via raw SQL ────────────────────
    # (ORM models use postgresql.UUID/JSONB; raw SQL works on SQLite)
    print("Step 2/7 — Creating test carrier in SQLite...")
    from sqlalchemy import text
    from cortexbot.db.session import get_db_session

    carrier_id = str(uuid.uuid4())
    load_id    = str(uuid.uuid4())
    carrier_mc  = f"MC-{uuid.uuid4().hex[:8].upper()}"

    async with get_db_session() as db:
        await db.execute(text("""
            INSERT INTO carriers (carrier_id, mc_number, company_name, owner_name,
                                  owner_email, owner_phone, whatsapp_phone,
                                  equipment_type, rate_floor_cpm,
                                  home_base_city, home_base_state)
            VALUES (:cid, :mc, 'Test Carrier LLC', 'John Driver',
                    'john@testcarrier.com', '+15550009999', '+15550009999',
                    '53_dry_van', 2.50, 'Dallas', 'TX')
        """), {"cid": carrier_id, "mc": carrier_mc})

        await db.execute(text("""
            INSERT INTO loads (load_id, tms_ref, carrier_id, status)
            VALUES (:lid, :tms, :cid, 'TRIAGED')
        """), {"lid": load_id, "tms": f"TMS-MOCK-{load_id[:8].upper()}", "cid": carrier_id})

    print(f"  carrier_id : {carrier_id}")
    print(f"  load_id    : {load_id}\n")

    # ── 3. Test DAT mock ──────────────────────────────────────
    print("Step 3/7 — Testing DAT load search mock...")
    from cortexbot.core.api_gateway import api_call

    dat_result = await api_call(
        "dat", "/loads/search", method="POST",
        payload={"originCity": "Dallas", "originState": "TX", "equipmentType": "Van"},
    )
    loads = dat_result.get("loads", [])
    print(f"  DAT search returned {len(loads)} loads")
    if loads:
        f = loads[0]
        print(f"  First: {f.get('dat_load_id')}  {f.get('origin_city')} → {f.get('destination_city')}  ${f.get('posted_rate_cpm')}/mi")
    print()

    # ── 4. Test rate mock ─────────────────────────────────────
    print("Step 4/7 — Testing DAT rate intelligence mock...")
    rate_result = await api_call(
        "dat_rates", "/rates/calculate", method="POST",
        payload={"originCity": "Dallas", "destinationCity": "Atlanta", "equipmentType": "Van"},
    )
    print(f"  Rate: ${rate_result.get('ratePerMile')}/mi  confidence={rate_result.get('confidence')}\n")

    # ── 5. Test Twilio mock ───────────────────────────────────
    print("Step 5/7 — Testing Twilio WhatsApp mock (see console output above)...")
    from cortexbot.integrations.twilio_client import send_whatsapp
    ok = await send_whatsapp("+15550009999", "Test load available: Dallas → Atlanta, $2.80/mi. Reply YES or NO.")
    print(f"  send_whatsapp returned: {ok}\n")

    # ── 6. Test Bland AI call mock ────────────────────────────
    print("Step 6/7 — Testing Bland AI voice call mock...")
    print("  (mock webhook fires 2 seconds after initiation...)\n")

    first_load = loads[0] if loads else {
        "dat_load_id": "DAT-TEST-001",
        "broker_phone": "+15550001234",
        "broker_company": "Test Freight LLC",
        "broker_mc": "MC-123456",
        "origin_city": "Dallas", "origin_state": "TX",
        "destination_city": "Atlanta", "destination_state": "GA",
    }

    call_state = {
        "load_id":            load_id,
        "carrier_id":         carrier_id,
        "carrier_mc":         carrier_mc,
        "carrier_email":      "john@testcarrier.com",
        "carrier_whatsapp":   "+15550009999",
        "carrier_equipment":  "53-foot dry van",
        "carrier_owner_name": "John Driver",
        "carrier_rate_floor": 2.50,
        "current_load":       first_load,
        "rate_brief": {
            "anchor_rate":    2.85,
            "counter_rate":   2.75,
            "walk_away_rate": 2.50,
            "talking_points": "DAT showing tight capacity on TX → GA",
        },
        "status": "TRIAGED", "retry_count": 0, "error_log": [],
    }

    # Save state to Redis — handle_call_complete reads it from here
    from cortexbot.core.redis_client import set_state
    await set_state(f"cortex:state:load:{load_id}", call_state)

    from cortexbot.agents.voice_calling import agent_g_voice_call
    updated = await agent_g_voice_call(call_state)

    bland_call_id = updated.get("bland_call_id", "none")
    print(f"  Call initiated: {bland_call_id}")
    print("  Waiting 4 seconds for mock webhook...")
    await asyncio.sleep(4)

    # Check Redis for call outcome written by handle_call_complete
    from cortexbot.core.redis_client import get_state
    final = await get_state(f"cortex:state:load:{load_id}")
    outcome = (final or {}).get("call_outcome", "UNKNOWN")
    rate    = (final or {}).get("agreed_rate_cpm", "?")
    print(f"  Call outcome: {outcome}  agreed rate: ${rate}/mi\n")

    # ── 7. Test carrier decision pub/sub ─────────────────────
    print("Step 7/7 — Testing carrier decision pub/sub...")
    from cortexbot.core.redis_client import publish_carrier_decision
    await publish_carrier_decision(load_id, "CONFIRMED")
    print(f"  Carrier CONFIRMED published for load {load_id[:8]}...\n")

    # ── Summary ───────────────────────────────────────────────
    all_passed = outcome == "BOOKED" and len(loads) > 0 and ok
    status_line = "ALL CHECKS PASSED" if all_passed else "SOME CHECKS FAILED — see output above"

    print(SEP)
    print(f"  {status_line}")
    print(SEP)
    print()
    print("What this validated:")
    print(f"  fakeredis     state read/write             {'OK' if final else 'FAIL'}")
    print(f"  SQLite        Carrier + Load rows          OK")
    print(f"  DAT mock      {len(loads)} loads returned from fixture  {'OK' if loads else 'FAIL'}")
    print(f"  Twilio mock   WhatsApp logged to console   {'OK' if ok else 'FAIL'}")
    print(f"  Bland AI mock call initiated + webhook     {outcome}")
    print(f"  Pub/sub       carrier decision published   OK")
    print()
    print("Next step:")
    print("  Replace one real API at a time (start with Twilio SMS ~$0.01)")
    print("  Run this script again after each swap to confirm nothing broke.")
    print(SEP)


if __name__ == "__main__":
    asyncio.run(main())
