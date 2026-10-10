"""Tiny JSON-file persistence for agent records.

Lock-guarded and atomic (write-tmp + rename) so it survives crashes and
concurrent web requests. One record per AI-agent home.
"""

from __future__ import annotations

import json
import os
import threading
import time


class Store:
    """A very small JSON document store keyed by agent id."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self.agents: dict[str, dict] = {}
        self._load()

    # ------------------------------------------------------------------ io
    def _load(self) -> None:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                self.agents = {a["id"]: a for a in data.get("agents", []) if a.get("id")}
            except (json.JSONDecodeError, OSError, KeyError, TypeError):
                self.agents = {}

    def _save(self) -> None:
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"agents": list(self.agents.values())}, fh, indent=2)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------- records
    def add(self, agent: dict) -> dict:
        with self._lock:
            self.agents[agent["id"]] = agent
            self._save()
            return agent

    def get(self, agent_id: str) -> dict | None:
        with self._lock:
            return self.agents.get(agent_id)

    def update(self, agent_id: str, **fields) -> dict | None:
        with self._lock:
            agent = self.agents.get(agent_id)
            if agent is None:
                return None
            agent.update(fields)
            agent["updated_at"] = int(time.time())
            self._save()
            return agent

    def all(self) -> list[dict]:
        with self._lock:
            return list(self.agents.values())

    def free_tier_used(self) -> int:
        """Number of *live* agents on the free tier (destroyed homes free their slot)."""
        return sum(
            1 for a in self.all()
            if a.get("free_tier") and a.get("status") != "destroyed"
        )

    def hostname_taken(self, hostname: str) -> bool:
        active = {"quote_required", "awaiting_payment", "provisioning", "running"}
        return any(
            a.get("hostname") == hostname and a.get("status") in active
            for a in self.all()
        )

    def alive_count(self) -> int:
        return sum(
            1 for a in self.all() if a.get("status") in {"provisioning", "running"}
        )