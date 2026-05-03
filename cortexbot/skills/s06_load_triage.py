"""
cortexbot/skills/s06_load_triage.py

Skill 06 — Load Triage

Evaluates raw loads against carrier profile criteria.

GAP FIXES:
  - Rate floor check added: loads paying below carrier's rate_floor_cpm are rejected
  - TWIC card filter: if load requires TWIC and carrier has no TWIC, rejected
  - No-touch filter: if carrier is no_touch_only and load has driver_assist, rejected
  - Team-only filter: if load requires team and carrier is not team_capable, rejected
  - Commodity exclusions: carrier can blacklist specific commodities beyond hazmat
  - Length filter: loads exceeding carrier's max trailer length are rejected
  - Fixed: posted_rate key was 'posted_rate' — corrected to 'posted_rate_cpm'
  - Score improvements: rate premium above floor, drop-and-hook preference, preferred lanes
"""

import logging

logger = logging.getLogger("cortexbot.skills.s06_triage")


async def skill_06_load_triage(state: dict) -> dict:
    """
    Triage raw loads against carrier constraints.
    Passing loads are scored and sorted — best load becomes current_load.
    """
    raw_loads = state.get("raw_loads", [])
    carrier_profile = state.get("carrier_profile", {})

    logger.info(f"🚦 [S06] Triaging {len(raw_loads)} loads for {state.get('carrier_id')}")

    max_weight          = carrier_profile.get("max_weight_lbs", 44000)
    has_hazmat          = carrier_profile.get("hazmat_cert", False)
    has_twic            = carrier_profile.get("twic_card", False)
    no_touch_only       = carrier_profile.get("no_touch_only", False)
    team_capable        = carrier_profile.get("team_capable", False)
    avoid_states        = set(s.upper() for s in carrier_profile.get("avoid_states", []))
    preferred_states    = set(s.upper() for s in carrier_profile.get("preferred_dest_states", []))
    equipment_type      = carrier_profile.get("equipment_type", "").upper()
    rate_floor_cpm      = float(carrier_profile.get("rate_floor_cpm", 0.0))
    commodity_excl      = set(
        c.upper() for c in carrier_profile.get("commodity_exclusions", [])
    )
    max_length_ft       = carrier_profile.get("max_loaded_length_ft") or 53

    eligible_loads = []

    for load in raw_loads:
        load_id = load.get("dat_load_id") or load.get("id", "")
        reqs    = load.get("load_requirements", {})
        commodity_raw = str(load.get("commodity", "")).upper()

        # 1. Equipment match
        load_equip = str(load.get("equipment_type", "")).upper()
        if equipment_type:
            if not load_equip or (
                equipment_type not in load_equip and load_equip not in equipment_type
            ):
                logger.debug(f"[S06] {load_id}: equipment mismatch ({load_equip!r})")
                continue

        # 2. Weight check
        load_weight = float(load.get("weight_lbs") or 0)
        if load_weight > max_weight:
            logger.debug(f"[S06] {load_id}: overweight ({load_weight} > {max_weight})")
            continue

        # 3. Hazmat
        is_hazmat = "HAZMAT" in commodity_raw or reqs.get("hazmat", False)
        if is_hazmat and not has_hazmat:
            logger.debug(f"[S06] {load_id}: hazmat cert required")
            continue

        # 4. TWIC card
        requires_twic = reqs.get("twic", False) or "TWIC" in commodity_raw
        if requires_twic and not has_twic:
            logger.debug(f"[S06] {load_id}: TWIC required, carrier lacks it")
            continue

        # 5. No-touch / driver assist
        load_driver_assist = load.get("driver_assist", False) or reqs.get("driver_assist", False)
        if no_touch_only and load_driver_assist:
            logger.debug(f"[S06] {load_id}: driver assist required, carrier is no-touch")
            continue

        # 6. Team-only loads
        requires_team = reqs.get("team_required", False) or reqs.get("team", False)
        if requires_team and not team_capable:
            logger.debug(f"[S06] {load_id}: team driver required")
            continue

        # 7. Avoid states
        dest_state = str(load.get("destination_state", "")).upper()
        if dest_state in avoid_states:
            logger.debug(f"[S06] {load_id}: destination in avoid_states ({dest_state})")
            continue

        # 8. Commodity exclusions (carrier-defined blacklist)
        if commodity_excl and any(ex in commodity_raw for ex in commodity_excl):
            logger.debug(f"[S06] {load_id}: commodity excluded ({commodity_raw})")
            continue

        # 9. Length check (flatbed / step-deck especially)
        load_length = float(load.get("length_ft") or 0)
        if load_length and load_length > max_length_ft:
            logger.debug(f"[S06] {load_id}: length {load_length}ft exceeds max {max_length_ft}ft")
            continue

        # 10. Rate floor check — FIX: was missing entirely
        #     Use posted_rate_cpm when available; otherwise defer to post-call check.
        posted_rate = load.get("posted_rate_cpm")  # FIX: was load.get("posted_rate")
        if posted_rate and float(posted_rate) < rate_floor_cpm:
            logger.debug(
                f"[S06] {load_id}: posted rate ${posted_rate:.2f}/mi below floor "
                f"${rate_floor_cpm:.2f}/mi"
            )
            continue

        # ── Scoring ──────────────────────────────────────────────
        score = 0

        # Preferred destination
        if dest_state in preferred_states:
            score += 20

        # Rate premium above floor
        if posted_rate:
            margin = float(posted_rate) - rate_floor_cpm
            if margin > 0:
                score += min(int(margin * 10), 30)  # up to +30 for very high rates

        # Drop-and-hook is operationally efficient (no wait time)
        if load.get("drop_and_hook", False):
            score += 15

        # Quick-pay option reduces cash-flow risk
        if load.get("quick_pay_available", False):
            score += 5

        eligible_loads.append({"load": load, "score": score})

    # Sort by score descending
    eligible_loads.sort(key=lambda x: x["score"], reverse=True)
    load_queue = [item["load"] for item in eligible_loads]

    if load_queue:
        logger.info(f"✅ [S06] {len(load_queue)} eligible loads (top score={eligible_loads[0]['score']})")
        return {
            **state,
            "status":         "ELIGIBLE",
            "load_queue":     load_queue,
            "eligible_loads": True,
            "current_load":   load_queue[0],
        }
    else:
        logger.warning(f"❌ [S06] 0 eligible loads after triage")
        return {
            **state,
            "status":         "NO_ELIGIBLE_LOADS",
            "load_queue":     [],
            "eligible_loads": False,
            "current_load":   None,
        }
