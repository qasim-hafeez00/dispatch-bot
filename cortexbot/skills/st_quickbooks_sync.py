"""
cortexbot/skills/st_quickbooks_sync.py — PHASE 3A FIXED

PHASE 3A FIX (GAP-07): Dual-definition of skill_t_quickbooks_sync.

Previously two files defined skill_t_quickbooks_sync with different signatures:

  sq_sr_ss_st_financial.py:
    async def skill_t_quickbooks_sync(event_type: str, state: dict) -> dict  # 2-arg

  st_quickbooks_sync.py:
    async def skill_t_quickbooks_sync(state: dict) -> dict                    # 1-arg

orchestrator.py imports from st_quickbooks_sync (1-arg, correct).
orchestrator_phase2.py imports from sq_sr_ss_st_financial (2-arg, correct).

Both call sites were individually correct but the dual definition was a
maintenance timebomb — easy to accidentally import the wrong one.

Fix:
  1. This file (st_quickbooks_sync.py) is the canonical 1-arg orchestrator
     entry point. It loops through all financial event types and delegates
     to the 2-arg helper in sq_sr_ss_st_financial.py.
  2. orchestrator_phase2.py's import of skill_t_quickbooks_sync from
     sq_sr_ss_st_financial is explicitly kept for its 2-arg call sites.
  3. A clear comment in sq_sr_ss_st_financial marks the 2-arg function as
     "internal helper — use st_quickbooks_sync for orchestrator integration".
"""

import logging
from datetime import datetime, timezone

from cortexbot.db.session import get_db_session
from cortexbot.db.models import Event

logger = logging.getLogger("cortexbot.skills.st")

# QuickBooks account code mapping — single source of truth
QBO_ACCOUNT_MAP = {
    "DISPATCH_FEE": "4000 — Dispatch Service Revenue",
    "SETTLEMENT":   "2000 — Carrier Settlements Payable",
    "FUEL_ADVANCE": "2100 — Driver Advances Payable",
    "LUMPER_ADVANCE": "2100 — Driver Advances Payable",
    "INVOICE":      "1200 — Accounts Receivable",
    "PAYMENT":      "1000 — Operating Checking",
    "EXPENSE":      "6000 — Operating Expenses",
}

# All event types synced per completed load
_ALL_LOAD_EVENTS = ("INVOICE", "PAYMENT", "DISPATCH_FEE", "SETTLEMENT")


# ─────────────────────────────────────────────────────────────
# SINGLE-ARG ORCHESTRATOR ENTRY POINT (imported by orchestrator.py)
# ─────────────────────────────────────────────────────────────

async def skill_t_quickbooks_sync(state: dict) -> dict:
    """
    Orchestrator node wrapper — syncs all financial events for a completed load.

    Signature: (state: dict) → dict
    Called by: orchestrator.py  run_quickbooks_sync()

    Loops through all 4 financial event types and delegates each to the
    2-arg helper skill_t_sync_to_quickbooks().

    COPILOT FIX: previously swallowed all errors and always returned
    qbo_synced=True. Now tracks per-event success/failure and returns:
      qbo_synced            True only if ALL events synced without error
      qbo_sync_status       "success" | "partial" | "failed"
      qbo_sync_events_attempted  int
      qbo_sync_events_succeeded  int
      qbo_sync_events_failed     int
      qbo_sync_errors            {event_type: error_str, ...}
    """
    sync_errors: dict[str, str] = {}
    succeeded_events: list[str] = []

    for event_type in _ALL_LOAD_EVENTS:
        try:
            state = await skill_t_sync_to_quickbooks(event_type, state)
            succeeded_events.append(event_type)
        except Exception as e:
            err_msg = f"{type(e).__name__}: {e}"
            logger.warning(f"[T] QBO sync failed for {event_type}: {err_msg}")
            sync_errors[event_type] = err_msg

    attempted  = len(_ALL_LOAD_EVENTS)
    n_success  = len(succeeded_events)
    n_failed   = len(sync_errors)

    if n_failed == 0:
        sync_status = "success"
        qbo_synced  = True
    elif n_success > 0:
        sync_status = "partial"
        qbo_synced  = False
    else:
        sync_status = "failed"
        qbo_synced  = False

    return {
        **state,
        "qbo_synced":                  qbo_synced,
        "qbo_sync_status":             sync_status,
        "qbo_sync_events_attempted":   attempted,
        "qbo_sync_events_succeeded":   n_success,
        "qbo_sync_events_failed":      n_failed,
        "qbo_sync_errors":             sync_errors,
    }


