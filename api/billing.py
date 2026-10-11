"""Metered billing: a per-second ticker over running homes, a ledger, and
statements.

Every home is created from a plan (see plans.py) at a `price_usd_per_hour`
recorded on the agent. Credit is bought up front:

  * free-tier homes get `billing.free_credit_usd` (0 => unmetered),
  * paid homes get the confirmed quote amount when payment lands
    (confirm-payment / a future webhook).

The watchdog ticks each metered, running home: elapsed seconds x price / 3600
are debited from credit into a per-agent ledger. Beneath
`billing.low_balance_warn_usd` the home is flagged `balance_low`; at zero
credit the container is stopped and the home `halted`. `renew()` re-adds
credit and, for a halted home, restarts the container.

Precision is per-second (fractional seconds are charged); the write cadence
is `billing.tick_interval_s` so the JSON store isn't churned every poll.
Elapsed time is capped at `billing.max_backfill_s` so a multi-day outage
does not bill for wall-clock time the operator could not have intervened on.
"""

from __future__ import annotations

import logging
import time

from proxmox import Proxmox, ProxmoxError  # noqa: F401  (type annotations)

log = logging.getLogger("agenc.billing")

LEDGER_CAP = 200  # keep the newest N ledger entries on the record


# ------------------------------------------------------------- queries
def credit(agent: dict) -> float:
    return float(agent.get("credit_usd") or 0.0)


def billed(agent: dict) -> float:
    return float(agent.get("billed_total_usd") or 0.0)


def balance_usd(agent: dict) -> float:
    return round(credit(agent) - billed(agent), 4)


def metered(agent: dict) -> bool:
    """Is this home charged for running time? (Stored flag; credit may be 0.)"""
    return bool(agent.get("metered"))


def warn_floor(cfg: dict) -> float:
    return float(cfg.get("billing", {}).get("low_balance_warn_usd", 0.25))


def tick_interval(cfg: dict) -> float:
    return float(cfg.get("billing", {}).get("tick_interval_s", 60))


def max_backfill(cfg: dict) -> float:
    return float(cfg.get("billing", {}).get("max_backfill_s", 86400))


# -------------------------------------------------------------- ticking
def tick(store, cfg: dict, px: Proxmox, agent: dict) -> None:
    """One metering pass for `agent`. No-op unless running + metered + due.

    Charges fractional seconds since `last_tick_at` at the plan's hourly
    price, appends a ledger entry, and halts the home at zero credit.
    Self-initializes `meter_started_at`/`last_tick_at` on the first pass.
    """
    if agent.get("status") != "running" or not metered(agent):
        return
    price = float(agent.get("price_per_hour") or 0.0)
    if price <= 0:
        return
    now = time.time()
    last = float(agent.get("last_tick_at") or 0.0)
    if not last:
        store.update(agent["id"], meter_started_at=now, last_tick_at=now)
        return
    if now - last < tick_interval(cfg):
        return  # keep the write cadence; seconds still count from last_tick

    elapsed = min(now - last, max_backfill(cfg))
    if elapsed < 1:
        store.update(agent["id"], last_tick_at=now)
        return
    amount = round(price * elapsed / 3600.0, 6)
    if amount <= 0:
        store.update(agent["id"], last_tick_at=now)
        return

    new_billed = round(billed(agent) + amount, 6)
    balance = round(credit(agent) - new_billed, 6)
    entry = {"at": int(now), "seconds": round(elapsed, 1), "amount_usd": amount}
    ledger = list(agent.get("ledger") or [])
    ledger.append(entry)
    if len(ledger) > LEDGER_CAP:
        ledger = ledger[-LEDGER_CAP:]

    update = {
        "billed_total_usd": new_billed,
        "credit_usd": credit(agent),
        "ledger": ledger,
        "last_tick_at": now,
        "balance_low": 0 < balance <= warn_floor(cfg),
    }
    if balance <= 0:
        # clip the overrun: billed never exceeds credit
        update["billed_total_usd"] = credit(agent)
        update["credit_usd"] = credit(agent)
        update["balance_low"] = False
        update["status"] = "halted"
        update["halted_at"] = int(now)
        vmid = agent.get("vmid")
        if vmid:
            try:
                px.stop_lxc(vmid)
            except ProxmoxError as exc:
                log.warning("halt %s (vmid %s): could not stop container: %s",
                            agent["id"], vmid, exc)
        log.warning("agent %s halted — credit exhausted (billed $%.2f @ $%.2f/hr)",
                    agent["id"], update["billed_total_usd"], price)
    store.update(agent["id"], **update)


# ---------------------------------------------------------- statements
def statement(cfg: dict, agent: dict) -> dict:
    """A per-home billing statement: summary + newest-first ledger."""
    return {
        "agent_id": agent["id"],
        "hostname": agent.get("hostname"),
        "status": agent.get("status"),
        "plan": agent.get("plan"),
        "price_usd_per_hour": agent.get("price_per_hour"),
        "metered": metered(agent),
        "credit_usd": credit(agent),
        "billed_total_usd": billed(agent),
        "balance_usd": balance_usd(agent),
        "balance_low": bool(agent.get("balance_low")),
        "meter_started_at": agent.get("meter_started_at"),
        "last_tick_at": agent.get("last_tick_at"),
        "halted_at": agent.get("halted_at"),
        "tick_interval_s": tick_interval(cfg),
        "warn_floor_usd": warn_floor(cfg),
        "entries": list(reversed((agent.get("ledger") or [])[-LEDGER_CAP:])),
    }


# --------------------------------------------------------------- renew
def renew(store, cfg: dict, px: Proxmox, agent: dict, amount_usd: float) -> dict:
    """Add credit; restart a halted home. Returns the updated agent record."""
    amount = round(float(amount_usd), 4)
    new_credit = round(credit(agent) + amount, 4)
    entry = {"at": int(time.time()), "renew_usd": amount}
    ledger = list(agent.get("ledger") or [])
    ledger.append(entry)
    if len(ledger) > LEDGER_CAP:
        ledger = ledger[-LEDGER_CAP:]
    update = {"credit_usd": new_credit, "ledger": ledger, "balance_low": False}
    if agent.get("status") == "halted":
        update["status"] = "running"
        update["halted_at"] = None
        update["last_tick_at"] = time.time()
        vmid = agent.get("vmid")
        if vmid:
            try:
                px.start_lxc(vmid)
            except ProxmoxError as exc:
                update["status"] = "error"
                update["error"] = f"renew: could not restart container: {exc}"
                log.warning("renew %s: restart failed: %s", agent["id"], exc)
    store.update(agent["id"], **update)
    log.info("agent %s renewed +$%.2f (credit $%.2f, status %s)",
             agent["id"], amount, new_credit, store.get(agent["id"]).get("status"))
    return store.get(agent["id"])