"""Prepaid period billing: pay up front weekly / fortnightly / monthly.

Customers buy billing periods at the plan's monthly rate (plans.py). Each
home carries `paid_until` — when the prepaid window lapses, the watchdog
flags `expiring` (near the end) and then stops the container and `halt`s
the home. `renew()` extends the window and restarts a halted home.

Nothing is metered per second; a ledger keeps the purchase history for the
per-home statement (and, later, for the Phase-1 account balance).
"""

from __future__ import annotations

import logging
import time

from proxmox import ProxmoxError  # noqa: F401  (type annotations)

log = logging.getLogger("agenc.billing")

LEDGER_CAP = 200  # keep the newest N ledger entries on the record


# ------------------------------------------------------------- queries
def billed(agent: dict) -> float:
    return float(agent.get("billed_total_usd") or 0.0)


def paid_until(agent: dict) -> float:
    return float(agent.get("paid_until") or 0.0)


def remaining_s(agent: dict) -> int | None:
    """Seconds until the prepaid window lapses (negative => expired)."""
    until = paid_until(agent)
    if not until:
        return None  # dev mode: no expiry
    return int(until - time.time())


def warn_before_s(cfg: dict) -> float:
    return float(cfg.get("billing", {}).get("warn_before_s", 86400))


# ----------------------------------------------------------- credits
def credit_periods(agent: dict, count: int, days: int, price: float = 0.0,
                   label: str = "period") -> dict:
    """Add `count` prepaid periods to `agent` in place (creation helper).

    Used for free-tier sign-up periods (price 0) and the paid quote on
    confirmation. Mutates and returns the agent dict.
    """
    now = time.time()
    base = max(float(agent.get("paid_until") or 0.0), now)
    agent["paid_until"] = base + count * int(days) * 86400
    agent["periods_bought"] = int(agent.get("periods_bought") or 0) + count
    agent["billed_total_usd"] = round(billed(agent) + float(price) * count, 2)
    ledger = list(agent.get("ledger") or [])
    ledger.append({"at": int(now), "days": int(days) * count,
                   "amount_usd": round(float(price) * count, 2), "note": label})
    if len(ledger) > LEDGER_CAP:
        ledger = ledger[-LEDGER_CAP:]
    agent["ledger"] = ledger
    agent["expiring"] = False
    return agent


# -------------------------------------------------------------- ticking
def tick(store, cfg: dict, px, agent: dict) -> None:
    """One expiry pass for `agent`. No-op unless running with a paid_until.

    Sets `expiring` as the prepaid window runs out; at the boundary (or
    past it) the container is stopped and the home `halted`. Billing stops
    with the halt — buy more time to resume.
    """
    if agent.get("status") != "running":
        return
    until = paid_until(agent)
    if not until:
        return  # dev mode home: no expiry
    now = time.time()
    left = until - now
    if left <= 0:
        store.update(
            agent["id"], status="halted", halted_at=int(now), expiring=False,
            last_tick_at=now)
        vmid = agent.get("vmid")
        if vmid:
            try:
                px.stop_lxc(vmid)
            except ProxmoxError as exc:
                log.warning("halt %s (vmid %s): could not stop container: %s",
                            agent["id"], vmid, exc)
        log.warning("agent %s halted — prepaid period lapsed "
                    "(%d days at $%.2f/mo; renew to resume)",
                    agent["id"], int(agent.get("period_days") or 30),
                    float(agent.get("price_usd_per_month") or 0))
        return
    expiring = left <= warn_before_s(cfg)
    if bool(agent.get("expiring")) != expiring:
        store.update(agent["id"], expiring=expiring)


# ---------------------------------------------------------- statements
def statement(cfg: dict, agent: dict) -> dict:
    """A per-home statement: period window + newest-first purchase ledger."""
    return {
        "agent_id": agent["id"],
        "hostname": agent.get("hostname"),
        "status": agent.get("status"),
        "plan": agent.get("plan"),
        "price_usd_per_month": agent.get("price_usd_per_month"),
        "billing_period": agent.get("billing_period"),
        "period_days": agent.get("period_days"),
        "period_price_usd": agent.get("period_price_usd"),
        "paid_until": paid_until(agent) or None,
        "remaining_s": remaining_s(agent),
        "expiring": bool(agent.get("expiring")),
        "periods_bought": agent.get("periods_bought", 0),
        "billed_total_usd": billed(agent),
        "warn_before_s": warn_before_s(cfg),
        "entries": list(reversed((agent.get("ledger") or [])[-LEDGER_CAP:])),
    }


# --------------------------------------------------------------- renew
def renew(store, cfg: dict, px, agent: dict, days: int,
          amount_usd: float = 0.0, label: str = "renew", count: int = 1) -> dict:
    """Extend the prepaid window; restart a halted home. Returns the record.

    `days`/`amount_usd` are recorded for the ledger, `label` describes what
    was bought (e.g. "weekly x2", "14 days"), and `count` is how many
    billing-period records this purchase represents.
    """
    days = max(1, min(int(days), 366))
    now = time.time()
    base = max(paid_until(agent), now) if agent.get("status") != "halted" else now
    new_until = base + days * 86400
    entry = {"at": int(now), "days": days, "amount_usd": round(float(amount_usd), 2),
             "note": label}
    ledger = list(agent.get("ledger") or [])
    ledger.append(entry)
    if len(ledger) > LEDGER_CAP:
        ledger = ledger[-LEDGER_CAP:]
    update = {
        "paid_until": new_until,
        "periods_bought": int(agent.get("periods_bought") or 0) + count,
        "billed_total_usd": round(billed(agent) + float(amount_usd), 2),
        "ledger": ledger,
        "expiring": False,
        "last_tick_at": now,
    }
    if agent.get("status") == "halted":
        update["status"] = "running"
        update["halted_at"] = None
        vmid = agent.get("vmid")
        if vmid:
            try:
                px.start_lxc(vmid)
            except ProxmoxError as exc:
                update["status"] = "error"
                update["error"] = f"renew: could not restart container: {exc}"
                log.warning("renew %s: restart failed: %s", agent["id"], exc)
    store.update(agent["id"], **update)
    log.info("agent %s renewed +%d days (until %s, status %s)",
             agent["id"], days, time.strftime("%Y-%m-%d", time.localtime(new_until)),
             store.get(agent["id"]).get("status"))
    return store.get(agent["id"])