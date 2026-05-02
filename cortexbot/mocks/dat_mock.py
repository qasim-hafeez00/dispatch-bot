"""
cortexbot/mocks/dat_mock.py

Returns realistic DAT load data from a local JSON fixture.
Rate data is randomised within a realistic band per lane.
"""
import json
import logging
import os
import random

logger = logging.getLogger("mock.dat")

_FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "dat_loads.json")


def _loads() -> list:
    with open(_FIXTURE_PATH) as f:
        return json.load(f)


async def mock_dat_search(origin_city: str = "", origin_state: str = "", **kwargs) -> dict:
    all_loads = _loads()
    # Filter loosely by origin state when provided
    if origin_state:
        filtered = [l for l in all_loads if l.get("origin_state", "").upper() == origin_state.upper()]
        pool = filtered if filtered else all_loads
    else:
        pool = all_loads
    sample = random.sample(pool, min(len(pool), 20))
    logger.info("[MOCK DAT] returning %d loads for %s, %s", len(sample), origin_city, origin_state)
    return {"loads": sample, "total": len(sample)}


async def mock_dat_rate(origin_city: str, dest_city: str, equipment: str = "Van") -> dict:
    rate = round(random.uniform(2.40, 3.20), 2)
    logger.info("[MOCK DAT rates] %s → %s  $%.2f/mi", origin_city, dest_city, rate)
    return {
        "ratePerMile":   rate,
        "confidence":    "HIGH",
        "sampleSize":    random.randint(12, 55),
        "origin":        origin_city,
        "destination":   dest_city,
        "equipmentType": equipment,
    }
