"""Plan tiers: RAM/disk/cores -> USD per month, billed up front per period.

Customers pay in advance for a billing period — weekly, fortnightly or
monthly — at the plan's monthly rate (periods are configurable fractions of
a month: 0.25 / 0.5 / 1.0). The LXC payload is sized from the plan specs,
and billing.py tracks each home's prepaid window (`paid_until`).

The "per-hour equivalent" is exposed in the catalog for orientation only
(price / 730 avg hours) — nothing is charged per hour.
"""

from __future__ import annotations

PLAN_KEYS = ("memory_mb", "disk_gb", "cores", "cpuunits", "price_usd_per_month")

# Average hours in a month, used only for the informational $-per-hour
# equivalent in the public catalog.
HOURS_PER_MONTH = 730.0

DEFAULT_PLANS: dict[str, dict] = {
    "micro":    {"memory_mb": 1024, "disk_gb": 30, "cores": 1,
                 "cpuunits": 256,  "price_usd_per_month": 8.0},
    "nano":     {"memory_mb": 4096, "disk_gb": 50, "cores": 4,
                 "cpuunits": 1024, "price_usd_per_month": 16.0},
    "standard": {"memory_mb": 8192, "disk_gb": 64, "cores": 8,
                 "cpuunits": 2048, "price_usd_per_month": 32.0},
}

DEFAULT_PERIODS: dict[str, dict] = {
    "weekly":      {"days": 7,  "months": 0.25},
    "fortnightly": {"days": 14, "months": 0.5},
    "monthly":     {"days": 30, "months": 1.0},
}


def plans(cfg: dict) -> dict[str, dict]:
    """All configured plans, normalized to PLAN_KEYS (missing -> defaults)."""
    raw = cfg.get("plans") or DEFAULT_PLANS
    out: dict[str, dict] = {}
    for pid, spec in raw.items():
        if not isinstance(spec, dict):
            continue
        out[str(pid)] = {k: spec.get(k, DEFAULT_PLANS.get(str(pid), {}).get(k))
                         for k in PLAN_KEYS}
    return out


def periods(cfg: dict) -> dict[str, dict]:
    """Billing periods: name -> {days, months fraction of the monthly price}."""
    raw = cfg.get("billing", {}).get("periods") or DEFAULT_PERIODS
    out: dict[str, dict] = {}
    for name, p in raw.items():
        if isinstance(p, dict) and p.get("days"):
            out[str(name)] = {"days": int(p["days"]),
                              "months": float(p.get("months", 1.0))}
    return out or dict(DEFAULT_PERIODS)


def default_period(cfg: dict) -> str:
    return str(cfg.get("billing", {}).get("default_period") or "monthly")


def get_period(cfg: dict, period_id: str | None) -> tuple[str | None, str | None]:
    """Resolve a requested period -> (name, None) or (None, error message)."""
    known = periods(cfg)
    name = str(period_id or "").strip().lower() or default_period(cfg)
    if name not in known:
        return None, f"unknown billing period '{name}' — pick one of: {', '.join(sorted(known))}"
    return name, None


def default_plan(cfg: dict) -> str:
    return str(cfg.get("billing", {}).get("default_plan") or "standard")


def get_plan(cfg: dict, plan_id: str | None) -> tuple[dict | None, str | None]:
    """Resolve a requested plan id -> (spec, None) or (None, error message)."""
    known = plans(cfg)
    pid = str(plan_id or "").strip().lower() or default_plan(cfg)
    spec = known.get(pid)
    if spec is None:
        return None, f"unknown plan '{pid}' — pick one of: {', '.join(sorted(known))}"
    return spec, None


def period_price(cfg: dict, spec: dict, period: str) -> float:
    """USD owed for one period of a plan: monthly price x period fraction."""
    known = periods(cfg)
    months = known.get(period, {}).get("months", 1.0)
    return round(float(spec["price_usd_per_month"]) * months, 2)


def days_of(cfg: dict, period: str) -> int:
    return int(periods(cfg).get(period, {}).get("days", 30))


def price_per_hour_equiv(cfg: dict, spec: dict) -> float:
    """Informational only: monthly price / avg hours in a month."""
    return round(float(spec["price_usd_per_month"]) / HOURS_PER_MONTH, 3)


def plans_catalog(cfg: dict) -> list[dict]:
    """Public, sorted plan list: id, specs, $/mo, period prices, $-hr equiv."""
    known = periods(cfg)
    out = []
    for pid, spec in plans(cfg).items():
        row = {"id": pid}
        row.update({k: spec[k] for k in PLAN_KEYS})
        row["price_usd_per_hour_equiv"] = price_per_hour_equiv(cfg, spec)
        row["period_prices"] = {
            name: period_price(cfg, spec, name) for name in sorted(known)
        }
        out.append(row)
    return sorted(out, key=lambda r: r["price_usd_per_month"])


def periods_catalog(cfg: dict) -> dict[str, dict]:
    return {name: dict(p) for name, p in periods(cfg).items()}