# ─────────────────────────────────────────────────────────────
# TWO-ARG HELPER (also used by orchestrator_phase2.py directly)
# ─────────────────────────────────────────────────────────────

async def skill_t_sync_to_quickbooks(event_type: str, state: dict) -> dict:
    """
    Sync a single financial event to QuickBooks Online.

    Signature: (event_type: str, state: dict) → dict
    Called by:
      - skill_t_quickbooks_sync() above (via loop)
      - orchestrator_phase2.py  run_post_payment_pipeline()

    In production this calls the QBO REST API.
    In dev / when QBO is unconfigured it logs the sync event only.
    """
    load_id = state.get("load_id", "")
    tms_ref = state.get("tms_ref", load_id)
    account = QBO_ACCOUNT_MAP.get(event_type, "9999 — Unclassified")

    amount = (
        state.get("dispatch_fee")
        or state.get("net_settlement")
        or state.get("invoice_amount")
        or state.get("amount_paid")
        or 0
    )

    logger.info(f"[T] QBO sync: {event_type} ${float(amount):.2f} | {tms_ref}")

    qbo_entity_id = None
    qbo_sync_error = None

    # Attempt live QBO sync if configured
    try:
        from cortexbot.integrations.quickbooks_client import (
            create_qbo_invoice,
            record_qbo_payment,
        )

        if settings_configured():
            if event_type == "INVOICE":
                broker_id      = state.get("broker_id", "1")
                invoice_number = state.get("invoice_number", f"INV-{load_id[:8]}")
                due_date       = (state.get("payment_due_date", "")[:10]
                                  if state.get("payment_due_date") else "")
                qbo_entity_id  = await create_qbo_invoice(
                    customer_ref=str(broker_id),
                    amount=float(amount),
                    description=f"Freight services — {tms_ref}",
                    doc_number=invoice_number,
                    due_date=due_date,
                )

            elif event_type == "PAYMENT" and state.get("qbo_invoice_id"):
                payment_date  = (
                    state.get("payment_received_date", "")[:10]
                    or datetime.now().strftime("%Y-%m-%d")
                )
                qbo_entity_id = await record_qbo_payment(
                    invoice_id=state["qbo_invoice_id"],
                    customer_ref=str(state.get("broker_id", "1")),
                    amount=float(amount),
                    payment_date=payment_date,
                )
    except Exception as e:
        logger.warning(f"[T] Live QBO sync failed: {e} — event logged only")
        qbo_sync_error = e

    # Always log the sync event regardless of live API result
    async with get_db_session() as db:
        db.add(Event(
            event_code=f"QBO_SYNC_{event_type}",
            entity_type="load",
            entity_id=load_id,
            triggered_by="st_quickbooks_sync",
            data={
                "event_type":    event_type,
                "qbo_account":   account,
                "qbo_entity_id": qbo_entity_id,
                "amount":        float(amount),
                "synced_at":     datetime.now(timezone.utc).isoformat(),
            },
        ))

    if qbo_sync_error:
        raise qbo_sync_error

    updated = {**state, "qbo_synced": True, "qbo_event_type": event_type}
    if qbo_entity_id:
        updated["qbo_invoice_id"] = qbo_entity_id

    return updated


def settings_configured() -> bool:
    """Return True only if QBO is fully configured."""
    try:
        from cortexbot.config import settings
        return bool(settings.quickbooks_client_id and settings.quickbooks_company_id)
    except Exception:
        return False