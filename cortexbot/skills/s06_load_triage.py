import logging
from typing import List

logger = logging.getLogger("cortexbot.skills.s06_triage")

async def skill_06_load_triage(state: dict) -> dict:
    """
    Skill 06 — Load Triage
    
    Evaluates raw loads against carrier profile criteria:
      - Equipment Type Match (V, R, F, etc.)
      - Max Weight
      - Hazmat Requirements
      - Avoid States / Preferred States
    
    Loads that pass are sorted by preference and stored in load_queue.
    """
    logger.info(f"🚦 Triaging {len(state.get('raw_loads', []))} loads for {state.get('carrier_id')}")

    raw_loads = state.get("raw_loads", [])
    carrier_profile = state.get("carrier_profile", {})
    
    max_weight = carrier_profile.get("max_weight_lbs", 44000)
    has_hazmat = carrier_profile.get("hazmat_cert", False)
    avoid_states = set(s.upper() for s in carrier_profile.get("avoid_states", []))
    preferred_dest_states = set(s.upper() for s in carrier_profile.get("preferred_dest_states", []))
    equipment_type = carrier_profile.get("equipment_type", "").upper()
    
    eligible_loads = []
    
    for load in raw_loads:
        # 1. Equipment Match (basic check)
        load_equip = str(load.get("equipment_type", "")).upper()
        if equipment_type and equipment_type not in load_equip and load_equip not in equipment_type:
            # Simple substring match (e.g. 'V' in 'VAN', 'R' in 'REEFER')
            # If no overlap and not explicitly empty, we might skip, but let's be forgiving if load_equip is unknown
            if load_equip:
                logger.debug(f"Skipping load {load.get('id')}: Equipment mismatch ({load_equip} != {equipment_type})")
                continue
                
        # 2. Weight Check
        load_weight = float(load.get("weight_lbs") or 0)
        if load_weight > max_weight:
            logger.debug(f"Skipping load {load.get('id')}: Overweight ({load_weight} > {max_weight})")
            continue
            
        # 3. Hazmat Check
        if str(load.get("commodity", "")).upper().find("HAZMAT") != -1 and not has_hazmat:
            logger.debug(f"Skipping load {load.get('id')}: Hazmat required")
            continue
            
        # 4. Avoid States
        dest_state = str(load.get("destination_state", "")).upper()
        if dest_state in avoid_states:
            logger.debug(f"Skipping load {load.get('id')}: Destination in avoid_states ({dest_state})")
            continue
            
        # Calculate preference score
        score = 0
        if dest_state in preferred_dest_states:
            score += 10
            
        # Preference to loads with posted rates
        if load.get("posted_rate"):
            score += 5
            
        eligible_loads.append({
            "load": load,
            "score": score
        })
        
    # Sort loads by score descending
    eligible_loads.sort(key=lambda x: x["score"], reverse=True)
    load_queue = [item["load"] for item in eligible_loads]
    
    if load_queue:
        logger.info(f"✅ Triage complete: {len(load_queue)} loads eligible")
        return {
            **state,
            "status": "ELIGIBLE",
            "load_queue": load_queue,
            "eligible_loads": True,
            "current_load": load_queue[0]
        }
    else:
        logger.warning(f"❌ Triage complete: 0 loads eligible")
        return {
            **state,
            "status": "NO_ELIGIBLE_LOADS",
            "load_queue": [],
            "eligible_loads": False,
            "current_load": None
        }
