"""
cortexbot/skills/s23_weather_monitoring.py

Skill 23 — Weather Risk Monitoring

FIX: This file was completely empty in the repository.
"""

from cortexbot.skills.s21_s22_s23_ops import skill_23_weather_monitoring

__all__ = ["skill_23_weather_monitoring"]


async def skill_23_weather_check(load_id: str) -> dict:
    """
    Entry point called by BullMQ worker via /internal/weather-check.
    Loads state from Redis and runs the weather monitoring skill.
    """
    from cortexbot.core.redis_client import get_state, set_state

    state = await get_state(f"cortex:state:load:{load_id}")
    if not state:
        return {"error": f"No state found for load {load_id}"}

    updated = await skill_23_weather_monitoring(state)
    await set_state(f"cortex:state:load:{load_id}", updated)
    return updated