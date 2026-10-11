"""Stablecoin quote engine.

First homes are free (see `billing.free_tier_containers`). Every
subsequent web-provisioned container gets a machine-readable quote
denominated in USDC/USDT: a fixed price, a receive address and a unique
`reference` the customer puts in the payment memo so the operator can
match the on-chain transfer to the agent home.

Real on-chain confirmation (e.g. scanning the chain or a payment
provider webhook) plugs into `POST /api/agents/<id>/confirm-payment`.
"""

from __future__ import annotations

import hashlib
import time


def _pick_asset(agent_id: str, assets: list[str]) -> str:
    if not assets:
        return "USDC"
    digest = int.from_bytes(hashlib.sha256(agent_id.encode()).digest()[:4], "big")
    return assets[digest % len(assets)]


def create_quote(agent_id: str, billing: dict, amount_usd: float | None = None,
                 plan: str | None = None) -> dict:
    """Build a quote dict for one agent home.

    The amount is the plan's deposit (price/hr x deposit_hours), computed
    by the caller; the customer sends it with memo=reference and the home
    is credited that amount on confirmation (see billing.renew-free path).
    """
    assets = billing.get("accepted_assets") or ["USDC", "USDT"]
    asset = _pick_asset(agent_id, assets)
    amount = float(amount_usd) if amount_usd is not None else 0.0
    chain_info = billing.get("chains", {}).get(asset, {})
    addresses = billing.get("receive_addresses", {})
    valid_hours = int(billing.get("quote_valid_hours", 72))

    quote = {
        "id": f"qt-{agent_id}",
        "asset": asset,
        "amount": f"{amount:.2f}",
        "plan": plan,
        "chain": chain_info.get("chain", "ethereum"),
        "network": chain_info.get("network", "ERC-20"),
        "address": addresses.get(asset, ""),
        "reference": agent_id,
        "status": "pending",
        "created_at": int(time.time()),
        "valid_until": int(time.time()) + valid_hours * 3600,
    }
    quote["note"] = (
        f"Send exactly {quote['amount']} {quote['asset']} on "
        f"{quote['chain']} ({quote['network']}) to {quote['address']} "
        f"with memo {quote['reference']}. This buys {float(amount):.2f} USD "
        f"of metered run time at the {plan or ''} plan rate."
    )
    return quote


def confirm(quote: dict, agent_id: str) -> dict:
    """Mark a quote paid. Returns the updated quote."""
    quote["status"] = "paid"
    quote["confirmed_at"] = int(time.time())
    quote["paid_for"] = agent_id
    return quote


def cancel(quote: dict) -> dict:
    if quote.get("status") == "pending":
        quote["status"] = "cancelled"
        quote["cancelled_at"] = int(time.time())
    return quote