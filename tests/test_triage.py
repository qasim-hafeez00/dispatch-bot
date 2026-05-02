"""
tests/test_triage.py

Tests for Skill 06 — Load Triage (s06_load_triage.py).

Focus: equipment type matching including the BUG-10 fix that rejects
loads with empty equipmentType when the carrier has a specific requirement.
"""

import pytest
from cortexbot.skills.s06_load_triage import skill_06_load_triage


def _make_state(equipment_type: str, loads: list) -> dict:
    return {
        "carrier_id": "c-001",
        "carrier_profile": {
            "equipment_type":  equipment_type,
            "max_weight_lbs":  44000,
            "hazmat_cert":     False,
            "avoid_states":    [],
            "preferred_dest_states": [],
        },
        "raw_loads": loads,
    }


def _load(equipment: str = "DRY_VAN", weight: int = 40000, dest_state: str = "GA") -> dict:
    return {
        "id":               f"load-{equipment}",
        "equipment_type":   equipment,
        "weight_lbs":       weight,
        "commodity":        "General Freight",
        "destination_state": dest_state,
        "posted_rate":      3.00,
    }


# ─────────────────────────────────────────────────────────────
# Equipment matching
# ─────────────────────────────────────────────────────────────

async def test_matching_equipment_passes():
    state = _make_state("DRY_VAN", [_load("DRY_VAN")])
    result = await skill_06_load_triage(state)
    assert result["status"] == "ELIGIBLE"
    assert len(result["load_queue"]) == 1


async def test_mismatched_equipment_rejected():
    state = _make_state("DRY_VAN", [_load("REEFER")])
    result = await skill_06_load_triage(state)
    assert result["status"] == "NO_ELIGIBLE_LOADS"
    assert result["load_queue"] == []


async def test_empty_equipment_on_load_rejected(base_state):
    """BUG-10 fix: loads with no equipment type must be rejected when carrier specifies one."""
    state = _make_state("DRY_VAN", [_load("")])
    result = await skill_06_load_triage(state)
    assert result["status"] == "NO_ELIGIBLE_LOADS", (
        "Load with empty equipmentType should be rejected when carrier requires DRY_VAN"
    )


async def test_no_carrier_equipment_pref_passes_anything():
    """If the carrier has no equipment preference, any load should pass."""
    state = _make_state("", [_load("REEFER"), _load("FLATBED"), _load("")])
    result = await skill_06_load_triage(state)
    assert result["status"] == "ELIGIBLE"
    assert len(result["load_queue"]) == 3


# ─────────────────────────────────────────────────────────────
# Weight filtering
# ─────────────────────────────────────────────────────────────

async def test_overweight_load_rejected():
    state = _make_state("DRY_VAN", [_load(weight=50000)])
    result = await skill_06_load_triage(state)
    assert result["status"] == "NO_ELIGIBLE_LOADS"


async def test_max_weight_load_accepted():
    state = _make_state("DRY_VAN", [_load(weight=44000)])
    result = await skill_06_load_triage(state)
    assert result["status"] == "ELIGIBLE"


# ─────────────────────────────────────────────────────────────
# Hazmat filtering
# ─────────────────────────────────────────────────────────────

async def test_hazmat_load_rejected_without_cert():
    load = _load()
    load["commodity"] = "HAZMAT Chemicals"
    state = _make_state("DRY_VAN", [load])
    result = await skill_06_load_triage(state)
    assert result["status"] == "NO_ELIGIBLE_LOADS"


async def test_hazmat_load_accepted_with_cert():
    load = _load()
    load["commodity"] = "HAZMAT Chemicals"
    state = _make_state("DRY_VAN", [load])
    state["carrier_profile"]["hazmat_cert"] = True
    result = await skill_06_load_triage(state)
    assert result["status"] == "ELIGIBLE"


# ─────────────────────────────────────────────────────────────
# Avoid states
# ─────────────────────────────────────────────────────────────

async def test_avoid_state_rejects_load():
    state = _make_state("DRY_VAN", [_load(dest_state="CA")])
    state["carrier_profile"]["avoid_states"] = ["CA"]
    result = await skill_06_load_triage(state)
    assert result["status"] == "NO_ELIGIBLE_LOADS"


# ─────────────────────────────────────────────────────────────
# Score ordering
# ─────────────────────────────────────────────────────────────

async def test_preferred_state_scores_higher():
    load_preferred = _load(dest_state="TX")
    load_preferred["id"] = "preferred"
    load_other = _load(dest_state="OH")
    load_other["id"] = "other"
    load_other["posted_rate"] = None  # no rate bonus

    state = _make_state("DRY_VAN", [load_other, load_preferred])
    state["carrier_profile"]["preferred_dest_states"] = ["TX"]

    result = await skill_06_load_triage(state)
    assert result["current_load"]["id"] == "preferred", (
        "Preferred-state load should rank first"
    )
