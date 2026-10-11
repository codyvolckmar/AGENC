"""Plan tiers: RAM/disk/cores -> USD/hour.

Sizing used to be baked into config (`container.memory_mb/disk_gb/cores`).
Now every home is created from a named plan: the API validates the plan,
the LXC payload is sized from its specs, and the billing ticker (billing.py)
charges its `price_usd_per_hour`. Plans live in `config.json` under `plans`;
the defaults below mirror the original home defaults (micro/nano/standard).
"""

from __future__ import annotations

PLAN_KEYS = ("memory_mb", "disk_gb", "cores", "cpuunits", "price_usd_per_hour")

DEFAULT_PLANS: dict[str, dict] = {
    # cores: 0 = all host cores, shared (0 is a real value, not "unset")
    "micro":    {"memory_mb": 512,  "disk_gb": 4, "cores": 1,
                 "cpuunits": 256,  "price_usd_per_hour": 0.05},
    "nano":     {"memory_mb": 1024, "disk_gb": 6, "cores": 2,
                 "cpuunits": 512,  "price_usd_per_hour": 0.10},
    "standard": {"memory_mb": 2048, "disk_gb": 8, "cores": 0,
                 "cpuunits": 1024, "price_usd_per_hour": 0.30},
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


def default_plan(cfg: dict) -> str:
    return str(cfg.get("billing", {}).get("default_plan") or "standard")


def get_plan(cfg: dict, plan_id: str | None) -> tuple[dict | None, str | None]:
    """Resolve a requested plan id -> (spec, None) or (None, error message).

    A missing/empty id resolves to the configured default plan.
    """
    known = plans(cfg)
    pid = str(plan_id or "").strip().lower() or default_plan(cfg)
    spec = known.get(pid)
    if spec is None:
        return None, f"unknown plan '{pid}' — pick one of: {', '.join(sorted(known))}"
    return spec, None


def deposit_hours(cfg: dict) -> float:
    """How many hours of run time a paid home buys in one quote/deposit."""
    return float(cfg.get("billing", {}).get("deposit_hours", 24))


def quote_amount(cfg: dict, spec: dict) -> float:
    """USD quote for a paid home = plan price x deposit hours."""
    return round(float(spec["price_usd_per_hour"]) * deposit_hours(cfg), 2)


def plans_catalog(cfg: dict) -> list[dict]:
    """Public, sorted plan list: id, specs, price/hr, deposit."""
    out = []
    deposit = deposit_hours(cfg)
    for pid, spec in plans(cfg).items():
        row = {"id": pid}
        row.update({k: spec[k] for k in PLAN_KEYS})
        row["deposit_usd"] = round(float(spec["price_usd_per_hour"]) * deposit, 2)
        out.append(row)
    return sorted(out, key=lambda r: r["price_usd_per_hour"])